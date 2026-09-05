"""双路功率锁定反馈字段读取的无硬件测试。"""

from __future__ import annotations

import csv
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from two_peak.power_lock import (
    RATIO_TARGET_MODE,
    PowerLockController,
    _read_feedback_value,
    _validate_controller,
)


class _FakeDaq:
    """记录 AO 写入但不接触真实 USB-6363。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.writes: list[dict[str, Any]] = []

    def write_ao(self, channel: str, value: float, min_val: float, max_val: float) -> dict[str, Any]:
        row = {
            "channel": channel,
            "value": float(value),
            "min_val": float(min_val),
            "max_val": float(max_val),
        }
        with self._lock:
            self.writes.append(row)
        return row

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.writes]


class _FakeTrendLogger:
    """每次查询都提供一个新的有效面积统计点。"""

    def __init__(self) -> None:
        self._frame_id = 0
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._frame_id += 1
            frame_id = self._frame_id
        return {
            "running": True,
            "settings": {"record_hz": 100.0},
            "latest_stats": {
                "frame_id": frame_id,
                "unix_time": 1000.0 + frame_id * 0.01,
                "window_revision": 1,
                "area_ema": 8.0,
            },
        }


class _FixedFrameTrendLogger:
    """始终返回同一个 frame_id，用来验证旧数据不会被重复积分。"""

    def status(self) -> dict[str, Any]:
        return {
            "running": True,
            "settings": {"record_hz": 1.0},
            "latest_stats": {
                "frame_id": 1,
                "unix_time": 1001.0,
                "window_revision": 1,
                "area_ema": 8.0,
            },
        }


class _RatioTrendLogger:
    """提供同一时刻的 A/B 双峰统计，供比例锁定线程测试。"""

    def __init__(self) -> None:
        self._frame_id = 0
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._frame_id += 1
            frame_id = self._frame_id
        return {
            "running": True,
            "settings": {"record_hz": 100.0},
            "latest_stats": {
                "frame_id": frame_id,
                "unix_time": 1000.0 + frame_id * 0.01,
                "window_revision": 1,
                "area_ema": 8.0,
                "area2_ema": 3.0,
            },
        }


def _controller(**overrides: Any) -> dict[str, Any]:
    """生成一路可用于测试的完整 PI 配置。"""

    result = {
        "name": "EOM",
        "channel": "ao0",
        "feedback_field": "area_ema",
        "target_mode": "fixed",
        "reference_field": "",
        "follow_ratio": 1.0,
        "target": 10.0,
        "initial_voltage": 2.0,
        "min_voltage": 1.0,
        "max_voltage": 3.5,
        "direction": 1.0,
        "max_step_v": 0.1,
        "kp": 1.0,
        "ki": 0.0,
        "enabled": True,
    }
    result.update(overrides)
    return result


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    """等待后台 PI 线程达到测试状态。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待功率锁定线程超时")


class PowerLockFeedbackTests(unittest.TestCase):
    def test_reads_top_feedback_for_both_windows(self) -> None:
        """A/B 两个窗口的 Top EMA 都应能直接作为锁定反馈。"""

        latest = {
            "top_ema": 0.041,
            "top2_ema": 0.026,
        }

        self.assertAlmostEqual(_read_feedback_value(latest, "top_ema"), 0.041)
        self.assertAlmostEqual(_read_feedback_value(latest, "top2_ema"), 0.026)

    def test_top_ema_falls_back_to_recent_mean_when_disabled(self) -> None:
        """EMA关闭后，保持和面积反馈一致的自动退回行为。"""

        latest = {
            "top_ema": None,
            "top_mean": 0.042,
            "top2_ema": None,
            "top2_mean": 0.027,
        }

        self.assertAlmostEqual(_read_feedback_value(latest, "top_ema"), 0.042)
        self.assertAlmostEqual(_read_feedback_value(latest, "top2_ema"), 0.027)

    def test_missing_top_feedback_returns_none(self) -> None:
        self.assertIsNone(_read_feedback_value({}, "top_ema"))
        self.assertIsNone(_read_feedback_value({}, "top2_ema"))

    def test_peak_height_ema_falls_back_to_recent_mean(self) -> None:
        """峰高 EMA 关闭时也应退回最近 N 帧均值。"""

        latest = {
            "peak_height_ema": None,
            "peak_height_mean": 0.052,
            "peak2_height_ema": None,
            "peak2_height_mean": 0.031,
        }
        self.assertAlmostEqual(_read_feedback_value(latest, "peak_height_ema"), 0.052)
        self.assertAlmostEqual(_read_feedback_value(latest, "peak2_height_ema"), 0.031)

    def test_ratio_mode_rejects_mismatched_measurement_types(self) -> None:
        """面积锁定峰不能错误引用采样峰的 Top 或峰高。"""

        with self.assertRaisesRegex(ValueError, "reference_field"):
            _validate_controller(
                _controller(
                    feedback_field="area2_ema",
                    target_mode=RATIO_TARGET_MODE,
                    reference_field="top_ema",
                    follow_ratio=0.5,
                )
            )


