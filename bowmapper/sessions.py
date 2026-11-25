"""Scan sessions on disk: scans/<YYYYmmdd-HHMMSS>_<name>/

  meta.json     what, when, machine + laser state, status
  recipe.json   exactly what was asked for
  points.csv    one row per measured point, appended as it is measured
  drift.csv     repeated measurements of the drift point
  results.json  analysis (re-run any time from points.csv)
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import time
from pathlib import Path

from .config import DIRS

POINT_FIELDS = ["i", "region", "ix", "iy", "label", "x_work", "y_work", "z_work", "x_mach", "y_mach",
                "z_mach", "reading_mean", "reading_median", "std_um", "n_valid", "n", "distance_mm",
                "height_mm", "t", "time", "retries", "flag", "u", "v"]
DRIFT_FIELDS = ["k", "after_point", "x_mach", "y_mach", "z_mach", "distance_mm", "height_mm", "std_um", "t", "time"]


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "scan").strip())[:60].strip("_")
    return s or "scan"


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.name
        self._lock = threading.Lock()

    @classmethod
    def create(cls, kind: str, name: str, recipe: dict, meta: dict) -> "Session":
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = DIRS["scans"] / f"{ts}_{slug(name)}"
        n = 1
        while p.exists():
            n += 1
            p = DIRS["scans"] / f"{ts}_{slug(name)}_{n}"
        p.mkdir(parents=True)
        s = cls(p)
        (p / "recipe.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
        m = {"id": s.id, "kind": kind, "name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
             "status": "running", **meta}
        s.write_meta(m)
        for fname, fields in (("points.csv", POINT_FIELDS), ("drift.csv", DRIFT_FIELDS)):
            with open(p / fname, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(fields)
        return s

    @classmethod
    def open(cls, sid: str) -> "Session":
        p = DIRS["scans"] / Path(sid).name
        if not (p / "meta.json").exists():
            raise FileNotFoundError(f"No session {sid}")
        return cls(p)

    # ------------------------------------------------------------ writing
    def write_meta(self, meta: dict) -> None:
        tmp = self.path / "meta.tmp"
        tmp.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path / "meta.json")

    def update_meta(self, **kw) -> dict:
        with self._lock:
            m = self.meta()
            m.update(kw)
            self.write_meta(m)
            return m

    def _append(self, fname: str, fields: list[str], row: dict) -> None:
        with self._lock:
            with open(self.path / fname, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([_fmt(row.get(k)) for k in fields])

    def add_point(self, row: dict) -> None:
        self._append("points.csv", POINT_FIELDS, row)

    def add_drift(self, row: dict) -> None:
        self._append("drift.csv", DRIFT_FIELDS, row)

    def save_results(self, res: dict) -> None:
        (self.path / "results.json").write_text(json.dumps(res, default=_json_default), encoding="utf-8")

    # ------------------------------------------------------------ reading
    def meta(self) -> dict:
        return json.loads((self.path / "meta.json").read_text(encoding="utf-8"))

    def recipe(self) -> dict:
        p = self.path / "recipe.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _read_csv(self, fname: str) -> list[dict]:
        p = self.path / fname
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def points(self) -> list[dict]:
        return self._read_csv("points.csv")

    def drift(self) -> list[dict]:
        return self._read_csv("drift.csv")

    def results(self) -> dict | None:
        p = self.path / "results.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6f}" if abs(v) < 1e6 else f"{v:.6g}"
    return v


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
    except ImportError:
        pass
    return str(o)


def list_sessions() -> list[dict]:
    out = []
    for p in sorted(DIRS["scans"].iterdir(), reverse=True):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        mp = p / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({k: m.get(k) for k in ("id", "kind", "name", "sample_id", "created", "status",
                                          "n_points", "n_done", "summary", "duration_s")})
    return out


def delete_session(sid: str) -> None:
    src = DIRS["scans"] / Path(sid).name
    if not src.exists():
        raise FileNotFoundError(sid)
    trash = DIRS["scans"] / "_deleted"
    trash.mkdir(exist_ok=True)
    shutil.move(str(src), str(trash / src.name))
