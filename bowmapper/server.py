"""FastAPI app: REST for commands, one WebSocket for live state + jogging."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, locate, programs, scan
from .config import DIRS, ROOT, Config
from .events import EventBus
from .laser import MedaqLaser, NoLaser, SerialLaser, SimLaser
from .machine import Machine, Refused
from .sessions import Session, delete_session, list_sessions, slug

STATIC = Path(__file__).resolve().parent / "static"


class AppState:
    def __init__(self):
        self.cfg = Config()
        self.bus = EventBus()
        self.machine = Machine(self.cfg, self.bus)
        self.laser = NoLaser(self.cfg, self.bus)
        self.machine.laser = None
        _install_factory_baseline()

    def laser_position(self):
        m = self.machine
        if m.sim is not None and (not m.g.connected or getattr(m.g.t, "kind", "") == "sim"):
            return list(m.sim.phys)
        if m.g.connected:
            return list(m.g.mpos)
        return None

    def set_laser(self, driver: str) -> None:
        try:
            self.laser.stop()
        except Exception:
            pass
        if driver == "sim":
            self.laser = SimLaser(self.cfg, self.bus, self.laser_position)
        elif driver == "medaq":
            backend = self.cfg["laser"].get("backend", "auto")
            if backend == "serial" or (backend == "auto" and os.name != "nt"):
                self.laser = SerialLaser(self.cfg, self.bus)      # no MEDAQLib (Linux / Pi)
            else:
                self.laser = MedaqLaser(self.cfg, self.bus)
        else:
            self.laser = NoLaser(self.cfg, self.bus)
        self.cfg.update({"laser": {"driver": driver}})
        self.machine.laser = self.laser if driver != "none" else None
        self.laser.start()


def _install_factory_baseline():
    """Copy the 2026-11-19 factory $$ capture next to the backups, once."""
    dst = DIRS["backups"] / "factory_2026-11-19.txt"
    src = ROOT.parent / "grbl_baseline_2026-11-19.txt"
    if not dst.exists() and src.exists():
        dst.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


S = AppState()
app = FastAPI(title="Bow Mapper", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(Refused)
async def _refused(request: Request, exc: Refused):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.exception_handler(FileNotFoundError)
async def _nf(request: Request, exc: FileNotFoundError):
    return JSONResponse({"ok": False, "error": f"Not found: {exc}"}, status_code=404)


@app.exception_handler(Exception)
async def _any(request: Request, exc: Exception):
    return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def ok(**kw):
    return {"ok": True, **kw}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- status/config
@app.get("/api/state")
def state():
    return {"machine": S.machine.snapshot(), "laser": S.laser.snapshot()}


@app.get("/api/config")
def get_config():
    return S.cfg.snapshot()


@app.post("/api/config")
async def set_config(request: Request):
    patch = await request.json()
    allowed = {"machine", "jog", "laser", "scan", "ui"}
    patch = {k: v for k, v in patch.items() if k in allowed}
    m = patch.get("machine", {})
    if "limits" in m:
        m["limits_source"] = "manual"
    cfg = S.cfg.update(patch)
    S.bus.publish("config", {"config": cfg})
    return ok(config=cfg)


@app.get("/api/ports")
def ports():
    return {"ports": S.machine.ports()}


# ---------------------------------------------------------------- connection
@app.post("/api/connect")
async def connect(request: Request):
    b = await request.json()
    snap = await asyncio.to_thread(S.machine.connect, b.get("transport"), b.get("port"), b.get("baud"),
                                   b.get("host"), b.get("tcp_port"))
    if b.get("transport") == "sim" and S.cfg["laser"]["driver"] == "sim":
        pass
    return ok(machine=snap)


@app.post("/api/disconnect")
def disconnect():
    S.machine.disconnect()
    return ok()


# ---------------------------------------------------------------- machine commands
@app.post("/api/home")
def home():
    S.machine.home()
    return ok()


@app.post("/api/unlock")
def unlock():
    S.machine.unlock()
    return ok()


@app.post("/api/trust")
def trust():
    S.machine.trust_position()
    return ok()


@app.post("/api/stop")
def stop():
    S.machine.stop()
    return ok()


@app.post("/api/estop")
def estop():
    S.machine.estop()
    return ok()


@app.post("/api/hold")
def hold():
    S.machine.hold()
    return ok()


@app.post("/api/resume")
def resume():
    S.machine.resume()
    return ok()


@app.post("/api/override")
async def override(request: Request):
    b = await request.json()
    S.machine.override(b.get("kind", "feed"), str(b.get("action", "reset")))
    return ok()


@app.post("/api/goto")
async def goto(request: Request):
    b = await request.json()
    f = lambda k: None if b.get(k) in (None, "") else float(b[k])
    S.machine.goto(f("x"), f("y"), f("z"), b.get("coords", "work"), f("feed"), bool(b.get("safe_z", True)))
    return ok()


@app.post("/api/jog")
async def jog(request: Request):
    """REST fallback for a single step jog (the UI uses the WebSocket)."""
    b = await request.json()
    res = await asyncio.to_thread(S.machine.jog_step, b.get("dx", 0), b.get("dy", 0), b.get("dz", 0), b.get("speed", "normal"))
    return ok(**res)


@app.post("/api/zero")
async def zero(request: Request):
    b = await request.json()
    S.machine.set_work_zero(b.get("axes", "XY"))
    return ok()


@app.post("/api/workcoords")
async def workcoords(request: Request):
    b = await request.json()
    S.machine.set_work_coords({k: b.get(k) for k in ("x", "y", "z") if b.get(k) not in (None, "")})
    return ok()


@app.post("/api/zfloor")
async def zfloor(request: Request):
    b = await request.json()
    act = b.get("action")
    if act == "clear":
        S.machine.set_z_floor(None)
    elif act == "here":
        S.machine.set_z_floor(S.machine.g.mpos[2] - float(b.get("below_mm", 0) or 0))
    else:
        S.machine.set_z_floor(float(b["value"]))
    return ok()


@app.post("/api/safez")
async def safez(request: Request):
    b = await request.json()
    if b.get("action") == "here":
        S.machine.set_safe_z(S.machine.g.mpos[2])
    else:
        S.machine.set_safe_z(float(b["value"]))
    return ok()


@app.post("/api/console")
async def console(request: Request):
    b = await request.json()
    res = await asyncio.to_thread(S.machine.console, str(b.get("line", "")))
    return ok(**res)


# ---------------------------------------------------------------- positions
@app.post("/api/positions")
async def positions(request: Request):
    b = await request.json()
    act = b.get("action")
    pos = list(S.cfg["ui"].get("positions", []))
    name = str(b.get("name", "")).strip()
    if act == "save":
        if not name:
            raise Refused("Give the position a name.")
        if not S.machine.homed:
            raise Refused("Home first: saved positions are stored in machine coordinates.")
        mp = S.machine.g.mpos
        pos = [p for p in pos if p["name"] != name] + [{"name": name, "x": round(mp[0], 4), "y": round(mp[1], 4), "z": round(mp[2], 4)}]
    elif act == "delete":
        pos = [p for p in pos if p["name"] != name]
    elif act == "goto":
        p = next((p for p in pos if p["name"] == name), None)
        if not p:
            raise Refused(f"No position named {name}")
        S.machine.goto(p["x"], p["y"], p["z"], "machine", None, True)
        return ok()
    S.cfg.update({"ui": {"positions": pos}})
    return ok(positions=pos)


# ---------------------------------------------------------------- jobs
@app.post("/api/job/pause")
def job_pause():
    S.machine.pause_job()
    return ok()


@app.post("/api/job/resume")
def job_resume():
    S.machine.resume_job()
    return ok()


@app.post("/api/job/stop")
def job_stop():
    S.machine.stop()
    return ok()


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def settings():
    if not S.machine.g.connected:
        return {"rows": [], "backups": _backups()}
    return {"rows": S.machine.settings_table(), "backups": _backups(), "firmware": S.machine.g.firmware}


def _backups():
    return sorted([p.name for p in DIRS["backups"].glob("*.txt")], reverse=True)


@app.post("/api/settings")
async def write_setting(request: Request):
    b = await request.json()
    rows = await asyncio.to_thread(S.machine.write_setting, int(b["key"]), str(b["value"]))
    return ok(rows=rows)


@app.post("/api/settings/backup")
def backup():
    return ok(file=S.machine.backup_settings(), backups=_backups())


@app.post("/api/settings/restore")
async def restore(request: Request):
    b = await request.json()
    changed = await asyncio.to_thread(S.machine.restore_settings, b["file"])
    return ok(changed=changed)


# ---------------------------------------------------------------- laser
@app.post("/api/laser/connect")
async def laser_connect(request: Request):
    b = await request.json()
    patch = {k: b[k] for k in ("port", "interface", "dll_path", "baud") if k in b and b[k] not in (None,)}
    if patch:
        S.cfg.update({"laser": patch})
    await asyncio.to_thread(S.set_laser, b.get("driver", "medaq"))
    return ok(laser=S.laser.snapshot())


@app.post("/api/laser/disconnect")
async def laser_disconnect():
    await asyncio.to_thread(S.set_laser, "none")
    return ok()


@app.post("/api/laser/measure")
async def laser_measure(request: Request):
    b = await request.json()
    n = int(b.get("samples", 100))
    if not S.laser.running:
        raise Refused("Connect the laser first.")
    st = await asyncio.to_thread(S.laser.measure, n, time.monotonic())
    row = scan.point_row(S.machine, st, {"label": b.get("label", "")}, time.monotonic())
    row["connected"] = S.machine.g.connected
    return ok(stats=st, row=row)


@app.post("/api/laser/standoff")
def laser_standoff():
    scan.start_standoff(S.machine, S.laser, S.bus)
    return ok()


# ---------------------------------------------------------------- scans
@app.post("/api/scan/estimate")
async def scan_estimate(request: Request):
    b = await request.json()
    r = scan.normalize(b, S.cfg["scan"])
    est = scan.estimate(r, rate_hz=S.laser.rate_hz or 1000.0)
    p = scan.plan(S.machine, r)
    import math as _m
    th = _m.radians(r.get("rotation_deg") or 0.0)
    wco = S.machine.g.wco
    outlines = []
    for g in r["regions"]:
        corners = [(g["x0"], g["y0"]), (g["x1"], g["y0"]), (g["x1"], g["y1"]), (g["x0"], g["y1"])]
        outlines.append({"name": g["name"], "pts": [[round(x * _m.cos(th) - y * _m.sin(th) + wco[0], 4),
                                                      round(x * _m.sin(th) + y * _m.cos(th) + wco[1], 4)] for x, y in corners]})
    return ok(estimate=est, errors=p["errors"], z_mach=p["z_mach"], outlines=outlines,
              points=[[round(q["x"], 4), round(q["y"], 4), round(q["xm"], 4), round(q["ym"], 4)] for q in p["points"][:40000]])


@app.post("/api/scan/start")
async def scan_start(request: Request):
    b = await request.json()
    job = scan.start_scan(S.machine, S.laser, S.bus, b)
    return ok(job=job.snapshot())


@app.get("/api/locate/templates")
def locate_templates():
    return {"templates": locate.TEMPLATES, "align": S.cfg.snapshot().get("align")}


@app.post("/api/locate/silicon")
async def locate_silicon(request: Request):
    b = await request.json()
    job = locate.start_find_silicon(S.machine, S.laser, S.bus, b)
    return ok(job=job.snapshot())


@app.post("/api/locate/manual")
async def locate_manual(request: Request):
    b = await request.json()
    fit = await asyncio.to_thread(locate.manual_corners, S.machine, S.bus, b.get("corners") or [],
                                  {k: b.get(k) for k in ("width", "height")})
    return ok(fit=fit)


@app.post("/api/locate/refine")
async def locate_refine(request: Request):
    b = await request.json()
    job = locate.start_refine(S.machine, S.laser, S.bus, b)
    return ok(job=job.snapshot())


@app.post("/api/locate/trace")
async def locate_trace(request: Request):
    b = await request.json()
    job = locate.start_trace(S.machine, S.bus, b)
    return ok(job=job.snapshot())


@app.post("/api/locate/clear")
def locate_clear():
    S.cfg.update({"align": None})
    S.bus.publish("config", {"config": S.cfg.snapshot()})
    return ok()


@app.get("/api/recipes")
def recipes():
    return {"recipes": sorted(p.stem for p in DIRS["recipes"].glob("*.json"))}


@app.get("/api/recipes/{name}")
def recipe_get(name: str):
    p = DIRS["recipes"] / f"{slug(name)}.json"
    if not p.exists():
        raise FileNotFoundError(name)
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/api/recipes")
async def recipe_save(request: Request):
    b = await request.json()
    name = slug(b.get("name") or "recipe")
    (DIRS["recipes"] / f"{name}.json").write_text(json.dumps(b, indent=2), encoding="utf-8")
    return ok(name=name)


@app.delete("/api/recipes/{name}")
def recipe_delete(name: str):
    p = DIRS["recipes"] / f"{slug(name)}.json"
    if p.exists():
        p.unlink()
    return ok()


# ---------------------------------------------------------------- programs
@app.get("/api/programs")
def program_list():
    return {"programs": programs.library()}


@app.post("/api/programs/import")
async def program_import(request: Request):
    b = await request.json()
    name = programs.save(b["filename"], b["text"])
    return ok(name=name, analysis=await asyncio.to_thread(programs.analyze, S.machine, name))


@app.get("/api/programs/{name}")
def program_get(name: str):
    return {"name": name, "text": programs.read(name)[:200000], "analysis": programs.analyze(S.machine, name)}


@app.post("/api/programs/{name}/run")
async def program_run(name: str, request: Request):
    b = await request.json()
    job = programs.run(S.machine, S.laser, S.bus, name, b.get("mode", "run"))
    return ok(job=job.snapshot())


@app.delete("/api/programs/{name}")
def program_delete(name: str):
    programs.delete(name)
    return ok()


# ---------------------------------------------------------------- sessions
@app.get("/api/sessions")
def sessions():
    return {"sessions": list_sessions()}


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    s = Session.open(sid)
    res = s.results()
    # locate runs are edge profiles, not a surface: never fit bow/warpage to them
    if res is None and s.points() and s.meta().get("kind") != "locate":
        res = scan.analyze_session(s)
    return {"meta": s.meta(), "recipe": s.recipe(), "results": res, "drift": s.drift(),
            "n_points": len(s.points())}


@app.get("/api/sessions/{sid}/points")
def session_points(sid: str):
    return {"points": Session.open(sid).points()}


@app.post("/api/sessions/{sid}/analyze")
async def session_analyze(sid: str, request: Request):
    b = await request.json()
    s = Session.open(sid)
    base = b.get("baseline")
    if base == "":
        base = None
    res = await asyncio.to_thread(scan.analyze_session, s, base, bool(b.get("drift", True)))
    s.update_meta(baseline=base)
    return ok(results=res)


@app.get("/api/sessions/{sid}/file/{fname}")
def session_file(sid: str, fname: str):
    s = Session.open(sid)
    p = s.path / Path(fname).name
    if not p.exists():
        raise FileNotFoundError(fname)
    return FileResponse(p, filename=f"{sid}_{p.name}")


@app.get("/api/compare")
def compare(a: str, b: str):
    from .analysis import compare as cmp
    return cmp(Session.open(a).points(), Session.open(b).points())


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    delete_session(sid)
    return ok()


@app.post("/api/open_folder")
async def open_folder(request: Request):
    b = await request.json()
    which = b.get("which", "scans")
    if which in DIRS:
        p = DIRS[which]
    else:
        p = Session.open(which).path
    if os.name == "nt":
        os.startfile(str(p))  # noqa: S606 - local desktop app
    return ok(path=str(p))


# ---------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q = S.bus.subscribe()
    await sock.send_text(json.dumps({"t": "hello", "version": __version__, "config": S.cfg.snapshot(),
                                     "history": list(S.bus.history)[-300:]}, default=str))
    alive = True

    async def sender():
        tick = 0
        while alive:
            items = []
            try:
                while len(items) < 400:
                    items.append(q.get_nowait())
            except queue.Empty:
                pass
            tick += 1
            msg = {"t": "tick", "machine": S.machine.snapshot(), "laser": S.laser.snapshot(), "events": items}
            if tick % 3 == 0 and S.laser.running:
                msg["trend"] = S.laser.trend(10.0, 250)
            await sock.send_text(json.dumps(msg, default=str))
            await asyncio.sleep(0.1)

    async def receiver():
        nonlocal alive
        while True:
            raw = await sock.receive_text()
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            op = m.get("op")
            try:
                if op == "jog_keep":
                    S.machine.jog_keepalive()
                    continue
                if op == "jog_stop":
                    S.machine.jog_stop()
                    continue
                if op == "jog_start":
                    res = await asyncio.to_thread(S.machine.jog_start, m.get("x", 0), m.get("y", 0), m.get("z", 0), m.get("speed", "normal"))
                elif op == "jog_step":
                    res = await asyncio.to_thread(S.machine.jog_step, m.get("x", 0), m.get("y", 0), m.get("z", 0), m.get("speed", "normal"))
                else:
                    continue
                await sock.send_text(json.dumps({"t": "jog", "ok": True, **res}))
            except Refused as e:
                await sock.send_text(json.dumps({"t": "jog", "ok": False, "error": str(e)}))
            except Exception as e:
                await sock.send_text(json.dumps({"t": "jog", "ok": False, "error": f"{type(e).__name__}: {e}"}))

    send_task = asyncio.create_task(sender())
    try:
        await receiver()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        alive = False
        S.bus.unsubscribe(q)
        send_task.cancel()
        S.machine.jog_stop(quiet=True)   # a closed tab never leaves a jog running


@app.on_event("shutdown")
def _shutdown():
    try:
        S.laser.stop()
    except Exception:
        pass
    S.machine.shutdown()

