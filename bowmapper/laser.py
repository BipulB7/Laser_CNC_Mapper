"""Laser displacement sensor: background acquisition into a time-stamped ring buffer.

A reader thread pulls every value the sensor produces. The scan engine asks
for "N samples taken after time t" (after the machine has settled), so motion
and measurement never overlap.

Reading scale: MEDAQLib reports the ILD1320 either relative to the start of
the measuring range (0..25 mm) or as distance from the sensor face
(25..50 mm), depending on sensor setup. reading_zero="auto" works it out
from the data; distances shown in the UI are always from the sensor face.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque

import numpy as np

from .config import ROOT

SENTINEL = -1e8


class LaserBase:
    driver = "none"

    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.buf: deque[tuple[float, float]] = deque(maxlen=100000)
        self.cv = threading.Condition()
        self.running = False
        self.connected = False
        self.error: str | None = None
        self.count = 0
        self.rate_hz = 0.0
        self._rate_t0 = time.monotonic()
        self._rate_n = 0
        self._dt_est = 0.001
        self._thread: threading.Thread | None = None
        self.zero_mode = None
        self._auto_vals: list[float] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"laser-{self.driver}")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._close()
        self.connected = False

    def _open(self) -> None: ...
    def _close(self) -> None: ...
    def _read(self) -> list[float]:
        return []

    def _short_error(self, e: Exception) -> str:
        """One readable line for the operator; the full text goes to the console log."""
        msg = str(e)
        port = self._cfgl().get("port") or "the laser port"
        if "ERROR_FILE_NOT_FOUND" in msg or "cannot find the file" in msg:
            return f"Laser port {port} not found: is the IF2001/USB plugged in and the 24 V supply on?"
        if "ACCESS_DENIED" in msg or "Access is denied" in msg:
            return f"Laser port {port} is in use by another program: close sensorTOOL."
        if "No laser COM port" in msg:
            return "No laser USB adapter (FTDI) found: plug in the IF2001/USB."
        return f"Laser: {msg[:160]}"

    def _loop(self) -> None:
        backoff = 1.0
        fails = 0
        while self.running:
            if not self.connected:
                try:
                    self._open()
                    self.connected = True
                    self.connected_t = time.monotonic()
                    self.error = None
                    backoff = 1.0
                    fails = 0
                    self.bus.note("ok", f"Laser connected ({self.describe()}).")
                except Exception as e:
                    fails += 1
                    self.error = self._short_error(e)
                    self.bus.log("!", f"laser connect attempt {fails}: {e}")      # full detail: Console tab only
                    if fails == 1:
                        self.bus.note("error", self.error)
                    elif fails == 5:
                        self.bus.note("warn", "Laser still not connecting: retrying quietly every 15 s. "
                                              "Laser tab > Disconnect to stop.")
                    if not self.cfg["laser"].get("auto_reconnect", True):
                        self.running = False
                        return
                    t_end = time.monotonic() + backoff
                    while self.running and time.monotonic() < t_end:
                        time.sleep(0.1)
                    backoff = min(backoff * 2, 15)
                    continue
            try:
                vals = self._read()
            except Exception as e:
                self.error = str(e)
                self.connected = False
                self.bus.note("error", f"Laser read failed: {e}. Reconnecting.")
                self._close()
                continue
            if not vals:
                time.sleep(0.003)
                continue
            self._push(vals)

    def _push(self, vals: list[float]) -> None:
        now = time.monotonic()
        n = len(vals)
        dt = self._dt_est
        with self.cv:
            for i, v in enumerate(vals):
                self.buf.append((now - (n - 1 - i) * dt, float(v)))
            self.count += n
            self._rate_n += n
            el = now - self._rate_t0
            if el >= 1.0:
                self.rate_hz = self._rate_n / el
                if self.rate_hz > 1:
                    self._dt_est = 1.0 / self.rate_hz
                self._rate_n = 0
                self._rate_t0 = now
            self.cv.notify_all()
        if self.zero_mode is None:
            self._auto_detect(vals)

    # ---------------------------------------------------------------- scale
    def _cfgl(self):
        return self.cfg["laser"]

    def mode(self) -> str:
        m = self._cfgl().get("reading_zero", "auto")
        if m in ("smr", "face"):
            return m
        return self.zero_mode or "smr"

    def _auto_detect(self, vals) -> None:
        if self._cfgl().get("reading_zero", "auto") != "auto":
            return
        smr = float(self._cfgl()["smr_mm"])
        rng = float(self._cfgl()["range_mm"])
        for v in vals:
            if math.isfinite(v) and v > SENTINEL and -1 < v < smr + rng + 1:
                self._auto_vals.append(v)
        if len(self._auto_vals) >= 200:
            med = float(np.median(self._auto_vals))
            if med > rng + 0.5:
                self.zero_mode = "face"
            elif med < smr - 0.5:
                self.zero_mode = "smr"
            self._auto_vals = self._auto_vals[-100:]

    def valid(self, v: float) -> bool:
        if not math.isfinite(v) or v <= SENTINEL:
            return False
        smr = float(self._cfgl()["smr_mm"])
        rng = float(self._cfgl()["range_mm"])
        lo, hi = (-0.2, rng + 0.2) if self.mode() == "smr" else (smr - 0.2, smr + rng + 0.2)
        return lo <= v <= hi

    def distance(self, v: float) -> float:
        """Reading -> distance from the sensor face (mm)."""
        return v + float(self._cfgl()["smr_mm"]) if self.mode() == "smr" else v

    # ---------------------------------------------------------------- reads
    def latest(self, window_s: float = 0.05) -> dict | None:
        with self.cv:
            if not self.buf:
                return None
            t_last = self.buf[-1][0]
            vals = []
            for t, v in reversed(self.buf):
                if t < t_last - window_s:
                    break
                vals.append(v)
        good = [v for v in vals if self.valid(v)]
        age = time.monotonic() - t_last
        smr = float(self._cfgl()["smr_mm"])
        rng = float(self._cfgl()["range_mm"])
        if not good or age > 1.0:
            return {"valid": False, "age_s": round(age, 2), "rate_hz": round(self.rate_hz)}
        r = float(np.mean(good))
        d = self.distance(r)
        return {
            "valid": True,
            "reading": round(r, 5),
            "distance_mm": round(d, 5),
            "gap_mm": round(d - smr, 4),
            "pos_pct": round((d - smr) / rng * 100, 1),
            "age_s": round(age, 3),
            "rate_hz": round(self.rate_hz),
            "frac_valid": round(len(good) / len(vals), 3),
        }

    def collect(self, n: int, after: float | None = None, timeout: float = 5.0) -> list[float]:
        """N raw readings with timestamp > after (default: now)."""
        after = time.monotonic() if after is None else after
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                got = [v for t, v in self.buf if t > after]
                if len(got) >= n:
                    return got[:n]
                left = deadline - time.monotonic()
                if left <= 0 or not self.running:
                    raise TimeoutError(f"Laser gave {len(got)} of {n} samples in {timeout:.1f} s "
                                       f"({'disconnected' if not self.connected else 'too slow'}).")
                self.cv.wait(min(left, 0.2))

    def measure(self, n: int, after: float | None = None, timeout: float | None = None) -> dict:
        if timeout is None:
            timeout = max(3.0, n / max(self.rate_hz, 50) * 3 + 1)
        raw = self.collect(n, after, timeout)
        return self.stats(raw)

    def stats(self, raw: list[float]) -> dict:
        good = np.array([v for v in raw if self.valid(v)], dtype=float)
        n = len(raw)
        out = {"n": n, "n_valid": int(good.size), "frac_invalid": round(1 - good.size / n, 4) if n else 1.0}
        if good.size == 0:
            out.update({"valid": False, "mean": None, "median": None, "std_um": None, "distance_mm": None})
            return out
        mean = float(good.mean())
        out.update({
            "valid": True,
            "mean": mean,
            "median": float(np.median(good)),
            "std_um": float(good.std(ddof=1) * 1000) if good.size > 1 else 0.0,
            "distance_mm": self.distance(mean),
            "min": float(good.min()),
            "max": float(good.max()),
        })
        return out

    def trend(self, seconds: float = 10.0, points: int = 300) -> list[list[float]]:
        with self.cv:
            if not self.buf:
                return []
            t_last = self.buf[-1][0]
            sel = [(t, v) for t, v in self.buf if t >= t_last - seconds]
        if not sel:
            return []
        step = max(1, len(sel) // points)
        out = []
        for i in range(0, len(sel), step):
            chunk = [v for _, v in sel[i:i + step] if self.valid(v)]
            t = sel[i][0] - t_last
            out.append([round(t, 3), round(self.distance(float(np.mean(chunk))), 5) if chunk else None])
        return out

    def describe(self) -> str:
        return self.driver

    def snapshot(self) -> dict:
        lr = self.latest() if self.running else None
        smr = float(self._cfgl()["smr_mm"])
        rng = float(self._cfgl()["range_mm"])
        return {
            "driver": self.driver,
            "running": self.running,
            "connected": self.connected,
            "uptime_s": round(time.monotonic() - getattr(self, "connected_t", time.monotonic())) if self.connected else 0,
            "error": self.error,
            "latest": lr,
            "rate_hz": round(self.rate_hz),
            "mode": self.mode(),
            "mode_source": self._cfgl().get("reading_zero", "auto"),
            "smr_mm": smr,
            "range_mm": rng,
            "mid_mm": smr + rng / 2,
            "describe": self.describe(),
        }


class NoLaser(LaserBase):
    driver = "none"

    def start(self) -> None:
        self.running = False

    def collect(self, n, after=None, timeout=5.0):
        raise TimeoutError("No laser connected. Connect it on the Laser tab.")

    def snapshot(self) -> dict:
        s = super().snapshot()
        s["latest"] = None
        return s


class MedaqLaser(LaserBase):
    driver = "medaq"

    def __init__(self, cfg, bus):
        super().__init__(cfg, bus)
        self.dev = None

    def describe(self) -> str:
        c = self._cfgl()
        u = getattr(self, "used", None) or {}
        return f"ILD1320 via {u.get('interface') or c.get('interface')} {u.get('port') or c.get('port')}" + \
               (f" @ {u['baud']}" if u.get("baud") else "")

    def _open(self) -> None:
        from .medaq import open_ild1320
        c = self._cfgl()
        p = c.get("dll_path") or "drivers/MEDAQLib.dll"
        path = (ROOT / p) if not (len(p) > 1 and p[1] == ":") else p
        dev, used = open_ild1320(path, c.get("port", ""), c.get("interface", "auto"), c.get("baud"),
                                 int(c.get("sensor_id", 41)), c.get("extra_params") or {},
                                 hint=c.get("last_ok"))
        self.dev = dev
        self.used = used
        self._errs = 0
        if c.get("interface", "auto") == "auto" or c.get("port") != used["port"]:
            self.cfg.update({"laser": {"last_ok": used, "port": used["port"]}})

    def _close(self) -> None:
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    def _read(self) -> list[float]:
        try:
            n = self.dev.data_avail()
            if n <= 0:
                time.sleep(0.004)
                return []
            vals = self.dev.transfer(min(n, 20000))
            self._errs = 0
            return vals
        except Exception:
            self._errs += 1
            if self._errs > 25:          # occasional -7 dropouts are normal; a run of them is not
                raise
            time.sleep(0.01)
            return []


class SimLaser(LaserBase):
    """Fake ILD1320 looking down at a modelled surface (machine coordinates).

    Bed (the flat plate) at machine Z -125 with a few um of waviness (the
    "machine baseline"). On it, an MI350 OAM package side up, lid removed,
    from the CAD (0-G362_OAM ASM): PCB face +4.631, substrate 72 x 76.5 at
    +7.001, silicon 49.93 x 59.31 at +7.891 (0.89 mm step), frame window
    73 x 80 with the frame top at +13.131. Centred at OAM_C, rotated OAM_DEG,
    so the silicon finder has a real offset and angle to recover.
    """

    # As mounted on the plate (2026-11-23): board turned 90 deg vs the CAD, so the
    # 59.31 side runs along X. The die's flat top ends at SI; past it the surface
    # rolls off over ROLL mm (ROLL_DROP lower at its foot), then the substrate is
    # uneven by ~0.15 mm - all as seen on the real part.
    driver = "sim"
    BED_Z = -125.0
    OAM_C = (216.0, 217.5)
    OAM_DEG = 0.35
    SI = (59.31, 49.93, 7.891)
    SUB = (76.5, 72.0, 7.001)
    WINDOW = (80.0, 73.0, 4.631)
    BOARD = (177.5, 101.0, 13.131)
    ROLL = 0.6
    ROLL_DROP = 0.45
    DOME_UM = 100.0
    DIE_TILT_UM_PER_MM = 0.15

    def __init__(self, cfg, bus, position_fn):
        super().__init__(cfg, bus)
        self.position_fn = position_fn
        self.rate = 1000.0
        self._last = time.monotonic()
        self._t0 = time.monotonic()
        self.noise_um = 0.8

    def describe(self) -> str:
        return "Simulated ILD1320"

    def _open(self) -> None:
        self._last = time.monotonic()

    def surface(self, x: float, y: float) -> float | None:
        z = self.BED_Z + 0.004 * math.sin(x / 37.0) * math.cos(y / 53.0) + 0.00002 * (x - 200) + 0.00001 * (y - 200)
        a = math.radians(self.OAM_DEG)
        dx, dy = x - self.OAM_C[0], y - self.OAM_C[1]
        u = dx * math.cos(a) + dy * math.sin(a)          # package frame
        v = -dx * math.sin(a) + dy * math.cos(a)
        bw, bh, btop = self.BOARD
        if abs(u) > bw / 2 or abs(v) > bh / 2:
            return z                                      # bare plate
        sw, sh, stop = self.SI
        p, q = u / (sw / 2), v / (sh / 2)
        die_top = z + stop + self.DOME_UM / 1000 * (1 - (p * p + q * q) / 2) + self.DIE_TILT_UM_PER_MM / 1000 * u
        if abs(u) <= sw / 2 and abs(v) <= sh / 2:
            return die_top
        out = max(abs(u) - sw / 2, abs(v) - sh / 2)        # distance past the flat top
        if out <= self.ROLL:
            return die_top - self.ROLL_DROP * out / self.ROLL  # edge roll-off
        uw, uh, utop = self.SUB
        if abs(u) <= uw / 2 and abs(v) <= uh / 2:
            return z + utop + 0.15 * math.sin(u * 1.3) * math.cos(v * 0.9)   # uneven outside surface
        ww, wh, wtop = self.WINDOW
        if abs(u) <= ww / 2 and abs(v) <= wh / 2:
            return z + wtop
        return z + btop

    def _read(self) -> list[float]:
        time.sleep(0.01)
        now = time.monotonic()
        n = int((now - self._last) * self.rate)
        if n <= 0:
            return []
        self._last += n / self.rate
        pos = self.position_fn()
        if pos is None:
            return [SENTINEL * 100] * n
        x, y, zf = pos
        s = self.surface(x, y)
        smr = float(self._cfgl()["smr_mm"])
        rng = float(self._cfgl()["range_mm"])
        drift = 0.0006 * math.sin((now - self._t0) / 400.0)      # slow thermal drift, ~0.6 um
        out = []
        for _ in range(n):
            if s is None or random.random() < 0.002:
                out.append(SENTINEL * 100)
                continue
            d = zf - s + drift + random.gauss(0, self.noise_um / 1000)
            if d < smr or d > smr + rng:
                out.append(SENTINEL * 100)
            else:
                out.append(d - smr)                              # 0..25 like the real sensor
        return out
