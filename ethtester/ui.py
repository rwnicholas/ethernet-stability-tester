"""User interface for real-time monitoring: terminal curses TUI and plain text mode."""
from __future__ import annotations

import curses
import os
import sys
import threading
import time
from typing import Optional

from . import __version__
from .recorder import Event, SEV_CRIT, SEV_INFO, SEV_WARN
from .util import fmt_duration, fmt_num, fmt_rate, short_time


class PlainTextUI:
    """Line-by-line streaming UI suitable for pipes, log files, or dumb terminals."""

    def __init__(self, session):
        self.s = session
        self.stop_event = threading.Event()
        self.s.rec.listeners.append(self.on_event)
        self.last_status_time = 0.0

    def on_event(self, ev: Event):
        t = short_time(ev.ts)
        print(f"[{t}] [{ev.severity:<4}] {ev.type:<16} {ev.message}", flush=True)

    def print_periodic_status(self):
        now = time.monotonic()
        if now - self.last_status_time < 5.0:
            return
        self.last_status_time = now

        lk = self.s.collector("link")
        link_state = lk.state if lk else "N/A"
        speed = f"{lk.speed}M" if lk and lk.speed else "N/A"
        duplex = lk.duplex or "N/A"
        discons = lk.total_disconnects() if lk else 0

        gw = self.s.collector("ping_gateway")
        gw_loss = "N/A"
        gw_lat = "N/A"
        if gw and gw.available and gw.sent:
            gw_loss = f"{gw.lost * 100.0 / gw.sent:.1f}%"
            if gw.rtt_min is not None:
                gw_lat = f"{gw.rtt_min:.1f}/{gw.rtt_sum / max(1, gw.received):.1f}/{gw.rtt_max:.1f}ms"

        cnt = self.s.collector("counters")
        rx_rate = fmt_rate(cnt.rx_bps) if cnt and cnt.rx_bps is not None else "N/A"
        tx_rate = fmt_rate(cnt.tx_bps) if cnt and cnt.tx_bps is not None else "N/A"

        el = fmt_duration(self.s.elapsed())
        rem = f" / {fmt_duration(self.s.remaining())} left" if self.s.cfg.duration else ""
        print(
            f"--- [STATUS {el}{rem}] Link: {link_state} ({speed}/{duplex}) | "
            f"Drops: {discons} | GW Loss: {gw_loss} | GW Lat: {gw_lat} | "
            f"RX: {rx_rate} TX: {tx_rate} ---",
            flush=True,
        )

    def run(self):
        print(f"=== Ethernet Stability Tester v{__version__} ===")
        print(f"Interface : {self.s.iface} (Driver: {self.s.hw.get('driver') or 'N/A'})")
        print(f"Report Dir: {self.s.report_dir}")
        print("Press Ctrl+C to stop test and export reports.\n")
        try:
            while not self.s.stop_event.is_set():
                if self.s.cfg.duration is not None and self.s.remaining() <= 0:
                    self.s.stop("planned duration reached")
                    break
                self.print_periodic_status()
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.s.stop("Ctrl+C interrupt")


