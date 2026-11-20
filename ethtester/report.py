"""Report generators: summary JSON builder and human-readable text formatter."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__
from .util import fmt_duration, fmt_num, fmt_rate, iso


def build_summary(session, final: bool) -> Dict[str, Any]:
    """Compile the complete statistical summary across all collectors."""
    now = session.end_ts or session.platform.read_path("/dev/null") or None
    import time
    end_time = session.end_ts or time.time()
    dur = end_time - session.start_ts if session.start_ts else 0.0

    lk = session.collector("link")
    lk_sum = lk.summary() if lk else {}

    cnt = session.collector("counters")
    cnt_sum = cnt.summary() if cnt else {}

    drv = session.collector("driver_stats")
    drv_sum = drv.summary() if drv else {}

    pings = {}
    for pc in session.ping_collectors():
        pings[pc.label] = pc.summary()

    ipf = session.collector("iperf3")
    ipf_sum = ipf.summary() if ipf else {}

    sys_col = session.collector("system")
    sys_sum = sys_col.summary() if sys_col else {}

    kern = session.collector("kernel")
    kern_sum = kern.summary() if kern else {}

    # Stability diagnostic evaluation
    disconnects = lk_sum.get("disconnects_total_estimate", 0) if lk else 0
    speed_changes = lk_sum.get("speed_changes", 0) if lk else 0
    duplex_changes = lk_sum.get("duplex_changes", 0) if lk else 0

    gw_ping = pings.get("gateway", {})
    gw_loss_pct = gw_ping.get("loss_pct")
    gw_loss_up_pct = gw_ping.get("loss_pct_while_link_up")
    gw_outages = gw_ping.get("outages", 0)
    gw_outages_link_up = gw_ping.get("outages_with_link_up", 0)

    # Key error metrics
    cnt_data = cnt_sum.get("counters", {}) if cnt_sum.get("available") else {}
    rx_crc = (cnt_data.get("rx_crc_errors") or {}).get("delta")
    rx_frame = (cnt_data.get("rx_frame_errors") or {}).get("delta")
    tx_carrier = (cnt_data.get("tx_carrier_errors") or {}).get("delta")
    rx_errors = (cnt_data.get("rx_errors") or {}).get("delta")
    tx_errors = (cnt_data.get("tx_errors") or {}).get("delta")

    findings = []
    verdict = "STABLE"

    if disconnects > 0:
        verdict = "UNSTABLE"
        findings.append(f"Physical link dropped {disconnects} time(s).")
    if speed_changes > 0:
        if verdict != "UNSTABLE":
            verdict = "DEGRADED"
        findings.append(f"Link speed changed {speed_changes} time(s) during test.")
    if duplex_changes > 0:
        verdict = "UNSTABLE"
        findings.append(f"Duplex mode changed {duplex_changes} time(s).")

    if rx_crc and rx_crc > 0:
        if verdict != "UNSTABLE":
            verdict = "DEGRADED"
        findings.append(f"Detected {rx_crc} RX CRC errors (cable/PHY signal integrity problem).")
    if rx_frame and rx_frame > 0:
        if verdict != "UNSTABLE":
            verdict = "DEGRADED"
        findings.append(f"Detected {rx_frame} RX frame alignment errors.")
    if tx_carrier and tx_carrier > 0:
        if verdict != "UNSTABLE":
            verdict = "DEGRADED"
        findings.append(f"Detected {tx_carrier} TX carrier errors.")
    if (rx_errors and rx_errors > 0) or (tx_errors and tx_errors > 0):
        findings.append(f"Interface reported RX errors: {rx_errors or 0}, TX errors: {tx_errors or 0}.")

    if gw_loss_up_pct is not None and gw_loss_up_pct > 1.0:
        verdict = "UNSTABLE" if gw_loss_up_pct > 5.0 else "DEGRADED"
        findings.append(
            f"Gateway packet loss while link was UP is {gw_loss_up_pct:.2f}% "
            f"({gw_outages_link_up} outage(s) while physical link remained active)."
        )
    elif gw_loss_pct is not None and gw_loss_pct > 0.0 and disconnects > 0:
        findings.append(
            f"Gateway packet loss was {gw_loss_pct:.2f}%, primarily occurring during physical link down periods."
        )

    if not findings:
        findings.append("No physical link drops, speed shifts, hardware errors, or significant packet losses detected.")

    return {
        "tool": {"name": "Ethernet Stability Tester", "version": __version__},
        "report_type": "final" if final else "interim",
        "generated_at": iso(end_time),
        "test": {
            "label": session.cfg.label or None,
            "interface": session.iface,
            "start": iso(session.start_ts),
            "end": iso(end_time) if final else None,
            "duration_seconds": round(dur, 2),
            "duration_formatted": fmt_duration(dur),
            "planned_duration_seconds": session.cfg.duration,
            "end_reason": session.end_reason if final else "running",
            "hardware": session.hw,
        },
        "diagnosis": {
            "verdict": verdict,
            "summary_findings": findings,
            "physical_disconnects": disconnects,
            "speed_changes": speed_changes,
            "packet_loss_pct_total": gw_loss_pct,
            "packet_loss_pct_while_link_up": gw_loss_up_pct,
            "layer1_physical_issue": disconnects > 0 or speed_changes > 0 or bool(rx_crc and rx_crc > 0),
            "layer3_packet_issue_isolated": bool(gw_loss_up_pct and gw_loss_up_pct > 1.0 and disconnects == 0),
        },
        "link": lk_sum,
        "ping": pings,
        "interface_statistics": cnt_sum,
        "driver_statistics": drv_sum,
        "traffic_load_iperf3": ipf_sum,
        "system": sys_sum,
        "kernel_log": kern_sum,
    }


def render_summary(s: Dict[str, Any]) -> str:
    """Format summary dictionary as an attractive, readable plain-text report."""
    lines = []
    w = 78
    sep = "=" * w
    sub = "-" * w

    lines.append(sep)
    lines.append(" ETHERNET STABILITY TESTER - SUMMARY REPORT")
    lines.append(sep)

    t = s.get("test", {})
    hw = t.get("hardware", {}) or {}
    diag = s.get("diagnosis", {})

    lines.append(f" Status        : {s.get('report_type', 'final').upper()}")
    if t.get("label"):
        lines.append(f" Test Label    : {t.get('label')}")
    lines.append(f" Interface     : {t.get('interface')} ({hw.get('driver') or 'unknown driver'})")
    if hw.get("vendor") or hw.get("model"):
        lines.append(f" Hardware      : {hw.get('vendor') or ''} {hw.get('model') or ''}".strip())
    if hw.get("bus_info"):
        lines.append(f" Bus Address   : {hw.get('bus_info')} ({hw.get('bus') or 'unknown bus'})")
    lines.append(f" Start Time    : {t.get('start')}")
    lines.append(f" End Time      : {t.get('end') or 'In progress'}")
    lines.append(f" Duration      : {t.get('duration_formatted')} ({t.get('duration_seconds')}s)")
    lines.append(f" End Reason    : {t.get('end_reason')}")

    lines.append("")
    lines.append(sub)
    lines.append(f" DIAGNOSIS VERDICT: [{diag.get('verdict', 'UNKNOWN')}]")
    lines.append(sub)
    for f in diag.get("summary_findings", []):
        lines.append(f" * {f}")

    # Physical Link Section
    lk = s.get("link", {})
    lines.append("")
    lines.append(sub)
    lines.append(" 1. PHYSICAL LINK STABILITY")
    lines.append(sub)
    lines.append(f" Final Link State            : {lk.get('final_state', 'N/A')}")
    lines.append(f" Total Disconnects (Estimate): {fmt_num(lk.get('disconnects_total_estimate'))}")
    lines.append(f"  - Observed by Polling      : {fmt_num(lk.get('disconnects_observed_by_polling'))}")
    lines.append(f"  - Missed Flaps (Fast Drops): {fmt_num(lk.get('short_flaps_between_polls'))}")
    lines.append(f"  - Kernel Carrier Down Delta: {fmt_num(lk.get('kernel_carrier_down_count_delta'))}")
    lines.append(f"  - Ethtool Link Down Delta  : {fmt_num(lk.get('ethtool_link_down_events_delta'))}")
    lines.append(f" Speed Changes Count         : {fmt_num(lk.get('speed_changes'))}")
    for sc in lk.get("speed_change_list", []):
        lines.append(f"    @ {sc.get('time')}: {sc.get('from_mbps')} Mbps -> {sc.get('to_mbps')} Mbps")
    lines.append(f" Duplex Changes Count        : {fmt_num(lk.get('duplex_changes'))}")
    for dc in lk.get("duplex_change_list", []):
        lines.append(f"    @ {dc.get('time')}: {dc.get('from')} -> {dc.get('to')}")
    lines.append(f" Link Availability           : {fmt_num(lk.get('link_availability_pct'), '%.3f%%')}")
    lines.append(f" Total Link UP Time          : {fmt_duration(lk.get('link_up_seconds'))}")
    lines.append(f" Total Link DOWN Time        : {fmt_duration(lk.get('link_down_seconds'))}")
    if lk.get("longest_down_s") is not None:
        lines.append(f" Longest Single Outage (Link): {lk.get('longest_down_s'):.3f}s")

    # Connectivity / ICMP Section
    pings = s.get("ping", {})
    lines.append("")
    lines.append(sub)
    lines.append(" 2. CONNECTIVITY & ICMP REACHABILITY")
    lines.append(sub)
    for lbl, pdata in pings.items():
        if not pdata.get("available"):
            lines.append(f" Target [{lbl}]: N/A ({pdata.get('reason')})")
            continue
        tgt = pdata.get("target") or pdata.get("configured")
        lines.append(f" Target [{lbl} - {tgt}]:")
        lines.append(f"   Probes Sent / Received    : {pdata.get('sent')} / {pdata.get('received')}")
        lines.append(f"   Total Packet Loss         : {fmt_num(pdata.get('loss_pct'), '%.3f%%')} ({pdata.get('lost')} lost)")
        lines.append(f"   * Loss While Link Was UP  : {fmt_num(pdata.get('loss_pct_while_link_up'), '%.3f%%')} ({pdata.get('lost_while_link_up')} lost)")
        lines.append(f"   * Loss While Link Was DOWN: {pdata.get('lost_while_link_down')} lost")
        lines.append(f"   Latency Min / Avg / Max   : {fmt_num(pdata.get('rtt_min_ms'), '%.2f')} / "
                     f"{fmt_num(pdata.get('rtt_avg_ms'), '%.2f')} / {fmt_num(pdata.get('rtt_max_ms'), '%.2f')} ms")
        lines.append(f"   Latency Percentiles       : p50={fmt_num(pdata.get('rtt_p50_ms'), '%.2f')}ms, "
                     f"p95={fmt_num(pdata.get('rtt_p95_ms'), '%.2f')}ms, p99={fmt_num(pdata.get('rtt_p99_ms'), '%.2f')}ms")
        lines.append(f"   Jitter (Mean Diff)        : {fmt_num(pdata.get('jitter_ms'), '%.3f')} ms")
        lines.append(f"   Unreachable Outages       : {pdata.get('outages', 0)} ({pdata.get('outages_with_link_up', 0)} with link UP, "
                     f"{pdata.get('outages_with_link_down', 0)} with link DOWN)")
        lines.append(f"   Total Unreachable Time    : {pdata.get('unreachable_seconds', 0):.2f}s")
        if pdata.get("excluded_suspend", 0) > 0:
            lines.append(f"   Excluded Suspend Probes   : {pdata.get('excluded_suspend')}")

    # Interface Counters Section
    cnt = s.get("interface_statistics", {})
    lines.append("")
    lines.append(sub)
    lines.append(" 3. INTERFACE ERROR COUNTERS (/sys/class/net)")
    lines.append(sub)
    if not cnt.get("available"):
        lines.append(f" Generic Counters: N/A ({cnt.get('reason')})")
    else:
        cd = cnt.get("counters", {})
        err_keys = [
            "rx_crc_errors", "rx_frame_errors", "rx_errors", "tx_errors",
            "rx_dropped", "tx_dropped", "collisions", "tx_carrier_errors",
            "rx_missed_errors", "rx_fifo_errors", "tx_fifo_errors"
        ]
        any_err = False
        for k in err_keys:
            info = cd.get(k)
            if not info:
                continue
            delta = info.get("delta")
            supp = info.get("support", "")
            if delta is not None and delta > 0:
                any_err = True
                lines.append(f"   ! {k:<20}: delta = +{delta:<6} (initial={info.get('initial')}, final={info.get('final')}) [{supp}]")
            elif delta is None:
                lines.append(f"     {k:<20}: N/A [{supp}]")
        if not any_err:
            lines.append("   All hardware error counters remained 0 during the test.")

    # Driver Specific Counters
    drv = s.get("driver_statistics", {})
    if drv.get("available"):
        inc = drv.get("error_counters_increased", {})
        if inc:
            lines.append("")
            lines.append("   Driver/Standard Counters Increased:")
            for k, val in inc.items():
                lines.append(f"   ! {k}: +{val}")

    # iperf3 Section
    ipf = s.get("traffic_load_iperf3", {})
    if ipf.get("available"):
        lines.append("")
        lines.append(sub)
        lines.append(f" 4. TRAFFIC LOAD TESTING (iperf3 -> {ipf.get('server')})")
        lines.append(sub)
        lines.append(f" Total Test Runs Completed   : {ipf.get('runs')}")
        tests = ipf.get("tests", {})
        for tname, tres in tests.items():
            lines.append(f"   Test [{tname}]:")
            lines.append(f"     Throughput (Avg/Max)    : {fmt_rate(tres.get('throughput_avg_bps'))} / {fmt_rate(tres.get('throughput_max_bps'))}")
            if "loss_avg_pct" in tres and tres.get("loss_avg_pct") is not None:
                lines.append(f"     UDP Loss (Avg/Max)      : {tres.get('loss_avg_pct'):.2f}% / {tres.get('loss_max_pct'):.2f}%")
            if "jitter_avg_ms" in tres and tres.get("jitter_avg_ms") is not None:
                lines.append(f"     UDP Jitter (Avg/Max)    : {tres.get('jitter_avg_ms'):.3f} ms / {tres.get('jitter_max_ms'):.3f} ms")
            if "retransmits_total" in tres and tres.get("retransmits_total") is not None:
                lines.append(f"     TCP Retransmits Total   : {tres.get('retransmits_total')}")

    # Kernel Section
    klog = s.get("kernel_log", {})
    if klog.get("available"):
        lines.append("")
        lines.append(sub)
        lines.append(" 5. KERNEL LOG DIAGNOSTICS")
        lines.append(sub)
        lines.append(f" Messages captured during test: {klog.get('messages_during_test', 0)} ({klog.get('warning_messages_during_test', 0)} warnings/severe)")
        lines.append(f" Pre-test boot messages matching: {klog.get('matching_messages_before_start_this_boot', 0)}")

    lines.append("")
    lines.append(sep)
    lines.append(" END OF REPORT")
    lines.append(sep)
    return "\n".join(lines) + "\n"
