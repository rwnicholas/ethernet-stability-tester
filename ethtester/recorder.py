"""Event model and the report directory writer.

Durability strategy for 4-24 h runs: all files are opened line-buffered (each row reaches the
kernel immediately, so nothing is lost if the program crashes or is killed) and are fsync'ed
periodically so that even a power loss loses at most a few seconds of data.  The summary is
rewritten periodically while the test runs."""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from .util import iso


SEV_INFO, SEV_WARN, SEV_CRIT = "INFO", "WARN", "CRIT"


class Event:
    __slots__ = ("ts", "type", "severity", "source", "message", "data")

    def __init__(self, type_: str, message: str, severity: str = SEV_INFO, source: str = "",
                 data: Optional[Dict[str, Any]] = None, ts: Optional[float] = None):
        self.ts = ts if ts is not None else time.time()
        self.type = type_
        self.severity = severity
        self.source = source
        self.message = message
        self.data = data or {}


class Throttle:
    """Allow at most one event per key per *interval*; count suppressed ones."""

    def __init__(self, interval: float):
        self.interval = interval
        self.last: Dict[str, float] = {}
        self.suppressed: Dict[str, int] = {}

    def allow(self, key: str) -> (bool, int):
        now = time.monotonic()
        if now - self.last.get(key, -1e9) >= self.interval:
            self.last[key] = now
            n = self.suppressed.pop(key, 0)
            return True, n
        self.suppressed[key] = self.suppressed.get(key, 0) + 1
        return False, 0


class CsvSink:
    def __init__(self, path: str, header: List[str]):
        self.path = path
        self.header = list(header)
        self.lock = threading.Lock()
        new = not os.path.exists(path)
        self.f = open(path, "a", newline="", buffering=1, encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.header)
        self.rows = 0

    def write(self, row: List[Any]):
        with self.lock:
            if self.f.closed:
                return
            self.w.writerow(["N/A" if v is None else v for v in row])
            self.rows += 1

    def sync(self):
        with self.lock:
            if not self.f.closed:
                self.f.flush()
                try:
                    os.fsync(self.f.fileno())
                except OSError:
                    pass

    def close(self):
        with self.lock:
            if not self.f.closed:
                self.f.flush()
                try:
                    os.fsync(self.f.fileno())
                except OSError:
                    pass
                self.f.close()


class TextSink(CsvSink):
    def __init__(self, path: str):  # noqa  (no header)
        self.path = path
        self.lock = threading.Lock()
        self.f = open(path, "a", buffering=1, encoding="utf-8")
        self.rows = 0

    def line(self, text: str):
        with self.lock:
            if not self.f.closed:
                self.f.write(text + "\n")
                self.rows += 1


class Recorder:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.sinks: Dict[str, CsvSink] = {}
        self.lock = threading.Lock()
        self.events_csv = self.csv("events.csv", ["timestamp", "epoch", "severity", "type", "source",
                                                   "message", "data_json"])
        self.events_log = self.text("events.log")
        self.recent = deque(maxlen=1000)
        self.counts: Dict[str, int] = {}
        self.listeners = []

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def csv(self, name: str, header: List[str]) -> CsvSink:
        with self.lock:
            if name not in self.sinks:
                os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
                self.sinks[name] = CsvSink(self.path(name), header)
            return self.sinks[name]

    def text(self, name: str) -> TextSink:
        with self.lock:
            if name not in self.sinks:
                self.sinks[name] = TextSink(self.path(name))
            return self.sinks[name]

    def write_json(self, name: str, obj: Any):
        """Atomic write (tmp + rename) so a crash never leaves a truncated JSON."""
        tmp = self.path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path(name))

    def write_text(self, name: str, text: str):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path(name))

    def emit(self, ev: Event):
        with self.lock:
            self.counts[ev.type] = self.counts.get(ev.type, 0) + 1
            self.recent.append(ev)
        self.events_csv.write([iso(ev.ts), "%.3f" % ev.ts, ev.severity, ev.type, ev.source, ev.message,
                               json.dumps(ev.data, default=str) if ev.data else ""])
        self.events_log.line("%s [%s] %-18s %s%s" % (iso(ev.ts), ev.severity, ev.type,
                                                     ("(%s) " % ev.source) if ev.source else "", ev.message))
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:
                pass

    def sync(self):
        for s in list(self.sinks.values()):
            s.sync()

    def close(self):
        for s in list(self.sinks.values()):
            s.close()