class PowerLockRuntimeUpdateTests(unittest.TestCase):
    def test_ratio_lock_uses_reference_peak_without_writing_a_second_ao(self) -> None:
        """比例模式只根据采样峰计算动态目标，并只写锁定峰的一路 AO。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _FakeTrendLogger())  # type: ignore[arg-type]
        controller = _controller(
            name="AOM",
            channel="ao1",
            feedback_field="area2_ema",
            target_mode=RATIO_TARGET_MODE,
            reference_field="area_ema",
            follow_ratio=0.5,
        )
        state = lock._update_one_controller(
            controller,
            {"voltage": 2.0, "integral": 0.0, "measurement_revision": 1},
            {
                "frame_id": 12,
                "window_revision": 1,
                "area_ema": 8.0,
                "area2_ema": 3.0,
            },
            dt=1.0,
        )

        self.assertAlmostEqual(state["reference_value"], 8.0)
        self.assertAlmostEqual(state["target"], 4.0)
        self.assertAlmostEqual(state["actual_ratio"], 0.375)
        self.assertAlmostEqual(state["relative_error"], 0.25)
        self.assertEqual([row["channel"] for row in daq.snapshot()], ["ao1"])

    def test_measurement_window_revision_resets_integral(self) -> None:
        """面积边界改变后，PI 只能从新测量口径重新累计积分。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _FakeTrendLogger())  # type: ignore[arg-type]
        state = lock._update_one_controller(
            _controller(kp=0.0, ki=1.0),
            {
                "voltage": 2.0,
                "integral": 5.0,
                "measurement_revision": 1,
            },
            {"area_ema": 8.0, "window_revision": 2},
            dt=1.0,
        )
        self.assertAlmostEqual(state["integral"], 0.2)
        self.assertEqual(state["measurement_revision"], 2)

    def test_runtime_update_preserves_voltage_and_uses_new_parameters(self) -> None:
        """热更新不能重写初始电压，新参数应从下一轮开始生效。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _FakeTrendLogger())  # type: ignore[arg-type]
        lock.start([_controller()], update_s=0.01)
        try:
            _wait_until(lambda: lock.status()["iterations"] >= 3)
            voltage_before = float(lock.status()["states"][0]["voltage"])
            self.assertGreater(voltage_before, 2.0)

            write_index = len(daq.snapshot())
            updated = lock.update_parameters(
                [_controller(target=9.0, kp=0.0, ki=0.0, max_step_v=0.02)],
                update_s=0.005,
            )
            self.assertEqual(updated["parameter_revision"], 2)
            self.assertEqual(updated["controllers"][0]["target"], 9.0)
            self.assertEqual(updated["controllers"][0]["max_step_v"], 0.02)

            previous_iterations = updated["iterations"]
            _wait_until(lambda: lock.status()["iterations"] > previous_iterations)
            writes_after_update = daq.snapshot()[write_index:]
            self.assertTrue(writes_after_update)
            for row in writes_after_update:
                self.assertAlmostEqual(row["value"], voltage_before)
                self.assertNotAlmostEqual(row["value"], 2.0)
        finally:
            lock.stop()

    def test_runtime_update_rejects_physical_definition_change_atomically(self) -> None:
        """反馈字段等停锁后参数发生变化时，原配置和版本号都不能被部分修改。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _FakeTrendLogger())  # type: ignore[arg-type]
        lock.start([_controller(kp=0.0)], update_s=0.05)
        try:
            _wait_until(lambda: lock.status()["iterations"] >= 1)
            with self.assertRaisesRegex(ValueError, "feedback_field"):
                lock.update_parameters(
                    [_controller(feedback_field="top_ema", target=9.0, kp=0.0)],
                    update_s=0.01,
                )
            status = lock.status()
            self.assertEqual(status["parameter_revision"], 1)
            self.assertEqual(status["controllers"][0]["feedback_field"], "area_ema")
            self.assertEqual(status["controllers"][0]["target"], 10.0)
        finally:
            lock.stop()

    def test_runtime_update_requires_running_lock(self) -> None:
        lock = PowerLockController(_FakeDaq(), _FakeTrendLogger())  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "not running"):
            lock.update_parameters([_controller()], update_s=1.0)

    def test_same_stats_frame_is_not_applied_twice(self) -> None:
        """轮询快于统计频率时，同一个 frame_id 只能触发一次 PI 更新。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _FixedFrameTrendLogger())  # type: ignore[arg-type]
        lock.start([_controller()], update_s=0.01)
        try:
            _wait_until(lambda: lock.status()["iterations"] >= 1)
            time.sleep(0.08)
            self.assertEqual(lock.status()["iterations"], 1)
            # 第一次是启动时写初始值，第二次才是 frame_id=1 的 PI 更新。
            self.assertEqual(len(daq.snapshot()), 2)
        finally:
            lock.stop()

    def test_follow_ratio_can_be_changed_while_running(self) -> None:
        """运行中改变目标比值应保留 AO 电压，并从下一条新统计点生效。"""

        daq = _FakeDaq()
        lock = PowerLockController(daq, _RatioTrendLogger())  # type: ignore[arg-type]
        controller = _controller(
            name="AOM",
            channel="ao1",
            feedback_field="area2_ema",
            target_mode=RATIO_TARGET_MODE,
            reference_field="area_ema",
            follow_ratio=0.5,
            kp=0.0,
            ki=0.0,
        )
        lock.start([controller], update_s=0.01)
        try:
            _wait_until(lambda: lock.status()["iterations"] >= 1)
            voltage_before = float(lock.status()["states"][0]["voltage"])
            updated_controller = dict(controller)
            updated_controller["follow_ratio"] = 0.6
            updated = lock.update_parameters([updated_controller], update_s=0.005)
            self.assertEqual(updated["parameter_revision"], 2)
            self.assertAlmostEqual(updated["states"][0]["voltage"], voltage_before)

            old_iterations = updated["iterations"]
            _wait_until(lambda: lock.status()["iterations"] > old_iterations)
            state = lock.status()["states"][0]
            self.assertAlmostEqual(state["follow_ratio"], 0.6)
            self.assertAlmostEqual(state["target"], 4.8)
            self.assertTrue(all(row["channel"] == "ao1" for row in daq.snapshot()))
        finally:
            lock.stop()

    def test_lock_updates_are_written_to_analysis_csv(self) -> None:
        """锁定日志应包含比值、误差、AO 和源 frame_id。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            daq = _FakeDaq()
            lock = PowerLockController(
                daq,
                _RatioTrendLogger(),  # type: ignore[arg-type]
                output_dir=Path(temp_dir),
            )
            controller = _controller(
                name="AOM",
                channel="ao1",
                feedback_field="area2_ema",
                target_mode=RATIO_TARGET_MODE,
                reference_field="area_ema",
                follow_ratio=0.5,
                kp=0.0,
                ki=0.0,
            )
            lock.start([controller], update_s=0.01)
            try:
                _wait_until(lambda: lock.status()["records_written"] >= 2)
            finally:
                status = lock.stop()

            csv_path = Path(status["csv_file"])
            self.assertTrue(csv_path.is_file())
            with csv_path.open("r", newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual(rows[0]["target_mode"], RATIO_TARGET_MODE)
            self.assertEqual(rows[0]["reference_field"], "area_ema")
            self.assertAlmostEqual(float(rows[0]["actual_ratio"]), 0.375)
            self.assertGreater(int(rows[0]["frame_id"]), 0)

    def test_log_directory_failure_does_not_leave_false_running_state(self) -> None:
        """日志目录不可用时，启动必须完整失败而不是留下半初始化状态。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            file_instead_of_directory = Path(temp_dir) / "not_a_directory"
            file_instead_of_directory.write_text("occupied", encoding="utf-8")
            lock = PowerLockController(
                _FakeDaq(),
                _FakeTrendLogger(),  # type: ignore[arg-type]
                output_dir=file_instead_of_directory,
            )
            with self.assertRaises(OSError):
                lock.start([_controller()], update_s=0.01)
            self.assertFalse(lock.status()["running"])
            self.assertEqual(lock.status()["iterations"], 0)


if __name__ == "__main__":
    unittest.main()
