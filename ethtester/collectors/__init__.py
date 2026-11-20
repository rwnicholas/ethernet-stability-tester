"""Collector registry.  Add new diagnostic collectors here."""
from .base import Collector
from .counters import DriverStatsCollector, InterfaceStatsCollector
from .iperf import IperfCollector
from .kernel import KernelLogCollector
from .link import LinkCollector
from .ping import PingCollector
from .system import SystemCollector


def build_collectors(session):
    """Ordered list of collector instances for a session."""
    cfg = session.cfg
    cols = [LinkCollector(session), InterfaceStatsCollector(session), DriverStatsCollector(session),
            KernelLogCollector(session)]
    if not cfg.no_ping:
        cols.append(PingCollector(session, "gateway", cfg.gateway))
        if cfg.target2:
            cols.append(PingCollector(session, "target2", cfg.target2))
    cols.append(IperfCollector(session))
    cols.append(SystemCollector(session))
    return cols


__all__ = ["Collector", "build_collectors"]
