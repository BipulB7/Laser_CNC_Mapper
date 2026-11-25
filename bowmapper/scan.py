"""Step-and-settle grid scans.

For every point: move, wait until GRBL reports the move finished and Idle,
wait the settle time, then average N fresh laser samples. Points that are
noisy or mostly out of range are re-measured (up to `retries`). Every N
points the head returns to the first point so slow drift (thermal, laser
warm-up) can be measured and removed.

Recipe (all X/Y in WORK coordinates, Z in machine coordinates):
{
  "name": "MI300X die", "sample_id": "SN123", "kind": "sample" | "baseline",
  "regions": [{"name": "Die", "x0": 0, "y0": 0, "x1": 40, "y1": 47}],
  "pitch_x": 1.0, "pitch_y": 1.0,
  "pattern": "serpentine" | "raster",
  "samples": 100, "settle_ms": 300, "feed": 1500,
  "z": null (= current Z), "drift_every": 50, "retries": 2,
  "max_invalid_frac": 0.2, "max_std_um": 10, "baseline": "<session id>" | null,
  "notes": ""
}
"""

from __future__ import annotations

import math
import time

from . import analysis
from .machine import Job, Machine, Refused
from .sessions import Session

MAX_POINTS = 200000


def _f(r: dict, k: str, default):
    v = r.get(k)
    if v is None or v == "":
        return default
    return float(v)


def normalize(recipe: dict, defaults: dict) -> dict:
    r = dict(recipe)
    r.setdefault("name", "scan")
    r.setdefault("kind", "sample")
    r["pattern"] = r.get("pattern") or defaults.get("pattern", "serpentine")
    r["samples"] = int(_f(r, "samples", defaults.get("samples", 100)))
    r["settle_ms"] = int(_f(r, "settle_ms", defaults.get("settle_ms", 300)))
    r["feed"] = _f(r, "feed", defaults.get("feed", 1500))
    r["drift_every"] = int(_f(r, "drift_every", defaults.get("drift_every", 50)))
    r["retries"] = int(_f(r, "retries", defaults.get("retries", 2)))
    r["max_invalid_frac"] = _f(r, "max_invalid_frac", defaults.get("max_invalid_frac", 0.2))
    r["max_std_um"] = _f(r, "max_std_um", defaults.get("max_std_um", 10))
    r["approach_mm"] = _f(r, "approach_mm", defaults.get("raster_approach_mm", 0.5))
    r["rotation_deg"] = _f(r, "rotation_deg", 0.0)
    regs = []
    for i, g in enumerate(r.get("regions") or []):
        g = dict(g)
        g.setdefault("name", f"Region {i + 1}")
        for k in ("x0", "y0", "x1", "y1"):
            if g.get(k) in (None, ""):
                raise Refused(f"{g['name']}: {k} is missing.")
            g[k] = float(g[k])
        g["pitch_x"] = _f(g, "pitch_x", _f(r, "pitch_x", 1.0))
        g["pitch_y"] = _f(g, "pitch_y", _f(r, "pitch_y", g["pitch_x"]))
        if g["pitch_x"] <= 0 or g["pitch_y"] <= 0:
            raise Refused(f"{g['name']}: pitch must be above 0.")
        regs.append(g)
    if not regs:
        raise Refused("Add at least one region.")
    r["regions"] = regs
    if r["samples"] < 1:
        raise Refused("Samples per point must be at least 1.")
    return r


def _axis(a: float, b: float, pitch: float) -> list[float]:
    lo, hi = min(a, b), max(a, b)
    span = hi - lo
    if span < 1e-9:
        return [lo]
    n = max(1, int(round(span / pitch)))
    return [lo + span * i / n for i in range(n + 1)]


def build_points(r: dict) -> list[dict]:
    """Ordered measurement points (work coordinates)."""
    pts = []
    for g in r["regions"]:
        xs = _axis(g["x0"], g["x1"], g["pitch_x"])
        ys = _axis(g["y0"], g["y1"], g["pitch_y"])
        for iy, y in enumerate(ys):
            order = list(range(len(xs)))
            if r["pattern"] == "serpentine" and iy % 2 == 1:
                order.reverse()
            for k, ix in enumerate(order):
                pts.append({"region": g["name"], "ix": ix, "iy": iy, "x": xs[ix], "y": ys[iy],
                            "row_start": k == 0})
                if len(pts) > MAX_POINTS:
                    raise Refused(f"More than {MAX_POINTS} points: increase the pitch.")
    return pts


