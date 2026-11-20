"""Test session: owns configuration, collectors, recorder and the report life cycle."""
from __future__ import annotations

import dataclasses
import os
import platform as pyplatform
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import __version__
from .collectors import build_collectors
from .netinfo import Ethtool, default_gateway, hardware_info, iface_addresses, read_routes
from .recorder import Event, Recorder, SEV_CRIT, SEV_INFO, SEV_WARN
from .util import fmt_duration, iso


@dataclass
class Config:
    interface: Optional[str] = None
    label: str = ""
    duration: Optional[float] = None         # seconds, None = until stopped
    output_dir: str = "reports"
    gateway: Optional[str] = None            # override auto-detected gateway
    target2: Optional[str] = None
    no_ping: bool = False
    ping_interval: float = 1.0
    ping_timeout: float = 1.0
    ping_method: str = "auto"                # auto | socket | command
    outage_threshold: int = 3                # consecutive lost probes = unreachable period
    latency_warn_ms: float = 100.0
    link_poll_interval: float = 0.25
    link_csv_interval: float = 1.0
    ethtool_link_interval: float = 30.0
    stats_interval: float = 1.0
    stats_csv_interval: float = 5.0
    ethtool_stats_interval: float = 10.0
    kernel_log: bool = True
    kernel_history_lines: int = 500
    iperf_server: Optional[str] = None
    iperf_port: int = 5201
    iperf_interval: float = 1800.0
    iperf_duration: int = 10
    iperf_start_delay: float = 15.0
    iperf_tests: List[str] = field(default_factory=lambda: ["tcp-up", "tcp-down", "udp-up", "udp-down"])
    iperf_udp_bitrate: str = "100M"
    event_throttle: float = 10.0
    summary_interval: float = 60.0
    sync_interval: float = 30.0
    route_check_interval: float = 30.0
    inhibit_sleep: bool = True
    simulate: bool = False
    ui: str = "auto"                          # auto | tui | plain


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


