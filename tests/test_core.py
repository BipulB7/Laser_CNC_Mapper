"""Core behaviour against the simulated controller. Run:  .venv\\Scripts\\python -m pytest -q"""

import math
import time

import numpy as np
import pytest

from bowmapper import analysis, programs, scan
from bowmapper.config import DIRS, Config
from bowmapper.events import EventBus
from bowmapper.gcode import analyze_gcode
from bowmapper.laser import SimLaser
from bowmapper.machine import Machine, Refused
from bowmapper.sim_grbl import SimGrbl


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    for k in ("scans", "programs", "recipes", "backups"):
        d = tmp_path / k
        d.mkdir()
        monkeypatch.setitem(DIRS, k, d)
    cfg = Config(tmp_path / "config.json")
    bus = EventBus()
    m = Machine(cfg, bus)
    m.sim = SimGrbl(time_scale=40.0)
    m.connect("sim")
    yield m, cfg, bus
    m.shutdown()


def wait_job(m, t=60):
    t0 = time.time()
    while m.job and time.time() - t0 < t:
        time.sleep(0.02)
    assert not m.job, "job did not finish"
    return m.last_job


def home(m):
    m.home()
    j = wait_job(m)
    assert j.status == "done", j.error


def test_boots_locked_and_blocks_goto(rig):
    m, cfg, _ = rig
    assert m.g.state == "Alarm"
    with pytest.raises(Refused):
        m.goto(10, 10, None)


def test_homing_derives_limits(rig):
    m, cfg, _ = rig
    home(m)
    assert m.homed
    assert cfg["machine"]["limits"] == {"x": [0.0, 400.0], "y": [0.0, 400.0], "z": [-130.0, 0.0]}
    assert m.g.mpos == [2.0, 2.0, -2.0]


def test_jog_step_clamps_to_margin(rig):
    m, *_ = rig
    home(m)
    m.jog_step(dx=-50)
    time.sleep(0.3)
    assert m.g.state == "Idle"
    assert m.g.mpos[0] == pytest.approx(0.5)
    with pytest.raises(Refused):
        m.jog_step(dx=-1)


def test_z_floor_blocks_down(rig):
    m, *_ = rig
    home(m)
    m.set_z_floor(m.g.mpos[2])
    with pytest.raises(Refused):
        m.jog_step(dz=-1)
    with pytest.raises(Refused):
        m.goto(None, None, -50, coords="machine")


def test_continuous_jog_watchdog(rig):
    m, *_ = rig
    home(m)
    m.sim.ts = 1.0
    m.jog_start(vy=1)
    time.sleep(1.5)                      # no keep-alives
    assert not m._jog_active
    time.sleep(0.3)
    y = m.g.mpos[1]
    time.sleep(0.3)
    assert m.g.mpos[1] == pytest.approx(y)
    assert y < 60                        # stopped well before the end of travel


def test_stream_never_overflows_rx_buffer(rig):
    m, *_ = rig
    home(m)
    lines = [f"G1X{10 + (i % 50) * 0.5:.3f}Y{10 + (i // 50) * 0.5:.3f}F20000" for i in range(400)]

    def body(job):
        m.stream_gcode(job, lines)
        m.job_wait(job)

    m.start_job("gcode", "stream test", body)
    j = wait_job(m, 120)
    assert j.status == "done", j.error
    assert m.sim.overflows == 0
    assert m.sim.max_rx <= 128
    assert m.g.mpos[0] == pytest.approx(10 + 49 * 0.5)


def test_hard_limit_alarm_clears_homed(rig):
    m, *_ = rig
    home(m)
    m.console("$J=G91X-20F2000")         # raw jog, bypasses the app's clamp
    time.sleep(0.6)
    assert m.g.state == "Alarm"
    assert not m.homed


def test_stop_during_move_keeps_homing(rig):
    m, *_ = rig
    home(m)
    m.sim.ts = 1.0
    m.goto(300, 300, None, coords="machine", safe_z=False)
    time.sleep(0.5)
    m.stop()
    wait_job(m)
    time.sleep(0.5)
    assert m.g.state == "Idle"
    assert m.homed
    assert m.g.mpos[0] < 300


def test_spindle_blocked(rig):
    m, *_ = rig
    home(m)
    with pytest.raises(Refused):
        m.console("M3 S1000")