def estimate(r: dict, rate_hz: float = 1000.0, accel: float = 50.0) -> dict:
    pts = build_points(r)
    feed = r["feed"] / 60.0
    t = 0.0
    prev = None
    for p in pts:
        if prev:
            d = math.hypot(p["x"] - prev["x"], p["y"] - prev["y"])
            t_acc = feed / accel
            t += (d / feed + t_acc) if d > feed * t_acc else 2 * math.sqrt(d / accel)
        prev = p
    per_point = r["settle_ms"] / 1000 + r["samples"] / max(rate_hz, 1) + 0.12
    t += len(pts) * per_point
    if r["drift_every"]:
        t += (len(pts) // r["drift_every"]) * (per_point + 4.0)
    return {"points": len(pts), "seconds": round(t), "per_point_s": round(per_point, 3)}


def plan(machine: Machine, r: dict) -> dict:
    """Points in work + machine coordinates, checked against the limits."""
    pts = build_points(r)
    wco = machine.g.wco
    z = r.get("z")
    zm = float(z) if z not in (None, "") else machine.g.mpos[2]
    errs = []
    th = math.radians(r.get("rotation_deg") or 0.0)
    c, s = math.cos(th), math.sin(th)
    for p in pts:
        # x, y are in the part's own frame; rotate about work zero to follow the part
        p["xm"] = p["x"] * c - p["y"] * s + wco[0]
        p["ym"] = p["x"] * s + p["y"] * c + wco[1]
        p["zm"] = zm
    if machine.homed:
        al = machine.allowed()
        for a, key in (("x", "xm"), ("y", "ym")):
            lo, hi = al[a]
            vals = [p[key] for p in pts]
            if min(vals) < lo - 1e-6 or max(vals) > hi + 1e-6:
                errs.append(f"{a.upper()} goes from {min(vals):.2f} to {max(vals):.2f} (machine); allowed {lo:.2f}..{hi:.2f}.")
        if zm < al["z"][0] - 1e-6 or zm > al["z"][1] + 1e-6:
            errs.append(f"Scan Z {zm:.3f} is outside {al['z'][0]:.2f}..{al['z'][1]:.2f} (check the Z floor).")
    return {"points": pts, "z_mach": zm, "errors": errs}


def measure_here(machine: Machine, laser, job: Job, samples: int, settle_ms: int, retries: int = 0,
                 max_invalid: float = 1.0, max_std_um: float = 1e9) -> tuple[dict, int, str]:
    st: dict = {}
    for attempt in range(retries + 1):
        machine.job_dwell(job, settle_ms / 1000.0)
        st = laser.measure(samples, after=time.monotonic())
        flag = ""
        if not st["valid"]:
            flag = "out-of-range"
        elif st["frac_invalid"] > max_invalid:
            flag = "dropouts"
        elif st["std_um"] is not None and st["std_um"] > max_std_um:
            flag = "noisy"
        if not flag:
            return st, attempt, ""
    return st, retries, flag


def point_row(machine: Machine, st: dict, extra: dict, t0: float) -> dict:
    mp = list(machine.g.mpos)
    w = machine.machine_to_work(mp)
    d = st.get("distance_mm")
    row = {
        "x_work": round(w[0], 4), "y_work": round(w[1], 4), "z_work": round(w[2], 4),
        "x_mach": round(mp[0], 4), "y_mach": round(mp[1], 4), "z_mach": round(mp[2], 4),
        "reading_mean": st.get("mean"), "reading_median": st.get("median"),
        "std_um": None if st.get("std_um") is None else round(st["std_um"], 3),
        "n_valid": st.get("n_valid"), "n": st.get("n"),
        "distance_mm": d,
        "height_mm": None if d is None else mp[2] - d,
        "t": round(time.monotonic() - t0, 3),
        "time": time.strftime("%H:%M:%S"),
    }
    row.update(extra)
    return row


def scan_body(machine: Machine, laser, bus, job: Job, recipe: dict, defaults: dict) -> str:
    """Run a scan inside an existing job. Returns the session id."""
    r = normalize(recipe, defaults)
    p = plan(machine, r)
    if p["errors"]:
        raise Refused("Scan outside the allowed travel: " + " ".join(p["errors"]))
    if not laser or not laser.running:
        raise Refused("The laser is not running. Connect it on the Laser tab.")
    pts = p["points"]
    n = len(pts)
    meta = {
        "sample_id": r.get("sample_id", ""),
        "notes": r.get("notes", ""),
        "n_points": n,
        "wco": list(machine.g.wco),
        "z_mach": p["z_mach"],
        "homed": machine.homed_how or "no",
        "firmware": machine.g.firmware,
        "grbl_settings": machine.g.settings,
        "laser": laser.snapshot(),
        "baseline": r.get("baseline"),
    }
    sess = Session.create(r["kind"], r["name"], r, meta)
    job.result = {"session": sess.id}
    job.detail.update({"session": sess.id, "points_total": n})
    t0 = time.monotonic()
    started = time.time()
    status = "failed"
    drift_k = 0
    first_valid_h = None
    try:
        machine.g.send("G21G90", timeout=5)
        feed = r["feed"]
        machine.job_move(job, z=p["z_mach"], feed=float(machine.cfg["machine"]["travel_feed_z"])) \
            if abs(machine.g.mpos[2] - p["z_mach"]) > 1e-3 else None
        drift_pt = pts[0]
        step_times = []
        for i, pt in enumerate(pts):
            ts = time.monotonic()
            job.checkpoint()
            if r["pattern"] == "raster" and pt["row_start"] and r["approach_mm"] > 0:
                machine.job_move(job, x=pt["xm"] - r["approach_mm"], y=pt["ym"], feed=feed, wait=False)
            machine.job_move(job, x=pt["xm"], y=pt["ym"], feed=feed)
            st, tries, flag = measure_here(machine, laser, job, r["samples"], r["settle_ms"], r["retries"],
                                           r["max_invalid_frac"], r["max_std_um"])
            row = point_row(machine, st, {"i": i, "region": pt["region"], "ix": pt["ix"], "iy": pt["iy"],
                                          "label": "", "retries": tries, "flag": flag,
                                          "u": round(pt["x"], 4), "v": round(pt["y"], 4)}, t0)
            sess.add_point(row)
            if i == 0 and row["height_mm"] is not None:
                sess.add_drift({"k": 0, "after_point": 0, **{k: row[k] for k in ("x_mach", "y_mach", "z_mach", "distance_mm", "height_mm", "std_um", "t", "time")}})
                drift_k = 1
            h = row["height_mm"]
            if h is not None and first_valid_h is None:
                first_valid_h = h
            step_times.append(time.monotonic() - ts)
            avg = sum(step_times[-50:]) / len(step_times[-50:])
            job.progress = {"done": i + 1, "total": n, "eta_s": round(avg * (n - i - 1)),
                            "elapsed_s": round(time.monotonic() - t0)}
            bus.publish("scan_point", {
                "session": sess.id, "i": i, "region": pt["region"], "x": row["x_work"], "y": row["y_work"],
                "xm": row["x_mach"], "ym": row["y_mach"],
                "h_um": None if h is None or first_valid_h is None else round((h - first_valid_h) * 1000, 3),
                "std_um": row["std_um"], "flag": flag, "done": i + 1, "total": n,
            })
            if r["drift_every"] and (i + 1) % r["drift_every"] == 0 and i + 1 < n:
                drift_k = _drift(machine, laser, job, sess, r, drift_pt, drift_k, i, t0, feed)
        if r["drift_every"] and n > 1:
            drift_k = _drift(machine, laser, job, sess, r, drift_pt, drift_k, n - 1, t0, feed)
        status = "done"
    except Exception as e:
        status = "stopped" if job.abort.is_set() else "failed"
        sess.update_meta(error=str(e) or type(e).__name__)
        raise
    finally:
        done = len(sess.points())
        summary = None
        try:
            res = analyze_session(sess, drift_correct=True)
            o = res.get("overall", {})
            if o.get("ok"):
                summary = {k: o.get(k) for k in ("bow_um", "warpage_pv_um", "tilt_mrad", "rms_um", "shape")}
        except Exception as e:  # analysis must never lose the data
            sess.update_meta(analysis_error=str(e))
        sess.update_meta(status=status, n_done=done, duration_s=round(time.time() - started),
                         ended=time.strftime("%Y-%m-%d %H:%M:%S"), summary=summary)
        bus.publish("sessions_changed", {"id": sess.id})
    return sess.id


def _drift(machine, laser, job, sess, r, dp, k, after_i, t0, feed) -> int:
    job.detail["phase"] = "drift check"
    machine.job_move(job, x=dp["xm"], y=dp["ym"], feed=feed)
    st, _, flag = measure_here(machine, laser, job, r["samples"], r["settle_ms"], r["retries"],
                               r["max_invalid_frac"], r["max_std_um"])
    mp = machine.g.mpos
    d = st.get("distance_mm")
    sess.add_drift({"k": k, "after_point": after_i, "x_mach": mp[0], "y_mach": mp[1], "z_mach": mp[2],
                    "distance_mm": d, "height_mm": None if d is None else mp[2] - d,
                    "std_um": st.get("std_um"), "t": round(time.monotonic() - t0, 3), "time": time.strftime("%H:%M:%S")})
    job.detail["phase"] = ""
    return k + 1


def analyze_session(sess: Session, baseline_id: str | None = "meta", drift_correct: bool = True) -> dict:
    meta = sess.meta()
    if baseline_id == "meta":
        baseline_id = meta.get("baseline")
    base_pts = None
    if baseline_id:
        try:
            base_pts = Session.open(baseline_id).points()
        except FileNotFoundError:
            base_pts = None
    res = analysis.analyze(sess.points(), sess.drift(), base_pts, drift_correct=drift_correct)
    res["baseline_id"] = baseline_id if base_pts else None
    res["session"] = sess.id
    sess.save_results(res)
    return res


def start_scan(machine: Machine, laser, bus, recipe: dict) -> Job:
    defaults = machine.cfg["scan"]
    r = normalize(recipe, defaults)
    machine.check_target(list(machine.g.mpos), "A scan")
    p = plan(machine, r)
    if p["errors"]:
        raise Refused("Scan outside the allowed travel: " + " ".join(p["errors"]))
    if not laser or not laser.running:
        raise Refused("The laser is not running. Connect it on the Laser tab.")
    return machine.start_job("scan", r["name"], lambda job: scan_body(machine, laser, bus, job, r, defaults))


def start_standoff(machine: Machine, laser, bus) -> Job:
    """Move Z until the laser reads mid-range (fixed XY)."""
    if not laser or not laser.running:
        raise Refused("Connect the laser first.")
    lr = laser.latest()
    if not lr or not lr.get("valid"):
        raise Refused("Laser is out of range. Jog Z until a reading appears, then try again.")
    machine.check_target(list(machine.g.mpos), "Auto standoff")
    c = machine.cfg["laser"]
    target = float(c["smr_mm"]) + float(c["range_mm"]) / 2

    def run(job: Job):
        machine.g.send("G21G90", timeout=5)
        for it in range(8):
            st = laser.measure(100, after=time.monotonic() + 0.2)
            if not st["valid"]:
                raise Refused("Lost the laser reading while adjusting. Jog back into range.")
            err = st["distance_mm"] - target
            job.message = f"Distance {st['distance_mm']:.3f} mm (target {target:.2f}), step {it + 1}"
            if abs(err) < 0.02:
                break
            step = max(-5.0, min(5.0, err))
            z_new = machine.g.mpos[2] - step
            machine.job_move(job, z=z_new, feed=float(machine.cfg["machine"]["travel_feed_z"]) / 2)
            machine.job_dwell(job, 0.3)
        bus.note("ok", f"Standoff set: laser at {target:.2f} mm (mid-range). Z machine {machine.g.mpos[2]:.3f}.")

    return machine.start_job("standoff", "Auto standoff", run)


def start_measure(machine: Machine, laser, bus, samples: int, label: str = "") -> Job:
    if not laser or not laser.running:
        raise Refused("Connect the laser first.")

    def run(job: Job):
        st, _, flag = measure_here(machine, laser, job, samples, 200)
        row = point_row(machine, st, {"label": label, "flag": flag}, time.monotonic())
        job.result = {"measurement": row, "stats": {k: st.get(k) for k in ("valid", "mean", "std_um", "n_valid", "n", "distance_mm")}}

    if machine.job and machine.job.status in ("running", "paused"):
        raise Refused("Busy: stop or finish the running job first.")
    return machine.start_job("measure", f"Measure {samples}", run)