class Session:
    def __init__(self, cfg: Config, platform, iface: str, extra_collectors=()):
        self.cfg = cfg
        self.platform = platform
        self.iface = iface
        self.stop_event = threading.Event()
        self.iperf_active = False
        self.start_ts: Optional[float] = None
        self.start_mono: Optional[float] = None
        self.end_ts: Optional[float] = None
        self.end_reason = ""
        self.finalized = False
        self._summary_lock = threading.Lock()
        self.inhibitor = None
        self.inhibit_status = "disabled"
        self.markers = 0
        self.hw = hardware_info(platform, iface)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = "%s_%s%s" % (stamp, _safe_name(iface), ("_" + _safe_name(cfg.label)) if cfg.label else "")
        self.report_dir = os.path.abspath(os.path.join(cfg.output_dir, name))
        self.rec = Recorder(self.report_dir)
        self.collectors = build_collectors(self) + [c(self) for c in extra_collectors]
        self.initial_info: Dict = {}

    # ------------------------------------------------------------------ helpers
    def collector(self, name: str):
        for c in self.collectors:
            if c.name == name:
                return c
        return None

    def ping_collectors(self):
        return [c for c in self.collectors if c.name.startswith("ping_") and c.available]

    def emit(self, ev: Event):
        self.rec.emit(ev)

    def info(self, msg, sev=SEV_INFO, type_="INFO"):
        self.emit(Event(type_, msg, sev, "session"))

    def elapsed(self) -> float:
        if self.start_mono is None:
            return 0.0
        return time.monotonic() - self.start_mono

    def remaining(self) -> Optional[float]:
        if self.cfg.duration is None:
            return None
        return max(0.0, self.cfg.duration - self.elapsed())

    def add_marker(self, text: str = ""):
        self.markers += 1
        self.info("User marker #%d%s" % (self.markers, (": " + text) if text else ""), SEV_INFO, "USER_MARKER")

    def request_iperf(self) -> bool:
        c = self.collector("iperf3")
        if c and c.enabled() and c.available:
            c.request.set()
            return True
        return False

    # ------------------------------------------------------------------ snapshots
    def interface_snapshot(self) -> Dict:
        p, i = self.platform, self.iface
        et = Ethtool(p, i)
        settings, s_err = et.settings()
        eee, e_err = et.eee()
        pause, p_err = et.pause()
        drv, d_err = et.driver_info()
        snap = {
            "timestamp": iso(),
            "present": p.iface_exists(i),
            "hardware": hardware_info(p, i),
            "addresses": iface_addresses(p, i),
            "gateway": default_gateway(p, i),
            "routes_ipv4": read_routes(p),
            "ethtool_settings": settings or "N/A (%s)" % s_err,
            "ethtool_driver_info": drv or "N/A (%s)" % d_err,
            "eee": eee or "N/A (%s)" % e_err,
            "pause": pause or "N/A (%s)" % p_err,
        }
        for c in self.collectors:
            try:
                s = c.snapshot() if c.available else None
            except Exception as e:  # pragma: no cover
                s = "error: %s" % e
            if s:
                snap[c.name] = s
        dc = self.collector("driver_stats")
        if dc is not None and dc.available:
            snap["driver_stats"] = dc.final_raw if self.finalized or dc.final_raw else dc.initial_raw
        return snap

    def host_info(self) -> Dict:
        osr = {}
        txt = self.platform.read_path("/etc/os-release") or ""
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                osr[k] = v.strip('"')
        u = os.uname()
        tools = {t: self.platform.which(t) for t in ("ethtool", "ping", "iperf3", "journalctl", "dmesg", "ip",
                                                     "udevadm", "lspci", "systemd-inhibit")}
        return {"hostname": socket.gethostname(), "kernel": u.release, "machine": u.machine,
                "os": osr.get("PRETTY_NAME") or osr.get("NAME"), "os_id": osr.get("ID"),
                "python": pyplatform.python_version(), "uid": os.getuid(), "running_as_root": os.getuid() == 0,
                "platform_backend": self.platform.name, "tools_available": tools}

    # ------------------------------------------------------------------ life cycle
    def start(self):
        self.start_ts = time.time()
        self.start_mono = time.monotonic()
        self.info("Ethernet Stability Tester %s started on %s (%s %s, driver %s)%s" % (
            __version__, self.iface, self.hw.get("vendor") or "?", self.hw.get("model") or "?",
            self.hw.get("driver") or "?", (" label=%s" % self.cfg.label) if self.cfg.label else ""),
            type_="TEST_START")
        if self.platform.name == "simulated":
            self.info("SIMULATION MODE: all data is synthetic", SEV_WARN)
        if self.cfg.inhibit_sleep:
            self.inhibitor, err = self.platform.start_sleep_inhibitor()
            if self.inhibitor:
                self.inhibit_status = "active"
                self.info("System sleep inhibited for the duration of the test")
            else:
                self.inhibit_status = "failed: %s" % err
                self.info("Could not inhibit system sleep (%s). Disable sleep manually for long tests." % err,
                          SEV_WARN)
        for c in self.collectors:
            if not c.enabled():
                c.available = False
                c.unavailable_reason = "not enabled"
                continue
            try:
                c.setup()
            except Exception as e:
                c.mark_unavailable("setup failed: %s: %s" % (type(e).__name__, e))
        self.initial_info = self.interface_snapshot()
        self._write_info(final=None)
        for c in self.collectors:
            if c.available:
                c.start()
        self.write_summary(final=False)

    def stop(self, reason: str = "stopped by user"):
        if not self.stop_event.is_set():
            self.end_reason = reason
            self.stop_event.set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout*; returns True when the session should stop."""
        rem = self.remaining()
        if rem is not None and rem <= 0:
            self.stop("planned duration reached")
        if self.stop_event.wait(min(timeout, rem) if rem is not None else timeout):
            return True
        rem = self.remaining()
        if rem is not None and rem <= 0:
            self.stop("planned duration reached")
            return True
        return False

    def finalize(self):
        if self.finalized:
            return
        self.stop(self.end_reason or "stopped")
        for c in self.collectors:
            c.join(timeout=self.cfg.ping_timeout + 3)
        self.end_ts = time.time()
        for c in self.collectors:
            if c.available:
                try:
                    c.teardown()
                except Exception as e:
                    self.info("teardown of %s failed: %s" % (c.name, e), SEV_WARN, "COLLECTOR_ERROR")
        self.finalized = True
        final = self.interface_snapshot()
        self._write_info(final=final)
        self.info("Test finished after %s (%s)" % (fmt_duration(self.end_ts - self.start_ts), self.end_reason),
                  type_="TEST_END")
        self.write_summary(final=True)
        if self.inhibitor:
            try:
                self.inhibitor.terminate()
                self.inhibitor.wait(timeout=3)
            except Exception:
                pass
        self.rec.close()

    # ------------------------------------------------------------------ output
    def config_dict(self) -> Dict:
        return dataclasses.asdict(self.cfg)

    def counter_deltas(self) -> Dict:
        out = {}
        c = self.collector("counters")
        if c and c.available:
            out["kernel_statistics"] = c.summary().get("counters")
        d = self.collector("driver_stats")
        if d and d.available:
            out["ethtool_driver"] = d.cs_driver.report()
            out["ethtool_standard"] = d.cs_std.report()
        lk = self.collector("link")
        if lk:
            out["link_counters"] = {k: {"initial": lk.counters_init.get(k), "final": lk.counters_last.get(k),
                                        "delta": lk.counters_accum.get(k)} for k in lk.COUNTERS}
            out["link_counters"]["ethtool_link_down_events"] = {
                "initial": lk.ld_events_init, "final": lk.ld_events_last,
                "delta": (lk.ld_events_last - lk.ld_events_init)
                if lk.ld_events_init is not None and lk.ld_events_last is not None else None}
        return out

    def _write_info(self, final: Optional[Dict]):
        doc = {
            "tool": {"name": "Ethernet Stability Tester", "version": __version__},
            "report_directory": self.report_dir,
            "label": self.cfg.label,
            "interface": self.iface,
            "test_start": iso(self.start_ts),
            "test_end": iso(self.end_ts) if self.end_ts else None,
            "host": self.host_info(),
            "configuration": self.config_dict(),
            "sleep_inhibitor": self.inhibit_status,
            "metric_availability": {c.name: ("available" if c.available else "N/A: %s" % c.unavailable_reason)
                                    for c in self.collectors},
            "initial": self.initial_info,
            "final": final,
            "counter_deltas": self.counter_deltas() if final else None,
        }
        self.rec.write_json("interface_info.json", doc)

    def write_summary(self, final: bool):
        from .report import build_summary, render_summary
        with self._summary_lock:
            if self.rec.events_csv.f.closed:
                return
            s = build_summary(self, final)
            self.rec.write_json("summary.json", s)
            self.rec.write_text("summary.txt", render_summary(s))
