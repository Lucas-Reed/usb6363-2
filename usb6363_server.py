"""USB-6363 本地 HTTP API 服务。

先启动这个服务：
    python usb6363_server.py

以后其他 Python 子程序不要直接 import nidaqmx，而是访问：
    http://127.0.0.1:8765/api/...

这样做的好处是：整台电脑上只有这个服务进程直接碰 USB-6363。
"""

from __future__ import annotations

import argparse
import io
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from usb6363_core import DaqController, DEVICE_NAME


# 127.0.0.1 表示只允许本机访问，不开放给局域网其他电脑。
DEFAULT_HOST = "127.0.0.1"

# 本地服务端口。只要没有被其他程序占用，就可以固定用 8765。
DEFAULT_PORT = 8765


def make_handler(controller: DaqController):
    """创建 HTTP 请求处理类。

    Python 标准库的 HTTPServer 要求传入一个“类”，所以这里用函数包一层，
    把已经创建好的 controller 传进去。这样所有请求都会共用同一个硬件控制器。
    """

    class Usb6363RequestHandler(BaseHTTPRequestHandler):
        server_version = "Usb6363Server/0.1"

        def do_GET(self) -> None:
            """处理 GET 请求。

            GET 适合“读取/查询”动作，例如读取 AI、查询设备信息。
            """

            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)

                if parsed.path == "/health":
                    # 健康检查：用于确认 server 是否活着。
                    self._send_json({"ok": True, "device": controller.device_name})
                elif parsed.path == "/api/devices":
                    # 列出 NI-DAQmx 当前能看到的所有设备。
                    devices = [device.__dict__ for device in controller.list_devices()]
                    self._send_json({"devices": devices})
                elif parsed.path == "/api/device":
                    # 查询当前目标设备 Dev2 的信息。
                    self._send_json(controller.get_device_info().__dict__)
                elif parsed.path == "/api/terminals":
                    # 查询 PFI、数字线、计数器等端子列表。
                    self._send_json(controller.list_signal_terminals())
                # LEGACY AI routes: 下面这些 /api/ai/read/status/latest/buffer/stats
                # 属于旧后台 AI 订阅/直接读取模型。新程序优先使用 /api/ai/unified/*。
                elif parsed.path == "/api/ai/read":
                    # 读取模拟输入。URL 参数会被 _ai_args 转成 Python 参数。
                    self._send_json(controller.read_ai_voltage(**_ai_args(query)))
                elif parsed.path == "/api/ai/status":
                    # 查询后台连续 AI 采样状态。
                    self._send_json(controller.get_ai_sampling_status())
                elif parsed.path == "/api/ai/latest":
                    # 读取某个已订阅 AI 通道的最近一个采样值。
                    self._send_json(controller.get_ai_latest(**_ai_latest_args(query)))
                elif parsed.path == "/api/ai/buffer":
                    # 读取某个已订阅 AI 通道最近的一段缓存数据。
                    self._send_json(controller.get_ai_buffer(**_ai_buffer_args(query)))
                elif parsed.path == "/api/ai/stats":
                    # 返回某个通道最近缓存数据的统计量，用于实时监测反馈。
                    self._send_json(controller.get_ai_stats(**_ai_stats_args(query)))
                # RECOMMENDED AI routes: 统一 AI 流。双峰、功率慢漂、示波器等应共享这里。
                elif parsed.path == "/api/ai/unified/status":
                    # 查询统一 AI 数据流状态。
                    self._send_json(controller.get_unified_ai_stream_status())
                elif parsed.path == "/api/ai/unified/latest_frame":
                    # 读取统一 AI 数据流的最新一帧完整波形。
                    self._send_json(controller.get_unified_ai_stream_latest_frame())
                elif parsed.path == "/api/ai/unified/frame_batch":
                    # 按 frame_id 批量补取历史帧。波形较大，因此返回 NPZ 而不是 JSON。
                    batch = controller.get_unified_ai_frame_batch(
                        **_ai_frame_batch_args(query)
                    )
                    self._send_npz(batch)
                elif parsed.path == "/api/ai/unified/latest":
                    # 读取统一 AI 数据流中某个通道的最近一个点。
                    self._send_json(controller.get_unified_ai_latest(**_ai_latest_args(query)))
                elif parsed.path == "/api/ai/unified/buffer":
                    # 读取统一 AI 数据流中某个通道最近的一段缓存。
                    self._send_json(controller.get_unified_ai_buffer(**_ai_buffer_args(query)))
                elif parsed.path == "/api/ai/unified/stats":
                    # 读取统一 AI 数据流中某个通道最近缓存的统计量。
                    self._send_json(controller.get_unified_ai_stats(**_ai_stats_args(query)))
                # LEGACY frame_stream routes: 旧双峰连续采集模型，仅保留兼容。
                elif parsed.path == "/api/ai/frame_stream/status":
                    # 查询固定点数分帧连续采集状态。
                    self._send_json(controller.get_ai_frame_stream_status())
                elif parsed.path == "/api/ai/frame_stream/latest":
                    # 读取固定点数分帧连续采集的最新一帧。
                    self._send_json(controller.get_ai_frame_stream_latest())
                elif parsed.path == "/api/pfi/read":
                    # 读取某个 PFI 或数字线的当前高低电平。
                    self._send_json(controller.read_digital_line(**_digital_read_args(query)))
                elif parsed.path == "/api/pfi/count":
                    # 在指定时间内，统计某个 PFI 的上升沿/下降沿数量。
                    self._send_json(controller.count_pfi_edges(**_pfi_count_args(query)))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                # 出错时返回 JSON，而不是让 server 直接崩掉。
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            """处理 POST 请求。

            POST 适合“会改变状态”的动作，例如写 AO 输出电压。
            """

            try:
                parsed = urlparse(self.path)
                body = self._read_json()

                if parsed.path == "/api/ao/write":
                    # 写模拟输出。请求体 body 是 JSON，例如 {"channel": "ao0", "value": 1.23}。
                    self._send_json(controller.write_ao_voltage(**_ao_args(body)))
                # LEGACY AI routes: 旧后台 AI 订阅/记录/同步采帧模型，仅保留兼容和调试。
                elif parsed.path == "/api/ai/subscribe":
                    # 订阅一个 AI 通道，后台会自动按通道数重算采样率。
                    self._send_json(controller.subscribe_ai_channel(**_ai_channel_body(body)))
                elif parsed.path == "/api/ai/unsubscribe":
                    # 取消订阅一个 AI 通道。
                    self._send_json(controller.unsubscribe_ai_channel(**_ai_channel_body(body)))
                elif parsed.path == "/api/ai/set_channels":
                    # 一次性设置活跃 AI 通道列表，相当于暂停其他未列出的通道。
                    self._send_json(controller.set_ai_channels(**_ai_channels_body(body)))
                elif parsed.path == "/api/ai/clear":
                    # 清空所有 AI 通道并停止后台连续采样。
                    self._send_json(controller.clear_ai_channels())
                elif parsed.path == "/api/ai/record_to_file":
                    # 把当前后台 AI 采样流接下来的一段数据保存成 .npy 文件。
                    self._send_json(controller.record_ai_to_file(**_ai_record_body(body)))
                elif parsed.path == "/api/ai/capture_frame":
                    # 同步读取多路 AI 的一帧数据，例如双峰锁定里的 ai0/ai1。
                    self._send_json(controller.capture_ai_frame(**_ai_capture_frame_body(body)))
                # RECOMMENDED AI routes: 只有统一 AI 控制台等底层控制入口应该启动/停止统一流。
                elif parsed.path == "/api/ai/unified/start":
                    # 启动统一 AI 数据流。
                    self._send_json(controller.start_unified_ai_stream(**_ai_frame_stream_body(body)))
                elif parsed.path == "/api/ai/unified/stop":
                    # 停止统一 AI 数据流。
                    self._send_json(controller.stop_unified_ai_stream())
                # LEGACY frame_stream routes: 旧双峰连续采集模型，仅保留兼容。
                elif parsed.path == "/api/ai/frame_stream/start":
                    # 启动固定点数分帧连续采集。
                    self._send_json(controller.start_ai_frame_stream(**_ai_frame_stream_body(body)))
                elif parsed.path == "/api/ai/frame_stream/stop":
                    # 停止固定点数分帧连续采集。
                    self._send_json(controller.stop_ai_frame_stream())
                elif parsed.path == "/api/pfi/write":
                    # 写 PFI 或数字线电平。请求体例如 {"line": "PFI0", "value": true}。
                    self._send_json(controller.write_digital_line(**_digital_write_args(body)))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            """忽略正常请求日志，避免高速状态查询形成巨型日志文件。

            双峰页面会高频请求 status 和 frame_batch。过去这些成功请求全部同步写到
            stdout，长期运行会产生 GB 级日志并增加磁盘停顿。硬件错误仍保存在统一流
            状态的 error 字段中，因此关闭访问日志不会隐藏真正的采集故障。
            """

            return

        def _read_json(self) -> dict[str, Any]:
            """读取 POST 请求里的 JSON 数据。"""

            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)

        def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            """把 Python dict 转成 JSON 响应发给调用方。"""

            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_npz(self, arrays: dict[str, Any]) -> None:
            """把一批 NumPy 数组编码成未压缩 NPZ 二进制响应。

            二进制接口只用于本机程序间传递波形，避免把大量浮点数展开成巨大 JSON。
            """

            buffer = io.BytesIO()
            np.savez(buffer, **arrays)
            payload = buffer.getvalue()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-npz")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            """用统一格式返回错误信息。"""

            self._send_json({"ok": False, "error": message}, status=status)

    return Usb6363RequestHandler


def _first(query: dict[str, list[str]], name: str, default: Any) -> Any:
    """从 URL 查询参数里取第一个值。

    parse_qs 会把 ?samples=1 解析成 {"samples": ["1"]}，
    所以这里取列表里的第一个元素。
    """

    values = query.get(name)
    if not values:
        return default
    return values[0]


def _ai_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 HTTP 的字符串参数转换成 read_ai_voltage 需要的参数类型。"""

    return {
        "channel": str(_first(query, "channel", "ai0")),
        "samples": int(_first(query, "samples", 1)),
        "rate": float(_first(query, "rate", 1000.0)),
        "terminal_config": str(_first(query, "terminal_config", "RSE")),
        "min_val": float(_first(query, "min_val", -10.0)),
        "max_val": float(_first(query, "max_val", 10.0)),
        "timeout": float(_first(query, "timeout", 10.0)),
    }


