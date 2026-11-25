import sys, time
sys.path.insert(0, ".")
from bowmapper.config import Config
from bowmapper.events import EventBus
from bowmapper.machine import Machine
from bowmapper.sim_grbl import SimGrbl
cfg = Config(); bus = EventBus(); q = bus.subscribe()
m = Machine(cfg, bus); m.sim = SimGrbl(time_scale=20.0); m.connect("sim")
orig = m.g._handle
def h(line):
    if True:
        print(f"{time.monotonic():.3f} RX {line[:90]}")
    orig(line)
m.g._handle = h
print("---- HOME"); m.home()
while m.job: time.sleep(0.05)
print(cfg["machine"]["limits"])
m.shutdown()
