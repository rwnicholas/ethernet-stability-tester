"""Simulated platform for end-to-end testing without hardware dependencies."""
from __future__ import annotations

import queue
import random
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

from .platform import KernelFollower
from .probes import Result
from .util import CmdResult


class SimulatedProber:
    kind = "simulated-icmp"

    def __init__(self, platform: "SimulatedPlatform", target: str, timeout: float):
        self.p = platform
        self.target = target
        self.timeout = timeout
        self.sent: Dict[int, float] = {}

    def open(self):
        pass

    def close(self):
        self.sent.clear()

    def send(self, seq: int) -> List[Result]:
        self.sent[seq] = time.monotonic()
        return []

    def forget(self, seq: int):
        self.sent.pop(seq, None)

    def poll(self, wait: float) -> List[Result]:
        if wait > 0:
            time.sleep(wait)
        out: List[Result] = []
        now = time.monotonic()
        link_up = self.p.sim_state.get("carrier", 1) == 1

        for seq, t0 in list(self.sent.items()):
            # check simulated packet loss
            drop = False
            if not link_up:
                drop = True
            elif self.p.sim_packet_loss_rate > 0 and random.random() < self.p.sim_packet_loss_rate:
                drop = True

            if drop:
                if now - t0 >= self.timeout:
                    del self.sent[seq]
                    out.append((seq, "timeout", None, "simulated timeout"))
            else:
                # Deliver response with realistic latency
                base_lat = self.p.sim_base_latency_ms
                jitter = random.uniform(-0.4, 0.6)
                rtt = max(0.2, base_lat + jitter)
                # Occasional spike
                if random.random() < self.p.sim_latency_spike_rate:
                    rtt += random.uniform(80.0, 160.0)
                del self.sent[seq]
                out.append((seq, "ok", rtt, ""))
        return out


class SimulatedKernelFollower:
    def __init__(self, log_queue: queue.Queue):
        self.q = log_queue
        self.closed = False

    def lines(self) -> Iterator[str]:
        while not self.closed:
            try:
                line = self.q.get(timeout=0.2)
                if line is None:
                    break
                yield line
            except queue.Empty:
                continue

    def close(self):
        self.closed = True
        try:
            self.q.put_nowait(None)
        except Exception:
            pass


