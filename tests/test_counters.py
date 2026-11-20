"""Tests for monotonic counter tracking and reset handling."""
import unittest

from ethtester.collectors.counterset import CounterSet


class TestCounterSet(unittest.TestCase):
    def test_initial_and_deltas(self):
        cs = CounterSet()
        deltas = cs.update({"rx_crc_errors": 10, "rx_packets": 100})
        self.assertEqual(deltas["rx_crc_errors"], 0)
        self.assertEqual(deltas["rx_packets"], 0)
        self.assertEqual(cs.total("rx_crc_errors"), 0)

        # Increment
        deltas2 = cs.update({"rx_crc_errors": 15, "rx_packets": 250})
        self.assertEqual(deltas2["rx_crc_errors"], 5)
        self.assertEqual(deltas2["rx_packets"], 150)
        self.assertEqual(cs.total("rx_crc_errors"), 5)
        self.assertEqual(cs.total("rx_packets"), 150)

    def test_counter_reset_on_usb_unplug(self):
        cs = CounterSet()
        cs.update({"rx_packets": 500})
        cs.update({"rx_packets": 600})  # +100
        # Reset occurs (e.g. interface reset back to 50)
        deltas = cs.update({"rx_packets": 50})
        self.assertEqual(deltas["rx_packets"], 50)
        self.assertEqual(cs.total("rx_packets"), 150)  # 100 + 50
        self.assertEqual(cs.resets["rx_packets"], 1)

    def test_unsupported_counter_none(self):
        cs = CounterSet()
        deltas = cs.update({"rx_crc_errors": None})
        self.assertIsNone(deltas["rx_crc_errors"])
        self.assertIsNone(cs.total("rx_crc_errors"))


if __name__ == "__main__":
    unittest.main()
