"""USB-6363 核心控制逻辑。

这个文件是给人读的“主逻辑”：
    1. 设备信息和通道名校验
    2. AI 动态采样率管理、后台缓存、统计、写文件
    3. AO 输出
    4. PFI/数字线读写和边沿计数

重要边界：
    本文件不直接 import nidaqmx。
    唯一直接接触 NI-DAQmx 的文件是 usb6363/nidaqmx_driver.py。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from usb6363 import nidaqmx_driver


# NI MAX / NI-DAQmx 里给 USB-6363 设置的设备名。
DEVICE_NAME = "Dev2"

# AI 采样率策略：
# 1 个 AI 通道活跃时，单通道拉满到 2 MHz。
# 2 个及以上 AI 通道活跃时，多通道总采样率按 1 MHz 均分。
AI_SINGLE_CHANNEL_MAX_RATE = 2_000_000.0
AI_MULTI_CHANNEL_AGGREGATE_RATE = 1_000_000.0

# 后台连续采样时，每个通道最多缓存多少个最近数据点。
AI_DEFAULT_BUFFER_SIZE = 100_000

# 后台采样线程每次从 NI-DAQmx 读一小块数据。
AI_READ_CHUNK_SECONDS = 0.01

# 高速采样写文件时的默认目录。
AI_CAPTURE_OUTPUT_DIR = "data"

# capture_ai_frame 会把原始数组放进 JSON 返回，不能拿它做长时间高速采集。
# 超过这个点数时应该使用 record_ai_to_file，避免 Web/API 被巨大 JSON 拖垮。
AI_FRAME_MAX_JSON_SAMPLES = 200_000

# unified stream 的“最新帧”适合实时显示，却无法补回上层程序卡顿期间错过的帧。
# 因此额外保留一段 float32 历史。128 MiB 是整个历史缓冲的总上限，
# 不是每个通道各占 128 MiB。
UNIFIED_HISTORY_MAX_BYTES = 128 * 1024 * 1024
UNIFIED_HISTORY_MAX_FRAMES = 10_000

# 二进制批量接口一次最多取 100 帧，同时限制响应中的波形数组约为 32 MiB。
# 调用方若没有追上最新帧，可以根据 has_more 继续分批读取。
UNIFIED_BATCH_MAX_FRAMES = 100
UNIFIED_BATCH_MAX_BYTES = 32 * 1024 * 1024


_AI_RE = re.compile(r"^(?:(?P<device>Dev\d+)/)?ai(?P<index>\d+)$")
_AO_RE = re.compile(r"^(?:(?P<device>Dev\d+)/)?ao(?P<index>\d+)$")
_PFI_RE = re.compile(r"^/?(?:(?P<device>Dev\d+)/)?pfi(?P<index>\d+)$", re.IGNORECASE)
_DIO_RE = re.compile(
    r"^/?(?:(?P<device>Dev\d+)/)?port(?P<port>\d+)/line(?P<line>\d+)$",
    re.IGNORECASE,
)
_CTR_RE = re.compile(r"^(?:(?P<device>Dev\d+)/)?ctr(?P<index>\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DeviceInfo:
    """设备信息的小容器。"""

    name: str
    product_type: str
    serial_num: str


class DaqController:
    """USB-6363 的统一控制入口。

    server 只创建一个 DaqController 实例。
    其他程序都通过 server/client 调用它，避免多个程序直接抢采集卡。
    """

    def __init__(self, device_name: str = DEVICE_NAME) -> None:
        self.device_name = device_name

        # 用于保护短硬件操作，例如 AO、PFI、单点 AI。
        self._hardware_lock = threading.RLock()

        # 用于保护后台 AI 采样状态。
        self._ai_lock = threading.RLock()
        self._ai_active_channels: list[str] = []
        self._ai_rate = 0.0
        self._ai_running = False
        self._ai_error: str | None = None
        self._ai_last_update = 0.0
        self._ai_sample_counts: dict[str, int] = {}
        self._ai_latest: dict[str, float] = {}
        self._ai_buffers: dict[str, deque[float]] = {}
        self._ai_buffer_size = AI_DEFAULT_BUFFER_SIZE

        # record_ai_to_file 打开这个记录器，后台采样线程负责填数据。
        self._ai_recording: dict[str, Any] | None = None

        self._ai_thread: threading.Thread | None = None
        self._ai_stop_event: threading.Event | None = None

        # frame_stream 是“固定点数分帧”的连续 AI 采集，接近旧版程序的方式：
        # 创建一次连续 AI task，然后每次 read(samples_per_frame) 得到一帧。
        # 它暂时和 subscribe_ai 互斥，避免两个 AI task 抢同一套采集硬件。
        self._frame_stream_running = False
        self._frame_stream_error: str | None = None
        self._frame_stream_thread: threading.Thread | None = None
        self._frame_stream_stop_event: threading.Event | None = None
        self._frame_stream_latest: dict[str, Any] | None = None
        self._frame_stream_frame_id = 0
        self._frame_stream_settings: dict[str, Any] | None = None

        # unified_ai_stream 是未来推荐使用的统一 AI 数据流。
        # 它只打开一个真实 AI task；双峰、功率慢漂、示波器等上层模块都从这里读同一份数据。
        self._unified_running = False
        self._unified_error: str | None = None
        self._unified_thread: threading.Thread | None = None
        self._unified_stop_event: threading.Event | None = None
        self._unified_settings: dict[str, Any] | None = None
        self._unified_frame_id = 0
        self._unified_latest_frame: dict[str, Any] | None = None
        self._unified_last_update = 0.0
        self._unified_latest: dict[str, float] = {}
        self._unified_buffers: dict[str, deque[float]] = {}
        self._unified_sample_counts: dict[str, int] = {}
        # 历史帧中的 values 是连续 float32 数组，用较小内存保存完整帧。
        # 这里使用 deque 的 maxlen 自动淘汰最老帧，供上层按 frame_id 补取。
        self._unified_frame_history: deque[dict[str, Any]] = deque()
        self._unified_history_capacity_frames = 0
        self._unified_history_bytes_per_frame = 0
        self._unified_history_evicted_frames = 0

    # ---------------------------------------------------------------------
    # 设备信息
    # ---------------------------------------------------------------------
    def list_devices(self) -> list[DeviceInfo]:
        """列出当前 NI-DAQmx 能看到的所有 NI 设备。"""

        with self._hardware_lock:
            return [
                DeviceInfo(
                    name=device["name"],
                    product_type=device["product_type"],
                    serial_num=device["serial_num"],
                )
                for device in nidaqmx_driver.list_devices()
            ]

    def get_device_info(self) -> DeviceInfo:
        """读取当前目标设备的基本信息。"""

        with self._hardware_lock:
            device = nidaqmx_driver.get_device_info(self.device_name)
            return DeviceInfo(
                name=device["name"],
                product_type=device["product_type"],
                serial_num=device["serial_num"],
            )

    def list_signal_terminals(self) -> dict[str, list[str]]:
        """列出 PFI、数字线、计数器等常用端子。"""

        with self._hardware_lock:
            return nidaqmx_driver.list_signal_terminals(self.device_name)

    # ---------------------------------------------------------------------
    # AI 后台连续采样管理
    # ---------------------------------------------------------------------
    # ---------------------------------------------------------------------
    # LEGACY AI subscription/direct/frame APIs
    # ---------------------------------------------------------------------
    # 下面这一组旧 AI 接口会直接或间接管理旧 AI task。
    # 新功能优先使用 unified AI stream；旧接口只保留给旧脚本、调试和逐步迁移。
    def subscribe_ai_channel(self, channel: str) -> dict[str, Any]:
        """订阅一个 AI 通道，让后台开始或继续连续采样。"""

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before subscribing AI channels")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before subscribing AI channels")
            if physical_channel not in self._ai_active_channels:
                self._ai_active_channels.append(physical_channel)

        self._restart_ai_sampling()
        return self.get_ai_sampling_status()

    def unsubscribe_ai_channel(self, channel: str) -> dict[str, Any]:
        """取消订阅一个 AI 通道。"""

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before changing subscribed AI channels")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before changing subscribed AI channels")
            if physical_channel in self._ai_active_channels:
                self._ai_active_channels.remove(physical_channel)

        self._restart_ai_sampling()
        return self.get_ai_sampling_status()

    def set_ai_channels(self, channels: list[str]) -> dict[str, Any]:
        """一次性设置当前需要连续采样的 AI 通道列表。

        这相当于“暂停其他通道，只保留我指定的这些通道”。
        """

        normalized_channels: list[str] = []
        for channel in channels:
            physical_channel = self._normalize_ai_channel(channel)
            if physical_channel not in normalized_channels:
                normalized_channels.append(physical_channel)

        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before setting AI channels")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before setting AI channels")
            self._ai_active_channels = normalized_channels

        self._restart_ai_sampling()
        return self.get_ai_sampling_status()

    def clear_ai_channels(self) -> dict[str, Any]:
        """取消所有 AI 通道订阅，并停止后台连续采样。"""

        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before clearing subscribed AI channels")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before clearing subscribed AI channels")
            self._ai_active_channels = []

        self._restart_ai_sampling()
        return self.get_ai_sampling_status()

    def get_ai_sampling_status(self) -> dict[str, Any]:
        """查询后台 AI 连续采样状态。"""

        with self._ai_lock:
            channels = list(self._ai_active_channels)
            return {
                "running": self._ai_running,
                "channels": channels,
                "channel_count": len(channels),
                "rate_per_channel": self._ai_rate,
                "aggregate_rate": self._ai_rate * len(channels),
                "buffer_size": self._ai_buffer_size,
                "last_update": self._ai_last_update,
                "sample_counts": dict(self._ai_sample_counts),
                "error": self._ai_error,
            }

    def get_ai_latest(self, channel: str) -> dict[str, Any]:
        """读取后台连续采样缓存里某个通道的最近一个值。"""

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._ai_active_channels:
                raise RuntimeError(f"{physical_channel} is not subscribed")
            if physical_channel not in self._ai_latest:
                raise RuntimeError(f"{physical_channel} has no sampled data yet")

            return {
                "device": self.device_name,
                "channel": physical_channel,
                "rate": self._ai_rate,
                "value": self._ai_latest[physical_channel],
                "last_update": self._ai_last_update,
                "sample_count": self._ai_sample_counts.get(physical_channel, 0),
            }

    def get_ai_buffer(self, channel: str, max_samples: int = 1000) -> dict[str, Any]:
        """读取后台连续采样缓存里某个通道最近的一段数据。"""

        if max_samples < 1:
            raise ValueError("max_samples must be >= 1")

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._ai_active_channels:
                raise RuntimeError(f"{physical_channel} is not subscribed")

            values = list(self._ai_buffers.get(physical_channel, []))[-max_samples:]
            return {
                "device": self.device_name,
                "channel": physical_channel,
                "rate": self._ai_rate,
                "samples": len(values),
                "values": values,
                "last_update": self._ai_last_update,
                "sample_count": self._ai_sample_counts.get(physical_channel, 0),
            }

    def get_ai_stats(self, channel: str, max_samples: int = 10000) -> dict[str, Any]:
        """返回某个通道最近一段缓存数据的统计量。

        这适合实时监测反馈，不返回完整高速原始数据。
        """

        if max_samples < 1:
            raise ValueError("max_samples must be >= 1")

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._ai_active_channels:
                raise RuntimeError(f"{physical_channel} is not subscribed")

            values = list(self._ai_buffers.get(physical_channel, []))[-max_samples:]
            rate = self._ai_rate
            last_update = self._ai_last_update
            sample_count = self._ai_sample_counts.get(physical_channel, 0)

        if not values:
            raise RuntimeError(f"{physical_channel} has no sampled data yet")

        data = np.asarray(values, dtype=np.float64)
        return {
            "device": self.device_name,
            "channel": physical_channel,
            "rate": rate,
            "samples": int(data.size),
            "mean": float(np.mean(data)),
            "min": float(np.min(data)),
            "max": float(np.max(data)),
            "rms": float(np.sqrt(np.mean(np.square(data)))),
            "last": float(data[-1]),
            "last_update": last_update,
            "sample_count": sample_count,
        }

    def record_ai_to_file(
        self,
        seconds: float,
        output_dir: str = AI_CAPTURE_OUTPUT_DIR,
        prefix: str = "ai_capture",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """把后台正在采集的 AI 数据记录到 .npy 文件。

        调用前必须先 set_ai_channels/subscribe_ai，让后台采样已经运行。
        """

        if seconds <= 0:
            raise ValueError("seconds must be > 0")

        with self._ai_lock:
            if not self._ai_active_channels or not self._ai_running:
                raise RuntimeError("AI sampling is not running. Call set_ai_channels first.")
            if self._ai_recording is not None:
                raise RuntimeError("Another AI file recording is already running")

            channels = list(self._ai_active_channels)
            rate = self._ai_rate
            samples_per_channel = max(1, int(round(rate * seconds)))
            done_event = threading.Event()
            self._ai_recording = {
                "channels": channels,
                "rate": rate,
                "seconds": seconds,
                "target_samples": samples_per_channel,
                "buffers": {channel: [] for channel in channels},
                "started_at": time.time(),
                "done_event": done_event,
            }

        wait_timeout = timeout if timeout is not None else seconds + 10.0
        if not done_event.wait(wait_timeout):
            with self._ai_lock:
                self._ai_recording = None
            raise TimeoutError("Timed out while recording AI data to file")

        with self._ai_lock:
            recording = self._ai_recording
            self._ai_recording = None

        if recording is None:
            raise RuntimeError("AI recording ended without data")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        npy_path = output_path / f"{prefix}_{timestamp}.npy"
        json_path = output_path / f"{prefix}_{timestamp}.json"

        data_rows = []
        for channel in channels:
            channel_data = recording["buffers"][channel][:samples_per_channel]
            data_rows.append(np.asarray(channel_data, dtype=np.float64))
        data = np.vstack(data_rows)

        np.save(npy_path, data)

        metadata = {
            "device": self.device_name,
            "channels": channels,
            "rate_per_channel": rate,
            "aggregate_rate": rate * len(channels),
            "seconds_requested": seconds,
            "samples_per_channel": int(data.shape[1]),
            "shape": list(data.shape),
            "npy_file": str(npy_path.resolve()),
            "metadata_file": str(json_path.resolve()),
            "started_at": recording["started_at"],
            "finished_at": time.time(),
            "format": "numpy .npy, shape=(channel_count, samples_per_channel)",
        }

        json_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata

    def read_ai_voltage(
        self,
        channel: str = "ai0",
        samples: int = 1,
        rate: float = 1000.0,
        terminal_config: str = "RSE",
        min_val: float = -10.0,
        max_val: float = 10.0,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """读取模拟输入 AI 电压。

        如果后台 AI 正在采样，已订阅通道会从缓存返回，避免抢设备。
        """

        if samples < 1:
            raise ValueError("samples must be >= 1")
        if rate <= 0:
            raise ValueError("rate must be > 0")

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before direct AI read")
            if self._ai_active_channels:
                if physical_channel not in self._ai_active_channels:
                    raise RuntimeError(
                        f"AI sampling is running, but {physical_channel} is not subscribed. "
                        "Subscribe it first or clear AI channels before direct read."
                    )
                if samples == 1:
                    if physical_channel not in self._ai_latest:
                        raise RuntimeError(f"{physical_channel} has no sampled data yet")
                    return {
                        "device": self.device_name,
                        "channel": physical_channel,
                        "samples": 1,
                        "rate": self._ai_rate,
                        "values": self._ai_latest[physical_channel],
                    }

                values = list(self._ai_buffers.get(physical_channel, []))[-samples:]
                return {
                    "device": self.device_name,
                    "channel": physical_channel,
                    "samples": len(values),
                    "rate": self._ai_rate,
                    "values": values,
                }

        with self._hardware_lock:
            values = nidaqmx_driver.read_ai_voltage(
                device_name=self.device_name,
                physical_channel=physical_channel,
                samples=samples,
                rate=rate,
                terminal_config_name=terminal_config,
                min_val=min_val,
                max_val=max_val,
                timeout=timeout,
            )

        return {
            "device": self.device_name,
            "channel": physical_channel,
            "samples": samples,
            "rate": rate,
            "values": values,
        }

    def capture_ai_frame(
        self,
        channels: list[str],
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
        """同步采集多路 AI 的一帧数据。

        这个接口是给“双峰锁定”这类程序准备的：
        - channels 例如 ["ai0", "ai1"]。
        - samples 是每个通道读多少个点。
        - rate 是每个通道的采样率。

        注意：它会把数据直接放进 JSON 返回，所以只适合“一帧波形”。
        长时间高速记录请用 record_ai_to_file。

        trigger_enabled=True 时，会用 PFI 等数字端子作为硬件开始触发源。
        例如 trigger_source="PFI0" 表示等待 /Dev2/PFI0 的指定边沿后开始采样。
        """

        if not channels:
            raise ValueError("channels must not be empty")
        if samples < 1:
            raise ValueError("samples must be >= 1")
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if min_val >= max_val:
            raise ValueError("min_val must be smaller than max_val")

        physical_channels: list[str] = []
        for channel in channels:
            physical_channel = self._normalize_ai_channel(channel)
            if physical_channel not in physical_channels:
                physical_channels.append(physical_channel)

        physical_trigger_source = None
        if trigger_enabled:
            physical_trigger_source = self._normalize_pfi_terminal(trigger_source)

        total_json_samples = len(physical_channels) * samples
        if total_json_samples > AI_FRAME_MAX_JSON_SAMPLES:
            raise ValueError(
                f"capture_ai_frame would return {total_json_samples} samples as JSON. "
                "Use record_ai_to_file for large or high-speed captures."
            )

        started_at = time.time()
        t0 = time.perf_counter()
        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before capture_ai_frame")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before capture_ai_frame")
            if self._ai_active_channels:
                raise RuntimeError(
                    "Background AI sampling is running. Call clear_ai_channels before "
                    "capture_ai_frame so the synchronous frame task can own the AI hardware."
                )

            # 持有 _ai_lock 可以防止另一个请求在本次同步采帧期间启动后台 AI。
            with self._hardware_lock:
                values = nidaqmx_driver.capture_ai_frame(
                    device_name=self.device_name,
                    physical_channels=physical_channels,
                    samples=samples,
                    rate=rate,
                    terminal_config_name=terminal_config,
                    min_val=min_val,
                    max_val=max_val,
                    timeout=timeout,
                    trigger_source=physical_trigger_source,
                    trigger_edge_name=trigger_edge,
                )
        duration_seconds = time.perf_counter() - t0

        return {
            "device": self.device_name,
            "channels": physical_channels,
            "channel_count": len(physical_channels),
            "samples_per_channel": samples,
            "rate_per_channel": rate,
            "aggregate_rate": rate * len(physical_channels),
            "terminal_config": terminal_config,
            "min_val": min_val,
            "max_val": max_val,
            "duration_seconds": duration_seconds,
            "started_at": started_at,
            "finished_at": time.time(),
            "trigger_enabled": trigger_enabled,
            "trigger_source": physical_trigger_source,
            "trigger_edge": trigger_edge,
            "values": values,
        }

    def start_ai_frame_stream(
        self,
        channels: list[str],
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
        """启动固定点数分帧的连续 AI 采集。

        旧版程序的核心方式就是：连续采样 task 不停，每次读取固定点数作为一帧。
        trigger_enabled=True 时，PFI 只作为“启动触发”，不是每帧触发。
        resync_every_frames>0 时，会每隔指定帧数重建一次 AI task，
        重新等待 PFI 边沿，以修正长时间运行时的帧边界慢漂。
        """

        if not channels:
            raise ValueError("channels must not be empty")
        if samples_per_frame < 1:
            raise ValueError("samples_per_frame must be >= 1")
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if min_val >= max_val:
            raise ValueError("min_val must be smaller than max_val")
        if resync_every_frames < 0:
            raise ValueError("resync_every_frames must be >= 0")
        if resync_every_frames > 0 and not trigger_enabled:
            raise ValueError("resync_every_frames requires trigger_enabled=True")

        physical_channels: list[str] = []
        for channel in channels:
            physical_channel = self._normalize_ai_channel(channel)
            if physical_channel not in physical_channels:
                physical_channels.append(physical_channel)

        physical_trigger_source = None
        if trigger_enabled:
            physical_trigger_source = self._normalize_pfi_terminal(trigger_source)

        total_json_samples = len(physical_channels) * samples_per_frame
        if total_json_samples > AI_FRAME_MAX_JSON_SAMPLES:
            raise ValueError(
                f"frame stream latest frame would return {total_json_samples} samples as JSON. "
                "Reduce channel count/samples_per_frame for the viewer experiment."
            )

        # 后端连续采集的“帧间隔”不是网页定时器决定的，
        # 而是由每通道点数 / 每通道采样率这个物理关系决定的。
        frame_duration_seconds = samples_per_frame / rate
        frame_duration_ms = frame_duration_seconds * 1000.0
        frame_rate_hz = rate / samples_per_frame

        with self._ai_lock:
            if self._unified_running:
                raise RuntimeError("Stop unified AI stream before starting frame stream")
            if self._ai_active_channels or self._ai_running:
                raise RuntimeError("Stop subscribe_ai/background AI before starting frame stream")
            if self._frame_stream_running:
                raise RuntimeError("AI frame stream is already running")

            settings = {
                "device": self.device_name,
                "channels": physical_channels,
                "samples_per_frame": samples_per_frame,
                "rate_per_channel": rate,
                "aggregate_rate": rate * len(physical_channels),
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
                "trigger_enabled": trigger_enabled,
                "trigger_source": physical_trigger_source,
                "trigger_edge": trigger_edge,
                "trigger_mode": "periodic_start" if resync_every_frames > 0 else ("start_only" if trigger_enabled else "off"),
                "resync_every_frames": int(resync_every_frames),
                "frame_duration_seconds": frame_duration_seconds,
                "frame_duration_ms": frame_duration_ms,
                "frame_rate_hz": frame_rate_hz,
            }
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._ai_frame_stream_worker,
                args=(settings, stop_event),
                daemon=True,
                name="usb6363-ai-frame-stream",
            )
            self._frame_stream_stop_event = stop_event
            self._frame_stream_thread = thread
            self._frame_stream_latest = None
            self._frame_stream_frame_id = 0
            self._frame_stream_error = None
            self._frame_stream_settings = dict(settings)
            self._frame_stream_running = True
            thread.start()

        return self.get_ai_frame_stream_status()

    def stop_ai_frame_stream(self) -> dict[str, Any]:
        """停止固定点数分帧的连续 AI 采集。"""

        self._stop_ai_frame_stream()
        return self.get_ai_frame_stream_status()

    def get_ai_frame_stream_status(self) -> dict[str, Any]:
        """查询固定点数分帧采集状态。"""

        with self._ai_lock:
            settings = dict(self._frame_stream_settings or {})
            return {
                "running": self._frame_stream_running,
                "error": self._frame_stream_error,
                "frame_id": self._frame_stream_frame_id,
                "has_frame": self._frame_stream_latest is not None,
                "frame_duration_seconds": settings.get("frame_duration_seconds"),
                "frame_duration_ms": settings.get("frame_duration_ms"),
                "frame_rate_hz": settings.get("frame_rate_hz"),
                "trigger_mode": settings.get("trigger_mode", "off"),
                "settings": settings,
            }

    def get_ai_frame_stream_latest(self) -> dict[str, Any]:
        """返回最新一帧固定点数采集数据。"""

        with self._ai_lock:
            if self._frame_stream_latest is None:
                raise RuntimeError("AI frame stream has no frame yet")
            return dict(self._frame_stream_latest)

    # ---------------------------------------------------------------------
    # 统一 AI 采集流
    # ---------------------------------------------------------------------
    # RECOMMENDED AI API：新上层模块共享这一条统一 AI 流，避免多个 AI task 抢设备。
    def start_unified_ai_stream(
        self,
        channels: list[str],
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

        这是未来推荐入口：底层只开一个 AI task，上层模块都读取同一份数据。
        第一版不做自动采样率协商，由调用者手动指定全局 channels/rate/samples_per_frame。
        """

        if not channels:
            raise ValueError("channels must not be empty")
        if samples_per_frame < 1:
            raise ValueError("samples_per_frame must be >= 1")
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if min_val >= max_val:
            raise ValueError("min_val must be smaller than max_val")
        if resync_every_frames < 0:
            raise ValueError("resync_every_frames must be >= 0")
        if resync_every_frames > 0 and not trigger_enabled:
            raise ValueError("resync_every_frames requires trigger_enabled=True")

        physical_channels: list[str] = []
        for channel in channels:
            physical_channel = self._normalize_ai_channel(channel)
            if physical_channel not in physical_channels:
                physical_channels.append(physical_channel)

        self._validate_ai_rate_request(physical_channels, rate)

        physical_trigger_source = None
        if trigger_enabled:
            physical_trigger_source = self._normalize_pfi_terminal(trigger_source)

        total_json_samples = len(physical_channels) * samples_per_frame
        if total_json_samples > AI_FRAME_MAX_JSON_SAMPLES:
            raise ValueError(
                f"unified latest_frame would return {total_json_samples} samples as JSON. "
                "Reduce channel count/samples_per_frame or use stats/buffer/file APIs."
            )

        frame_duration_seconds = samples_per_frame / rate
        frame_duration_ms = frame_duration_seconds * 1000.0
        frame_rate_hz = rate / samples_per_frame
        history_bytes_per_frame = (
            len(physical_channels) * samples_per_frame * np.dtype(np.float32).itemsize
        )
        history_capacity_frames = max(
            1,
            min(
                UNIFIED_HISTORY_MAX_FRAMES,
                UNIFIED_HISTORY_MAX_BYTES // max(1, history_bytes_per_frame),
            ),
        )

        with self._ai_lock:
            if self._ai_active_channels or self._ai_running:
                raise RuntimeError("Stop subscribe_ai/background AI before starting unified AI stream")
            if self._frame_stream_running:
                raise RuntimeError("Stop AI frame stream before starting unified AI stream")
            if self._unified_running:
                raise RuntimeError("Unified AI stream is already running")

            settings = {
                "device": self.device_name,
                "channels": physical_channels,
                "samples_per_frame": samples_per_frame,
                "rate_per_channel": rate,
                "aggregate_rate": rate * len(physical_channels),
                "terminal_config": terminal_config,
                "min_val": min_val,
                "max_val": max_val,
                "timeout": timeout,
                "trigger_enabled": trigger_enabled,
                "trigger_source": physical_trigger_source,
                "trigger_edge": trigger_edge,
                "trigger_mode": "periodic_start" if resync_every_frames > 0 else ("start_only" if trigger_enabled else "off"),
                "resync_every_frames": int(resync_every_frames),
                "frame_duration_seconds": frame_duration_seconds,
                "frame_duration_ms": frame_duration_ms,
                "frame_rate_hz": frame_rate_hz,
                "buffer_size": self._ai_buffer_size,
                "history_capacity_frames": history_capacity_frames,
                "history_bytes_per_frame": history_bytes_per_frame,
                "history_bytes_limit": UNIFIED_HISTORY_MAX_BYTES,
            }
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._unified_ai_stream_worker,
                args=(settings, stop_event),
                daemon=True,
                name="usb6363-unified-ai-stream",
            )
            self._unified_stop_event = stop_event
            self._unified_thread = thread
            self._unified_settings = dict(settings)
            self._unified_frame_id = 0
            self._unified_error = None
            self._unified_latest_frame = None
            self._unified_last_update = 0.0
            self._unified_latest = {}
            self._unified_buffers = {
                channel: deque(maxlen=self._ai_buffer_size)
                for channel in physical_channels
            }
            self._unified_sample_counts = {channel: 0 for channel in physical_channels}
            self._unified_frame_history = deque(maxlen=history_capacity_frames)
            self._unified_history_capacity_frames = history_capacity_frames
            self._unified_history_bytes_per_frame = history_bytes_per_frame
            self._unified_history_evicted_frames = 0
            self._unified_running = True
            thread.start()

        return self.get_unified_ai_stream_status()

    def stop_unified_ai_stream(self) -> dict[str, Any]:
        """停止统一 AI 数据流。"""

        self._stop_unified_ai_stream()
        return self.get_unified_ai_stream_status()

    def get_unified_ai_stream_status(self) -> dict[str, Any]:
        """查询统一 AI 数据流状态。"""

        with self._ai_lock:
            settings = dict(self._unified_settings or {})
            oldest_frame_id = (
                int(self._unified_frame_history[0]["frame_id"])
                if self._unified_frame_history
                else None
            )
            latest_history_frame_id = (
                int(self._unified_frame_history[-1]["frame_id"])
                if self._unified_frame_history
                else None
            )
            frame_rate_hz = float(settings.get("frame_rate_hz") or 0.0)
            return {
                "running": self._unified_running,
                "error": self._unified_error,
                "frame_id": self._unified_frame_id,
                "has_frame": self._unified_latest_frame is not None,
                "last_update": self._unified_last_update,
                "sample_counts": dict(self._unified_sample_counts),
                "frame_duration_seconds": settings.get("frame_duration_seconds"),
                "frame_duration_ms": settings.get("frame_duration_ms"),
                "frame_rate_hz": settings.get("frame_rate_hz"),
                "trigger_mode": settings.get("trigger_mode", "off"),
                "history_capacity_frames": self._unified_history_capacity_frames,
                "history_stored_frames": len(self._unified_frame_history),
                "history_oldest_frame_id": oldest_frame_id,
                "history_latest_frame_id": latest_history_frame_id,
                "history_evicted_frames": self._unified_history_evicted_frames,
                "history_bytes_used": (
                    len(self._unified_frame_history) * self._unified_history_bytes_per_frame
                ),
                "history_bytes_limit": UNIFIED_HISTORY_MAX_BYTES,
                "history_retention_seconds": (
                    self._unified_history_capacity_frames / frame_rate_hz
                    if frame_rate_hz > 0
                    else None
                ),
                "settings": settings,
            }

    def get_unified_ai_stream_latest_frame(self) -> dict[str, Any]:
        """返回统一 AI 数据流的最新一帧完整波形。"""

        with self._ai_lock:
            if self._unified_latest_frame is None:
                raise RuntimeError("Unified AI stream has no frame yet")
            return dict(self._unified_latest_frame)

    def get_unified_ai_frame_batch(
        self,
        after_frame_id: int,
        channels: list[str],
        max_frames: int = UNIFIED_BATCH_MAX_FRAMES,
    ) -> dict[str, Any]:
        """按 frame_id 返回统一流中尚未消费的历史帧。

        返回值含有 NumPy 数组，专门交给 HTTP server 编码成 NPZ；它不是 JSON 接口。
        上层必须检查 history_overrun，不能把已经被覆盖的历史伪装成连续数据。
        """

        if after_frame_id < 0:
            raise ValueError("after_frame_id must be >= 0")
        if not channels:
            raise ValueError("channels must not be empty")
        if not 1 <= max_frames <= UNIFIED_BATCH_MAX_FRAMES:
            raise ValueError(
                f"max_frames must be between 1 and {UNIFIED_BATCH_MAX_FRAMES}"
            )

        requested_channels: list[str] = []
        for channel in channels:
            physical_channel = self._normalize_ai_channel(channel)
            if physical_channel not in requested_channels:
                requested_channels.append(physical_channel)

        with self._ai_lock:
            settings = dict(self._unified_settings or {})
            stream_channels = [str(item) for item in settings.get("channels", [])]
            if not stream_channels:
                raise RuntimeError("Unified AI stream has not been configured")

            missing_channels = [
                channel for channel in requested_channels if channel not in stream_channels
            ]
            if missing_channels:
                raise RuntimeError(
                    "Unified AI stream does not contain channels: "
                    + ", ".join(missing_channels)
                )

            channel_indexes = [stream_channels.index(channel) for channel in requested_channels]
            samples_per_frame = int(settings.get("samples_per_frame", 0))
            selected_bytes_per_frame = (
                len(requested_channels)
                * samples_per_frame
                * np.dtype(np.float32).itemsize
            )
            payload_limited_frames = max(
                1,
                UNIFIED_BATCH_MAX_BYTES // max(1, selected_bytes_per_frame),
            )
            effective_max_frames = min(max_frames, payload_limited_frames)

            history = list(self._unified_frame_history)
            oldest_available = int(history[0]["frame_id"]) if history else 0
            latest_available = int(history[-1]["frame_id"]) if history else 0
            missing_before_first = (
                max(0, oldest_available - after_frame_id - 1) if history else 0
            )
            selected = [
                frame for frame in history if int(frame["frame_id"]) > after_frame_id
            ][:effective_max_frames]
            stream_frame_id = self._unified_frame_id

        # 数组拼接可能花费一点时间，因此离开锁以后再做，避免阻塞采集线程。
        if selected:
            values = np.stack(
                [frame["values"][channel_indexes, :] for frame in selected],
                axis=0,
            ).astype(np.float32, copy=False)
            returned_last_frame_id = int(selected[-1]["frame_id"])
        else:
            values = np.empty(
                (0, len(requested_channels), samples_per_frame),
                dtype=np.float32,
            )
            returned_last_frame_id = after_frame_id

        return {
            "frame_id": np.asarray(
                [frame["frame_id"] for frame in selected], dtype=np.int64
            ),
            "segment_id": np.asarray(
                [frame["segment_id"] for frame in selected], dtype=np.int64
            ),
            "segment_frame_id": np.asarray(
                [frame["segment_frame_id"] for frame in selected], dtype=np.int64
            ),
            "started_at": np.asarray(
                [frame["started_at"] for frame in selected], dtype=np.float64
            ),
            "finished_at": np.asarray(
                [frame["finished_at"] for frame in selected], dtype=np.float64
            ),
            "values": values,
            "channels": np.asarray(requested_channels, dtype=np.str_),
            "oldest_available_frame_id": np.asarray([oldest_available], dtype=np.int64),
            "latest_available_frame_id": np.asarray([latest_available], dtype=np.int64),
            "stream_frame_id": np.asarray([stream_frame_id], dtype=np.int64),
            "history_overrun": np.asarray(
                [missing_before_first > 0], dtype=np.bool_
            ),
            "missing_before_first": np.asarray(
                [missing_before_first], dtype=np.int64
            ),
            "has_more": np.asarray(
                [returned_last_frame_id < latest_available], dtype=np.bool_
            ),
        }

    def get_unified_ai_latest(self, channel: str) -> dict[str, Any]:
        """读取统一 AI 数据流里某个通道的最近一个点。"""

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._unified_buffers:
                raise RuntimeError(f"{physical_channel} is not in unified AI stream")
            if physical_channel not in self._unified_latest:
                raise RuntimeError(f"{physical_channel} has no unified sampled data yet")
            settings = dict(self._unified_settings or {})
            return {
                "device": self.device_name,
                "channel": physical_channel,
                "rate": settings.get("rate_per_channel", 0.0),
                "value": self._unified_latest[physical_channel],
                "last_update": self._unified_last_update,
                "sample_count": self._unified_sample_counts.get(physical_channel, 0),
                "frame_id": self._unified_frame_id,
            }

    def get_unified_ai_buffer(self, channel: str, max_samples: int = 1000) -> dict[str, Any]:
        """读取统一 AI 数据流里某个通道最近的一段缓存。"""

        if max_samples < 1:
            raise ValueError("max_samples must be >= 1")

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._unified_buffers:
                raise RuntimeError(f"{physical_channel} is not in unified AI stream")
            values = list(self._unified_buffers.get(physical_channel, []))[-max_samples:]
            settings = dict(self._unified_settings or {})
            return {
                "device": self.device_name,
                "channel": physical_channel,
                "rate": settings.get("rate_per_channel", 0.0),
                "samples": len(values),
                "values": values,
                "last_update": self._unified_last_update,
                "sample_count": self._unified_sample_counts.get(physical_channel, 0),
                "frame_id": self._unified_frame_id,
            }

    def get_unified_ai_stats(self, channel: str, max_samples: int = 10000) -> dict[str, Any]:
        """返回统一 AI 数据流里某个通道最近缓存的统计量。"""

        if max_samples < 1:
            raise ValueError("max_samples must be >= 1")

        physical_channel = self._normalize_ai_channel(channel)
        with self._ai_lock:
            if physical_channel not in self._unified_buffers:
                raise RuntimeError(f"{physical_channel} is not in unified AI stream")
            values = list(self._unified_buffers.get(physical_channel, []))[-max_samples:]
            settings = dict(self._unified_settings or {})
            last_update = self._unified_last_update
            sample_count = self._unified_sample_counts.get(physical_channel, 0)
            frame_id = self._unified_frame_id

        if not values:
            raise RuntimeError(f"{physical_channel} has no unified sampled data yet")

        data = np.asarray(values, dtype=np.float64)
        return {
            "device": self.device_name,
            "channel": physical_channel,
            "rate": settings.get("rate_per_channel", 0.0),
            "samples": int(data.size),
            "mean": float(np.mean(data)),
            "std": float(np.std(data)),
            "min": float(np.min(data)),
            "max": float(np.max(data)),
            "rms": float(np.sqrt(np.mean(np.square(data)))),
            "last": float(data[-1]),
            "last_update": last_update,
            "sample_count": sample_count,
            "frame_id": frame_id,
        }

    # ---------------------------------------------------------------------
    # AO / PFI
    # ---------------------------------------------------------------------
    def write_ao_voltage(
        self,
        channel: str = "ao0",
        value: float = 0.0,
        min_val: float = -10.0,
        max_val: float = 10.0,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """输出一个静态模拟电压 AO。"""

        if not min_val <= value <= max_val:
            raise ValueError(f"value must be between {min_val} and {max_val}")

        physical_channel = self._normalize_ao_channel(channel)
        with self._hardware_lock:
            nidaqmx_driver.write_ao_voltage(
                device_name=self.device_name,
                physical_channel=physical_channel,
                value=float(value),
                min_val=min_val,
                max_val=max_val,
                timeout=timeout,
            )

        return {
            "device": self.device_name,
            "channel": physical_channel,
            "value": float(value),
        }

    def read_digital_line(self, line: str = "PFI0", timeout: float = 10.0) -> dict[str, Any]:
        """读取一个数字线或 PFI 端子的高低电平。"""

        physical_line = self._normalize_digital_line(line)
        with self._hardware_lock:
            value = nidaqmx_driver.read_digital_line(
                device_name=self.device_name,
                physical_line=physical_line,
                timeout=timeout,
            )

        return {
            "device": self.device_name,
            "line": physical_line,
            "value": value,
        }

    def write_digital_line(
        self,
        line: str = "PFI0",
        value: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """写一个数字线或 PFI 端子的高低电平。"""

        physical_line = self._normalize_digital_line(line)
        with self._hardware_lock:
            nidaqmx_driver.write_digital_line(
                device_name=self.device_name,
                physical_line=physical_line,
                value=bool(value),
                timeout=timeout,
            )

        return {
            "device": self.device_name,
            "line": physical_line,
            "value": bool(value),
        }

    def count_pfi_edges(
        self,
        line: str = "PFI0",
        counter: str = "ctr0",
        seconds: float = 1.0,
        edge: str = "RISING",
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """用计数器统计某个 PFI 端子在一段时间内出现了多少个边沿。"""

        if seconds <= 0:
            raise ValueError("seconds must be > 0")

        terminal = self._normalize_pfi_terminal(line)
        physical_counter = self._normalize_counter(counter)
        with self._hardware_lock:
            result = nidaqmx_driver.count_pfi_edges(
                device_name=self.device_name,
                terminal=terminal,
                physical_counter=physical_counter,
                seconds=seconds,
                edge_name=edge,
                timeout=timeout,
            )

        return {
            "device": self.device_name,
            "line": terminal,
            "counter": physical_counter,
            "seconds": seconds,
            "edge": result["edge"],
            "count": result["count"],
        }

    # ---------------------------------------------------------------------
    # 内部 AI 线程
    # ---------------------------------------------------------------------
    def _calculate_ai_rate(self, channels: list[str]) -> float:
        """根据活跃 AI 通道数量计算每个通道的采样率。"""

        channel_count = len(channels)
        if channel_count == 0:
            return 0.0
        if channel_count == 1:
            return AI_SINGLE_CHANNEL_MAX_RATE
        return AI_MULTI_CHANNEL_AGGREGATE_RATE / channel_count

    def _validate_ai_rate_request(self, channels: list[str], rate: float) -> None:
        """校验手动指定的 AI 采样率是否符合当前项目约定。

        约定是：
        - 单通道最高 2 MHz。
        - 多通道总采样率最高 1 MHz，因此每通道 rate * 通道数不能超过 1 MHz。
        """

        channel_count = len(channels)
        if channel_count <= 0:
            raise ValueError("channels must not be empty")
        if channel_count == 1:
            if rate > AI_SINGLE_CHANNEL_MAX_RATE:
                raise ValueError(
                    f"single-channel rate must be <= {AI_SINGLE_CHANNEL_MAX_RATE:g} Hz"
                )
            return
        aggregate_rate = rate * channel_count
        if aggregate_rate > AI_MULTI_CHANNEL_AGGREGATE_RATE:
            raise ValueError(
                f"multi-channel aggregate rate {aggregate_rate:g} Hz exceeds "
                f"{AI_MULTI_CHANNEL_AGGREGATE_RATE:g} Hz"
            )

    def _restart_ai_sampling(self) -> None:
        """按当前订阅通道重启后台 AI 连续采样任务。"""

        self._stop_ai_sampling()

        with self._ai_lock:
            channels = list(self._ai_active_channels)
            if not channels:
                self._ai_rate = 0.0
                self._ai_running = False
                self._ai_error = None
                return

            self._ai_rate = self._calculate_ai_rate(channels)
            self._ai_error = None

            for channel in channels:
                self._ai_buffers.setdefault(channel, deque(maxlen=self._ai_buffer_size))
                self._ai_sample_counts.setdefault(channel, 0)

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._ai_sampling_worker,
                args=(channels, self._ai_rate, stop_event),
                daemon=True,
                name="usb6363-ai-sampling",
            )
            self._ai_stop_event = stop_event
            self._ai_thread = thread
            self._ai_running = True
            thread.start()

    def _stop_ai_sampling(self) -> None:
        """停止当前后台 AI 连续采样任务。"""

        with self._ai_lock:
            stop_event = self._ai_stop_event
            thread = self._ai_thread
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._ai_lock:
            if self._ai_thread is thread:
                self._ai_thread = None
                self._ai_stop_event = None
                self._ai_running = False

    def _stop_ai_frame_stream(self) -> None:
        """停止固定点数分帧采集线程。"""

        with self._ai_lock:
            stop_event = self._frame_stream_stop_event
            thread = self._frame_stream_thread
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._ai_lock:
            if self._frame_stream_thread is thread:
                self._frame_stream_thread = None
                self._frame_stream_stop_event = None
                self._frame_stream_running = False

    def _stop_unified_ai_stream(self) -> None:
        """停止统一 AI 数据流线程。"""

        with self._ai_lock:
            stop_event = self._unified_stop_event
            thread = self._unified_thread
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._ai_lock:
            if self._unified_thread is thread:
                self._unified_thread = None
                self._unified_stop_event = None
                self._unified_running = False

    def _unified_ai_stream_worker(
        self,
        settings: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        """统一 AI 数据流线程。

        这个线程和旧 frame_stream 一样按固定点数读帧；
        额外维护每通道 latest/buffer/sample_count，供慢漂、示波器等模块读取小 JSON。
        """

        channels = list(settings["channels"])
        samples_per_frame = int(settings["samples_per_frame"])
        resync_every_frames = int(settings.get("resync_every_frames", 0))
        segment_id = 0
        try:
            while not stop_event.is_set():
                segment_id += 1
                segment_frame_id = 0
                task = nidaqmx_driver.create_continuous_ai_task(
                    channels=channels,
                    rate=float(settings["rate_per_channel"]),
                    samples_per_read=samples_per_frame,
                    terminal_config_name=str(settings["terminal_config"]),
                    min_val=float(settings["min_val"]),
                    max_val=float(settings["max_val"]),
                    start_trigger_source=settings["trigger_source"],
                    start_trigger_edge_name=str(settings["trigger_edge"]),
                )
                try:
                    while not stop_event.is_set():
                        channel_values = nidaqmx_driver.read_continuous_ai_chunk(
                            task=task,
                            samples_per_read=samples_per_frame,
                            channel_count=len(channels),
                            timeout=float(settings["timeout"]),
                        )
                        now = time.time()
                        segment_frame_id += 1
                        # 转换放在状态锁之外，避免大数组复制阻塞状态查询和批量读取。
                        history_values = np.ascontiguousarray(
                            channel_values,
                            dtype=np.float32,
                        )
                        with self._ai_lock:
                            if stop_event.is_set():
                                break
                            self._unified_frame_id += 1
                            frame = {
                                "device": self.device_name,
                                "channels": channels,
                                "channel_count": len(channels),
                                "samples_per_channel": samples_per_frame,
                                "rate_per_channel": float(settings["rate_per_channel"]),
                                "aggregate_rate": float(settings["aggregate_rate"]),
                                "terminal_config": str(settings["terminal_config"]),
                                "min_val": float(settings["min_val"]),
                                "max_val": float(settings["max_val"]),
                                "trigger_enabled": bool(settings["trigger_enabled"]),
                                "trigger_source": settings["trigger_source"],
                                "trigger_edge": str(settings["trigger_edge"]),
                                "trigger_mode": str(settings["trigger_mode"]),
                                "resync_every_frames": resync_every_frames,
                                "segment_id": segment_id,
                                "segment_frame_id": segment_frame_id,
                                "frame_duration_seconds": float(settings["frame_duration_seconds"]),
                                "frame_duration_ms": float(settings["frame_duration_ms"]),
                                "frame_rate_hz": float(settings["frame_rate_hz"]),
                                "frame_id": self._unified_frame_id,
                                "started_at": now,
                                "finished_at": now,
                                "values": channel_values,
                            }
                            self._unified_latest_frame = frame
                            if (
                                self._unified_frame_history.maxlen is not None
                                and len(self._unified_frame_history)
                                == self._unified_frame_history.maxlen
                            ):
                                self._unified_history_evicted_frames += 1
                            self._unified_frame_history.append(
                                {
                                    "frame_id": self._unified_frame_id,
                                    "segment_id": segment_id,
                                    "segment_frame_id": segment_frame_id,
                                    "started_at": now,
                                    "finished_at": now,
                                    "values": history_values,
                                }
                            )
                            for channel, values in zip(channels, channel_values):
                                if not values:
                                    continue
                                self._unified_latest[channel] = values[-1]
                                self._unified_buffers.setdefault(
                                    channel,
                                    deque(maxlen=self._ai_buffer_size),
                                ).extend(values)
                                self._unified_sample_counts[channel] = (
                                    self._unified_sample_counts.get(channel, 0) + len(values)
                                )
                            self._unified_last_update = now
                            self._unified_error = None

                        # 周期重对齐：关闭当前 task，外层循环会新建 task 并重新等待 PFI 边沿。
                        if resync_every_frames > 0 and segment_frame_id >= resync_every_frames:
                            break
                finally:
                    task.close()
        except Exception as exc:
            with self._ai_lock:
                self._unified_error = str(exc)
        finally:
            with self._ai_lock:
                if self._unified_thread is threading.current_thread():
                    self._unified_running = False

    def _ai_frame_stream_worker(
        self,
        settings: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        """固定点数分帧采集线程。"""

        channels = list(settings["channels"])
        samples_per_frame = int(settings["samples_per_frame"])
        resync_every_frames = int(settings.get("resync_every_frames", 0))
        segment_id = 0
        try:
            while not stop_event.is_set():
                segment_id += 1
                segment_frame_id = 0
                task = nidaqmx_driver.create_continuous_ai_task(
                    channels=channels,
                    rate=float(settings["rate_per_channel"]),
                    samples_per_read=samples_per_frame,
                    terminal_config_name=str(settings["terminal_config"]),
                    min_val=float(settings["min_val"]),
                    max_val=float(settings["max_val"]),
                    start_trigger_source=settings["trigger_source"],
                    start_trigger_edge_name=str(settings["trigger_edge"]),
                )
                try:
                    while not stop_event.is_set():
                        channel_values = nidaqmx_driver.read_continuous_ai_chunk(
                            task=task,
                            samples_per_read=samples_per_frame,
                            channel_count=len(channels),
                            timeout=float(settings["timeout"]),
                        )
                        now = time.time()
                        segment_frame_id += 1
                        with self._ai_lock:
                            if stop_event.is_set():
                                break
                            self._frame_stream_frame_id += 1
                            self._frame_stream_latest = {
                                "device": self.device_name,
                                "channels": channels,
                                "channel_count": len(channels),
                                "samples_per_channel": samples_per_frame,
                                "rate_per_channel": float(settings["rate_per_channel"]),
                                "aggregate_rate": float(settings["aggregate_rate"]),
                                "terminal_config": str(settings["terminal_config"]),
                                "min_val": float(settings["min_val"]),
                                "max_val": float(settings["max_val"]),
                                "trigger_enabled": bool(settings["trigger_enabled"]),
                                "trigger_source": settings["trigger_source"],
                                "trigger_edge": str(settings["trigger_edge"]),
                                "trigger_mode": str(settings["trigger_mode"]),
                                "resync_every_frames": resync_every_frames,
                                "segment_id": segment_id,
                                "segment_frame_id": segment_frame_id,
                                "frame_duration_seconds": float(settings["frame_duration_seconds"]),
                                "frame_duration_ms": float(settings["frame_duration_ms"]),
                                "frame_rate_hz": float(settings["frame_rate_hz"]),
                                "frame_id": self._frame_stream_frame_id,
                                "started_at": now,
                                "finished_at": now,
                                "values": channel_values,
                            }
                            self._frame_stream_error = None

                        # 如果启用了 PFI 周期重对齐，读满 N 帧就退出内层循环。
                        # finally 会关闭当前 task，外层 while 会立刻新建 task 并重新等待 PFI 边沿。
                        if resync_every_frames > 0 and segment_frame_id >= resync_every_frames:
                            break
                finally:
                    task.close()
        except Exception as exc:
            with self._ai_lock:
                self._frame_stream_error = str(exc)
        finally:
            with self._ai_lock:
                if self._frame_stream_thread is threading.current_thread():
                    self._frame_stream_running = False

    def _ai_sampling_worker(
        self,
        channels: list[str],
        rate: float,
        stop_event: threading.Event,
    ) -> None:
        """后台 AI 采样线程。"""

        samples_per_read = max(2, int(rate * AI_READ_CHUNK_SECONDS))

        try:
            task = nidaqmx_driver.create_continuous_ai_task(
                channels=channels,
                rate=rate,
                samples_per_read=samples_per_read,
            )
            try:
                while not stop_event.is_set():
                    channel_values = nidaqmx_driver.read_continuous_ai_chunk(
                        task=task,
                        samples_per_read=samples_per_read,
                        channel_count=len(channels),
                        timeout=2.0,
                    )
                    now = time.time()

                    with self._ai_lock:
                        if stop_event.is_set():
                            break
                        for channel, values in zip(channels, channel_values):
                            if not values:
                                continue
                            self._ai_latest[channel] = values[-1]
                            self._ai_buffers.setdefault(
                                channel,
                                deque(maxlen=self._ai_buffer_size),
                            ).extend(values)
                            self._ai_sample_counts[channel] = (
                                self._ai_sample_counts.get(channel, 0) + len(values)
                            )
                        self._append_ai_recording_locked(channels, channel_values)
                        self._ai_last_update = now
                        self._ai_error = None
            finally:
                task.close()

        except Exception as exc:
            with self._ai_lock:
                self._ai_error = str(exc)
        finally:
            with self._ai_lock:
                if self._ai_thread is threading.current_thread():
                    self._ai_running = False

    def _append_ai_recording_locked(
        self,
        channels: list[str],
        channel_values: list[list[float]],
    ) -> None:
        """把当前数据块追加到文件记录器。

        调用这个函数时外层已经持有 _ai_lock。
        """

        recording = self._ai_recording
        if recording is None:
            return
        if recording["channels"] != channels:
            recording["done_event"].set()
            return

        target_samples = recording["target_samples"]
        buffers = recording["buffers"]

        for channel, values in zip(channels, channel_values):
            current = buffers[channel]
            remaining = target_samples - len(current)
            if remaining > 0:
                current.extend(values[:remaining])

        if all(len(buffers[channel]) >= target_samples for channel in channels):
            recording["done_event"].set()

    # ---------------------------------------------------------------------
    # 通道名校验
    # ---------------------------------------------------------------------
    def _normalize_ai_channel(self, channel: str) -> str:
        return self._normalize_channel(channel, kind="ai")

    def _normalize_ao_channel(self, channel: str) -> str:
        return self._normalize_channel(channel, kind="ao")

    def _normalize_channel(self, channel: str, kind: str) -> str:
        """统一并校验 AI/AO 通道名。"""

        pattern = _AI_RE if kind == "ai" else _AO_RE
        match = pattern.match(channel)
        if match is None:
            raise ValueError(f"Invalid {kind.upper()} channel: {channel!r}")

        device = match.group("device") or self.device_name
        index = int(match.group("index"))
        if device != self.device_name:
            raise ValueError(f"Only {self.device_name} is allowed, got {device}")

        max_index = 31 if kind == "ai" else 3
        if index > max_index:
            raise ValueError(f"{kind.upper()} channel index must be 0-{max_index}")

        return f"{self.device_name}/{kind}{index}"

    def _normalize_digital_line(self, line: str) -> str:
        """统一并校验数字线或 PFI 端子名。"""

        pfi_match = _PFI_RE.match(line)
        if pfi_match is not None:
            return self._normalize_pfi_terminal(line).lstrip("/")

        dio_match = _DIO_RE.match(line)
        if dio_match is None:
            raise ValueError(f"Invalid digital line: {line!r}")

        device = dio_match.group("device") or self.device_name
        port = int(dio_match.group("port"))
        line_index = int(dio_match.group("line"))

        if device != self.device_name:
            raise ValueError(f"Only {self.device_name} is allowed, got {device}")
        if port == 0:
            max_line = 31
        elif port in (1, 2):
            max_line = 7
        else:
            raise ValueError("Digital port must be 0, 1, or 2")
        if line_index > max_line:
            raise ValueError(f"port{port} line index must be 0-{max_line}")

        return f"{self.device_name}/port{port}/line{line_index}"

    def _normalize_pfi_terminal(self, line: str) -> str:
        """统一并校验 PFI 端子名，返回 /Dev2/PFIx。"""

        match = _PFI_RE.match(line)
        if match is None:
            raise ValueError(f"Invalid PFI terminal: {line!r}")

        device = match.group("device") or self.device_name
        index = int(match.group("index"))

        if device != self.device_name:
            raise ValueError(f"Only {self.device_name} is allowed, got {device}")
        if index > 15:
            raise ValueError("PFI index must be 0-15")

        return f"/{self.device_name}/PFI{index}"

    def _normalize_counter(self, counter: str) -> str:
        """统一并校验计数器名，USB-6363 有 ctr0-ctr3。"""

        match = _CTR_RE.match(counter)
        if match is None:
            raise ValueError(f"Invalid counter: {counter!r}")

        device = match.group("device") or self.device_name
        index = int(match.group("index"))

        if device != self.device_name:
            raise ValueError(f"Only {self.device_name} is allowed, got {device}")
        if index > 3:
            raise ValueError("Counter index must be 0-3")

        return f"{self.device_name}/ctr{index}"


__all__ = ["DaqController", "DEVICE_NAME", "DeviceInfo"]
