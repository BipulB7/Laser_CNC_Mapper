"""Die outline: edge walks, manual corners, laser refine, auto find, trace (simulated OAM as mounted)."""

import math
import random
import time

import pytest

from bowmapper import locate, scan
from bowmapper.laser import SimLaser
from tests.test_core import home, rig, wait_job  # noqa: F401  (fixture)

TRUE_W, TRUE_H = SimLaser.SI[0], SimLaser.SI[1]      # flat top as mounted: 59.31 along X, 49.93 along Y


def surf(edge, outside, ramp=0.0, drop=0.0, dropout_at=None):
    """1-D test surface: flat die (slight bow) to `edge`, optional roll-off, then `outside`."""
    def m(s):
        if dropout_at is not None and abs(s - dropout_at) < 1e-9:
            return None
        if s < edge:
            return 0.00001 * s * s
        if s < edge + ramp:
            return 0.00001 * edge * edge - drop * (s - edge) / ramp
        return outside
    return m


def test_walk_out_drop_rise_bow_dropout():
    r = locate.walk_out(surf(24.9, -0.35, dropout_at=10.0), 1.0, 45, 1.0, 0.0, 0.25)
    assert r["s_in"] == 24.0 and r["s_out"] == 25.0
    r = locate.walk_out(surf(24.9, +2.3), 1.0, 45, 1.0, 0.0, 0.25)            # higher feature past the edge
    assert r["s_out"] == 25.0
    with pytest.raises(locate.EdgeError):
        locate.walk_out(surf(24.9, -0.35), 30.0, 45, 1.0, 0.0, 0.25)          # starts off the die
    with pytest.raises(locate.EdgeError):
        locate.walk_out(surf(99, 0), 1.0, 45, 1.0, 0.0, 0.25)                 # never leaves the die


def test_gradual_roll_off_is_not_crept_over():
    """The real failure: 0.6 mm roll-off sampled at 0.05 mm. The old adaptive reference walked down it."""
    m = surf(29.45, -0.6, ramp=0.6, drop=0.45)
    r = locate.walk_out(m, 26.5, 31, 0.25, 0.0, 0.25, lag=1.0)
    assert 29.4 < r["s_out"] < 30.3
    f = locate.walk_out(m, r["s_in"] - 0.5, r["s_out"] + 0.6, 0.05, r["level"], 0.1, lag=None)
    edge = locate.knee(f["level"], f["tail"], f["s_in"], f["s_out"], 0.05)
    assert edge == pytest.approx(29.45, abs=0.02)     # top corner of the die, not part-way down the roll-off


def test_fit_corners_any_order_and_quality():
    W, H, th, cx, cy = 59.31, 49.93, 0.4, 0.6, -0.3
    pts = locate.die_corners([cx, cy], W, H, th)
    random.Random(3).shuffle(pts)
    f = locate.fit_corners(pts)                      # die_corners rounds to 0.1 um
    assert f["center"] == pytest.approx([cx, cy], abs=1e-3)
    assert f["width"] == pytest.approx(W, abs=1e-3) and f["height"] == pytest.approx(H, abs=1e-3)
    assert f["angle_deg"] == pytest.approx(th, abs=1e-3)
    assert f["worst_corner_mm"] < 1e-3
    pts[0] = [pts[0][0] + 0.8, pts[0][1]]            # one badly placed corner shows up
    assert locate.fit_corners(pts)["worst_corner_mm"] > 0.15


def test_fit_rectangle_recovers_pose():
    W, H, th, cx, cy = 49.93, 59.31, math.radians(0.5), 1.2, -0.7
    c, s = math.cos(th), math.sin(th)

    def P(u, v):
        return (cx + u * c - v * s, cy + u * s + v * c)
    edges = {"left": [P(-W / 2, 15), P(-W / 2, -15)], "right": [P(W / 2, -15), P(W / 2, 15)],
             "bottom": [P(12, -H / 2), P(-12, -H / 2)], "top": [P(-12, H / 2), P(12, H / 2)]}
    f = locate.fit_rectangle(edges)
    assert f["angle_deg"] == pytest.approx(0.5, abs=1e-6)
    assert f["width"] == pytest.approx(W, abs=1e-6) and f["center"] == pytest.approx([cx, cy], abs=1e-6)


