"""Machine layer: every motion the app makes goes through here.

Safety rules enforced in software (independent of GRBL's own $20 soft limits):
  * every target is checked against the travel limits (minus a margin) and
    the Z floor before it is sent;
  * go-to, programs and scans need a homed machine (configurable);
  * a continuous jog only runs while the browser keeps sending keep-alives;
  * the laser guard stops a downward move when the surface gets too close;
  * spindle-on commands are refused (the laser hangs on the spindle);
  * alarms and resets abort the running job and, when GRBL says position
    may be lost, clear the homed flag.
"""

from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path

from .config import DIRS, Config
from .events import EventBus
from .grbl import (RT_FEED_100, RT_FEED_M1, RT_FEED_M10, RT_FEED_P1, RT_FEED_P10, RT_HOLD,
                   RT_JOG_CANCEL, RT_RAPID_25, RT_RAPID_50, RT_RAPID_100, RT_RESET, RT_RESUME,
                   Cancelled, GrblController, LinkError)
from .grbl_codes import SETTINGS, alarm_loses_position, alarm_text
from .transport import SerialTransport, SimTransport, TcpTransport, TransportError, list_serial_ports

AXES = "xyz"
SPINDLE_RE = re.compile(r"(?<![A-Z])M0*[34](?!\d)")


class Refused(Exception):
    """A request the machine will not carry out; the message is for the operator."""


class JobAborted(Exception):
    pass


class MotionError(Exception):
    pass


class Job:
    def __init__(self, kind: str, name: str):
        self.id = f"{kind}-{int(time.time() * 1000)}"
        self.kind = kind
        self.name = name
        self.abort = threading.Event()
        self.resume_evt = threading.Event()
        self.resume_evt.set()
        self.paused = False
        self.held = False            # we sent a feed hold for this pause
        self.status = "running"
        self.message = ""
        self.progress = {"done": 0, "total": 0}
        self.detail: dict = {}
        self.started = time.time()
        self.ended: float | None = None
        self.error: str | None = None
        self.result: dict = {}
        self.thread: threading.Thread | None = None

    def checkpoint(self) -> None:
        """Call between steps: blocks while paused, raises if stopped."""
        while not self.resume_evt.wait(0.1):
            if self.abort.is_set():
                raise JobAborted()
        if self.abort.is_set():
            raise JobAborted()

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "status": self.status,
            "paused": self.paused, "message": self.message, "progress": self.progress,
            "detail": self.detail, "started": self.started, "ended": self.ended,
            "error": self.error, "result": self.result,
        }


