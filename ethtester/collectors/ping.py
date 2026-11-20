"""ICMP reachability / latency monitoring of the default gateway and an optional second target.

Every probe is tagged with the physical link state observed while it was in flight, so packet
loss can be split into 'loss while the link was up' (a layer 2/3 or router problem) and
'loss caused by a physical disconnect'.  Probes that span a system suspend are excluded."""
from __future__ import annotations

import math
import time
from array import array
from collections import deque
from typing import Dict, List, Optional

from ..netinfo import default_gateway, resolve_ipv4
from ..probes import ProberUnavailable
from ..recorder import SEV_CRIT, SEV_INFO, SEV_WARN, Throttle
from ..util import boottime_offset, fmt_duration, iso
from .base import Collector


class PingCollector(Collector):
    description = "ICMP ping"

    def __init__(self, session, label: str, host: Optional[str]):
        super().__init__(session)
        self.label = label
        self.name = "ping_" + label
        self.description = "ICMP ping (%s)" % label
        self.host = host            # None -> auto gateway
        self.dynamic = host is None
        self.ip: Optional[str] = None
        self.interval = self.cfg.ping_interval
        self.timeout = self.cfg.ping_timeout
        self.prober = None
        self.kind = None
        # statistics
        self.sent = self.received = self.timeouts = self.errors = 0
        self.sent_up = self.lost_up = 0
        self.sent_down = self.lost_down = 0
        self.suspended = 0
        self.rtts = array("d")
        self.rtt_min = self.rtt_max = None
        self.rtt_sum = 0.0
        self.rtt_sq = 0.0
        self.jitter_sum = 0.0
        self.jitter_n = 0
        self.last_rtt = None
        self.last_status = None
        self.recent = deque(maxlen=60)
        self.spikes = 0
        # outages
        self.streak = 0
        self.streak_start = None
        self.streak_link_down = False
        self.streak_ldseq = 0
        self.outages: List[Dict] = []
        self.loss_bursts = 0
        self.throttle = Throttle(self.cfg.event_throttle)
        self._gw_check = 0.0

    @property
    def lost(self):
        return self.timeouts + self.errors

    def link(self):
        return self.s.collector("link")

    # ------------------------------------------------------------------ setup
    def setup(self):
        try:
            self.prober = self.p.open_prober("127.0.0.1", self.timeout, self.cfg.ping_method)
        except ProberUnavailable as e:
            self.mark_unavailable("%s. Fix: allow unprivileged ICMP with "
                                  "'sysctl net.ipv4.ping_group_range=\"0 2147483647\"' or give ping "
                                  "cap_net_raw" % e)
            return
        self.kind = self.prober.kind
        if self.kind != "icmp-socket":
            # subprocess ping reports its own timeout; give it some slack
            self.timeout = max(1.0, round(self.timeout)) + 0.5
        self._resolve(initial=True)
        self.csv = self.s.rec.csv("ping_%s.csv" % self.label, [
            "timestamp", "epoch", "seq", "target", "status", "rtt_ms", "link_state", "iperf_active", "detail"])
        self.ocsv = self.s.rec.csv("ping_outages.csv", [
            "target_label", "target", "start", "end", "duration_s", "probes_lost", "link_down_during",
            "classification"])

    def _resolve(self, initial=False):
        if self.dynamic:
            gw = default_gateway(self.p, self.iface)
            if gw and gw != self.ip:
                if self.ip is None:
                    self.emit("GATEWAY", "Default gateway via %s: %s" % (self.iface, gw))
                else:
                    self.emit("GATEWAY_CHANGE", "Default gateway changed %s -> %s" % (self.ip, gw), SEV_WARN)
                self.ip = gw
            elif not gw and initial:
                self.emit("GATEWAY", "No IPv4 default gateway via %s yet; will keep looking" % self.iface,
                          SEV_WARN)
        else:
            ip = resolve_ipv4(self.host)
            if ip and ip != self.ip:
                self.emit("TARGET", "Target %s resolved to %s" % (self.host, ip))
                self.ip = ip
            elif not ip and initial:
                self.emit("TARGET", "Cannot resolve %s yet; will retry" % self.host, SEV_WARN)
        if self.prober and self.ip:
            self.prober.target = self.ip

    # ------------------------------------------------------------------ main loop
    def run_loop(self):
        stop = self.s.stop_event
        pending: Dict[int, Dict] = {}
        seq = 0
        nxt = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now - self._gw_check >= (10.0 if self.ip else 2.0):
                    self._gw_check = now
                    self._safe(self._resolve)
                if now >= nxt:
                    if now - nxt > 3 * self.interval:
                        nxt = now  # resync after a stall/suspend
                    nxt += self.interval
                    if self.ip:
                        seq += 1
                        lk = self.link()
                        ctx = {"mono": now, "ts": time.time(), "boot": boottime_offset(), "target": self.ip,
                               "up": bool(lk and lk.is_up), "ldseq": lk.link_down_seq if lk else 0,
                               "iperf": self.s.iperf_active}
                        pending[seq] = ctx
                        for r in self.prober.send(seq):
                            self._finish(pending, *r)
                now = time.monotonic()
                for sq in [s for s, c in pending.items() if now - c["mono"] >= self.timeout]:
                    self.prober.forget(sq)
                    self._finish(pending, sq, "timeout", None, "no reply within %.1fs" % self.timeout)
                wake = nxt
                if pending:
                    wake = min(wake, min(c["mono"] for c in pending.values()) + self.timeout)
                for r in self.prober.poll(min(0.5, max(0.0, wake - time.monotonic()))):
                    self._finish(pending, *r)
        finally:
            self.prober.close()

    def _finish(self, pending, seq, status, rtt, detail):
        ctx = pending.pop(seq, None)
        if ctx is None:
            return
        self._safe(lambda: self._account(seq, ctx, status, rtt, detail))

    def _account(self, seq, ctx, status, rtt, detail):
        lk = self.link()
        if boottime_offset() - ctx["boot"] > 0.5:
            status, detail = "suspended", "system was suspended during probe (excluded)"
        link_ok = ctx["up"] and bool(lk and lk.is_up) and (lk.link_down_seq if lk else 0) == ctx["ldseq"]
        link_state = "UP" if link_ok else "DOWN"
        self.csv.write([iso(ctx["ts"]), "%.3f" % ctx["ts"], seq, ctx["target"], status,
                        "%.3f" % rtt if rtt is not None else None, link_state, int(ctx["iperf"]), detail])
        if status == "suspended":
            self.suspended += 1
            return
        self.sent += 1
        if link_ok:
            self.sent_up += 1
        else:
            self.sent_down += 1
        self.last_status = status
        if status == "ok":
            self.received += 1
            self.recent.append(True)
            self.rtts.append(rtt)
            self.rtt_sum += rtt
            self.rtt_sq += rtt * rtt
            self.rtt_min = rtt if self.rtt_min is None else min(self.rtt_min, rtt)
            self.rtt_max = rtt if self.rtt_max is None else max(self.rtt_max, rtt)
            if self.last_rtt is not None:
                self.jitter_sum += abs(rtt - self.last_rtt)
                self.jitter_n += 1
            self.last_rtt = rtt
            if rtt >= self.cfg.latency_warn_ms:
                self.spikes += 1
                ok, sup = self.throttle.allow("spike")
                if ok:
                    self.emit("LATENCY_SPIKE", "%s %s: %.1f ms%s%s" % (
                        self.label, ctx["target"], rtt, " (iperf3 running)" if ctx["iperf"] else "",
                        " (+%d more suppressed)" % sup if sup else ""), SEV_INFO)
            self._streak_end(ctx["ts"])
        else:
            if status == "timeout":
                self.timeouts += 1
            else:
                self.errors += 1
            self.recent.append(False)
            if link_ok:
                self.lost_up += 1
            else:
                self.lost_down += 1
            self._streak_fail(ctx, link_ok, detail)

    def _streak_fail(self, ctx, link_ok, detail):
        lk = self.link()
        if self.streak == 0:
            self.streak_start = ctx["ts"]
            self.streak_link_down = not link_ok
            self.streak_ldseq = ctx["ldseq"]
            self.streak_detail = detail
        else:
            self.streak_link_down |= not link_ok
        self.streak += 1
        if self.streak == self.cfg.outage_threshold:
            cause = "LINK DOWN (physical disconnect)" if self.streak_link_down else \
                "link UP -> packet loss WITHOUT physical disconnect"
            self.emit("PING_OUTAGE_START", "%s %s unreachable (%d consecutive losses, %s; %s)" % (
                self.label, ctx["target"], self.streak, cause, detail), SEV_CRIT)

    def _streak_end(self, ts, ongoing=False):
        if self.streak == 0:
            return
        lk = self.link()
        link_down = self.streak_link_down or (lk is not None and lk.link_down_seq != self.streak_ldseq)
        cls = "link_down" if link_down else "loss_with_link_up"
        if self.streak >= self.cfg.outage_threshold:
            dur = ts - self.streak_start
            o = {"start": self.streak_start, "end": None if ongoing else ts, "duration_s": dur,
                 "lost": self.streak, "link_down_during": link_down, "classification": cls}
            self.outages.append(o)
            self.ocsv.write([self.label, self.ip, iso(self.streak_start), "ongoing" if ongoing else iso(ts),
                             "%.3f" % dur, self.streak, int(link_down), cls])
            self.emit("PING_OUTAGE_END" if not ongoing else "PING_OUTAGE_ONGOING",
                      "%s %s unreachable for %s (%d probes lost, %s)" % (
                          self.label, self.ip, fmt_duration(dur), self.streak,
                          "link went DOWN" if link_down else "link stayed UP"),
                      SEV_CRIT if not link_down else SEV_WARN, {k: v for k, v in o.items()})
        else:
            self.loss_bursts += 1
            ok, sup = self.throttle.allow("loss" + cls)
            if ok:
                self.emit("PING_LOSS", "%s %s: %d probe(s) lost (%s)%s" % (
                    self.label, self.ip, self.streak,
                    "link went DOWN" if link_down else "link stayed UP",
                    " (+%d more bursts suppressed)" % sup if sup else ""), SEV_WARN)
        self.streak = 0

    def teardown(self):
        if self.available and self.streak:
            self._streak_end(time.time(), ongoing=self.streak >= self.cfg.outage_threshold)

    # ------------------------------------------------------------------ results
    def stats(self, percentiles: bool = True) -> Dict:
        """percentiles=False is cheap enough to call every UI refresh."""
        n = len(self.rtts)
        avg = self.rtt_sum / n if n else None
        mdev = math.sqrt(max(0.0, self.rtt_sq / n - avg * avg)) if n else None
        srt = sorted(self.rtts) if (n and percentiles) else None

        def pct(p):
            if not srt:
                return None
            return srt[min(n - 1, max(0, int(math.ceil(p / 100.0 * n)) - 1))]

        return {"sent": self.sent, "received": self.received, "lost": self.lost,
                "timeouts": self.timeouts, "send_errors": self.errors,
                "loss_pct": round(100.0 * self.lost / self.sent, 4) if self.sent else None,
                "sent_while_link_up": self.sent_up, "lost_while_link_up": self.lost_up,
                "loss_pct_while_link_up": round(100.0 * self.lost_up / self.sent_up, 4) if self.sent_up else None,
                "sent_while_link_down": self.sent_down, "lost_while_link_down": self.lost_down,
                "excluded_suspend": self.suspended,
                "rtt_min_ms": self.rtt_min, "rtt_avg_ms": avg, "rtt_max_ms": self.rtt_max,
                "rtt_mdev_ms": mdev, "rtt_p50_ms": pct(50), "rtt_p95_ms": pct(95), "rtt_p99_ms": pct(99),
                "jitter_ms": self.jitter_sum / self.jitter_n if self.jitter_n else None,
                "latency_spikes": self.spikes, "latency_spike_threshold_ms": self.cfg.latency_warn_ms,
                "loss_bursts_short": self.loss_bursts}

    def summary(self) -> Dict:
        if not self.available:
            return {"available": False, "reason": self.unavailable_reason}
        st = self.stats()
        if self.sent == 0 and self.ip is None:
            st["note"] = "target never resolved / no default gateway on %s" % self.iface
        outs = self.outages
        st.update({
            "available": True, "label": self.label, "target": self.ip, "configured": self.host or "auto (gateway)",
            "method": self.kind, "interval_s": self.interval, "timeout_s": self.timeout,
            "outage_threshold_probes": self.cfg.outage_threshold,
            "outages": len(outs),
            "outages_with_link_up": sum(1 for o in outs if not o["link_down_during"]),
            "outages_with_link_down": sum(1 for o in outs if o["link_down_during"]),
            "unreachable_seconds": round(sum(o["duration_s"] for o in outs), 3),
            "longest_outage_s": round(max((o["duration_s"] for o in outs), default=0.0), 3) if outs else None,
            "outage_list": [{"start": iso(o["start"]), "end": iso(o["end"]) if o["end"] else "ongoing",
                             "duration_s": round(o["duration_s"], 3), "probes_lost": o["lost"],
                             "classification": o["classification"]} for o in outs[-200:]],
        })
        for k, v in list(st.items()):
            if isinstance(v, float):
                st[k] = round(v, 3)
        return st