def _setup(m, cfg, bus, dx=1.2, dy=-0.8):
    home(m)
    las = SimLaser(cfg, bus, lambda: list(m.sim.phys))
    las.start()
    m.laser = las
    m.goto(SimLaser.OAM_C[0] + dx, SimLaser.OAM_C[1] + dy, -79.5, coords="machine")
    wait_job(m)
    m.set_work_zero("XY")
    return las


def _check_truth(cfg, tol_c=0.06, tol_size=0.08, tol_ang=0.06):
    a = cfg["align"]
    assert a["center_machine"] == pytest.approx(list(SimLaser.OAM_C), abs=tol_c)
    assert a["angle_deg"] == pytest.approx(SimLaser.OAM_DEG, abs=tol_ang)
    assert a["width"] == pytest.approx(TRUE_W, abs=tol_size) and a["height"] == pytest.approx(TRUE_H, abs=tol_size)


def test_manual_corners_then_refine_then_trace(rig):  # noqa: F811
    m, cfg, bus = rig
    las = _setup(m, cfg, bus)
    try:
        # hand-placed corners, each off by up to ~0.25 mm, entered in a scrambled order
        true = locate.die_corners(list(SimLaser.OAM_C), TRUE_W, TRUE_H, SimLaser.OAM_DEG)
        err = [(0.2, -0.1), (-0.15, 0.25), (0.1, 0.2), (-0.25, -0.05)]
        entered = [[x + ex, y + ey] for (x, y), (ex, ey) in zip(true, err)]
        fit = locate.manual_corners(m, bus, [entered[2], entered[0], entered[3], entered[1]],
                                    {"width": 59.31, "height": 49.93})
        assert fit["source"] == "manual corners"
        _check_truth(cfg, tol_c=0.2, tol_size=0.5, tol_ang=0.6)
        assert not fit["warnings"] and fit["ok"]
        assert m.g.wco[0] == pytest.approx(fit["center_machine"][0], abs=1e-3)
        # laser refine from the manual outline
        locate.start_refine(m, las, bus, {"samples": 10, "settle_ms": 10})
        j = wait_job(m, 300)
        assert j.status == "done", j.error
        _check_truth(cfg)
        # trace stops exactly at each outline corner
        found = cfg["align"]["corners_machine"]
        locate.start_trace(m, bus, {"feed": 3000, "stop_at_corners": True})
        seen = []
        t0 = time.time()
        while m.job and time.time() - t0 < 120:
            if m.job.paused:
                seen.append([round(v, 3) for v in m.g.mpos[:2]])
                m.resume_job()
            time.sleep(0.05)
        assert m.last_job.status == "done", m.last_job.error
        assert len(seen) == 4
        for p_, f in zip(seen, found):
            assert p_ == pytest.approx(f, abs=0.01)
    finally:
        las.stop()


def test_auto_find_on_real_like_part(rig):  # noqa: F811
    m, cfg, bus = rig
    las = _setup(m, cfg, bus, dx=-2.0, dy=1.5)
    try:
        locate.start_find_silicon(m, las, bus, {"template": "mi350_oam", "samples": 10, "settle_ms": 10})
        j = wait_job(m, 400)
        assert j.status == "done", j.error
        _check_truth(cfg)
        assert not j.result["fit"]["warnings"]
        # a rotated scan inside the outline stays on the flat die
        a = cfg["align"]
        rec = {"name": "si", "regions": [{"name": "Si", "x0": -a["width"] / 2 + 0.5, "y0": -a["height"] / 2 + 0.5,
                                          "x1": a["width"] / 2 - 0.5, "y1": a["height"] / 2 - 0.5}],
               "pitch_x": 14, "pitch_y": 12, "samples": 10, "settle_ms": 10, "drift_every": 0, "rotation_deg": a["angle_deg"]}
        scan.start_scan(m, las, bus, rec)
        j = wait_job(m, 120)
        assert j.status == "done", j.error
        from bowmapper.sessions import Session
        pts = Session.open(j.result["session"]).points()
        hs = [float(p["height_mm"]) for p in pts]
        assert max(hs) - min(hs) < 0.15                  # all on the die (dome ~0.1 mm), none on the roll-off
    finally:
        las.stop()
