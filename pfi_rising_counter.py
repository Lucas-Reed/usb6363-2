"""PFI 上升沿最小计数器。

用途：
    把信号源的同步输出接到 USB-6363 的某个 PFI 端子后，
    用这个脚本检查 NI-DAQmx 是否能看到上升沿。

运行前请先启动底层服务：
    python usb6363_server.py

常用运行方式：
    python pfi_rising_counter.py
    python pfi_rising_counter.py --line PFI1 --counter ctr1 --seconds 0.5

重要边界：
    本脚本不直接 import nidaqmx。
    它只通过 usb6363_client.Usb6363Client 调用底层 API。
"""

from __future__ import annotations

import argparse
import time

from usb6363_client import DEFAULT_BASE_URL, Usb6363Client


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="Count rising edges on a USB-6363 PFI terminal.")
    parser.add_argument(
        "--line",
        default="PFI0",
        help="要计数的 PFI 端子，例如 PFI0、PFI1、/Dev2/PFI0。",
    )
    parser.add_argument(
        "--counter",
        default="ctr0",
        help="用于计数的计数器，例如 ctr0、ctr1。不同任务不要同时抢同一个 ctr。",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=1.0,
        help="每次统计的时间窗口，单位秒。",
    )
    parser.add_argument(
        "--edge",
        default="RISING",
        choices=["RISING", "FALLING"],
        help="计数上升沿还是下降沿。默认 RISING。",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=0,
        help="重复次数。0 表示一直重复，按 Ctrl+C 停止。",
    )
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_BASE_URL,
        help="底层 USB-6363 API 地址，默认 http://127.0.0.1:8765。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP/API 超时时间，单位秒。",
    )
    args = parser.parse_args()

    if args.seconds <= 0:
        raise ValueError("--seconds must be > 0")

    daq = Usb6363Client(base_url=args.api_base_url, timeout=args.timeout)

    print("PFI edge counter")
    print(f"  API:     {args.api_base_url}")
    print(f"  line:    {args.line}")
    print(f"  counter: {args.counter}")
    print(f"  edge:    {args.edge}")
    print(f"  window:  {args.seconds} s")
    print("  Press Ctrl+C to stop.")
    print()

    index = 0
    try:
        while args.repeat == 0 or index < args.repeat:
            index += 1
            started = time.time()
            result = daq.count_pfi_edges(
                line=args.line,
                counter=args.counter,
                seconds=args.seconds,
                edge=args.edge,
                timeout=max(args.timeout, args.seconds + 2.0),
            )
            elapsed = time.time() - started
            count = int(result["count"])
            frequency = count / float(args.seconds)
            print(
                f"[{index:04d}] count={count:8d}  "
                f"approx_freq={frequency:12.3f} Hz  "
                f"elapsed={elapsed:.3f} s"
            )
    except KeyboardInterrupt:
        print("\nStopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
