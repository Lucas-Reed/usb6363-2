"""双峰波形查看器的 HTTP 服务。"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

from two_peak.viewer_capture import (
    frame_summary,
    get_area_trend_status,
    get_frame_stream_latest,
    get_frame_stream_status,
    list_saved_frames,
    load_saved_frame,
    measure_latest_frame,
    save_latest_frame,
    start_area_trend,
    start_frame_stream,
    stop_area_trend,
    stop_frame_stream,
    update_area_trend_windows,
)
from two_peak.viewer_state import ViewerState


STATIC_DIR = Path(__file__).with_name("static")


def load_viewer_html() -> str:
    """读取前端页面。"""

    return (STATIC_DIR / "viewer.html").read_text(encoding="utf-8")


def make_handler(state: ViewerState):
    """创建 HTTP 请求处理类。"""

    class TwoPeakViewerHandler(BaseHTTPRequestHandler):
        server_version = "TwoPeakViewer/0.2"

        def do_GET(self) -> None:
            """处理页面和只读 API。"""

            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/":
                    self._send_html(load_viewer_html())
                elif path == "/api/defaults":
                    self._send_json(state.active_web_defaults())
                elif path == "/api/latest":
                    self._send_json(
                        {
                            "has_frame": state.latest_frame is not None,
                            "frame": frame_summary(state.latest_frame),
                            "measurement": state.latest_measurement,
                        }
                    )
                elif path == "/api/stream/status":
                    self._send_json(
                        get_frame_stream_status(
                            state,
                            query.get("stream_source", ["unified_stream"])[0],
                        )
                    )
                elif path == "/api/stream/latest":
                    self._send_json(
                        get_frame_stream_latest(
                            state,
                            {
                                "channels": query.get("channels", [""])[0],
                                "stream_source": query.get("stream_source", [""])[0],
                            },
                        )
                    )
                elif path == "/api/trend/status":
                    self._send_json(get_area_trend_status(state))
                elif path == "/api/test_sync/status":
                    self._send_json(
                        state.sync_test.status(
                            power_api_url=query.get("power_api_url", [None])[0]
                        )
                    )
                elif path == "/api/ao_scan/status":
                    self._send_json(state.ao_scan_calibrator.status())
                elif path == "/api/power_lock/status":
                    self._send_json(state.power_lock.status())
                elif path == "/api/samples":
                    self._send_json(list_saved_frames(state))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            """处理会改变查看器状态的 API。"""

            try:
                if self.path == "/api/stream/start":
                    body = self._read_json()
                    self._send_json(start_frame_stream(state, body))
                elif self.path == "/api/stream/stop":
                    body = self._read_json()
                    self._send_json(stop_frame_stream(state, body))
                elif self.path == "/api/trend/start":
                    body = self._read_json()
                    self._send_json(start_area_trend(state, body))
                elif self.path == "/api/trend/stop":
                    self._send_json(stop_area_trend(state))
                elif self.path == "/api/trend/update_windows":
                    body = self._read_json()
                    self._send_json(update_area_trend_windows(state, body))
                elif self.path == "/api/test_sync/start":
                    body = self._read_json()
                    self._send_json(
                        state.sync_test.start(
                            body,
                            start_trend=lambda parameters: start_area_trend(state, parameters),
                        )
                    )
                elif self.path == "/api/test_sync/stop":
                    self._send_json(state.sync_test.stop())
                elif self.path == "/api/ao_scan/start":
                    body = self._read_json()
                    self._send_json(
                        state.ao_scan_calibrator.start(
                            channel=str(body.get("channel", "ao0")),
                            start_voltage=float(body.get("start_voltage", 0.0)),
                            stop_voltage=float(body.get("stop_voltage", 1.0)),
                            step_voltage=float(body.get("step_voltage", 0.05)),
                            min_val=float(body.get("min_val", -10.0)),
                            max_val=float(body.get("max_val", 10.0)),
                            settle_s=float(body.get("settle_s", 0.5)),
                            dwell_s=float(body.get("dwell_s", 2.0)),
                            measure_field=str(body.get("measure_field", "area_sum_ema")),
                            restore_voltage=_optional_float(body.get("restore_voltage")),
                        )
                    )
                elif self.path == "/api/ao_scan/stop":
                    self._send_json(state.ao_scan_calibrator.stop())
                elif self.path == "/api/ao_test/write":
                    body = self._read_json()
                    self._send_json(_write_single_ao_for_test(state, body))
                elif self.path == "/api/power_lock/write_initial_ao":
                    body = self._read_json()
                    self._send_json(_write_power_lock_initial_ao(state, body))
                elif self.path == "/api/power_lock/start":
                    body = self._read_json()
                    self._send_json(
                        state.power_lock.start(
                            controllers=body.get("controllers", []),
                            update_s=float(body.get("update_s", 1.0)),
                        )
                    )
                elif self.path == "/api/power_lock/update":
                    body = self._read_json()
                    self._send_json(
                        state.power_lock.update_parameters(
                            controllers=body.get("controllers", []),
                            update_s=float(body.get("update_s", 1.0)),
                        )
                    )
                elif self.path == "/api/power_lock/stop":
                    self._send_json(state.power_lock.stop())
                elif self.path == "/api/measure":
                    body = self._read_json()
                    measurement = measure_latest_frame(state, body)
                    state.latest_measurement = measurement
                    self._send_json(measurement)
                elif self.path == "/api/save":
                    body = self._read_json()
                    saved = save_latest_frame(state, body)
                    self._send_json(saved)
                elif self.path == "/api/load":
                    body = self._read_json()
                    loaded = load_saved_frame(state, body)
                    self._send_json(loaded)
                elif self.path == "/api/defaults/save":
                    body = self._read_json()
                    defaults = state.save_user_defaults(body.get("parameters", body))
                    self._send_json(defaults)
                elif self.path == "/api/defaults/reset":
                    defaults = state.reset_user_defaults()
                    self._send_json(defaults)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            """忽略正常 HTTP 访问日志，避免高频轮询持续写盘。

            真正的采集错误仍会通过各状态 API 返回给 WebUI；正常情况下每秒十几次的
            status/latest 请求没有诊断价值，长期打印反而会产生很大的日志文件。
            """

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

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            """用统一格式返回错误。"""

            self._send_json({"ok": False, "error": message}, status=status)

    return TwoPeakViewerHandler


def _optional_float(value: Any) -> float | None:
    """把前端传来的可选数字转换成 float。

    空字符串/None 表示用户不想在扫描结束后自动恢复 AO 电压。
    """

    if value in (None, ""):
        return None
    return float(value)


def _write_single_ao_for_test(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """单路 AO 诊断写入。

    这个接口故意和 AO 扫描一样，只写一个 AO 通道。
    用途是把“底层 AO 是否能重复写入”和“双路锁定逻辑是否有问题”分开排查。
    """

    channel = str(body.get("channel", "ao0")).strip()
    value = float(body.get("value", 0.0))
    min_val = float(body.get("min_val", -10.0))
    max_val = float(body.get("max_val", 10.0))
    if not channel:
        raise ValueError("AO channel is empty")
    if min_val >= max_val:
        raise ValueError("min_val must be smaller than max_val")
    if value < min_val or value > max_val:
        raise ValueError(f"value {value} is outside [{min_val}, {max_val}]")

    result = state.daq.write_ao(
        channel=channel,
        value=value,
        min_val=min_val,
        max_val=max_val,
    )
    return {
        "ok": True,
        "channel": channel,
        "value": value,
        "min_val": min_val,
        "max_val": max_val,
        "result": result,
    }


def _write_power_lock_initial_ao(state: ViewerState, body: dict[str, Any]) -> dict[str, Any]:
    """把双路功率锁定的初始 AO 电压写到硬件。

    这里只负责“手动写初值”，不启动 PID。
    这样做是为了先确认 AOM/EOM 的安全电压范围和工作点，避免一上来闭环。
    """

    controllers = body.get("controllers", [])
    if not isinstance(controllers, list) or not controllers:
        raise ValueError("controllers must be a non-empty list")

    written: list[dict[str, Any]] = []
    for controller in controllers:
        if not isinstance(controller, dict):
            raise ValueError("each controller must be an object")
        if controller.get("enabled", True) is False:
            continue

        name = str(controller.get("name", ""))
        channel = str(controller.get("channel", "")).strip()
        initial_voltage = float(controller.get("initial_voltage"))
        min_voltage = float(controller.get("min_voltage"))
        max_voltage = float(controller.get("max_voltage"))

        if not channel:
            raise ValueError(f"{name or 'controller'} channel is empty")
        if min_voltage >= max_voltage:
            raise ValueError(f"{name or channel} min_voltage must be smaller than max_voltage")
        if initial_voltage < min_voltage or initial_voltage > max_voltage:
            raise ValueError(
                f"{name or channel} initial_voltage {initial_voltage} is outside "
                f"[{min_voltage}, {max_voltage}]"
            )

        result = state.daq.write_ao(
            channel=channel,
            value=initial_voltage,
            min_val=min_voltage,
            max_val=max_voltage,
        )
        written.append(
            {
                "name": name,
                "channel": channel,
                "initial_voltage": initial_voltage,
                "min_voltage": min_voltage,
                "max_voltage": max_voltage,
                "result": result,
            }
        )

    if not written:
        raise ValueError("no enabled controller to write")
    return {"ok": True, "written": written}