def test_gcode_analyzer():
    allowed = {"x": [0.5, 399.5], "y": [0.5, 399.5], "z": [-129.5, -0.5]}
    r = analyze_gcode("G90\nG1 X10 Y10\nM3 S100\nG0 X500\n$H\n", [0, 0, 0], [5, 5, -5], allowed, True)
    texts = " ".join(e["text"] for e in r["errors"])
    assert "Feed rate" in texts and "spindle" in texts and "outside" in texts and "System" in texts
    r = analyze_gcode("G21 G90 F600\nG0 X20 Y20\nG2 X40 Y20 I10 J0\n", [0, 0, 0], [5, 5, -5], allowed, True)
    assert r["ok"]
    ys = [s[1] for s in r["preview"]] + [s[3] for s in r["preview"]]
    assert max(ys) == pytest.approx(30, abs=0.05) or min(ys) == pytest.approx(10, abs=0.05)


def test_bow_sign_and_value():
    xs, ys = np.meshgrid(np.linspace(-20, 20, 21), np.linspace(-10, 10, 11))
    x, y = xs.ravel(), ys.ravel()
    dome = 0.010 * (1 - (x / 20) ** 2) + 0.002 * x / 20          # 10 um dome along X + tilt
    m = analysis.surface_metrics(x, y, dome)
    assert m["bow_um"] == pytest.approx(10, rel=0.02)
    assert m["shape"].startswith("dome")
    m = analysis.surface_metrics(x, y, -dome)
    assert m["bow_um"] == pytest.approx(-10, rel=0.02)
    m = analysis.surface_metrics(x, y, 0.001 * x + 0.0005 * y)   # pure tilt
    assert abs(m["bow_um"]) < 1e-6 and m["warpage_pv_um"] < 1e-6


def test_csv_and_json_programs(rig):
    m, *_ = rig
    p = programs.csv_program("pts.csv", "x,y,label\n0,0,a\n5,5,b\n")
    assert [s["do"] for s in p["steps"]] == ["move", "measure", "move", "measure"]
    with pytest.raises(Refused):
        programs.parse_measure("bad.json", '{"steps":[{"do":"fly"}]}')
    home(m)
    prog = programs.parse_measure("ok.json", '{"steps":[{"do":"repeat","times":3,"steps":[{"do":"move","x":1},{"do":"measure"}]}]}')
    a = programs.analyze_measure(m, prog)
    assert a["ok"] and a["measures"] == 3 and a["steps"] == 6


def test_scan_end_to_end(rig):
    m, cfg, bus = rig
    home(m)
    las = SimLaser(cfg, bus, lambda: list(m.sim.phys))
    las.start()
    m.laser = las
    try:
        m.goto(216, 217.5, -79.6, coords="machine")
        wait_job(m)
        m.set_work_zero("XY")
        rec = {"name": "t", "regions": [{"name": "Die", "x0": -15, "y0": -18, "x1": 15, "y1": 18}],
               "pitch_x": 6, "pitch_y": 6, "samples": 40, "settle_ms": 20, "drift_every": 10}
        scan.start_scan(m, las, bus, rec)
        j = wait_job(m, 120)
        assert j.status == "done", j.error
        from bowmapper.sessions import Session
        res = Session.open(j.result["session"]).results()
        o = res["overall"]
        # sim silicon: 22 um dome over 49.93 x 59.31; this window reaches u=0.60, v=0.61
        expected = 100 * ((15 / 29.655) ** 2 + (18 / 24.965) ** 2) / 2   # sim die as mounted: 100 um dome, 59.31 x 49.93
        assert o["bow_um"] == pytest.approx(expected, abs=2.5)
        assert o["shape"].startswith("dome")
        assert res["corrections"]["drift"]["applied"]
    finally:
        las.stop()


def test_scan_refused_outside_travel(rig):
    m, cfg, bus = rig
    home(m)
    las = SimLaser(cfg, bus, lambda: list(m.sim.phys))
    las.start()
    try:
        with pytest.raises(Refused):
            scan.start_scan(m, las, bus, {"regions": [{"x0": -10, "y0": -10, "x1": 10, "y1": 10}], "pitch_x": 5})
    finally:
        las.stop()


def test_geometry_helpers():
    assert scan._axis(0, 10, 3) == pytest.approx([0, 10 / 3, 20 / 3, 10])
    r = scan.normalize({"regions": [{"x0": 0, "y0": 0, "x1": 2, "y1": 1}], "pitch_x": 1, "pattern": "serpentine"}, {})
    pts = scan.build_points(r)
    assert [(p["ix"], p["iy"]) for p in pts] == [(0, 0), (1, 0), (2, 0), (2, 1), (1, 1), (0, 1)]
    assert math.isclose(pts[-1]["x"], 0)
