# Ethernet Stability Tester

A native, zero-dependency diagnostic application for Linux and SteamOS designed to monitor, record, and evaluate Ethernet connection stability over extended durations (15 minutes to 24+ hours).

Developed specifically to investigate Ethernet stability issues—such as intermittent link flapping, auto-negotiation degradation, PHY state changes, and packet loss—on devices like the **Valve Steam Deck** (connected via official or third-party USB-C docks) comparing routers such as the **Intelbras IH 3000** against a baseline router (e.g., **Huawei**).

---

## Table of Contents

- [Key Capabilities](#key-capabilities)
- [Architecture & Safety Principles](#architecture--safety-principles)
- [SteamOS Installation & Prerequisites](#steamos-installation--prerequisites)
- [Quick Start](#quick-start)
- [Testing Intelbras IH 3000 vs. Huawei Router](#testing-intelbras-ih-3000-vs-huawei-router)
- [CLI Reference](#cli-reference)
- [Generated Report Structure](#generated-report-structure)
- [Interpreting Test Results](#interpreting-test-results)
- [Simulation Mode for Verification](#simulation-mode-for-verification)

---

## Key Capabilities

1. **Automatic Network Interface Detection**:
   - Detects all physical and virtual interfaces, identifying driver name, bus address (PCIe/USB), vendor, and adapter model (e.g., Realtek RTL8153, Intel I225/I226, ASIX).
   - Retrieves link speed, duplex mode, auto-negotiation, pause frames, and Energy Efficient Ethernet (EEE) status via `ethtool`.
2. **Continuous Physical Link Monitoring**:
   - High-frequency carrier detection (`/sys/class/net/<iface>/carrier`, `carrier_changes`, `carrier_down_count`).
   - Detects rapid link flaps even if they occur faster than regular polling intervals.
   - Records link speed changes (e.g., gigabit dropping to 100 Mbps) and duplex transitions.
3. **Connectivity & Latency Monitoring**:
   - Continuous 1 Hz ICMP ping probing of the default gateway and an optional second target (e.g., `1.1.1.1`).
   - Uses unprivileged Linux ICMP datagram sockets (`SOCK_DGRAM`) with zero root/setuid requirement.
   - **Crucial Diagnostic Distinction**: Automatically categorizes packet loss into:
     - **Loss during physical link down** (Layer 1 hardware/PHY drop).
     - **Loss while physical link remained UP** (Layer 2/3 router forwarding stall or bufferbloat).
4. **Hardware & Driver Error Accounting**:
   - Monitored counters: RX/TX packets, bytes, errors, drops, CRC errors, frame alignment errors, FIFO overruns, carrier errors, collisions.
   - Driver-specific and IEEE 802.3 standard statistics via `ethtool -S`.
   - Distinguishes supported vs. unverified/unsupported driver metrics (reporting `N/A` rather than misleading zeros).
   - Monotonic tracking with automatic detection of interface resets or USB re-enumeration.
5. **Kernel Message Capture**:
   - Streams kernel ring buffer messages related to the network adapter (journalctl / dmesg) in real time.
6. **Optional Traffic Load Testing (`iperf3`)**:
   - TCP and UDP throughput, jitter, and packet loss benchmarking against a local iperf3 server without interrupting basic monitoring.
7. **Real-time User Interface**:
   - Interactive curses terminal UI with status metrics and live event log.
   - Streaming plain-text UI for non-interactive shells, log files, and automation scripts.
8. **Crash-Resilient Report Export**:
   - Every metric row is immediately flushed to disk (`fsync` protected). A power cut or crash never corrupts previously gathered hours of test data.

---

## Architecture & Safety Principles

In accordance with strict diagnostic standards:
- **Zero Configuration Modification**: The tool **never** modifies link settings, disables EEE, alters autonegotiation, changes duplex, or resets the interface.
- **Unprivileged Operation**: Runs entirely as a standard user without needing `sudo`.
- **Low Overhead**: Operates asynchronously with negligible CPU utilization (< 0.5% on Steam Deck CPU).
- **Zero External Python Dependencies**: Built entirely with Python standard libraries (`curses`, `socket`, `struct`, `subprocess`, `threading`, `csv`, `json`).

---

## SteamOS Installation & Prerequisites

SteamOS runs an immutable, read-only root file system (`/usr`). **Ethernet Stability Tester does not require modifying the OS or disabling read-only mode (`steamos-readonly`).**

### 1. Switch to Desktop Mode
On your Steam Deck:
- Press the **STEAM** button.
- Select **Power** → **Switch to Desktop**.

### 2. Open Konsole Terminal
- Click the Application Launcher (bottom left corner) → **System** → **Konsole**.

### 3. Clone or Copy the Repository
```bash
cd ~/
git clone <repository_url> ethernet_test
cd ethernet_test
chmod +x ethernet_stability_tester.py
```

### 4. Verify System Tools
The Steam Deck comes with Python 3, `ip`, and `ping` pre-installed. `ethtool` is typically present. Verify:
```bash
python3 --version
which ethtool ping ip journalctl
```
*(Optional)* If you wish to test local network throughput, install `iperf3` (or run it from a container/flatpak). Basic stability testing does not require `iperf3`.

---

## Quick Start

### 1. List Available Interfaces
```bash
./ethernet_stability_tester.py --list-interfaces
```
Output example:
```
Interface    Kind       Driver       Carrier  Hardware Description
---------------------------------------------------------------------------
enp7s0       ethernet   igc          DOWN     Intel Corporation Ethernet Controller I226-V
eth0         ethernet   r8152        UP       Realtek Semiconductor Corp. RTL8153 Gigabit Ethernet Adapter
wlp6s0       wireless   mt7921e      UP       MEDIATEK Corp. MT7922 802.11ax Wireless Adapter
```

### 2. Run a Quick 60-Second Check
```bash
./ethernet_stability_tester.py -d 60s
```
*(If no interface is specified, the tool automatically selects the active Ethernet adapter).*

### 3. Run a Standard 4-Hour Stability Test
```bash
./ethernet_stability_tester.py -d 4h --label intelbras_test1
```

### 4. Run an Overnight 12-Hour Test with Second Target
```bash
./ethernet_stability_tester.py -d 12h --label intelbras_overnight --target2 1.1.1.1
```

---

## Testing Intelbras IH 3000 vs. Huawei Router

To scientifically determine whether the Intelbras IH 3000 causes Ethernet instability:

### Protocol for Comparative Testing

1. **Keep Environment Identical**:
   - Use the **same Steam Deck USB-C dock / Ethernet dongle**.
   - Use the **same Cat6 / Cat5e Ethernet patch cable**.
   - Turn off Wi-Fi on the Steam Deck so 100% of traffic routes over the Ethernet interface.
2. **Phase 1: Test with Intelbras IH 3000**:
   - Connect the Steam Deck Ethernet cable to a LAN port on the Intelbras IH 3000.
   - Run a 4-hour test:
     ```bash
     ./ethernet_stability_tester.py -d 4h --label router_intelbras_ih3000 --target2 1.1.1.1
     ```
3. **Phase 2: Test with Huawei Router**:
   - Connect the same Ethernet cable to a LAN port on the Huawei router.
   - Run the same 4-hour test:
     ```bash
     ./ethernet_stability_tester.py -d 4h --label router_huawei --target2 1.1.1.1
     ```
4. **Compare the Reports**:
   - Inspect the resulting `reports/<date>_eth0_router_intelbras_ih3000/summary.txt` against `reports/<date>_eth0_router_huawei/summary.txt`.

---

## CLI Reference

```
usage: ethernet-stability-tester [-h] [-v] [-i INTERFACE] [-l] [--label LABEL]
                                 [-d DURATION] [-o OUTPUT_DIR]
                                 [--gateway GATEWAY] [--target2 TARGET2]
                                 [--no-ping] [--ping-interval PING_INTERVAL]
                                 [--ping-timeout PING_TIMEOUT]
                                 [--iperf-server IPERF_SERVER]
                                 [--iperf-port IPERF_PORT]
                                 [--iperf-interval IPERF_INTERVAL]
                                 [--iperf-duration IPERF_DURATION]
                                 [--iperf-udp-bitrate IPERF_UDP_BITRATE]
                                 [--no-kernel-log] [--no-inhibit-sleep]
                                 [--ui {auto,tui,plain}] [--simulate]
                                 [--sim-scenario {default,unstable_link,packet_loss_layer3,crc_errors}]
```

### Key Arguments:
- `-i, --interface <name>`: Network interface to test (e.g. `eth0`). If omitted, auto-selects.
- `-d, --duration <time>`: Duration string (`30s`, `15m`, `4h`, `24h`). If omitted, runs until you press `Q` or `Ctrl+C`.
- `--label <text>`: Custom identifier appended to the report directory name and headers.
- `--target2 <host>`: Second ping target (e.g. `1.1.1.1` or `8.8.8.8`).
- `--iperf-server <host>`: IP of a machine running `iperf3 -s` to evaluate throughput under load.
- `--ui {auto,tui,plain}`: `tui` opens the curses dashboard; `plain` streams log lines (best for background/pipes).

### Interactive Keybindings (TUI mode):
- `Q`: Cleanly finish test and generate all reports.
- `M`: Insert a timestamped user marker bookmark into the event log.
- `I`: Trigger an immediate on-demand iperf3 load test.

---

## Generated Report Structure

Each test run creates an isolated directory inside `reports/`:
`reports/YYYYMMDD-HHMMSS_<interface>_<label>/`

| File | Purpose |
| :--- | :--- |
| `summary.txt` | Human-readable executive summary, diagnostic verdict, and key metrics. |
| `summary.json` | Complete machine-readable summary of all collectors. |
| `interface_info.json` | Hardware details, driver info, initial vs. final state, and counter deltas. |
| `events.log` | Human-readable chronological log of all connection events, flaps, and warnings. |
| `events.csv` | Structured CSV table of all events for spreadsheet analysis. |
| `link_state.csv` | Time-series of carrier state, operstate, speed, and duplex. |
| `link_down_periods.csv` | Exact start, end, and duration of every physical disconnect. |
| `interface_stats.csv` | Standard `/sys/class/net` RX/TX counters and error metrics. |
| `driver_stats.csv` | Detailed `ethtool -S` NIC driver counters. |
| `ping_gateway.csv` | Per-second ICMP RTT, response status, and link state tag. |
| `ping_target2.csv` | Per-second ICMP statistics for secondary target (if configured). |
| `ping_outages.csv` | Every unreachable streak with physical vs. layer 3 classification. |
| `kernel.log` | Filtered kernel messages for the monitored device during the test. |
| `kernel_before_start.log`| Kernel boot messages preceding the test for baseline reference. |

---

## Interpreting Test Results

### 1. Physical Disconnects (Layer 1)
```
DIAGNOSIS VERDICT: [UNSTABLE]
* Physical link dropped 3 time(s).
```
- **What it means**: The Ethernet PHY lost electrical carrier with the router switch port.
- **Root causes**:
  - Auto-negotiation failure or PHY energy management (EEE/LPI) incompatibilities between the router's switch chip and the USB-C dock's NIC (e.g., Realtek RTL8153).
  - Defective cable or loose RJ45 connector pin.
  - Power throttling on the USB-C hub.

### 2. Speed Downshifting (Gigabit → 100 Mbps)
```
Speed Changes Count: 2
  @ 2026-11-20T14:10:00: 1000 Mbps -> 100 Mbps
```
- **What it means**: The link renegotiated down to Fast Ethernet.
- **Root causes**: High electrical noise, marginal cable pair termination, or router port PHY instability forcing fallback.

### 3. Layer 3 Loss with Link UP (Router Forwarding Problem)
```
Target [gateway - 192.168.1.1]:
  Total Packet Loss         : 4.200% (42 lost)
  * Loss While Link Was UP  : 4.200% (42 lost)
  * Loss While Link Was DOWN: 0 lost
```
- **What it means**: Physical link remained green (`UP`) without a single drop, yet packets to the router IP were dropped or timed out.
- **Root causes**:
  - The router's internal CPU/network stack is freezing, dropping ARP, or overwhelmed.
  - Switch chip buffer exhaustion or software-induced latency spikes in router firmware.

### 4. CRC / Frame Errors
```
3. INTERFACE ERROR COUNTERS
  ! rx_crc_errors: delta = +14
```
- **What it means**: Corrupted Ethernet frames received.
- **Root causes**: Electrical interference or physical cable/port defect.

---

## Simulation Mode for Verification

The suite includes a comprehensive simulated platform that reproduces real-world behaviors without touching physical hardware:

```bash
# Clean baseline run (3 seconds)
./ethernet_stability_tester.py --simulate --duration 3s --ui plain

# Flapping link scenario (simulates link drops and speed changes)
./ethernet_stability_tester.py --simulate --sim-scenario unstable_link --duration 12s --ui plain

# Layer 3 packet loss scenario (router dropping packets while link stays UP)
./ethernet_stability_tester.py --simulate --sim-scenario packet_loss_layer3 --duration 10s --ui plain

# CRC error scenario (simulating cable/PHY corruption)
./ethernet_stability_tester.py --simulate --sim-scenario crc_errors --duration 10s --ui plain
```

To run all automated unit and integration tests:
```bash
python3 -m unittest discover tests
```