class Machine:
    def __init__(self, cfg: Config, bus: EventBus):
        self.cfg = cfg
        self.bus = bus
        self.g = GrblController(on_event=self._on_grbl)
        self.sim = None
        self.laser = None                # set by the app; used by the guard
        self.homed = False
        self.homed_how = ""              # homing | trusted | kept
        self.job: Job | None = None
        self.last_job: Job | None = None
        self._lock = threading.RLock()
        self._jog_active = False
        self._jog_dir = (0.0, 0.0, 0.0)
        self._jog_keep = 0.0
        self._jog_target: list[float] | None = None
        self._expect_reset = False
        self._kept_home: dict | None = None
        self._prev_z = None
        self._guard_trip_t = 0.0
        self._stop_evt = threading.Event()
        threading.Thread(target=self._watchdog, daemon=True, name="jog-watchdog").start()

    # ================================================================ connect
    def ports(self) -> list[dict]:
        return list_serial_ports()

    def connect(self, transport: str | None = None, port: str | None = None,
                baud: int | None = None, host: str | None = None, tcp_port: int | None = None) -> dict:
        if self.job and self.job.status in ("running", "paused"):
            raise Refused("A job is running. Stop it before reconnecting.")
        m = self.cfg["machine"]
        transport = transport or m["transport"]
        if self.g.connected:
            self.disconnect()
        if transport == "sim":
            from .sim_grbl import SimGrbl
            if self.sim is None:
                self.sim = SimGrbl()
            t = SimTransport(self.sim)
        elif transport == "tcp":
            t = TcpTransport(host or m["tcp_host"], int(tcp_port or m["tcp_port"]))
        else:
            port = port or m["port"] or self._guess_port()
            if not port:
                raise Refused("No serial port found. Plug the CNC in by USB, or use WiFi / Simulator.")
            t = SerialTransport(port, int(baud or m["baud"]), reset=bool(m.get("reset_on_connect")))
        try:
            self.g.connect(t, float(m.get("status_hz", 10)))
        except (LinkError, TransportError, TimeoutError) as e:
            raise Refused(str(e)) from e
        patch = {"transport": transport}
        if transport == "serial":
            patch["port"] = t.name
        self.cfg.update({"machine": patch})
        self._after_connect()
        return self.snapshot()

    def _guess_port(self) -> str:
        ports = self.ports()
        laser_port = (self.cfg["laser"].get("port") or "").upper()
        for p in ports:
            if p["guess"] == "cnc":
                return p["device"]
        for p in ports:
            if p["device"].upper() != laser_port:
                return p["device"]
        return ""

    def _after_connect(self) -> None:
        if self.cfg["machine"].get("limits_source") == "settings":
            self._limits_from_settings()
        st = self.g.state
        kept = self._kept_home
        self.homed = False
        self.homed_how = ""
        if kept and st not in ("Alarm",) and self.g.banner_seq == 0 and any(abs(v) > 1e-6 for v in self.g.mpos):
            # Same controller, never rebooted while we were away: position still valid.
            self.homed = True
            self.homed_how = "kept"
            self.bus.note("info", "Reconnected without a controller reboot; homing kept.")
        elif st == "Alarm":
            self.bus.note("warn", "Controller is locked (alarm). Home the machine (or Unlock to jog without limits).")
        else:
            self.bus.note("warn", "Connected. Machine position is not verified: home before go-to, programs or scans.")
        f = self.g.setting(1)
        if f != 255:
            self.bus.note("info", f"$1={f:g}: motors release {f:g} ms after each move. $1=255 is recommended for scanning (Machine tab).")

    def disconnect(self) -> None:
        self.jog_stop(quiet=True)
        if self.homed:
            self._kept_home = {"mpos": list(self.g.mpos), "t": time.time()}
        self.g.disconnect()
        self.homed = False

    # ================================================================ events from GRBL
    def _on_grbl(self, kind: str, data: dict) -> None:
        if kind == "tx":
            self.bus.log(">", data["line"])
        elif kind == "log":
            self.bus.log(data.get("dir", "<"), data["line"])
        elif kind == "alarm":
            code = data["code"]
            if alarm_loses_position(code):
                if self.homed:
                    self.bus.note("error", "Position may be lost: home again before measuring.")
                self.homed = False
            self._abort_job(f"ALARM:{code} - {alarm_text(code)}")
            self.bus.publish("alarm", data, keep=True)
            self.bus.note("error", f"ALARM:{code} - {alarm_text(code)}")
        elif kind == "reset":
            self._jog_active = False
            self._jog_target = None
            if self._expect_reset:
                self._expect_reset = False
            else:
                self._abort_job("Controller reset")
            threading.Thread(target=self._reread_offsets, daemon=True).start()
        elif kind == "disconnected":
            self._jog_active = False
            if self.homed:
                self._kept_home = {"mpos": list(self.g.mpos), "t": time.time()}
            self.homed = False
            self._abort_job("Lost connection to the controller")
            if data.get("reason") != "closed":
                self.bus.note("error", f"Controller disconnected: {data.get('reason')}")
        elif kind == "stale":
            self.bus.note("warn", f"No status from the controller for {data['seconds']} s.")
        elif kind == "connected":
            self.bus.note("ok", f"Connected to {data.get('name')}.")

    def _reread_offsets(self) -> None:
        time.sleep(0.5)
        try:
            if self.g.connected:
                self.g.read_offsets()
        except Exception:
            pass

    # ================================================================ limits
    def _limits_from_settings(self) -> None:
        g = self.g
        if not g.settings:
            return
        mask = int(g.setting(23))
        lim = {}
        for i, a in enumerate(AXES):
            t = g.setting(130 + i, 400.0)
            if g.firmware.get("esp32"):
                lim[a] = [0.0, t] if mask & (1 << i) else [-t, 0.0]
            else:
                lim[a] = [-t, 0.0]
        self.cfg.update({"machine": {"limits": lim, "limits_source": "settings"}})

    def _limits_from_homing(self) -> None:
        if self.cfg["machine"].get("limits_source") == "manual":
            return
        g = self.g
        mask = int(g.setting(23))
        pull = g.setting(27, 1.0)
        lim = {}
        for i, a in enumerate(AXES):
            t = g.setting(130 + i, 400.0)
            p = g.mpos[i]
            if mask & (1 << i):
                lo = round(p - pull, 3)
                lim[a] = [lo, round(lo + t, 3)]
            else:
                hi = round(p + pull, 3)
                lim[a] = [round(hi - t, 3), hi]
        self.cfg.update({"machine": {"limits": lim, "limits_source": "homing"}})

    def allowed(self) -> dict:
        """Per-axis machine-coordinate range a target may use."""
        m = self.cfg["machine"]
        margin = float(m.get("limit_margin_mm", 0.5))
        out = {}
        for a in AXES:
            lo, hi = m["limits"][a]
            out[a] = [lo + margin, hi - margin]
        floor = m.get("z_floor_mpos")
        if floor is not None:
            out["z"][0] = max(out["z"][0], float(floor))
        return out

    def check_target(self, tgt: list[float], what: str = "Move") -> None:
        """Raise Refused if a machine-coordinate target is outside what is allowed."""
        if not self.homed:
            if self.cfg["machine"].get("require_homing", True):
                raise Refused(f"{what} needs a homed machine. Home first (or 'Trust position' if it already is).")
            return
        al = self.allowed()
        floor = self.cfg["machine"].get("z_floor_mpos")
        for i, a in enumerate(AXES):
            lo, hi = al[a]
            v = tgt[i]
            if v < lo - 1e-6 or v > hi + 1e-6:
                if a == "z" and floor is not None and v < lo and v >= self.cfg["machine"]["limits"]["z"][0]:
                    raise Refused(f"{what}: Z {v:.3f} is below the Z floor ({float(floor):.3f}). Clear or lower the floor first.")
                raise Refused(f"{what}: {a.upper()} {v:.3f} is outside the travel ({lo:.2f} to {hi:.2f} machine).")

    def work_to_machine(self, w: list[float | None]) -> list[float]:
        mp = self.g.mpos
        return [mp[i] if w[i] is None else w[i] + self.g.wco[i] for i in range(3)]

    def machine_to_work(self, m: list[float]) -> list[float]:
        return [m[i] - self.g.wco[i] for i in range(3)]

    # ================================================================ snapshot
    def snapshot(self) -> dict:
        g = self.g
        m = self.cfg["machine"]
        alarm = None
        if g.state == "Alarm":
            code = g.alarm
            alarm = {"code": code, "text": alarm_text(code) if code else "Locked: home or unlock to continue."}
        return {
            "connected": g.connected,
            "transport": m.get("transport"),
            "name": getattr(g.t, "name", "") if g.t else "",
            "state": g.state,
            "sub": g.status.get("sub", ""),
            "alarm": alarm,
            "mpos": [round(v, 4) for v in g.mpos],
            "wpos": [round(v, 4) for v in g.wpos],
            "wco": [round(v, 4) for v in g.wco],
            "feed": g.feed,
            "accel": [g.setting(120 + i, 50.0) for i in range(3)],
            "hold_motors": g.setting(1, 0) == 255,
            "ov": g.ov,
            "pins": g.pins,
            "homed": self.homed,
            "homed_how": self.homed_how,
            "limits": m["limits"],
            "limits_source": m.get("limits_source"),
            "allowed": self.allowed(),
            "z_floor": m.get("z_floor_mpos"),
            "safe_z": m.get("safe_z_mpos"),
            "firmware": g.firmware,
            "jogging": self._jog_active,
            "job": (self.job or self.last_job).snapshot() if (self.job or self.last_job) else None,
            "stale": g._stale,
        }

    # ================================================================ basic commands
    def _need_link(self) -> None:
        if not self.g.connected:
            raise Refused("Not connected to the CNC.")

    def _need_free(self, allow_paused: bool = False) -> None:
        self._need_link()
        j = self.job
        if j and j.status in ("running", "paused"):
            if allow_paused and j.paused and not j.held:
                return
            raise Refused(f"Busy: {j.kind} '{j.name}' is {'paused' if j.paused else 'running'}. Stop it first.")

    def home(self) -> None:
        self._need_free()
        if not self.g.settings or self.g.setting(22) == 0:
            raise Refused("Homing is disabled on the controller ($22=0).")
        self.start_job("home", "Homing", self.home_blocking)

    def home_blocking(self, job: Job) -> None:
        """Run $H inside a job and wait for it (used by the Home button and programs)."""
        if self.g.setting(22) == 0:
            raise Refused("Homing is disabled on the controller ($22=0).")
        if self.g.state not in ("Idle", "Alarm"):
            self.job_wait(job)
        job.message = "Homing: Z goes up first, then X and Y."
        r = self.g.queue("$H", timeout=10)
        while not r.done.wait(0.1):
            if job.abort.is_set():
                raise JobAborted()
            if time.monotonic() - r.t_sent > 180:
                raise MotionError("Homing took longer than 3 minutes.")
        if not r.ok:
            raise MotionError(f"Homing failed: {r.message}")
        self.g.wait_status(timeout=2)
        self.homed = True
        self.homed_how = "homing"
        self._limits_from_homing()
        self.g.read_offsets()
        job.message = ""
        lim = self.cfg["machine"]["limits"]
        self.bus.note("ok", "Homed. Travel: " + ", ".join(f"{a.upper()} {lim[a][0]:g}..{lim[a][1]:g}" for a in AXES))

    def unlock(self) -> None:
        self._need_free()
        r = self.g.send("$X", timeout=5)
        if not r.ok:
            raise Refused(r.message)
        if not self.homed:
            self.bus.note("warn", "Unlocked without homing: limits are not enforced. Jogs are capped; home before measuring.")

    def trust_position(self) -> None:
        self._need_link()
        if self.g.state == "Alarm":
            raise Refused("Controller is in alarm. Home or unlock first.")
        self.homed = True
        self.homed_how = "trusted"
        self.bus.note("warn", "Position trusted without homing. Only do this if the machine was homed and has not lost steps.")

    def hold(self) -> None:
        self._need_link()
        self.g.realtime(RT_HOLD)

    def resume(self) -> None:
        self._need_link()
        j = self.job
        if j and j.paused:
            self.resume_job()
            return
        self.g.realtime(RT_RESUME)

    def stop(self) -> None:
        """Controlled stop: cancel jog / job, decelerate, flush, keep position."""
        if not self.g.connected:
            return
        j = self.job
        if j:
            j.abort.set()
            j.resume_evt.set()
        st = self.g.state
        if self._jog_active or st == "Jog":
            self.jog_stop(quiet=True)
            return
        if st == "Home":
            self.g.realtime(RT_RESET)   # the only way to stop homing; GRBL raises ALARM:6
            return
        if st in ("Run", "Hold", "Check") or self.g.pending_count():
            self.g.realtime(RT_HOLD)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.0:
                s = self.g.status
                if (s.get("state") == "Hold" and s.get("sub") == "0") or s.get("state") == "Idle":
                    break
                time.sleep(0.03)
            self._expect_reset = j is None
            self.g.realtime(RT_RESET)   # flushes planner + RX buffer; held position is kept
            self.bus.note("info", "Stopped.")

    def estop(self) -> None:
        """Immediate soft reset. Position may be lost if it was moving."""
        if self.job:
            self.job.abort.set()
            self.job.resume_evt.set()
        self._jog_active = False
        if self.g.connected:
            self.g.realtime(RT_RESET)
        self.bus.note("warn", "E-STOP sent (soft reset). If the machine was moving, home again.")

    def override(self, kind: str, action: str) -> None:
        self._need_link()
        table = {
            ("feed", "reset"): RT_FEED_100, ("feed", "+10"): RT_FEED_P10, ("feed", "-10"): RT_FEED_M10,
            ("feed", "+1"): RT_FEED_P1, ("feed", "-1"): RT_FEED_M1,
            ("rapid", "100"): RT_RAPID_100, ("rapid", "50"): RT_RAPID_50, ("rapid", "25"): RT_RAPID_25,
        }
        b = table.get((kind, action))
        if not b:
            raise Refused(f"Unknown override {kind} {action}")
        self.g.realtime(b)

    # ================================================================ jogging
    def _feed_cap(self, d: tuple[float, float, float]) -> float:
        caps = [self.g.setting(110 + i, 1000.0) for i in range(3)]
        n = math.sqrt(sum(x * x for x in d)) or 1.0
        lim = min((caps[i] / (abs(d[i]) / n) for i in range(3) if abs(d[i]) > 1e-9), default=caps[0])
        return lim

    def _jog_feed(self, d, speed: str) -> float:
        j = self.cfg["jog"]
        base = float(j["feed_z"]) if abs(d[0]) < 1e-9 and abs(d[1]) < 1e-9 else float(j["feed_xy"])
        f = base * {"fast": float(j["fast_factor"]), "fine": float(j["fine_factor"])}.get(speed, 1.0)
        return max(1.0, min(f, self._feed_cap(d)))

    def _jog_ready(self) -> None:
        self._need_free(allow_paused=True)
        st = self.g.state
        if st == "Alarm":
            raise Refused("Controller is locked (alarm). Home, or Unlock to jog without limits.")
        if st in ("Hold", "Door"):
            raise Refused("Machine is in feed hold. Resume or Stop first.")
        if st in ("Run", "Home", "Check"):
            raise Refused(f"Machine is busy ({st}).")

    def jog_step(self, dx: float = 0, dy: float = 0, dz: float = 0, speed: str = "normal") -> dict:
        self._jog_ready()
        d = (float(dx), float(dy), float(dz))
        if not any(abs(v) > 1e-9 for v in d):
            return {"moved": False}
        feed = self._jog_feed(d, speed)
        if self.homed:
            base = self._jog_target if (self._jog_target and self.g.state == "Jog") else list(self.g.mpos)
            al = self.allowed()
            tgt = []
            clamped = []
            for i, a in enumerate(AXES):
                v = base[i] + d[i]
                lo, hi = al[a]
                if abs(d[i]) > 1e-9:
                    if v < lo:
                        v, _ = lo, clamped.append(a)
                    elif v > hi:
                        v, _ = hi, clamped.append(a)
                tgt.append(v)
            if all(abs(tgt[i] - base[i]) < 0.0005 for i in range(3)):
                raise Refused(f"At the {'/'.join(clamped).upper() or ''} limit" + (" (Z floor)" if "z" in clamped and self.cfg['machine'].get('z_floor_mpos') is not None else "") + ".")
            words = "".join(f"{a.upper()}{tgt[i]:.4f}" for i, a in enumerate(AXES) if abs(d[i]) > 1e-9)
            line = f"$J=G53G90G21{words}F{feed:.0f}"
            self._jog_target = tgt
        else:
            words = "".join(f"{a.upper()}{d[i]:.4f}" for i, a in enumerate(AXES) if abs(d[i]) > 1e-9)
            line = f"$J=G91G21{words}F{feed:.0f}"
            clamped = []
        r = self.g.send(line, timeout=5)
        if not r.ok:
            self._jog_target = None
            raise Refused(f"Jog refused: {r.message}")
        return {"moved": True, "clamped": clamped}

    def jog_start(self, vx: float = 0, vy: float = 0, vz: float = 0, speed: str = "normal") -> dict:
        """Continuous jog toward the travel limit; runs until jog_stop or the watchdog."""
        self._jog_ready()
        if self._jog_active:
            self.jog_stop(quiet=True)
            time.sleep(0.05)
        v = [float(vx), float(vy), float(vz)]
        n = math.sqrt(sum(x * x for x in v))
        if n < 1e-9:
            return {"moved": False}
        u = [x / n for x in v]
        feed = self._jog_feed(tuple(u), speed)
        pos = list(self.g.mpos)
        if self.homed:
            al = self.allowed()
            s = math.inf
            for i, a in enumerate(AXES):
                if abs(u[i]) > 1e-9:
                    lo, hi = al[a]
                    bound = hi if u[i] > 0 else lo
                    s = min(s, (bound - pos[i]) / u[i])
            if s < 0.01:
                raise Refused("Already at the limit in that direction.")
            tgt = [pos[i] + u[i] * s for i in range(3)]
            words = "".join(f"{a.upper()}{tgt[i]:.4f}" for i, a in enumerate(AXES) if abs(u[i]) > 1e-9)
            line = f"$J=G53G90G21{words}F{feed:.0f}"
        else:
            s = float(self.cfg["machine"].get("unhomed_jog_max_mm", 25.0))
            words = "".join(f"{a.upper()}{u[i] * s:.4f}" for i, a in enumerate(AXES) if abs(u[i]) > 1e-9)
            line = f"$J=G91G21{words}F{feed:.0f}"
        self._jog_keep = time.monotonic()
        self._jog_dir = tuple(u)
        r = self.g.send(line, timeout=5)
        if not r.ok:
            raise Refused(f"Jog refused: {r.message}")
        self._jog_active = True
        self._jog_keep = time.monotonic()
        return {"moved": True, "distance": round(s, 3), "feed": feed}

    def jog_keepalive(self) -> None:
        self._jog_keep = time.monotonic()

    def jog_stop(self, quiet: bool = False) -> None:
        was = self._jog_active
        self._jog_active = False
        self._jog_target = None
        if self.g.connected and (was or self.g.state == "Jog"):
            self.g.realtime(RT_JOG_CANCEL)

    def _watchdog(self) -> None:
        """Jog keep-alive watchdog + laser proximity guard (50 Hz)."""
        while not self._stop_evt.wait(0.02):
            try:
                if self._jog_active:
                    if time.monotonic() - self._jog_keep > float(self.cfg["jog"].get("watchdog_s", 0.6)):
                        self.jog_stop(quiet=True)
                        self.bus.note("warn", "Jog stopped: the browser stopped sending (window lost focus or link dropped).")
                    elif self.g.state == "Idle" and time.monotonic() - self._jog_keep > 0.3:
                        self._jog_active = False   # reached the end of travel
                self._guard()
            except Exception:
                pass

    def _guard(self) -> None:
        las = self.laser
        g = self.g
        z = g.mpos[2]
        prev, self._prev_z = self._prev_z, z
        if not las or not g.connected or prev is None:
            return
        if g.state not in ("Jog", "Run"):
            return
        going_down = z < prev - 1e-4 or (self._jog_active and self._jog_dir[2] < 0)
        if not going_down:
            return
        lr = las.latest()
        if not lr or not lr.get("valid"):
            return
        gap = lr.get("gap_mm")     # distance left before the near end of range
        if gap is None or gap > float(self.cfg["laser"].get("guard_mm", 1.0)):
            return
        if time.monotonic() - self._guard_trip_t < 1.0:
            return
        self._guard_trip_t = time.monotonic()
        if g.state == "Jog":
            self.jog_stop(quiet=True)
        else:
            g.realtime(RT_HOLD)
        self.bus.note("error", f"Laser guard: surface only {gap:.2f} mm from the near end of the range. Downward move stopped.")

    # ================================================================ coordinates
    def set_work_zero(self, axes: str = "XY") -> None:
        self._need_free(allow_paused=True)
        axes = "".join(a for a in axes.upper() if a in "XYZ")
        if not axes:
            raise Refused("No axes given.")
        if self.g.state not in ("Idle",):
            raise Refused(f"Machine must be Idle to set zero (it is {self.g.state}).")
        r = self.g.send("G10L20P1" + "".join(f"{a}0" for a in axes), timeout=5)
        if not r.ok:
            raise Refused(r.message)
        self.g.read_offsets()
        self.bus.note("ok", f"Work zero set: {axes} = 0 at the current position.")

    def set_work_coords(self, values: dict) -> None:
        self._need_free(allow_paused=True)
        words = "".join(f"{a.upper()}{float(v):.4f}" for a, v in values.items() if v is not None and a.lower() in AXES)
        if not words:
            raise Refused("No values given.")
        if self.g.state != "Idle":
            raise Refused("Machine must be Idle.")
        r = self.g.send("G10L20P1" + words, timeout=5)
        if not r.ok:
            raise Refused(r.message)
        self.g.read_offsets()

    def set_work_origin_machine_xy(self, mx: float, my: float) -> None:
        """Put work X0 Y0 at a machine-coordinate point (no motion; G10 L2 P1)."""
        self._need_link()
        g92 = self.g.offsets.get("G92", [0.0, 0.0, 0.0])
        r = self.g.send(f"G10L2P1X{mx - g92[0]:.4f}Y{my - g92[1]:.4f}", timeout=5)
        if not r.ok:
            raise Refused(f"Could not set work zero: {r.message}")
        self.g.read_offsets()

    def set_z_floor(self, value: float | None) -> None:
        if value is not None and not self.homed:
            raise Refused("Home first: the Z floor is stored in machine coordinates.")
        self.cfg.update({"machine": {"z_floor_mpos": None if value is None else round(float(value), 4)}})
        self.bus.note("ok", "Z floor cleared." if value is None else f"Z floor set at machine Z {float(value):.3f}: nothing will move the laser lower.")

    def set_safe_z(self, value: float) -> None:
        self.cfg.update({"machine": {"safe_z_mpos": round(float(value), 4)}})

    # ================================================================ jobs
    def start_job(self, kind: str, name: str, fn, allow_paused_jog=False) -> Job:
        with self._lock:
            self._need_free()
            job = Job(kind, name)
            self.job = job

        def runner():
            try:
                fn(job)
                if job.abort.is_set():
                    job.status = "stopped"
                else:
                    job.status = "done"
            except (JobAborted, Cancelled):
                job.status = "stopped"
                if not job.error:
                    job.error = "Stopped by operator"
            except Refused as e:
                job.status = "failed"
                job.error = str(e)
            except (MotionError, LinkError, TimeoutError, TransportError) as e:
                job.status = "failed"
                job.error = str(e)
            except Exception as e:  # pragma: no cover
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.ended = time.time()
                job.paused = False
                with self._lock:
                    if self.job is job:
                        self.job = None
                    self.last_job = job
                level = {"done": "ok", "stopped": "warn"}.get(job.status, "error")
                msg = f"{job.name}: {job.status}"
                if job.error and job.status != "done":
                    msg += f" - {job.error}"
                self.bus.note(level, msg)
                self.bus.publish("job", job.snapshot())

        job.thread = threading.Thread(target=runner, daemon=True, name=f"job-{kind}")
        job.thread.start()
        self.bus.publish("job", job.snapshot())
        return job

    def _abort_job(self, reason: str) -> None:
        j = self.job
        if j and j.status in ("running", "paused"):
            j.error = reason
            j.abort.set()
            j.resume_evt.set()

    def pause_job(self) -> None:
        j = self.job
        if not j:
            raise Refused("Nothing is running.")
        j.paused = True
        j.status = "paused"
        j.resume_evt.clear()
        if j.kind in ("gcode", "goto", "check"):
            self.g.realtime(RT_HOLD)
            j.held = True
        if not j.message:
            j.message = "Paused"
        self.bus.publish("job", j.snapshot())

    def resume_job(self) -> None:
        j = self.job
        if not j:
            self.g.realtime(RT_RESUME)
            return
        if self._jog_active:
            self.jog_stop(quiet=True)
        if j.held:
            self.g.realtime(RT_RESUME)
            j.held = False
        j.paused = False
        j.status = "running"
        j.message = ""
        j.resume_evt.set()
        self.bus.publish("job", j.snapshot())

    def prompt_pause(self, job: Job, message: str) -> None:
        """A program step asking the operator to do something, then Resume."""
        job.paused = True
        job.status = "paused"
        job.message = message
        job.resume_evt.clear()
        self.bus.publish("job", job.snapshot())
        self.bus.note("info", f"Paused: {message}")
        job.checkpoint()

    # ================================================================ motion for jobs
    def job_move(self, job: Job, x=None, y=None, z=None, feed: float | None = None,
                 rapid: bool = False, wait: bool = True) -> list[float]:
        """Absolute move in MACHINE coordinates, limit-checked. Returns the target."""
        job.checkpoint()
        tgt = list(self.g.mpos)
        if self._last_target_valid(job):
            tgt = list(job.detail["_target"])
        for i, v in enumerate((x, y, z)):
            if v is not None:
                tgt[i] = float(v)
        self.check_target(tgt)
        words = "".join(f"{a.upper()}{tgt[i]:.4f}" for i, a in enumerate(AXES) if (x, y, z)[i] is not None)
        if not words:
            return tgt
        if rapid:
            line = f"G53G0{words}"
        else:
            f = feed or float(self.cfg["machine"]["travel_feed_xy"])
            line = f"G53G1{words}F{f:.0f}"
        r = self.g.queue(line, timeout=None, cancel=job.abort.is_set)
        self._wait_reply(job, r, line)
        job.detail["_target"] = tgt
        if wait:
            self.job_wait(job)
        return tgt

    def _last_target_valid(self, job: Job) -> bool:
        return "_target" in job.detail and self.g.pending_count() > 0

    def _wait_reply(self, job: Job, r, line: str, timeout: float | None = None) -> None:
        t0 = time.monotonic()
        while not r.done.wait(0.05):
            if job.abort.is_set():
                raise JobAborted()
            if timeout and time.monotonic() - t0 > timeout:
                raise MotionError(f"No reply to {line}")
        if not r.ok:
            raise MotionError(f"{line}: {r.message}")

    def job_wait(self, job: Job) -> dict:
        """Block until every queued move has finished and the machine is Idle."""
        r = self.g.queue("G4P0", timeout=None, cancel=job.abort.is_set)
        self._wait_reply(job, r, "G4P0")
        while True:
            st = self.g.wait_status(timeout=3)
            if st["state"] == "Idle":
                return st
            if st["state"] == "Alarm":
                raise MotionError("Controller went into alarm")
            if job.abort.is_set():
                raise JobAborted()

    def job_dwell(self, job: Job, seconds: float) -> None:
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            job.checkpoint()
            time.sleep(min(0.05, max(0.0, t_end - time.monotonic())))

    def goto(self, x=None, y=None, z=None, coords: str = "work", feed: float | None = None,
             safe_z: bool = True) -> Job:
        self._need_free()
        if all(v is None for v in (x, y, z)):
            raise Refused("Give at least one coordinate.")
        if coords == "work":
            tgt = self.work_to_machine([x, y, z])
        else:
            mp = self.g.mpos
            tgt = [mp[i] if v is None else float(v) for i, v in enumerate((x, y, z))]
        self.check_target(tgt, "Go-to")
        m = self.cfg["machine"]
        sz = m.get("safe_z_mpos")
        start = list(self.g.mpos)
        xy_dist = math.hypot(tgt[0] - start[0], tgt[1] - start[1])
        use_safe = bool(safe_z and sz is not None and xy_dist > 0.5 and start[2] < float(sz) - 0.01)
        if use_safe:
            self.check_target([start[0], start[1], float(sz)], "Safe-Z travel")

        def run(job: Job):
            job.detail = {"target_machine": tgt}
            self.g.send("G21G90", timeout=5)
            fz = float(m["travel_feed_z"])
            fxy = feed or float(m["travel_feed_xy"])
            if use_safe:
                self.job_move(job, z=float(sz), feed=fz)
            if xy_dist > 1e-4:
                self.job_move(job, x=tgt[0], y=tgt[1], feed=fxy)
            if abs(tgt[2] - self.g.mpos[2]) > 1e-4:
                self.job_move(job, z=tgt[2], feed=fz)

        label = ", ".join(f"{a.upper()}{v:g}" for a, v in zip(AXES, (x, y, z)) if v is not None)
        return self.start_job("goto", f"Go to {label} ({coords})", run)

    def stream_gcode(self, job: Job, lines: list[str], stop_on_error: bool = True) -> None:
        """Character-counting stream of already-cleaned lines."""
        from collections import deque
        n = len(lines)
        replies: deque = deque()
        acked = 0
        job.progress = {"done": 0, "total": n}
        for i, line in enumerate(lines):
            if job.abort.is_set():
                raise JobAborted()
            r = self.g.queue(line, timeout=None, cancel=job.abort.is_set)
            replies.append((i, line, r))
            while replies and replies[0][2].done.is_set():
                idx, ln, rr = replies.popleft()
                acked += 1
                if not rr.ok:
                    if stop_on_error or rr.error in ("reset", "link"):
                        if rr.error not in ("reset", "link"):
                            self.g.realtime(RT_HOLD)
                        raise MotionError(f"Line {idx + 1} '{ln}': {rr.message}")
                    self.bus.note("warn", f"Line {idx + 1} '{ln}': {rr.message} (continuing)")
            job.progress = {"done": acked, "total": n, "line": i + 1}
        while replies:
            idx, ln, rr = replies.popleft()
            self._wait_reply(job, rr, ln) if stop_on_error else rr.done.wait()
            acked += 1
            job.progress = {"done": acked, "total": n}

    # ================================================================ console
    def console(self, line: str) -> dict:
        self._need_link()
        line = line.strip()
        if not line:
            raise Refused("Empty line.")
        if line in ("?", "!", "~"):
            self.g.realtime(line.encode())
            return {"ok": True, "lines": []}
        up = line.upper().replace(" ", "")
        if self.cfg["machine"].get("block_spindle", True) and SPINDLE_RE.search(re.sub(r"\(.*?\)", "", up.split(";")[0])):
            raise Refused("Spindle on (M3/M4) is blocked: the laser is mounted on the spindle. Change in Machine settings if needed.")
        if up == "$H":
            self.home()
            return {"ok": True, "lines": ["Homing started"]}
        if up == "$X":
            self.unlock()
            return {"ok": True, "lines": []}
        busy = self.job and self.job.status in ("running", "paused")
        if busy and not up.startswith("$") and not up.startswith("?"):
            raise Refused("A job is running; console G-code is blocked until it ends.")
        if not up.startswith("$") and self.homed:
            self._check_console_motion(up)
        r = self.g.send(line, timeout=20)
        if up.startswith("G10") or up.startswith("G92"):
            self.g.read_offsets()
        if re.match(r"^\$\d+=", up):
            self._after_setting_change(int(up[1:].split("=")[0]))
        return {"ok": bool(r.ok), "message": r.message, "lines": r.lines}

    def _check_console_motion(self, up: str) -> None:
        """Best-effort limit check for hand-typed absolute G53/G90 moves."""
        from .gcode import GcodeInterpreter
        it = GcodeInterpreter(wco=list(self.g.wco), start=list(self.g.mpos))
        it.feed_line(up, 1)
        for seg in it.segments:
            self.check_target(list(seg[3:6]), "Console move")

    # ================================================================ settings
    def write_setting(self, key: int, value: str) -> dict:
        self._need_free()
        if self.g.state not in ("Idle", "Alarm"):
            raise Refused("Settings can only be changed while Idle.")
        value = str(value).strip()
        if not re.match(r"^[-+]?\d*\.?\d+$", value):
            raise Refused("Value must be a number.")
        r = self.g.send(f"${int(key)}={value}", timeout=5)
        if not r.ok:
            raise Refused(r.message)
        self._after_setting_change(int(key))
        return self.settings_table()

    def _after_setting_change(self, key: int) -> None:
        try:
            self.g.read_settings()
        except Exception:
            pass
        if key in (23, 27, 130, 131, 132):
            if self.cfg["machine"].get("limits_source") != "manual":
                self._limits_from_settings()
            self.homed = False
            self.bus.note("warn", f"${key} changed: home again so the travel limits are re-derived.")

    def settings_table(self) -> list[dict]:
        rows = []
        baseline = self.factory_baseline()
        for k in sorted(self.g.settings):
            name, unit, note = SETTINGS.get(k, (f"Setting {k}", "", ""))
            v = self.g.settings[k]
            fac = baseline.get(k)
            rows.append({"key": k, "value": v, "name": name, "unit": unit, "note": note,
                         "factory": fac, "changed": fac is not None and _num(fac) != _num(v)})
        return rows

    def factory_baseline(self) -> dict[int, str]:
        out = {}
        for p in sorted(DIRS["backups"].glob("factory_*.txt")):
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\$(\d+)=(\S+)", ln.strip())
                if m:
                    out[int(m.group(1))] = m.group(2)
            break
        return out

    def backup_settings(self) -> str:
        self._need_link()
        self.g.read_settings()
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = DIRS["backups"] / f"grbl_settings_{ts}.txt"
        lines = [f"GRBL settings backup {ts}", f"[VER:{self.g.firmware.get('version')}]"]
        lines += [f"${k}={v}" for k, v in sorted(self.g.settings.items())]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p.name

    def restore_settings(self, filename: str) -> list[str]:
        self._need_free()
        p = (DIRS["backups"] / Path(filename).name)
        if not p.exists():
            raise Refused(f"No backup named {filename}")
        changed = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\$(\d+)=(\S+)", ln.strip())
            if not m:
                continue
            k, v = int(m.group(1)), m.group(2)
            if _num(self.g.settings.get(k, "")) == _num(v):
                continue
            r = self.g.send(f"${k}={v}", timeout=5)
            changed.append(f"${k}={v} {'ok' if r.ok else r.message}")
        self._after_setting_change(130)
        return changed

    def shutdown(self) -> None:
        self._stop_evt.set()
        try:
            self.stop()
        except Exception:
            pass
        self.g.disconnect()


def _num(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v
