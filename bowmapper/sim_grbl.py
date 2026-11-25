"""A simulated Grbl_ESP32 1.3a controller, close enough to drive the whole app.

Models: alarm lock at power-up, homing with pull-off, jog / jog cancel,
feed hold / resume, soft reset (ALARM:3 if reset while moving), soft and hard
limits, check mode, work offsets (G10 L2/L20, G92), the 128-byte serial
receive buffer, G4 waiting for the planner to drain, and status reports.

The machine has a *physical* position (where the head really is) and a
reported machine position. They differ until the machine is homed, exactly
like a real controller that powers up thinking it is at 0,0,0.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque

DEFAULT_SETTINGS = {
    0: "10", 1: "25", 2: "0", 3: "5", 4: "0", 5: "1", 6: "0", 10: "1", 11: "0.010",
    12: "0.002", 13: "0", 20: "0", 21: "1", 22: "1", 23: "3", 24: "25.000",
    25: "1000.000", 26: "250.000", 27: "2.000", 30: "1000.000", 31: "0.000", 32: "0",
    39: "0", 100: "800.000", 101: "800.000", 102: "800.000", 103: "800.000",
    110: "4000.000", 111: "4000.000", 112: "800.000", 113: "2000.000",
    120: "50.000", 121: "50.000", 122: "50.000", 123: "50.000",
    130: "400.000", 131: "400.000", 132: "130.000", 133: "400.000",
}

WORD_RE = re.compile(r"([A-Z])([-+]?[0-9]*\.?[0-9]+)")
RX_SIZE = 128
PLANNER_SIZE = 16


class Block:
    __slots__ = ("target", "feed", "kind", "dwell")

    def __init__(self, target, feed, kind, dwell=0.0):
        self.target = target        # machine coords
        self.feed = feed            # mm/min
        self.kind = kind            # line | rapid | jog | dwell
        self.dwell = dwell


class SimGrbl:
    def __init__(self, time_scale: float = 1.0, start_phys=(123.4, 210.0, -40.0),
                 homing_lock: bool = True):
        self.ts = time_scale
        self.settings = dict(DEFAULT_SETTINGS)
        self.lock = threading.RLock()
        self._out = None
        self.phys = list(start_phys)
        self.offset = list(start_phys)      # phys - mpos (unknown until homed)
        self.g54 = [0.0, 0.0, 0.0]
        self.g92 = [0.0, 0.0, 0.0]
        self.homing_lock = homing_lock
        self.overflows = 0
        self.max_rx = 0
        self._booted = False
        self._reset_state(power_up=True)
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True, name="sim-grbl")
        self._t.start()

    # ---------------------------------------------------------------- wiring
    def attach(self, out_fn) -> None:
        with self.lock:
            self._out = out_fn
            if out_fn and not self._booted:   # first attach = power-up banner
                self._booted = True
                self._banner()

    def _emit(self, text: str) -> None:
        if self._out:
            self._out((text + "\r\n").encode())

    def _banner(self) -> None:
        self._emit("")
        self._emit("Grbl 1.3a ['$' for help]")
        if self.state == "Alarm":
            self._emit("[MSG:'$H'|'$X' to unlock]")

    def close(self) -> None:
        self._stop = True

    # ---------------------------------------------------------------- state
    def _reset_state(self, power_up=False) -> None:
        self.rx = bytearray()
        self.lines: deque[bytes] = deque()
        self.planner: deque[Block] = deque()
        self.hold = False
        self.check = False
        self.m0_pending = False
        self.homing = None
        self.distance = "G90"
        self.units = "G21"
        self.motion = "G0"
        self.feed = 0.0
        self.cur_feed = 0.0
        self.feed_ov = 100
        self.rapid_ov = 100
        self.report_n = 0
        self.wco_dirty = True
        self.dwell_until = None
        if power_up:
            self.g92 = [0.0, 0.0, 0.0]
            self.alarm = None
            self.state = "Alarm" if (self._b(22) and self.homing_lock) else "Idle"
        elif self.state not in ("Alarm",):
            self.state = "Idle"

    def _f(self, n) -> float:
        return float(self.settings[n])

    def _b(self, n) -> bool:
        return self._f(n) != 0

    @property
    def mpos(self):
        return [p - o for p, o in zip(self.phys, self.offset)]

    def limits(self):
        """Machine-coordinate travel, Grbl_ESP32 convention (home_mpos = 0)."""
        mask = int(self._f(23))
        out = []
        for i in range(3):
            travel = self._f(130 + i)
            out.append((0.0, travel) if mask & (1 << i) else (-travel, 0.0))
        return out

    def phys_limits(self):
        # The physical frame equals the homed machine frame.
        return self.limits()

    # ---------------------------------------------------------------- input
    def receive(self, data: bytes) -> None:
        with self.lock:
            for b in data:
                if b == ord("?"):
                    self._status()
                elif b == ord("!"):
                    if self.state in ("Run", "Jog", "Home"):
                        if self.state == "Jog":
                            self._jog_cancel()
                        elif self.state == "Run":
                            self.hold = True
                elif b == ord("~"):
                    if self.hold or self.state.startswith("Hold"):
                        self.hold = False
                        self.m0_pending = False
                elif b == 0x18:
                    self._soft_reset()
                elif b == 0x85:
                    self._jog_cancel()
                elif b == 0x90:
                    self.feed_ov = 100
                elif b == 0x91:
                    self.feed_ov = min(200, self.feed_ov + 10)
                elif b == 0x92:
                    self.feed_ov = max(10, self.feed_ov - 10)
                elif b == 0x93:
                    self.feed_ov = min(200, self.feed_ov + 1)
                elif b == 0x94:
                    self.feed_ov = max(10, self.feed_ov - 1)
                elif b == 0x95:
                    self.rapid_ov = 100
                elif b == 0x96:
                    self.rapid_ov = 50
                elif b == 0x97:
                    self.rapid_ov = 25
                elif b >= 0x80:
                    pass
                else:
                    self.rx.append(b)
                    if b == ord("\n"):
                        line = bytes(self.rx)
                        self.rx = bytearray()
                        self.lines.append(line)
            used = len(self.rx) + sum(len(x) for x in self.lines)
            self.max_rx = max(self.max_rx, used)
            if used > RX_SIZE:
                self.overflows += 1

    # ---------------------------------------------------------------- realtime
    def _jog_cancel(self) -> None:
        if self.state == "Jog" or any(b.kind == "jog" for b in self.planner):
            self.planner = deque(b for b in self.planner if b.kind != "jog")
            if not self.planner:
                self.state = "Idle"

    def _soft_reset(self) -> None:
        moving = bool(self.planner) and not self.hold and self.state in ("Run", "Jog")
        homing = self.homing is not None
        if moving or homing:
            self.alarm = 6 if homing else 3
            self.state = "Alarm"
        self._reset_state()
        if self.alarm in (3, 6):
            self.state = "Alarm"
            self._emit(f"ALARM:{self.alarm}")
        self._banner()

    def _alarm(self, code: int) -> None:
        self.alarm = code
        self.state = "Alarm"
        self.planner.clear()
        self.lines.clear()
        self.rx = bytearray()
        self.homing = None
        self._emit(f"ALARM:{code}")
        self._reset_state()
        self.state = "Alarm"
        self._banner()

    def _status(self) -> None:
        st = self.state
        if self.check:
            st = "Check"
        elif self.hold or (self.m0_pending and not self.planner):
            st = "Hold:0"
        mp = self.mpos
        parts = [st, "MPos:" + ",".join(f"{v:.3f}" for v in mp),
                 f"FS:{self.cur_feed:.0f},0"]
        self.report_n += 1
        if self.wco_dirty or self.report_n % 10 == 0:
            wco = [a + b for a, b in zip(self.g54, self.g92)]
            parts.append("WCO:" + ",".join(f"{v:.3f}" for v in wco))
            self.wco_dirty = False
        if self.report_n % 10 == 5:
            parts.append(f"Ov:{self.feed_ov},{self.rapid_ov},100")
        pins = ""
        lim = self.limits()
        for i, axis in enumerate("XYZ"):
            lo, hi = lim[i]
            p = self.phys[i]
            if (int(self._f(23)) >> i) & 1:
                if p <= lo + 0.001:
                    pins += axis
            elif p >= hi - 0.001:
                pins += axis
        if pins:
            parts.append("Pn:" + pins)
        self._emit("<" + "|".join(parts) + ">")

    # ---------------------------------------------------------------- main loop
    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop:
            time.sleep(0.004)
            now = time.monotonic()
            dt = (now - last) * self.ts
            last = now
            with self.lock:
                self._process_lines()
                self._step(dt)

    def _process_lines(self) -> None:
        while self.lines:
            raw = self.lines[0]
            line = raw.decode(errors="replace").strip().upper().replace(" ", "")
            line = re.sub(r"\(.*?\)", "", line).split(";")[0]
            if self.m0_pending or self.homing is not None:
                return              # M0 pause / homing: GRBL parses nothing else until done
            # Lines that must wait for the planner before they run.
            if re.match(r"^(N\d+)?(G0?4(?!\d)|M0?[01](?!\d))", line):
                if self.planner and not self.check:
                    return
            if line and not line.startswith("$") and len(self.planner) >= PLANNER_SIZE:
                return
            if self.dwell_until is not None:
                return
            self.lines.popleft()
            if not line:
                self._emit("ok")
                continue
            try:
                resp = self._execute(line)
            except _GErr as e:
                self._emit(f"error:{e.code}")
                continue
            if resp == "defer":
                continue
            if resp is not None:
                self._emit(resp)

    # ---------------------------------------------------------------- commands
    def _execute(self, line: str):
        if line.startswith("$"):
            return self._system(line)
        if self.state == "Alarm":
            raise _GErr(9)
        if self.state == "Jog":
            raise _GErr(9)
        return self._gcode(line)

    def _system(self, line: str):
        if line == "$$":
            if self.state not in ("Idle", "Alarm") and not self.check:
                raise _GErr(8)
            for k in sorted(self.settings):
                self._emit(f"${k}={self.settings[k]}")
            return "ok"
        if line == "$I":
            self._emit("[VER:1.3a.20231030:]")
            self._emit("[OPT:PHSW]")
            self._emit("[MSG:Using machine:Simulated LUNYEE 4040 Nova]")
            return "ok"
        if line == "$G":
            self._emit(f"[GC:{self.motion} G54 G17 {self.units} {self.distance} G94 M5 M9 T0 F{self.feed:g} S0]")
            return "ok"
        if line == "$#":
            self._emit("[G54:" + ",".join(f"{v:.3f}" for v in self.g54) + "]")
            for g in ("G55", "G56", "G57", "G58", "G59", "G28", "G30"):
                self._emit(f"[{g}:0.000,0.000,0.000]")
            self._emit("[G92:" + ",".join(f"{v:.3f}" for v in self.g92) + "]")
            self._emit("[TLO:0.000]")
            self._emit("[PRB:0.000,0.000,0.000:0]")
            return "ok"
        if line == "$H":
            if not self._b(22):
                raise _GErr(5)
            if self.state not in ("Idle", "Alarm"):
                raise _GErr(8)
            self._start_homing()
            return "defer"
        if line == "$X":
            if self.state == "Alarm":
                self.state = "Idle"
                self.alarm = None
                self._emit("[MSG:Caution: Unlocked]")
            return "ok"
        if line == "$C":
            if self.check:
                self.check = False
                self._emit("[MSG:Disabled]")
                self._emit("ok")
                self._reset_state()
                self._banner()
                return None
            if self.state != "Idle":
                raise _GErr(8)
            self.check = True
            self._emit("[MSG:Enabled]")
            return "ok"
        if line.startswith("$J="):
            return self._jog(line[3:])
        m = re.match(r"^\$(\d+)=([-+]?[0-9]*\.?[0-9]+)$", line)
        if m:
            if self.state not in ("Idle", "Alarm"):
                raise _GErr(8)
            k = int(m.group(1))
            if k not in self.settings:
                raise _GErr(3)
            v = float(m.group(2))
            self.settings[k] = f"{v:.3f}" if "." in self.settings[k] else str(int(v))
            return "ok"
        if line in ("$SLP", "$RST=$", "$RST=#", "$RST=*"):
            return "ok"
        raise _GErr(3)

    def _words(self, text: str) -> list[tuple[str, float]]:
        pos = 0
        out = []
        for m in WORD_RE.finditer(text):
            if m.start() != pos:
                raise _GErr(1)
            out.append((m.group(1), float(m.group(2))))
            pos = m.end()
        if pos != len(text):
            raise _GErr(1 if pos < len(text) else 2)
        return out

    def _target(self, words, distance, g53, units):
        cur = self.mpos
        wco = [a + b for a, b in zip(self.g54, self.g92)]
        tgt = list(cur)
        k = 25.4 if units == "G20" else 1.0
        for letter, v in words:
            if letter in "XYZ":
                i = "XYZ".index(letter)
                v *= k
                if distance == "G91" and not g53:
                    tgt[i] = cur[i] + v
                elif g53:
                    tgt[i] = v
                else:
                    tgt[i] = v + wco[i]
        return tgt

    def _within(self, tgt) -> bool:
        for i, (lo, hi) in enumerate(self.limits()):
            if tgt[i] < lo - 1e-6 or tgt[i] > hi + 1e-6:
                return False
        return True

    def _jog(self, text: str):
        if self.state not in ("Idle", "Jog"):
            raise _GErr(8)
        words = self._words(text)
        distance, units, g53, feed, axes = self.distance, self.units, False, None, False
        for letter, v in words:
            if letter == "G":
                if v in (90, 91):
                    distance = f"G{int(v)}"
                elif v in (20, 21):
                    units = f"G{int(v)}"
                elif v == 53:
                    g53 = True
                else:
                    raise _GErr(16)
            elif letter == "F":
                feed = v
            elif letter in "XYZ":
                axes = True
            else:
                raise _GErr(16)
        if feed is None:
            raise _GErr(22)
        if not axes:
            raise _GErr(26)
        tgt = self._target(words, distance, g53, units)
        if self._b(20) and not self._within(tgt):
            raise _GErr(15)
        if self.check:
            return "ok"
        self.planner.append(Block(tgt, feed, "jog"))
        self.state = "Jog"
        return "ok"

    def _gcode(self, line: str):
        words = self._words(line)
        g53 = False
        motion = None
        dwell = None
        g10 = None
        g92 = False
        nonmodal_axes = False
        pword = lword = None
        for letter, v in words:
            if letter == "G":
                if v in (0, 1, 2, 3):
                    motion = f"G{int(v)}"
                elif v == 4:
                    dwell = True
                elif v == 10:
                    g10 = True
                elif v in (17, 18, 19, 40, 49, 80, 94, 54, 64, 61):
                    pass
                elif v in (20, 21):
                    self.units = f"G{int(v)}"
                elif v in (90, 91):
                    self.distance = f"G{int(v)}"
                elif v == 53:
                    g53 = True
                elif v in (28, 30):
                    nonmodal_axes = True
                elif abs(v - 92) < 1e-9:
                    g92 = True
                elif abs(v - 92.1) < 1e-6:
                    self.g92 = [0.0, 0.0, 0.0]
                    self.wco_dirty = True
                else:
                    raise _GErr(20)
            elif letter == "M":
                if v in (0, 1):
                    self.m0_pending = True
                elif v in (2, 30):
                    self.distance, self.units, self.motion = "G90", "G21", "G1"
                elif v in (3, 4, 5, 7, 8, 9):
                    pass
                else:
                    raise _GErr(20)
            elif letter == "F":
                self.feed = v * (25.4 if self.units == "G20" else 1)
            elif letter == "P":
                pword = v
            elif letter == "L":
                lword = v
            elif letter in "XYZIJKRSTN":
                pass
            else:
                raise _GErr(20)
        has_axes = any(l in "XYZ" for l, _ in words)
        if dwell:
            if pword is None:
                raise _GErr(28)
            if not self.check and pword > 0:
                self.planner.append(Block(self.mpos, 0, "dwell", pword))
                self.state = "Run"
            return "ok"
        if g10:
            if lword not in (2, 20) or pword not in (None, 1, 0):
                raise _GErr(20)
            cur = self.mpos
            for letter, v in words:
                if letter in "XYZ":
                    i = "XYZ".index(letter)
                    if lword == 2:
                        self.g54[i] = v
                    else:
                        self.g54[i] = cur[i] - self.g92[i] - v
            self.wco_dirty = True
            return "ok"
        if g92:
            cur = self.mpos
            for letter, v in words:
                if letter in "XYZ":
                    i = "XYZ".index(letter)
                    self.g92[i] = cur[i] - self.g54[i] - v
            self.wco_dirty = True
            return "ok"
        if nonmodal_axes:
            return "ok"
        if motion:
            self.motion = motion
        if not has_axes:
            return "ok"
        m = motion or self.motion
        if g53 and m not in ("G0", "G1"):
            raise _GErr(30)
        if m in ("G1", "G2", "G3") and self.feed <= 0:
            raise _GErr(22)
        tgt = self._target(words, self.distance, g53, self.units)
        if self._b(20) and not self._within(tgt):
            self._alarm(2)
            return None
        if self.check:
            return "ok"
        rapid = m == "G0"
        self.planner.append(Block(tgt, self._f(110) if rapid else self.feed, "rapid" if rapid else "line"))
        self.state = "Run"
        return "ok"

    # ---------------------------------------------------------------- homing
    def _start_homing(self) -> None:
        lim = self.limits()
        mask = int(self._f(23))
        pull = self._f(27)
        home = []
        for i in range(3):
            lo, hi = lim[i]
            home.append(lo if mask & (1 << i) else hi)
        # Z first, then X and Y together, then pull off.
        seek = self._f(25) * 4
        z_first = [self.phys[0], self.phys[1], home[2]]
        xy = [home[0], home[1], home[2]]
        off = [home[i] + (pull if mask & (1 << i) else -pull) for i in range(3)]
        self.homing = deque([(z_first, seek), (xy, seek), (off, self._f(24) * 10)])
        self.state = "Home"

    def _finish_homing(self) -> None:
        self.homing = None
        self.offset = [0.0, 0.0, 0.0]
        self.state = "Idle"
        self.alarm = None
        self.wco_dirty = True
        self._emit("ok")

    # ---------------------------------------------------------------- motion
    def _move_toward(self, target_phys, feed_mm_min, dt) -> bool:
        d = [t - p for t, p in zip(target_phys, self.phys)]
        dist = math.sqrt(sum(x * x for x in d))
        step = feed_mm_min / 60.0 * dt
        self.cur_feed = feed_mm_min
        if dist <= step or dist < 1e-9:
            self.phys = list(target_phys)
            return True
        self.phys = [p + x / dist * step for p, x in zip(self.phys, d)]
        return False

    def _step(self, dt: float) -> None:
        if self.homing is not None:
            tgt, feed = self.homing[0]
            if self._move_toward(tgt, feed, dt):
                self.homing.popleft()
                if not self.homing:
                    self._finish_homing()
            return
        if self.hold or not self.planner:
            self.cur_feed = 0.0
            if not self.planner and self.state in ("Run", "Jog"):
                self.state = "Idle"
            return
        b = self.planner[0]
        if b.kind == "dwell":
            if self.dwell_until is None:
                self.dwell_until = time.monotonic() + b.dwell / self.ts
            if time.monotonic() >= self.dwell_until:
                self.dwell_until = None
                self.planner.popleft()
            return
        feed = b.feed * (self.rapid_ov if b.kind == "rapid" else self.feed_ov) / 100.0
        if b.kind == "jog":
            feed = b.feed
        tgt_phys = [t + o for t, o in zip(b.target, self.offset)]
        # Hard limits: the switches sit at the homing end of each axis.
        lim = self.phys_limits()
        mask = int(self._f(23))
        for i in range(3):
            lo, hi = lim[i]
            if mask & (1 << i):
                tgt_phys[i] = max(tgt_phys[i], lo - 0.5)
            else:
                tgt_phys[i] = min(tgt_phys[i], hi + 0.5)
        done = self._move_toward(tgt_phys, feed, dt)
        if self._b(21):
            for i in range(3):
                lo, hi = lim[i]
                hit = self.phys[i] < lo - 0.2 if mask & (1 << i) else self.phys[i] > hi + 0.2
                if hit:
                    self._alarm(1)
                    return
        if done:
            self.planner.popleft()
            if not self.planner:
                self.state = "Idle"
                self.cur_feed = 0.0


class _GErr(Exception):
    def __init__(self, code: int):
        self.code = code
