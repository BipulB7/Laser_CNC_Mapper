"""Find the silicon die outline: manual corners, laser refine, or fully automatic.

Real de-lidded MI350 OAM (2026-11-23, standoff on the die): the die is flat
to ~0.1 mm (37.50 mm at the centre, ~37.6 mm near the edges = slight dome).
Its edge is NOT a sharp step: it rolls off over ~0.6 mm (37.8 -> 38.25 mm),
then the surface outside is uneven (38.2-38.5 mm), and ~5 mm further out
there is a feature 2.2 mm higher. The board sits turned 90 deg from the CAD:
the 59.31 mm side runs along machine X, 49.93 mm along Y.

Three ways to get the outline (all end in the same place: work zero at the die
centre, rotation stored, outline drawn, Trace outline available):

  Manual corners  You put the laser spot on each corner and press "= here".
                  Rectangle fit: centre = mean of corners, rotation = mean edge
                  angle, size = mean of opposite edges. Quality = diagonal
                  mismatch and worst corner residual. About +/-0.1-0.2 mm.
  Refine          From the current outline, measures each edge at two points
                  across the roll-off: coarse 0.25 mm steps from 3 mm inside,
                  then 0.05 mm steps. Edge = the TOP of the roll-off: first
                  point that falls more than `fine_threshold` (0.1 mm) below
                  the flat die level, measured 1-2 mm inside (pinned, so it
                  cannot creep down the slope). About +/-0.02-0.05 mm.
  Auto find       Walks out from work zero in +/-X, +/-Y to get a rough
                  outline, then runs the same precise edge stage as Refine.
"""

from __future__ import annotations

import math
import time

import numpy as np

from .machine import Job, Machine, Refused
from .scan import measure_here, point_row
from .sessions import Session

TEMPLATES = {
    "mi350_oam": {
        "name": "MI350 OAM silicon (as mounted: 59.31 along X)",
        "width": 59.31, "height": 49.93,
        "note": "CAD 0-G362: 49.93 x 59.31 mm. On the plate the board is turned 90 deg, so X 59.31, Y 49.93.",
    },
}

DEFAULTS = {"threshold": 0.25, "fine_threshold": 0.1, "reach": 45.0, "rough_step": 1.0, "coarse_step": 0.25,
            "fine_step": 0.05, "back_off": 3.0, "along": 0.6, "lag": 1.0, "samples": 30, "settle_ms": 120}
CORNER_NAMES = ["front-left", "front-right", "back-right", "back-left"]   # -u-v, +u-v, +u+v, -u+v


class EdgeError(Exception):
    pass


def off_die(h: float | None, ref: float, threshold: float) -> bool:
    return h is None or not math.isfinite(h) or abs(h - ref) > threshold


def walk_out(measure, s_start: float, s_max: float, step: float, ref: float, threshold: float,
             confirm: int = 3, lag: float | None = 1.0) -> dict:
    """Walk outward measuring until `confirm` points in a row are off the die.

    measure(s) -> height (mm) or None. The on-die reference is the median of
    on-die points at least `lag` mm behind the current point, so it follows
    the die's slow bow but cannot creep down a gradual edge roll-off.
    lag=None pins the reference to `ref` for the whole walk.
    Returns {"s_in", "s_out", "level", "outside", "n"}.
    """
    on: list[tuple[float, float]] = []
    last_in = None
    pending: list[tuple[float, float | None]] = []
    s = s_start
    n = 0
    while s <= s_max + 1e-9:
        h = measure(s)
        n += 1
        if lag is None:
            local = ref
        else:
            old = [hh for ss, hh in on if abs(s - ss) >= lag - 1e-9]
            local = float(np.median(old[-4:])) if old else ref
        if off_die(h, local, threshold):
            pending.append((s, h))
            if len(pending) >= confirm:
                if last_in is None:
                    raise EdgeError(f"the start point ({s_start:+.2f} mm) is not on the die")
                return {"s_in": last_in, "s_out": pending[0][0], "level": local, "outside": pending[0][1], "n": n,
                        "tail": list(pending)}
        else:
            on.append((s, h))
            last_in = s
            pending = []
        s += step
    raise EdgeError(f"no edge within {s_max:g} mm (still on the die, or the threshold is too large)")


