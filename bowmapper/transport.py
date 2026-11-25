"""Byte transports to the controller: USB serial, WiFi telnet, or the simulator."""

from __future__ import annotations

import queue
import socket
import time


class TransportError(Exception):
    pass


class SerialTransport:
    kind = "serial"

    def __init__(self, port: str, baud: int = 115200, reset: bool = False):
        import serial  # pyserial

        self.name = port
        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.timeout = 0.05
        s.write_timeout = 2
        if not reset:
            # Holding DTR/RTS low at open stops the ESP32 auto-reset circuit
            # from rebooting the controller (which would also drop homing).
            s.dtr = False
            s.rts = False
        try:
            s.open()
        except Exception as e:
            raise TransportError(f"Cannot open {port}: {e}. Is another program (UGS, sensorTOOL) using it?") from e
        if reset:
            s.rts = True
            time.sleep(0.1)
            s.rts = False
        self.s = s

    def write(self, data: bytes) -> None:
        try:
            self.s.write(data)
        except Exception as e:
            raise TransportError(f"Write failed: {e}") from e

    def read(self) -> bytes:
        try:
            n = self.s.in_waiting
            return self.s.read(n if n else 1)
        except Exception as e:
            raise TransportError(f"Read failed (cable unplugged?): {e}") from e

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass


class TcpTransport:
    kind = "tcp"

    def __init__(self, host: str, port: int = 23):
        self.name = f"{host}:{port}"
        try:
            self.s = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            raise TransportError(f"Cannot reach {host}:{port}: {e}. Joined the GRBL_ESP WiFi?") from e
        self.s.settimeout(0.05)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def write(self, data: bytes) -> None:
        try:
            self.s.sendall(data)
        except OSError as e:
            raise TransportError(f"Write failed: {e}") from e

    def read(self) -> bytes:
        try:
            d = self.s.recv(4096)
        except socket.timeout:
            return b""
        except OSError as e:
            raise TransportError(f"Read failed: {e}") from e
        if not d:
            raise TransportError("Controller closed the connection")
        return d

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass


class SimTransport:
    kind = "sim"

    def __init__(self, sim):
        self.name = "Simulator"
        self.sim = sim
        self._q: queue.Queue[bytes] = queue.Queue()
        sim.attach(self._push)

    def _push(self, data: bytes) -> None:
        self._q.put(data)

    def write(self, data: bytes) -> None:
        self.sim.receive(data)

    def read(self) -> bytes:
        try:
            out = [self._q.get(timeout=0.05)]
        except queue.Empty:
            return b""
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return b"".join(out)

    def close(self) -> None:
        self.sim.attach(None)


def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        vid = p.vid or 0
        guess = ""
        if vid in (0x1A86, 0x10C4):       # CH340 / CP210x: ESP32 controller boards
            guess = "cnc"
        elif vid == 0x0403:               # FTDI: Micro-Epsilon IF2001/USB
            guess = "laser"
        out.append({
            "device": p.device,
            "description": p.description or "",
            "hwid": p.hwid or "",
            "guess": guess,
        })
    return out
