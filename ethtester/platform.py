"""Access to the operating system.  Every collector goes through a Platform object so the whole
application can run against a simulated adapter (see sim.py) for testing.

All operations here are strictly read-only with respect to network configuration."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Iterator, List, Optional, Tuple

from .probes import open_prober
from .util import CmdResult, c_env, run_cmd


class KernelFollower:
    """Wraps a long running `journalctl -f` / `dmesg -w` process."""

    def __init__(self, source: str, proc: subprocess.Popen):
        self.source = source
        self.proc = proc

    def lines(self) -> Iterator[str]:
        for line in self.proc.stdout:
            yield line.rstrip("\n")

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class LinuxPlatform:
    name = "linux"
    sysnet = "/sys/class/net"

    # ---- sysfs / procfs --------------------------------------------------------------
    def list_ifaces(self) -> List[str]:
        try:
            return sorted(os.listdir(self.sysnet))
        except OSError:
            return []

    def iface_exists(self, iface: str) -> bool:
        return os.path.isdir(os.path.join(self.sysnet, iface))

    def read(self, iface: str, rel: str) -> Optional[str]:
        """Read /sys/class/net/<iface>/<rel>.  None if missing or unreadable (e.g. EINVAL for
        speed while the link is down)."""
        try:
            with open(os.path.join(self.sysnet, iface, rel), "r") as f:
                return f.read().strip()
        except OSError:
            return None

    def exists(self, iface: str, rel: str) -> bool:
        return os.path.exists(os.path.join(self.sysnet, iface, rel))

    def listdir(self, iface: str, rel: str) -> Optional[List[str]]:
        try:
            return sorted(os.listdir(os.path.join(self.sysnet, iface, rel)))
        except OSError:
            return None

    def realpath(self, iface: str, rel: str) -> Optional[str]:
        p = os.path.join(self.sysnet, iface, rel)
        if not os.path.exists(p):
            return None
        return os.path.realpath(p)

    def read_path(self, path: str) -> Optional[str]:
        try:
            with open(path, "r") as f:
                return f.read()
        except OSError:
            return None

    # ---- commands ---------------------------------------------------------------------
    def which(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def run(self, cmd: List[str], timeout: float = 10.0) -> CmdResult:
        return run_cmd(cmd, timeout)

    def popen(self, cmd: List[str]) -> subprocess.Popen:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                env=c_env(), stdin=subprocess.DEVNULL, errors="replace")

    # ---- kernel log --------------------------------------------------------------------
    def kernel_history(self, max_lines: int = 5000) -> Tuple[Optional[str], List[str], str]:
        """Kernel messages of the current boot.  Returns (source, lines, error)."""
        r = self.run(["journalctl", "-k", "-b", "-q", "--no-pager", "-o", "short-iso-precise",
                      "-n", str(max_lines)], timeout=30)
        if r.ok and r.out.strip():
            return "journalctl", r.out.splitlines(), ""
        err1 = r.error_text() if not r.ok else "journalctl returned no kernel messages (permissions?)"
        r2 = self.run(["dmesg", "--time-format", "iso"], timeout=15)
        if r2.ok:
            return "dmesg", r2.out.splitlines()[-max_lines:], ""
        return None, [], "journalctl: %s; dmesg: %s" % (err1, r2.error_text())

    def kernel_follow(self, source: str) -> KernelFollower:
        if source == "journalctl":
            cmd = ["journalctl", "-k", "-f", "-q", "-n", "0", "--no-pager", "-o", "short-iso-precise"]
        else:
            cmd = ["dmesg", "-w", "--time-format", "iso"]
        return KernelFollower(source, self.popen(cmd))

    # ---- ICMP ----------------------------------------------------------------------------
    def open_prober(self, target: str, timeout: float, prefer: str = "auto"):
        return open_prober(target, timeout, prefer)

    # ---- sleep inhibitor -------------------------------------------------------------------
    def start_sleep_inhibitor(self) -> Tuple[Optional[subprocess.Popen], str]:
        if not self.which("systemd-inhibit"):
            return None, "systemd-inhibit not found"
        try:
            p = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=Ethernet Stability Tester",
                                  "--why=Long running Ethernet stability test", "--mode=block",
                                  "sleep", "infinity"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        except OSError as e:
            return None, str(e)
        try:
            p.wait(timeout=1.5)
            err = (p.stderr.read() or "").strip() if p.stderr else ""
            return None, err or "systemd-inhibit exited (rc=%s)" % p.returncode
        except subprocess.TimeoutExpired:
            return p, ""
