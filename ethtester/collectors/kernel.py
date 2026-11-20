"""Kernel log messages related to the monitored adapter (journalctl -k -f, or dmesg -w)."""
from __future__ import annotations

import re
import threading
import time
from typing import Dict, List

from ..recorder import SEV_CRIT, SEV_INFO, SEV_WARN, Throttle
from .base import Collector

SEVERE = re.compile(r"tx (unit )?hang|timeout|watchdog|reset|fail|error|AER|corrected|uncorrect|"
                    r"disconnect|firmware|PCIe Bus Error|link is down|link down|NETDEV", re.I)
LINKMSG = re.compile(r"link (is )?(up|down)|renamed|carrier", re.I)


class KernelLogCollector(Collector):
    name = "kernel"
    description = "Kernel messages"

    def __init__(self, session):
        super().__init__(session)
        self.source = None
        self.follower = None
        self.pattern = None
        self.matches = 0
        self.severe = 0
        self.throttle = Throttle(self.cfg.event_throttle)
        self.history_matches: List[str] = []

    def _build_pattern(self):
        hw = self.s.hw or {}
        terms = [self.iface]
        if hw.get("bus_info"):
            terms.append(hw["bus_info"])
        if hw.get("usb_port"):
            terms.append("usb " + hw["usb_port"])
        drv = hw.get("driver")
        if drv:
            terms.append(drv)
        parts = [r"(?<![\w-])%s(?![\w-])" % re.escape(t) for t in terms if t]
        return re.compile("|".join(parts), re.I)

    def setup(self):
        if not self.cfg.kernel_log:
            self.mark_unavailable("disabled by --no-kernel-log")
            return
        self.pattern = self._build_pattern()
        src, lines, err = self.p.kernel_history()
        if src is None:
            self.mark_unavailable("kernel log not readable without privileges (%s). Add your user to "
                                  "the 'systemd-journal' or 'wheel' group, or run with sudo" % err)
            return
        self.source = src
        self.history_matches = [l for l in lines if self.pattern.search(l)]
        sink = self.s.rec.text("kernel_before_start.log")
        for l in self.history_matches[-self.cfg.kernel_history_lines:]:
            sink.line(l)
        self.out = self.s.rec.text("kernel.log")
        try:
            self.follower = self.p.kernel_follow(src)
        except Exception as e:
            self.mark_unavailable("cannot follow kernel log: %s" % e)

    def run_loop(self):
        if not self.follower:
            return
        # stop watcher: closing the follower process ends the iteration below
        def watcher():
            self.s.stop_event.wait()
            self.follower.close()
        threading.Thread(target=watcher, daemon=True, name="kernel-stop").start()
        try:
            for line in self.follower.lines():
                if self.s.stop_event.is_set():
                    break
                if line and self.pattern.search(line):
                    self._safe(lambda l=line: self._handle(l))
        except Exception as e:
            if not self.s.stop_event.is_set():
                self.emit("COLLECTOR_ERROR", "kernel log follower stopped: %s" % e, SEV_WARN)

    def _handle(self, line: str):
        self.matches += 1
        self.out.line(line)
        sev = SEV_INFO
        if SEVERE.search(line):
            sev = SEV_WARN
            self.severe += 1
        msg = line
        # strip journal prefix "2026-01-01T10:00:00.123456-0300 host kernel: "
        m = re.search(r"\bkernel:\s*(.*)$", line)
        if m:
            msg = m.group(1)
        ok, sup = self.throttle.allow("k" + ("L" if LINKMSG.search(msg) else "") + sev)
        if ok:
            self.emit("KERNEL", msg + (" (+%d similar suppressed, see kernel.log)" % sup if sup else ""), sev)

    def teardown(self):
        if self.follower:
            self.follower.close()

    def summary(self) -> Dict:
        if not self.available:
            return {"available": False, "reason": self.unavailable_reason}
        return {"available": True, "source": self.source, "messages_during_test": self.matches,
                "warning_messages_during_test": self.severe,
                "matching_messages_before_start_this_boot": len(self.history_matches)}
