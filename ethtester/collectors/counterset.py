"""Monotonic counter bookkeeping with reset detection (driver reload / USB re-plug)."""
from __future__ import annotations

from typing import Dict, Optional


class CounterSet:
    def __init__(self):
        self.initial: Dict[str, Optional[int]] = {}
        self.last: Dict[str, Optional[int]] = {}
        self.accum: Dict[str, int] = {}
        self.resets: Dict[str, int] = {}

    def update(self, values: Dict[str, Optional[int]]) -> Dict[str, Optional[int]]:
        """Feed new absolute values; returns per-key delta since the previous update.
        Delta is None when the counter is unavailable.  A counter that decreases is treated as
        reset to 0 (the new value is counted as the delta)."""
        deltas: Dict[str, Optional[int]] = {}
        for k, v in values.items():
            if v is None:
                deltas[k] = None
                if k not in self.initial:
                    self.initial[k] = None
                continue
            if self.initial.get(k) is None:
                self.initial[k] = v
                self.last[k] = v
                self.accum[k] = 0
                deltas[k] = 0
                continue
            last = self.last.get(k)
            if last is None:
                d = 0
            elif v >= last:
                d = v - last
            else:
                self.resets[k] = self.resets.get(k, 0) + 1
                d = v
            self.accum[k] = self.accum.get(k, 0) + d
            self.last[k] = v
            deltas[k] = d
        return deltas

    def total(self, k: str) -> Optional[int]:
        """Increase over the whole test (None = unavailable)."""
        return self.accum.get(k) if self.initial.get(k) is not None else None

    def report(self) -> Dict[str, Dict]:
        out = {}
        for k in self.initial:
            out[k] = {"initial": self.initial.get(k), "final": self.last.get(k) if self.initial.get(k) is not None
                      else None, "delta": self.total(k), "resets": self.resets.get(k, 0)}
        return out
