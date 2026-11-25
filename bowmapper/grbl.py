"""Low-level GRBL link: reader thread, '?' poller, character-counting sender.

Every line sent gets a Reply that resolves on its ok / error:N. Lines are
only written while the controller's 128-byte receive buffer has room for
them (character counting), so a program can be streamed at full speed
without overflowing it. A reset banner or a lost link fails every reply
still waiting, so no caller hangs.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from .grbl_codes import alarm_text, error_text
from .transport import TransportError

RX_BUF = 127  # GRBL's serial RX buffer is 128; keep 1 byte margin

RT_HOLD = b"!"
RT_RESUME = b"~"
RT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"
RT_FEED_100, RT_FEED_P10, RT_FEED_M10, RT_FEED_P1, RT_FEED_M1 = b"\x90", b"\x91", b"\x92", b"\x93", b"\x94"
RT_RAPID_100, RT_RAPID_50, RT_RAPID_25 = b"\x95", b"\x96", b"\x97"

_FLOAT_LIST = ("MPos", "WPos", "WCO", "FS", "F", "Ov")


class LinkError(Exception):
    pass


class Cancelled(Exception):
    pass


class Reply:
    __slots__ = ("line", "nbytes", "lines", "done", "ok", "error", "t_sent")

    def __init__(self, line: str):
        self.line = line
        self.nbytes = len(line) + 1
        self.lines: list[str] = []
        self.done = threading.Event()
        self.ok: bool | None = None
        self.error: str | None = None   # "12" for error:12, or "reset" / "link"
        self.t_sent = time.monotonic()

    def wait(self, timeout: float = 10.0) -> "Reply":
        if not self.done.wait(timeout):
            raise TimeoutError(f"No reply to {self.line!r} within {timeout:.0f}s")
        return self

    @property
    def message(self) -> str:
        if self.ok:
            return "ok"
        if self.error and self.error.isdigit():
            return f"error:{self.error} - {error_text(self.error)}"
        if self.error == "reset":
            return "Controller reset before this line ran"
        if self.error == "link":
            return "Connection to the controller was lost"
        return str(self.error)


def parse_status(line: str) -> dict | None:
    if not (line.startswith("<") and line.endswith(">")):
        return None
    body = line[1:-1].split("|")
    name, _, sub = body[0].partition(":")
    st = {"state": name, "sub": sub}
    for field in body[1:]:
        k, _, v = field.partition(":")
        if k in _FLOAT_LIST:
            try:
                st[k] = [float(x) for x in v.split(",")]
            except ValueError:
                continue
        elif k == "Bf":
            try:
                st[k] = [int(x) for x in v.split(",")]
            except ValueError:
                continue
        else:
            st[k] = v
    return st


class GrblController:
    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda kind, data: None)
        self.t = None
        self._wlock = threading.Lock()
        self._q = threading.Condition()
        self.pending: deque[Reply] = deque()
        self._status_cv = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.reset_state()

    def reset_state(self) -> None:
        self.connected = False
        self.status: dict = {"state": "Disconnected", "sub": ""}
        self.mpos = [0.0, 0.0, 0.0]
        self.wpos = [0.0, 0.0, 0.0]
        self.wco = [0.0, 0.0, 0.0]
        self.feed = 0.0
        self.ov = [100, 100, 100]
        self.pins = ""
        self.buffer = None
        self.status_seq = 0
        self.last_status_t = 0.0
        self.banner_seq = 0
        self.last_banner = ""
        self.alarm: int | None = None
        self.firmware = {"version": "", "options": "", "esp32": False}
        self.settings: dict[int, str] = {}
        self.offsets: dict[str, list[float]] = {}
        self._stale = False

    # ------------------------------------------------------------------ link
    def connect(self, transport, status_hz: float = 10.0) -> None:
        if self.connected:
            self.disconnect()
        self.reset_state()
        self._stop.clear()
        self.t = transport
        self.connected = True
        self._spawn(self._reader, "grbl-reader")
        # Is anybody there? Booting ESP32 boards ignore input for ~1-2 s.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and self.status_seq == 0:
            self.realtime(b"?")
            with self._status_cv:
                self._status_cv.wait(0.3)
        if self.status_seq == 0:
            self.disconnect()
            raise LinkError("No answer from the controller. Check the port, baud (115200) and that the CNC is powered.")
        if self.banner_seq:
            time.sleep(0.3)      # fresh boot: let the start-up messages finish
        self.write_raw(b"\n")    # flush any partial line left from a previous session
        time.sleep(0.15)
        self._spawn(lambda: self._poller(status_hz), "grbl-poller")
        self.refresh_info()
        self.on_event("connected", {"name": getattr(transport, "name", "")})

    def _spawn(self, fn, name) -> None:
        th = threading.Thread(target=fn, daemon=True, name=name)
        th.start()
        self._threads.append(th)

    def disconnect(self) -> None:
        was = self.connected
        self._stop.set()
        self.connected = False
        self._fail_all("link")
        if self.t:
            try:
                self.t.close()
            except Exception:
                pass
        self.t = None
        self.status = {"state": "Disconnected", "sub": ""}
        if was:
            self.on_event("disconnected", {"reason": "closed"})

    def refresh_info(self) -> None:
        r = self.send("$I", timeout=4)
        for ln in r.lines:
            if ln.startswith("[VER:"):
                self.firmware["version"] = ln[5:-1].rstrip(":")
            elif ln.startswith("[OPT:"):
                self.firmware["options"] = ln[5:-1]
        self.firmware["esp32"] = self.firmware["version"].startswith("1.3") or "ESP" in " ".join(r.lines).upper()
        self.read_settings()
        self.read_offsets()

    def read_settings(self) -> dict[int, str]:
        r = self.send("$$", timeout=5)
        if not r.ok:
            raise LinkError(f"$$ failed: {r.message}")
        out = {}
        for ln in r.lines:
            m = re.match(r"^\$(\d+)=([^\s(]*)", ln)
            if m:
                out[int(m.group(1))] = m.group(2)
        if out:
            self.settings = out
        return self.settings

    def read_offsets(self) -> dict:
        r = self.send("$#", timeout=4)
        for ln in r.lines:
            m = re.match(r"^\[(G5[4-9]|G28|G30|G92|TLO|PRB):([^\]]*)\]$", ln)
            if m:
                vals = m.group(2).split(":")[0].split(",")
                try:
                    self.offsets[m.group(1)] = [float(v) for v in vals]
                except ValueError:
                    pass
        g54 = self.offsets.get("G54", [0, 0, 0])
        g92 = self.offsets.get("G92", [0, 0, 0])
        tlo = self.offsets.get("TLO", [0])[0]
        self.wco = [g54[0] + g92[0], g54[1] + g92[1], g54[2] + g92[2] + tlo]
        self.wpos = [m - w for m, w in zip(self.mpos, self.wco)]
        return self.offsets

    # ------------------------------------------------------------------ io
    def write_raw(self, data: bytes) -> None:
        if not self.t:
            raise LinkError("Not connected")
        with self._wlock:
            self.t.write(data)

    def realtime(self, b: bytes) -> None:
        try:
            self.write_raw(b)
        except (TransportError, LinkError) as e:
            self._link_lost(e)

    def queue(self, line: str, timeout: float | None = 60.0, cancel=None) -> Reply:
        """Write a line as soon as the RX buffer has room; returns its Reply.

        timeout=None waits indefinitely (a program held for minutes is normal);
        cancel() returning True abandons the wait with Cancelled.
        """
        if not self.connected:
            raise LinkError("Not connected to the controller")
        r = Reply(line)
        if r.nbytes > RX_BUF:
            raise ValueError(f"Line too long ({len(line)} chars): {line[:40]}...")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._q:
            while sum(p.nbytes for p in self.pending) + r.nbytes > RX_BUF:
                if not self.connected:
                    raise LinkError("Not connected to the controller")
                if cancel and cancel():
                    raise Cancelled()
                left = 0.1 if deadline is None else deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Controller stopped accepting lines")
                self._q.wait(min(left, 0.1))
            self.pending.append(r)
            r.t_sent = time.monotonic()
            try:
                with self._wlock:
                    self.t.write((line + "\n").encode())
            except TransportError as e:
                self.pending.remove(r)
                self._link_lost(e)
                raise LinkError(str(e)) from e
        self.on_event("tx", {"line": line})
        return r

    def send(self, line: str, timeout: float = 10.0) -> Reply:
        """Send one line and wait for its reply (never raises on error:N)."""
        r = self.queue(line, timeout)
        return r.wait(timeout)

    def pending_count(self) -> int:
        with self._q:
            return len(self.pending)

    def wait_drained(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._q:
            while self.pending:
                left = deadline - time.monotonic()
                if left <= 0 or not self.connected:
                    return False
                self._q.wait(min(left, 0.1))
        return True

    def wait_status(self, after_seq: int | None = None, timeout: float = 2.0) -> dict:
        """Wait for a status report newer than after_seq (default: now)."""
        seq = self.status_seq if after_seq is None else after_seq
        deadline = time.monotonic() + timeout
        with self._status_cv:
            while self.status_seq <= seq:
                left = deadline - time.monotonic()
                if left <= 0 or not self.connected:
                    raise TimeoutError("No status report from the controller")
                self._status_cv.wait(left)
            return dict(self.status)

    # ------------------------------------------------------------------ threads
    def _reader(self) -> None:
        buf = b""
        t = self.t
        while not self._stop.is_set():
            try:
                data = t.read()
            except TransportError as e:
                self._link_lost(e)
                return
            except Exception as e:  # pragma: no cover - defensive
                self._link_lost(e)
                return
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    try:
                        self._handle(line)
                    except Exception as e:  # never let a bad line kill the reader
                        self.on_event("log", {"dir": "!", "line": f"parse error on {line!r}: {e}"})

    def _poller(self, hz: float) -> None:
        period = 1.0 / max(1.0, hz)
        while not self._stop.is_set():
            self.realtime(b"?")
            if self.last_status_t and time.monotonic() - self.last_status_t > 3.0:
                if not self._stale:
                    self._stale = True
                    self.on_event("stale", {"seconds": round(time.monotonic() - self.last_status_t, 1)})
            self._stop.wait(period)

    def _link_lost(self, err) -> None:
        if not self.connected:
            return
        self.connected = False
        self._stop.set()
        self._fail_all("link")
        self.status = {"state": "Disconnected", "sub": ""}
        try:
            if self.t:
                self.t.close()
        except Exception:
            pass
        self.on_event("disconnected", {"reason": str(err)})

    def _fail_all(self, reason: str) -> None:
        with self._q:
            while self.pending:
                r = self.pending.popleft()
                r.ok = False
                r.error = reason
                r.done.set()
            self._q.notify_all()

    def _resolve(self, ok: bool, err: str | None) -> None:
        with self._q:
            if self.pending:
                r = self.pending.popleft()
                r.ok = ok
                r.error = err
                r.done.set()
            else:
                self.on_event("log", {"dir": "!", "line": "unexpected " + ("ok" if ok else f"error:{err}")})
            self._q.notify_all()

    def _handle(self, line: str) -> None:
        if line.startswith("<"):
            st = parse_status(line)
            if st:
                self._apply_status(st)
            return
        if line == "ok":
            self._resolve(True, None)
            return
        if line.startswith("error:"):
            code = line[6:].strip()
            self._resolve(False, code)
            self.on_event("log", {"dir": "<", "line": f"{line}  ({error_text(code)})"})
            return
        self.on_event("log", {"dir": "<", "line": line})
        if line.startswith("ALARM:"):
            try:
                self.alarm = int(line[6:])
            except ValueError:
                self.alarm = -1
            self.on_event("alarm", {"code": self.alarm, "text": alarm_text(self.alarm)})
            return
        if line.startswith("Grbl ") or line.startswith("Grbl_ESP32"):
            self.banner_seq += 1
            self.last_banner = line
            self._fail_all("reset")
            self.on_event("reset", {"banner": line})
            return
        with self._q:
            if self.pending:
                self.pending[0].lines.append(line)

    def _apply_status(self, st: dict) -> None:
        if "WCO" in st:
            self.wco = st["WCO"][:3]
        if "MPos" in st:
            self.mpos = st["MPos"][:3]
            self.wpos = [m - w for m, w in zip(self.mpos, self.wco)]
        elif "WPos" in st:
            self.wpos = st["WPos"][:3]
            self.mpos = [w + o for w, o in zip(self.wpos, self.wco)]
        if "FS" in st:
            self.feed = st["FS"][0]
        elif "F" in st:
            self.feed = st["F"][0]
        if "Ov" in st:
            self.ov = [int(v) for v in st["Ov"][:3]]
        self.pins = st.get("Pn", "")
        self.buffer = st.get("Bf")
        if st["state"] != "Alarm":
            self.alarm = None
        with self._status_cv:
            self.status = st
            self.status_seq += 1
            self.last_status_t = time.monotonic()
            self._stale = False
            self._status_cv.notify_all()

    # ------------------------------------------------------------------ helpers
    @property
    def state(self) -> str:
        return self.status.get("state", "Disconnected")

    def setting(self, n: int, default: float = 0.0) -> float:
        try:
            return float(self.settings.get(n, default))
        except ValueError:
            return default
