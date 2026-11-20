"""Tests for link transitions and report rendering."""
import unittest

from ethtester.report import build_summary, render_summary
from ethtester.session import Config, Session
from ethtester.sim import SimulatedPlatform


class TestReportAndDiagnosis(unittest.TestCase):
    def test_stable_report(self):
        platform = SimulatedPlatform(scenario="default")
        cfg = Config(interface="eth0", duration=2.0, label="unit_test")
        session = Session(cfg, platform, "eth0")
        session.start()
        session.wait(2.0)
        session.finalize()

        s = build_summary(session, final=True)
        self.assertEqual(s["diagnosis"]["verdict"], "STABLE")
        self.assertEqual(s["diagnosis"]["physical_disconnects"], 0)
        self.assertEqual(s["diagnosis"]["speed_changes"], 0)

        txt = render_summary(s)
        self.assertIn("DIAGNOSIS VERDICT: [STABLE]", txt)
        self.assertIn("Total Disconnects (Estimate): 0", txt)

    def test_unstable_report(self):
        platform = SimulatedPlatform(scenario="unstable_link")
        cfg = Config(interface="eth0", duration=6.0, label="unit_test_unstable")
        session = Session(cfg, platform, "eth0")
        session.start()
        session.wait(6.0)
        session.finalize()

        s = build_summary(session, final=True)
        self.assertEqual(s["diagnosis"]["verdict"], "UNSTABLE")
        self.assertGreater(s["diagnosis"]["physical_disconnects"], 0)

        txt = render_summary(s)
        self.assertIn("DIAGNOSIS VERDICT: [UNSTABLE]", txt)
        self.assertTrue(s["diagnosis"]["layer1_physical_issue"])


if __name__ == "__main__":
    unittest.main()
