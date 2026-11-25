"""Find out how MEDAQLib talks to the ILD1320 on this PC and show what it reads.

Run via TEST_Laser.bat (close sensorTOOL first). Optional:  TEST_Laser.bat COM5
Writes a MEDAQLib log to logs\\medaq_probe.log for troubleshooting.
"""

from __future__ import annotations

import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bowmapper.medaq import MedaqError, find_laser_port, open_ild1320  # noqa: E402

SENTINEL = -1e8


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else ""
    print("\nSerial ports on this PC:")
    try:
        from serial.tools import list_ports
        for p in sorted(list_ports.comports(), key=lambda p: p.device):
            tag = "  <- FTDI, likely the IF2001/USB (laser)" if (p.vid or 0) == 0x0403 else ""
            print(f"  {p.device:<7} {p.description}{tag}")
    except ImportError:
        print("  (pyserial missing)")
    port = port or find_laser_port()
    if not port:
        print("\nNo FTDI port found. Plug in the IF2001/USB, or pass the port: TEST_Laser.bat COM5")
        return 1
    print(f"\nTrying the laser on {port} (sensorTOOL must be closed)...")
    log = ROOT / "logs" / "medaq_probe.log"
    log.parent.mkdir(exist_ok=True)
    try:
        dev, used = open_ild1320(ROOT / "drivers" / "MEDAQLib.dll", port, "auto", log_file=str(log),
                                 report=lambda s: print("  " + s))
    except MedaqError as e:
        print(f"\nFAILED: {e}\nMEDAQLib log: {log}")
        return 2
    print(f"\nConnected: interface {used['interface']}, port {used['port']}, baud {used['baud'] or 'default'}")
    print("Reading for 3 seconds... (hold a flat target 25-50 mm under the laser)")
    vals: list[float] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0:
        n = dev.data_avail()
        if n > 0:
            vals += dev.transfer(min(n, 20000))
        else:
            time.sleep(0.005)
    el = time.monotonic() - t0
    dev.close()
    good = [v for v in vals if math.isfinite(v) and v > SENTINEL and -1 < v < 60]
    print(f"\n  samples      {len(vals)}  ({len(vals) / el:.0f} per second)")
    print(f"  valid        {len(good)}  ({100 * len(good) / max(1, len(vals)):.1f} %)")
    if good:
        mean = statistics.fmean(good)
        sd = statistics.pstdev(good) * 1000
        print(f"  mean         {mean:.4f} mm")
        print(f"  min / max    {min(good):.4f} / {max(good):.4f} mm")
        print(f"  noise (1 sd) {sd:.2f} um")
        scale = "0..25 (from start of range): distance from face = reading + 25" if mean < 24.5 else \
                "25..50 (from sensor face)" if mean > 25.5 else "ambiguous (near 25 mm) - move the target and rerun"
        print(f"  scale        {scale}")
        print("\nCompare 'mean' with what sensorTOOL shows for the same target.")
    else:
        bad = sorted(set(round(v, 1) for v in vals[:50]))[:8]
        print(f"  no valid readings - target out of range? first raw values: {bad}")
    print("\nNext: start the app, Laser tab > ILD1320 > Connect (port 'auto' remembers this combination).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
