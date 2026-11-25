"""App configuration: defaults, load/merge from config.json, save.

Everything a user might want to change lives here and is editable from the
Machine tab. Positions are stored in machine coordinates (they survive a
change of work zero).
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DIRS = {
    "scans": ROOT / "scans",
    "programs": ROOT / "programs",
    "recipes": ROOT / "recipes",
    "backups": ROOT / "backups",
    "logs": ROOT / "logs",
    "drivers": ROOT / "drivers",
}

DEFAULTS: dict = {
    "machine": {
        "transport": "serial",          # serial | tcp | sim
        "port": "",                     # COM port; "" = pick the likely CNC port
        "baud": 115200,
        "tcp_host": "192.168.0.1",
        "tcp_port": 23,
        "status_hz": 10,
        "reset_on_connect": False,      # False = keep DTR/RTS low so the ESP32 does not reboot
        # Travel limits in MACHINE coordinates. Re-derived after every homing
        # cycle from the homed position, $23, $27 and $130-$132.
        "limits": {"x": [0.0, 400.0], "y": [0.0, 400.0], "z": [-130.0, 0.0]},
        "limits_source": "settings",    # settings | homing | manual
        "limit_margin_mm": 0.5,
        "z_floor_mpos": None,           # never move the laser below this machine Z
        "safe_z_mpos": -2.5,            # machine Z used for "safe travel" moves
        "require_homing": True,         # block go-to / programs / scans until homed
        "block_spindle": True,          # refuse M3/M4: the laser is mounted on the spindle
        "unhomed_jog_max_mm": 25.0,     # continuous jog length cap when limits are unknown
        "travel_feed_xy": 2500.0,
        "travel_feed_z": 500.0,
    },
    "jog": {
        "feed_xy": 1000.0,             # $120=50 mm/s2: a held jog coasts v^2/2a (~2.8 mm at 1000)
        "feed_z": 400.0,
        "fast_factor": 2.5,
        "fine_factor": 0.2,
        "steps": [0.01, 0.1, 1.0, 10.0, 50.0],
        "default_step": 1.0,
        "watchdog_s": 0.6,              # continuous jog stops if the browser goes quiet this long
    },
    "laser": {
        "driver": "none",               # medaq | sim | none   ("medaq" = the real ILD1320)
        "backend": "auto",              # auto (MEDAQLib on Windows, direct serial elsewhere) | medaq | serial
        "serial_baud": None,            # direct serial: None = auto-detect (921600 first)
        "serial_last_ok": None,
        "dll_path": "drivers/MEDAQLib.dll",
        "sensor_id": 41,                # ME_SENSOR.SENSOR_ILD1320
        "interface": "auto",            # auto | RS232 | IF2001_USB (auto tries both, remembers what worked)
        "port": "auto",                 # auto = first FTDI (IF2001/USB) port, or e.g. "COM5"
        "last_ok": None,
        "baud": None,                   # None = MEDAQLib default
        "extra_params": {},             # any other MEDAQLib IP_ parameters
        "smr_mm": 25.0,                 # start of measuring range from the sensor face
        "range_mm": 25.0,               # measuring range (ILD1320-25)
        "reading_zero": "auto",         # auto | smr (0..25) | face (25..50)
        "guard_mm": 1.0,                # stop a Z-down move inside this much of the near end
        "auto_reconnect": True,
    },
    "scan": {
        "samples": 100,
        "settle_ms": 300,
        "feed": 1500.0,
        "pattern": "serpentine",        # serpentine | raster
        "drift_every": 50,              # revisit the first point every N points (0 = off)
        "retries": 2,
        "max_invalid_frac": 0.2,
        "max_std_um": 10.0,
        "raster_approach_mm": 0.5,
    },
    "ui": {
        "flip_x": False,                # mirror the map/jog arrows if the machine faces the other way
        "flip_y": False,
        "positions": [],                # [{name, x, y, z}] machine coordinates
        "theme": "auto",
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe config holder backed by config.json."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                self.data = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # a corrupt file must not stop the app; keep a copy
                path.rename(path.with_suffix(".corrupt.json"))
        for d in DIRS.values():
            d.mkdir(parents=True, exist_ok=True)
        self.save()

    def __getitem__(self, key):
        with self._lock:
            return self.data[key]

    def get(self, section: str, key: str, default=None):
        with self._lock:
            return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value) -> None:
        with self._lock:
            self.data.setdefault(section, {})[key] = value
            self.save()

    def update(self, patch: dict) -> dict:
        with self._lock:
            self.data = _merge(self.data, patch)
            self.save()
            return copy.deepcopy(self.data)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
