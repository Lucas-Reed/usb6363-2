import unittest

from two_peak.pfi_monitor import PfiMonitorConfig


class PfiMonitorTests(unittest.TestCase):
    def test_pfi1_interval_must_be_shorter(self):
        with self.assertRaises(ValueError):
            PfiMonitorConfig(pfi0_poll_interval_s=0.01, pfi1_poll_interval_s=0.01)

    def test_intervals_are_configurable(self):
        config = PfiMonitorConfig(pfi0_poll_interval_s=0.2, pfi1_poll_interval_s=0.03)
        self.assertEqual(config.pfi0_poll_interval_s, 0.2)
        self.assertEqual(config.pfi1_poll_interval_s, 0.03)


if __name__ == "__main__":
    unittest.main()
