"""统一 AI 采集流控制台。

这个小 WebUI 的定位是“总开关”：
1. 它不直接 import nidaqmx，也不直接接触 USB-6363。
2. 它只调用 usb6363_server.py 暴露出来的统一 AI 流 API。
3. 以后双峰查看器、功率慢漂、示波器等上层程序，都可以共用同一个统一 AI 流。

启动示例：
    python ai_stream_console.py

然后在浏览器打开：
    http://127.0.0.1:8768
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from usb6363_client import Usb6363Client


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>统一 AI 采集控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --line: #d7e0ea;
      --text: #182230;
      --muted: #667085;
      --blue: #1d6fdc;
      --red: #b42318;
      --green: #067647;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 14px;
      min-height: 100vh;
      padding: 14px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 20px;
      letter-spacing: 0;
    }
    h2 {
      margin: 18px 0 10px;
      font-size: 15px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    label {
      display: block;
      margin: 10px 0 4px;
      color: var(--muted);
      font-size: 13px;
    }
    input, select {
      width: 100%;
      height: 34px;
      border: 1px solid #c9d4e2;
      border-radius: 6px;
      padding: 6px 8px;
      font: inherit;
      background: #fff;
    }
    .grid2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
    }
    .row input[type="checkbox"] {
      width: 18px;
      height: 18px;
      padding: 0;
    }
    button {
      height: 36px;
      border: 1px solid #c9d4e2;
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }
    button.primary {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }
    button.danger {
      background: #fff;
      color: var(--red);
      border-color: #f0b8b3;
    }
    button:disabled {
      opacity: 0.55;
      cursor: default;
    }
    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 16px;
    }
    .status-grid {
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 8px 12px;
      align-items: start;
      font-size: 14px;
    }
    .key {
      color: var(--muted);
    }
    .ok { color: var(--green); font-weight: 600; }
    .bad { color: var(--red); font-weight: 600; }
    pre {
      margin: 12px 0 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfdff;
      overflow: auto;
      max-height: 48vh;
      font-size: 12px;
      line-height: 1.45;
    }
    .hint {
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      .status-grid { grid-template-columns: 130px 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <section>
      <h1>统一 AI 采集控制台</h1>

      <label for="channels">AI 通道，用英文逗号分隔</label>
      <input id="channels" value="ai0,ai1,ai2" />

      <div class="grid2">
        <div>
          <label for="rate">每通道采样率 / Hz</label>
          <input id="rate" type="number" value="100000" min="1" step="1" />
        </div>
        <div>
          <label for="samples">每帧每通道点数</label>
          <input id="samples" type="number" value="10000" min="1" step="1" />
        </div>
      </div>

      <div class="grid2">
        <div>
          <label for="terminal_config">输入方式</label>
          <select id="terminal_config">
            <option value="DIFF">DIFF</option>
            <option value="RSE">RSE</option>
            <option value="NRSE">NRSE</option>
          </select>
        </div>
        <div>
          <label for="timeout">超时 / s</label>
          <input id="timeout" type="number" value="10" min="0.1" step="0.1" />
        </div>
      </div>

      <div class="grid2">
        <div>
          <label for="min_val">AI 最小电压 / V</label>
          <input id="min_val" type="number" value="-5" step="0.1" />
        </div>
        <div>
          <label for="max_val">AI 最大电压 / V</label>
          <input id="max_val" type="number" value="5" step="0.1" />
        </div>
      </div>

      <h2>PFI 触发</h2>
      <div class="row">
        <input id="trigger_enabled" type="checkbox" />
        <label for="trigger_enabled" style="margin:0;color:var(--text)">等待 PFI 边沿后开始</label>
      </div>

      <div class="grid2">
        <div>
          <label for="trigger_source">触发源</label>
          <input id="trigger_source" value="PFI0" />
        </div>
        <div>
          <label for="trigger_edge">触发边沿</label>
          <select id="trigger_edge">
            <option value="RISING">RISING</option>
            <option value="FALLING">FALLING</option>
          </select>
        </div>
      </div>

      <label for="resync_every_frames">每隔 N 帧重新等待触发，0 表示关闭</label>
      <input id="resync_every_frames" type="number" value="0" min="0" step="1" />

      <div class="actions">
        <button class="primary" id="startBtn" onclick="startStream()">启动统一流</button>
        <button class="danger" id="stopBtn" onclick="stopStream()">停止统一流</button>
      </div>

      <p class="hint">
        这个页面只负责启动和停止统一 AI 流。双峰查看器、功率慢漂等程序之后只读取这个流，不再自己抢 AI 任务。
      </p>
    </section>

    <section>
      <h1>运行状态</h1>
      <div class="status-grid">
        <div class="key">running</div><div id="running">--</div>
        <div class="key">frame_id</div><div id="frame_id">--</div>
        <div class="key">has_frame</div><div id="has_frame">--</div>
        <div class="key">frame_rate_hz</div><div id="frame_rate_hz">--</div>
        <div class="key">frame_duration_ms</div><div id="frame_duration_ms">--</div>
        <div class="key">channels</div><div id="status_channels">--</div>
        <div class="key">sample_counts</div><div id="sample_counts">--</div>
        <div class="key">error</div><div id="error">--</div>
      </div>
      <pre id="rawStatus">{}</pre>
    </section>
  </main>

  <script>
    function numberValue(id) {
      return Number(document.getElementById(id).value);
    }

    function settingsFromForm() {
      const channels = document.getElementById("channels").value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      return {
        channels,
        rate: numberValue("rate"),
        samples_per_frame: numberValue("samples"),
        terminal_config: document.getElementById("terminal_config").value,
        min_val: numberValue("min_val"),
        max_val: numberValue("max_val"),
        timeout: numberValue("timeout"),
        trigger_enabled: document.getElementById("trigger_enabled").checked,
        trigger_source: document.getElementById("trigger_source").value.trim(),
        trigger_edge: document.getElementById("trigger_edge").value,
        resync_every_frames: numberValue("resync_every_frames"),
      };
    }

    async function requestJson(url, options) {
      const response = await fetch(url, options);
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || response.statusText);
      }
      return data;
    }

    async function startStream() {
      try {
        await requestJson("/api/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(settingsFromForm()),
        });
        await refreshStatus();
      } catch (err) {
        alert(err.message || String(err));
      }
    }

    async function stopStream() {
      try {
        await requestJson("/api/stop", { method: "POST" });
        await refreshStatus();
      } catch (err) {
        alert(err.message || String(err));
      }
    }

    function setText(id, value, className) {
      const node = document.getElementById(id);
      node.textContent = value;
      node.className = className || "";
    }

    async function refreshStatus() {
      try {
        const data = await requestJson("/api/status");
        const status = data.status || data;
        const running = Boolean(status.running);
        setText("running", running ? "true" : "false", running ? "ok" : "");
        setText("frame_id", status.frame_id ?? "--");
        setText("has_frame", status.has_frame ? "true" : "false");
        setText("frame_rate_hz", status.frame_rate_hz ?? "--");
        setText("frame_duration_ms", status.frame_duration_ms ?? "--");
        setText("status_channels", (status.channels || []).join(", ") || "--");
        setText("sample_counts", JSON.stringify(status.sample_counts || {}));
        setText("error", status.error || "--", status.error ? "bad" : "");
        document.getElementById("rawStatus").textContent = JSON.stringify(status, null, 2);
      } catch (err) {
        setText("running", "unknown", "bad");
        setText("error", err.message || String(err), "bad");
      }
    }

    refreshStatus();
    setInterval(refreshStatus, 1000);
  </script>
</body>
</html>
"""


