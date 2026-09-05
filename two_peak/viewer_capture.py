"""双峰波形查看器的采集、测峰和样本保存逻辑。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from two_peak.signal import locate_and_measure_two_peaks
from two_peak.signal import measure_manual_area
from two_peak.viewer_state import ViewerState


def parse_channels(value: Any) -> list[str]:
    """把 WebUI 传来的通道参数整理成列表。

    支持两种写法：
        ["ai0", "ai1"]
        "ai0, ai1"
    """

    if isinstance(value, list):
        channels = [str(item).strip() for item in value]
    elif isinstance(value, str):
        channels = [item.strip() for item in value.split(",")]
    else:
        raise ValueError("channels must be a list or comma-separated string")

    channels = [channel for channel in channels if channel]
    if not channels:
        raise ValueError("channels must not be empty")
    return channels


def bool_value(value: Any) -> bool:
    """把 WebUI/JSON 里的布尔值安全转换成 Python bool。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", ""):
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def stream_source_from_body(body: dict[str, Any] | None) -> str:
    """读取前端选择的连续采集来源。

    frame_stream:
        旧模式，双峰查看器独占启动一个固定点数分帧 AI 采集。
    unified_stream:
        新模式，双峰查看器读取统一 AI 流；统一流可以同时被功率慢漂等模块消费。
    """

    source = str((body or {}).get("stream_source", "frame_stream"))
    if source not in ("frame_stream", "unified_stream"):
        raise ValueError("stream_source must be frame_stream or unified_stream")
    return source


def _status_with_source(status: dict[str, Any], source: str) -> dict[str, Any]:
    """给底层状态补一个来源标记，方便前端显示和调试。"""

    result = dict(status)
    result["stream_source"] = source
    settings = result.get("settings") or {}
    if "channels" not in result and isinstance(settings, dict):
        result["channels"] = settings.get("channels")
    return result


def _channel_short_name(channel: str) -> str:
    """把 Dev2/ai1、ai1 这两种写法统一成 ai1，方便比较。"""

    return str(channel).strip().split("/")[-1].lower()


def _filter_frame_channels(frame: dict[str, Any], requested_channels: list[str]) -> dict[str, Any]:
    """只保留查看器当前选择的通道。

    unified stream 可能全局采了 ai0/ai1/ai2，但双峰查看器当前只想看 ai1。
    如果不在后端过滤，前端虽然可以少画几条线，但测峰和保存样本仍会拿到完整帧，
    容易把 analysis_channel_index=0 错当成 ai0。这里直接把帧裁成查看器需要的通道。
    """

    if not requested_channels:
        return frame

    channels = [str(channel) for channel in frame.get("channels", [])]
    values = frame.get("values", [])
    if len(channels) != len(values):
        raise RuntimeError("latest frame channels and values length do not match")

    filtered_channels: list[str] = []
    filtered_values: list[Any] = []
    used_indices: set[int] = set()
    for requested in requested_channels:
        requested_key = _channel_short_name(requested)
        match_index = None
        for index, channel in enumerate(channels):
            if index in used_indices:
                continue
            if str(channel) == requested or _channel_short_name(channel) == requested_key:
                match_index = index
                break
        if match_index is None:
            raise RuntimeError(
                f"latest frame does not contain requested channel {requested}; "
                f"available channels are {channels}"
            )
        used_indices.add(match_index)
        filtered_channels.append(channels[match_index])
        filtered_values.append(values[match_index])

    filtered = dict(frame)
    filtered["source_channels"] = channels
    filtered["channels"] = filtered_channels
    filtered["values"] = filtered_values
    filtered["channel_count"] = len(filtered_channels)
    return filtered


def _ensure_unified_has_channels(status: dict[str, Any], requested_channels: list[str]) -> None:
    """确认统一 AI 流包含双峰查看器想读取的通道。

    注意：这里只检查，不修改统一流配置。统一流真实采哪些通道，只能由统一 AI 控制台决定。
    """

    settings = status.get("settings") or {}
    available = settings.get("channels") or status.get("channels") or []
    if not available:
        sample_counts = status.get("sample_counts") or {}
        available = list(sample_counts)
    available_short = {_channel_short_name(channel) for channel in available}
    missing = [
        channel
        for channel in requested_channels
        if _channel_short_name(channel) not in available_short
    ]
    if missing:
        raise RuntimeError(
            f"统一 AI 流没有包含双峰查看器选择的通道 {missing}。"
            f"当前统一流通道为：{available}。"
            "请先在统一 AI 控制台中设置并启动包含这些通道的统一流。"
        )


