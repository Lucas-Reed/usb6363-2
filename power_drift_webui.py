"""光电探测器功率慢漂 WebUI。

这个文件提供一个很小的本地网页，用来长期监测光电探测器输出。

运行方式：
    python power_drift_webui.py

然后打开：
    http://127.0.0.1:8767

设计边界：
- 本文件不 import nidaqmx。
- 本文件不直接控制 USB-6363。
- 真正读硬件仍然通过 Usb6363Client -> 8765 底层 API。
- 采集/统计逻辑复用 power_drift_monitor.py，避免命令行版和 WebUI 版各写一套。
"""

from __future__ import annotations

import argparse
import csv
import json
import threading
import time
from collections import deque
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from power_drift_monitor import DEFAULT_OUTPUT_DIR
from power_drift_monitor import PowerDriftMonitor
from power_drift_monitor import PowerDriftPoint
from power_drift_monitor import PowerDriftSettings
from usb6363_client import DEFAULT_BASE_URL


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
DEFAULTS_PATH = Path("data") / "power_drift_defaults.json"


@dataclass
class PowerDriftChannelSettings:
    """某一个 AI 通道的长漂显示、换算和脱锁判断参数。"""

    channel: str
    note: str
    power_per_volt: float
    zero_voltage: float
    unlock_enabled: bool
    unlock_min_v: float | None
    unlock_max_v: float | None


@dataclass
class PowerDriftWebSettings:
    """一次多通道长漂会话的公共参数和通道列表。"""

    channels: list[PowerDriftChannelSettings]
    data_source: str
    interval: float
    samples: int
    rate: float
    terminal_config: str
    min_val: float
    max_val: float
    timeout: float
    duration: float | None
    output_dir: Path
    api_base_url: str
    allow_busy_ai: bool