class CursesTUI:
    """Interactive full-terminal dashboard with status cards and scrolling event log."""

    def __init__(self, session):
        self.s = session
        self.stdscr = None
        self.events = []
        self.lock = threading.Lock()
        self.s.rec.listeners.append(self.on_event)

    def on_event(self, ev: Event):
        with self.lock:
            self.events.append(ev)
            if len(self.events) > 300:
                self.events = self.events[-300:]

    def run(self):
        try:
            curses.wrapper(self._main_loop)
        except Exception as e:
            # Fall back cleanly
            print(f"TUI exited: {e}", file=sys.stderr)
            self.s.stop(f"TUI error: {e}")

    def _init_colors(self):
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)   # OK / UP
            curses.init_pair(2, curses.COLOR_YELLOW, -1)  # WARN / Flap
            curses.init_pair(3, curses.COLOR_RED, -1)     # DOWN / CRIT / Error
            curses.init_pair(4, curses.COLOR_CYAN, -1)    # Info / Labels
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Header banner
            curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)  # Status bar

    def _main_loop(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(500)
        self._init_colors()

        while not self.s.stop_event.is_set():
            if self.s.cfg.duration is not None and self.s.remaining() <= 0:
                self.s.stop("planned duration reached")
                break
            # Handle key inputs
            try:
                ch = stdscr.getch()
                if ch in (ord("q"), ord("Q")):
                    self.s.stop("User quit via TUI")
                    break
                elif ch in (ord("m"), ord("M")):
                    self.s.add_marker("Manual bookmark from TUI")
                elif ch in (ord("i"), ord("I")):
                    if self.s.request_iperf():
                        self.s.info("iperf3 load test triggered on demand", SEV_INFO)
                    else:
                        self.s.info("iperf3 not configured or unavailable", SEV_WARN)
            except curses.error:
                pass

            self._render(stdscr)

    def _render(self, scr):
        scr.erase()
        max_y, max_x = scr.getmaxyx()
        if max_y < 16 or max_x < 70:
            scr.addstr(0, 0, f"Terminal window too small ({max_x}x{max_y}). Min: 70x16.")
            scr.refresh()
            return

        c_green = curses.color_pair(1) | curses.A_BOLD
        c_yellow = curses.color_pair(2) | curses.A_BOLD
        c_red = curses.color_pair(3) | curses.A_BOLD
        c_cyan = curses.color_pair(4)
        c_banner = curses.color_pair(5) | curses.A_BOLD
        c_status = curses.color_pair(6) | curses.A_BOLD

        # Header banner
        title = f" ETHERNET STABILITY TESTER v{__version__} "
        el = fmt_duration(self.s.elapsed())
        rem = f" | Left: {fmt_duration(self.s.remaining())}" if self.s.cfg.duration else ""
        timing = f" Duration: {el}{rem} "
        space = max(0, max_x - len(title) - len(timing))
        banner_text = title + (" " * space) + timing
        scr.attron(c_banner)
        scr.addstr(0, 0, banner_text[:max_x])
        scr.attroff(c_banner)

        # Line 1: Adapter and Hardware info
        hw = self.s.hw or {}
        adapter_str = f"Interface: {self.s.iface} [{hw.get('driver') or 'N/A'}]"
        hw_desc = f"{hw.get('vendor') or ''} {hw.get('model') or ''}".strip()
        if hw_desc:
            adapter_str += f" - {hw_desc}"
        scr.addstr(1, 1, adapter_str[:max_x - 2], curses.A_BOLD)

        # Line 2: Divider
        scr.addstr(2, 0, "-" * max_x)

        # Lines 3-6: Metrics Dashboard
        lk = self.s.collector("link")
        link_state = lk.state if lk else "N/A"
        speed = f"{lk.speed} Mbps" if lk and lk.speed else "N/A"
        duplex = lk.duplex or "N/A"
        discons = lk.total_disconnects() if lk else 0
        speed_shifts = len(lk.speed_changes) if lk else 0

        # Physical link status
        scr.addstr(3, 2, "Link State    : ")
        if link_state == "UP":
            scr.addstr("UP", c_green)
        elif link_state == "DOWN":
            scr.addstr("DOWN", c_red)
        else:
            scr.addstr(str(link_state), c_yellow)
        scr.addstr(f"  ({speed}, {duplex})")

        scr.addstr(4, 2, f"Disconnects   : ")
        if discons > 0:
            scr.addstr(f"{discons}", c_red)
        else:
            scr.addstr("0", c_green)
        if lk and lk.flaps_missed:
            scr.addstr(f" (incl {lk.flaps_missed} missed fast flaps)", c_yellow)
        if speed_shifts > 0:
            scr.addstr(f"  | Speed changes: {speed_shifts}", c_yellow)

        # Ping metrics (Gateway)
        gw = self.s.collector("ping_gateway")
        scr.addstr(3, 40, "Gateway Ping  : ")
        if not gw or not gw.available:
            scr.addstr("N/A", c_yellow)
        else:
            if gw.ip:
                tgt = gw.ip
                scr.addstr(f"{tgt}")
            elif link_state != "UP":
                scr.addstr("waiting (Link DOWN)", c_yellow)
            else:
                scr.addstr("waiting for DHCP / IP...", c_yellow)
            scr.addstr(4, 40, "Packet Loss   : ")
            loss_pct = (gw.lost * 100.0 / gw.sent) if gw.sent else 0.0
            loss_str = f"{loss_pct:.2f}% ({gw.lost}/{gw.sent})"
            if loss_pct > 2.0:
                scr.addstr(loss_str, c_red)
            elif loss_pct > 0.0:
                scr.addstr(loss_str, c_yellow)
            else:
                scr.addstr(loss_str, c_green)

            # Latency stats
            scr.addstr(5, 40, "Latency RTT   : ")
            if gw.rtt_min is not None:
                avg = gw.rtt_sum / max(1, gw.received)
                lat_str = f"min {gw.rtt_min:.1f} / avg {avg:.1f} / max {gw.rtt_max:.1f} ms"
                scr.addstr(lat_str)
            else:
                scr.addstr("N/A")

        # RX/TX Throughput and Errors
        cnt = self.s.collector("counters")
        rx_rate = fmt_rate(cnt.rx_bps) if cnt and cnt.rx_bps is not None else "0 bps"
        tx_rate = fmt_rate(cnt.tx_bps) if cnt and cnt.tx_bps is not None else "0 bps"
        scr.addstr(5, 2, f"Throughput    : RX {rx_rate:<12} TX {tx_rate}")

        # Hardware error counters
        scr.addstr(6, 2, "Hardware Errs : ")
        if cnt and cnt.available:
            rep = cnt.cs.report()
            crc = (rep.get("rx_crc_errors") or {}).get("delta")
            frame = (rep.get("rx_frame_errors") or {}).get("delta")
            carrier = (rep.get("tx_carrier_errors") or {}).get("delta")
            rx_err = (rep.get("rx_errors") or {}).get("delta")
            tx_err = (rep.get("tx_errors") or {}).get("delta")
            err_parts = []
            if crc:
                err_parts.append(f"CRC:+{crc}")
            if frame:
                err_parts.append(f"Frame:+{frame}")
            if carrier:
                err_parts.append(f"Carrier:+{carrier}")
            if rx_err:
                err_parts.append(f"RX_err:+{rx_err}")
            if tx_err:
                err_parts.append(f"TX_err:+{tx_err}")

            if err_parts:
                scr.addstr(", ".join(err_parts), c_red)
            else:
                scr.addstr("0 (No CRC / frame / carrier errors)", c_green)
        else:
            scr.addstr("N/A", c_yellow)

        # Line 7: Divider before event log
        scr.addstr(7, 0, "=" * max_x)
        scr.addstr(7, 2, " [ Real-Time Event History ] ", curses.A_BOLD | c_cyan)

        # Lines 8 to max_y - 2: Scrolling Event Log
        log_height = max_y - 9
        with self.lock:
            recent_events = self.events[-log_height:] if self.events else []

        start_row = 8
        for idx, ev in enumerate(recent_events):
            row = start_row + idx
            if row >= max_y - 1:
                break
            t = short_time(ev.ts)
            sev = ev.severity
            sev_attr = curses.A_NORMAL
            if sev == SEV_CRIT:
                sev_attr = c_red
            elif sev == SEV_WARN:
                sev_attr = c_yellow
            elif sev == SEV_INFO:
                sev_attr = c_green

            scr.addstr(row, 1, f"[{t}] ")
            scr.addstr(f"[{sev:<4}] ", sev_attr)
            scr.addstr(f"{ev.type:<15} ")
            max_msg_len = max(10, max_x - 30)
            scr.addstr(f"{ev.message[:max_msg_len]}")

        # Footer Status / Keybindings line
        footer = " Keys: [Q] Quit & Export Report | [M] Add Marker | [I] Run iperf3 "
        if max_x > len(footer):
            footer = footer + " " * (max_x - len(footer))
        scr.attron(c_status)
        try:
            scr.addstr(max_y - 1, 0, footer[:max_x])
        except curses.error:
            pass
        scr.attroff(c_status)

        scr.refresh()


def run_ui(session):
    """Run chosen UI mode."""
    mode = session.cfg.ui
    if mode == "auto":
        mode = "tui" if sys.stdout.isatty() and os.environ.get("TERM") != "dumb" else "plain"

    if mode == "tui":
        ui = CursesTUI(session)
    else:
        ui = PlainTextUI(session)

    ui.run()
