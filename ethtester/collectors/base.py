"""Collector plug-in base class.

A collector is an independent unit of measurement running in its own thread.  To add a new
diagnostic test: subclass Collector, implement setup()/poll()/summary(), and register the class
in collectors/__init__.py (or pass it to Session(extra_collectors=...))."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Dict, Optional

from ..recorder import Event, SEV_INFO, SEV_WARN


class Collector:
    name = "base"
    interval = 1.0           # seconds between poll() calls
    description = ""

    def __init__(self, session):
        self.s = session
        self.cfg = session.cfg
        self.p = session.platform
        self.iface = session.iface
        self.available = True
        self.unavailable_reason: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self.errors = 0

    # ---- helpers -------------------------------------------------------------------
    def emit(self, type_, message, severity=SEV_INFO, data=None, ts=None):
        self.s.emit(Event(type_, message, severity, self.name, data, ts))

    def mark_unavailable(self, reason: str):
        self.available = False
        self.unavailable_reason = reason
        self.emit("METRIC_UNAVAILABLE", "%s: %s" % (self.description or self.name, reason), SEV_WARN)

    # ---- life cycle (override) -------------------------------------------------------
    def enabled(self) -> bool:
        return True

    def setup(self):
        """Check availability, open output files, take initial values."""

    def poll(self):
        """One measurement cycle."""

    def teardown(self):
        """Take final values, close resources."""

    def summary(self) -> Dict:
        return {}

    def snapshot(self) -> Dict:
        """Initial/final information stored in interface_info.json."""
        return {}

    # ---- thread machinery -----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="collector-" + self.name, daemon=True)
        self._thread.start()

    def join(self, timeout=5.0):
        if self._thread:
            self._thread.join(timeout)

    def run_loop(self):
        """Default loop: call poll() on a drift-free monotonic schedule."""
        stop = self.s.stop_event
        nxt = time.monotonic()
        while not stop.is_set():
            self._safe(self.poll)
            nxt += self.interval
            delay = nxt - time.monotonic()
            if delay < -self.interval:  # we fell behind (e.g. system suspend): resync
                nxt = time.monotonic() + self.interval
                delay = self.interval
            stop.wait(max(0.0, delay))

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:  # never let one collector kill the test
            self.errors += 1
            if self.errors <= 20:
                self.emit("COLLECTOR_ERROR", "%s: %s" % (type(e).__name__, e), SEV_WARN,
                          {"traceback": traceback.format_exc(limit=5)})

    def _run(self):
        if not self.available:
            return
        self.run_loop()
