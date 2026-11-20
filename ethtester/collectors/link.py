"""Physical link monitoring: carrier, operstate, speed, duplex, autoneg, flap detection."""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..netinfo import Ethtool
from ..recorder import SEV_CRIT, SEV_INFO, SEV_WARN
from ..util import fmt_duration, iso, to_int
from .base import Collector


class LinkCollector(Collector):
    name = "link"
    description = "Physical link"

    def __init__(self, session):
        super().__init__(session)
        self.interval = self.cfg.link_poll_interval
        self.et = Ethtool(self.p, self.iface)
        self.state: Optional[str] = None       # UP / DOWN / ABSENT
        self.state_since = time.time()
        self.speed: Optional[int] = None
        self.duplex: Optional[str] = None
        self.operstate: Optional[str] = None
        self.autoneg: Optional[str] = None
        self.partner_modes = None
        self.ethtool_info: Dict = {}
        self.ethtool_error = ""
        self.last_up_speed: Optional[int] = None
        self.last_up_duplex: Optional[str] = None
        self.disconnects_observed = 0      # transitions UP -> not UP seen by polling
        self.flaps_missed = 0              # extra carrier changes too short to be seen by polling
        self.speed_changes: List[Dict] = []
        self.duplex_changes: List[Dict] = []
        self.down_periods: List[Dict] = []
        self.down_start: Optional[float] = None
        self.time_by_state: Dict[str, float] = {}
        self.counters_init: Dict[str, Optional[int]] = {}
        self.counters_last: Dict[str, Optional[int]] = {}
        self.counters_accum: Dict[str, int] = {}
        self.ld_events_init: Optional[int] = None
        self.ld_events_last: Optional[int] = None
        self._last_csv = 0.0
        self._last_poll_mono: Optional[float] = None
        self._next_ethtool = 0.0
        self.link_down_seq = 0   # incremented each time the link is (or was) down; used by ping

    COUNTERS = ("carrier_changes", "carrier_up_count", "carrier_down_count")

    # ------------------------------------------------------------------ sampling
    def _sample(self) -> Dict:
        p, i = self.p, self.iface
        if not p.iface_exists(i):
            return {"present": False}
        carrier = to_int(p.read(i, "carrier"))
        flags = p.read(i, "flags")
        admin_up = None
        if flags:
            try:
                admin_up = bool(int(flags, 16) & 0x1)
            except ValueError:
                pass
        speed = to_int(p.read(i, "speed"))
        if speed is not None and speed <= 0:
            speed = None
        duplex = p.read(i, "duplex")
        if duplex in (None, "", "unknown"):
            duplex = None
        d = {"present": True, "carrier": carrier, "operstate": p.read(i, "operstate"), "admin_up": admin_up,
             "speed": speed, "duplex": duplex}
        for c in self.COUNTERS:
            d[c] = to_int(p.read(i, c))
        return d

    def setup(self):
        s = self._sample()
        if not s["present"]:
            self.emit("LINK_ABSENT", "Interface %s not present at start" % self.iface, SEV_CRIT)
        self.csv = self.s.rec.csv("link_state.csv", [
            "timestamp", "epoch", "link", "operstate", "carrier", "admin_up", "speed_mbps", "duplex",
            "autoneg", "carrier_changes", "carrier_up_count", "carrier_down_count", "ethtool_link_down_events",
            "event"])
        for c in self.COUNTERS:
            v = s.get(c)
            self.counters_init[c] = v
            self.counters_last[c] = v
            self.counters_accum[c] = 0 if v is not None else None
        self._refresh_ethtool(initial=True)
        self.ld_events_init = self.ethtool_info.get("link_down_events")
        self.ld_events_last = self.ld_events_init
        self._apply(s, initial=True)

    # ------------------------------------------------------------------ ethtool
    def _refresh_ethtool(self, initial=False):
        info, err = self.et.settings()
        self.ethtool_error = err
        if not info:
            return
        old = self.ethtool_info
        self.ethtool_info = info
        if info.get("link_down_events") is not None:
            self.ld_events_last = info["link_down_events"]
            if self.ld_events_init is None:
                self.ld_events_init = info["link_down_events"]
        if initial:
            self.autoneg = info.get("autoneg")
            self.partner_modes = info.get("partner_advertised_modes")
            return
        if info.get("autoneg") != self.autoneg and info.get("autoneg") is not None:
            self.emit("AUTONEG_CHANGE", "Auto-negotiation %s -> %s" % (self.autoneg, info.get("autoneg")), SEV_WARN)
            self.autoneg = info.get("autoneg")
        pm = info.get("partner_advertised_modes")
        if pm is not None and self.partner_modes is not None and pm != self.partner_modes and self.state == "UP":
            self.emit("PARTNER_MODES_CHANGE", "Link partner advertised modes changed: %s -> %s"
                      % (" ".join(self.partner_modes) or "none", " ".join(pm) or "none"), SEV_WARN)
        if pm is not None and (self.state == "UP" or self.partner_modes is None):
            self.partner_modes = pm
        if old.get("advertised_modes") is not None and info.get("advertised_modes") != old.get("advertised_modes"):
            self.emit("ADVERTISED_MODES_CHANGE", "Local advertised modes changed: %s -> %s"
                      % (" ".join(old.get("advertised_modes") or []), " ".join(info.get("advertised_modes") or [])),
                      SEV_WARN)

    # ------------------------------------------------------------------ state machine
    def _apply(self, s: Dict, initial=False):
        now = time.time()
        mono = time.monotonic()
        if self._last_poll_mono is not None and self.state is not None:
            key = self.state if self.state != "UP" else "UP@%s/%s" % (self.speed or "?", self.duplex or "?")
            self.time_by_state[key] = self.time_by_state.get(key, 0.0) + (mono - self._last_poll_mono)
        self._last_poll_mono = mono

        if not s["present"]:
            new = "ABSENT"
        elif s["carrier"] == 1:
            new = "UP"
        else:
            new = "DOWN"
        event = ""

        # --- kernel counters, missed flaps, counter resets
        cc_delta = None
        if s["present"]:
            for c in self.COUNTERS:
                v, last = s.get(c), self.counters_last.get(c)
                if v is not None and last is not None:
                    if v >= last:
                        if c == "carrier_changes":
                            cc_delta = v - last
                        self.counters_accum[c] = (self.counters_accum.get(c) or 0) + (v - last)
                    else:
                        self.emit("COUNTER_RESET", "%s went backwards (%s -> %s): interface re-created?"
                                  % (c, last, v), SEV_WARN)
                if v is not None:
                    self.counters_last[c] = v
                    if self.counters_init.get(c) is None:
                        self.counters_init[c] = v
                        self.counters_accum[c] = 0

        prev = self.state
        changed = prev is not None and new != prev
        if not initial and cc_delta is not None and prev in ("UP", "DOWN") and new in ("UP", "DOWN"):
            expected = 1 if changed else 0
            extra = cc_delta - expected
            if extra >= 2:
                missed = extra // 2
                self.flaps_missed += missed
                self.link_down_seq += 1
                event = "FLAP_MISSED"
                self.emit("LINK_FLAP", "%d very short link flap(s) detected via carrier_changes (+%d) "
                          "between polls (< %.2fs)" % (missed, cc_delta, self.interval), SEV_CRIT,
                          {"carrier_changes_delta": cc_delta})

        if initial:
            self.state = new
            self.state_since = now
            if new != "UP":
                self.down_start = now
                self.link_down_seq += 1
                self.emit("LINK_DOWN", "Link is %s at test start" % new, SEV_CRIT)
            event = "INITIAL"
        elif changed:
            dur = now - self.state_since
            if prev == "UP":
                self.disconnects_observed += 1
                self.link_down_seq += 1
                self.down_start = now
                self.emit("LINK_DOWN" if new == "DOWN" else "IFACE_REMOVED",
                          "Link %s -> %s after %s up (disconnect #%d)" % (prev, new, fmt_duration(dur),
                                                                        self.disconnects_observed), SEV_CRIT,
                          {"up_duration_s": round(dur, 3)})
            elif new == "UP":
                start = self.down_start or self.state_since
                down = now - start
                self.down_periods.append({"start": start, "end": now, "duration_s": down, "state": prev})
                self.s.rec.csv("link_down_periods.csv", ["start", "end", "duration_s", "state_while_down"]) \
                    .write([iso(start), iso(now), "%.3f" % down, prev])
                self.down_start = None
                self.emit("LINK_UP", "Link %s -> UP after %s down%s" % (
                    prev, fmt_duration(down) if down >= 1 else "%.2fs" % down,
                    (" at %s Mb/s %s" % (s.get("speed") or "?", s.get("duplex") or "?"))), SEV_WARN,
                    {"down_duration_s": round(down, 3)})
                self._next_ethtool = mono + 2.0  # re-read negotiation results soon
            else:  # DOWN <-> ABSENT
                self.emit("IFACE_REMOVED" if new == "ABSENT" else "IFACE_ADDED",
                          "Interface state %s -> %s" % (prev, new), SEV_CRIT)
            self.state = new
            self.state_since = now
            event = "%s->%s" % (prev, new)

        # --- speed / duplex
        self.operstate = s.get("operstate")
        if new == "UP":
            sp, du = s.get("speed"), s.get("duplex")
            if sp is not None:
                if self.last_up_speed is not None and sp != self.last_up_speed:
                    rec = {"ts": now, "from": self.last_up_speed, "to": sp}
                    self.speed_changes.append(rec)
                    self.emit("SPEED_CHANGE", "Link speed %s -> %s Mb/s" % (self.last_up_speed, sp),
                              SEV_CRIT if sp < self.last_up_speed else SEV_WARN, rec)
                    event = (event + " SPEED").strip()
                self.last_up_speed = sp
            if du is not None:
                if self.last_up_duplex is not None and du != self.last_up_duplex:
                    rec = {"ts": now, "from": self.last_up_duplex, "to": du}
                    self.duplex_changes.append(rec)
                    self.emit("DUPLEX_CHANGE", "Duplex %s -> %s" % (self.last_up_duplex, du), SEV_CRIT, rec)
                    event = (event + " DUPLEX").strip()
                self.last_up_duplex = du
            self.speed, self.duplex = sp, du
        else:
            self.speed, self.duplex = None, None

        # --- csv: once per second and on every change
        if event or now - self._last_csv >= self.cfg.link_csv_interval:
            self._last_csv = now
            self.csv.write([iso(now), "%.3f" % now, new, s.get("operstate"), s.get("carrier"),
                            s.get("admin_up"), self.speed, self.duplex, self.autoneg,
                            s.get("carrier_changes"), s.get("carrier_up_count"), s.get("carrier_down_count"),
                            self.ld_events_last, event])

    def poll(self):
        s = self._sample()
        self._apply(s)
        mono = time.monotonic()
        if mono >= self._next_ethtool:
            self._next_ethtool = mono + self.cfg.ethtool_link_interval
            if s.get("present"):
                self._refresh_ethtool()

    # ------------------------------------------------------------------ results
    @property
    def is_up(self) -> Optional[bool]:
        return None if self.state is None else self.state == "UP"

    def kernel_link_downs(self) -> Optional[int]:
        return self.counters_accum.get("carrier_down_count")

    def total_disconnects(self) -> int:
        """Best estimate of physical link drops during the test."""
        k = self.kernel_link_downs()
        observed = self.disconnects_observed + self.flaps_missed
        return max(k, observed) if k is not None else observed

    def teardown(self):
        if self.p.iface_exists(self.iface):
            self._refresh_ethtool()
        if self.state != "UP" and self.down_start:
            now = time.time()
            self.down_periods.append({"start": self.down_start, "end": None,
                                      "duration_s": now - self.down_start, "state": self.state,
                                      "ongoing_at_end": True})
        self._apply(self._sample())

    def summary(self) -> Dict:
        total = sum(self.time_by_state.values()) or 0.0
        up = sum(v for k, v in self.time_by_state.items() if k.startswith("UP"))
        ld = None
        if self.ld_events_init is not None and self.ld_events_last is not None:
            ld = self.ld_events_last - self.ld_events_init
        return {
            "final_state": self.state,
            "disconnects_total_estimate": self.total_disconnects(),
            "disconnects_observed_by_polling": self.disconnects_observed,
            "short_flaps_between_polls": self.flaps_missed,
            "kernel_carrier_down_count_delta": self.kernel_link_downs(),
            "kernel_carrier_changes_delta": self.counters_accum.get("carrier_changes"),
            "ethtool_link_down_events_delta": ld,
            "speed_changes": len(self.speed_changes),
            "speed_change_list": [{"time": iso(c["ts"]), "from_mbps": c["from"], "to_mbps": c["to"]}
                                  for c in self.speed_changes],
            "duplex_changes": len(self.duplex_changes),
            "duplex_change_list": [{"time": iso(c["ts"]), "from": c["from"], "to": c["to"]}
                                   for c in self.duplex_changes],
            "link_up_seconds": round(up, 1),
            "link_down_seconds": round(total - up, 1),
            "link_availability_pct": round(100.0 * up / total, 4) if total else None,
            "time_by_state_seconds": {k: round(v, 1) for k, v in sorted(self.time_by_state.items())},
            "longest_down_s": round(max((d["duration_s"] for d in self.down_periods), default=0.0), 3)
            if self.down_periods else None,
            "down_periods": [{"start": iso(d["start"]), "end": iso(d["end"]) if d.get("end") else None,
                              "duration_s": round(d["duration_s"], 3), "state": d["state"]}
                             for d in self.down_periods[-200:]],
        }

    def snapshot(self) -> Dict:
        s = self._sample()
        return {"sysfs": s, "ethtool": self.ethtool_info or None,
                "ethtool_error": self.ethtool_error or None}
