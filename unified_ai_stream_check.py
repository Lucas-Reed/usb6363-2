"""统一 AI 采集流最小检查脚本。

这个脚本只通过 usb6363_client.Usb6363Client 调用 8765 底层 API，
不 import nidaqmx，也不直接接触 USB-6363。

典型用法：
    python unified_ai_stream_check.py --channels ai0,ai1,ai2 --rate 100000 --samples 10000

脚本会：
1. 启动 unified_ai_stream。
2. 等待至少一帧。
3. 读取 latest_frame。
4. 读取指定通道 latest。
5. 读取指定通道 stats。
6. 停止 unified_ai_stream。
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from usb6363_client import DEFAULT_BASE_URL
from usb6363_client import Usb6363Client


def parse_channels(text: str) -> list[str]:
    """把命令行里的 ai0,ai1,ai2 转成通道列表。"""

    channels = [item.strip() for item in text.split(",") if item.strip()]
    if not channels:
        raise ValueError("--channels must not be empty")
    return channels


def wait_for_frame(
    daq: Usb6363Client,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    """等待 unified_ai_stream 产生第一帧。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = daq.get_unified_ai_stream_status()
        if status.get("has_frame"):
            return status
        if status.get("error"):
            raise RuntimeError(status["error"])
        time.sleep(poll_interval)
    raise TimeoutError("Timed out while waiting for unified AI frame")


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="Check USB-6363 unified AI stream.")
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--channels", default="ai0,ai1,ai2")
    parser.add_argument("--rate", type=float, default=100_000.0)
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--terminal-config", default="DIFF", choices=["RSE", "DIFF", "NRSE"])
    parser.add_argument("--min-val", type=float, default=-5.0)
    parser.add_argument("--max-val", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--trigger-enabled", action="store_true")
    parser.add_argument("--trigger-source", default="PFI0")
    parser.add_argument("--trigger-edge", default="RISING", choices=["RISING", "FALLING"])
    parser.add_argument("--resync-every-frames", type=int, default=0)
    parser.add_argument("--read-channel", default=None)
    parser.add_argument("--stats-samples", type=int, default=10_000)
    parser.add_argument("--wait-timeout", type=float, default=15.0)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    args = parser.parse_args()

    channels = parse_channels(args.channels)
    read_channel = args.read_channel or channels[0]
    daq = Usb6363Client(base_url=args.api_base_url, timeout=max(args.timeout + 5.0, 10.0))

    print("Unified AI stream check")
    print(f"  API:      {args.api_base_url}")
    print(f"  channels: {channels}")
    print(f"  rate:     {args.rate:g} Hz/ch")
    print(f"  samples:  {args.samples} samples/ch/frame")
    print(f"  read:     {read_channel}")
    print()

    started = False
    try:
        status = daq.start_unified_ai_stream(
            channels=channels,
            samples_per_frame=args.samples,
            rate=args.rate,
            terminal_config=args.terminal_config,
            min_val=args.min_val,
            max_val=args.max_val,
            timeout=args.timeout,
            trigger_enabled=args.trigger_enabled,
            trigger_source=args.trigger_source,
            trigger_edge=args.trigger_edge,
            resync_every_frames=args.resync_every_frames,
        )
        started = True
        print(f"started: running={status.get('running')} frame_id={status.get('frame_id')}")

        status = wait_for_frame(
            daq=daq,
            timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
        )
        print(f"first frame: frame_id={status.get('frame_id')} has_frame={status.get('has_frame')}")

        frame = daq.get_unified_ai_stream_latest_frame()
        print(
            "latest_frame: "
            f"channels={frame.get('channels')} "
            f"samples/ch={frame.get('samples_per_channel')} "
            f"frame_id={frame.get('frame_id')}"
        )

        latest = daq.get_unified_ai_latest(read_channel)
        print(
            "latest: "
            f"channel={latest.get('channel')} "
            f"value={float(latest.get('value')):.8e} "
            f"sample_count={latest.get('sample_count')}"
        )

        stats = daq.get_unified_ai_stats(read_channel, max_samples=args.stats_samples)
        print(
            "stats: "
            f"channel={stats.get('channel')} "
            f"samples={stats.get('samples')} "
            f"mean={float(stats.get('mean')):.8e} "
            f"std={float(stats.get('std')):.8e} "
            f"rms={float(stats.get('rms')):.8e}"
        )

    finally:
        if started:
            status = daq.stop_unified_ai_stream()
            print(f"stopped: running={status.get('running')} frame_id={status.get('frame_id')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
