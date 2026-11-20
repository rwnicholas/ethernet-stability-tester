"""Interface statistics: generic kernel counters (/sys/class/net/<if>/statistics) and
driver-specific / IEEE standard counters (ethtool -S)."""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional

from ..netinfo import Ethtool
from ..recorder import SEV_INFO, SEV_WARN, SEV_CRIT, Throttle
from ..util import iso, to_int
from .base import Collector
from .counterset import CounterSet

SYSFS_COUNTERS = [
    "rx_packets", "tx_packets", "rx_bytes", "tx_bytes",
    "rx_errors", "tx_errors", "rx_dropped", "tx_dropped",
    "rx_crc_errors", "rx_frame_errors", "rx_length_errors", "rx_over_errors", "rx_fifo_errors",
    "rx_missed_errors", "rx_nohandler", "multicast", "collisions",
    "tx_carrier_errors", "tx_aborted_errors", "tx_fifo_errors", "tx_heartbeat_errors", "tx_window_errors",
]
TRAFFIC = {"rx_packets", "tx_packets", "rx_bytes", "tx_bytes", "multicast"}
# rx_dropped / rx_nohandler are incremented by the stack for frames nobody wants (unknown
# protocols, LLDP...), so they are informational rather than a hardware fault indicator.
SOFT = {"rx_dropped", "rx_nohandler", "tx_dropped"}

# ethtool driver/standard stat names that corroborate a generic counter is really populated
VERIFY = {
    "rx_crc_errors": r"crc|fcs|FrameCheckSequenceErrors",
    "rx_frame_errors": r"align|frame_err|AlignmentErrors",
    "tx_carrier_errors": r"carrier|CarrierSenseErrors",
    "rx_missed_errors": r"miss",
    "rx_length_errors": r"length|oversize|undersize|LengthErrors|jabber",
    "rx_fifo_errors": r"fifo|overrun|overflow",
    "rx_over_errors": r"over",
    "collisions": r"collision",
    "rx_errors": r"rx_errors|rx_err",
    "tx_errors": r"tx_errors|tx_err",
}

ERRORISH = re.compile(r"err|crc|fcs|drop|miss|fifo|collision|carrier|align|symbol|fail|lost|timeout|"
                      r"reset|restart|overrun|underrun|jabber|undersize|oversize|discard|abort|hang|"
                      r"invalid|bad|no_buf|nobuf|alloc|busy|fragment", re.I)
NOT_ERROR = re.compile(r"_ok$|good|bytes$|packets$|_pkts$|xon|xoff|pause", re.I)


