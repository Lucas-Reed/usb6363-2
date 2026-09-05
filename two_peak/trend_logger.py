"""双窗口面积与峰高慢漂记录器。

这个模块只负责一件事：在后端长期记录动态窗口内面积和峰高的慢漂趋势。
它不直接访问 nidaqmx，而是通过 Usb6363Client 读取统一 AI 流的最新帧。

当前统计原则：
- A、B 两个窗口独立跟随各自的最高点，窗口宽度保持不变。
- 面积使用窗口内原始采样值求和，峰高使用同一窗口内原始采样值的最大值。
- Top 指标使用同一原始窗口内最高指定百分比采样点的平均值。
- 用最近 N 帧计算滑动均值、标准差、相对标准差和 shot-to-shot 抖动。
- 面积、峰高与 Top 使用同一个 EMA alpha，便于直接比较三种测量量。
- 按指定记录频率写 CSV，适合一整晚观察慢漂。
"""

from __future__ import annotations

import csv
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from two_peak.signal import (
    measure_manual_area,
    measure_top_fraction_mean,
    track_and_measure_manual_area,
)
from two_peak.window_voltage_recorder import WindowVoltageRecorder
from usb6363_client import Usb6363Client


class FrameHistoryError(RuntimeError):
    """表示历史帧已经不连续，继续记录会产生无法察觉的数据缺口。"""


@dataclass
class AreaTrendSample:
    """一帧动态窗口测量数据。

    面积和峰高来自同一帧、同一个窗口。峰高使用移动完成后的原始波形最大值，
    因此不会因为跟随定位使用了平滑波形而改变峰高本身的物理含义。
    """

    frame_id: int
    timestamp: float
    area: float
    peak_height: float
    top_value: float
    top_point_count: int
    area2: float | None = None
    peak2_height: float | None = None
    top2_value: float | None = None
    top2_point_count: int | None = None
    area_left: int | None = None
    area_right: int | None = None
    area_peak_index: int | None = None
    peak_height_index: int | None = None
    area2_left: int | None = None
    area2_right: int | None = None
    area2_peak_index: int | None = None
    peak2_height_index: int | None = None


