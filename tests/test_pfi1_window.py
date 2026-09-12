import unittest

from two_peak.pfi1_window import ExponentialMovingAverage, PiFeedback, analyze_pulse


class Pfi1WindowTests(unittest.TestCase):
    def test_fixed_threshold_pulse(self):
        result = analyze_pulse([0, 0.2, 1.2, 1.5, 0.1], threshold=1.0)
        self.assertTrue(result.has_pulse)
        self.assertEqual(result.rising_index, 2)
        self.assertEqual(result.falling_index, 4)
        self.assertAlmostEqual(result.pulse_mean, 1.35)

    def test_no_pulse_uses_window_mean(self):
        result = analyze_pulse([0.1, 0.2, 0.3], threshold=1.0)
        self.assertFalse(result.has_pulse)
        self.assertAlmostEqual(result.pulse_mean, result.mean_value)

    def test_ema_and_pi(self):
        ema = ExponentialMovingAverage(0.5)
        self.assertEqual(ema.update(2), 2)
        self.assertEqual(ema.update(4), 3)
        pi = PiFeedback(target=1, initial_voltage=0, min_voltage=-1, max_voltage=1,
                        max_step_v=0.1, kp=1)
        self.assertAlmostEqual(pi.update(0, 1), 0.1)


if __name__ == "__main__":
    unittest.main()
