"""Tests for duration parsing and utilities."""
import unittest

from ethtester.util import fmt_duration, fmt_rate, parse_duration


class TestUtil(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("30"), 30.0)
        self.assertEqual(parse_duration("30s"), 30.0)
        self.assertEqual(parse_duration("15m"), 900.0)
        self.assertEqual(parse_duration("1h"), 3600.0)
        self.assertEqual(parse_duration("1h30m"), 5400.0)
        self.assertEqual(parse_duration("1d"), 86400.0)
        with self.assertRaises(ValueError):
            parse_duration("invalid")
        with self.assertRaises(ValueError):
            parse_duration("")

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(0), "00:00:00")
        self.assertEqual(fmt_duration(65), "00:01:05")
        self.assertEqual(fmt_duration(3665), "01:01:05")
        self.assertEqual(fmt_duration(86400 + 3600), "1d 01:00:00")

    def test_fmt_rate(self):
        self.assertEqual(fmt_rate(1_000_000_000), "1.00 Gbit/s")
        self.assertEqual(fmt_rate(100_000_000), "100.00 Mbit/s")
        self.assertEqual(fmt_rate(50_000), "50.00 kbit/s")


if __name__ == "__main__":
    unittest.main()
