"""双峰查看器数据源选择的无硬件回归测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from two_peak.viewer_capture import get_frame_stream_status, start_area_trend


class _FakeDaq:
    """同时提供统一流和旧流状态，不访问真实 USB-6363。"""

    def __init__(
        self,
        unified_status: dict[str, Any],
        frame_status: dict[str, Any],
    ) -> None:
        self.unified_status = unified_status
        self.frame_status = frame_status

    def get_unified_ai_stream_status(self) -> dict[str, Any]:
        return dict(self.unified_status)

    def get_ai_frame_stream_status(self) -> dict[str, Any]:
        return dict(self.frame_status)


class _FakeTrendLogger:
    """保存慢漂启动参数，验证后端是否覆盖浏览器缓存的旧来源。"""

    def __init__(self) -> None:
        self.arguments: dict[str, Any] = {}

    def start(self, **kwargs: Any) -> dict[str, Any]:
        self.arguments = dict(kwargs)
        return {"running": True, "settings": dict(kwargs)}


class ViewerStreamSourceTests(unittest.TestCase):
    """确保状态查询不会在统一流停止后偷偷切换到旧流。"""

    def test_unified_request_does_not_fall_back_to_running_legacy_stream(self) -> None:
        state = SimpleNamespace(
            daq=_FakeDaq(
                unified_status={"running": False, "error": "-200279", "frame_id": 99},
                frame_status={"running": True, "error": None, "frame_id": 5},
            )
        )

        status = get_frame_stream_status(state, "unified_stream")

        self.assertFalse(status["running"])
        self.assertEqual(status["error"], "-200279")
        self.assertEqual(status["frame_id"], 99)
        self.assertEqual(status["stream_source"], "unified_stream")

    def test_legacy_request_still_returns_legacy_status(self) -> None:
        state = SimpleNamespace(
            daq=_FakeDaq(
                unified_status={"running": True, "frame_id": 99},
                frame_status={"running": True, "frame_id": 5},
            )
        )

        status = get_frame_stream_status(state, "frame_stream")

        self.assertTrue(status["running"])
        self.assertEqual(status["frame_id"], 5)
        self.assertEqual(status["stream_source"], "frame_stream")

    def test_area_trend_ignores_stale_legacy_source_from_browser(self) -> None:
        daq = _FakeDaq(
            unified_status={
                "running": True,
                "frame_id": 20,
                "settings": {"channels": ["Dev2/ai0", "Dev2/ai1", "Dev2/ai2"]},
            },
            frame_status={"running": False, "frame_id": 0},
        )
        trend_logger = _FakeTrendLogger()
        state = SimpleNamespace(daq=daq, trend_logger=trend_logger)

        status = start_area_trend(
            state,
            {
                "stream_source": "frame_stream",
                "channels": "ai0",
                "area_left": 10,
                "area_right": 20,
            },
        )

        self.assertTrue(status["running"])
        self.assertEqual(trend_logger.arguments["stream_source"], "unified_stream")
        self.assertEqual(trend_logger.arguments["channels"], ["ai0"])


if __name__ == "__main__":
    unittest.main()