class InterfaceStatsCollector(Collector):
    name = "counters"
    description = "Interface RX/TX statistics (/sys/class/net)"

    def __init__(self, session):
        super().__init__(session)
        self.interval = self.cfg.stats_interval
        self.cs = CounterSet()
        self.present: Dict[str, bool] = {}
        self.throttle = Throttle(self.cfg.event_throttle)
        self.pending: Dict[str, int] = {}
        self.rx_bps: Optional[float] = None
        self.tx_bps: Optional[float] = None
        self._last_mono = None
        self._last_csv = 0.0
        self._interval_deltas: Dict[str, int] = {}

    def _read(self) -> Dict[str, Optional[int]]:
        out = {}
        for c in SYSFS_COUNTERS:
            v = self.p.read(self.iface, "statistics/" + c)
            out[c] = to_int(v)
        return out

    def setup(self):
        vals = self._read()
        self.present = {k: v is not None for k, v in vals.items()}
        if not any(self.present.values()):
            self.mark_unavailable("statistics not readable for %s" % self.iface)
            return
        missing = [k for k, ok in self.present.items() if not ok]
        if missing:
            self.emit("METRIC_UNAVAILABLE", "Kernel counters not provided: %s" % ", ".join(missing), SEV_INFO)
        self.cs.update(vals)
        self._last_mono = time.monotonic()
        self.csv = self.s.rec.csv("interface_stats.csv", ["timestamp", "epoch", "rx_bps", "tx_bps"] +
                                  SYSFS_COUNTERS + ["d_" + c for c in SYSFS_COUNTERS])

    def poll(self):
        vals = self._read()
        now, mono = time.time(), time.monotonic()
        if all(v is None for v in vals.values()):
            return  # interface absent (unplugged) - the link collector reports that
        deltas = self.cs.update(vals)
        dt = mono - self._last_mono if self._last_mono else None
        self._last_mono = mono
        if dt and dt > 0:
            rb, tb = deltas.get("rx_bytes"), deltas.get("tx_bytes")
            self.rx_bps = rb * 8 / dt if rb is not None else None
            self.tx_bps = tb * 8 / dt if tb is not None else None
        for k, d in deltas.items():
            if d:
                self._interval_deltas[k] = self._interval_deltas.get(k, 0) + d
                if k not in TRAFFIC:
                    self.pending[k] = self.pending.get(k, 0) + d
        if self.pending:
            hard = {k: v for k, v in self.pending.items() if k not in SOFT}
            key = "hard" if hard else "soft"
            ok, sup = self.throttle.allow(key)
            if ok:
                parts = ", ".join("%s +%d (total %s)" % (k, v, self.cs.total(k)) for k, v in sorted(self.pending.items()))
                self.emit("IFACE_ERRORS" if hard else "IFACE_DROPS", parts, SEV_WARN if hard else SEV_INFO,
                          dict(self.pending))
                self.pending = {}
        if now - self._last_csv >= self.cfg.stats_csv_interval:
            self._last_csv = now
            self.csv.write([iso(now), "%.3f" % now,
                            None if self.rx_bps is None else round(self.rx_bps),
                            None if self.tx_bps is None else round(self.tx_bps)] +
                           [vals.get(c) for c in SYSFS_COUNTERS] +
                           [self._interval_deltas.get(c, 0) if vals.get(c) is not None else None
                            for c in SYSFS_COUNTERS])
            self._interval_deltas = {}

    def teardown(self):
        if self.available:
            self.poll()
            if self.pending:
                self.emit("IFACE_ERRORS", ", ".join("%s +%d" % kv for kv in sorted(self.pending.items())), SEV_WARN)

    def verification(self) -> Dict[str, str]:
        """For each generic counter: 'driver-confirmed', 'generic' (kernel field exists but the
        driver may never populate it) or 'N/A'."""
        keys = []
        ds = self.s.collector("driver_stats")
        if ds is not None:
            keys = list((ds.cs_driver.initial or {}).keys()) + list((ds.cs_std.initial or {}).keys())
        out = {}
        for c in SYSFS_COUNTERS:
            if not self.present.get(c):
                out[c] = "N/A"
            elif c in TRAFFIC or c in SOFT:
                out[c] = "kernel"
            elif any(re.search(VERIFY.get(c, re.escape(c)), k, re.I) for k in keys):
                out[c] = "driver-confirmed"
            else:
                out[c] = "generic (driver support unverified)"
        return out

    def summary(self) -> Dict:
        if not self.available:
            return {"available": False, "reason": self.unavailable_reason}
        rep = self.cs.report()
        ver = self.verification()
        for k in rep:
            rep[k]["support"] = ver.get(k)
        return {"available": True, "counters": rep}

    def snapshot(self) -> Dict:
        return {k: v for k, v in self._read().items()}


