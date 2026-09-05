"""统一 AI 历史帧和 NPZ 客户端的无硬件测试。"""

from __future__ import annotations

import threading
import unittest
from collections import deque
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import numpy as np

from usb6363_client import Usb6363Client
from usb6363_core import DaqController
from usb6363_server import make_handler


def _history_frame(frame_id: int) -> dict[str, Any]:
    """生成两个通道、每通道三个点的简单模拟帧。"""

    return {
        "frame_id": frame_id,
        "segment_id": 1,
        "segment_frame_id": frame_id,
        "started_at": 1000.0 + frame_id,
        "finished_at": 1000.1 + frame_id,
        "values": np.asarray(
            [
                [frame_id, frame_id + 0.1, frame_id + 0.2],
                [frame_id + 10, frame_id + 10.1, frame_id + 10.2],
            ],
            dtype=np.float32,
        ),
    }


class UnifiedHistoryCoreTests(unittest.TestCase):
    """直接检查 DaqController 的历史选择规则，不访问采集卡。"""

    def setUp(self) -> None:
        self.controller = DaqController("Dev2")
        self.controller._unified_settings = {
            "channels": ["Dev2/ai0", "Dev2/ai1"],
            "samples_per_frame": 3,
            "frame_rate_hz": 10.0,
        }
        self.controller._unified_frame_id = 6
        self.controller._unified_frame_history = deque(
            [_history_frame(4), _history_frame(5), _history_frame(6)],
            maxlen=3,
        )
        self.controller._unified_history_capacity_frames = 3
        self.controller._unified_history_bytes_per_frame = 24
        self.controller._unified_history_evicted_frames = 3

    def test_batch_filters_channel_and_keeps_order(self) -> None:
        batch = self.controller.get_unified_ai_frame_batch(
            after_frame_id=4,
            channels=["ai1"],
            max_frames=100,
        )

        np.testing.assert_array_equal(batch["frame_id"], [5, 6])
        self.assertEqual(batch["values"].shape, (2, 1, 3))
        self.assertEqual(batch["values"].dtype, np.float32)
        np.testing.assert_allclose(batch["values"][0, 0], [15.0, 15.1, 15.2])
        self.assertFalse(bool(batch["history_overrun"][0]))
        self.assertFalse(bool(batch["has_more"][0]))

    def test_batch_reports_overrun_and_has_more(self) -> None:
        batch = self.controller.get_unified_ai_frame_batch(
            after_frame_id=1,
            channels=["ai0"],
            max_frames=1,
        )

        self.assertTrue(bool(batch["history_overrun"][0]))
        self.assertEqual(int(batch["missing_before_first"][0]), 2)
        np.testing.assert_array_equal(batch["frame_id"], [4])
        self.assertTrue(bool(batch["has_more"][0]))

    def test_status_exposes_history_capacity_and_usage(self) -> None:
        status = self.controller.get_unified_ai_stream_status()

        self.assertEqual(status["history_capacity_frames"], 3)
        self.assertEqual(status["history_stored_frames"], 3)
        self.assertEqual(status["history_oldest_frame_id"], 4)
        self.assertEqual(status["history_latest_frame_id"], 6)
        self.assertEqual(status["history_evicted_frames"], 3)
        self.assertEqual(status["history_bytes_used"], 72)

    def test_start_calculates_128_mib_history_without_hardware(self) -> None:
        controller = DaqController("Dev2")
        # 替换采集线程主体后，start 只执行参数和状态初始化，不会访问 NI 设备。
        with patch.object(controller, "_unified_ai_stream_worker", return_value=None):
            status = controller.start_unified_ai_stream(
                channels=["ai0", "ai1", "ai2"],
                samples_per_frame=10_000,
                rate=100_000.0,
            )
        try:
            self.assertEqual(status["history_capacity_frames"], 1118)
            self.assertAlmostEqual(status["history_retention_seconds"], 111.8)
            self.assertEqual(status["settings"]["history_bytes_per_frame"], 120_000)
        finally:
            controller.stop_unified_ai_stream()


class _FakeBatchController:
    """只实现测试路由需要的方法，避免创建任何 NI-DAQmx task。"""

    device_name = "Dev2"

    def get_unified_ai_frame_batch(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs != {
            "after_frame_id": 10,
            "channels": ["ai0"],
            "max_frames": 2,
        }:
            raise AssertionError(f"查询参数解析错误：{kwargs}")
        return {
            "frame_id": np.asarray([11, 12], dtype=np.int64),
            "segment_id": np.asarray([1, 1], dtype=np.int64),
            "segment_frame_id": np.asarray([11, 12], dtype=np.int64),
            "started_at": np.asarray([11.0, 12.0], dtype=np.float64),
            "finished_at": np.asarray([11.1, 12.1], dtype=np.float64),
            "values": np.asarray([[[1.0, 2.0]], [[3.0, 4.0]]], dtype=np.float32),
            "channels": np.asarray(["Dev2/ai0"], dtype=np.str_),
            "oldest_available_frame_id": np.asarray([1], dtype=np.int64),
            "latest_available_frame_id": np.asarray([12], dtype=np.int64),
            "stream_frame_id": np.asarray([12], dtype=np.int64),
            "history_overrun": np.asarray([False], dtype=np.bool_),
            "missing_before_first": np.asarray([0], dtype=np.int64),
            "has_more": np.asarray([False], dtype=np.bool_),
        }


class UnifiedHistoryHttpTests(unittest.TestCase):
    """通过真实本地 HTTP 往返验证 server 和 client 的 NPZ 协议。"""

    def test_client_decodes_binary_batch(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_FakeBatchController()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = Usb6363Client(f"http://{host}:{port}")
            batch = client.get_unified_ai_frame_batch(10, ["ai0"], max_frames=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.assertEqual([frame["frame_id"] for frame in batch["frames"]], [11, 12])
        self.assertEqual(batch["frames"][0]["values"].shape, (1, 2))
        self.assertEqual(batch["frames"][0]["values"].dtype, np.float32)
        np.testing.assert_allclose(batch["frames"][1]["values"], [[3.0, 4.0]])
        self.assertFalse(batch["history_overrun"])
        self.assertFalse(batch["has_more"])


if __name__ == "__main__":
    unittest.main()
