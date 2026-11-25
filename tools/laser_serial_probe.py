"""Check the ILD1320 over the IF2001/USB without MEDAQLib (Linux / Raspberry Pi).

Stop Bow Mapper first (it holds the port), then from the repo folder:
    .venv/bin/python tools/laser_serial_probe.py            # auto port + baud
    .venv/bin/python tools/laser_serial_probe.py /dev/ttyUSB0 921600
Prints the port/baud found and 2 s of readings.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bowmapper.ild_serial import (  # noqa: E402
    STATE_CODES, FrameDecoder, SerialLaserError, open_ild1320_serial, to_mm,
)

SMR_MM = 25.0      # ILD1320-25
MR_MM = 25.0


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "auto"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print("\nSerial ports:")
    from serial.tools import list_ports
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        tag = "  <- FTDI (laser adapter)" if (p.vid or 0) == 0x0403 else ""
        print(f"  {p.device:<14} {p.description}{tag}")
    print("\nLooking for the sensor:")
    try:
        s, used = open_ild1320_serial(port, baud, report=lambda m: print("  " + m))
    except SerialLaserError as e:
        print(f"\n  {e}\n")
        return 1
    if used.get("info"):
        print(f"  GETINFO: {used['info']}")
    dec = FrameDecoder()
    raw: list[int] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        raw += dec.feed(s.read(max(1, s.in_waiting)))
    el = time.monotonic() - t0
    s.close()
    good = [to_mm(x, MR_MM) for x in raw if x <= 65520]
    states: dict[str, int] = {}
    for x in raw:
        if x > 65520:
            k = STATE_CODES.get(x, f"code {x} (zeroing/mastering on?)")
            states[k] = states.get(k, 0) + 1
    print(f"\n  {len(raw)} values in {el:.1f} s = {len(raw) / el:.0f} Hz, {dec.bad} bad bytes")
    if good:
        m = statistics.fmean(good)
        sd = statistics.pstdev(good) * 1000 if len(good) > 1 else 0.0
        print(f"  valid {len(good)}: mean {m:.4f} mm from SMR = {m + SMR_MM:.4f} mm from the sensor face, "
              f"noise {sd:.2f} um (1 sigma)")
    for k, n in states.items():
        print(f"  {n:>6} x {k}")
    print()
    return 0 if good else 2


if __name__ == "__main__":
    raise SystemExit(main())
