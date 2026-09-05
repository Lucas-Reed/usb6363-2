"""功率慢漂多通道记录和脱锁判断的无硬件测试。"""

from __future__ import annotations

import csv
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import power_drift_webui
from power_drift_monitor import PowerDriftPoint
from power_drift_webui import PowerDriftChannelSettings
from power_drift_webui import PowerDriftWebState
from power_drift_webui import _record_with_unlock_status
from power_drift_webui import _settings_from_body


def _point(channel: str, mean_v: float, timestamp: float = 1000.0) -> PowerDriftPoint:
    """构造一个最小统计点，避免测试访问真实采集卡。"""

    return PowerDriftPoint(
        iso_time=datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
        unix_time=timestamp,
        elapsed_s=1.0,
        index=1,
        session_id=None,
        source_frame_id=10,
        channel=channel,
        samples=100,
        rate_hz=1000.0,
        mean_v=mean_v,
        std_v=0.01,
        rel_std_percent=1.0,
        min_v=mean_v - 0.02,
        max_v=mean_v + 0.02,
        peak_to_peak_v=0.04,
        rms_v=abs(mean_v),
        power_estimate=mean_v,
    )


class _FakePowerDriftMonitor:
    """为每个通道返回固定均值的慢漂读取器。"""

    def __init__(self, settings: object) -> None:
        self.settings = settings

    def check_hardware_idle(self) -> None:
        return

    def read_one_point(self, row_index: int, start_time: float) -> PowerDriftPoint:
        channel = str(self.settings.channel)
        mean_v = 0.5 if channel.lower().endswith("ai0") else 2.0
        point = _point(channel, mean_v, timestamp=time.time())
        point.index = row_index
        point.elapsed_s = point.unix_time - start_time
        return point


class PowerDriftSettingsTests(unittest.TestCase):
    """验证新旧请求格式都能生成明确的通道设置。"""

    def test_parses_multiple_channels_and_unlock_limits(self) -> None:
        settings = _settings_from_body(
            {
                "channels": [
                    {
                        "channel": "ai0",
                        "note": "激光器总功率",
                        "power_per_volt": 2.0,
                        "zero_voltage": 0.1,
                        "unlock_enabled": True,
                        "unlock_min_v": 0.2,
                        "unlock_max_v": 0.8,
                    },
                    {"channel": "ai1", "unlock_enabled": False},
                ],
                "data_source": "unified_stream",
            }
        )

        self.assertEqual([item.channel for item in settings.channels], ["ai0", "ai1"])
        self.assertEqual(settings.channels[0].note, "激光器总功率")
        self.assertEqual(settings.channels[0].power_per_volt, 2.0)
        self.assertTrue(settings.channels[0].unlock_enabled)
        self.assertIsNone(settings.channels[1].unlock_min_v)

    def test_accepts_legacy_single_channel_request(self) -> None:
        settings = _settings_from_body(
            {"channel": "ai2", "power_per_volt": 3.0, "zero_voltage": 0.2}
        )

        self.assertEqual(len(settings.channels), 1)
        self.assertEqual(settings.channels[0].channel, "ai2")
        self.assertEqual(settings.channels[0].power_per_volt, 3.0)

    def test_rejects_duplicate_channels_and_reversed_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate channel"):
            _settings_from_body({"channels": [{"channel": "ai0"}, {"channel": "AI0"}]})
        with self.assertRaisesRegex(ValueError, "最小值必须小于最大值"):
            _settings_from_body(
                {
                    "channels": [
                        {
                            "channel": "ai0",
                            "unlock_enabled": True,
                            "unlock_min_v": 1.0,
                            "unlock_max_v": 0.0,
                        }
                    ]
                }
            )


