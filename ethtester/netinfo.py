"""Interface detection, read-only ethtool queries and parsing, gateway / route discovery."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
from typing import Dict, List, Optional, Tuple

from .util import to_int

ARPHRD_ETHER = "1"


# ---------------------------------------------------------------------------- ethtool parsing
def parse_ethtool_kv(text: str) -> Dict[str, str]:
    """Parse the 'Key: value' layout used by `ethtool`, `ethtool -i`, `--show-eee`, `-a`.
    Continuation lines (link mode lists) are joined to the previous key with a space."""
    out: Dict[str, str] = {}
    last = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if i == 0 and not line[:1].isspace() and re.search(r"\b(for|settings)\b", line, re.I) \
                and line.rstrip().endswith(":"):
            continue  # header like "Settings for eth0:"
        m = re.match(r"^\s*([^:\s][^:]*?):\s*(.*?)\s*$", line)
        if m:
            last = m.group(1).strip()
            out[last] = m.group(2)
        elif last is not None:
            out[last] = (out[last] + " " + line.strip()).strip()
    return out


def parse_ethtool_stats(text: str) -> Dict[str, int]:
    """Parse `ethtool -S` (driver and standard group) output: 'name: integer' lines.
    Nested group headers are used as prefixes."""
    stats: Dict[str, int] = {}
    prefix_stack: List[Tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        indent = len(line.expandtabs(8)) - len(line.expandtabs(8).lstrip())
        m = re.match(r"^\s*(.+?):\s*(-?\d+)\s*$", line)
        if m:
            while prefix_stack and prefix_stack[-1][0] >= indent:
                prefix_stack.pop()
            name = m.group(1).strip()
            if prefix_stack:
                name = ".".join(p for _, p in prefix_stack) + "." + name
            stats[name] = int(m.group(2))
            continue
        m = re.match(r"^\s*(.+?):\s*$", line)
        if m:
            head = m.group(1).strip()
            if re.match(r"^(NIC statistics|Standard stats for .*)$", head):
                prefix_stack = []
                continue
            while prefix_stack and prefix_stack[-1][0] >= indent:
                prefix_stack.pop()
            prefix_stack.append((indent, head))
    return stats


def modes(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() in ("not reported", "none"):
        return []
    return v.split()


def parse_speed(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    m = re.match(r"(\d+)\s*Mb/s", value)
    return int(m.group(1)) if m else None


def summarize_settings(kv: Dict[str, str]) -> Dict:
    """Reduce `ethtool <iface>` output to the fields relevant for link stability."""
    if not kv:
        return {}
    autoneg = kv.get("Auto-negotiation")
    return {
        "speed_mbps": parse_speed(kv.get("Speed")),
        "speed_raw": kv.get("Speed"),
        "duplex": kv.get("Duplex"),
        "autoneg": autoneg,
        "port": kv.get("Port"),
        "mdix": kv.get("MDI-X"),
        "link_detected": kv.get("Link detected"),
        "supported_modes": modes(kv.get("Supported link modes")),
        "advertised_modes": modes(kv.get("Advertised link modes")),
        "partner_advertised_modes": modes(kv.get("Link partner advertised link modes")),
        "partner_autoneg": kv.get("Link partner advertised auto-negotiation"),
        "advertised_pause": kv.get("Advertised pause frame use"),
        "partner_pause": kv.get("Link partner advertised pause frame use"),
        "master_slave": kv.get("master-slave status"),
        "link_down_events": to_int(kv.get("Link Down Events")),
    }


class Ethtool:
    """Read-only ethtool queries.  Never issues a 'set' operation."""

    def __init__(self, platform, iface: str):
        self.p = platform
        self.iface = iface
        self.available = platform.which("ethtool")

    def _run(self, args):
        if not self.available:
            return None, "ethtool not installed"
        r = self.p.run(["ethtool"] + args, timeout=10)
        if r.ok and r.out.strip():
            return r.out, ""
        return None, r.error_text() if not r.ok else "no output"

    def settings(self) -> Tuple[Dict, str]:
        out, err = self._run([self.iface])
        if out is None:
            return {}, err
        kv = parse_ethtool_kv(out)
        info = summarize_settings(kv)
        # link-down event counter (ethtool >= 5.x with driver support)
        out2, _ = self._run(["-I", self.iface])
        if out2:
            kv2 = parse_ethtool_kv(out2)
            for k, v in kv2.items():
                if k.lower() == "link down events":
                    info["link_down_events"] = to_int(v)
        return info, ""

    def driver_info(self) -> Tuple[Dict, str]:
        out, err = self._run(["-i", self.iface])
        return (parse_ethtool_kv(out), "") if out else ({}, err)

    def eee(self) -> Tuple[Dict, str]:
        out, err = self._run(["--show-eee", self.iface])
        return (parse_ethtool_kv(out), "") if out else ({}, err)

    def pause(self) -> Tuple[Dict, str]:
        out, err = self._run(["-a", self.iface])
        return (parse_ethtool_kv(out), "") if out else ({}, err)

    def driver_stats(self) -> Tuple[Optional[Dict[str, int]], str]:
        out, err = self._run(["-S", self.iface])
        if out is None:
            return None, err
        st = parse_ethtool_stats(out)
        return (st, "") if st else (None, "driver exposes no statistics")

    def standard_stats(self) -> Tuple[Optional[Dict[str, int]], str]:
        """IEEE 802.3 / RMON standard counters (ethtool >= 5.13, driver support required)."""
        out, err = self._run(["-S", self.iface, "--all-groups"])
        if out is None:
            return None, err
        st = parse_ethtool_stats(out)
        return (st, "") if st else (None, "driver exposes no standard statistics groups")


# ---------------------------------------------------------------------------- detection
def classify_iface(p, name: str) -> Dict:
    """Return {'name', 'is_ethernet', 'kind', 'reason'}"""
    info = {"name": name, "is_ethernet": False, "kind": "unknown", "reason": ""}
    if name == "lo":
        info.update(kind="loopback", reason="loopback")
        return info
    typ = p.read(name, "type")
    if p.exists(name, "wireless") or p.exists(name, "phy80211"):
        info.update(kind="wireless", reason="wireless interface")
        return info
    if p.exists(name, "bridge"):
        info.update(kind="bridge", reason="bridge")
        return info
    if p.exists(name, "bonding"):
        info.update(kind="bond", reason="bond")
        return info
    if p.exists(name, "tun_flags"):
        info.update(kind="tun", reason="tun/tap")
        return info
    if typ != ARPHRD_ETHER:
        info.update(kind="type-%s" % typ, reason="not an Ethernet link type")
        return info
    if not p.exists(name, "device"):
        info.update(kind="virtual", reason="virtual interface (no hardware device)")
        return info
    info.update(is_ethernet=True, kind="ethernet")
    return info


def detect_ethernet(p, include_all: bool = False) -> List[Dict]:
    res = []
    for n in p.list_ifaces():
        c = classify_iface(p, n)
        if c["is_ethernet"] or (include_all and c["kind"] != "loopback"):
            res.append(c)
    return res


def _udev_props(p, iface: str) -> Dict[str, str]:
    if not p.which("udevadm"):
        return {}
    r = p.run(["udevadm", "info", "-q", "property", "-p", "/sys/class/net/%s" % iface], timeout=5)
    if not r.ok:
        return {}
    out = {}
    for line in r.out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def hardware_info(p, iface: str) -> Dict:
    """Driver, bus, vendor and model of the adapter, as far as available."""
    hw: Dict = {"driver": None, "bus": None, "bus_info": None, "vendor": None, "model": None,
                "vendor_id": None, "device_id": None, "driver_version": None, "firmware_version": None}
    drv = p.realpath(iface, "device/driver")
    if drv:
        hw["driver"] = os.path.basename(drv)
    subsys = p.realpath(iface, "device/subsystem")
    if subsys:
        hw["bus"] = os.path.basename(subsys)
    dev = p.realpath(iface, "device")
    if dev:
        hw["bus_info"] = os.path.basename(dev)
    et = Ethtool(p, iface)
    di, _ = et.driver_info()
    if di:
        hw["driver"] = di.get("driver") or hw["driver"]
        hw["driver_version"] = di.get("version") or None
        hw["firmware_version"] = di.get("firmware-version") or None
        hw["bus_info"] = di.get("bus-info") or hw["bus_info"]
    udev = _udev_props(p, iface)
    hw["vendor"] = udev.get("ID_VENDOR_FROM_DATABASE") or udev.get("ID_VENDOR_ENC") or None
    hw["model"] = udev.get("ID_MODEL_FROM_DATABASE") or udev.get("ID_MODEL_ENC") or None
    if hw["bus"] == "pci":
        hw["vendor_id"] = p.read(iface, "device/vendor")
        hw["device_id"] = p.read(iface, "device/device")
        if (not hw["vendor"] or not hw["model"]) and hw["bus_info"] and p.which("lspci"):
            r = p.run(["lspci", "-vmm", "-s", hw["bus_info"]], timeout=5)
            if r.ok:
                kv = parse_ethtool_kv(r.out)
                hw["vendor"] = hw["vendor"] or kv.get("Vendor")
                hw["model"] = hw["model"] or kv.get("Device")
    elif hw["bus"] == "usb":
        # interface dir -> parent usb device has manufacturer/product/idVendor/idProduct
        hw["vendor_id"] = p.read(iface, "device/../idVendor")
        hw["device_id"] = p.read(iface, "device/../idProduct")
        hw["vendor"] = hw["vendor"] or p.read(iface, "device/../manufacturer")
        hw["model"] = hw["model"] or p.read(iface, "device/../product")
        if dev:
            hw["usb_port"] = os.path.basename(os.path.dirname(dev))
    return hw


def iface_addresses(p, iface: str) -> Dict:
    out = {"mac": p.read(iface, "address"), "mtu": to_int(p.read(iface, "mtu")), "ipv4": [], "ipv6": []}
    if p.which("ip"):
        r = p.run(["ip", "-o", "addr", "show", "dev", iface], timeout=5)
        if r.ok:
            for line in r.out.splitlines():
                m = re.search(r"\binet6?\s+(\S+)", line)
                if m:
                    (out["ipv6"] if " inet6 " in line else out["ipv4"]).append(m.group(1))
    return out


# ---------------------------------------------------------------------------- routing
def _hex_ip(h: str) -> str:
    return socket.inet_ntoa(struct.pack("<I", int(h, 16)))


def read_routes(p) -> List[Dict]:
    text = p.read_path("/proc/net/route") or ""
    routes = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            routes.append({"iface": f[0], "dst": _hex_ip(f[1]), "gw": _hex_ip(f[2]),
                           "flags": int(f[3], 16), "metric": int(f[6]), "mask": _hex_ip(f[7])})
        except ValueError:
            continue
    return routes


def default_gateway(p, iface: str) -> Optional[str]:
    """IPv4 default gateway reachable through *iface* (lowest metric)."""
    cands = [r for r in read_routes(p) if r["iface"] == iface and r["dst"] == "0.0.0.0"
             and r["mask"] == "0.0.0.0" and r["flags"] & 0x2 and r["flags"] & 0x1]
    if not cands:
        return None
    return sorted(cands, key=lambda r: r["metric"])[0]["gw"]


def route_iface(p, target: str) -> Optional[str]:
    """Interface the kernel would use to reach *target*."""
    if p.which("ip"):
        r = p.run(["ip", "-4", "route", "get", target], timeout=5)
        if r.ok:
            m = re.search(r"\bdev\s+(\S+)", r.out)
            if m:
                return m.group(1)
    # fallback: longest-prefix match on /proc/net/route
    try:
        t = int(ipaddress.IPv4Address(target))
    except ValueError:
        return None
    best = None
    for r in read_routes(p):
        if not r["flags"] & 0x1:
            continue
        mask = int(ipaddress.IPv4Address(r["mask"]))
        if t & mask == int(ipaddress.IPv4Address(r["dst"])) & mask:
            key = (bin(mask).count("1"), -r["metric"])
            if best is None or key > best[0]:
                best = (key, r["iface"])
    return best[1] if best else None


def resolve_ipv4(host: str) -> Optional[str]:
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (socket.gaierror, IndexError, OSError):
        return None