class DriverStatsCollector(Collector):
    name = "driver_stats"
    description = "Driver statistics (ethtool -S)"

    def __init__(self, session):
        super().__init__(session)
        self.interval = self.cfg.ethtool_stats_interval
        self.et = Ethtool(self.p, self.iface)
        self.cs_driver = CounterSet()
        self.cs_std = CounterSet()
        self.driver_ok = False
        self.std_ok = False
        self.driver_err = ""
        self.std_err = ""
        self.throttle = Throttle(self.cfg.event_throttle)
        self.pending: Dict[str, int] = {}
        self.columns: List[str] = []
        self.initial_raw: Dict = {}
        self.final_raw: Dict = {}

    def _fetch(self):
        d, derr = self.et.driver_stats()
        st, serr = self.et.standard_stats() if self.std_ok or not self.columns else (None, self.std_err)
        return d, derr, st, serr

    def setup(self):
        d, self.driver_err = self.et.driver_stats()
        st, self.std_err = self.et.standard_stats()
        self.driver_ok, self.std_ok = d is not None, st is not None
        if not self.driver_ok and not self.std_ok:
            self.mark_unavailable("Driver statistics: %s" % (self.driver_err or "not supported by driver"))
            return
        if not self.std_ok:
            self.emit("METRIC_UNAVAILABLE", "IEEE standard statistics (ethtool -S --all-groups): %s" % self.std_err)
        if not self.driver_ok:
            self.emit("METRIC_UNAVAILABLE", "Driver statistics: %s" % self.driver_err, SEV_WARN)
        d, st = d or {}, st or {}
        self.initial_raw = {"driver": d or None, "standard": st or None}
        self.cs_driver.update(d)
        self.cs_std.update(st)
        self.columns = sorted(d) + ["std." + k for k in sorted(st)]
        self.csv = self.s.rec.csv("driver_stats.csv", ["timestamp", "epoch"] + self.columns)
        self._write(d, st)

    def _write(self, d, st):
        now = time.time()
        self.csv.write([iso(now), "%.3f" % now] +
                       [d.get(c) if not c.startswith("std.") else st.get(c[4:]) for c in self.columns])

    def poll(self):
        d, derr = self.et.driver_stats() if self.driver_ok else (None, self.driver_err)
        st, serr = self.et.standard_stats() if self.std_ok else (None, self.std_err)
        if d is None and st is None:
            return  # transient (interface absent); keep going
        d, st = d or {}, st or {}
        self.final_raw = {"driver": d or None, "standard": st or None}
        changes = {}
        for prefix, cs, vals in (("", self.cs_driver, d), ("std.", self.cs_std, st)):
            for k, delta in cs.update(vals).items():
                if delta and ERRORISH.search(k) and not NOT_ERROR.search(k):
                    changes[prefix + k] = delta
        for k, v in changes.items():
            self.pending[k] = self.pending.get(k, 0) + v
        if self.pending:
            ok, _ = self.throttle.allow("drv")
            if ok:
                self.emit("DRIVER_COUNTERS", ", ".join("%s +%d" % kv for kv in sorted(self.pending.items())),
                          SEV_WARN, dict(self.pending))
                self.pending = {}
        self._write(d, st)

    def teardown(self):
        if self.available:
            self.poll()
            if self.pending:
                self.emit("DRIVER_COUNTERS", ", ".join("%s +%d" % kv for kv in sorted(self.pending.items())),
                          SEV_WARN)

    def changed_error_counters(self) -> Dict[str, int]:
        out = {}
        for prefix, cs in (("", self.cs_driver), ("std.", self.cs_std)):
            for k in cs.initial:
                t = cs.total(k)
                if t and ERRORISH.search(k) and not NOT_ERROR.search(k):
                    out[prefix + k] = t
        return out

    def summary(self) -> Dict:
        if not self.available:
            return {"available": False, "reason": self.unavailable_reason}
        return {"available": True,
                "driver_stats_available": self.driver_ok,
                "driver_stats_error": None if self.driver_ok else self.driver_err,
                "standard_stats_available": self.std_ok,
                "standard_stats_error": None if self.std_ok else self.std_err,
                "error_counters_increased": self.changed_error_counters(),
                "driver": self.cs_driver.report(), "standard": self.cs_std.report()}