class PowerDriftWebState:
    """WebUI 的运行状态。

    浏览器会不断请求 /api/status。这个对象负责保存：
    - 当前是否正在记录；
    - CSV 文件路径；
    - 最近一些点，用于前端画趋势图；
    - 后台线程中的错误信息。
    """

    def __init__(self, defaults_path: Path = DEFAULTS_PATH) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._running = False
        self._error: str | None = None
        self._csv_path: Path | None = None
        self._metadata_path: Path | None = None
        self._settings: PowerDriftWebSettings | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._rows_written = 0
        self._cycles_written = 0
        self._latest_points: dict[str, dict[str, Any]] = {}
        self._recent_points_by_channel: dict[str, deque[dict[str, Any]]] = {}
        self._unlock_status: dict[str, dict[str, Any]] = {}
        # “预备同步触发”只保存在内存中，8767 重启后自然清除。
        self._armed_settings: PowerDriftWebSettings | None = None
        self._session_id: str | None = None
        self._trigger_unix_time: float | None = None
        self._start_after_frame_id = 0
        # 默认值属于实验运行配置，保存在 data/ 下而不是写死到 Python 代码中。
        self._defaults_path = defaults_path
        self._user_defaults = self._load_user_defaults()

    def start(
        self,
        settings: PowerDriftWebSettings,
        session_id: str | None = None,
        trigger_unix_time: float | None = None,
        start_after_frame_id: int = 0,
    ) -> dict[str, Any]:
        """启动后台功率慢漂记录线程。"""

        with self._lock:
            if self._running:
                raise RuntimeError("功率慢漂记录已经在运行")

            settings.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = settings.output_dir / f"power_drift_web_{timestamp}.csv"
            metadata_path = settings.output_dir / f"power_drift_web_{timestamp}.json"

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(
                    settings,
                    csv_path,
                    metadata_path,
                    stop_event,
                    session_id,
                    trigger_unix_time,
                    int(start_after_frame_id),
                ),
                daemon=True,
                name="power-drift-web-monitor",
            )

            self._thread = thread
            self._stop_event = stop_event
            self._running = True
            self._error = None
            self._csv_path = csv_path
            self._metadata_path = metadata_path
            self._settings = settings
            self._started_at = time.time()
            self._finished_at = None
            self._rows_written = 0
            self._cycles_written = 0
            self._latest_points = {}
            self._recent_points_by_channel = {
                item.channel: deque(maxlen=1000) for item in settings.channels
            }
            self._unlock_status = {
                item.channel: _initial_unlock_status(item) for item in settings.channels
            }
            self._session_id = session_id
            self._trigger_unix_time = trigger_unix_time
            self._start_after_frame_id = int(start_after_frame_id)
            thread.start()

        return self.status()

    def arm(self, settings: PowerDriftWebSettings) -> dict[str, Any]:
        """保存同步测试待启动参数，但此时不创建文件也不读取采集卡。"""

        if settings.data_source != "unified_stream":
            raise ValueError("同步测试只允许使用 unified_stream 数据来源")
        with self._lock:
            if self._running:
                raise RuntimeError("功率慢漂正在记录，不能进入同步预备状态")
            self._armed_settings = settings
        return self.status()

    def disarm(self) -> dict[str, Any]:
        """取消尚未启动的同步测试预备参数。"""

        with self._lock:
            if self._running:
                raise RuntimeError("功率慢漂正在记录，不能取消当前运行会话")
            self._armed_settings = None
        return self.status()

    def start_armed(
        self,
        session_id: str,
        trigger_unix_time: float,
        start_after_frame_id: int,
    ) -> dict[str, Any]:
        """消费已经预备的参数并启动同步测试功率记录。"""

        if not session_id:
            raise ValueError("session_id is required")
        if start_after_frame_id < 0:
            raise ValueError("start_after_frame_id must be >= 0")
        with self._lock:
            settings = self._armed_settings
        if settings is None:
            raise RuntimeError("功率慢漂尚未预备同步触发")

        self.start(
            settings,
            session_id=session_id,
            trigger_unix_time=trigger_unix_time,
            start_after_frame_id=start_after_frame_id,
        )
        with self._lock:
            self._armed_settings = None
        return self.status()

    def stop(self) -> dict[str, Any]:
        """停止后台记录。

        如果底层正在进行一次 read_ai，线程可能要等这次读取完成后才会停下。
        """

        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._running = False
                self._thread = None
                self._stop_event = None
                self._finished_at = time.time()

        return self.status()

    def status(self) -> dict[str, Any]:
        """返回前端需要显示的状态。"""

        with self._lock:
            primary_channel = (
                self._settings.channels[0].channel
                if self._settings is not None and self._settings.channels
                else None
            )
            latest_primary = (
                self._latest_points.get(primary_channel) if primary_channel else None
            )
            recent_primary = (
                list(self._recent_points_by_channel.get(primary_channel, ()))
                if primary_channel
                else []
            )
            return {
                "running": self._running,
                "error": self._error,
                "csv_file": str(self._csv_path.resolve()) if self._csv_path else None,
                "metadata_file": str(self._metadata_path.resolve()) if self._metadata_path else None,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "rows_written": self._rows_written,
                "cycles_written": self._cycles_written,
                "settings": _settings_for_json(self._settings) if self._settings else None,
                # 保留旧单通道字段，避免同步测试和已有分析脚本突然失效。
                "latest_point": latest_primary,
                "recent_points": recent_primary,
                "latest_points": dict(self._latest_points),
                "recent_points_by_channel": {
                    channel: list(points)
                    for channel, points in self._recent_points_by_channel.items()
                },
                "unlock_status": dict(self._unlock_status),
                "armed": self._armed_settings is not None,
                "armed_settings": (
                    _settings_for_json(self._armed_settings)
                    if self._armed_settings is not None
                    else None
                ),
                "session_id": self._session_id,
                "trigger_unix_time": self._trigger_unix_time,
                "start_after_frame_id": self._start_after_frame_id,
            }

    def latest_csv_path(self) -> Path:
        """返回当前或最近一次记录的 CSV 路径，用于下载。"""

        with self._lock:
            if self._csv_path is None:
                raise FileNotFoundError("还没有 CSV 文件")
            return self._csv_path

    def active_defaults(self) -> dict[str, Any]:
        """返回前端启动时应加载的默认值及其来源。"""

        with self._lock:
            parameters = _factory_default_parameters()
            parameters.update(self._user_defaults)
            parameters["defaults_source"] = "user" if self._user_defaults else "factory"
            parameters["defaults_file"] = str(self._defaults_path.resolve())
            return parameters

    def save_defaults(self, settings: PowerDriftWebSettings) -> dict[str, Any]:
        """把当前完整参数保存为用户默认值，备注也随通道一起保存。"""

        parameters = _settings_for_json(settings)
        if parameters is None:
            raise RuntimeError("没有可以保存的默认参数")
        with self._lock:
            self._defaults_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._defaults_path.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps(parameters, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 先完整写入临时文件再替换，避免程序意外退出时留下半截 JSON。
            temporary_path.replace(self._defaults_path)
            self._user_defaults = dict(parameters)
        return self.active_defaults()

    def reset_defaults(self) -> dict[str, Any]:
        """删除用户默认值，恢复代码中提供的程序默认值。"""

        with self._lock:
            self._user_defaults = {}
            if self._defaults_path.exists():
                self._defaults_path.unlink()
        return self.active_defaults()

    def _load_user_defaults(self) -> dict[str, Any]:
        """8767 启动时读取上次保存的默认参数。"""

        if not self._defaults_path.exists():
            return {}
        try:
            data = json.loads(self._defaults_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _worker(
        self,
        settings: PowerDriftWebSettings,
        csv_path: Path,
        metadata_path: Path,
        stop_event: threading.Event,
        session_id: str | None,
        trigger_unix_time: float | None,
        start_after_frame_id: int,
    ) -> None:
        """后台记录线程主体。"""

        monitors = [
            (item, PowerDriftMonitor(_monitor_settings(settings, item)))
            for item in settings.channels
        ]
        fieldnames = list(PowerDriftPoint.__dataclass_fields__) + [
            "channel_note",
            "unlock_enabled",
            "unlock_min_v",
            "unlock_max_v",
            "outside_unlock_range",
            "unlock_state",
            "unlock_event_unix_time",
            "unlock_event_iso_time",
        ]
        start_time = time.time()
        next_start_time = start_time
        row_index = 0
        unlock_events: dict[str, tuple[float, str] | None] = {
            item.channel: None for item in settings.channels
        }

        try:
            # 启动前检查硬件状态，避免和双峰连续采集等任务互相抢 AI。
            for _, monitor in monitors:
                monitor.check_hardware_idle()
            if start_after_frame_id > 0:
                _wait_for_unified_frame_after(
                    monitors[0][1],
                    start_after_frame_id,
                    stop_event,
                    timeout=settings.timeout,
                )

            metadata_path.write_text(
                json.dumps(
                    {
                        "started_at": datetime.fromtimestamp(start_time).isoformat(timespec="seconds"),
                        "csv_file": str(csv_path.resolve()),
                        "settings": _settings_for_json(settings),
                        "session_id": session_id,
                        "trigger_unix_time": trigger_unix_time,
                        "start_after_frame_id": start_after_frame_id,
                        "note": (
                            "CSV 为长格式：同一 index 的多行属于同一个记录周期；"
                            "脱锁范围判断使用该通道每周期的 mean_v。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with csv_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()

                while not stop_event.is_set():
                    now = time.time()
                    if settings.duration is not None and now - start_time >= settings.duration:
                        break

                    sleep_s = next_start_time - now
                    if sleep_s > 0:
                        stop_event.wait(timeout=sleep_s)
                        if stop_event.is_set():
                            break

                    row_index += 1
                    cycle_records: dict[str, dict[str, Any]] = {}
                    cycle_unlock_status: dict[str, dict[str, Any]] = {}
                    for channel_settings, monitor in monitors:
                        point = monitor.read_one_point(
                            row_index=row_index,
                            start_time=start_time,
                        )
                        point.session_id = session_id
                        record, unlock = _record_with_unlock_status(
                            point,
                            channel_settings,
                            unlock_events[channel_settings.channel],
                        )
                        if unlock_events[channel_settings.channel] is None and unlock.get(
                            "unlock_event_unix_time"
                        ) is not None:
                            unlock_events[channel_settings.channel] = (
                                float(unlock["unlock_event_unix_time"]),
                                str(unlock["unlock_event_iso_time"]),
                            )
                        writer.writerow(record)
                        cycle_records[channel_settings.channel] = record
                        cycle_unlock_status[channel_settings.channel] = unlock
                    file.flush()

                    with self._lock:
                        self._cycles_written += 1
                        self._rows_written += len(cycle_records)
                        self._latest_points.update(cycle_records)
                        for channel, record in cycle_records.items():
                            self._recent_points_by_channel[channel].append(record)
                        self._unlock_status.update(cycle_unlock_status)
                        self._error = None

                    next_start_time += settings.interval

        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._running = False
                    self._thread = None
                    self._stop_event = None
                    self._finished_at = time.time()


def make_handler(state: PowerDriftWebState):
    """创建 HTTP 请求处理类。"""

    class PowerDriftHandler(BaseHTTPRequestHandler):
        server_version = "PowerDriftWebUI/0.1"

        def do_GET(self) -> None:
            """处理页面、状态查询和 CSV 下载。"""

            try:
                parsed = urlparse(self.path)
                if parsed.path == "/" or parsed.path == "/index.html":
                    self._send_html(HTML_PAGE)
                elif parsed.path == "/api/status":
                    self._send_json(state.status())
                elif parsed.path == "/api/defaults":
                    self._send_json(state.active_defaults())
                elif parsed.path == "/api/download":
                    self._send_csv_file(state.latest_csv_path())
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            """处理开始和停止记录。"""

            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/start":
                    settings = _settings_from_body(self._read_json())
                    self._send_json(state.start(settings))
                elif parsed.path == "/api/defaults/save":
                    settings = _settings_from_body(self._read_json())
                    self._send_json(state.save_defaults(settings))
                elif parsed.path == "/api/defaults/reset":
                    self._send_json(state.reset_defaults())
                elif parsed.path == "/api/arm":
                    settings = _settings_from_body(self._read_json())
                    self._send_json(state.arm(settings))
                elif parsed.path == "/api/disarm":
                    self._send_json(state.disarm())
                elif parsed.path == "/api/start_armed":
                    body = self._read_json()
                    self._send_json(
                        state.start_armed(
                            session_id=str(body.get("session_id", "")),
                            trigger_unix_time=float(body.get("trigger_unix_time", time.time())),
                            start_after_frame_id=int(body.get("start_after_frame_id", 0)),
                        )
                    )
                elif parsed.path == "/api/stop":
                    self._send_json(state.stop())
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            """忽略正常轮询日志，避免长期监测产生无意义的大文件。"""

            return

        def _read_json(self) -> dict[str, Any]:
            """读取 POST 请求里的 JSON。"""

            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)

        def _send_html(self, html: str) -> None:
            """返回 HTML 页面。"""

            payload = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            """返回 JSON。"""

            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_csv_file(self, path: Path) -> None:
            """把 CSV 文件作为附件返回给浏览器下载。"""

            if not path.exists():
                raise FileNotFoundError(str(path))
            payload = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            """用统一 JSON 格式返回错误。"""

            self._send_json({"ok": False, "error": message}, status=status)

    return PowerDriftHandler


def _settings_from_body(body: dict[str, Any]) -> PowerDriftWebSettings:
    """把前端 JSON 转成一次多通道长漂会话设置。

    旧页面只会发送 channel/power_per_volt/zero_voltage，仍然把它转换为一个通道，
    因此浏览器没有强制刷新时也不会立刻失效。
    """

    raw_channels = body.get("channels")
    if raw_channels is None:
        raw_channels = [
            {
                "channel": body.get("channel", "ai2"),
                "note": body.get("note", ""),
                "power_per_volt": body.get("power_per_volt", 1.0),
                "zero_voltage": body.get("zero_voltage", 0.0),
                "unlock_enabled": body.get("unlock_enabled", False),
                "unlock_min_v": body.get("unlock_min_v"),
                "unlock_max_v": body.get("unlock_max_v"),
            }
        ]
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("channels must be a non-empty list")

    channels: list[PowerDriftChannelSettings] = []
    seen: set[str] = set()
    for raw_item in raw_channels:
        if not isinstance(raw_item, dict):
            raise ValueError("each channels item must be an object")
        channel = str(raw_item.get("channel", "")).strip()
        if not channel:
            raise ValueError("channel must not be empty")
        channel_key = channel.lower()
        if channel_key in seen:
            raise ValueError(f"duplicate channel: {channel}")
        seen.add(channel_key)

        unlock_enabled = _bool_value(raw_item.get("unlock_enabled", False))
        unlock_min_v = _optional_float(raw_item.get("unlock_min_v"))
        unlock_max_v = _optional_float(raw_item.get("unlock_max_v"))
        if unlock_enabled:
            if unlock_min_v is None or unlock_max_v is None:
                raise ValueError(f"{channel} 启用脱锁监测后必须填写最小值和最大值")
            if unlock_min_v >= unlock_max_v:
                raise ValueError(f"{channel} 的脱锁最小值必须小于最大值")

        channels.append(
            PowerDriftChannelSettings(
                channel=channel,
                note=str(raw_item.get("note", "")).strip(),
                power_per_volt=float(raw_item.get("power_per_volt", 1.0)),
                zero_voltage=float(raw_item.get("zero_voltage", 0.0)),
                unlock_enabled=unlock_enabled,
                unlock_min_v=unlock_min_v,
                unlock_max_v=unlock_max_v,
            )
        )

    settings = PowerDriftWebSettings(
        channels=channels,
        data_source=str(body.get("data_source", "unified_stream")),
        interval=float(body.get("interval", 1.0)),
        samples=int(body.get("samples", 1000)),
        rate=float(body.get("rate", 1000.0)),
        terminal_config=str(body.get("terminal_config", "RSE")),
        min_val=float(body.get("min_val", -10.0)),
        max_val=float(body.get("max_val", 10.0)),
        timeout=float(body.get("timeout", 10.0)),
        duration=_optional_float(body.get("duration")),
        output_dir=Path(str(body.get("output_dir", DEFAULT_OUTPUT_DIR))),
        api_base_url=str(body.get("api_base_url", DEFAULT_BASE_URL)),
        allow_busy_ai=_bool_value(body.get("allow_busy_ai", False)),
    )
    _validate_settings(settings)
    return settings


def _wait_for_unified_frame_after(
    monitor: PowerDriftMonitor,
    baseline_frame_id: int,
    stop_event: threading.Event,
    timeout: float,
) -> None:
    """同步测试时跳过点击触发前已经缓存在统一流里的旧帧。"""

    deadline = time.time() + max(float(timeout), 1.0)
    while not stop_event.is_set():
        status = monitor.daq.get_unified_ai_stream_status()
        if int(status.get("frame_id", 0)) > baseline_frame_id:
            return
        if time.time() >= deadline:
            raise TimeoutError("等待同步触发后的第一帧超时")
        stop_event.wait(0.02)


def _validate_settings(settings: PowerDriftWebSettings) -> None:
    """检查前端传入的参数是否合理。"""

    if settings.interval <= 0:
        raise ValueError("interval must be > 0")
    if settings.samples < 1:
        raise ValueError("samples must be >= 1")
    if settings.rate <= 0:
        raise ValueError("rate must be > 0")
    if settings.timeout <= 0:
        raise ValueError("timeout must be > 0")
    if settings.duration is not None and settings.duration <= 0:
        raise ValueError("duration must be > 0")
    if settings.terminal_config not in ("RSE", "DIFF", "NRSE"):
        raise ValueError("terminal_config must be RSE, DIFF, or NRSE")
    if settings.data_source not in ("direct_read", "unified_stream"):
        raise ValueError("data_source must be direct_read or unified_stream")


def _monitor_settings(
    settings: PowerDriftWebSettings,
    channel: PowerDriftChannelSettings,
) -> PowerDriftSettings:
    """把公共会话参数和某一路参数组合成已有的单通道读取器设置。"""

    return PowerDriftSettings(
        channel=channel.channel,
        data_source=settings.data_source,
        interval=settings.interval,
        samples=settings.samples,
        rate=settings.rate,
        terminal_config=settings.terminal_config,
        min_val=settings.min_val,
        max_val=settings.max_val,
        timeout=settings.timeout,
        duration=settings.duration,
        output_dir=settings.output_dir,
        api_base_url=settings.api_base_url,
        power_per_volt=channel.power_per_volt,
        zero_voltage=channel.zero_voltage,
        allow_busy_ai=settings.allow_busy_ai,
    )


def _initial_unlock_status(channel: PowerDriftChannelSettings) -> dict[str, Any]:
    """构造记录尚未产生第一个点时的脱锁状态。"""

    return {
        "channel": channel.channel,
        "note": channel.note,
        "enabled": channel.unlock_enabled,
        "state": "waiting" if channel.unlock_enabled else "disabled",
        "min_v": channel.unlock_min_v,
        "max_v": channel.unlock_max_v,
        "latest_mean_v": None,
        "outside_range": False,
        "unlock_event_unix_time": None,
        "unlock_event_iso_time": None,
    }


def _record_with_unlock_status(
    point: PowerDriftPoint,
    channel: PowerDriftChannelSettings,
    previous_event: tuple[float, str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """给一个平均点附加脱锁判断，并锁存本次会话的第一次脱锁时间。"""

    outside_range = False
    event = previous_event
    if channel.unlock_enabled:
        # 正常入口已经验证上下限；这里再检查一次，使这个函数单独调用时也安全。
        lower = channel.unlock_min_v
        upper = channel.unlock_max_v
        if lower is None or upper is None:
            raise ValueError(f"{channel.channel} 的脱锁上下限不能为空")
        outside_range = bool(point.mean_v < lower or point.mean_v > upper)
        if outside_range and event is None:
            event = (
                point.unix_time,
                # 带 UTC 偏移的 ISO 时间可以明确表示绝对时刻，避免只看到相对秒数。
                _local_iso_time(point.unix_time),
            )

    if not channel.unlock_enabled:
        state = "disabled"
    elif event is not None:
        state = "unlocked"
    else:
        state = "locked"

    event_unix_time = event[0] if event is not None else None
    event_iso_time = event[1] if event is not None else None
    record = asdict(point)
    record.update(
        {
            "channel_note": channel.note,
            "unlock_enabled": channel.unlock_enabled,
            "unlock_min_v": channel.unlock_min_v,
            "unlock_max_v": channel.unlock_max_v,
            "outside_unlock_range": outside_range,
            "unlock_state": state,
            "unlock_event_unix_time": event_unix_time,
            "unlock_event_iso_time": event_iso_time,
        }
    )
    status = {
        "channel": channel.channel,
        "note": channel.note,
        "enabled": channel.unlock_enabled,
        "state": state,
        "min_v": channel.unlock_min_v,
        "max_v": channel.unlock_max_v,
        "latest_mean_v": point.mean_v,
        "outside_range": outside_range,
        "unlock_event_unix_time": event_unix_time,
        "unlock_event_iso_time": event_iso_time,
    }
    return record, status


def _optional_float(value: Any) -> float | None:
    """把空字符串转换成 None，否则转换成 float。"""

    if value in (None, ""):
        return None
    return float(value)


def _local_iso_time(timestamp: float) -> str:
    """把 Unix 时间转换为带本地 UTC 偏移的绝对时间字符串。"""

    # Windows 对很早年份的历史时区转换支持不一致；固定使用当前本地时区对象，
    # 对实验中的当前时间和测试构造的旧时间戳都能稳定工作。
    local_timezone = datetime.now().astimezone().tzinfo
    return datetime.fromtimestamp(timestamp, tz=local_timezone).isoformat(timespec="seconds")


def _bool_value(value: Any) -> bool:
    """把 JSON 里的布尔值安全转换成 Python bool。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


def _settings_for_json(settings: PowerDriftWebSettings | None) -> dict[str, Any] | None:
    """把设置转换成 JSON 友好的普通字典。"""

    if settings is None:
        return None
    data = asdict(settings)
    data["output_dir"] = str(settings.output_dir)
    return data


def _factory_default_parameters() -> dict[str, Any]:
    """返回 8767 在用户尚未保存配置时使用的程序默认值。"""

    return {
        "channels": [
            {
                "channel": channel,
                "note": "",
                "power_per_volt": 1.0,
                "zero_voltage": 0.0,
                "unlock_enabled": False,
                "unlock_min_v": None,
                "unlock_max_v": None,
            }
            for channel in ("ai0", "ai1", "ai2")
        ],
        "data_source": "unified_stream",
        "interval": 1.0,
        "samples": 1000,
        "rate": 1000.0,
        "terminal_config": "DIFF",
        "min_val": -10.0,
        "max_val": 10.0,
        "timeout": 10.0,
        "duration": None,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "api_base_url": DEFAULT_BASE_URL,
        "allow_busy_ai": False,
    }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>功率慢漂监测</title>
<style>
  :root {
    --bg: #f5f7f9;
    --panel: #ffffff;
    --line: #d7dde5;
    --text: #18202a;
    --muted: #657386;
    --blue: #1f6feb;
    --green: #16833a;
    --red: #c62828;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    height: 100vh;
    display: grid;
    grid-template-columns: 340px 1fr;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", Arial, sans-serif;
    overflow: hidden;
  }
  aside {
    background: var(--panel);
    border-right: 1px solid var(--line);
    padding: 12px;
    overflow: auto;
  }
  main {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto auto 1fr;
    gap: 10px;
    padding: 12px;
  }
  h1 { font-size: 18px; margin: 0 0 12px; }
  h2 {
    font-size: 14px;
    margin: 18px 0 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--line);
  }
  label {
    display: block;
    font-size: 12px;
    color: var(--muted);
    margin: 8px 0 4px;
  }
  input, select, button {
    width: 100%;
    font: inherit;
    font-size: 13px;
  }
  input, select {
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 7px 8px;
    background: #fff;
    color: var(--text);
  }
  input[type="checkbox"] {
    width: auto;
    margin-right: 7px;
  }
  button {
    border: 0;
    border-radius: 6px;
    padding: 8px 10px;
    background: var(--blue);
    color: #fff;
    font-weight: 650;
    cursor: pointer;
  }
  button.green { background: var(--green); }
  button.secondary { background: #59636f; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .grid2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 12px;
  }
  .inline {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 10px;
    color: var(--text);
    font-size: 13px;
  }
  .status {
    border: 1px solid var(--line);
    background: var(--panel);
    border-radius: 8px;
    padding: 9px 10px;
    font-size: 13px;
    color: var(--muted);
  }
  .status.ok { color: var(--green); }
  .status.error { color: var(--red); }
  .metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr));
    gap: 8px;
  }
  .channel-list-row {
    display: grid;
    grid-template-columns: 1fr 108px;
    gap: 8px;
    align-items: end;
  }
  .channel-settings {
    display: grid;
    gap: 8px;
    margin-top: 8px;
  }
  .channel-config {
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px;
    background: #f8fafc;
  }
  .channel-config-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 4px;
  }
  .channel-panels {
    min-height: 0;
    overflow: auto;
    display: grid;
    /* 每个通道独占一整行，图高仍由 .channel-plot 保持为 220 px。 */
    grid-template-columns: minmax(0, 1fr);
    align-content: start;
    gap: 10px;
  }
  .channel-panel {
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel);
    padding: 10px;
  }
  .channel-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
  }
  .channel-panel-title { font-size: 15px; font-weight: 700; }
  .channel-panel-note { margin-top: 2px; color: var(--muted); font-size: 12px; }
  .unlock-badge {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 3px 8px;
    color: var(--muted);
    font-size: 12px;
    white-space: nowrap;
  }
  .unlock-badge.locked { color: var(--green); border-color: #84c798; background: #f1fbf4; }
  .unlock-badge.unlocked { color: var(--red); border-color: #e2a0a0; background: #fff3f3; }
  .unlock-time {
    margin: 6px 0 2px;
    color: var(--red);
    font-size: 13px;
    font-weight: 700;
  }
  .channel-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(100px, 1fr));
    gap: 8px;
  }
  .channel-metric label { margin: 0 0 3px; }
  .channel-metric div {
    font-family: Consolas, "SFMono-Regular", monospace;
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .channel-plot { height: 220px; margin-top: 8px; border-top: 1px solid var(--line); }
  .channel-detail { margin-top: 6px; color: var(--muted); font-size: 12px; }
  .metric {
    border: 1px solid var(--line);
    background: var(--panel);
    border-radius: 8px;
    padding: 10px;
    min-width: 0;
  }
  .metric label {
    margin: 0 0 5px;
    font-size: 11px;
  }
  .metric div {
    font-family: Consolas, "SFMono-Regular", monospace;
    font-size: 15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .plot {
    min-height: 0;
    border: 1px solid var(--line);
    background: var(--panel);
    border-radius: 8px;
    overflow: hidden;
  }
  canvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    background: var(--panel);
    border: 1px solid var(--line);
  }
  th, td {
    border-bottom: 1px solid var(--line);
    padding: 6px 8px;
    text-align: right;
  }
  th:first-child, td:first-child { text-align: left; }
  .mono { font-family: Consolas, "SFMono-Regular", monospace; }
  @media (max-width: 900px) {
    body { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
    aside { border-right: 0; border-bottom: 1px solid var(--line); max-height: 45vh; }
    .metrics { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
    .channel-panels { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<aside>
  <h1>功率慢漂监测</h1>

  <h2>采集</h2>
  <label>数据来源</label>
  <select id="data_source">
    <option value="unified_stream" selected>unified_stream：读取已经运行的统一 AI 流</option>
    <option value="direct_read">direct_read：单独慢漂监测，独占读取 AI</option>
  </select>
  <div class="channel-list-row">
    <div>
      <label>AI 通道，用英文逗号分隔</label>
      <input id="channel_list" class="setting-control" value="ai0,ai1,ai2">
    </div>
    <button id="applyChannelsBtn" class="secondary setting-control" onclick="applyChannelList()">应用通道</button>
  </div>
  <div id="channelSettings" class="channel-settings"></div>
  <label>接线方式</label>
  <select id="terminal_config">
    <option value="RSE">RSE</option>
    <option value="DIFF" selected>DIFF</option>
    <option value="NRSE">NRSE</option>
  </select>
  <div class="grid2">
    <div>
      <label>记录间隔 / s</label>
      <input id="interval" type="number" value="1" min="0.01" step="0.5">
    </div>
    <div>
      <label>总时长 / s</label>
      <input id="duration" type="number" value="" min="0" step="60" placeholder="空=一直记录">
    </div>
  </div>
  <div class="grid2">
    <div>
      <label>每次点数</label>
      <input id="samples" type="number" value="1000" min="1" step="100">
    </div>
    <div>
      <label>采样率 / Hz</label>
      <input id="rate" type="number" value="1000" min="1" step="100">
    </div>
  </div>
  <div class="grid2">
    <div>
      <label>最小电压 / V</label>
      <input id="min_val" type="number" value="-10" step="0.5">
    </div>
    <div>
      <label>最大电压 / V</label>
      <input id="max_val" type="number" value="10" step="0.5">
    </div>
  </div>
  <label>读取超时 / s</label>
  <input id="timeout" type="number" value="10" min="0.1" step="1">

  <h2>文件与服务</h2>
  <label>输出目录</label>
  <input id="output_dir" value="data/power_drift">
  <label>底层 API</label>
  <input id="api_base_url" value="http://127.0.0.1:8765">
  <label class="inline"><input id="allow_busy_ai" type="checkbox"> 允许 AI 忙时继续</label>
  <div class="actions" style="margin-top:8px;">
    <button class="secondary" id="saveDefaultsBtn" onclick="saveDefaults()">保存为默认值</button>
    <button class="secondary" id="resetDefaultsBtn" onclick="resetDefaults()">恢复程序默认值</button>
  </div>

  <div class="actions">
    <button class="green" id="startBtn" onclick="startMonitor()">开始记录</button>
    <button class="secondary" id="stopBtn" onclick="stopMonitor()" disabled>停止记录</button>
  </div>
  <button class="secondary" id="downloadBtn" onclick="downloadCsv()" disabled style="margin-top:8px;">导出 CSV</button>

  <h2>临时同步测试</h2>
  <label>同步预备状态</label>
  <input id="armed_status" value="未预备" readonly>
  <div class="actions">
    <button class="green" id="armBtn" onclick="armSyncTest()">预备同步触发</button>
    <button class="secondary" id="disarmBtn" onclick="disarmSyncTest()" disabled>取消预备</button>
  </div>
</aside>

<main>
  <div id="status" class="status">WebUI 已打开。请确认 8765 底层 API 服务正在运行。</div>

  <div class="metrics">
    <div class="metric"><label>状态</label><div id="m_running">停止</div></div>
    <div class="metric"><label>记录周期</label><div id="m_cycles">0</div></div>
    <div class="metric"><label>CSV 行数</label><div id="m_rows">0</div></div>
    <div class="metric"><label>CSV 文件</label><div id="m_csv">--</div></div>
  </div>

  <div id="channelPanels" class="channel-panels"></div>
</main>

<script>
let latestStatus = null;
let pollTimer = null;
let channelDrafts = [];
let panelSignature = '';
let sidebarSessionSignature = '';
const CHANNEL_COLORS = ['#1f6feb', '#16833a', '#b05a00', '#8b5cf6', '#c62828', '#00796b'];
const COMMON_SETTING_IDS = [
  'channel_list', 'applyChannelsBtn', 'data_source', 'interval', 'duration',
  'samples', 'rate', 'terminal_config', 'min_val', 'max_val', 'timeout',
  'output_dir', 'api_base_url', 'allow_busy_ai', 'saveDefaultsBtn', 'resetDefaultsBtn',
];

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function parseChannelList() {
  const channels = document.getElementById('channel_list').value
    .split(',')
    .map(value => value.trim())
    .filter(Boolean);
  if (!channels.length) throw new Error('请至少填写一个 AI 通道。');
  const keys = channels.map(channel => channel.toLowerCase());
  if (new Set(keys).size !== keys.length) throw new Error('AI 通道不能重复。');
  return channels;
}

function readChannelDrafts() {
  return channelDrafts.map((draft, index) => {
    const unlockEnabled = document.getElementById(`channel_unlock_${index}`).checked;
    return {
      channel: draft.channel,
      note: document.getElementById(`channel_note_${index}`).value.trim(),
      power_per_volt: Number(document.getElementById(`channel_power_${index}`).value),
      zero_voltage: Number(document.getElementById(`channel_zero_${index}`).value),
      unlock_enabled: unlockEnabled,
      unlock_min_v: unlockEnabled ? document.getElementById(`channel_min_${index}`).value : '',
      unlock_max_v: unlockEnabled ? document.getElementById(`channel_max_${index}`).value : '',
    };
  });
}

function renderChannelSettings() {
  const container = document.getElementById('channelSettings');
  container.innerHTML = channelDrafts.map((draft, index) => `
    <section class="channel-config">
      <div class="channel-config-title">${escapeHtml(draft.channel)}</div>
      <label>备注</label>
      <input id="channel_note_${index}" class="channel-setting" type="text"
             value="${escapeHtml(draft.note ?? '')}" placeholder="例如：总功率 PD"
             oninput="updateDraftNote(${index})">
      <div class="grid2">
        <div>
          <label>功率/电压系数</label>
          <input id="channel_power_${index}" class="channel-setting" type="number"
                 value="${escapeHtml(draft.power_per_volt ?? 1)}" step="0.001">
        </div>
        <div>
          <label>零功率电压 / V</label>
          <input id="channel_zero_${index}" class="channel-setting" type="number"
                 value="${escapeHtml(draft.zero_voltage ?? 0)}" step="0.001">
        </div>
      </div>
      <label class="inline">
        <input id="channel_unlock_${index}" class="channel-setting" type="checkbox"
               ${draft.unlock_enabled ? 'checked' : ''} onchange="toggleUnlock(${index})">
        脱锁监测
      </label>
      <div class="grid2">
        <div>
          <label>允许最小值 / V</label>
          <input id="channel_min_${index}" class="channel-setting unlock-limit" type="number"
                 value="${draft.unlock_min_v ?? ''}" step="0.001">
        </div>
        <div>
          <label>允许最大值 / V</label>
          <input id="channel_max_${index}" class="channel-setting unlock-limit" type="number"
                 value="${draft.unlock_max_v ?? ''}" step="0.001">
        </div>
      </div>
    </section>
  `).join('');
  channelDrafts.forEach((_, index) => toggleUnlock(index));
}

function applyChannelList() {
  try {
    const oldDrafts = new Map();
    if (channelDrafts.length && document.querySelector('.channel-setting')) {
      readChannelDrafts().forEach(item => oldDrafts.set(item.channel.toLowerCase(), item));
    }
    channelDrafts = parseChannelList().map(channel => oldDrafts.get(channel.toLowerCase()) || {
      channel,
      note: '',
      power_per_volt: 1,
      zero_voltage: 0,
      unlock_enabled: false,
      unlock_min_v: '',
      unlock_max_v: '',
    });
    renderChannelSettings();
    ensureChannelPanels(channelDrafts);
    setStatus(`已应用 ${channelDrafts.length} 个通道。`, 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

function updateDraftNote(index) {
  channelDrafts[index].note = document.getElementById(`channel_note_${index}`).value.trim();
  panelSignature = '';
  ensureChannelPanels(channelDrafts);
}

function toggleUnlock(index) {
  const enabled = document.getElementById(`channel_unlock_${index}`).checked;
  const globallyLocked = Boolean(latestStatus && (latestStatus.running || latestStatus.armed));
  document.getElementById(`channel_min_${index}`).disabled = globallyLocked || !enabled;
  document.getElementById(`channel_max_${index}`).disabled = globallyLocked || !enabled;
}

function getSettings() {
  return {
    channels: readChannelDrafts(),
    data_source: document.getElementById('data_source').value,
    interval: Number(document.getElementById('interval').value),
    samples: Number(document.getElementById('samples').value),
    rate: Number(document.getElementById('rate').value),
    terminal_config: document.getElementById('terminal_config').value,
    min_val: Number(document.getElementById('min_val').value),
    max_val: Number(document.getElementById('max_val').value),
    timeout: Number(document.getElementById('timeout').value),
    duration: document.getElementById('duration').value,
    output_dir: document.getElementById('output_dir').value,
    api_base_url: document.getElementById('api_base_url').value,
    allow_busy_ai: document.getElementById('allow_busy_ai').checked,
  };
}

function setStatus(text, kind) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = 'status' + (kind ? ' ' + kind : '');
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || '请求失败');
  }
  return data;
}

function setControlValue(id, value) {
  const element = document.getElementById(id);
  if (!element) return;
  if (element.type === 'checkbox') {
    element.checked = Boolean(value);
  } else {
    element.value = value === null || value === undefined ? '' : value;
  }
}

function applyDefaultsToUi(defaults) {
  const channels = Array.isArray(defaults.channels) && defaults.channels.length
    ? defaults.channels
    : [{channel: 'ai0'}, {channel: 'ai1'}, {channel: 'ai2'}];
  channelDrafts = channels.map(item => typeof item === 'string'
    ? {
        channel: item,
        note: '',
        power_per_volt: 1,
        zero_voltage: 0,
        unlock_enabled: false,
        unlock_min_v: '',
        unlock_max_v: '',
      }
    : {...item, note: item.note || ''});
  document.getElementById('channel_list').value = channelDrafts.map(item => item.channel).join(',');

  [
    'data_source', 'interval', 'duration', 'samples', 'rate', 'terminal_config',
    'min_val', 'max_val', 'timeout', 'output_dir', 'api_base_url', 'allow_busy_ai',
  ].forEach(id => setControlValue(id, defaults[id]));
  renderChannelSettings();
  panelSignature = '';
  ensureChannelPanels(channelDrafts);
}

async function loadDefaults(showMessage = true) {
  try {
    const response = await fetch('/api/defaults');
    const defaults = await response.json();
    if (!response.ok || defaults.ok === false) {
      throw new Error(defaults.error || '读取默认值失败');
    }
    applyDefaultsToUi(defaults);
    if (showMessage) {
      const source = defaults.defaults_source === 'user' ? '用户默认值' : '程序默认值';
      setStatus(`已加载${source}。`, 'ok');
    }
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

async function saveDefaults() {
  try {
    const defaults = await postJson('/api/defaults/save', getSettings());
    applyDefaultsToUi(defaults);
    setStatus(`当前参数和通道备注已保存为默认值：${defaults.defaults_file}`, 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

async function resetDefaults() {
  try {
    const defaults = await postJson('/api/defaults/reset', {});
    applyDefaultsToUi(defaults);
    setStatus('已恢复程序默认值。', 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

async function startMonitor() {
  try {
    const status = await postJson('/api/start', getSettings());
    updateStatus(status);
    setStatus('功率慢漂记录已启动。', 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
    await refreshStatus(false);
  }
}

async function stopMonitor() {
  try {
    const status = await postJson('/api/stop', {});
    updateStatus(status);
    setStatus('功率慢漂记录已停止。', 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

async function armSyncTest() {
  try {
    const status = await postJson('/api/arm', getSettings());
    updateStatus(status);
    setStatus('功率慢漂参数已预备。现在可以到双峰页面点击同步开始。', 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
    await refreshStatus(false);
  }
}

async function disarmSyncTest() {
  try {
    const status = await postJson('/api/disarm', {});
    updateStatus(status);
    setStatus('已取消同步预备，可以继续修改功率慢漂参数。', 'ok');
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  }
}

function downloadCsv() {
  window.location.href = '/api/download';
}

async function refreshStatus(showError = true) {
  try {
    const response = await fetch('/api/status');
    const status = await response.json();
    if (!response.ok || status.ok === false) {
      throw new Error(status.error || '查询状态失败');
    }
    updateStatus(status);
  } catch (err) {
    if (showError) {
      setStatus(String(err.message || err), 'error');
    }
  }
}

function updateStatus(status) {
  latestStatus = status;
  const running = Boolean(status.running);
  const armed = Boolean(status.armed);
  const error = status.error ? `错误：${status.error}` : '';

  // 页面在记录期间被刷新时，从后端恢复真正运行中的通道和阈值。
  const activeSettings = running
    ? status.settings
    : (armed ? status.armed_settings : null);
  if ((running || armed) && activeSettings && Array.isArray(activeSettings.channels)) {
    const signature = JSON.stringify(activeSettings.channels);
    if (signature !== sidebarSessionSignature) {
      sidebarSessionSignature = signature;
      channelDrafts = activeSettings.channels.map(item => ({...item}));
      document.getElementById('channel_list').value = channelDrafts.map(item => item.channel).join(',');
      renderChannelSettings();
    }
  } else if (!running && !armed) {
    sidebarSessionSignature = '';
  }

  document.getElementById('startBtn').disabled = running || armed;
  document.getElementById('stopBtn').disabled = !running;
  document.getElementById('armBtn').disabled = running || armed;
  document.getElementById('disarmBtn').disabled = running || !armed;
  document.getElementById('downloadBtn').disabled = !status.csv_file;
  document.getElementById('armed_status').value = running && status.session_id
    ? `同步记录中：${status.session_id}`
    : (armed ? '已预备，等待双峰页面触发' : '未预备');
  COMMON_SETTING_IDS.forEach(id => {
    const element = document.getElementById(id);
    if (element) element.disabled = running || armed;
  });
  document.querySelectorAll('.channel-setting').forEach(element => {
    element.disabled = running || armed;
  });
  if (!running && !armed) {
    channelDrafts.forEach((_, index) => toggleUnlock(index));
  }
  const unlockedChannels = Object.values(status.unlock_status || {})
    .filter(item => item && item.state === 'unlocked')
    .map(item => `${item.channel} 脱锁于 ${formatAbsoluteTime(item)}`);
  document.getElementById('m_running').textContent = error
    || (unlockedChannels.length ? `脱锁：${unlockedChannels.join('；')}` : (running ? '记录中' : '停止'));
  document.getElementById('m_cycles').textContent = status.cycles_written || 0;
  document.getElementById('m_rows').textContent = status.rows_written || 0;
  const csvElement = document.getElementById('m_csv');
  csvElement.textContent = status.csv_file ? status.csv_file.split(/[\\/]/).pop() : '--';
  csvElement.title = status.csv_file || '';

  const configuredChannels = activeSettings && Array.isArray(activeSettings.channels)
    ? activeSettings.channels
    : channelDrafts;
  ensureChannelPanels(configuredChannels);
  updateChannelPanels(status, configuredChannels);
}

function ensureChannelPanels(channels) {
  const channelViews = channels.map(item => typeof item === 'string'
    ? {channel: item, note: ''}
    : {channel: item.channel, note: item.note || ''});
  const signature = JSON.stringify(channelViews);
  if (signature === panelSignature) return;
  panelSignature = signature;
  document.getElementById('channelPanels').innerHTML = channelViews.map((item, index) => `
    <section class="channel-panel">
      <div class="channel-panel-header">
        <div>
          <div class="channel-panel-title">${escapeHtml(item.channel)}</div>
          ${item.note ? `<div class="channel-panel-note">${escapeHtml(item.note)}</div>` : ''}
        </div>
        <div id="unlock_badge_${index}" class="unlock-badge">未启用</div>
      </div>
      <div class="channel-metrics">
        <div class="channel-metric"><label>均值 / V</label><div id="channel_mean_${index}">--</div></div>
        <div class="channel-metric"><label>标准差 / V</label><div id="channel_std_${index}">--</div></div>
        <div class="channel-metric"><label>相对标准差</label><div id="channel_rel_${index}">--</div></div>
      </div>
      <div id="unlock_time_${index}" class="unlock-time" hidden></div>
      <div class="channel-plot"><canvas id="trendCanvas_${index}"></canvas></div>
      <div id="channel_detail_${index}" class="channel-detail">等待数据...</div>
    </section>
  `).join('');
}

function lookupChannelValue(values, channel) {
  if (!values) return null;
  if (values[channel] !== undefined) return values[channel];
  const shortName = channel.includes('/') ? channel.split('/').pop() : channel;
  const fullName = channel.includes('/') ? channel : `Dev2/${channel}`;
  return values[shortName] ?? values[fullName] ?? null;
}

function formatExponential(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? '--'
    : Number(value).toExponential(6);
}

function formatAbsoluteTime(unlock) {
  if (!unlock) return '--';
  const unixTime = Number(unlock.unlock_event_unix_time);
  if (Number.isFinite(unixTime)) {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
      timeZoneName: 'short',
    }).format(new Date(unixTime * 1000));
  }
  return unlock.unlock_event_iso_time || '--';
}

function updateChannelPanels(status, channels) {
  channels.forEach((item, index) => {
    const channel = typeof item === 'string' ? item : item.channel;
    const point = lookupChannelValue(status.latest_points, channel);
    const unlock = lookupChannelValue(status.unlock_status, channel);
    const points = lookupChannelValue(status.recent_points_by_channel, channel) || [];
    document.getElementById(`channel_mean_${index}`).textContent = point ? formatExponential(point.mean_v) : '--';
    document.getElementById(`channel_std_${index}`).textContent = point ? formatExponential(point.std_v) : '--';
    document.getElementById(`channel_rel_${index}`).textContent = point && point.rel_std_percent !== null
      ? Number(point.rel_std_percent).toFixed(4) + '%'
      : '--';

    const badge = document.getElementById(`unlock_badge_${index}`);
    const unlockTime = document.getElementById(`unlock_time_${index}`);
    badge.className = 'unlock-badge';
    unlockTime.hidden = true;
    unlockTime.textContent = '';
    if (!unlock || !unlock.enabled) {
      badge.textContent = '未启用';
    } else if (unlock.state === 'waiting') {
      badge.textContent = '等待首点';
    } else if (unlock.state === 'unlocked') {
      badge.classList.add('unlocked');
      badge.textContent = '脱锁';
      unlockTime.hidden = false;
      unlockTime.textContent = `脱锁时间（本地绝对时间）：${formatAbsoluteTime(unlock)}`;
    } else {
      badge.classList.add('locked');
      badge.textContent = '锁定范围内';
    }

    const rangeText = unlock && unlock.enabled
      ? `${formatExponential(unlock.min_v)} 至 ${formatExponential(unlock.max_v)} V`
      : '未启用';
    const currentState = unlock && unlock.enabled
      ? (unlock.outside_range ? '本点越界' : '本点在范围内')
      : '不判断';
    document.getElementById(`channel_detail_${index}`).textContent = point
      ? `时间 ${point.iso_time} · ${point.samples} 点 · 峰峰值 ${formatExponential(point.peak_to_peak_v)} V · 功率估计 ${formatExponential(point.power_estimate)} · 脱锁范围 ${rangeText} · ${currentState}`
      : `等待数据 · 脱锁范围 ${rangeText}`;
    drawChannelTrend(document.getElementById(`trendCanvas_${index}`), points, CHANNEL_COLORS[index % CHANNEL_COLORS.length]);
  });
}

function drawChannelTrend(canvas, points, color) {
  if (!canvas) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.max(360, Math.floor(rect.width * devicePixelRatio));
  canvas.height = Math.max(220, Math.floor(rect.height * devicePixelRatio));
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);

  const padX = 56 * devicePixelRatio;
  const padY = 30 * devicePixelRatio;
  const x0 = padX;
  const y0 = padY;
  const plotW = w - 2 * padX;
  const plotH = h - 2 * padY;

  ctx.strokeStyle = '#d7dde5';
  ctx.lineWidth = 1 * devicePixelRatio;
  ctx.strokeRect(x0, y0, plotW, plotH);

  ctx.fillStyle = '#657386';
  ctx.font = `${12 * devicePixelRatio}px Consolas`;
  ctx.fillText('mean voltage trend / V', x0, 20 * devicePixelRatio);

  if (points.length < 2) {
    ctx.fillText('等待数据...', x0 + 12 * devicePixelRatio, y0 + 28 * devicePixelRatio);
    return;
  }

  const xs = points.map(p => Number(p.elapsed_s));
  const ys = points.map(p => Number(p.mean_v));
  const minX = xs[0];
  const maxX = xs[xs.length - 1];
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (Math.abs(maxY - minY) < 1e-15) {
    minY -= 1;
    maxY += 1;
  }
  const yPad = (maxY - minY) * 0.08;
  minY -= yPad;
  maxY += yPad;

  ctx.beginPath();
  points.forEach((p, i) => {
    const x = x0 + ((Number(p.elapsed_s) - minX) / Math.max(1e-12, maxX - minX)) * plotW;
    const y = y0 + (1 - (Number(p.mean_v) - minY) / Math.max(1e-30, maxY - minY)) * plotH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6 * devicePixelRatio;
  ctx.stroke();

  ctx.fillStyle = '#657386';
  ctx.fillText(`t ${minX.toFixed(1)}-${maxX.toFixed(1)} s`, x0, h - 10 * devicePixelRatio);
  ctx.fillText(`y ${minY.toExponential(3)}-${maxY.toExponential(3)}`, x0 + 180 * devicePixelRatio, h - 10 * devicePixelRatio);
}

function drawAllTrends() {
  if (!latestStatus) return;
  const settings = latestStatus.running
    ? latestStatus.settings
    : (latestStatus.armed ? latestStatus.armed_settings : null);
  const channels = settings && Array.isArray(settings.channels)
    ? settings.channels
    : channelDrafts;
  updateChannelPanels(latestStatus, channels);
}

window.addEventListener('resize', drawAllTrends);
async function initializePage() {
  await loadDefaults(false);
  await refreshStatus(false);
  pollTimer = setInterval(() => refreshStatus(false), 1000);
}
initializePage();
</script>
</body>
</html>
"""


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="Run the photodetector power drift WebUI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    state = PowerDriftWebState()
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Power drift WebUI running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping power drift WebUI.")
    finally:
        state.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
