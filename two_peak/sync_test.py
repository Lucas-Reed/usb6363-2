"""双峰窗口记录与功率慢漂的临时同步测试协调器。

协调器不访问 nidaqmx，也不启动统一 AI 流。它只完成三件事：
1. 检查 8767 是否已经预备；
2. 用同一个 session_id 依次启动功率记录和现有趋势记录；
3. 监视两边状态，在单边异常退出时停止另一边。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from two_peak.trend_logger import AreaTrendLogger
from usb6363_client import Usb6363Client


TrendStarter = Callable[[dict[str, Any]], dict[str, Any]]


class SyncTestCoordinator:
    """管理一次软件触发的双记录器测试会话。"""

    def __init__(
        self,
        daq: Usb6363Client,
        trend_logger: AreaTrendLogger,
        output_dir: Path,
    ) -> None:
        self._daq = daq
        self._trend_logger = trend_logger
        self._output_dir = output_dir
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop: threading.Event | None = None
        self._running = False
        self._error: str | None = None
        self._session_id: str | None = None
        self._trigger_unix_time: float | None = None
        self._baseline_frame_id = 0
        self._power_api_url = "http://127.0.0.1:8767"
        self._manifest_path: Path | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._trend_status: dict[str, Any] = {}
        self._power_status: dict[str, Any] = {}
        self._unified_settings: dict[str, Any] = {}

    def start(
        self,
        body: dict[str, Any],
        start_trend: TrendStarter,
    ) -> dict[str, Any]:
        """预检后启动两边记录；本地启动失败时回滚远端功率记录。"""

        with self._operation_lock:
            if self.status().get("running"):
                raise RuntimeError("同步测试已经在运行")

            power_url = _validate_local_power_url(
                str(body.get("power_api_url", "http://127.0.0.1:8767"))
            )
            voltage_mode = str(body.get("window_voltage_mode", "none")).lower()
            record_full_frame = bool(body.get("record_full_frame", False))
            if voltage_mode == "none" and not record_full_frame:
                raise ValueError("同步测试必须选择动态窗口记录或完整分析通道记录")

            trend_before = self._trend_logger.status()
            if trend_before.get("running"):
                raise RuntimeError("面积/峰高慢漂记录已经在运行，请先停止")

            unified = self._daq.get_unified_ai_stream_status()
            if not unified.get("running"):
                raise RuntimeError("统一 AI 流没有运行，请先启动统一采集")

            power_before = _request_json(power_url, "/api/status")
            if power_before.get("running"):
                raise RuntimeError("功率慢漂已经在运行，请先停止")
            if not power_before.get("armed"):
                raise RuntimeError("功率慢漂尚未预备，请先在 8767 页面点击“预备同步触发”")
            armed_settings = power_before.get("armed_settings") or {}
            if armed_settings.get("data_source") != "unified_stream":
                raise RuntimeError("功率慢漂同步预备必须使用 unified_stream")

            now = time.time()
            session_id = f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            baseline_frame_id = int(unified.get("frame_id", 0))
            self._output_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = self._output_dir / f"{session_id}.json"

            with self._lock:
                self._running = False
                self._error = None
                self._session_id = session_id
                self._trigger_unix_time = now
                self._baseline_frame_id = baseline_frame_id
                self._power_api_url = power_url
                self._manifest_path = manifest_path
                self._started_at = now
                self._finished_at = None
                self._trend_status = {}
                self._power_status = power_before
                self._unified_settings = dict(unified.get("settings") or {})
            self._write_manifest("starting")

            power_status = _request_json(
                power_url,
                "/api/start_armed",
                method="POST",
                body={
                    "session_id": session_id,
                    "trigger_unix_time": now,
                    "start_after_frame_id": baseline_frame_id,
                },
            )
            try:
                trend_body = dict(body)
                trend_body.update(
                    {
                        "session_id": session_id,
                        "trigger_unix_time": now,
                        "start_after_frame_id": baseline_frame_id,
                    }
                )
                trend_status = start_trend(trend_body)
            except Exception as exc:
                rollback_error = None
                try:
                    _request_json(power_url, "/api/stop", method="POST", body={})
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
                with self._lock:
                    self._error = (
                        f"双峰记录启动失败：{exc}"
                        + (f"；功率记录回滚失败：{rollback_error}" if rollback_error else "")
                    )
                    self._finished_at = time.time()
                    self._power_status = power_status
                self._write_manifest("start_failed")
                raise RuntimeError(self._error) from exc

            with self._lock:
                self._running = True
                self._trend_status = trend_status
                self._power_status = power_status
            self._write_manifest("running")
            self._start_monitor()
            return self.status()

    def stop(self) -> dict[str, Any]:
        """幂等停止两边记录，并刷新会话清单。"""

        with self._operation_lock:
            monitor_stop = self._monitor_stop
            if monitor_stop is not None:
                monitor_stop.set()

            errors: list[str] = []
            try:
                trend_status = self._trend_logger.stop()
            except Exception as exc:
                trend_status = {}
                errors.append(f"停止双峰记录失败：{exc}")
            try:
                power_status = _request_json(
                    self._power_api_url,
                    "/api/stop",
                    method="POST",
                    body={},
                )
            except Exception as exc:
                power_status = {}
                # 尚未开始过会话时，8767 不在线不应让本地停止操作失败。
                if self._session_id is not None:
                    errors.append(f"停止功率记录失败：{exc}")

            thread = self._monitor_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3.0)
            with self._lock:
                self._running = False
                self._finished_at = time.time()
                self._trend_status = trend_status
                self._power_status = power_status
                if errors:
                    self._error = "；".join(errors)
            if self._manifest_path is not None:
                self._write_manifest("stopped" if not errors else "stop_error")
            return self.status()

    def status(self, power_api_url: str | None = None) -> dict[str, Any]:
        """返回同步状态；未运行时可顺便探测功率端是否已经预备。"""

        with self._lock:
            result = {
                "running": self._running,
                "error": self._error,
                "session_id": self._session_id,
                "trigger_unix_time": self._trigger_unix_time,
                "baseline_frame_id": self._baseline_frame_id,
                "power_api_url": self._power_api_url,
                "manifest_file": (
                    str(self._manifest_path.resolve()) if self._manifest_path else None
                ),
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "trend": _compact_trend_status(self._trend_status),
                "power": _compact_power_status(self._power_status),
            }

        probe_url = power_api_url
        if not result["running"] and probe_url:
            try:
                probe = _request_json(_validate_local_power_url(probe_url), "/api/status")
                result["power_probe_error"] = None
                result["power_armed"] = bool(probe.get("armed"))
                result["power_running"] = bool(probe.get("running"))
            except Exception as exc:
                result["power_probe_error"] = str(exc)
                result["power_armed"] = False
                result["power_running"] = False
        else:
            result["power_probe_error"] = None
            result["power_armed"] = bool(result["power"].get("armed"))
            result["power_running"] = bool(result["power"].get("running"))
        return result

    def _start_monitor(self) -> None:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._monitor_worker,
            args=(stop_event,),
            daemon=True,
            name="two-peak-sync-test-monitor",
        )
        self._monitor_stop = stop_event
        self._monitor_thread = thread
        thread.start()

    def _monitor_worker(self, stop_event: threading.Event) -> None:
        """每秒检查两边；一边意外停止时收掉另一边。"""

        consecutive_failures = 0
        while not stop_event.wait(1.0):
            try:
                trend = self._trend_logger.status()
                power = _request_json(self._power_api_url, "/api/status")
                consecutive_failures = 0
                with self._lock:
                    self._trend_status = trend
                    self._power_status = power
                    if self._error and self._error.startswith("同步状态检查失败（"):
                        self._error = None

                trend_running = bool(trend.get("running"))
                power_running = bool(power.get("running"))
                if trend_running and power_running:
                    self._write_manifest("running")
                    continue
                if not trend_running and not power_running:
                    with self._lock:
                        self._running = False
                        self._finished_at = time.time()
                        if trend.get("error") or power.get("error"):
                            self._error = str(trend.get("error") or power.get("error"))
                    self._write_manifest("completed" if self._error is None else "error")
                    return

                if trend_running:
                    trend = self._trend_logger.stop()
                if power_running:
                    power = _request_json(
                        self._power_api_url,
                        "/api/stop",
                        method="POST",
                        body={},
                    )
                with self._lock:
                    self._running = False
                    self._finished_at = time.time()
                    self._trend_status = trend
                    self._power_status = power
                    # 达到用户设定时长属于正常结束。同步协调器会收掉功率记录，
                    # 但不会把一个按计划完成的实验标成“意外停止”。
                    trend_finished_on_time = (
                        not trend_running
                        and trend.get("stop_reason") == "duration_elapsed"
                        and not trend.get("error")
                    )
                    if trend_finished_on_time:
                        self._error = None
                    else:
                        stopped_side = "功率慢漂" if not power_running else "双峰窗口记录"
                        detail = power.get("error") if not power_running else trend.get("error")
                        self._error = f"{stopped_side}意外停止" + (f"：{detail}" if detail else "")
                self._write_manifest("completed" if self._error is None else "error")
                return
            except Exception as exc:
                consecutive_failures += 1
                with self._lock:
                    self._error = (
                        f"同步状态检查失败（{consecutive_failures}/3）：{exc}"
                    )
                self._write_manifest("monitor_warning")
                if consecutive_failures >= 3:
                    try:
                        self._trend_logger.stop()
                    except Exception:
                        pass
                    with self._lock:
                        self._running = False
                        self._finished_at = time.time()
                        self._error = f"功率服务连续 3 次无法访问，已停止双峰记录：{exc}"
                    self._write_manifest("error")
                    return

    def _write_manifest(self, state: str) -> None:
        """把三个输出位置和会话状态写入一个小型 JSON 清单。"""

        with self._lock:
            path = self._manifest_path
            if path is None:
                return
            trend = _compact_trend_status(self._trend_status)
            power = _compact_power_status(self._power_status)
            payload = {
                "format": "two_peak_power_sync_test_v1",
                "state": state,
                "session_id": self._session_id,
                "trigger_unix_time": self._trigger_unix_time,
                "baseline_frame_id": self._baseline_frame_id,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "error": self._error,
                "power_api_url": self._power_api_url,
                "unified_settings_at_trigger": dict(self._unified_settings),
                "trend_csv": trend.get("csv_file"),
                "window_voltage_directory": (
                    (trend.get("window_voltage") or {}).get("directory")
                ),
                "power_csv": power.get("csv_file"),
                "power_metadata": power.get("metadata_file"),
                "trend_status": trend,
                "power_status": power,
            }
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)


def _validate_local_power_url(value: str) -> str:
    """同步测试只允许访问本机 8767 一类的 HTTP 服务。"""

    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("功率服务地址必须是本机 HTTP 地址")
    if parsed.path not in ("", "/"):
        raise ValueError("功率服务地址不要包含 API 路径")
    return normalized


def _request_json(
    base_url: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """调用本机功率 WebUI API，并把 HTTP 错误转换成可读异常。"""

    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"功率服务返回错误：{message}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接功率服务 {base_url}：{exc.reason}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("功率服务返回了无效 JSON")
    if result.get("ok") is False:
        raise RuntimeError(str(result.get("error", "功率服务请求失败")))
    return result


def _compact_trend_status(status: dict[str, Any]) -> dict[str, Any]:
    """清除 recent_stats，避免同步状态和清单被趋势历史撑大。"""

    if not status:
        return {}
    return {
        key: value
        for key, value in status.items()
        if key not in {"recent_stats", "latest_stats"}
    }


def _compact_power_status(status: dict[str, Any]) -> dict[str, Any]:
    """清除单通道和多通道历史点，状态接口只保留会话摘要。"""

    if not status:
        return {}
    return {
        key: value
        for key, value in status.items()
        if key not in {"recent_points", "recent_points_by_channel"}
    }
