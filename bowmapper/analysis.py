"""Height-map analysis: tilt, warpage, signed bow, curvature, region steps.

Conventions
  height  = machine Z of the sensor - distance from the sensor face (mm),
            so larger height = surface closer to the laser (higher).
  bow     = (fitted quadratic at the centre) - (mean of it at the four
            corners), after removing the best-fit plane. Positive = dome
            (centre high, convex toward the laser), negative = dish.
  warpage = peak-to-valley of the residual after removing the best-fit plane.
"""

from __future__ import annotations

import math

import numpy as np


def _valid(points: list[dict], key: str = "height_mm"):
    rows = [p for p in points if p.get(key) not in (None, "") and _isnum(p.get(key))]
    if not rows:
        return np.empty(0), np.empty(0), np.empty(0), rows
    x = np.array([float(p["x_work"]) for p in rows])
    y = np.array([float(p["y_work"]) for p in rows])
    h = np.array([float(p[key]) for p in rows])
    return x, y, h, rows


def _isnum(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def plane_fit(x, y, h):
    A = np.c_[x, y, np.ones_like(x)]
    coef, *_ = np.linalg.lstsq(A, h, rcond=None)
    return coef  # a, b, c : h = a x + b y + c


def surface_metrics(x, y, h) -> dict:
    """Metrics for one surface (heights in mm, coordinates in mm)."""
    n = len(h)
    if n < 3:
        return {"n_valid": int(n), "ok": False, "reason": "fewer than 3 valid points"}
    a, b, c = plane_fit(x, y, h)
    resid = h - (a * x + b * y + c)
    out = {
        "n_valid": int(n),
        "ok": True,
        "mean_height_mm": float(h.mean()),
        "slope_x_um_per_mm": float(a * 1000),
        "slope_y_um_per_mm": float(b * 1000),
        "tilt_mrad": float(math.atan(math.hypot(a, b)) * 1000),
        "tilt_dir_deg": float(math.degrees(math.atan2(b, a))),
        "warpage_pv_um": float((resid.max() - resid.min()) * 1000),
        "rms_um": float(np.sqrt(np.mean(resid ** 2)) * 1000),
        "resid_min_um": float(resid.min() * 1000),
        "resid_max_um": float(resid.max() * 1000),
        "extent_mm": [float(x.max() - x.min()), float(y.max() - y.min())],
        "center_mm": [float((x.max() + x.min()) / 2), float((y.max() + y.min()) / 2)],
    }
    span_x = x.max() - x.min()
    span_y = y.max() - y.min()
    if n >= 6 and span_x > 0 and span_y > 0:
        xc, yc = out["center_mm"]
        u, v = x - xc, y - yc
        A = np.c_[np.ones_like(u), u, v, u * u, u * v, v * v]
        q, *_ = np.linalg.lstsq(A, h, rcond=None)
        cxx, cxy, cyy = q[3], q[4], q[5]
        hx, hy = span_x / 2, span_y / 2
        bow = -(cxx * hx * hx + cyy * hy * hy) * 1000
        out.update({
            "bow_um": float(bow),
            "shape": "dome (centre high)" if bow > 0 else "dish (centre low)",
            "curv_x_per_m": float(2 * cxx * 1000),
            "curv_y_per_m": float(2 * cyy * 1000),
            "radius_x_m": float(1 / (2 * cxx) / 1000) if abs(cxx) > 1e-12 else None,
            "radius_y_m": float(1 / (2 * cyy) / 1000) if abs(cyy) > 1e-12 else None,
            "twist_um": float(cxy * hx * hy * 1000),
            "quad_fit_rms_um": float(np.sqrt(np.mean((h - A @ q) ** 2)) * 1000),
        })
    elif n >= 3:
        # A line scan: fit a parabola along the longer direction.
        t = x if span_x >= span_y else y
        tc = (t.max() + t.min()) / 2
        if t.max() > t.min():
            p = np.polyfit(t - tc, h, 2)
            half = (t.max() - t.min()) / 2
            bow = -p[0] * half * half * 1000
            out.update({"bow_um": float(bow), "shape": "dome (centre high)" if bow > 0 else "dish (centre low)",
                        "radius_m": float(1 / (2 * p[0]) / 1000) if abs(p[0]) > 1e-12 else None})
    return out


def grid_maps(points: list[dict], key: str = "height_mm") -> list[dict]:
    """Per-region 2-D arrays (row = iy) of raw height and plane-removed residual (um)."""
    regions: dict[str, list[dict]] = {}
    for p in points:
        regions.setdefault(p.get("region") or "Region", []).append(p)
    out = []
    for name, rows in regions.items():
        try:
            nx = max(int(p["ix"]) for p in rows) + 1
            ny = max(int(p["iy"]) for p in rows) + 1
        except (KeyError, ValueError, TypeError):
            continue
        xs = [None] * nx
        ys = [None] * ny
        raw = [[None] * nx for _ in range(ny)]
        res = [[None] * nx for _ in range(ny)]
        x, y, h, vrows = _valid(rows, key)
        plane = plane_fit(x, y, h) if len(h) >= 3 else None
        for p in rows:
            ix, iy = int(p["ix"]), int(p["iy"])
            xs[ix] = float(p["u"]) if _isnum(p.get("u")) else float(p["x_work"])
            ys[iy] = float(p["v"]) if _isnum(p.get("v")) else float(p["y_work"])
            v = p.get(key)
            if _isnum(v):
                v = float(v)
                raw[iy][ix] = round(v, 6)
                if plane is not None:
                    a, b, c = plane
                    res[iy][ix] = round((v - (a * float(p["x_work"]) + b * float(p["y_work"]) + c)) * 1000, 3)
        out.append({"region": name, "x": xs, "y": ys, "height_mm": raw, "residual_um": res})
    return out


# ------------------------------------------------------------------ corrections
def drift_correction(points: list[dict], drift: list[dict]) -> tuple[list[dict], dict]:
    """Subtract the drift-point change (linear in time) from each height."""
    d = [r for r in drift if _isnum(r.get("height_mm")) and _isnum(r.get("t"))]
    info = {"applied": False, "n": len(d)}
    if len(d) < 2:
        return points, info
    t = np.array([float(r["t"]) for r in d])
    h = np.array([float(r["height_mm"]) for r in d])
    order = np.argsort(t)
    t, h = t[order], h[order]
    out = []
    for p in points:
        q = dict(p)
        if _isnum(p.get("height_mm")) and _isnum(p.get("t")):
            corr = float(np.interp(float(p["t"]), t, h)) - h[0]
            q["height_mm"] = float(p["height_mm"]) - corr
            q["drift_um"] = round(corr * 1000, 3)
        out.append(q)
    info.update({"applied": True, "span_um": float((h.max() - h.min()) * 1000),
                 "end_minus_start_um": float((h[-1] - h[0]) * 1000), "duration_s": float(t[-1] - t[0])})
    return out, info


def baseline_correction(points: list[dict], baseline: list[dict]) -> tuple[list[dict], dict]:
    """Subtract the reference-flat map (plane removed) at each point's machine XY."""
    bx = np.array([float(p["x_mach"]) for p in baseline if _isnum(p.get("height_mm"))])
    by = np.array([float(p["y_mach"]) for p in baseline if _isnum(p.get("height_mm"))])
    bh = np.array([float(p["height_mm"]) for p in baseline if _isnum(p.get("height_mm"))])
    info = {"applied": False}
    if bh.size < 3:
        info["reason"] = "baseline has fewer than 3 valid points"
        return points, info
    a, b, c = plane_fit(bx, by, bh)
    br = bh - (a * bx + b * by + c)
    pitch = _typical_pitch(bx, by)
    lo_x, hi_x = bx.min() - pitch, bx.max() + pitch
    lo_y, hi_y = by.min() - pitch, by.max() + pitch
    out = []
    outside = 0
    for p in points:
        q = dict(p)
        if _isnum(p.get("height_mm")):
            x, y = float(p["x_mach"]), float(p["y_mach"])
            if lo_x <= x <= hi_x and lo_y <= y <= hi_y:
                d2 = (bx - x) ** 2 + (by - y) ** 2
                k = min(4, d2.size)
                idx = np.argpartition(d2, k - 1)[:k]
                if d2[idx].min() < 1e-12:
                    corr = float(br[idx[np.argmin(d2[idx])]])
                else:
                    w = 1.0 / d2[idx]
                    corr = float(np.sum(w * br[idx]) / np.sum(w))
                q["height_mm"] = float(p["height_mm"]) - corr
                q["baseline_um"] = round(corr * 1000, 3)
            else:
                outside += 1
                q["flag"] = (q.get("flag") or "") + " no-baseline"
        out.append(q)
    info.update({"applied": True, "baseline_pv_um": float((br.max() - br.min()) * 1000),
                 "points_outside": outside})
    return out, info


def _typical_pitch(x, y) -> float:
    if x.size < 2:
        return 1.0
    ux = np.unique(np.round(x, 3))
    uy = np.unique(np.round(y, 3))
    steps = [np.median(np.diff(u)) for u in (ux, uy) if u.size > 1]
    return float(max(steps)) if steps else 1.0


# ------------------------------------------------------------------ session-level
def analyze(points: list[dict], drift: list[dict] | None = None, baseline: list[dict] | None = None,
            drift_correct: bool = True) -> dict:
    pts = [dict(p) for p in points]
    info: dict = {}
    if drift_correct and drift:
        pts, info["drift"] = drift_correction(pts, drift)
    else:
        info["drift"] = {"applied": False}
    if baseline:
        pts, info["baseline"] = baseline_correction(pts, baseline)
    else:
        info["baseline"] = {"applied": False}
    x, y, h, rows = _valid(pts)
    overall = surface_metrics(x, y, h)
    overall["n_points"] = len(pts)
    regions = []
    names = []
    for p in pts:
        r = p.get("region") or "Region"
        if r not in names:
            names.append(r)
    first_mean = None
    for name in names:
        sub = [p for p in pts if (p.get("region") or "Region") == name]
        rx, ry, rh, _ = _valid(sub)
        m = surface_metrics(rx, ry, rh)
        m["region"] = name
        m["n_points"] = len(sub)
        if m.get("ok"):
            if first_mean is None:
                first_mean = m["mean_height_mm"]
            m["step_vs_first_um"] = float((m["mean_height_mm"] - first_mean) * 1000)
        regions.append(m)
    flags = {}
    for p in pts:
        f = (p.get("flag") or "").strip()
        if f:
            for k in f.split():
                flags[k] = flags.get(k, 0) + 1
    return {
        "overall": overall,
        "regions": regions,
        "corrections": info,
        "flags": flags,
        "maps": grid_maps(pts),
        "points_corrected": [
            {"i": p.get("i"), "x": p.get("x_work"), "y": p.get("y_work"), "h": p.get("height_mm")} for p in pts
        ] if len(pts) <= 20000 else [],
    }


def compare(a: list[dict], b: list[dict]) -> dict:
    """B minus A at matching work XY (plane removed from each first)."""
    def keyed(pts):
        x, y, h, rows = _valid(pts)
        if len(h) < 3:
            return {}
        pa, pb, pc = plane_fit(x, y, h)
        return {(round(float(r["x_work"]), 2), round(float(r["y_work"]), 2)):
                float(r["height_mm"]) - (pa * float(r["x_work"]) + pb * float(r["y_work"]) + pc) for r in rows}
    ka, kb = keyed(a), keyed(b)
    common = sorted(set(ka) & set(kb))
    if len(common) < 3:
        return {"ok": False, "reason": f"only {len(common)} matching points (scan the same grid)"}
    d = np.array([kb[k] - ka[k] for k in common]) * 1000
    return {
        "ok": True,
        "n": len(common),
        "mean_um": float(d.mean()),
        "pv_um": float(d.max() - d.min()),
        "rms_um": float(np.sqrt(np.mean((d - d.mean()) ** 2))),
        "points": [[k[0], k[1], round(float(v), 3)] for k, v in zip(common, d)],
    }
