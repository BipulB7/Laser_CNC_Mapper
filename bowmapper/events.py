"""In-process event bus: threads publish, each browser connection subscribes."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque


class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.history: deque[dict] = deque(maxlen=600)   # log + notes, replayed to new tabs

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data: dict | None = None, keep: bool = False) -> None:
        msg = {"t": kind, "ts": time.time(), **(data or {})}
        if keep:
            self.history.append(msg)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def note(self, level: str, text: str) -> None:
        """A message for the operator. level: info | ok | warn | error."""
        self.publish("note", {"level": level, "text": text}, keep=True)

    def log(self, direction: str, line: str) -> None:
        self.publish("log", {"dir": direction, "line": line}, keep=True)