def knee(level: float, tail: list, s_in: float, s_out: float, step: float) -> float:
    """Where the roll-off, extended back up, meets the flat die level (= top corner of the die).

    tail: the first off-die (s, h) points of the fine walk, all on the slope.
    Falls back to the midpoint between the last on-die and first off-die point.
    """
    mid = (s_in + s_out) / 2
    pts = [(ss, hh) for ss, hh in tail if hh is not None and math.isfinite(hh)]
    if len(pts) < 2:
        return mid
    S_ = np.array([p[0] for p in pts])
    H_ = np.array([p[1] for p in pts])
    slope, icpt = np.polyfit(S_, H_, 1)
    if abs(slope) < 1e-6:
        return mid
    k = float((level - icpt) / slope)
    return k if s_in - 1.0 <= k <= s_out + step else mid     # the corner can sit up to ~threshold/slope before s_out


def fit_rectangle(edges: dict) -> dict:
    """edges: {"left": [(x, y), (x, y)], "right", "bottom", "top"}: points ON each edge (work coords)."""
    edges = {k: sorted(v, key=(lambda p: p[1]) if k in ("left", "right") else (lambda p: p[0]))
             for k, v in edges.items()}
    angs = []
    for side in ("left", "right"):
        (x1, y1), (x2, y2) = edges[side]
        angs.append(-math.atan2(x2 - x1, y2 - y1))
    for side in ("bottom", "top"):
        (x1, y1), (x2, y2) = edges[side]
        angs.append(math.atan2(y2 - y1, x2 - x1))
    th = float(np.mean(angs))
    c, s = math.cos(-th), math.sin(-th)
    rot = {k: [(x * c - y * s, x * s + y * c) for x, y in v] for k, v in edges.items()}
    left = np.mean([p[0] for p in rot["left"]])
    right = np.mean([p[0] for p in rot["right"]])
    bottom = np.mean([p[1] for p in rot["bottom"]])
    top = np.mean([p[1] for p in rot["top"]])
    cu, cv = (left + right) / 2, (bottom + top) / 2
    c2, s2 = math.cos(th), math.sin(th)
    return {
        "center": [float(cu * c2 - cv * s2), float(cu * s2 + cv * c2)],
        "width": float(right - left),
        "height": float(top - bottom),
        "angle_deg": float(math.degrees(th)),
        "edge_angles_deg": [round(math.degrees(a), 4) for a in angs],
        "angle_spread_deg": float(math.degrees(max(angs) - min(angs))),
    }


def order_corners(pts: list) -> list:
    """Any 4 corner points -> counter-clockwise starting at the front-left one (names don't matter)."""
    P = np.asarray(pts, dtype=float)
    c = P.mean(axis=0)
    ang = np.arctan2(P[:, 1] - c[1], P[:, 0] - c[0])
    P = P[np.argsort(ang)]                       # CCW from about -180 deg
    k = int(np.argmin(P[:, 0] + P[:, 1]))         # front-left = smallest x + y
    return [list(map(float, p)) for p in np.roll(P, -k, axis=0)]


