"""Program library: G-code files, JSON step programs and CSV point lists.

  *.nc / *.gcode / *.ngc / *.tap / *.txt   G-code, streamed to GRBL
  *.json   {"format": "bowmapper-program/1", "steps": [...]}  (see README)
  *.csv    x,y[,z][,label][,samples] rows -> move + measure at each point

Every program is checked before it runs (and again at run time, because the
work zero may have changed since it was imported).
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from pathlib import Path

from .config import DIRS
from .gcode import GcodeInterpreter, analyze_gcode, clean_line
from .machine import AXES, Job, Machine, Refused
from .scan import build_points, estimate, measure_here, normalize, point_row, scan_body
from .sessions import Session

GCODE_EXT = {".nc", ".gcode", ".ngc", ".tap", ".txt", ".gc", ".cnc", ".nc1"}
ALL_EXT = GCODE_EXT | {".json", ".csv"}
OPS = {"home", "move", "move_rel", "measure", "dwell", "pause", "goto_position", "set_zero", "gcode",
       "scan", "repeat", "note"}
MAX_STEPS = 100000


def safe_name(name: str) -> str:
    base = Path(name).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(base).stem).strip() or "program"
    ext = Path(base).suffix.lower()
    if ext not in ALL_EXT:
        raise Refused(f"Unsupported file type '{ext}'. Use .nc/.gcode/.ngc/.tap/.txt, .json or .csv.")
    return stem + ext


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    return "measure" if ext in (".json", ".csv") else "gcode"


def library() -> list[dict]:
    out = []
    for p in sorted(DIRS["programs"].iterdir(), key=lambda p: p.name.lower()):
        if p.is_file() and p.suffix.lower() in ALL_EXT:
            st = p.stat()
            out.append({"name": p.name, "kind": kind_of(p.name), "size": st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def save(name: str, text: str) -> str:
    n = safe_name(name)
    if len(text) > 50_000_000:
        raise Refused("File too large (50 MB max).")
    if kind_of(n) == "measure":
        parse_measure(n, text)   # reject broken JSON/CSV now, not at run time
    (DIRS["programs"] / n).write_text(text, encoding="utf-8")
    return n


def read(name: str) -> str:
    p = DIRS["programs"] / Path(name).name
    if not p.exists():
        raise Refused(f"No program named {name}")
    return p.read_text(encoding="utf-8", errors="replace")


def delete(name: str) -> None:
    p = DIRS["programs"] / Path(name).name
    if p.exists():
        trash = DIRS["programs"] / "_deleted"
        trash.mkdir(exist_ok=True)
        p.replace(trash / p.name)


# ---------------------------------------------------------------- measure programs
def parse_measure(name: str, text: str) -> dict:
    if name.lower().endswith(".csv"):
        return csv_program(name, text)
    try:
        prog = json.loads(text)
    except json.JSONDecodeError as e:
        raise Refused(f"JSON error line {e.lineno} col {e.colno}: {e.msg}") from e
    if not isinstance(prog, dict) or not isinstance(prog.get("steps"), list):
        raise Refused('A JSON program needs {"steps": [...]} at the top level.')
    prog.setdefault("name", Path(name).stem)
    prog.setdefault("coords", "work")
    if prog["coords"] not in ("work", "machine"):
        raise Refused('"coords" must be "work" or "machine".')
    _check_steps(prog["steps"], "steps")
    return prog


def _check_steps(steps, where):
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise Refused(f"{where}[{i}] must be an object.")
        op = s.get("do") or s.get("op")
        if op not in OPS:
            raise Refused(f"{where}[{i}]: unknown step '{op}'. Known: {', '.join(sorted(OPS))}.")
        s["do"] = op
        if op == "repeat":
            if not isinstance(s.get("steps"), list):
                raise Refused(f"{where}[{i}]: repeat needs a steps list.")
            _check_steps(s["steps"], f"{where}[{i}].steps")


def csv_program(name: str, text: str) -> dict:
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].strip().startswith("#")]
    if not rows:
        raise Refused("CSV is empty.")
    head = [c.strip().lower() for c in rows[0]]
    if "x" in head and "y" in head:
        body = rows[1:]
    else:
        head = ["x", "y", "z", "label", "samples"][: len(rows[0])]
        body = rows
    idx = {k: head.index(k) for k in ("x", "y", "z", "label", "samples") if k in head}
    steps = []
    for n, r in enumerate(body, start=2 if body is not rows else 1):
        try:
            x = float(r[idx["x"]])
            y = float(r[idx["y"]])
        except (ValueError, IndexError) as e:
            raise Refused(f"CSV line {n}: x and y must be numbers ({r}).") from e
        mv = {"do": "move", "x": x, "y": y}
        if "z" in idx and idx["z"] < len(r) and r[idx["z"]].strip():
            mv["z"] = float(r[idx["z"]])
        steps.append(mv)
        ms = {"do": "measure", "label": r[idx["label"]].strip() if "label" in idx and idx["label"] < len(r) else f"P{len(steps)}"}
        if "samples" in idx and idx["samples"] < len(r) and r[idx["samples"]].strip():
            ms["samples"] = int(float(r[idx["samples"]]))
        steps.append(ms)
    return {"format": "bowmapper-program/1", "name": Path(name).stem, "coords": "work", "steps": steps}


def flatten(steps: list[dict], depth: int = 0) -> list[dict]:
    out = []
    for s in steps:
        if s["do"] == "repeat":
            times = int(s.get("times", 1))
            if times < 0 or times > 10000:
                raise Refused("repeat times must be 0..10000")
            inner = flatten(s["steps"], depth + 1)
            for k in range(times):
                out.extend(inner)
                if len(out) > MAX_STEPS:
                    raise Refused(f"Program expands to more than {MAX_STEPS} steps.")
        else:
            out.append(s)
    return out


def analyze_measure(machine: Machine, prog: dict) -> dict:
    """Dry-walk the steps: targets, limits, preview, time estimate."""
    cfg = machine.cfg
    errors: list[dict] = []
    warnings: list[dict] = []
    preview: list[list] = []
    points: list[list] = []
    defaults = prog.get("defaults", {})
    samples_def = int(defaults.get("samples", cfg["scan"]["samples"]))
    settle_def = int(defaults.get("settle_ms", cfg["scan"]["settle_ms"]))
    feed_def = float(defaults.get("feed", cfg["machine"]["travel_feed_xy"]))
    try:
        steps = flatten(prog["steps"])
    except Refused as e:
        return {"kind": "measure", "ok": False, "errors": [{"line": 0, "text": str(e)}], "warnings": [], "preview": []}
    pos = list(machine.g.mpos)
    wco = list(machine.g.wco)
    known = machine.homed
    al = machine.allowed()
    est = 0.0
    rate = 1000.0
    n_meas = 0
    homed = machine.homed
    positions = {p["name"]: p for p in cfg["ui"].get("positions", [])}

    def check(tgt, i):
        if not homed:
            return
        for k, a in enumerate(AXES):
            lo, hi = al[a]
            if tgt[k] < lo - 1e-6 or tgt[k] > hi + 1e-6:
                errors.append({"line": i + 1, "text": f"step {i + 1}: {a.upper()} {tgt[k]:.3f} outside {lo:.2f}..{hi:.2f} (machine)"})
                return

    def go(tgt, i, feed):
        nonlocal pos, est
        check(tgt, i)
        d = math.dist(pos, tgt)
        est += d / max(feed, 1) * 60 + 0.2
        preview.append([round(pos[0], 3), round(pos[1], 3), round(tgt[0], 3), round(tgt[1], 3), 0])
        pos = tgt

    for i, s in enumerate(steps):
        op = s["do"]
        if op == "home":
            if i > 0:
                warnings.append({"line": i + 1, "text": f"step {i + 1}: homing mid-program"})
            homed = True
            lim = cfg["machine"]["limits"]
            pos = [lim["x"][0] + 2, lim["y"][0] + 2, lim["z"][1] - 2]
            est += 25
        elif op == "move":
            tgt = list(pos)
            in_work = s.get("coords", prog["coords"]) == "work"
            for k, a in enumerate(AXES):
                v = s.get(a)
                if v is not None:
                    tgt[k] = float(v) + (wco[k] if in_work else 0)
            go(tgt, i, float(s.get("feed", feed_def)))
        elif op == "move_rel":
            tgt = [pos[k] + float(s.get("d" + a, 0) or 0) for k, a in enumerate(AXES)]
            go(tgt, i, float(s.get("feed", feed_def)))
        elif op == "goto_position":
            p = positions.get(s.get("name"))
            if not p:
                errors.append({"line": i + 1, "text": f"step {i + 1}: no saved position named '{s.get('name')}'"})
                continue
            go([float(p["x"]), float(p["y"]), float(p["z"])], i, feed_def)
        elif op == "measure":
            n_meas += 1
            sm = int(s.get("samples", samples_def))
            est += int(s.get("settle_ms", settle_def)) / 1000 + sm / rate + 0.1
            points.append([round(pos[0], 3), round(pos[1], 3), s.get("label", f"P{n_meas}")])
        elif op == "dwell":
            est += float(s.get("seconds", 0))
        elif op == "pause":
            warnings.append({"line": i + 1, "text": f"step {i + 1}: pauses for the operator: {s.get('message', '')}"})
        elif op == "set_zero":
            for k, a in enumerate(AXES):
                if a.upper() in str(s.get("axes", "XY")).upper():
                    wco[k] = pos[k]
        elif op == "gcode":
            lines = s.get("lines") or ([s["line"]] if s.get("line") else [])
            it = GcodeInterpreter(wco=wco, start=pos, block_spindle=cfg["machine"].get("block_spindle", True))
            for ln in lines:
                it.feed_line(clean_line(ln), i + 1)
            for e in it.errors:
                errors.append({"line": i + 1, "text": f"step {i + 1}: {e['text']}"})
            for sg in it.segments:
                check([sg[3], sg[4], sg[5]], i)
                preview.append([round(sg[0], 3), round(sg[1], 3), round(sg[3], 3), round(sg[4], 3), sg[6]])
            pos = list(it.pos)
            wco = list(it.wco)
            est += it.time_s
        elif op == "scan":
            try:
                r = normalize(s.get("recipe") or {}, cfg["scan"])
                pts = build_points(r)
                e = estimate(r)
                est += e["seconds"]
                for p in pts:
                    tgt = [p["x"] + wco[0], p["y"] + wco[1], pos[2] if r.get("z") in (None, "") else float(r["z"])]
                    check(tgt, i)
                    points.append([round(tgt[0], 3), round(tgt[1], 3), ""])
                pos = [pts[-1]["x"] + wco[0], pts[-1]["y"] + wco[1], pos[2]]
                n_meas += len(pts)
            except Refused as ex:
                errors.append({"line": i + 1, "text": f"step {i + 1}: {ex}"})
        elif op == "note":
            pass
    if not homed:
        warnings.insert(0, {"line": 0, "text": "Machine not homed: limits could not be checked."})
    _ = known
    return {"kind": "measure", "ok": not errors, "errors": errors[:200], "warnings": warnings[:200],
            "steps": len(steps), "measures": n_meas, "est_s": round(est), "preview": preview[:60000],
            "points": points[:60000]}


def analyze(machine: Machine, name: str) -> dict:
    text = read(name)
    if kind_of(name) == "gcode":
        res = analyze_gcode(text, list(machine.g.wco), list(machine.g.mpos), machine.allowed(), machine.homed,
                            rapid_rate=machine.g.setting(110, 4000.0),
                            block_spindle=machine.cfg["machine"].get("block_spindle", True))
        res.pop("lines", None)
        res.pop("source_lines", None)
    else:
        try:
            prog = parse_measure(name, text)
        except Refused as e:
            return {"kind": "measure", "ok": False, "errors": [{"line": 0, "text": str(e)}], "warnings": [], "preview": []}
        res = analyze_measure(machine, prog)
        res["description"] = prog.get("description", "")
    res["name"] = name
    return res


# ---------------------------------------------------------------- running
def run(machine: Machine, laser, bus, name: str, mode: str = "run") -> Job:
    text = read(name)
    if kind_of(name) == "gcode":
        res = analyze_gcode(text, list(machine.g.wco), list(machine.g.mpos), machine.allowed(), machine.homed,
                            rapid_rate=machine.g.setting(110, 4000.0),
                            block_spindle=machine.cfg["machine"].get("block_spindle", True))
        if not res["ok"]:
            first = res["errors"][0]
            raise Refused(f"Program has {len(res['errors'])} problem(s). First: line {first['line']}: {first['text']}")
        if mode != "check":
            machine.check_target(list(machine.g.mpos), "Running a program")
        lines = res["lines"]
        if mode == "check":
            return machine.start_job("check", f"Check {name}", lambda job: _check_mode(machine, job, lines))
        return machine.start_job("gcode", name, lambda job: _run_gcode(machine, job, lines))
    prog = parse_measure(name, text)
    res = analyze_measure(machine, prog)
    if not res["ok"]:
        first = res["errors"][0]
        raise Refused(f"Program has {len(res['errors'])} problem(s). First: {first['text']}")
    if res.get("measures") and (not laser or not laser.running):
        raise Refused("This program measures, but the laser is not running.")
    return machine.start_job("program", prog.get("name", name), lambda job: _run_measure(machine, laser, bus, job, prog, name))


def _run_gcode(machine: Machine, job: Job, lines: list[str]) -> None:
    machine.g.send("G21", timeout=5)
    machine.stream_gcode(job, lines)
    machine.job_wait(job)


def _check_mode(machine: Machine, job: Job, lines: list[str]) -> None:
    if machine.g.state != "Idle":
        raise Refused("Check mode needs the machine Idle.")
    r = machine.g.send("$C", timeout=5)
    if not r.ok:
        raise Refused(f"$C failed: {r.message}")
    try:
        machine.stream_gcode(job, lines, stop_on_error=False)
        machine.g.wait_drained(timeout=30)
    finally:
        machine._expect_reset = True
        machine.g.send("$C", timeout=5)     # leaving check mode soft-resets GRBL
        time.sleep(0.5)
    machine.bus.note("ok", f"Check mode finished: {len(lines)} lines parsed by GRBL.")


def _run_measure(machine: Machine, laser, bus, job: Job, prog: dict, fname: str) -> None:
    cfg = machine.cfg
    defaults = prog.get("defaults", {})
    samples_def = int(defaults.get("samples", cfg["scan"]["samples"]))
    settle_def = int(defaults.get("settle_ms", cfg["scan"]["settle_ms"]))
    feed_def = float(defaults.get("feed", cfg["machine"]["travel_feed_xy"]))
    steps = flatten(prog["steps"])
    positions = {p["name"]: p for p in cfg["ui"].get("positions", [])}
    sess = None
    t0 = time.monotonic()
    n_meas = 0
    job.progress = {"done": 0, "total": len(steps)}
    machine.g.send("G21G90", timeout=5)
    status = "failed"
    try:
        for i, s in enumerate(steps):
            job.checkpoint()
            op = s["do"]
            job.detail["step"] = f"{i + 1}: {op}"
            if op == "home":
                machine.home_blocking(job)
            elif op in ("move", "move_rel", "goto_position"):
                if op == "move":
                    coords = s.get("coords", prog.get("coords", "work"))
                    w = [s.get(a) for a in AXES]
                    w = [None if v is None else float(v) for v in w]
                    tgt = machine.work_to_machine(w) if coords == "work" else [machine.g.mpos[k] if w[k] is None else w[k] for k in range(3)]
                elif op == "move_rel":
                    tgt = [machine.g.mpos[k] + float(s.get("d" + a, 0) or 0) for k, a in enumerate(AXES)]
                else:
                    p = positions.get(s.get("name"))
                    if not p:
                        raise Refused(f"No saved position named '{s.get('name')}'")
                    tgt = [float(p["x"]), float(p["y"]), float(p["z"])]
                feed = float(s.get("feed", feed_def))
                if s.get("safe_z") and cfg["machine"].get("safe_z_mpos") is not None:
                    sz = float(cfg["machine"]["safe_z_mpos"])
                    if machine.g.mpos[2] < sz:
                        machine.job_move(job, z=sz, feed=float(cfg["machine"]["travel_feed_z"]))
                machine.job_move(job, x=tgt[0], y=tgt[1], feed=feed)
                if abs(tgt[2] - machine.g.mpos[2]) > 1e-4:
                    machine.job_move(job, z=tgt[2], feed=float(cfg["machine"]["travel_feed_z"]))
            elif op == "measure":
                if sess is None:
                    sess = Session.create("points", prog.get("name", fname), prog,
                                          {"program": fname, "wco": list(machine.g.wco), "homed": machine.homed_how or "no",
                                           "laser": laser.snapshot(), "n_points": None})
                    job.result = {"session": sess.id}
                st, tries, flag = measure_here(machine, laser, job, int(s.get("samples", samples_def)),
                                               int(s.get("settle_ms", settle_def)), int(s.get("retries", 1)),
                                               cfg["scan"]["max_invalid_frac"], cfg["scan"]["max_std_um"])
                row = point_row(machine, st, {"i": n_meas, "label": s.get("label", f"P{n_meas + 1}"), "region": s.get("region", "Points"),
                                              "retries": tries, "flag": flag}, t0)
                sess.add_point(row)
                n_meas += 1
                bus.publish("scan_point", {"session": sess.id, "i": n_meas - 1, "x": row["x_work"], "y": row["y_work"],
                                           "xm": row["x_mach"], "ym": row["y_mach"], "label": row["label"],
                                           "h_um": None, "distance_mm": row["distance_mm"], "std_um": row["std_um"], "flag": flag})
            elif op == "dwell":
                machine.job_dwell(job, float(s.get("seconds", 0)))
            elif op == "pause":
                machine.prompt_pause(job, s.get("message", "Press Resume to continue."))
            elif op == "set_zero":
                axes = "".join(a for a in str(s.get("axes", "XY")).upper() if a in "XYZ")
                machine.job_wait(job)
                r = machine.g.send("G10L20P1" + "".join(f"{a}0" for a in axes), timeout=5)
                if not r.ok:
                    raise Refused(r.message)
                machine.g.read_offsets()
            elif op == "gcode":
                lines = [clean_line(x) for x in (s.get("lines") or ([s["line"]] if s.get("line") else []))]
                machine.stream_gcode(job, [x for x in lines if x])
                machine.job_wait(job)
            elif op == "scan":
                sid = scan_body(machine, laser, bus, job, s.get("recipe") or {}, cfg["scan"])
                job.result.setdefault("scans", []).append(sid)
            elif op == "note":
                bus.note("info", str(s.get("text", "")))
            job.progress = {"done": i + 1, "total": len(steps), "elapsed_s": round(time.monotonic() - t0)}
        status = "done"
    finally:
        if sess:
            pts = sess.points()
            summary = None
            if len(pts) >= 3:
                from .scan import analyze_session
                try:
                    o = analyze_session(sess).get("overall", {})
                    if o.get("ok"):
                        summary = {k: o.get(k) for k in ("bow_um", "warpage_pv_um", "tilt_mrad", "rms_um", "shape")}
                except Exception:
                    pass
            sess.update_meta(status=status if not job.abort.is_set() else "stopped", n_done=len(pts),
                             n_points=len(pts), summary=summary, ended=time.strftime("%Y-%m-%d %H:%M:%S"))
            bus.publish("sessions_changed", {"id": sess.id})