def _ai_latest_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 URL 参数转换成 get_ai_latest 需要的参数类型。"""

    return {
        "channel": str(_first(query, "channel", "ai0")),
    }


def _ai_buffer_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 URL 参数转换成 get_ai_buffer 需要的参数类型。"""

    return {
        "channel": str(_first(query, "channel", "ai0")),
        "max_samples": int(_first(query, "max_samples", 1000)),
    }


def _ai_stats_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 URL 参数转换成 get_ai_stats 需要的参数类型。"""

    return {
        "channel": str(_first(query, "channel", "ai0")),
        "max_samples": int(_first(query, "max_samples", 10000)),
    }


def _ai_frame_batch_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """解析统一流历史帧接口的查询参数。"""

    raw_channels = str(_first(query, "channels", "")).strip()
    channels = [item.strip() for item in raw_channels.split(",") if item.strip()]
    if not channels:
        raise ValueError("channels must contain at least one AI channel")
    return {
        "after_frame_id": int(_first(query, "after_frame_id", 0)),
        "channels": channels,
        "max_frames": int(_first(query, "max_frames", 100)),
    }


def _ai_channel_body(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成单通道订阅/取消订阅需要的参数。"""

    return {
        "channel": str(body.get("channel", "ai0")),
    }


def _ai_channels_body(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 set_ai_channels 需要的通道列表。"""

    channels = body.get("channels", [])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")
    return {
        "channels": [str(channel) for channel in channels],
    }


def _ai_record_body(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 record_ai_to_file 需要的参数。"""

    return {
        "seconds": float(body["seconds"]),
        "output_dir": str(body.get("output_dir", "data")),
        "prefix": str(body.get("prefix", "ai_capture")),
        "timeout": (
            None
            if body.get("timeout") is None
            else float(body.get("timeout"))
        ),
    }


def _ai_capture_frame_body(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 capture_ai_frame 需要的参数。"""

    channels = body.get("channels", ["ai0", "ai1"])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")

    return {
        "channels": [str(channel) for channel in channels],
        "samples": int(body.get("samples", 5000)),
        "rate": float(body.get("rate", 50_000.0)),
        "terminal_config": str(body.get("terminal_config", "DIFF")),
        "min_val": float(body.get("min_val", -5.0)),
        "max_val": float(body.get("max_val", 5.0)),
        "timeout": float(body.get("timeout", 10.0)),
        "trigger_enabled": _bool_value(body.get("trigger_enabled", False)),
        "trigger_source": str(body.get("trigger_source", "PFI0")),
        "trigger_edge": str(body.get("trigger_edge", "RISING")),
    }


def _ai_frame_stream_body(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 start_ai_frame_stream 需要的参数。"""

    channels = body.get("channels", ["ai0", "ai1"])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")

    return {
        "channels": [str(channel) for channel in channels],
        "samples_per_frame": int(body.get("samples_per_frame", body.get("samples", 5000))),
        "rate": float(body.get("rate", 50_000.0)),
        "terminal_config": str(body.get("terminal_config", "DIFF")),
        "min_val": float(body.get("min_val", -5.0)),
        "max_val": float(body.get("max_val", 5.0)),
        "timeout": float(body.get("timeout", 10.0)),
        "trigger_enabled": _bool_value(body.get("trigger_enabled", False)),
        "trigger_source": str(body.get("trigger_source", "PFI0")),
        "trigger_edge": str(body.get("trigger_edge", "RISING")),
        "resync_every_frames": int(body.get("resync_every_frames", 0)),
    }


def _ao_args(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 write_ao_voltage 需要的参数类型。"""

    return {
        "channel": str(body.get("channel", "ao0")),
        "value": float(body["value"]),
        "min_val": float(body.get("min_val", -10.0)),
        "max_val": float(body.get("max_val", 10.0)),
        "timeout": float(body.get("timeout", 10.0)),
    }


def _digital_read_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 URL 参数转换成 read_digital_line 需要的参数类型。"""

    return {
        "line": str(_first(query, "line", "PFI0")),
        "timeout": float(_first(query, "timeout", 10.0)),
    }


def _digital_write_args(body: dict[str, Any]) -> dict[str, Any]:
    """把 POST JSON 转换成 write_digital_line 需要的参数类型。"""

    return {
        "line": str(body.get("line", "PFI0")),
        "value": _bool_value(body.get("value", False)),
        "timeout": float(body.get("timeout", 10.0)),
    }


def _pfi_count_args(query: dict[str, list[str]]) -> dict[str, Any]:
    """把 URL 参数转换成 count_pfi_edges 需要的参数类型。"""

    return {
        "line": str(_first(query, "line", "PFI0")),
        "counter": str(_first(query, "counter", "ctr0")),
        "seconds": float(_first(query, "seconds", 1.0)),
        "edge": str(_first(query, "edge", "RISING")),
        "timeout": float(_first(query, "timeout", 10.0)),
    }


def _bool_value(value: Any) -> bool:
    """把 JSON 里的布尔值安全转换成 Python bool。

    这样 true/false、"true"/"false"、1/0 都能按直觉工作。
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on", "high"):
            return True
        if normalized in ("false", "0", "no", "off", "low"):
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def main() -> int:
    # argparse 让你可以在命令行改端口或设备名，例如：
    #   python usb6363_server.py --port 9000 --device Dev2
    parser = argparse.ArgumentParser(description="Run the USB-6363 local API server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", default=DEVICE_NAME)
    args = parser.parse_args()

    # 这个 controller 是整个服务进程里唯一直接访问 USB-6363 的对象。
    controller = DaqController(device_name=args.device)
    handler = make_handler(controller)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"USB-6363 API server running at http://{args.host}:{args.port}")
    print(f"Target device: {args.device}")
    print("Device info will be read when an API route actually needs hardware.")
    print("Press Ctrl+C to stop.")

    try:
        # serve_forever 会一直阻塞运行，直到按 Ctrl+C。
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
