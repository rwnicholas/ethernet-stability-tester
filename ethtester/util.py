"""Small shared helpers: time formatting, subprocess execution, parsing of durations."""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import time
from typing import List, Optional

NA = "N/A"


def iso(ts: Optional[float] = None) -> str:
    """Local ISO-8601 timestamp with milliseconds and UTC offset."""
    if ts is None:
        ts = time.time()
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def short_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def boottime_offset() -> float:
    """CLOCK_BOOTTIME - CLOCK_MONOTONIC.  This grows only while the system is suspended,
    which lets us detect sleep/resume (very common on a Steam Deck)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return 0.0


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return NA
    seconds = int(round(seconds))
    days, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    base = "%02d:%02d:%02d" % (h, m, s)
    return ("%dd %s" % (days, base)) if days else base


def fmt_rate(bps: Optional[float]) -> str:
    """Bits per second -> human string."""
    if bps is None:
        return NA
    for unit, div in (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3)):
        if bps >= div:
            return "%.2f %s" % (bps / div, unit)
    return "%.0f bit/s" % bps


def fmt_num(v, fmt: str = "%s") -> str:
    if v is None:
        return NA
    if isinstance(v, str):
        return v
    return fmt % v


_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd]?)", re.I)


def parse_duration(text: str) -> float:
    """'90' -> 90s, '15m' -> 900, '1h30m' -> 5400, '1d' -> 86400."""
    text = str(text).strip().lower()
    if not text:
        raise ValueError("empty duration")
    pos = 0
    total = 0.0
    for m in _DUR_RE.finditer(text):
        if text[pos:m.start()].strip():
            raise ValueError("invalid duration: %r" % text)
        mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        total += float(m.group(1)) * mult
        pos = m.end()
    if text[pos:].strip() or pos == 0:
        raise ValueError("invalid duration: %r" % text)
    return total


def c_env() -> dict:
    """Environment forcing the C locale so tool output is parseable (e.g. ping in pt_BR)."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = "C"
    return env


class CmdResult:
    __slots__ = ("cmd", "rc", "out", "err", "missing", "timed_out")

    def __init__(self, cmd, rc=None, out="", err="", missing=False, timed_out=False):
        self.cmd = cmd
        self.rc = rc
        self.out = out
        self.err = err
        self.missing = missing
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.missing and not self.timed_out

    def error_text(self) -> str:
        if self.missing:
            return "command not found: %s" % self.cmd[0]
        if self.timed_out:
            return "timed out"
        msg = (self.err or self.out or "").strip().splitlines()
        return msg[-1] if msg else "exit code %s" % self.rc


def find_tool(tool: str) -> Optional[str]:
    """Find binary in system PATH, including standard sbin directories common on SteamOS/Linux."""
    path = os.environ.get("PATH", "")
    extended_path = path + ":/usr/local/sbin:/usr/sbin:/sbin"
    return shutil.which(tool, path=extended_path) or shutil.which(tool)


def run_cmd(cmd: List[str], timeout: float = 10.0) -> CmdResult:
    bin_path = find_tool(cmd[0])
    if bin_path is None:
        return CmdResult(cmd, missing=True)
    full_cmd = [bin_path] + cmd[1:]
    try:
        p = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           timeout=timeout, env=c_env(), stdin=subprocess.DEVNULL)
        return CmdResult(cmd, p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return CmdResult(cmd, timed_out=True)
    except OSError as e:
        return CmdResult(cmd, rc=-1, err=str(e))


def to_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
