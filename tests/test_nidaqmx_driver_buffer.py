"""连续 AI task 输入缓冲配置的无硬件测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from usb6363 import nidaqmx_driver


class _FakeChannels:
    """记录添加通道动作；测试不需要模拟真实通道属性。"""

    def __init__(self) -> None:
        self.added: list[str] = []

    def add_ai_voltage_chan(self, channel: str, **_kwargs: object) -> None:
        self.added.append(channel)


class _FakeTiming:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    def cfg_samp_clk_timing(self, **kwargs: object) -> None:
        self.arguments = dict(kwargs)


class _FakeStartTrigger:
    def cfg_dig_edge_start_trig(self, **_kwargs: object) -> None:
        return


class _FakeTask:
    """只实现 create_continuous_ai_task 本次会访问的最小接口。"""

    def __init__(self) -> None:
        self.ai_channels = _FakeChannels()
        self.timing = _FakeTiming()
        self.in_stream = type("FakeInStream", (), {"input_buf_size": 0})()
        self.triggers = type(
            "FakeTriggers",
            (),
            {"start_trigger": _FakeStartTrigger()},
        )()
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class ContinuousAiBufferTests(unittest.TestCase):
    """确认连续采集至少留有五秒电脑端缓冲。"""

    def test_continuous_task_allocates_five_seconds_of_input_buffer(self) -> None:
        task = _FakeTask()
        fake_module = type("FakeNidaqmx", (), {"Task": lambda _self: task})()
        fake_acquisition_type = type("FakeAcquisitionType", (), {"CONTINUOUS": "continuous"})

        with (
            patch.object(
                nidaqmx_driver,
                "_load_nidaqmx",
                return_value=(fake_module, fake_acquisition_type, None, None, None),
            ),
            patch.object(nidaqmx_driver, "_terminal_config", return_value="DIFF"),
        ):
            result = nidaqmx_driver.create_continuous_ai_task(
                channels=["Dev2/ai0", "Dev2/ai1", "Dev2/ai2"],
                rate=100_000.0,
                samples_per_read=10_000,
                terminal_config_name="DIFF",
            )

        self.assertIs(result, task)
        self.assertTrue(task.started)
        self.assertEqual(task.timing.arguments["samps_per_chan"], 500_000)
        self.assertEqual(task.in_stream.input_buf_size, 500_000)


if __name__ == "__main__":
    unittest.main()
