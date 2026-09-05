"""USB-6363 API 的客户端工具。

其他 Python 子程序应该 import 这个文件里的 Usb6363Client，
不要直接 import nidaqmx，也不要直接 import usb6363_core。

调用关系应该是：
    子程序 -> Usb6363Client -> HTTP API -> usb6363_server -> usb6363_core -> USB-6363
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np


# 默认连接本机的 USB-6363 API 服务。
DEFAULT_BASE_URL = "http://127.0.0.1:8765"


class Usb6363Client:
    """给其他 Python 程序使用的简单客户端。

    你未来写子程序时，大多数情况下只需要：

        from usb6363_client import Usb6363Client
        daq = Usb6363Client()
        value = daq.get_unified_ai_latest("ai0")
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0) -> None:
        # base_url 是 server 地址，默认就是本机 8765 端口。
        self.base_url = base_url.rstrip("/")

        # timeout 是网络请求最长等待时间，不是采集卡采样时间。
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        """检查服务是否正在运行。"""

        return self._get("/health")

    def list_devices(self) -> dict[str, Any]:
        """列出 server 那边 NI-DAQmx 能看到的设备。"""

        return self._get("/api/devices")

    def get_device(self) -> dict[str, Any]:
        """获取当前目标设备 Dev2 的信息。"""

        return self._get("/api/device")

    def list_terminals(self) -> dict[str, Any]:
        """列出 PFI、数字线、计数器等端子。

        刚开始接线调试时，可以先调用这个函数看看设备暴露了哪些名字。
        """

        return self._get("/api/terminals")

    # ------------------------------------------------------------------
    # LEGACY AI API
    # ------------------------------------------------------------------
    # 下面这一组 read_ai / subscribe_ai / frame_stream 接口会直接或间接创建旧 AI task。
    # 新程序请优先使用 unified AI stream，也就是 start_unified_ai_stream /
    # get_unified_ai_stream_latest_frame / get_unified_ai_stats 等方法。
    # 这些旧方法暂时保留，只用于旧脚本兼容、单独调试和逐步迁移。
    def read_ai(
        self,
        channel: str = "ai0",
        samples: int = 1,
        rate: float = 1000.0,
        terminal_config: str = "RSE",
        min_val: float = -10.0,
        max_val: float = 10.0,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """读取模拟输入 AI。

        常用写法：
            daq.read_ai("ai0")
            daq.read_ai("ai1", samples=100, rate=1000)
        """

        return self._get(
            "/api/ai/read",
            {
                "channel": channel,
                "samples": samples,
                "rate": rate,
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
            },
        )

    def subscribe_ai(self, channel: str = "ai0") -> dict[str, Any]:
        """订阅一个 AI 通道，启动或重建后台连续采样。

        采样率由 server 自动管理：
            1 个通道 -> 2 MHz
            多个通道 -> 1 MHz / 通道数
        """

        return self._post(
            "/api/ai/subscribe",
            {
                "channel": channel,
            },
        )

    def unsubscribe_ai(self, channel: str = "ai0") -> dict[str, Any]:
        """取消订阅一个 AI 通道。"""

        return self._post(
            "/api/ai/unsubscribe",
            {
                "channel": channel,
            },
        )

    def set_ai_channels(self, channels: list[str]) -> dict[str, Any]:
        """一次性设置后台连续采样的 AI 通道列表。

        这适合“暂停其他通道，只采某几个通道”的场景。
        """

        return self._post(
            "/api/ai/set_channels",
            {
                "channels": channels,
            },
        )

    def clear_ai_channels(self) -> dict[str, Any]:
        """停止所有 AI 后台连续采样。"""

        return self._post("/api/ai/clear", {})

    def get_ai_status(self) -> dict[str, Any]:
        """查询后台 AI 连续采样状态。"""

        return self._get("/api/ai/status")

    def get_ai_latest(self, channel: str = "ai0") -> dict[str, Any]:
        """读取已订阅通道的最近一个采样值。"""

        return self._get(
            "/api/ai/latest",
            {
                "channel": channel,
            },
        )

    def get_ai_buffer(
        self,
        channel: str = "ai0",
        max_samples: int = 1000,
    ) -> dict[str, Any]:
        """读取已订阅通道最近的一段缓存数据。"""

        return self._get(
            "/api/ai/buffer",
            {
                "channel": channel,
                "max_samples": max_samples,
            },
        )

    def get_ai_stats(
        self,
        channel: str = "ai0",
        max_samples: int = 10000,
    ) -> dict[str, Any]:
        """读取已订阅通道最近缓存数据的统计量。

        适合实时反馈：返回均值、最大值、最小值、RMS 等小 JSON。
        """

        return self._get(
            "/api/ai/stats",
            {
                "channel": channel,
                "max_samples": max_samples,
            },
        )

    def record_ai_to_file(
        self,
        seconds: float,
        output_dir: str = "data",
        prefix: str = "ai_capture",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """把当前后台 AI 采样流接下来的一段完整数据保存成 .npy 文件。

        调用前需要先 set_ai_channels 或 subscribe_ai。
        返回的 JSON 只包含文件路径和元数据，不包含巨大原始数组。
        """

        return self._post(
            "/api/ai/record_to_file",
            {
                "seconds": seconds,
                "output_dir": output_dir,
                "prefix": prefix,
                "timeout": timeout,
            },
        )

    def capture_ai_frame(
        self,
        channels: list[str] | None = None,
        samples: int = 5000,
        rate: float = 50_000.0,
        terminal_config: str = "DIFF",
        min_val: float = -5.0,
        max_val: float = 5.0,
        timeout: float = 10.0,
        trigger_enabled: bool = False,
        trigger_source: str = "PFI0",
        trigger_edge: str = "RISING",
    ) -> dict[str, Any]:
        """同步读取多路 AI 的一帧数据。

        这个方法适合双峰锁定这种“每次要拿到 ai0/ai1 同一帧波形”的场景。
        返回值里的 values 是二维列表：
            values[0] 对应 channels[0]
            values[1] 对应 channels[1]

        trigger_enabled=True 时，底层采集会等待 PFI 等数字触发源的边沿。
        """

        return self._post(
            "/api/ai/capture_frame",
            {
                "channels": ["ai0", "ai1"] if channels is None else channels,
                "samples": samples,
                "rate": rate,
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
                "trigger_enabled": trigger_enabled,
                "trigger_source": trigger_source,
                "trigger_edge": trigger_edge,
            },
        )

    def start_ai_frame_stream(
        self,
        channels: list[str] | None = None,
        samples_per_frame: int = 5000,
        rate: float = 50_000.0,
        terminal_config: str = "DIFF",
        min_val: float = -5.0,
        max_val: float = 5.0,
        timeout: float = 10.0,
        trigger_enabled: bool = False,
        trigger_source: str = "PFI0",
        trigger_edge: str = "RISING",
        resync_every_frames: int = 0,
    ) -> dict[str, Any]:
        """启动固定点数分帧连续采集。

        这是旧版程序那种模式：创建一次连续 AI task，每次读取固定点数作为一帧。
        """

        return self._post(
            "/api/ai/frame_stream/start",
            {
                "channels": ["ai0", "ai1"] if channels is None else channels,
                "samples_per_frame": samples_per_frame,
                "rate": rate,
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
                "trigger_enabled": trigger_enabled,
                "trigger_source": trigger_source,
                "trigger_edge": trigger_edge,
                "resync_every_frames": resync_every_frames,
            },
        )

    def stop_ai_frame_stream(self) -> dict[str, Any]:
        """停止固定点数分帧连续采集。"""

        return self._post("/api/ai/frame_stream/stop", {})

    def get_ai_frame_stream_status(self) -> dict[str, Any]:
        """查询固定点数分帧连续采集状态。"""

        return self._get("/api/ai/frame_stream/status")

    def get_ai_frame_stream_latest(self) -> dict[str, Any]:
        """读取固定点数分帧连续采集的最新一帧。"""

        return self._get("/api/ai/frame_stream/latest")

    # ------------------------------------------------------------------
    # RECOMMENDED AI API
    # ------------------------------------------------------------------
    # 新的上层模块应当共享这一条统一 AI 流，避免多个程序各自打开 AI task 抢硬件。
    def start_unified_ai_stream(
        self,
        channels: list[str] | None = None,
        samples_per_frame: int = 5000,
        rate: float = 50_000.0,
        terminal_config: str = "DIFF",
        min_val: float = -5.0,
        max_val: float = 5.0,
        timeout: float = 10.0,
        trigger_enabled: bool = False,
        trigger_source: str = "PFI0",
        trigger_edge: str = "RISING",
        resync_every_frames: int = 0,
    ) -> dict[str, Any]:
        """启动统一 AI 数据流。

        这是未来推荐入口：双峰、慢漂、示波器等模块应尽量读取同一个统一流，
        避免多个程序各自创建 AI task。
        """

        return self._post(
            "/api/ai/unified/start",
            {
                "channels": ["ai0", "ai1"] if channels is None else channels,
                "samples_per_frame": samples_per_frame,
                "rate": rate,
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
                "trigger_enabled": trigger_enabled,
                "trigger_source": trigger_source,
                "trigger_edge": trigger_edge,
                "resync_every_frames": resync_every_frames,
            },
        )

    def stop_unified_ai_stream(self) -> dict[str, Any]:
        """停止统一 AI 数据流。"""

        return self._post("/api/ai/unified/stop", {})

    def get_unified_ai_stream_status(self) -> dict[str, Any]:
        """查询统一 AI 数据流状态。"""

        return self._get("/api/ai/unified/status")

    def get_unified_ai_stream_latest_frame(self) -> dict[str, Any]:
        """读取统一 AI 数据流的最新一帧完整波形。"""

        return self._get("/api/ai/unified/latest_frame")

    def get_unified_ai_frame_batch(
        self,
        after_frame_id: int,
        channels: list[str],
        max_frames: int = 100,
    ) -> dict[str, Any]:
        """按 frame_id 补取统一流历史帧。

        这个接口返回本机 NPZ 二进制，不会把大量浮点数展开成 JSON。
        frames 中每帧的 values 仍保持二维形状：通道数 × 每通道点数。
        """

        if not channels:
            raise ValueError("channels must not be empty")
        raw = self._get_bytes(
            "/api/ai/unified/frame_batch",
            {
                "after_frame_id": int(after_frame_id),
                "channels": ",".join(str(channel) for channel in channels),
                "max_frames": int(max_frames),
            },
        )
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            frame_ids = data["frame_id"].astype(np.int64, copy=False)
            segment_ids = data["segment_id"].astype(np.int64, copy=False)
            segment_frame_ids = data["segment_frame_id"].astype(np.int64, copy=False)
            started_at = data["started_at"].astype(np.float64, copy=False)
            finished_at = data["finished_at"].astype(np.float64, copy=False)
            values = data["values"].astype(np.float32, copy=False)
            physical_channels = [str(item) for item in data["channels"].tolist()]

            frames = [
                {
                    "frame_id": int(frame_ids[index]),
                    "segment_id": int(segment_ids[index]),
                    "segment_frame_id": int(segment_frame_ids[index]),
                    "started_at": float(started_at[index]),
                    "finished_at": float(finished_at[index]),
                    "channels": physical_channels,
                    "channel_count": len(physical_channels),
                    "samples_per_channel": int(values.shape[2]),
                    # copy() 让数组在 np.load 上下文关闭后仍然独立有效。
                    "values": values[index].copy(),
                }
                for index in range(len(frame_ids))
            ]
            return {
                "frames": frames,
                "oldest_available_frame_id": int(data["oldest_available_frame_id"][0]),
                "latest_available_frame_id": int(data["latest_available_frame_id"][0]),
                "stream_frame_id": int(data["stream_frame_id"][0]),
                "history_overrun": bool(data["history_overrun"][0]),
                "missing_before_first": int(data["missing_before_first"][0]),
                "has_more": bool(data["has_more"][0]),
            }

    def get_unified_ai_latest(self, channel: str = "ai0") -> dict[str, Any]:
        """读取统一 AI 数据流中某个通道的最近一个点。"""

        return self._get(
            "/api/ai/unified/latest",
            {
                "channel": channel,
            },
        )

    def get_unified_ai_buffer(
        self,
        channel: str = "ai0",
        max_samples: int = 1000,
    ) -> dict[str, Any]:
        """读取统一 AI 数据流中某个通道最近的一段缓存。"""

        return self._get(
            "/api/ai/unified/buffer",
            {
                "channel": channel,
                "max_samples": max_samples,
            },
        )

    def get_unified_ai_stats(
        self,
        channel: str = "ai0",
        max_samples: int = 10000,
    ) -> dict[str, Any]:
        """读取统一 AI 数据流中某个通道最近缓存的统计量。"""

        return self._get(
            "/api/ai/unified/stats",
            {
                "channel": channel,
                "max_samples": max_samples,
            },
        )

    def write_ao(
        self,
        channel: str = "ao0",
        value: float = 0.0,
        min_val: float = -10.0,
        max_val: float = 10.0,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """写模拟输出 AO。

        这会真实改变输出电压，例如：
            daq.write_ao("ao0", 1.23)
        """

        return self._post(
            "/api/ao/write",
            {
                "channel": channel,
                "value": value,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
            },
        )

    def read_pfi(self, line: str = "PFI0", timeout: float = 10.0) -> dict[str, Any]:
        """读取 PFI 或数字线的高低电平。

        返回 value=True 表示高电平，value=False 表示低电平。
        """

        return self._get(
            "/api/pfi/read",
            {
                "line": line,
                "timeout": timeout,
            },
        )

    def write_pfi(
        self,
        line: str = "PFI0",
        value: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """写 PFI 或数字线的高低电平。

        注意：这会真实改变端子电平。外部设备已连接时要先确认安全。
        """

        return self._post(
            "/api/pfi/write",
            {
                "line": line,
                "value": value,
                "timeout": timeout,
            },
        )

    def count_pfi_edges(
        self,
        line: str = "PFI0",
        counter: str = "ctr0",
        seconds: float = 1.0,
        edge: str = "RISING",
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """统计 PFI 在一段时间内的边沿数量。

        例子：
            daq.count_pfi_edges("PFI0", seconds=1.0)

        这会返回 1 秒内 PFI0 出现了多少个上升沿。
        """

        return self._get(
            "/api/pfi/count",
            {
                "line": line,
                "counter": counter,
                "seconds": seconds,
                "edge": edge,
                "timeout": timeout,
            },
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 GET 请求。普通使用者通常不需要直接调用这个函数。"""

        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            with urlopen(url, timeout=self.timeout) as response:
                return self._decode_response(response.read())
        except HTTPError as exc:
            raise RuntimeError(self._error_message(exc)) from exc

    def _get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """读取二进制响应，当前用于统一流的 NPZ 历史帧。"""

        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            with urlopen(url, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise RuntimeError(self._error_message(exc)) from exc

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """发送 POST 请求。普通使用者通常不需要直接调用这个函数。"""

        payload = json.dumps(data).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return self._decode_response(response.read())
        except HTTPError as exc:
            raise RuntimeError(self._error_message(exc)) from exc

    @staticmethod
    def _decode_response(raw: bytes) -> dict[str, Any]:
        """把 server 返回的 JSON 字节数据转换成 Python dict。"""

        data = json.loads(raw.decode("utf-8"))
        if data.get("ok") is False:
            raise RuntimeError(data.get("error", "USB-6363 API request failed"))
        return data

    @staticmethod
    def _error_message(exc: HTTPError) -> str:
        """尽量从 HTTP 错误响应里提取 server 返回的具体错误原因。"""

        try:
            raw = exc.read()
            data = json.loads(raw.decode("utf-8"))
            return data.get("error", str(exc))
        except Exception:
            return str(exc)
