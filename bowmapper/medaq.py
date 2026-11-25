"""Minimal ctypes binding to Micro-Epsilon MEDAQLib (64-bit), enough for the ILD1320.

Signatures follow Micro-Epsilon's own MEDAQLib.py wrapper. The DLL path is
explicit (the vendor wrapper loads from the current directory, which breaks
as soon as the app is started from anywhere else).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, byref, c_char_p, c_double, c_int32, c_uint32
from pathlib import Path

ERR_NOERROR = 0
ERR_WARNING = -26
ERR_NAMES = {
    -1: "function not supported", -2: "cannot open", -3: "not open", -4: "error applying parameters",
    -5: "error sending command", -6: "error clearing buffer", -7: "hardware communication error",
    -8: "timeout reading from sensor", -9: "error reading sensor data", -10: "interface not supported",
    -11: "already open", -12: "cannot create interface", -13: "no sensor data available",
    -14: "unknown sensor command", -15: "unknown sensor answer", -16: "sensor answer error",
    -17: "sensor answer too short", -18: "wrong parameter", -19: "out of memory",
    -20: "no answer received", -21: "answer does not match command", -22: "baud rate too low",
    -23: "overflow", -24: "instance does not exist", -25: "not found", -26: "warning",
    -27: "sensor answer warning",
}


class MedaqError(Exception):
    pass


class Medaq:
    def __init__(self, dll_path: str | Path):
        if os.name != "nt":
            raise MedaqError("MEDAQLib needs Windows.")
        p = Path(dll_path)
        if not p.exists():
            raise MedaqError(f"MEDAQLib.dll not found at {p}. Copy the 64-bit DLL there or set the path in Machine > Laser.")
        try:
            os.add_dll_directory(str(p.parent.resolve()))
        except (OSError, AttributeError):
            pass
        try:
            self.lib = ctypes.WinDLL(str(p.resolve()))
        except OSError as e:
            raise MedaqError(f"Cannot load {p.name}: {e} (a 32-bit DLL with 64-bit Python?)") from e
        L = self.lib
        L.CreateSensorInstance.argtypes = [c_int32]
        L.CreateSensorInstance.restype = c_uint32
        for name in ("SetParameterString",):
            getattr(L, name).argtypes = [c_uint32, c_char_p, c_char_p]
            getattr(L, name).restype = c_int32
        L.SetParameterInt.argtypes = [c_uint32, c_char_p, c_int32]
        L.SetParameterInt.restype = c_int32
        L.SetParameterDouble.argtypes = [c_uint32, c_char_p, c_double]
        L.SetParameterDouble.restype = c_int32
        for name in ("OpenSensor", "CloseSensor", "ReleaseSensorInstance"):
            getattr(L, name).argtypes = [c_uint32]
            getattr(L, name).restype = c_int32
        L.DataAvail.argtypes = [c_uint32, POINTER(c_int32)]
        L.DataAvail.restype = c_int32
        L.TransferData.argtypes = [c_uint32, POINTER(c_int32), POINTER(c_double), c_int32, POINTER(c_int32)]
        L.TransferData.restype = c_int32
        L.GetError.argtypes = [c_uint32, c_char_p, c_uint32]
        L.GetError.restype = c_int32
        self.h = 0

    def _check(self, ret: int, what: str) -> None:
        if ret not in (ERR_NOERROR, ERR_WARNING):
            raise MedaqError(f"{what}: {ret} ({ERR_NAMES.get(ret, 'error')}) {self.error_text()}".strip())

    def error_text(self) -> str:
        if not self.h:
            return ""
        buf = ctypes.create_string_buffer(1024)
        try:
            self.lib.GetError(self.h, buf, 1024)
            return buf.value.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def create(self, sensor_id: int) -> None:
        self.h = self.lib.CreateSensorInstance(int(sensor_id))
        if not self.h:
            raise MedaqError("CreateSensorInstance failed (unknown sensor id?)")

    def set_param(self, name: str, value) -> None:
        if isinstance(value, bool) or isinstance(value, int):
            ret = self.lib.SetParameterInt(self.h, name.encode(), int(value))
        elif isinstance(value, float):
            ret = self.lib.SetParameterDouble(self.h, name.encode(), value)
        else:
            ret = self.lib.SetParameterString(self.h, name.encode(), str(value).encode())
        self._check(ret, f"SetParameter {name}")

    def open(self) -> None:
        self._check(self.lib.OpenSensor(self.h), "OpenSensor")

    def close(self) -> None:
        if self.h:
            try:
                self.lib.CloseSensor(self.h)
                self.lib.ReleaseSensorInstance(self.h)
            finally:
                self.h = 0

    def data_avail(self) -> int:
        n = c_int32(0)
        self._check(self.lib.DataAvail(self.h, byref(n)), "DataAvail")
        return n.value

    def transfer(self, max_values: int) -> list[float]:
        raw = (c_int32 * max_values)()
        scaled = (c_double * max_values)()
        read = c_int32(0)
        self._check(self.lib.TransferData(self.h, raw, scaled, max_values, byref(read)), "TransferData")
        return list(scaled[: read.value])


# The IF2001/USB (RS422 -> USB, FTDI) appears as a COM port; this MEDAQLib
# build has no IF2001_USB interface ("does not exist"), so it is opened as
# "RS232". Baud None = MEDAQLib default; otherwise the ILD1320's RS422 rates,
# factory default 921600 first.
AUTO_CANDIDATES = [
    ("RS232", None),
    ("RS232", 921600),
    ("RS232", 691200),
    ("RS232", 460800),
    ("RS232", 230400),
    ("RS232", 115200),
]


def find_laser_port() -> str:
    """COM port of the first FTDI device (IF2001/USB), or ''."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return ""
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        if (p.vid or 0) == 0x0403:
            return p.device
    return ""