def die_corners(center, width: float, height: float, angle_deg: float, offset: float = 0.0) -> list[list[float]]:
    """Corners in the same frame as `center`, CCW from (-u, -v). offset > 0 = outside the die."""
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    hw, hh = width / 2 + offset, height / 2 + offset
    return [[round(center[0] + u * c - v * s, 4), round(center[1] + u * s + v * c, 4)]
            for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def fit_corners(pts: list) -> dict:
    """Rectangle from 4 hand-placed corners (any order)."""
    if len(pts) != 4:
        raise Refused("Need all 4 corners.")
    fl, fr, br, bl = [np.array(p) for p in order_corners(pts)]
    cen = (fl + fr + br + bl) / 4
    ang = [math.atan2(*(fr - fl)[::-1]), math.atan2(*(br - bl)[::-1]),
           math.atan2(*(bl - fl)[::-1]) - math.pi / 2, math.atan2(*(br - fr)[::-1]) - math.pi / 2]
    ang = [math.atan2(math.sin(a), math.cos(a)) for a in ang]
    th = float(np.mean(ang))
    W = float((np.linalg.norm(fr - fl) + np.linalg.norm(br - bl)) / 2)
    H = float((np.linalg.norm(bl - fl) + np.linalg.norm(br - fr)) / 2)
    if W < 1 or H < 1:
        raise Refused("Corners are too close together.")
    ideal = np.array(die_corners(cen, W, H, math.degrees(th)))
    resid = np.linalg.norm(np.array([fl, fr, br, bl]) - ideal, axis=1)
    return {
        "center": [float(cen[0]), float(cen[1])], "width": W, "height": H, "angle_deg": math.degrees(th),
        "edge_angles_deg": [round(math.degrees(a), 4) for a in ang],
        "angle_spread_deg": float(math.degrees(max(ang) - min(ang))),
        "diagonal_mismatch_mm": float(abs(np.linalg.norm(br - fl) - np.linalg.norm(bl - fr))),
        "corner_residual_mm": [round(float(r), 4) for r in resid],
        "worst_corner_mm": float(resid.max()),
    }


class _Prober:
    """Moves + measures one point at a time and logs it to the locate session."""

    def __init__(self, machine: Machine, laser, job: Job, sess: Session, bus, samples: int, settle: int):
        self.m, self.l, self.job, self.sess, self.bus = machine, laser, job, sess, bus
        self.samples, self.settle = samples, settle
        self.t0 = time.monotonic()
        self.n = 0

    def height(self, x: float, y: float, label: str) -> float | None:
        self.job.checkpoint()
        wco = self.m.g.wco
        self.m.job_move(self.job, x=x + wco[0], y=y + wco[1], feed=1500)
        st, _, flag = measure_here(self.m, self.l, self.job, self.samples, self.settle, 0)
        row = point_row(self.m, st, {"i": self.n, "region": label, "label": f"{label} ({x:+.3f},{y:+.3f})", "flag": flag}, self.t0)
        self.sess.add_point(row)
        self.n += 1
        h = row["height_mm"]
        self.bus.publish("scan_point", {"session": self.sess.id, "i": self.n, "x": row["x_work"], "y": row["y_work"],
                                        "xm": row["x_mach"], "ym": row["y_mach"], "label": label, "h_um": None,
                                        "distance_mm": row["distance_mm"], "std_um": row["std_um"], "flag": flag})
        return None if h is None or flag == "out-of-range" else float(h)

    def walk(self, label, ox, oy, dx, dy, s0, s1, step, ref, thr, lag=1.0):
        return walk_out(lambda s: self.height(ox + dx * s, oy + dy * s, label), s0, s1, step, ref, thr, lag=lag)


def _precise_edges(pr: _Prober, job: Job, q: dict, cx: float, cy: float, a: float, b: float,
                   theta_deg: float, ref: float, first: int = 0, total: int = 8) -> tuple[dict, list]:
    """Two edge measurements per side along the part's own axes (rotated by theta)."""
    th = math.radians(theta_deg)
    eu = (math.cos(th), math.sin(th))            # part +u (width direction)
    ev = (-math.sin(th), math.cos(th))           # part +v (height direction)
    plan = []
    for k, f in ((1, -q["along"]), (2, q["along"])):
        for side, d, half, off_axis, off in (("right", eu, a, ev, f * b), ("left", (-eu[0], -eu[1]), a, ev, f * b),
                                             ("top", ev, b, eu, f * a), ("bottom", (-ev[0], -ev[1]), b, eu, f * a)):
            plan.append((f"{side}{k}", side, cx + off_axis[0] * off, cy + off_axis[1] * off, d, half))
    edges: dict[str, list] = {"left": [], "right": [], "bottom": [], "top": []}
    details = []
    back = float(q["back_off"])
    for n, (label, side, ox, oy, d, half) in enumerate(plan):
        job.detail["phase"] = f"edge {n + 1}/8: {label}"
        job.progress = {"done": first + n, "total": total}
        s0 = max(0.5, half - back)
        try:
            c1 = pr.walk(label, ox, oy, d[0], d[1], s0, half + back + 3, float(q["coarse_step"]), ref,
                         float(q["threshold"]), lag=float(q["lag"]))
            die_level = c1["level"]
            c2 = pr.walk(label + "-fine", ox, oy, d[0], d[1], max(s0, c1["s_in"] - 0.5), c1["s_out"] + 0.6,
                         float(q["fine_step"]), die_level, float(q["fine_threshold"]), lag=None)
        except EdgeError as e:
            raise Refused(f"{label}: {e}") from e
        e = knee(die_level, c2["tail"], c2["s_in"], c2["s_out"], float(q["fine_step"]))
        pt = (ox + d[0] * e, oy + d[1] * e)
        edges[side].append(pt)
        details.append({"profile": label, "edge_work": [round(pt[0], 4), round(pt[1], 4)], "die_level_mm": round(die_level, 4),
                        "coarse_edge": round((c1["s_in"] + c1["s_out"]) / 2, 3), "fine_edge": round(e, 4)})
    return edges, details


def _finish(machine: Machine, bus, fit: dict, source: str, sess_id: str | None, q: dict) -> dict:
    """Common end: checks, work zero to the centre, store + publish the outline."""
    warn = list(fit.get("warnings", []))
    W, H = q.get("width"), q.get("height")
    if W and H:
        W, H = float(W), float(H)
        fit["nominal"] = [W, H]
        fit["size_error_mm"] = [round(fit["width"] - W, 4), round(fit["height"] - H, 4)]
        if abs(fit["width"] - H) < 1.0 and abs(fit["height"] - W) < 1.0 and abs(fit["width"] - W) > 1.0:
            warn.append("size matches the expected size turned 90 degrees (press Swap W/H?)")
        elif max(abs(v) for v in fit["size_error_mm"]) > 1.0:
            warn.append(f"size differs from the expected {W:g} x {H:g} by more than 1 mm")
    if fit["angle_spread_deg"] > 1.0:
        warn.append(f"edges disagree on rotation by {fit['angle_spread_deg']:.2f} deg")
    fit["warnings"] = warn
    fit["ok"] = not warn
    fit["source"] = source
    wco = machine.g.wco
    cm = [fit["center"][0] + wco[0], fit["center"][1] + wco[1]]
    fit["center_machine"] = [round(v, 4) for v in cm]
    fit["corners_machine"] = die_corners(cm, fit["width"], fit["height"], fit["angle_deg"])
    machine.set_work_origin_machine_xy(cm[0], cm[1])
    cfg = machine.cfg.update({"align": {"angle_deg": round(fit["angle_deg"], 5), "width": round(fit["width"], 4),
                                        "height": round(fit["height"], 4), "center_machine": fit["center_machine"],
                                        "corners_machine": fit["corners_machine"], "source": source,
                                        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "session": sess_id}})
    bus.publish("config", {"config": cfg})
    msg = (f"Die outline ({source}): {fit['width']:.3f} x {fit['height']:.3f} mm, rotation {fit['angle_deg']:+.3f} deg. "
           "Work zero moved to its centre.")
    bus.note("warn" if warn else "ok", msg + (" Note: " + "; ".join(warn) if warn else ""))
    return fit


def _job_session(name: str, q: dict, machine: Machine, laser) -> Session:
    return Session.create("locate", name, q, {"wco_before": list(machine.g.wco), "homed": machine.homed_how or "no",
                                              "laser": laser.snapshot()})


def _close_session(sess: Session, bus, fit: dict | None, err: str | None = None) -> None:
    if fit is not None:
        sess.save_results({"locate": fit})
        sess.update_meta(status="done", n_done=len(sess.points()),
                         locate={k: fit.get(k) for k in ("center_machine", "width", "height", "angle_deg", "warnings", "source")})
    else:
        sess.update_meta(status="failed", error=err, n_done=len(sess.points()))
    bus.publish("sessions_changed", {"id": sess.id})


def find_silicon(machine: Machine, laser, bus, job: Job, p: dict) -> dict:
    """Automatic: rough outward walks from work zero, then the precise edge stage."""
    q = {**DEFAULTS, **{k: v for k, v in p.items() if v not in (None, "")}}
    sess = _job_session("Find silicon (auto)", q, machine, laser)
    job.result = {"session": sess.id}
    machine.g.send("G21G90", timeout=5)
    pr = _Prober(machine, laser, job, sess, bus, int(q["samples"]), int(q["settle_ms"]))
    try:
        ref = pr.height(0.0, 0.0, "centre")
        if ref is None:
            raise Refused("No laser reading at work zero. Put the laser on the die (work zero there) and set the standoff.")
        rough = {}
        for n, (side, dx, dy) in enumerate((("right", 1, 0), ("top", 0, 1), ("left", -1, 0), ("bottom", 0, -1))):
            job.detail["phase"] = f"rough outline: {side}"
            job.progress = {"done": n, "total": 12}
            try:
                r = pr.walk(f"rough-{side}", 0.0, 0.0, dx, dy, float(q["rough_step"]), float(q["reach"]),
                            float(q["rough_step"]), ref, float(q["threshold"]), lag=float(q["lag"]))
            except EdgeError as e:
                raise Refused(f"Rough {side} walk: {e}. Is work zero on the die?") from e
            rough[side] = (r["s_in"] + r["s_out"]) / 2
        cx, cy = (rough["right"] - rough["left"]) / 2, (rough["top"] - rough["bottom"]) / 2
        a, b = (rough["right"] + rough["left"]) / 2, (rough["top"] + rough["bottom"]) / 2
        if min(a, b) < 2 * float(q["back_off"]):
            raise Refused(f"Rough outline only {2 * a:.1f} x {2 * b:.1f} mm: work zero may be on a small feature, not the die.")
        edges, details = _precise_edges(pr, job, q, cx, cy, a, b, 0.0, ref, first=4, total=12)
        fit = fit_rectangle(edges)
        fit.update({"profiles": details, "rough": rough})
        if fit["angle_spread_deg"] > 1.0:
            raise Refused(f"Edges disagree on rotation ({fit['angle_spread_deg']:.2f} deg). Work zero NOT moved; see the locate session.")
        fit = _finish(machine, bus, fit, "auto", sess.id, q)
    except Exception as e:
        _close_session(sess, bus, None, str(e))
        raise
    _close_session(sess, bus, fit)
    job.result.update({"fit": fit})
    return fit


def refine(machine: Machine, laser, bus, job: Job, p: dict) -> dict:
    """Precise edges starting from the current outline (manual corners or a previous find)."""
    a0 = machine.cfg.snapshot().get("align")
    if not a0 or not a0.get("center_machine"):
        raise Refused("No outline to refine. Enter the 4 corners (or run Auto find) first.")
    q = {**DEFAULTS, **{k: v for k, v in p.items() if v not in (None, "")}}
    sess = _job_session("Refine die outline", q, machine, laser)
    job.result = {"session": sess.id}
    machine.g.send("G21G90", timeout=5)
    pr = _Prober(machine, laser, job, sess, bus, int(q["samples"]), int(q["settle_ms"]))
    wco = machine.g.wco
    cx, cy = a0["center_machine"][0] - wco[0], a0["center_machine"][1] - wco[1]
    try:
        ref = pr.height(cx, cy, "centre")
        if ref is None:
            raise Refused("No laser reading at the outline centre. Set the standoff on the die first.")
        edges, details = _precise_edges(pr, job, q, cx, cy, a0["width"] / 2, a0["height"] / 2,
                                        float(a0["angle_deg"]), ref, first=0, total=8)
        fit = fit_rectangle(edges)
        fit.update({"profiles": details,
                    "change_from_previous_mm": {"centre": [round(fit["center"][0] - cx, 4), round(fit["center"][1] - cy, 4)],
                                                "size": [round(fit["width"] - a0["width"], 4), round(fit["height"] - a0["height"], 4)],
                                                "angle_deg": round(fit["angle_deg"] - a0["angle_deg"], 4)}})
        if fit["angle_spread_deg"] > 1.0:
            raise Refused(f"Edges disagree on rotation ({fit['angle_spread_deg']:.2f} deg). Outline NOT changed; see the locate session.")
        fit = _finish(machine, bus, fit, "laser refine", sess.id, q)
    except Exception as e:
        _close_session(sess, bus, None, str(e))
        raise
    _close_session(sess, bus, fit)
    job.result.update({"fit": fit})
    return fit


def manual_corners(machine: Machine, bus, corners_machine: list, params: dict) -> dict:
    """Outline from 4 hand-placed corners (machine X/Y, any order). No motion."""
    if not machine.g.connected:
        raise Refused("Not connected to the CNC.")
    pts = [[float(c[0]), float(c[1])] for c in corners_machine if c and c[0] is not None and c[1] is not None]
    if len(pts) != 4:
        raise Refused("Set all 4 corners first.")
    wco = machine.g.wco
    fit = fit_corners([[x - wco[0], y - wco[1]] for x, y in pts])
    if fit["worst_corner_mm"] > 1.0:
        fit.setdefault("warnings", []).append(f"one corner is {fit['worst_corner_mm']:.2f} mm off a true rectangle (mis-placed?)")
    if fit["diagonal_mismatch_mm"] > 0.5:
        fit.setdefault("warnings", []).append(f"diagonals differ by {fit['diagonal_mismatch_mm']:.2f} mm")
    fit["corners_entered_machine"] = order_corners(pts)
    return _finish(machine, bus, fit, "manual corners", None, params)


def start_find_silicon(machine: Machine, laser, bus, params: dict) -> Job:
    _ready(machine, laser)
    p = _with_template(params)
    return machine.start_job("locate", "Find silicon (auto)", lambda job: find_silicon(machine, laser, bus, job, p))


def start_refine(machine: Machine, laser, bus, params: dict) -> Job:
    _ready(machine, laser)
    p = _with_template(params)
    return machine.start_job("locate", "Refine die outline", lambda job: refine(machine, laser, bus, job, p))


def _ready(machine: Machine, laser) -> None:
    if not laser or not laser.running:
        raise Refused("Connect the laser first.")
    machine.check_target(list(machine.g.mpos), "Finding the die")
    lr = laser.latest()
    if not lr or not lr.get("valid"):
        raise Refused("Laser is out of range here. Put the laser over the die and set the standoff first.")


def _with_template(params: dict) -> dict:
    t = TEMPLATES.get(params.get("template", ""), {})
    return {**{k: t[k] for k in ("width", "height") if k in t}, **{k: v for k, v in params.items() if v not in (None, "")}}


def start_trace(machine: Machine, bus, params: dict) -> Job:
    """Drive the laser spot round the die outline so it can be checked by eye."""
    a = machine.cfg.snapshot().get("align")
    if not a or not a.get("center_machine"):
        raise Refused("No die outline yet. Enter the corners or run a find first.")
    offset = float(params.get("offset_mm") or 0.0)
    feed = float(params.get("feed") or 300.0)
    stop = bool(params.get("stop_at_corners", True))
    loops = max(1, min(10, int(params.get("loops") or 1)))
    corners = die_corners(a["center_machine"], a["width"], a["height"], a["angle_deg"], offset)
    z = machine.g.mpos[2]
    for x, y in corners:
        machine.check_target([x, y, z], "Trace outline")
    seq = [0] + [1, 2, 3, 0] * loops          # start corner, then round the die `loops` times

    def run(job: Job):
        machine.g.send("G21G90", timeout=5)
        last = len(seq) - 1
        for n, i in enumerate(seq):
            f = float(machine.cfg["machine"]["travel_feed_xy"]) if n == 0 else feed
            machine.job_move(job, x=corners[i][0], y=corners[i][1], feed=f)
            job.progress = {"done": n, "total": last}
            if n == last:
                break
            where = f"Corner {i + 1}/4 ({CORNER_NAMES[i]})" + (f", loop {n // 4 + 1}/{loops}" if loops > 1 else "")
            if stop:
                extra = f" ({offset:+.2f} mm outside)" if offset > 0 else f" ({-offset:.2f} mm inside)" if offset < 0 else ""
                machine.prompt_pause(job, f"{where}: check the laser spot sits on the die corner{extra}. Resume for the next one.")
                job.message = ""
            else:
                job.message = where
                machine.job_dwell(job, 1.0)

    return machine.start_job("trace", "Trace die outline", run)
