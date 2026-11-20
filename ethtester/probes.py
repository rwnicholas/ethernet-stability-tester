"""ICMP probing back-ends.

Preferred: unprivileged ICMP "ping" sockets (SOCK_DGRAM / IPPROTO_ICMP).  They need no root,
only that the user's group is inside net.ipv4.ping_group_range (systemd default allows everybody,
which is the case on SteamOS).  Timing is taken from CLOCK_MONOTONIC in-process.

Fallback: the system `ping` binary, one process per probe (setuid / file capabilities).

Probers are asynchronous:  send(seq) starts a probe, poll(wait) returns finished results as a
list of (seq, status, rtt_ms, detail) with status in {"ok", "timeout", "error"}.
The ping collector is responsible for declaring a probe lost after its timeout.
"""
from __future__ import annotations

import errno
import os
import re
import select
import socket
import struct
import subprocess
import time
from typing import List, Optional, Tuple

from .util import c_env

Result = Tuple[int, str, Optional[float], str]


class ProberUnavailable(Exception):
    pass


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


class IcmpSocketProber:
    kind = "icmp-socket"

    def __init__(self, target: str, timeout: float):
        self.target = target
        self.timeout = timeout
        self.sock = None
        self.token = os.urandom(8)
        self.sent = {}  # seq16 -> (seq, mono)

    def open(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except OSError as e:
            raise ProberUnavailable("ICMP datagram socket not permitted (%s)" % e.strerror)
        self.sock.setblocking(False)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, seq: int) -> List[Result]:
        s16 = seq & 0xFFFF
        payload = self.token + struct.pack("!Q", seq) + b"ethtester-probe" + b"\0" * 25
        hdr = struct.pack("!BBHHH", 8, 0, 0, 0, s16)
        pkt = hdr[:2] + struct.pack("!H", _checksum(hdr + payload)) + hdr[4:] + payload
        now = time.monotonic()
        try:
            self.sock.sendto(pkt, (self.target, 0))
        except OSError as e:
            return [(seq, "error", None, "send: %s" % (e.strerror or e))]
        self.sent[s16] = (seq, now)
        return []

    def forget(self, seq: int):
        self.sent.pop(seq & 0xFFFF, None)

    def poll(self, wait: float) -> List[Result]:
        out: List[Result] = []
        wait = max(0.0, wait)
        try:
            r, _, _ = select.select([self.sock], [], [], wait)
        except (OSError, ValueError):
            time.sleep(wait)
            return out
        if not r:
            return out
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                break
            now = time.monotonic()
            if len(data) < 8:
                continue
            typ, code, _ck, _id, s16 = struct.unpack("!BBHHH", data[:8])
            if typ != 0 or data[8:16] != self.token:
                continue
            ent = self.sent.pop(s16, None)
            if ent is None:
                continue
            seq, t0 = ent
            out.append((seq, "ok", (now - t0) * 1000.0, ""))
        return out


_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


class PingCmdProber:
    """One `ping -c 1` process per probe.  Higher overhead, used only as fallback."""
    kind = "ping-command"

    def __init__(self, target: str, timeout: float):
        self.target = target
        self.timeout = timeout
        self.procs = {}  # seq -> Popen

    def open(self):
        # verify that ping is usable at all (permission problems are reported immediately)
        try:
            p = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", "127.0.0.1"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=5, env=c_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ProberUnavailable("ping command unusable: %s" % e)
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip().splitlines()
            raise ProberUnavailable("ping command unusable: %s" % (msg[-1] if msg else p.returncode))

    def close(self):
        for p in self.procs.values():
            try:
                p.kill()
            except OSError:
                pass
        self.procs.clear()

    def send(self, seq: int) -> List[Result]:
        w = max(1, int(round(self.timeout)))
        try:
            self.procs[seq] = subprocess.Popen(["ping", "-n", "-c", "1", "-W", str(w), self.target],
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, env=c_env(), stdin=subprocess.DEVNULL)
        except OSError as e:
            return [(seq, "error", None, "spawn: %s" % e)]
        return []

    def forget(self, seq: int):
        p = self.procs.pop(seq, None)
        if p:
            try:
                p.kill()
                p.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def poll(self, wait: float) -> List[Result]:
        deadline = time.monotonic() + max(0.0, wait)
        out: List[Result] = []
        while True:
            for seq, p in list(self.procs.items()):
                if p.poll() is not None:
                    text = p.stdout.read() if p.stdout else ""
                    if p.stdout:
                        p.stdout.close()
                    del self.procs[seq]
                    m = _TIME_RE.search(text)
                    if p.returncode == 0 and m:
                        out.append((seq, "ok", float(m.group(1)), ""))
                    elif p.returncode == 1:
                        out.append((seq, "timeout", None, ""))
                    else:
                        lines = [l for l in text.strip().splitlines() if l.strip()]
                        out.append((seq, "error", None, lines[-1] if lines else "rc=%s" % p.returncode))
            if out or time.monotonic() >= deadline:
                return out
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def open_prober(target: str, timeout: float, prefer: str = "auto"):
    """Return an opened prober, trying the ICMP socket first.  Raises ProberUnavailable."""
    errors = []
    order = {"auto": [IcmpSocketProber, PingCmdProber], "socket": [IcmpSocketProber],
             "command": [PingCmdProber]}[prefer]
    for cls in order:
        p = cls(target, timeout)
        try:
            p.open()
            return p
        except ProberUnavailable as e:
            errors.append(str(e))
    raise ProberUnavailable("; ".join(errors))
