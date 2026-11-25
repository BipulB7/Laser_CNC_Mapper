import sys, time, threading
sys.path.insert(0, ".")
from bowmapper.config import Config
from bowmapper.events import EventBus
from bowmapper.machine import Machine, Refused
from bowmapper.sim_grbl import SimGrbl
from bowmapper.laser import SimLaser
from bowmapper import scan

cfg = Config(); bus = EventBus()
q = bus.subscribe()
m = Machine(cfg, bus)
m.sim = SimGrbl(time_scale=20.0)
m.connect("sim")
print("state after connect:", m.g.state, m.g.firmware, "settings:", len(m.g.settings))
print("limits", cfg["machine"]["limits"], cfg["machine"]["limits_source"])
try:
    m.goto(10, 10, None)
except Refused as e:
    print("goto refused (expected):", e)
m.home()
while m.job: time.sleep(0.05)
print("after home:", m.g.state, m.g.mpos, m.homed, cfg["machine"]["limits"], m.last_job.status, m.last_job.error)
r = m.jog_step(dx=5); print("jog_step", r)
time.sleep(0.5); print("pos", m.g.mpos)
try:
    m.jog_step(dx=-50)
    time.sleep(0.5); print("after big -X step (clamped)", m.g.mpos)
except Refused as e: print("refused", e)
# continuous jog with keepalive then stop
print(m.jog_start(vy=1))
for _ in range(5): m.jog_keepalive(); time.sleep(0.1)
m.jog_stop(); time.sleep(0.3); print("after cont jog", m.g.state, m.g.mpos)
# watchdog
m.jog_start(vx=1); time.sleep(1.2); print("watchdog: jogging?", m._jog_active, m.g.state)
m.set_work_zero("XY"); print("wco", m.g.wco, "wpos", m.g.wpos)
j = m.goto(20, 30, None); 
while m.job: time.sleep(0.05)
print("goto done", m.last_job.status, m.last_job.error, "wpos", m.g.wpos)
las = SimLaser(cfg, bus, lambda: list(m.sim.phys)); las.start(); m.laser = las
time.sleep(0.5)
print("laser latest", las.latest())
# move over die: die at machine 196..236, 194..241; set Z so distance ~37.5
m.goto(216, 217, -84.0, coords="machine")
while m.job: time.sleep(0.05)
print("over die", m.last_job.status, m.last_job.error, las.latest())
j = scan.start_standoff(m, las, bus)
while m.job: time.sleep(0.05)
print("standoff", m.last_job.status, m.last_job.error, las.latest(), m.g.mpos)
m.set_work_zero("XY")
rec = {"name":"smoke die", "regions":[{"name":"Die","x0":-15,"y0":-18,"x1":15,"y1":18}], "pitch_x":5, "pitch_y":6, "samples":50, "settle_ms":50, "drift_every":10}
print("estimate", scan.estimate(scan.normalize(rec, cfg["scan"])))
j = scan.start_scan(m, las, bus, rec)
while m.job: time.sleep(0.1)
print("scan", m.last_job.status, m.last_job.error, m.last_job.result, m.last_job.progress)
from bowmapper.sessions import Session
s = Session.open(m.last_job.result["session"])
res = s.results(); o = res["overall"]
print({k: (round(v,3) if isinstance(v,float) else v) for k,v in o.items() if k in ("n_valid","bow_um","shape","warpage_pv_um","tilt_mrad","slope_x_um_per_mm","rms_um")})
print("drift", res["corrections"]["drift"])
print("sim overflows", m.sim.overflows, "max rx", m.sim.max_rx)
notes = []
while not q.empty():
    e = q.get()
    if e["t"] == "note": notes.append((e["level"], e["text"]))
for n in notes: print("NOTE", n)
las.stop(); m.shutdown()