class UnlockStatusTests(unittest.TestCase):
    """脱锁事件一旦发生，应保留首次时间而不是自动清除。"""

    def test_first_unlock_time_is_latched_after_signal_recovers(self) -> None:
        channel = PowerDriftChannelSettings(
            channel="ai0",
            note="测试通道",
            power_per_volt=1.0,
            zero_voltage=0.0,
            unlock_enabled=True,
            unlock_min_v=0.2,
            unlock_max_v=0.8,
        )

        _, locked = _record_with_unlock_status(_point("ai0", 0.5), channel, None)
        _, unlocked = _record_with_unlock_status(_point("ai0", 1.2), channel, None)
        event = (
            float(unlocked["unlock_event_unix_time"]),
            str(unlocked["unlock_event_iso_time"]),
        )
        record, recovered = _record_with_unlock_status(_point("ai0", 0.5, 1002.0), channel, event)

        self.assertEqual(locked["state"], "locked")
        self.assertEqual(unlocked["state"], "unlocked")
        self.assertIsNotNone(
            datetime.fromisoformat(str(unlocked["unlock_event_iso_time"])).tzinfo
        )
        self.assertEqual(recovered["state"], "unlocked")
        self.assertFalse(recovered["outside_range"])
        self.assertEqual(recovered["unlock_event_unix_time"], event[0])
        self.assertEqual(record["unlock_state"], "unlocked")


class MultiChannelWorkerTests(unittest.TestCase):
    """验证一次记录周期会为所有通道各写一行 CSV。"""

    def test_worker_writes_long_format_rows_for_every_channel(self) -> None:
        Path("data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path("data")) as temp_dir:
            settings = _settings_from_body(
                {
                    "channels": [
                        {
                            "channel": "ai0",
                            "note": "参考功率",
                            "unlock_enabled": True,
                            "unlock_min_v": 0.0,
                            "unlock_max_v": 1.0,
                        },
                        {
                            "channel": "ai1",
                            "note": "实验信号",
                            "unlock_enabled": True,
                            "unlock_min_v": 0.0,
                            "unlock_max_v": 1.0,
                        },
                    ],
                    "data_source": "unified_stream",
                    "interval": 0.02,
                    "duration": 0.09,
                    "output_dir": temp_dir,
                }
            )
            state = PowerDriftWebState()
            with patch.object(
                power_drift_webui,
                "PowerDriftMonitor",
                _FakePowerDriftMonitor,
            ):
                state.start(settings)
                deadline = time.time() + 2.0
                while state.status()["running"] and time.time() < deadline:
                    time.sleep(0.01)
                status = state.status()

            self.assertFalse(status["running"])
            self.assertIsNone(status["error"])
            self.assertGreaterEqual(status["cycles_written"], 2)
            self.assertEqual(status["rows_written"], status["cycles_written"] * 2)
            self.assertEqual(set(status["latest_points"]), {"ai0", "ai1"})
            self.assertEqual(status["unlock_status"]["ai0"]["state"], "locked")
            self.assertEqual(status["unlock_status"]["ai1"]["state"], "unlocked")

            with Path(str(status["csv_file"])).open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), status["rows_written"])
            first_cycle = [row for row in rows if row["index"] == "1"]
            self.assertEqual({row["channel"] for row in first_cycle}, {"ai0", "ai1"})
            self.assertEqual(
                {row["channel_note"] for row in first_cycle},
                {"参考功率", "实验信号"},
            )


class PowerDriftDefaultsTests(unittest.TestCase):
    """默认值必须写入磁盘，并能在模拟服务重启后恢复。"""

    def test_channel_notes_survive_state_recreation(self) -> None:
        Path("data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path("data")) as temp_dir:
            defaults_path = Path(temp_dir) / "power_drift_defaults.json"
            settings = _settings_from_body(
                {
                    "channels": [
                        {
                            "channel": "ai2",
                            "note": "腔后功率探测器",
                            "power_per_volt": 2.5,
                        }
                    ],
                    "interval": 2.0,
                }
            )

            first_state = PowerDriftWebState(defaults_path=defaults_path)
            saved = first_state.save_defaults(settings)
            recreated_state = PowerDriftWebState(defaults_path=defaults_path)
            loaded = recreated_state.active_defaults()

            self.assertEqual(saved["defaults_source"], "user")
            self.assertEqual(loaded["defaults_source"], "user")
            self.assertEqual(loaded["channels"][0]["note"], "腔后功率探测器")
            self.assertEqual(loaded["channels"][0]["power_per_volt"], 2.5)
            self.assertEqual(loaded["interval"], 2.0)

            reset = recreated_state.reset_defaults()
            self.assertEqual(reset["defaults_source"], "factory")
            self.assertFalse(defaults_path.exists())


if __name__ == "__main__":
    unittest.main()
