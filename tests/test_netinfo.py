"""Tests for netinfo and ethtool parsing."""
import unittest

from ethtester.netinfo import (
    parse_ethtool_kv,
    parse_ethtool_stats,
    parse_speed,
    summarize_settings,
)


class TestNetinfoParsing(unittest.TestCase):
    def test_parse_ethtool_kv(self):
        sample = """Settings for eth0:
\tSupported ports: [ TP MII ]
\tSupported link modes:   10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\t                        1000baseT/Full
\tSpeed: 1000Mb/s
\tDuplex: Full
\tAuto-negotiation: on
\tLink detected: yes
"""
        kv = parse_ethtool_kv(sample)
        self.assertEqual(kv["Speed"], "1000Mb/s")
        self.assertEqual(kv["Duplex"], "Full")
        self.assertEqual(kv["Auto-negotiation"], "on")
        self.assertEqual(kv["Link detected"], "yes")
        self.assertIn("1000baseT/Full", kv["Supported link modes"])

    def test_summarize_settings(self):
        kv = {
            "Speed": "1000Mb/s",
            "Duplex": "Full",
            "Auto-negotiation": "on",
            "Link detected": "yes",
            "Supported link modes": "100baseT/Full 1000baseT/Full",
            "Advertised link modes": "1000baseT/Full",
            "Link partner advertised link modes": "1000baseT/Full",
            "Link Down Events": "3",
        }
        s = summarize_settings(kv)
        self.assertEqual(s["speed_mbps"], 1000)
        self.assertEqual(s["duplex"], "Full")
        self.assertEqual(s["autoneg"], "on")
        self.assertEqual(s["link_down_events"], 3)
        self.assertEqual(s["advertised_modes"], ["1000baseT/Full"])

    def test_parse_ethtool_stats(self):
        sample = """NIC statistics:
     tx_packets: 1234
     rx_packets: 5678
     rx_crc_errors: 0
     rx_missed_errors: 2
Standard stats for eth0:
  eth-mac:
    FramesTransmittedOK: 1234
    FramesReceivedOK: 5678
"""
        st = parse_ethtool_stats(sample)
        self.assertEqual(st["tx_packets"], 1234)
        self.assertEqual(st["rx_packets"], 5678)
        self.assertEqual(st["rx_crc_errors"], 0)
        self.assertEqual(st["rx_missed_errors"], 2)
        self.assertEqual(st["eth-mac.FramesTransmittedOK"], 1234)

    def test_parse_speed(self):
        self.assertEqual(parse_speed("1000Mb/s"), 1000)
        self.assertEqual(parse_speed("100Mb/s"), 100)
        self.assertEqual(parse_speed("10Mb/s"), 10)
        self.assertIsNone(parse_speed("Unknown!"))
        self.assertIsNone(parse_speed(None))


if __name__ == "__main__":
    unittest.main()