class SimulatedPlatform:
    name = "simulated"
    sysnet = "/sys/class/net"

    def __init__(self, iface: str = "eth0", scenario: str = "default"):
        self.iface = iface
        self.scenario = scenario
        self.start_mono = time.monotonic()

        self.sim_base_latency_ms = 1.2
        self.sim_packet_loss_rate = 0.0
        self.sim_latency_spike_rate = 0.01

        self.klog_queue: queue.Queue = queue.Queue()
        self.klog_history: List[str] = [
            f"kernel: r8152 2-1:1.0 {iface}: v1.12.13 (2023/05/18)",
            f"kernel: r8152 2-1:1.0 {iface}: eth0: RTL8153 Gigabit Ethernet adapter",
            f"kernel: r8152 2-1:1.0 {iface}: carrier on, link up 1000Mbps, full duplex",
        ]

        self.sim_state = {
            "carrier": 1,
            "operstate": "up",
            "speed": 1000,
            "duplex": "full",
            "autoneg": "on",
            "carrier_changes": 2,
            "carrier_up_count": 1,
            "carrier_down_count": 1,
            "ethtool_link_down_events": 0,
        }

        self.stats = {
            "rx_packets": 10000, "tx_packets": 8000,
            "rx_bytes": 12500000, "tx_bytes": 7400000,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "rx_crc_errors": 0, "rx_frame_errors": 0,
            "rx_length_errors": 0, "rx_over_errors": 0,
            "rx_fifo_errors": 0, "rx_missed_errors": 0,
            "rx_nohandler": 0, "multicast": 42,
            "collisions": 0, "tx_carrier_errors": 0,
            "tx_aborted_errors": 0, "tx_fifo_errors": 0,
            "tx_heartbeat_errors": 0, "tx_window_errors": 0,
        }

        self.driver_stats = {
            "tx_packets": 8000, "rx_packets": 10000,
            "tx_errors": 0, "rx_errors": 0,
            "rx_crc_errors": 0, "align_errors": 0,
            "tx_carrier_errors": 0,
            "rx_missed": 0,
            "link_down_events": 0,
        }

        # Scenario setup
        if scenario == "unstable_link":
            # Will simulate link flaps and speed downgrades
            self._start_unstable_link_scenario()
        elif scenario == "packet_loss_layer3":
            # Link stays up, but packet loss occurs (router issue)
            self.sim_packet_loss_rate = 0.08
        elif scenario == "crc_errors":
            self._start_crc_errors_scenario()

    def _start_unstable_link_scenario(self):
        def _flapper():
            # After 4s drop link for 3s, then recover, then drop again
            time.sleep(3.0)
            self.trigger_link_drop(duration=2.5)
            time.sleep(4.0)
            self.trigger_speed_change(100)
            time.sleep(3.0)
            self.trigger_link_drop(duration=1.5)

        t = threading.Thread(target=_flapper, daemon=True, name="sim-flapper")
        t.start()

    def _start_crc_errors_scenario(self):
        def _errs():
            for _ in range(5):
                time.sleep(2.0)
                self.stats["rx_crc_errors"] += 3
                self.stats["rx_errors"] += 3
                self.driver_stats["rx_crc_errors"] += 3
        threading.Thread(target=_errs, daemon=True, name="sim-errs").start()

    def trigger_link_drop(self, duration: float = 2.0):
        self.sim_state["carrier"] = 0
        self.sim_state["operstate"] = "down"
        self.sim_state["speed"] = -1
        self.sim_state["duplex"] = "unknown"
        self.sim_state["carrier_changes"] += 1
        self.sim_state["carrier_down_count"] += 1
        self.sim_state["ethtool_link_down_events"] += 1
        self.driver_stats["link_down_events"] += 1
        self.klog_queue.put(f"kernel: r8152 2-1:1.0 {self.iface}: carrier off, link down")

        def _recover():
            time.sleep(duration)
            self.sim_state["carrier"] = 1
            self.sim_state["operstate"] = "up"
            self.sim_state["speed"] = 1000
            self.sim_state["duplex"] = "full"
            self.sim_state["carrier_changes"] += 1
            self.sim_state["carrier_up_count"] += 1
            self.klog_queue.put(f"kernel: r8152 2-1:1.0 {self.iface}: carrier on, link up 1000Mbps, full duplex")

        threading.Thread(target=_recover, daemon=True).start()

    def trigger_speed_change(self, new_speed: int):
        self.sim_state["speed"] = new_speed
        self.klog_queue.put(f"kernel: r8152 2-1:1.0 {self.iface}: link speed renegotiated to {new_speed}Mbps")

    # Platform interface
    def list_ifaces(self) -> List[str]:
        return ["lo", self.iface, "wlp1s0"]

    def iface_exists(self, iface: str) -> bool:
        return iface in self.list_ifaces()

    def read(self, iface: str, rel: str) -> Optional[str]:
        if iface != self.iface:
            if iface == "lo" and rel == "type":
                return "772"
            return None
        # Progress packets slightly on read
        self.stats["rx_packets"] += 10
        self.stats["tx_packets"] += 8
        self.stats["rx_bytes"] += 14000
        self.stats["tx_bytes"] += 9000

        if rel == "carrier":
            return str(self.sim_state["carrier"])
        if rel == "operstate":
            return self.sim_state["operstate"]
        if rel == "speed":
            sp = self.sim_state["speed"]
            return str(sp) if sp > 0 else None
        if rel == "duplex":
            return self.sim_state["duplex"]
        if rel == "type":
            return "1"  # ARPHRD_ETHER
        if rel == "flags":
            return "0x1003"  # UP | BROADCAST | MULTICAST
        if rel == "address":
            return "00:e0:4c:68:01:23"
        if rel == "mtu":
            return "1500"
        if rel in self.sim_state:
            return str(self.sim_state[rel])
        if rel.startswith("statistics/"):
            stat_name = rel.split("/", 1)[1]
            if stat_name in self.stats:
                return str(self.stats[stat_name])
        return None

    def exists(self, iface: str, rel: str) -> bool:
        if iface != self.iface:
            return False
        if rel == "device":
            return True
        if rel in ("wireless", "phy80211", "bridge", "bonding", "tun_flags"):
            return False
        return True

    def listdir(self, iface: str, rel: str) -> Optional[List[str]]:
        if iface == self.iface and rel == "statistics":
            return list(self.stats.keys())
        return None

    def realpath(self, iface: str, rel: str) -> Optional[str]:
        if iface == self.iface:
            if rel == "device/driver":
                return "/sys/bus/usb/drivers/r8152"
            if rel == "device/subsystem":
                return "/sys/bus/usb"
            if rel == "device":
                return "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0"
        return None

    def read_path(self, path: str) -> Optional[str]:
        if path == "/proc/net/route":
            # Simulate default route through self.iface
            return (
                "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
                f"{self.iface}\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"  # 192.168.1.1
                f"{self.iface}\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
            )
        if path == "/etc/os-release":
            return 'NAME="SteamOS"\nPRETTY_NAME="SteamOS 3.6 (Holo)"\nID="steamos"\nVERSION_ID="3.6"\n'
        return None

    def which(self, tool: str) -> bool:
        if tool in ("ethtool", "ping", "ip", "journalctl", "dmesg", "udevadm"):
            return True
        if tool == "iperf3":
            return True
        return False

    def run(self, cmd: List[str], timeout: float = 10.0) -> CmdResult:
        base = cmd[0]
        if base == "ethtool":
            if "-S" in cmd:
                # Return driver stats
                lines = ["NIC statistics:"]
                for k, v in self.driver_stats.items():
                    lines.append(f"     {k}: {v}")
                return CmdResult(cmd, rc=0, out="\n".join(lines) + "\n")
            if "-i" in cmd:
                out = (
                    f"driver: r8152\n"
                    f"version: v1.12.13\n"
                    f"firmware-version: rtl8153a-4 v2 02/07/20\n"
                    f"expansion-rom-version:\n"
                    f"bus-info: 2-1:1.0\n"
                    f"supports-statistics: yes\n"
                    f"supports-test: no\n"
                    f"supports-eeprom-access: no\n"
                    f"supports-register-dump: no\n"
                    f"supports-priv-flags: no\n"
                )
                return CmdResult(cmd, rc=0, out=out)
            if "--show-eee" in cmd:
                out = (
                    f"EEE status for {self.iface}:\n"
                    f"    EEE support: yes\n"
                    f"    Link partner advertised EEE: yes\n"
                    f"    Tx LPI: 17 (17us)\n"
                )
                return CmdResult(cmd, rc=0, out=out)
            if "-a" in cmd:
                out = (
                    f"Pause parameters for {self.iface}:\n"
                    f"    Autonegotiate: on\n"
                    f"    RX: on\n"
                    f"    TX: on\n"
                )
                return CmdResult(cmd, rc=0, out=out)
            # Default ethtool <iface>
            sp = self.sim_state["speed"]
            speed_str = f"{sp}Mb/s" if sp > 0 else "Unknown!"
            out = (
                f"Settings for {self.iface}:\n"
                f"    Supported ports: [ TP MII ]\n"
                f"    Supported link modes:   10baseT/Half 10baseT/Full\n"
                f"                            100baseT/Half 100baseT/Full\n"
                f"                            1000baseT/Full\n"
                f"    Advertised link modes:  10baseT/Half 10baseT/Full\n"
                f"                            100baseT/Half 100baseT/Full\n"
                f"                            1000baseT/Full\n"
                f"    Speed: {speed_str}\n"
                f"    Duplex: {self.sim_state['duplex']}\n"
                f"    Auto-negotiation: {self.sim_state['autoneg']}\n"
                f"    Port: MII\n"
                f"    PHYAD: 32\n"
                f"    Transceiver: internal\n"
                f"    Link detected: {'yes' if self.sim_state['carrier'] == 1 else 'no'}\n"
                f"    Link Down Events: {self.sim_state['ethtool_link_down_events']}\n"
            )
            return CmdResult(cmd, rc=0, out=out)
        elif base == "udevadm":
            out = (
                "ID_VENDOR_FROM_DATABASE=Realtek Semiconductor Corp.\n"
                "ID_MODEL_FROM_DATABASE=RTL8153 Gigabit Ethernet Adapter\n"
                "ID_BUS=usb\n"
                "ID_USB_DRIVER=r8152\n"
            )
            return CmdResult(cmd, rc=0, out=out)
        elif base == "ip":
            if "route" in cmd:
                return CmdResult(cmd, rc=0, out=f"192.168.1.1 dev {self.iface} src 192.168.1.150 uid 1000\n")
            if "addr" in cmd:
                return CmdResult(cmd, rc=0, out=f"2: {self.iface}    inet 192.168.1.150/24 brd 192.168.1.255 scope global dynamic {self.iface}\n")
        elif base == "iperf3":
            # Simulate iperf3 JSON output
            import json
            data = {
                "start": {"test_start": {"protocol": "TCP"}},
                "end": {
                    "sum_sent": {"bits_per_second": 941000000, "retransmits": 2},
                    "sum_received": {"bits_per_second": 938000000},
                    "sum": {"jitter_ms": 0.045, "lost_packets": 0, "packets": 80000, "lost_percent": 0.0}
                }
            }
            return CmdResult(cmd, rc=0, out=json.dumps(data))

        return CmdResult(cmd, rc=0, out="")

    def popen(self, cmd: List[str]):
        raise NotImplementedError("Use kernel_follow in simulation")

    def kernel_history(self, max_lines: int = 5000) -> Tuple[Optional[str], List[str], str]:
        return "journalctl", list(self.klog_history), ""

    def kernel_follow(self, source: str) -> KernelFollower:
        return KernelFollower("simulated", SimulatedKernelFollower(self.klog_queue))

    def open_prober(self, target: str, timeout: float, prefer: str = "auto"):
        p = SimulatedProber(self, target, timeout)
        p.open()
        return p

    def start_sleep_inhibitor(self) -> Tuple[Optional[object], str]:
        return None, "simulation mode (sleep inhibition mock)"
