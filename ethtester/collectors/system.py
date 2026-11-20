"""Housekeeping: suspend/resume detection, routing sanity checks, self overhead, periodic
flushing of data and summary to disk."""
from __future__ import annotations

import os
import resource
import time
from typing import Dict, Optional

from ..netinfo import read_routes, route_iface
from ..recorder import SEV_CRIT, SEV_INFO, SEV_WARN
from ..util import boottime_offset, fmt_duration
from .base import Collector


class SystemCollector(Collector):
    name = "system"
    description = "System housekeeping"
    interval = 1.0

    def __init__(self, session):
        super().__init__(session)
        self.boot0 = boottime_offset()
        self.boot_last = self.boot0
        self.suspends = []
        self.route_state: Dict[str, Optional[str]] = {}
        self._next_route = 0.0
        self._next_sync = 0.0
        self._next_summary = 0.0
        self.cpu_pct: Optional[float] = None
        self._cpu_last = None
        self.max_rss_mb: Optional[float] = None
        self.cpu_samples = []

    def setup(self):
        others = sorted({r["iface"] for r in read_routes(self.p)
                         if r["dst"] == "0.0.0.0" and r["mask"] == "0.0.0.0" and r["iface"] != self.iface})
        if others:
            self.emit("ROUTE_WARNING", "Other interfaces also have a default route: %s. For a clean test "
                      "disable Wi-Fi so all traffic uses %s." % (", ".join(others), self.iface), SEV_WARN)
        self._next_route = time.monotonic() + 5
        self._next_summary = time.monotonic() + self.cfg.summary_interval

    def _cpu(self):
        ru = resource.getrusage(resource.RUSAGE_SELF)
        rc = resource.getrusage(resource.RUSAGE_CHILDREN)
        t = ru.ru_utime + ru.ru_stime + rc.ru_utime + rc.ru_stime
        now = time.monotonic()
        if self._cpu_last:
            dt = now - self._cpu_last[1]
            if dt > 0:
                self.cpu_pct = 100.0 * (t - self._cpu_last[0]) / dt
                self.cpu_samples.append(self.cpu_pct)
                if len(self.cpu_samples) > 3600:
                    self.cpu_samples = self.cpu_samples[-3600:]
        self._cpu_last = (t, now)
        self.max_rss_mb = ru.ru_maxrss / 1024.0

    def poll(self):
        b = boottime_offset()
        if b - self.boot_last > 2.0:
            dur = b - self.boot_last
            self.suspends.append({"resumed": time.time(), "duration_s": dur})
            self.emit("SYSTEM_SUSPEND", "System was suspended for %s; link/ping events around this time are "
                      "caused by sleep, not by the network" % fmt_duration(dur), SEV_CRIT, {"duration_s": dur})
        self.boot_last = b
        mono = time.monotonic()
        if mono >= self._next_route:
            self._next_route = mono + self.cfg.route_check_interval
            self._check_routes()
        if int(mono) % 5 == 0:
            self._cpu()
        if mono >= self._next_sync:
            self._next_sync = mono + self.cfg.sync_interval
            self.s.rec.sync()
        if mono >= self._next_summary:
            self._next_summary = mono + self.cfg.summary_interval
            self.s.write_summary(final=False)

    def _check_routes(self):
        for c in self.s.ping_collectors():
            if not c.ip:
                continue
            dev = route_iface(self.p, c.ip)
            if dev == self.route_state.get(c.ip, "<unset>"):
                continue
            self.route_state[c.ip] = dev
            if dev and dev != self.iface:
                self.emit("ROUTE_WARNING", "Traffic to %s (%s) is routed via %s, NOT via %s. Results for this "
                          "target do not reflect the Ethernet link." % (c.ip, c.label, dev, self.iface), SEV_CRIT)
            elif dev == self.iface:
                self.emit("ROUTE_OK", "%s (%s) is routed via %s" % (c.ip, c.label, dev))

    def summary(self) -> Dict:
        cpu_avg = sum(self.cpu_samples) / len(self.cpu_samples) if self.cpu_samples else None
        return {"suspends": len(self.suspends), "suspended_seconds": round(sum(s["duration_s"] for s in self.suspends), 1),
                "tester_cpu_avg_pct": round(cpu_avg, 3) if cpu_avg is not None else None,
                "tester_max_rss_mb": round(self.max_rss_mb, 1) if self.max_rss_mb else None,
                "routes": self.route_state}