def start_frame_stream(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """通过底层 API 启动固定点数分帧连续采集。"""

    source = stream_source_from_body(body)
    channels = parse_channels(body.get("channels", ["ai0", "ai1"]))

    if source == "unified_stream":
        status = state.daq.get_unified_ai_stream_status()
        if not status.get("running"):
            raise RuntimeError("统一 AI 流未运行。请先在统一 AI 控制台启动统一流。")
        _ensure_unified_has_channels(status, channels)
        return _status_with_source(status, "unified_stream")

    common_kwargs = {
        "channels": channels,
        "samples_per_frame": int(body.get("samples_per_frame", body.get("samples", 5000))),
        "rate": float(body.get("rate", 50_000.0)),
        "terminal_config": str(body.get("terminal_config", "DIFF")),
        "min_val": float(body.get("min_val", -5.0)),
        "max_val": float(body.get("max_val", 5.0)),
        "timeout": float(body.get("timeout", 10.0)),
        "trigger_enabled": bool_value(body.get("trigger_enabled", False)),
        "trigger_source": str(body.get("trigger_source", "PFI0")),
        "trigger_edge": str(body.get("trigger_edge", "RISING")),
        "resync_every_frames": int(body.get("resync_every_frames", 0)),
    }

    return _status_with_source(state.daq.start_ai_frame_stream(**common_kwargs), "frame_stream")


def stop_frame_stream(state: ViewerState, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """通过底层 API 停止固定点数分帧连续采集。"""

    source = stream_source_from_body(body)
    if source == "unified_stream":
        status = _status_with_source(state.daq.get_unified_ai_stream_status(), "unified_stream")
        status["running"] = False
        status["viewer_detached"] = True
        return status
    return _status_with_source(state.daq.stop_ai_frame_stream(), "frame_stream")


def get_frame_stream_status(
    state: ViewerState,
    requested_source: str = "unified_stream",
) -> dict[str, Any]:
    """查询查看器明确指定的数据源状态。

    不能根据“哪一种底层流碰巧正在运行”自动切换来源。统一流异常停止后，
    如果这里自动回退到旧 frame_stream，前端下一次点击开始就会误开一个旧 AI task，
    随后统一流会因为设备被占用而无法重启。
    """

    source = stream_source_from_body({"stream_source": requested_source})
    if source == "unified_stream":
        status = state.daq.get_unified_ai_stream_status()
    else:
        status = state.daq.get_ai_frame_stream_status()
    return _status_with_source(status, source)


def get_frame_stream_latest(state: ViewerState, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """通过底层 API 获取最新帧，并同步到查看器状态。"""

    requested_source = stream_source_from_body(body) if body and body.get("stream_source") else None
    status = get_frame_stream_status(state, requested_source or "unified_stream")
    if requested_source == "unified_stream":
        unified_status = state.daq.get_unified_ai_stream_status()
        if not unified_status.get("running") and not unified_status.get("has_frame"):
            raise RuntimeError("统一 AI 流未运行。请先在统一 AI 控制台启动统一流。")
        frame = state.daq.get_unified_ai_stream_latest_frame()
        frame["captured_by"] = "usb6363_unified_stream"
    elif requested_source == "frame_stream":
        frame = state.daq.get_ai_frame_stream_latest()
        frame["captured_by"] = "usb6363_frame_stream"
    elif status.get("stream_source") == "unified_stream":
        frame = state.daq.get_unified_ai_stream_latest_frame()
        frame["captured_by"] = "usb6363_unified_stream"
    else:
        frame = state.daq.get_ai_frame_stream_latest()
        frame["captured_by"] = "usb6363_frame_stream"

    if body and body.get("channels") not in (None, ""):
        frame = _filter_frame_channels(frame, parse_channels(body.get("channels")))

    frame["viewer_received_at"] = time.time()
    state.latest_frame = frame
    state.latest_measurement = None
    return frame


def start_area_trend(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """启动手动面积慢漂记录。

    这个函数不直接采集波形，而是读取已经启动的后端连续采集流。
    这样可以避免为了“记录趋势”再开一个新的 DAQ 任务，减少和采集卡抢资源的风险。
    """

    area_left = body.get("area_left")
    area_right = body.get("area_right")
    area2_left = body.get("area2_left")
    area2_right = body.get("area2_right")
    if area_left in (None, "") or area_right in (None, ""):
        raise ValueError("area_left and area_right are required")
    if (area2_left in (None, "")) != (area2_right in (None, "")):
        raise ValueError("area2_left and area2_right must be set together")

    # 双峰慢漂现在属于统一 AI 流的消费者，不能再相信浏览器隐藏字段里的旧值。
    # 浏览器可能缓存很久以前的页面并提交 frame_stream；如果后端照单全收，就会创建
    # 一个看似运行、实际上永远读不到统一流数据的记录线程。
    channels = parse_channels(body.get("channels", ["ai0", "ai1"]))
    unified_status = state.daq.get_unified_ai_stream_status()
    if not unified_status.get("running"):
        detail = unified_status.get("error")
        message = "统一 AI 流未运行，请先在 8768 控制台启动统一采集"
        if detail:
            message += f"。底层错误：{detail}"
        raise RuntimeError(message)
    _ensure_unified_has_channels(unified_status, channels)

    return state.trend_logger.start(
        analysis_channel_index=int(body.get("analysis_channel_index", 0)),
        area_left=int(area_left),
        area_right=int(area_right),
        area2_left=None if area2_left in (None, "") else int(area2_left),
        area2_right=None if area2_right in (None, "") else int(area2_right),
        window_frames=int(body.get("window_frames", 200)),
        record_hz=float(body.get("record_hz", 1.0)),
        duration_minutes=float(body.get("duration_minutes", 0.0) or 0.0),
        ema_alpha=float(body.get("ema_alpha", 0.02)),
        top_percent=float(body.get("top_percent", 10.0)),
        auto_track_enabled=_as_bool(body.get("auto_track_enabled", False)),
        auto_track_smooth_window=int(body.get("auto_track_smooth_window", 9)),
        auto_track_max_shift=int(body.get("auto_track_max_shift", 5)),
        poll_interval=float(body.get("poll_interval", 0.05)),
        stream_source="unified_stream",
        channels=channels,
        window_voltage_mode=str(body.get("window_voltage_mode", "none")),
        record_full_frame=_as_bool(body.get("record_full_frame", False)),
        window_voltage_output_dir=Path(
            str(body.get("window_voltage_output_dir", "data/two_peak_window_voltage"))
        ),
        session_id=(None if body.get("session_id") in (None, "") else str(body["session_id"])),
        trigger_unix_time=(
            None
            if body.get("trigger_unix_time") in (None, "")
            else float(body["trigger_unix_time"])
        ),
        start_after_frame_id=int(body.get("start_after_frame_id", 0)),
    )


def stop_area_trend(state: ViewerState) -> dict[str, Any]:
    """停止手动面积慢漂记录。"""

    return state.trend_logger.stop()


def update_area_trend_windows(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """把 WebUI 新填写的 A/B 边界交给正在运行的慢漂记录器。"""

    area_left = body.get("area_left")
    area_right = body.get("area_right")
    area2_left = body.get("area2_left")
    area2_right = body.get("area2_right")
    if area_left in (None, "") or area_right in (None, ""):
        raise ValueError("area_left and area_right are required")
    if (area2_left in (None, "")) != (area2_right in (None, "")):
        raise ValueError("area2_left and area2_right must be set together")
    return state.trend_logger.update_area_windows(
        area_left=int(area_left),
        area_right=int(area_right),
        area2_left=None if area2_left in (None, "") else int(area2_left),
        area2_right=None if area2_right in (None, "") else int(area2_right),
    )


def get_area_trend_status(state: ViewerState) -> dict[str, Any]:
    """查询手动面积慢漂记录状态。"""

    return state.trend_logger.status()


def measure_latest_frame(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """测量最近一帧里的 P1/P2。

    当前版本默认在第一路 AI 波形上测峰，因为旧双峰程序里 AI0 是 FP 透射信号。
    """

    frame = state.latest_frame
    if frame is None:
        raise RuntimeError("No frame has been captured yet")

    values = np.asarray(frame["values"], dtype=float)
    if values.ndim != 2 or values.shape[0] < 1:
        raise RuntimeError("latest frame has no AI waveform")

    peak_indices = body.get("peak_indices")
    if not isinstance(peak_indices, list) or len(peak_indices) != 2:
        raise ValueError("peak_indices must be a two-item list")

    smooth_window = int(body.get("smooth_window", 25))
    search_window_half = int(body.get("search_window_half", 20))
    measure_half = int(body.get("measure_half", 5))
    peak_mode = str(body.get("peak_mode", "height"))
    manual_area_left = body.get("manual_area_left")
    manual_area_right = body.get("manual_area_right")
    manual_area2_left = body.get("manual_area2_left")
    manual_area2_right = body.get("manual_area2_right")
    analysis_channel_index = int(body.get("analysis_channel_index", 0))
    if analysis_channel_index < 0 or analysis_channel_index >= values.shape[0]:
        raise ValueError("analysis_channel_index is out of range")

    analysis_signal = values[analysis_channel_index]
    _, measurements = locate_and_measure_two_peaks(
        ai0=analysis_signal,
        peak_indices=[int(peak_indices[0]), int(peak_indices[1])],
        smooth_window=smooth_window,
        search_window_half=search_window_half,
        measure_half=measure_half,
        mode=peak_mode,
    )

    manual_area = None
    manual_areas: list[dict[str, Any]] = []
    if manual_area_left not in (None, "") and manual_area_right not in (None, ""):
        # 手动面积是一个单独的、最朴素的监测量：
        # 只对原始波形直接求和，不使用上面的平滑波形。
        manual_area = asdict(
            measure_manual_area(
                analysis_signal,
                left_index=int(manual_area_left),
                right_index=int(manual_area_right),
            )
        )
        area_a = dict(manual_area)
        area_a["name"] = "A"
        manual_areas.append(area_a)

    if manual_area2_left not in (None, "") and manual_area2_right not in (None, ""):
        # 第二个面积窗口用于同时观察两个峰。
        # 这里依然只做原始点值求和，不扣本底、不滤波，便于和第一个窗口直接比较。
        area_b = asdict(
            measure_manual_area(
                analysis_signal,
                left_index=int(manual_area2_left),
                right_index=int(manual_area2_right),
            )
        )
        area_b["name"] = "B"
        manual_areas.append(area_b)

    return {
        "analysis_channel_index": analysis_channel_index,
        "analysis_channel": frame["channels"][analysis_channel_index],
        "peak_indices_input": [int(peak_indices[0]), int(peak_indices[1])],
        "smooth_window": smooth_window,
        "search_window_half": search_window_half,
        "measure_half": measure_half,
        "peak_mode": peak_mode,
        "measurements": [asdict(item) for item in measurements],
        "manual_area": manual_area,
        "manual_areas": manual_areas,
    }


def save_latest_frame(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """保存最近一帧原始数据和元数据。"""

    frame = state.latest_frame
    if frame is None:
        raise RuntimeError("No frame has been captured yet")

    sample_dir = state.sample_dir
    sample_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = str(body.get("label", "")).strip()
    safe_label = "".join(ch for ch in label if ch.isalnum() or ch in ("-", "_"))
    stem = f"{timestamp}_{safe_label}" if safe_label else timestamp

    npy_path = sample_dir / f"{stem}.npy"
    json_path = sample_dir / f"{stem}.json"

    values = np.asarray(frame["values"], dtype=np.float64)
    np.save(npy_path, values)

    metadata = {
        "saved_at": time.time(),
        "npy_file": str(npy_path.resolve()),
        "metadata_file": str(json_path.resolve()),
        "shape": list(values.shape),
        "frame": frame_summary(frame),
        "measurement": state.latest_measurement,
        "web_parameters": body.get("parameters", {}),
        "factory_defaults": state.settings.to_web_parameters(),
        "active_defaults": state.active_web_defaults(),
        "format": "numpy .npy, shape=(channel_count, samples_per_channel)",
    }
    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return metadata


def list_saved_frames(state: ViewerState, limit: int = 50) -> dict[str, Any]:
    """列出最近保存的样本元数据文件。

    这里只读取 .json 元数据，不会把巨大的 .npy 波形数组都加载进内存。
    """

    sample_dir = state.sample_dir
    if not sample_dir.exists():
        return {"samples": []}

    rows: list[dict[str, Any]] = []
    for json_path in sorted(sample_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(
            {
                "metadata_file": str(json_path.resolve()),
                "npy_file": metadata.get("npy_file"),
                "saved_at": metadata.get("saved_at"),
                "shape": metadata.get("shape"),
                "frame": metadata.get("frame"),
                "measurement": metadata.get("measurement"),
            }
        )
    return {"samples": rows}


def load_saved_frame(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """从保存过的 .json/.npy 文件中加载一帧波形。

    加载后，WebUI 可以像刚采集完一样继续缩放、选峰、测峰。
    """

    metadata_file = body.get("metadata_file")
    if not metadata_file:
        raise ValueError("metadata_file is required")

    sample_dir = state.sample_dir.resolve()
    json_path = _resolve_sample_file(sample_dir, Path(str(metadata_file)), ".json")
    metadata = json.loads(json_path.read_text(encoding="utf-8"))

    npy_value = metadata.get("npy_file") or json_path.with_suffix(".npy")
    npy_path = _resolve_sample_file(sample_dir, Path(str(npy_value)), ".npy")
    values = np.load(npy_path)

    frame = dict(metadata.get("frame") or {})
    frame["values"] = values.tolist()
    frame.setdefault("channel_count", int(values.shape[0]) if values.ndim >= 1 else 1)
    frame.setdefault("samples_per_channel", int(values.shape[-1]) if values.ndim >= 1 else 0)
    frame["captured_by"] = "saved_sample"
    frame["loaded_from"] = str(json_path)
    frame["viewer_received_at"] = time.time()

    state.latest_frame = frame
    state.latest_measurement = metadata.get("measurement")
    return {
        "frame": frame,
        "measurement": state.latest_measurement,
        "metadata": metadata,
    }


def frame_summary(frame: dict[str, Any] | None) -> dict[str, Any] | None:
    """返回不包含巨大 values 的帧摘要。"""

    if frame is None:
        return None
    return {
        "device": frame.get("device"),
        "channels": frame.get("channels"),
        "channel_count": frame.get("channel_count"),
        "samples_per_channel": frame.get("samples_per_channel"),
        "rate_per_channel": frame.get("rate_per_channel"),
        "aggregate_rate": frame.get("aggregate_rate"),
        "terminal_config": frame.get("terminal_config"),
        "min_val": frame.get("min_val"),
        "max_val": frame.get("max_val"),
        "duration_seconds": frame.get("duration_seconds"),
        "started_at": frame.get("started_at"),
        "finished_at": frame.get("finished_at"),
        "trigger_enabled": frame.get("trigger_enabled"),
        "trigger_source": frame.get("trigger_source"),
        "trigger_edge": frame.get("trigger_edge"),
        "trigger_mode": frame.get("trigger_mode"),
        "resync_every_frames": frame.get("resync_every_frames"),
        "segment_id": frame.get("segment_id"),
        "segment_frame_id": frame.get("segment_frame_id"),
        "frame_duration_seconds": frame.get("frame_duration_seconds"),
        "frame_duration_ms": frame.get("frame_duration_ms"),
        "frame_rate_hz": frame.get("frame_rate_hz"),
    }


def _resolve_sample_file(sample_dir: Path, path: Path, suffix: str) -> Path:
    """把样本文件路径限制在 sample_dir 内，避免误读其他文件。"""

    candidate = path if path.is_absolute() else sample_dir / path
    resolved = candidate.resolve()
    if resolved.suffix.lower() != suffix:
        raise ValueError(f"sample file must end with {suffix}")
    try:
        resolved.relative_to(sample_dir)
    except ValueError as exc:
        raise ValueError("sample file must be inside the sample directory") from exc
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _as_bool(value: Any) -> bool:
    """把前端传来的 checkbox 值转换成 bool。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False
