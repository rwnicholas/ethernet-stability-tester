"""Command-line interface and argument parsing for Ethernet Stability Tester."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .netinfo import classify_iface, detect_ethernet, hardware_info
from .platform import LinuxPlatform
from .session import Config, Session
from .sim import SimulatedPlatform
from .ui import run_ui
from .util import parse_duration


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ethernet-stability-tester",
        description="Diagnose Ethernet connection instability, link drops, and packet loss on Linux / SteamOS.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    # Target interface selection
    p.add_argument("-i", "--interface", help="Ethernet interface to monitor (e.g. eth0, enp7s0). Auto-detected if omitted.")
    p.add_argument("-l", "--list-interfaces", action="store_true", help="List detected network interfaces and exit.")

    # Test parameters
    p.add_argument("--label", default="", help="Label for this test run (e.g. 'intelbras_ih3000', 'huawei_ax3').")
    p.add_argument("-d", "--duration", help="Test duration (e.g. '30s', '15m', '4h', '24h'). Default: run until stopped.")
    p.add_argument("-o", "--output-dir", default="reports", help="Directory where test report folders are stored.")

    # Target destinations
    p.add_argument("--gateway", help="Default gateway IP override for ICMP reachability test.")
    p.add_argument("--target2", help="Optional second destination to ping simultaneously (e.g. 1.1.1.1, 8.8.8.8).")
    p.add_argument("--no-ping", action="store_true", help="Disable ICMP reachability probing.")
    p.add_argument("--ping-interval", type=float, default=1.0, help="ICMP ping interval in seconds.")
    p.add_argument("--ping-timeout", type=float, default=1.0, help="ICMP probe timeout in seconds.")

    # iperf3 traffic load testing
    p.add_argument("--iperf-server", help="IP address or hostname of an iperf3 server on the local network.")
    p.add_argument("--iperf-port", type=int, default=5201, help="iperf3 server port.")
    p.add_argument("--iperf-interval", type=float, default=1800.0, help="Interval between periodic iperf3 load runs in seconds (0 = run once at start).")
    p.add_argument("--iperf-duration", type=int, default=10, help="Duration of each iperf3 run in seconds.")
    p.add_argument("--iperf-udp-bitrate", default="100M", help="Target bitrate for UDP load tests.")

    # System & Platform
    p.add_argument("--no-kernel-log", action="store_true", help="Disable monitoring of kernel log messages.")
    p.add_argument("--no-inhibit-sleep", action="store_true", help="Do not attempt to block systemd sleep during test.")
    p.add_argument("--ui", choices=["auto", "tui", "plain"], default="auto", help="User interface mode.")

    # Simulation mode
    p.add_argument("--simulate", action="store_true", help="Run with a simulated Ethernet adapter without touching real hardware.")
    p.add_argument(
        "--sim-scenario",
        choices=["default", "unstable_link", "packet_loss_layer3", "crc_errors"],
        default="default",
        help="Fault scenario for simulation mode.",
    )

    return p


def select_interface(platform, manual: str = None) -> str:
    """Interactively or automatically select the interface to test."""
    if manual:
        if not platform.iface_exists(manual):
            print(f"Error: Specified interface '{manual}' does not exist.", file=sys.stderr)
            sys.exit(1)
        return manual

    detected = detect_ethernet(platform, include_all=False)
    if not detected:
        # Fall back to any non-loopback interface
        all_ifaces = detect_ethernet(platform, include_all=True)
        if not all_ifaces:
            print("Error: No network interfaces detected.", file=sys.stderr)
            sys.exit(1)
        print("Notice: No dedicated hardware Ethernet interface detected. Available interfaces:")
        for idx, iface_info in enumerate(all_ifaces):
            print(f"  [{idx + 1}] {iface_info['name']:<12} ({iface_info['kind']})")
        if len(all_ifaces) == 1 or not sys.stdin.isatty():
            choice = 0
        else:
            try:
                raw = input("Select interface number [1]: ").strip()
                choice = int(raw) - 1 if raw else 0
            except (ValueError, EOFError):
                choice = 0
        return all_ifaces[choice]["name"]

    if len(detected) == 1:
        chosen = detected[0]["name"]
        hw = hardware_info(platform, chosen)
        hw_desc = f"{hw.get('vendor') or ''} {hw.get('model') or ''} (driver {hw.get('driver') or 'unknown'})".strip()
        print(f"Auto-selected Ethernet interface: {chosen} [{hw_desc}]")
        return chosen

    # Multiple Ethernet interfaces
    print("Detected multiple Ethernet interfaces:")
    for idx, d in enumerate(detected):
        hw = hardware_info(platform, d["name"])
        hw_desc = f"{hw.get('vendor') or ''} {hw.get('model') or ''} [driver: {hw.get('driver') or 'N/A'}]".strip()
        print(f"  [{idx + 1}] {d['name']:<12} {hw_desc}")

    if not sys.stdin.isatty():
        return detected[0]["name"]

    try:
        raw = input(f"Select interface to test [1-{len(detected)}] (default 1): ").strip()
        choice = int(raw) - 1 if raw else 0
        if 0 <= choice < len(detected):
            return detected[choice]["name"]
    except (ValueError, EOFError):
        pass
    return detected[0]["name"]


def list_interfaces_cmd(platform):
    """Print all discovered interfaces with classification and hardware details."""
    all_names = platform.list_ifaces()
    print(f"{'Interface':<12} {'Kind':<10} {'Driver':<12} {'Carrier':<8} {'Hardware Description'}")
    print("-" * 75)
    for name in all_names:
        c = classify_iface(platform, name)
        hw = hardware_info(platform, name)
        carrier = platform.read(name, "carrier")
        carrier_str = "UP" if carrier == "1" else ("DOWN" if carrier == "0" else "N/A")
        desc = f"{hw.get('vendor') or ''} {hw.get('model') or ''}".strip() or "-"
        driver = hw.get("driver") or "-"
        print(f"{name:<12} {c['kind']:<10} {driver:<12} {carrier_str:<8} {desc}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Determine platform
    if args.simulate:
        platform = SimulatedPlatform(iface=args.interface or "eth0", scenario=args.sim_scenario)
    else:
        platform = LinuxPlatform()

    if args.list_interfaces:
        list_interfaces_cmd(platform)
        return 0

    iface = select_interface(platform, manual=args.interface)

    duration = None
    if args.duration:
        try:
            duration = parse_duration(args.duration)
        except ValueError as e:
            print(f"Error parsing duration: {e}", file=sys.stderr)
            return 1

    cfg = Config(
        interface=iface,
        label=args.label,
        duration=duration,
        output_dir=args.output_dir,
        gateway=args.gateway,
        target2=args.target2,
        no_ping=args.no_ping,
        ping_interval=args.ping_interval,
        ping_timeout=args.ping_timeout,
        iperf_server=args.iperf_server,
        iperf_port=args.iperf_port,
        iperf_interval=args.iperf_interval,
        iperf_duration=args.iperf_duration,
        iperf_udp_bitrate=args.iperf_udp_bitrate,
        kernel_log=not args.no_kernel_log,
        inhibit_sleep=not args.no_inhibit_sleep,
        simulate=args.simulate,
        ui=args.ui,
    )

    session = Session(cfg, platform, iface)

    try:
        session.start()
        run_ui(session)
    finally:
        session.finalize()

    print(f"\nTest report saved to: {session.report_dir}")
    print(f"Summary text file: {os.path.join(session.report_dir, 'summary.txt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
