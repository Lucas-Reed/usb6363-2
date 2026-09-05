"""双峰锁定最小波形查看器入口。

运行方式：
    1. 先启动底层 USB-6363 API：
       python usb6363_server.py

    2. 再启动本查看器：
       python two_peak_viewer.py

    3. 浏览器打开：
       http://127.0.0.1:8766

重要边界：
    本文件不直接 import nidaqmx。
    它只启动查看器 HTTP 服务；采集卡访问通过 usb6363_client 走底层 API。
"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from two_peak.viewer_server import make_handler
from two_peak.viewer_state import ViewerState
from usb6363_client import DEFAULT_BASE_URL


DEFAULT_VIEWER_HOST = "127.0.0.1"
DEFAULT_VIEWER_PORT = 8766
DEFAULT_SAMPLE_DIR = Path("data") / "two_peak_samples"


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="Run the two-peak waveform viewer.")
    parser.add_argument("--host", default=DEFAULT_VIEWER_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_VIEWER_PORT)
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    args = parser.parse_args()

    state = ViewerState(
        api_base_url=args.api_base_url,
        sample_dir=Path(args.sample_dir),
    )
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Two-peak waveform viewer running at http://{args.host}:{args.port}")
    print(f"Using USB-6363 API at {args.api_base_url}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
