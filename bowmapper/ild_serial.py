"""Direct RS422 driver for the optoNCDT ILD1320, no MEDAQLib (Linux / Raspberry Pi).

The IF2001/USB (FTDI) is a plain serial port. From the optoNCDT 1320 manual:
- ch. 8.1: 8N1, factory 921600 baud, max 1 MBaud.
- ch. 8.2: each output value is 3 bytes, sent L, M, H. Top two bits tag the byte:
  L = 00, M = 01, H = 10 for the first value of a frame, 11 for additional
  values (OUTADD_RS422). The low 6 bits of each give D5..0, D11..6, D17..12.
- ch. 7.5.2.1: distance, no zeroing/mastering: d = (102/65520 * x - 1) * MR / 100 mm,
  measured from the start of the measuring range (0 at SMR). 262075..262082 are
  state codes (no peak, too near, too far, ...).
- ch. 16.1: ASCII commands end in LF; every answer ends with the prompt "->".
"""

from __future__ import annotations

import os
import re
import time

BAUDS = [921600, 691200, 460800, 230400, 115200, 1000000]
MAX_DIST = 65520                 # 16-bit distance values run 0..65520
STATE_CODES = {
    262075: "data volume too large for the baud rate",
    262076: "no peak (no target / too dark)",
    262077: "target too near (before measuring range)",
    262078: "target too far (after measuring range)",
    262080: "value cannot be evaluated",
    262081: "peak too wide",
    262082: "laser is off",
}


class SerialLaserError(Exception):
    pass


class FrameDecoder:
    """Bytes in, raw 18-bit values out (first value of each frame only = distance)."""

    def __init__(self):
        self.l = None
        self.m = None
        self.bad = 0        # bytes thrown away while resyncing
        self.values = 0     # complete values decoded (all positions in the frame)

    def feed(self, data: bytes) -> list[int]:
        out = []
        lo, mid = self.l, self.m
        for b in data:
            tag = b >> 6
            if tag == 0:
                if lo is not None:
                    self.bad += 1 + (mid is not None)
                lo, mid = b & 0x3F, None
            elif tag == 1:
                if lo is None or mid is not None:
                    self.bad += 1 + (lo is not None) + (mid is not None)
                    lo = mid = None
                    continue
                mid = b & 0x3F
            else:
                if lo is None or mid is None:
                    self.bad += 1 + (lo is not None)
                    lo = mid = None
                    continue
                self.values += 1
                if tag == 2:
                    out.append(lo | (mid << 6) | ((b & 0x3F) << 12))
                lo = mid = None
        self.l, self.m = lo, mid
        return out


def to_mm(x: int, mr_mm: float) -> float | None:
    """Raw distance value -> mm from the start of the measuring range; None for state codes."""
    if x > MAX_DIST:
        return None
    return (102.0 / 65520.0 * x - 1.0) * mr_mm / 100.0


def find_laser_port() -> str:
    """First FTDI (IF2001/USB) port, preferring one that names Micro-Epsilon."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return ""
    ftdi = [p for p in sorted(list_ports.comports(), key=lambda p: p.device) if (p.vid or 0) == 0x0403]
    for p in ftdi:
        if "micro-epsilon" in f"{p.manufacturer} {p.product} {p.description}".lower():
            return p.device
    return ftdi[0].device if ftdi else ""


def resolve_port(port: str | None) -> str:
    port = (port or "").strip()
    if not port or port.lower() == "auto" or (os.name != "nt" and port.upper().startswith("COM")):
        port = find_laser_port()
    if not port:
        raise SerialLaserError("No laser USB adapter (FTDI IF2001/USB) found: plug it in and power the sensor.")
    return port


def open_port(port: str, baud: int):
    import serial
    s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.02, write_timeout=1)
    try:
        s.set_low_latency_mode(True)        # Linux ftdi_sio: 1 ms latency instead of 16 ms
    except Exception:
        pass
    return s


def sniff(s, seconds: float) -> bytes:
    buf = bytearray()
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        buf += s.read(max(1, s.in_waiting))
    return bytes(buf)


def command(s, cmd: str, wait_s: float = 1.0) -> str:
    """Send one ASCII command (only while the sensor is not streaming) and return the answer up to "->"."""
    s.reset_input_buffer()
    s.write((cmd + "\r\n").encode("ascii"))
    buf = bytearray()
    t_end = time.monotonic() + wait_s
    while time.monotonic() < t_end:
        buf += s.read(max(1, s.in_waiting))
        if b"->" in buf:
            break
    return buf.decode("ascii", errors="replace")


def looks_like_stream(data: bytes) -> tuple[bool, int]:
    dec = FrameDecoder()
    vals = dec.feed(data)
    ok = dec.values >= 20 and dec.values * 3 >= 0.9 * len(data)
    return ok, len(vals)


def open_ild1320_serial(port: str | None = "auto", baud: int | None = None, hint: dict | None = None,
                        enable_output: bool = True, report=None):
    """Find the sensor's baud rate and make sure distance values are streaming.

    Tries the configured baud, else the last one that worked, then BAUDS. At
    each: if well-formed frames are already arriving, done. If the line is
    silent, ask GETINFO; if the sensor answers, switch its output to RS422.
    Returns (serial.Serial, {"port", "baud", "info"}).
    """
    port = resolve_port(port)
    if baud:
        cands = [int(baud)]
    else:
        cands = list(BAUDS)
        if hint and hint.get("baud") in cands:
            cands.remove(hint["baud"])
            cands.insert(0, hint["baud"])
    errors = []

    def say(msg):
        if report:
            report(msg)

    for bd in cands:
        try:
            s = open_port(port, bd)
        except Exception as e:
            raise SerialLaserError(f"Cannot open {port}: {e}") from e
        try:
            s.reset_input_buffer()
            data = sniff(s, 0.3)
            if data:
                ok, n = looks_like_stream(data)
                if ok:
                    say(f"OK    {port} @ {bd}: {n} values in 0.3 s")
                    return s, {"port": port, "baud": bd, "info": ""}
                raise SerialLaserError(f"{len(data)} bytes but no valid frames (wrong baud)")
            ans = command(s, "GETINFO")
            if "->" not in ans:
                raise SerialLaserError("silent, no answer to GETINFO")
            info = " ".join(ans.replace("->", " ").split())[:200]
            say(f"..    {port} @ {bd}: sensor answers but is not streaming ({info[:80]})")
            if not enable_output:
                raise SerialLaserError("sensor answers but its data output is not RS422")
            r = command(s, "OUTPUT RS422")
            if re.search(r"\bE\d{3}\b", r):
                raise SerialLaserError(f"OUTPUT RS422 refused: {r.strip()}")
            data = sniff(s, 0.4)
            ok, n = looks_like_stream(data)
            if not ok:
                raise SerialLaserError("switched output to RS422 but no data followed")
            say(f"OK    {port} @ {bd}: output switched to RS422, {n} values in 0.4 s")
            return s, {"port": port, "baud": bd, "info": info}
        except Exception as e:
            errors.append(f"{bd}: {e}")
            say(f"FAIL  {port} @ {bd}: {e}")
            try:
                s.close()
            except Exception:
                pass
    raise SerialLaserError(f"No ILD1320 data on {port}. Tried " + " | ".join(errors)
                           + ". Is the sensor's 24 V on? Is another program (sensorTOOL, a second Bow Mapper) holding the port?")