def open_ild1320(dll_path, port: str, interface: str = "auto", baud=None, sensor_id: int = 41,
                 extra: dict | None = None, log_file: str | None = None, hint: dict | None = None,
                 wait_s: float = 1.5, report=None):
    """Open the sensor and confirm data is flowing.

    interface="auto" tries the hint first (the combination that worked last
    time), then AUTO_CANDIDATES. Returns (Medaq, {"interface", "baud", "port"}).
    report(str) receives one line per attempt.
    """
    import time

    port = port if port and port.lower() != "auto" else find_laser_port()
    if not port:
        raise MedaqError("No laser COM port: set it on the Laser tab (Device Manager > Ports > USB Serial Port).")
    if interface and interface != "auto":
        cands = [(interface, baud)]
    else:
        cands = list(AUTO_CANDIDATES)
        if hint and hint.get("interface"):
            h = (hint["interface"], hint.get("baud"))
            cands = [h] + [c for c in cands if c != h]
    errors = []
    for itf, bd in cands:
        dev = Medaq(dll_path)
        try:
            dev.create(int(sensor_id))
            dev.set_param("IP_Interface", itf)
            dev.set_param("IP_Port", port)
            if bd:
                dev.set_param("IP_Baudrate", int(bd))
            if log_file:
                dev.set_param("IP_EnableLogging", 1)
                dev.set_param("IP_LogFile", str(log_file))
            for k, v in (extra or {}).items():
                dev.set_param(k, v)
            dev.open()
            t_end = time.monotonic() + wait_s
            n = 0
            while time.monotonic() < t_end and n <= 0:
                n = dev.data_avail()
                time.sleep(0.05)
            if n <= 0:
                raise MedaqError("opened, but no data arrived")
            if report:
                report(f"OK    {itf:<10} {port} baud {bd or 'default'}: data flowing")
            return dev, {"interface": itf, "baud": bd, "port": port}
        except Exception as e:
            msg = f"{itf} {port} baud {bd or 'default'}: {e}"
            errors.append(msg)
            if report:
                report("FAIL  " + msg)
            try:
                dev.close()
            except Exception:
                pass
    raise MedaqError("Could not open the ILD1320. Tried: " + " | ".join(errors)
                     + ". Close sensorTOOL (it holds the port); if it still fails, connect and disconnect once in sensorTOOL, close it, retry.")