def _json_response(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    """把 Python 字典作为 JSON 发给浏览器。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """读取 POST 请求中的 JSON；空请求体按空字典处理。"""

    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _start_body_to_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """把浏览器传来的 JSON 转换成 client 启动统一流所需的参数。"""

    channels = body.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("channels must be a non-empty list")

    return {
        "channels": [str(channel).strip() for channel in channels if str(channel).strip()],
        "samples_per_frame": int(body.get("samples_per_frame", 10000)),
        "rate": float(body.get("rate", 100000.0)),
        "terminal_config": str(body.get("terminal_config", "DIFF")),
        "min_val": float(body.get("min_val", -5.0)),
        "max_val": float(body.get("max_val", 5.0)),
        "timeout": float(body.get("timeout", 10.0)),
        "trigger_enabled": bool(body.get("trigger_enabled", False)),
        "trigger_source": str(body.get("trigger_source", "PFI0")),
        "trigger_edge": str(body.get("trigger_edge", "RISING")),
        "resync_every_frames": int(body.get("resync_every_frames", 0)),
    }


def make_handler(daq: Usb6363Client) -> type[BaseHTTPRequestHandler]:
    """创建 HTTP handler 类，并把 Usb6363Client 绑定进去。"""

    class AiStreamConsoleHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # 保持终端输出简洁：只在出现异常时由代码显式打印。
            return

        def do_GET(self) -> None:  # noqa: N802 - http.server 固定方法名
            if self.path == "/":
                body = HTML_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/api/status":
                try:
                    _json_response(self, {"ok": True, "status": daq.get_unified_ai_stream_status()})
                except Exception as exc:  # noqa: BLE001 - WebUI 需要把错误显示出来
                    _json_response(self, {"ok": False, "error": str(exc)}, 500)
                return

            _json_response(self, {"ok": False, "error": "Unknown route"}, 404)

        def do_POST(self) -> None:  # noqa: N802 - http.server 固定方法名
            try:
                if self.path == "/api/start":
                    kwargs = _start_body_to_kwargs(_read_json_body(self))
                    status = daq.start_unified_ai_stream(**kwargs)
                    _json_response(self, {"ok": True, "status": status})
                    return

                if self.path == "/api/stop":
                    status = daq.stop_unified_ai_stream()
                    _json_response(self, {"ok": True, "status": status})
                    return

                _json_response(self, {"ok": False, "error": "Unknown route"}, 404)
            except Exception as exc:  # noqa: BLE001 - WebUI 需要把错误显示出来
                _json_response(self, {"ok": False, "error": str(exc)}, 500)

    return AiStreamConsoleHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="USB-6363 统一 AI 采集流控制台")
    parser.add_argument("--host", default="127.0.0.1", help="WebUI 监听地址")
    parser.add_argument("--port", type=int, default=8768, help="WebUI 监听端口")
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8765",
        help="usb6363_server.py 的 API 地址",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="调用底层 API 的超时时间 / s")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    daq = Usb6363Client(base_url=args.api_base_url, timeout=args.timeout)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(daq))

    print("Unified AI stream console")
    print(f"  WebUI: {args.host}:{args.port}")
    print(f"  API:   {args.api_base_url}")
    print("  Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