class AreaTrendLogger:
    """后端慢漂 CSV 记录器。

    WebUI 可以关闭或刷新，只要 viewer 后端还在运行，本 logger 就能继续记录。
    """

    def __init__(self, daq: Usb6363Client, output_dir: Path) -> None:
        self._daq = daq
        self._output_dir = output_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._running = False
        self._error: str | None = None
        self._csv_path: Path | None = None
        self._settings: dict[str, Any] = {}
        self._frames_seen = 0
        self._records_written = 0
        self._last_frame_id = 0
        self._latest_stats: dict[str, Any] | None = None
        self._recent_stats: deque[dict[str, Any]] = deque(maxlen=1000)
        self._voltage_recorder: WindowVoltageRecorder | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._stop_reason: str | None = None
        # WebUI 的运行中边界更新先放到这里，再由记录线程在两帧之间原子应用。
        # 这样面积、峰高、Top 和 NPZ 不会在同一帧混用新旧边界。
        self._pending_window_update: dict[str, Any] | None = None
        self._window_revision = 0

    def start(
        self,
        analysis_channel_index: int,
        area_left: int,
        area_right: int,
        area2_left: int | None = None,
        area2_right: int | None = None,
        window_frames: int = 200,
        record_hz: float = 1.0,
        duration_minutes: float = 0.0,
        ema_alpha: float = 0.02,
        top_percent: float = 10.0,
        auto_track_enabled: bool = False,
        auto_track_smooth_window: int = 9,
        auto_track_max_shift: int = 5,
        poll_interval: float = 0.05,
        stream_source: str = "unified_stream",
        channels: list[str] | None = None,
        window_voltage_mode: str = "none",
        record_full_frame: bool = False,
        window_voltage_output_dir: Path | None = None,
        session_id: str | None = None,
        trigger_unix_time: float | None = None,
        start_after_frame_id: int = 0,
    ) -> dict[str, Any]:
        """启动长期记录。

        参数说明：
        - analysis_channel_index：分析哪一路 AI。
        - area_left/area_right：手动面积窗口左右边界。
        - area2_left/area2_right：可选的第二个面积窗口，用于同时记录另一个峰。
        - window_frames：每个 CSV 点使用最近多少帧做滑动统计。
        - record_hz：每秒写几行 CSV，例如 1 Hz 表示每秒记录一次。
        - duration_minutes：自动停止时长，单位为分钟；0 表示一直记录到手动停止。
        - top_percent：每个最终窗口内参与 Top 均值的最高采样点比例。
        - poll_interval：后端检查新帧的间隔，一般保持默认即可。
        """

        if window_frames < 2:
            raise ValueError("window_frames must be >= 2")
        if record_hz <= 0:
            raise ValueError("record_hz must be > 0")
        if duration_minutes < 0:
            raise ValueError("duration_minutes must be >= 0")
        if ema_alpha < 0 or ema_alpha > 1:
            raise ValueError("ema_alpha must be between 0 and 1")
        if not np.isfinite(top_percent) or top_percent <= 0 or top_percent > 100:
            raise ValueError("top_percent must be > 0 and <= 100")
        if auto_track_smooth_window < 1:
            raise ValueError("auto_track_smooth_window must be >= 1")
        if auto_track_max_shift < 0:
            raise ValueError("auto_track_max_shift must be >= 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        voltage_mode = str(window_voltage_mode).strip().lower()
        if voltage_mode not in ("none", "a", "b", "both"):
            raise ValueError("window_voltage_mode must be none, a, b or both")
        if voltage_mode in ("b", "both") and (area2_left is None or area2_right is None):
            raise ValueError("记录窗口 B 电压时，必须同时设置面积 B 的左右边界")
        if start_after_frame_id < 0:
            raise ValueError("start_after_frame_id must be >= 0")

        actual_start_after_frame_id = int(start_after_frame_id)
        source_stream_settings: dict[str, Any] = {}
        if str(stream_source) == "unified_stream":
            # 普通记录可能在统一流运行很久以后才开始。此时从“点击开始”的当前帧
            # 往后记录，而不是错误地要求补回从 frame 1 开始的全部历史。
            stream_status = self._daq.get_unified_ai_stream_status()
            source_stream_settings = dict(stream_status.get("settings") or {})
            if actual_start_after_frame_id == 0:
                actual_start_after_frame_id = int(stream_status.get("frame_id", 0))

        with self._lock:
            if self._running:
                raise RuntimeError("Area trend logger is already running")

            self._output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = self._output_dir / f"area_trend_{timestamp}.csv"
            actual_session_id = session_id or f"{timestamp}_{uuid4().hex[:8]}"
            configured_channels = list(channels or [])
            analysis_channel = (
                configured_channels[analysis_channel_index]
                if 0 <= analysis_channel_index < len(configured_channels)
                else None
            )

            settings = {
                "analysis_channel_index": int(analysis_channel_index),
                "analysis_channel": analysis_channel,
                "area_left": int(area_left),
                "area_right": int(area_right),
                "area2_left": None if area2_left is None else int(area2_left),
                "area2_right": None if area2_right is None else int(area2_right),
                "window_frames": int(window_frames),
                "record_hz": float(record_hz),
                "duration_minutes": float(duration_minutes),
                "duration_seconds": float(duration_minutes) * 60.0,
                "ema_alpha": float(ema_alpha),
                "top_percent": float(top_percent),
                "auto_track_enabled": bool(auto_track_enabled),
                "auto_track_smooth_window": int(auto_track_smooth_window),
                "auto_track_max_shift": int(auto_track_max_shift),
                "poll_interval": float(poll_interval),
                "stream_source": str(stream_source),
                "channels": configured_channels,
                "window_voltage_mode": voltage_mode,
                "record_full_frame": bool(record_full_frame),
                # 保存记录开始时的统一流参数快照，离线读取 full_values 时可据此
                # 还原物理通道、每通道采样率和每帧点数。
                "source_stream_settings": source_stream_settings,
                "window_voltage_output_dir": str(
                    window_voltage_output_dir
                    or (self._output_dir.parent / "two_peak_window_voltage")
                ),
                "session_id": actual_session_id,
                "trigger_unix_time": trigger_unix_time,
                "start_after_frame_id": actual_start_after_frame_id,
                "window_revision": 1,
            }

            voltage_recorder = None
            if voltage_mode != "none" or record_full_frame:
                voltage_recorder = WindowVoltageRecorder(
                    output_dir=Path(settings["window_voltage_output_dir"]),
                    session_id=actual_session_id,
                    mode=voltage_mode,
                    metadata=settings,
                    record_full_frame=bool(record_full_frame),
                )
                voltage_recorder.start()

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(settings, csv_path, stop_event, voltage_recorder),
                daemon=True,
                name="two-peak-area-trend-logger",
            )
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
            self._error = None
            self._csv_path = csv_path
            self._settings = dict(settings)
            self._frames_seen = 0
            self._records_written = 0
            self._last_frame_id = 0
            self._latest_stats = None
            self._recent_stats.clear()
            self._voltage_recorder = voltage_recorder
            self._started_at = time.time()
            self._finished_at = None
            self._stop_reason = None
            self._pending_window_update = None
            self._window_revision = 1
            try:
                thread.start()
            except Exception:
                if voltage_recorder is not None:
                    voltage_recorder.stop()
                self._running = False
                self._voltage_recorder = None
                raise

        return self.status()

    def update_area_windows(
        self,
        area_left: int,
        area_right: int,
        area2_left: int | None,
        area2_right: int | None,
    ) -> dict[str, Any]:
        """让正在运行的慢漂记录从下一帧开始使用新的 A/B 边界。

        修改边界会改变测量量本身，因此记录线程会清空最近 N 帧统计并重新建立 EMA。
        如果正在保存 A/B 逐点 NPZ，则对应窗口点数必须保持不变，保证每个 NPZ
        数组仍是规则的二维矩阵。完整波形记录不受这个宽度限制。
        """

        new_a = _validate_window_pair(area_left, area_right, "A")
        new_b = _validate_optional_window_pair(area2_left, area2_right, "B")

        with self._lock:
            if not self._running:
                raise RuntimeError("面积慢漂记录没有运行")

            latest = self._latest_stats or {}
            current_a = (
                int(latest.get("area_left", self._settings["area_left"])),
                int(latest.get("area_right", self._settings["area_right"])),
            )
            current_b = _current_optional_window(self._settings, latest)
            if (current_b is None) != (new_b is None):
                raise ValueError("运行中不能新增或删除窗口 B，只能修改已有窗口的边界")

            samples_per_frame = int(
                (self._settings.get("source_stream_settings") or {}).get(
                    "samples_per_frame",
                    0,
                )
                or 0
            )
            if samples_per_frame > 0:
                _validate_window_in_frame(new_a, samples_per_frame, "A")
                if new_b is not None:
                    _validate_window_in_frame(new_b, samples_per_frame, "B")

            voltage_mode = str(self._settings.get("window_voltage_mode", "none"))
            if voltage_mode in ("a", "both") and _window_width(new_a) != _window_width(current_a):
                raise ValueError("正在保存窗口 A 逐点 NPZ，运行中只能平移 A，不能改变点数")
            if (
                voltage_mode in ("b", "both")
                and current_b is not None
                and new_b is not None
                and _window_width(new_b) != _window_width(current_b)
            ):
                raise ValueError("正在保存窗口 B 逐点 NPZ，运行中只能平移 B，不能改变点数")

            self._window_revision += 1
            update = {
                "area_left": new_a[0],
                "area_right": new_a[1],
                "area2_left": None if new_b is None else new_b[0],
                "area2_right": None if new_b is None else new_b[1],
                "window_revision": self._window_revision,
            }
            self._pending_window_update = update
            self._settings.update(update)
            # 曲线历史不能跨越统计口径变化继续连接；最新值保留到下一帧新结果产生，
            # 避免功率锁定在这个极短间隔里因为反馈字段暂时不存在而退出。
            self._recent_stats.clear()

        return self.status()

    def stop(self) -> dict[str, Any]:
        """停止长期记录。"""

        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            if self._running and self._stop_reason is None:
                self._stop_reason = "manual"
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
                self._running = False

        return self.status()

    def status(self) -> dict[str, Any]:
        """返回当前记录状态。"""

        with self._lock:
            voltage_recorder = self._voltage_recorder
            now = time.time()
            elapsed_seconds = (
                max(0.0, (self._finished_at or now) - self._started_at)
                if self._started_at is not None
                else 0.0
            )
            duration_seconds = float(self._settings.get("duration_seconds", 0.0))
            remaining_seconds = (
                max(0.0, duration_seconds - elapsed_seconds)
                if duration_seconds > 0 and self._running
                else None
            )
            result = {
                "running": self._running,
                "error": self._error,
                "stop_reason": self._stop_reason,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed_seconds": elapsed_seconds,
                "remaining_seconds": remaining_seconds,
                "csv_file": str(self._csv_path.resolve()) if self._csv_path else None,
                "settings": dict(self._settings),
                "frames_seen": self._frames_seen,
                "records_written": self._records_written,
                "last_frame_id": self._last_frame_id,
                "window_revision": self._window_revision,
                "latest_stats": dict(self._latest_stats or {}),
                "recent_stats": [dict(row) for row in self._recent_stats],
            }
        result["window_voltage"] = (
            voltage_recorder.status()
            if voltage_recorder is not None
            else {
                "enabled": False,
                "running": False,
                "mode": "none",
                "record_full_frame": False,
                "frames_written": 0,
                "source_gap_frames": 0,
                "queue_dropped_frames": 0,
                "chunks_written": 0,
                "bytes_written": 0,
                "error": None,
            }
        )
        return result

    def _worker(
        self,
        settings: dict[str, Any],
        csv_path: Path,
        stop_event: threading.Event,
        voltage_recorder: WindowVoltageRecorder | None,
    ) -> None:
        """记录线程主体。"""

        samples: deque[AreaTrendSample] = deque(maxlen=max(10_000, int(settings["window_frames"]) * 5))
        last_seen_frame_id = int(settings.get("start_after_frame_id", 0))
        last_recorded_frame_id = int(settings.get("start_after_frame_id", 0))
        record_period = 1.0 / float(settings["record_hz"])
        next_record_timestamp: float | None = None
        ema_alpha = float(settings.get("ema_alpha", 0.02))
        area_ema: float | None = None
        area2_ema: float | None = None
        area_sum_ema: float | None = None
        peak_height_ema: float | None = None
        peak2_height_ema: float | None = None
        top_ema: float | None = None
        top2_ema: float | None = None
        duration_seconds = float(settings.get("duration_seconds", 0.0))
        deadline = time.monotonic() + duration_seconds if duration_seconds > 0 else None

        try:
            with csv_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=_csv_fieldnames())
                writer.writeheader()

                while not stop_event.is_set():
                    # 自动停止由后端线程执行，因此即使关闭浏览器页面，计时仍然可靠。
                    # 达到时限后退出同一个 worker，让 CSV 和 NPZ 走统一的正常收尾流程。
                    if deadline is not None and time.monotonic() >= deadline:
                        with self._lock:
                            self._stop_reason = "duration_elapsed"
                        break
                    try:
                        status = self._get_stream_status(settings)
                        if status.get("running") is not True and not status.get("has_frame"):
                            # 采集源根本没有启动时继续轮询不会产生数据，却会让 WebUI
                            # 长期显示 running=true。把它作为明确的启动/运行错误结束记录。
                            raise RuntimeError("AI stream is not running and has no frame")

                        frames, has_more = self._get_stream_frames(
                            settings,
                            last_seen_frame_id,
                            status,
                        )
                        for frame in frames:
                            with self._lock:
                                window_update = self._pending_window_update
                                self._pending_window_update = None
                            if window_update is not None:
                                # 新边界会改变测量量的定义，所以最近 N 帧和所有 EMA
                                # 都必须从头建立，不能把不同区间的数据混在一起。
                                settings.update(window_update)
                                samples.clear()
                                next_record_timestamp = None
                                area_ema = None
                                area2_ema = None
                                area_sum_ema = None
                                peak_height_ema = None
                                peak2_height_ema = None
                                top_ema = None
                                top2_ema = None

                            frame_id = int(frame.get("frame_id", 0))
                            expected_frame_id = last_seen_frame_id + 1
                            if (
                                settings.get("stream_source") == "unified_stream"
                                and frame_id != expected_frame_id
                            ):
                                raise FrameHistoryError(
                                    "统一 AI 历史帧不连续："
                                    f"期望 frame_id={expected_frame_id}，实际得到 {frame_id}。"
                                    "记录已停止，避免生成带有隐藏缺口的数据。"
                                )

                            sample = self._measure_frame(frame, settings)
                            samples.append(sample)
                            if voltage_recorder is not None:
                                voltage_recorder.append(
                                    _build_window_voltage_record(
                                        frame,
                                        sample,
                                        analysis_channel_index=int(
                                            settings["analysis_channel_index"]
                                        ),
                                        mode=str(settings["window_voltage_mode"]),
                                        record_full_frame=bool(
                                            settings.get("record_full_frame", False)
                                        ),
                                    )
                                )
                            last_seen_frame_id = sample.frame_id
                            with self._lock:
                                self._frames_seen += 1
                                self._last_frame_id = sample.frame_id
                                # 自动跟随会逐帧移动局部 settings；同步回状态副本后，
                                # WebUI 和下一次运行中更新都能看到真正的当前边界。
                                self._settings["area_left"] = sample.area_left
                                self._settings["area_right"] = sample.area_right
                                self._settings["area2_left"] = sample.area2_left
                                self._settings["area2_right"] = sample.area2_right
                                self._error = None

                            # CSV 降采样使用帧自身的实验时间。即使一次补回多帧，
                            # 也会按原采集时刻写入，而不是挤在补取发生的电脑时间上。
                            if next_record_timestamp is None:
                                next_record_timestamp = sample.timestamp
                            if (
                                sample.timestamp + 1e-9 >= next_record_timestamp
                                and sample.frame_id > last_recorded_frame_id
                            ):
                                row = self._build_csv_row(samples, settings)
                                area_ema = _update_ema(
                                    area_ema, row.get("area_mean"), ema_alpha
                                )
                                area2_ema = _update_ema(
                                    area2_ema, row.get("area2_mean"), ema_alpha
                                )
                                area_sum_ema = _update_ema(
                                    area_sum_ema, row.get("area_sum_mean"), ema_alpha
                                )
                                peak_height_ema = _update_ema(
                                    peak_height_ema,
                                    row.get("peak_height_mean"),
                                    ema_alpha,
                                )
                                peak2_height_ema = _update_ema(
                                    peak2_height_ema,
                                    row.get("peak2_height_mean"),
                                    ema_alpha,
                                )
                                top_ema = _update_ema(
                                    top_ema,
                                    row.get("top_mean"),
                                    ema_alpha,
                                )
                                top2_ema = _update_ema(
                                    top2_ema,
                                    row.get("top2_mean"),
                                    ema_alpha,
                                )
                                row["area_ema_alpha"] = ema_alpha
                                row["area_ema"] = area_ema
                                row["area2_ema_alpha"] = ema_alpha
                                row["area2_ema"] = area2_ema
                                row["area_sum_ema_alpha"] = ema_alpha
                                row["area_sum_ema"] = area_sum_ema
                                row["peak_height_ema_alpha"] = ema_alpha
                                row["peak_height_ema"] = peak_height_ema
                                row["peak2_height_ema_alpha"] = ema_alpha
                                row["peak2_height_ema"] = peak2_height_ema
                                row["top_ema_alpha"] = ema_alpha
                                row["top_ema"] = top_ema
                                row["top2_ema_alpha"] = ema_alpha
                                row["top2_ema"] = top2_ema
                                writer.writerow(row)
                                file.flush()
                                last_recorded_frame_id = sample.frame_id
                                with self._lock:
                                    self._records_written += 1
                                    self._latest_stats = dict(row)
                                    self._recent_stats.append(dict(row))
                                while next_record_timestamp <= sample.timestamp + 1e-9:
                                    next_record_timestamp += record_period

                        if voltage_recorder is not None:
                            voltage_error = voltage_recorder.status().get("error")
                            if voltage_error:
                                self._set_error(f"窗口电压写盘失败：{voltage_error}")
                                break

                        # 历史接口还有数据时立即继续补取，不额外 sleep。
                        if has_more:
                            continue

                    except FrameHistoryError as exc:
                        self._set_error(str(exc))
                        break
                    except Exception as exc:
                        # 网络短暂失败沿用原行为：显示错误但继续尝试，成功读取后会清除。
                        self._set_error(str(exc))

                    time.sleep(float(settings["poll_interval"]))

        finally:
            if voltage_recorder is not None:
                recorder_status = voltage_recorder.stop()
                if recorder_status.get("error"):
                    self._set_error(f"窗口电压写盘失败：{recorder_status['error']}")
            with self._lock:
                if self._thread is threading.current_thread():
                    self._running = False
                    self._finished_at = time.time()
                    if self._stop_reason is None:
                        self._stop_reason = "error" if self._error else "completed"

    def _get_stream_status(self, settings: dict[str, Any]) -> dict[str, Any]:
        """读取当前面积慢漂记录所依赖的数据流状态。"""

        if settings.get("stream_source") == "unified_stream":
            return self._daq.get_unified_ai_stream_status()
        return self._daq.get_ai_frame_stream_status()

    def _get_stream_frames(
        self,
        settings: dict[str, Any],
        last_seen_frame_id: int,
        status: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        """返回尚未处理的帧，以及是否应立即继续读取下一批。"""

        channels = [str(channel) for channel in (settings.get("channels") or [])]
        if settings.get("stream_source") == "unified_stream":
            if not channels:
                channels = [
                    str(channel)
                    for channel in ((status.get("settings") or {}).get("channels") or [])
                ]
            batch = self._daq.get_unified_ai_frame_batch(
                after_frame_id=last_seen_frame_id,
                channels=channels,
                max_frames=100,
            )
            if batch.get("history_overrun"):
                missing = int(batch.get("missing_before_first", 0))
                oldest = int(batch.get("oldest_available_frame_id", 0))
                raise FrameHistoryError(
                    f"统一 AI 历史缓冲区已经覆盖 {missing} 帧，"
                    f"当前最老可用 frame_id={oldest}。"
                    "记录已停止，避免生成带有隐藏缺口的数据。"
                )
            return list(batch.get("frames") or []), bool(batch.get("has_more"))

        # 旧 frame_stream 没有历史缓冲，只维持原来的“有新帧就取最新帧”行为。
        if int(status.get("frame_id", 0)) <= last_seen_frame_id:
            return [], False
        frame = self._daq.get_ai_frame_stream_latest()
        if channels:
            frame = _filter_frame_channels(frame, channels)
        return [frame], False

    def _measure_frame(self, frame: dict[str, Any], settings: dict[str, Any]) -> AreaTrendSample:
        """从一帧波形里计算两个动态窗口的面积、峰高和 Top 值。"""

        values = np.asarray(frame["values"], dtype=float)
        channel_index = int(settings["analysis_channel_index"])
        if values.ndim != 2:
            raise RuntimeError("latest frame values must be a 2D array")
        if channel_index < 0 or channel_index >= values.shape[0]:
            raise ValueError("analysis_channel_index is out of range")

        signal = values[channel_index]
        if settings.get("auto_track_enabled"):
            measurement = track_and_measure_manual_area(
                signal,
                left_index=int(settings["area_left"]),
                right_index=int(settings["area_right"]),
                smooth_window=int(settings.get("auto_track_smooth_window", 9)),
                max_shift_per_frame=int(settings.get("auto_track_max_shift", 5)),
            )
            # 记录线程内部保存最新窗口位置，下一帧从这个位置继续跟随。
            settings["area_left"] = measurement.left_index
            settings["area_right"] = measurement.right_index
        else:
            measurement = measure_manual_area(
                signal,
                left_index=int(settings["area_left"]),
                right_index=int(settings["area_right"]),
            )
        measurement2 = None
        if settings.get("area2_left") is not None and settings.get("area2_right") is not None:
            # 第二个面积窗口和第一个完全独立，仍然使用同一帧、同一分析通道。
            if settings.get("auto_track_enabled"):
                measurement2 = track_and_measure_manual_area(
                    signal,
                    left_index=int(settings["area2_left"]),
                    right_index=int(settings["area2_right"]),
                    smooth_window=int(settings.get("auto_track_smooth_window", 9)),
                    max_shift_per_frame=int(settings.get("auto_track_max_shift", 5)),
                )
                settings["area2_left"] = measurement2.left_index
                settings["area2_right"] = measurement2.right_index
            else:
                measurement2 = measure_manual_area(
                    signal,
                    left_index=int(settings["area2_left"]),
                    right_index=int(settings["area2_right"]),
                )

        # 跟随算法只负责决定窗口位置；峰高始终在移动完成后的原始数据窗口内寻找。
        # 这样面积和峰高严格使用相同的左右边界，WebUI 和 CSV 的统计口径也一致。
        peak_height, peak_height_index = _measure_window_peak_height(
            signal,
            measurement.left_index,
            measurement.right_index,
        )
        top_measurement = measure_top_fraction_mean(
            signal,
            measurement.left_index,
            measurement.right_index,
            percentage=float(settings.get("top_percent", 10.0)),
        )
        peak2_height = None
        peak2_height_index = None
        top2_measurement = None
        if measurement2 is not None:
            peak2_height, peak2_height_index = _measure_window_peak_height(
                signal,
                measurement2.left_index,
                measurement2.right_index,
            )
            top2_measurement = measure_top_fraction_mean(
                signal,
                measurement2.left_index,
                measurement2.right_index,
                percentage=float(settings.get("top_percent", 10.0)),
            )

        return AreaTrendSample(
            frame_id=int(frame.get("frame_id", 0)),
            timestamp=float(frame.get("finished_at", time.time())),
            area=float(measurement.value),
            peak_height=peak_height,
            top_value=float(top_measurement.value),
            top_point_count=int(top_measurement.selected_point_count),
            area2=None if measurement2 is None else float(measurement2.value),
            peak2_height=peak2_height,
            top2_value=None if top2_measurement is None else float(top2_measurement.value),
            top2_point_count=(
                None
                if top2_measurement is None
                else int(top2_measurement.selected_point_count)
            ),
            area_left=int(measurement.left_index),
            area_right=int(measurement.right_index),
            area_peak_index=measurement.peak_index,
            peak_height_index=peak_height_index,
            area2_left=None if measurement2 is None else int(measurement2.left_index),
            area2_right=None if measurement2 is None else int(measurement2.right_index),
            area2_peak_index=None if measurement2 is None else measurement2.peak_index,
            peak2_height_index=peak2_height_index,
        )

    def _build_csv_row(
        self,
        samples: deque[AreaTrendSample],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """根据最近 N 帧面积、峰高和 Top 值构造一行 CSV。"""

        window_frames = int(settings["window_frames"])
        recent = list(samples)[-window_frames:]
        areas = np.asarray([sample.area for sample in recent], dtype=float)
        area2_values = [sample.area2 for sample in recent if sample.area2 is not None]
        area_sums = np.asarray(
            [sample.area + (0.0 if sample.area2 is None else sample.area2) for sample in recent],
            dtype=float,
        )
        peak_heights = np.asarray([sample.peak_height for sample in recent], dtype=float)
        peak2_heights = [
            sample.peak2_height for sample in recent if sample.peak2_height is not None
        ]
        top_values = np.asarray([sample.top_value for sample in recent], dtype=float)
        top2_values = [sample.top2_value for sample in recent if sample.top2_value is not None]
        frame_ids = [sample.frame_id for sample in recent]

        area_stats = _area_stats(areas)
        area2_stats = _area_stats(np.asarray(area2_values, dtype=float)) if area2_values else {}
        area_sum_stats = _area_stats(area_sums)
        peak_height_stats = _area_stats(peak_heights)
        peak2_height_stats = (
            _area_stats(np.asarray(peak2_heights, dtype=float)) if peak2_heights else {}
        )
        top_stats = _area_stats(top_values)
        top2_stats = _area_stats(np.asarray(top2_values, dtype=float)) if top2_values else {}

        latest = recent[-1]
        latest_area_sum = latest.area + (0.0 if latest.area2 is None else latest.area2)
        return {
            "iso_time": datetime.fromtimestamp(latest.timestamp).isoformat(timespec="seconds"),
            "unix_time": latest.timestamp,
            "frame_id": latest.frame_id,
            "frame_id_start": frame_ids[0],
            "frame_id_end": frame_ids[-1],
            "session_id": str(settings.get("session_id", "")),
            "analysis_channel_index": int(settings["analysis_channel_index"]),
            "window_revision": int(settings.get("window_revision", 1)),
            "auto_track_enabled": bool(settings.get("auto_track_enabled", False)),
            "auto_track_smooth_window": int(settings.get("auto_track_smooth_window", 9)),
            "auto_track_max_shift": int(settings.get("auto_track_max_shift", 5)),
            "area_left": latest.area_left,
            "area_right": latest.area_right,
            "area_peak_index": latest.area_peak_index,
            "peak_height_index": latest.peak_height_index,
            "area2_left": latest.area2_left,
            "area2_right": latest.area2_right,
            "area2_peak_index": latest.area2_peak_index,
            "peak2_height_index": latest.peak2_height_index,
            "window_frames": window_frames,
            "sample_count": int(areas.size),
            "area_current": float(areas[-1]),
            "area_mean": area_stats["mean"],
            "area_std": area_stats["std"],
            "area_rel_std_percent": area_stats["rel_std_percent"],
            "area_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "area_ema": None,
            "area2_current": latest.area2,
            "area2_mean": area2_stats.get("mean"),
            "area2_std": area2_stats.get("std"),
            "area2_rel_std_percent": area2_stats.get("rel_std_percent"),
            "area2_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "area2_ema": None,
            "area_sum_current": latest_area_sum,
            "area_sum_mean": area_sum_stats["mean"],
            "area_sum_std": area_sum_stats["std"],
            "area_sum_rel_std_percent": area_sum_stats["rel_std_percent"],
            "area_sum_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "area_sum_ema": None,
            "peak_height_current": latest.peak_height,
            "peak_height_mean": peak_height_stats["mean"],
            "peak_height_std": peak_height_stats["std"],
            "peak_height_rel_std_percent": peak_height_stats["rel_std_percent"],
            "peak_height_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "peak_height_ema": None,
            "peak2_height_current": latest.peak2_height,
            "peak2_height_mean": peak2_height_stats.get("mean"),
            "peak2_height_std": peak2_height_stats.get("std"),
            "peak2_height_rel_std_percent": peak2_height_stats.get("rel_std_percent"),
            "peak2_height_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "peak2_height_ema": None,
            "top_percent": float(settings.get("top_percent", 10.0)),
            "top_point_count": latest.top_point_count,
            "top_current": latest.top_value,
            "top_mean": top_stats["mean"],
            "top_std": top_stats["std"],
            "top_rel_std_percent": top_stats["rel_std_percent"],
            "top_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "top_ema": None,
            "top2_point_count": latest.top2_point_count,
            "top2_current": latest.top2_value,
            "top2_mean": top2_stats.get("mean"),
            "top2_std": top2_stats.get("std"),
            "top2_rel_std_percent": top2_stats.get("rel_std_percent"),
            "top2_ema_alpha": float(settings.get("ema_alpha", 0.02)),
            "top2_ema": None,
            "shot2shot_last_delta": area_stats["last_delta"],
            "shot2shot_last_delta_rel_percent": area_stats["last_delta_rel_percent"],
            "shot2shot_std": area_stats["shot2shot_std"],
            "shot2shot_rel_std_percent": area_stats["shot2shot_rel_std_percent"],
            "area_sum_shot2shot_last_delta": area_sum_stats["last_delta"],
            "area_sum_shot2shot_last_delta_rel_percent": area_sum_stats["last_delta_rel_percent"],
            "area_sum_shot2shot_std": area_sum_stats["shot2shot_std"],
            "area_sum_shot2shot_rel_std_percent": area_sum_stats["shot2shot_rel_std_percent"],
            "area2_shot2shot_last_delta": area2_stats.get("last_delta"),
            "area2_shot2shot_last_delta_rel_percent": area2_stats.get("last_delta_rel_percent"),
            "area2_shot2shot_std": area2_stats.get("shot2shot_std"),
            "area2_shot2shot_rel_std_percent": area2_stats.get("shot2shot_rel_std_percent"),
            "peak_height_shot2shot_last_delta": peak_height_stats["last_delta"],
            "peak_height_shot2shot_last_delta_rel_percent": peak_height_stats[
                "last_delta_rel_percent"
            ],
            "peak_height_shot2shot_std": peak_height_stats["shot2shot_std"],
            "peak_height_shot2shot_rel_std_percent": peak_height_stats[
                "shot2shot_rel_std_percent"
            ],
            "peak2_height_shot2shot_last_delta": peak2_height_stats.get("last_delta"),
            "peak2_height_shot2shot_last_delta_rel_percent": peak2_height_stats.get(
                "last_delta_rel_percent"
            ),
            "peak2_height_shot2shot_std": peak2_height_stats.get("shot2shot_std"),
            "peak2_height_shot2shot_rel_std_percent": peak2_height_stats.get(
                "shot2shot_rel_std_percent"
            ),
            "record_hz": float(settings["record_hz"]),
        }

    def _set_error(self, message: str) -> None:
        """记录线程错误，但不立刻退出，方便等待采集流恢复。"""

        with self._lock:
            self._error = message


def _validate_window_pair(left: int, right: int, name: str) -> tuple[int, int]:
    """校验一个闭区间，并保留用户明确填写的左右方向。"""

    normalized = (int(left), int(right))
    if normalized[0] < 0 or normalized[1] < 0:
        raise ValueError(f"窗口 {name} 的边界必须 >= 0")
    if normalized[0] > normalized[1]:
        raise ValueError(f"窗口 {name} 的左边界必须小于或等于右边界")
    return normalized


def _validate_optional_window_pair(
    left: int | None,
    right: int | None,
    name: str,
) -> tuple[int, int] | None:
    """校验可选窗口；左右边界必须同时存在或同时为空。"""

    if (left is None) != (right is None):
        raise ValueError(f"窗口 {name} 的左右边界必须同时填写")
    if left is None or right is None:
        return None
    return _validate_window_pair(left, right, name)


def _validate_window_in_frame(
    window: tuple[int, int],
    samples_per_frame: int,
    name: str,
) -> None:
    """防止运行中把窗口设置到当前波形点数之外。"""

    if window[1] >= samples_per_frame:
        raise ValueError(
            f"窗口 {name} 的右边界必须小于每帧点数 {samples_per_frame}"
        )


def _current_optional_window(
    settings: dict[str, Any],
    latest: dict[str, Any],
) -> tuple[int, int] | None:
    """优先从最新统计读取自动跟随后真实的 B 窗口。"""

    left = latest.get("area2_left", settings.get("area2_left"))
    right = latest.get("area2_right", settings.get("area2_right"))
    if left is None or right is None:
        return None
    return int(left), int(right)


def _window_width(window: tuple[int, int]) -> int:
    """返回闭区间包含的采样点数。"""

    return window[1] - window[0] + 1


def _relative_percent(std_or_delta: float, mean: float) -> float | None:
    """把绝对标准差或差值转换成相对百分比。"""

    if abs(mean) < 1e-30:
        return None
    return float(std_or_delta / abs(mean) * 100.0)


def _measure_window_peak_height(
    signal: np.ndarray,
    left_index: int,
    right_index: int,
) -> tuple[float, int]:
    """返回指定闭区间内原始波形的最高值及其全局索引。"""

    data = np.asarray(signal, dtype=float)
    left = max(0, min(int(left_index), data.size - 1))
    right = max(0, min(int(right_index), data.size - 1))
    if right < left:
        left, right = right, left
    window = data[left : right + 1]
    if window.size == 0:
        raise ValueError("peak height window must not be empty")
    local_index = int(np.argmax(window))
    return float(window[local_index]), left + local_index


def _build_window_voltage_record(
    frame: dict[str, Any],
    sample: AreaTrendSample,
    analysis_channel_index: int,
    mode: str,
    record_full_frame: bool = False,
) -> dict[str, Any]:
    """从已完成统计的同一帧中切出 A/B 原始电压窗口。"""

    values = np.asarray(frame["values"], dtype=float)
    if values.ndim != 2 or analysis_channel_index < 0 or analysis_channel_index >= values.shape[0]:
        raise RuntimeError("窗口电压记录找不到分析通道")
    signal = values[analysis_channel_index]
    record: dict[str, Any] = {
        "frame_id": sample.frame_id,
        "finished_at": sample.timestamp,
    }
    for field in ("started_at", "segment_id", "segment_frame_id"):
        if frame.get(field) is not None:
            record[field] = frame[field]
    if record_full_frame:
        # 必须在放入后台写盘队列前复制。这样后续采集覆盖内存时，磁盘数据仍然
        # 对应当前 frame_id，而不会被下一帧悄悄改写。
        record["full_values"] = np.asarray(signal, dtype=np.float32).copy()
    if mode in ("a", "both"):
        record.update(_window_record_fields("a", signal, sample, second=False))
    if mode in ("b", "both"):
        record.update(_window_record_fields("b", signal, sample, second=True))
    return record


def _window_record_fields(
    prefix: str,
    signal: np.ndarray,
    sample: AreaTrendSample,
    second: bool,
) -> dict[str, Any]:
    """构造一个窗口的逐帧值和位置元数据。"""

    left = sample.area2_left if second else sample.area_left
    right = sample.area2_right if second else sample.area_right
    if left is None or right is None:
        raise RuntimeError(f"窗口 {prefix.upper()} 没有有效边界")
    return {
        f"{prefix}_values": np.asarray(signal[left : right + 1], dtype=np.float32).copy(),
        f"{prefix}_left": int(left),
        f"{prefix}_right": int(right),
        f"{prefix}_track_peak_index": (
            sample.area2_peak_index if second else sample.area_peak_index
        ),
        f"{prefix}_peak_height_index": (
            sample.peak2_height_index if second else sample.peak_height_index
        ),
    }


def _update_ema(previous: float | None, value: Any, alpha: float) -> float | None:
    """更新指数滑动平均。

    alpha=0 表示关闭 EMA，此时返回 None。
    value=None 常见于没有设置 B 窗口的情况，此时不更新。
    """

    if alpha <= 0 or value is None:
        return previous if alpha > 0 else None
    raw_value = float(value)
    if previous is None:
        return raw_value
    return float(alpha * raw_value + (1.0 - alpha) * previous)


def _area_stats(values: np.ndarray) -> dict[str, Any]:
    """计算一个面积序列的滑动统计量。

    这里被 A/B 两个面积窗口共用，避免两套公式以后不小心改得不一致。
    """

    if values.size < 1:
        return {
            "mean": None,
            "std": None,
            "rel_std_percent": None,
            "last_delta": None,
            "last_delta_rel_percent": None,
            "shot2shot_std": None,
            "shot2shot_rel_std_percent": None,
        }

    mean = float(np.mean(values))
    std = float(np.std(values))
    rel_std_percent = _relative_percent(std, mean)

    if values.size >= 2:
        diffs = np.diff(values)
        shot2shot_std = float(np.std(diffs))
        shot2shot_rel_std_percent = _relative_percent(shot2shot_std, mean)
        last_delta = float(values[-1] - values[-2])
        last_delta_rel_percent = _relative_percent(last_delta, mean)
    else:
        shot2shot_std = 0.0
        shot2shot_rel_std_percent = None
        last_delta = 0.0
        last_delta_rel_percent = None

    return {
        "mean": mean,
        "std": std,
        "rel_std_percent": rel_std_percent,
        "last_delta": last_delta,
        "last_delta_rel_percent": last_delta_rel_percent,
        "shot2shot_std": shot2shot_std,
        "shot2shot_rel_std_percent": shot2shot_rel_std_percent,
    }


def _channel_short_name(channel: str) -> str:
    """把 Dev2/ai1、ai1 这两种写法统一成 ai1。"""

    return str(channel).strip().split("/")[-1].lower()


def _filter_frame_channels(frame: dict[str, Any], requested_channels: list[str]) -> dict[str, Any]:
    """把统一流的大帧裁成双峰查看器当前选择的通道。"""

    frame_channels = [str(channel) for channel in frame.get("channels", [])]
    frame_values = frame.get("values", [])
    if len(frame_channels) != len(frame_values):
        raise RuntimeError("latest frame channels and values length do not match")

    selected_channels: list[str] = []
    selected_values: list[Any] = []
    used_indices: set[int] = set()
    for requested in requested_channels:
        requested_short = _channel_short_name(requested)
        match_index = None
        for index, channel in enumerate(frame_channels):
            if index in used_indices:
                continue
            if _channel_short_name(channel) == requested_short:
                match_index = index
                break
        if match_index is None:
            raise RuntimeError(
                f"latest frame does not contain requested channel {requested}; "
                f"available channels are {frame_channels}"
            )
        used_indices.add(match_index)
        selected_channels.append(frame_channels[match_index])
        selected_values.append(frame_values[match_index])

    filtered = dict(frame)
    filtered["source_channels"] = frame_channels
    filtered["channels"] = selected_channels
    filtered["values"] = selected_values
    filtered["channel_count"] = len(selected_channels)
    return filtered


def _csv_fieldnames() -> list[str]:
    """CSV 列名集中放在这里，避免写入行时顺序混乱。"""

    return [
        "iso_time",
        "unix_time",
        "frame_id",
        "frame_id_start",
        "frame_id_end",
        "session_id",
        "analysis_channel_index",
        "window_revision",
        "auto_track_enabled",
        "auto_track_smooth_window",
        "auto_track_max_shift",
        "area_left",
        "area_right",
        "area_peak_index",
        "peak_height_index",
        "area2_left",
        "area2_right",
        "area2_peak_index",
        "peak2_height_index",
        "window_frames",
        "sample_count",
        "area_current",
        "area_mean",
        "area_std",
        "area_rel_std_percent",
        "area_ema_alpha",
        "area_ema",
        "area2_current",
        "area2_mean",
        "area2_std",
        "area2_rel_std_percent",
        "area2_ema_alpha",
        "area2_ema",
        "area_sum_current",
        "area_sum_mean",
        "area_sum_std",
        "area_sum_rel_std_percent",
        "area_sum_ema_alpha",
        "area_sum_ema",
        "peak_height_current",
        "peak_height_mean",
        "peak_height_std",
        "peak_height_rel_std_percent",
        "peak_height_ema_alpha",
        "peak_height_ema",
        "peak2_height_current",
        "peak2_height_mean",
        "peak2_height_std",
        "peak2_height_rel_std_percent",
        "peak2_height_ema_alpha",
        "peak2_height_ema",
        "top_percent",
        "top_point_count",
        "top_current",
        "top_mean",
        "top_std",
        "top_rel_std_percent",
        "top_ema_alpha",
        "top_ema",
        "top2_point_count",
        "top2_current",
        "top2_mean",
        "top2_std",
        "top2_rel_std_percent",
        "top2_ema_alpha",
        "top2_ema",
        "shot2shot_last_delta",
        "shot2shot_last_delta_rel_percent",
        "shot2shot_std",
        "shot2shot_rel_std_percent",
        "area_sum_shot2shot_last_delta",
        "area_sum_shot2shot_last_delta_rel_percent",
        "area_sum_shot2shot_std",
        "area_sum_shot2shot_rel_std_percent",
        "area2_shot2shot_last_delta",
        "area2_shot2shot_last_delta_rel_percent",
        "area2_shot2shot_std",
        "area2_shot2shot_rel_std_percent",
        "peak_height_shot2shot_last_delta",
        "peak_height_shot2shot_last_delta_rel_percent",
        "peak_height_shot2shot_std",
        "peak_height_shot2shot_rel_std_percent",
        "peak2_height_shot2shot_last_delta",
        "peak2_height_shot2shot_last_delta_rel_percent",
        "peak2_height_shot2shot_std",
        "peak2_height_shot2shot_rel_std_percent",
        "record_hz",
    ]
