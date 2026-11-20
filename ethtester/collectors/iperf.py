"""Optional traffic load test with iperf3 (TCP/UDP throughput, UDP jitter and loss)."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from ..recorder import SEV_INFO, SEV_WARN
from ..util import fmt_rate, iso
from .base import Collector

TESTS = {
    "tcp-up": ("tcp", False), "tcp-down": ("tcp", True),
    "udp-up": ("udp", False), "udp-down": ("udp", True),
}


def parse_iperf_json(text: str, proto: str) -> Dict:
    try:
        j = json.loads(text)
    except (ValueError, TypeError):
        return {"error": "invalid iperf3 JSON output"}
    if j.get("error"):
        return {"error": j["error"]}
    end = j.get("end") or {}
    r: Dict = {"throughput_bps": None, "sender_bps": None, "retransmits": None, "jitter_ms": None,
               "lost_packets": None, "packets": None, "lost_percent": None, "error": None}
    if proto == "tcp":
        sent, recv = end.get("sum_sent") or {}, end.get("sum_received") or {}
        r["throughput_bps"] = recv.get("bits_per_second", sent.get("bits_per_second"))
        r["sender_bps"] = sent.get("bits_per_second")
        r["retransmits"] = sent.get("retransmits")
    else:
        s = end.get("sum_received") or end.get("sum") or {}
        r["throughput_bps"] = s.get("bits_per_second")
        r["sender_bps"] = (end.get("sum_sent") or end.get("sum") or {}).get("bits_per_second")
        r["jitter_ms"] = s.get("jitter_ms")
        r["lost_packets"] = s.get("lost_packets")
        r["packets"] = s.get("packets")
        r["lost_percent"] = s.get("lost_percent")
    if r["throughput_bps"] is None:
        r["error"] = "no summary in iperf3 output"
    return r


class IperfCollector(Collector):
    name = "iperf3"
    description = "iperf3 load test"

    def __init__(self, session):
        super().__init__(session)
        self.server = self.cfg.iperf_server
        self.results: List[Dict] = []
        self.runs = 0
        self.last: Dict[str, Dict] = {}
        self.request = threading.Event()
        self.running_test: Optional[str] = None

    def enabled(self):
        return bool(self.server)

    def setup(self):
        if not self.p.which("iperf3"):
            self.mark_unavailable("iperf3 not installed (load testing disabled; basic monitoring unaffected)")
            return
        bad = [t for t in self.cfg.iperf_tests if t not in TESTS]
        if bad:
            self.mark_unavailable("unknown iperf3 test(s): %s" % ", ".join(bad))
            return
        self.csv = self.s.rec.csv("iperf3.csv", [
            "timestamp", "epoch", "run", "test", "protocol", "direction", "duration_s", "throughput_bps",
            "sender_bps", "retransmits", "jitter_ms", "lost_packets", "packets", "lost_percent", "link_state",
            "error"])
        os.makedirs(self.s.rec.path("iperf3_raw"), exist_ok=True)

    def run_loop(self):
        stop = self.s.stop_event
        if stop.wait(self.cfg.iperf_start_delay) and stop.is_set():
            return
        while not stop.is_set():
            self._safe(self.run_once)
            if self.cfg.iperf_interval <= 0:
                # only on demand from now on
                while not stop.is_set() and not self.request.wait(1.0):
                    pass
            else:
                deadline = time.monotonic() + self.cfg.iperf_interval
                while not stop.is_set() and time.monotonic() < deadline:
                    if self.request.wait(1.0):
                        break
            self.request.clear()

    def run_once(self):
        self.runs += 1
        run = self.runs
        self.emit("IPERF_START", "iperf3 run #%d against %s (%s)" % (run, self.server, ",".join(self.cfg.iperf_tests)))
        for t in self.cfg.iperf_tests:
            if self.s.stop_event.is_set():
                break
            proto, reverse = TESTS[t]
            cmd = ["iperf3", "-c", self.server, "-p", str(self.cfg.iperf_port), "-t", str(self.cfg.iperf_duration),
                   "-J"]
            if reverse:
                cmd.append("-R")
            if proto == "udp":
                cmd += ["-u", "-b", self.cfg.iperf_udp_bitrate]
            self.running_test = t
            self.s.iperf_active = True
            t0 = time.time()
            try:
                r = self.p.run(cmd, timeout=self.cfg.iperf_duration + 30)
            finally:
                self.s.iperf_active = False
                self.running_test = None
            raw = r.out or r.err or ""
            with open(self.s.rec.path("iperf3_raw/run%03d_%s.json" % (run, t)), "w") as f:
                f.write(raw)
            if r.missing or r.timed_out or not r.out.strip():
                res = {"error": r.error_text()}
            else:
                res = parse_iperf_json(r.out, proto)
            lk = self.s.collector("link")
            row = dict(res, ts=t0, run=run, test=t, protocol=proto,
                       direction="download" if reverse else "upload",
                       link_state=lk.state if lk else None)
            self.results.append(row)
            self.last[t] = row
            self.csv.write([iso(t0), "%.3f" % t0, run, t, proto, row["direction"], self.cfg.iperf_duration,
                            res.get("throughput_bps"), res.get("sender_bps"), res.get("retransmits"),
                            res.get("jitter_ms"), res.get("lost_packets"), res.get("packets"),
                            res.get("lost_percent"), row["link_state"], res.get("error")])
            if res.get("error"):
                self.emit("IPERF_ERROR", "%s: %s" % (t, res["error"]), SEV_WARN)
            else:
                extra = ""
                if proto == "udp":
                    extra = ", jitter %s ms, loss %s%%" % (
                        "%.3f" % res["jitter_ms"] if res.get("jitter_ms") is not None else "N/A",
                        "%.3f" % res["lost_percent"] if res.get("lost_percent") is not None else "N/A")
                elif res.get("retransmits") is not None:
                    extra = ", %d retransmits" % res["retransmits"]
                self.emit("IPERF_RESULT", "%s: %s%s" % (t, fmt_rate(res["throughput_bps"]), extra))
            if self.s.stop_event.wait(2):
                break

    def summary(self) -> Dict:
        if not self.enabled():
            return {"available": False, "reason": "not configured (use --iperf-server)"}
        if not self.available:
            return {"available": False, "reason": self.unavailable_reason}
        per = {}
        for t in self.cfg.iperf_tests:
            rows = [r for r in self.results if r["test"] == t and not r.get("error")]
            thr = [r["throughput_bps"] for r in rows]
            d = {"runs_ok": len(rows), "runs_failed": sum(1 for r in self.results if r["test"] == t and r.get("error")),
                 "throughput_min_bps": min(thr) if thr else None,
                 "throughput_avg_bps": sum(thr) / len(thr) if thr else None,
                 "throughput_max_bps": max(thr) if thr else None}
            if TESTS[t][0] == "udp":
                j = [r["jitter_ms"] for r in rows if r.get("jitter_ms") is not None]
                lp = [r["lost_percent"] for r in rows if r.get("lost_percent") is not None]
                d.update(jitter_avg_ms=sum(j) / len(j) if j else None, jitter_max_ms=max(j) if j else None,
                         loss_avg_pct=sum(lp) / len(lp) if lp else None, loss_max_pct=max(lp) if lp else None)
            else:
                rt = [r["retransmits"] for r in rows if r.get("retransmits") is not None]
                d["retransmits_total"] = sum(rt) if rt else None
            per[t] = d
        return {"available": True, "server": self.server, "runs": self.runs, "tests": per}
