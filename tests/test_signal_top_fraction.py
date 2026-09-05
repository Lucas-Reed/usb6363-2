"""峰顶最高百分比均值的纯算法测试。"""

from __future__ import annotations

import unittest

import numpy as np

from two_peak.signal import measure_top_fraction_mean


class TopFractionMeasurementTests(unittest.TestCase):
    def test_uses_highest_values_inside_requested_window(self) -> None:
        """只应选择窗口内部最高的点，窗口外大值不得参与。"""

        signal = np.asarray([100.0, 200.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = measure_top_fraction_mean(signal, 2, 7, percentage=50.0)

        self.assertEqual(result.point_count, 6)
        self.assertEqual(result.selected_point_count, 3)
        self.assertAlmostEqual(result.value, 5.0)

    def test_251_points_at_ten_percent_selects_25_points(self) -> None:
        """按照研究约定，251点窗口的10%使用四舍五入后的25点。"""

        signal = np.arange(251, dtype=float)
        result = measure_top_fraction_mean(signal, 0, 250, percentage=10.0)

        self.assertEqual(result.selected_point_count, 25)
        self.assertAlmostEqual(result.value, float(np.mean(np.arange(226, 251))))

    def test_percentage_is_adjustable(self) -> None:
        signal = np.arange(1, 11, dtype=float)

        top_ten = measure_top_fraction_mean(signal, 0, 9, percentage=10.0)
        top_thirty = measure_top_fraction_mean(signal, 0, 9, percentage=30.0)
        all_points = measure_top_fraction_mean(signal, 0, 9, percentage=100.0)

        self.assertEqual(top_ten.selected_point_count, 1)
        self.assertAlmostEqual(top_ten.value, 10.0)
        self.assertEqual(top_thirty.selected_point_count, 3)
        self.assertAlmostEqual(top_thirty.value, 9.0)
        self.assertAlmostEqual(all_points.value, 5.5)

    def test_rejects_invalid_percentage_and_non_finite_data(self) -> None:
        signal = np.arange(10, dtype=float)
        for percentage in (0.0, -1.0, 100.1, float("nan")):
            with self.subTest(percentage=percentage):
                with self.assertRaises(ValueError):
                    measure_top_fraction_mean(signal, 0, 9, percentage=percentage)

        signal[5] = np.nan
        with self.assertRaisesRegex(ValueError, "non-finite"):
            measure_top_fraction_mean(signal, 0, 9, percentage=10.0)


if __name__ == "__main__":
    unittest.main()
