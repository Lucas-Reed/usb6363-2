"""光电探测器总功率慢漂监测。

这个文件是一个独立的小模块，用来做一件事：
    通过 USB-6363 的某一路 AI，慢速记录光电探测器输出电压的长期漂移。

重要边界：
- 本文件不 import nidaqmx，不直接碰采集卡。
- 本文件只通过 usb6363_client.Usb6363Client 调用底层 8765 API。
- 默认要求双峰 frame_stream 和后台 AI 采样都没有运行，避免多个实验互相抢 AI 硬件。

典型用法：
    python power_drift_monitor.py --channel ai2 --interval 1 --samples 1000 --rate 1000

含义：
- 每 1 秒记录一次。
- 每次从 ai2 读取 1000 个点，采样率 1000 Hz，所以这一行 CSV 是 1 秒内的平均功率信号。
- CSV 会保存在 data/power_drift/ 目录下。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from usb6363_client import DEFAULT_BASE_URL
from usb6363_client import Usb6363Client


DEFAULT_OUTPUT_DIR = Path("data") / "power_drift"


@dataclass
class PowerDriftSettings:
    """一次功率慢漂记录的参数。

    channel:
        光电探测器接到的 AI 通道，例如 ai2。
    interval:
        CSV 两行之间的时间间隔，单位是秒。
    samples:
        每次读取多少个点。点数越多，均值越稳，但每行采集耗时也越长。
    rate:
        每次小段采集的采样率，单位是 Hz。
    terminal_config:
        接线方式。差分接线通常用 DIFF，单端接线常用 RSE。
    power_per_volt:
        可选的电压到功率换算系数。未知时保持 1.0 即可，CSV 里仍然会保留电压值。
    zero_voltage:
        可选的零功率本底电压。功率估计会用 (mean_v - zero_voltage) * power_per_volt。
    """

    channel: str
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
    power_per_volt: float
    zero_voltage: float
    allow_busy_ai: bool


@dataclass
class PowerDriftPoint:
    """CSV 中的一行功率慢漂数据。"""

    iso_time: str
    unix_time: float
    elapsed_s: float
    index: int
    session_id: str | None
    source_frame_id: int | None
    channel: str
    samples: int
    rate_hz: float
    mean_v: float
    std_v: float
    rel_std_percent: float | None
    min_v: float
    max_v: float
    peak_to_peak_v: float
    rms_v: float
    power_estimate: float


class PowerDriftMonitor:
    """光电探测器功率慢漂记录器。

    它的核心循环很简单：
    1. 调用底层 API 读取一小段 AI 数据。
    2. 计算均值、标准差、峰峰值、RMS。
    3. 把结果写入 CSV。
    4. 等到下一个 interval。
    """

    def __init__(self, settings: PowerDriftSettings) -> None:
        self.settings = settings
        self.daq = Usb6363Client(
            base_url=settings.api_base_url,
            timeout=max(settings.timeout + 5.0, 10.0),
        )

    def check_hardware_idle(self) -> None:
        """启动前检查 AI 是否空闲。

        这里故意保守：如果双峰 frame_stream 或后台 AI 采样正在运行，就先报错。
        这样新模块不会悄悄打断别的实验，也不会读到别的程序留下来的缓存。
        """

        if self.settings.data_source == "unified_stream":
            unified_status = self.daq.get_unified_ai_stream_status()
            if not unified_status.get("running"):
                raise RuntimeError("统一 AI 流没有运行。请先用 ai_stream_console.py 启动统一 AI 流。")

            settings = unified_status.get("settings") or {}
            channels = settings.get("channels") or unified_status.get("channels") or []
            requested = self.settings.channel
            requested_full = requested if "/" in requested else f"Dev2/{requested}"
            if channels and requested not in channels and requested_full not in channels:
                raise RuntimeError(
                    f"统一 AI 流正在运行，但没有包含功率通道 {requested}。"
                    f"当前统一流通道为：{channels}"
                )
            return

        unified_status = self.daq.get_unified_ai_stream_status()
        if unified_status.get("running"):
            raise RuntimeError(
                "统一 AI 流正在运行。direct_read 会重新打开 AI task，容易抢硬件；"
                "请停止统一流，或者把数据来源改成 unified_stream。"
            )

        frame_stream = self.daq.get_ai_frame_stream_status()
        if frame_stream.get("running"):
            raise RuntimeError(
                "底层 AI frame_stream 正在运行。请先在双峰 WebUI 里停止连续采集，"
                "或者调用 /api/ai/frame_stream/stop 后再启动功率慢漂监测。"
            )

        ai_status = self.daq.get_ai_status()
        if ai_status.get("running") and not self.settings.allow_busy_ai:
            raise RuntimeError(
                "后台 AI 采样正在运行。请先停止其他 AI 订阅，"
                "或者确认你真的要混用后加 --allow-busy-ai。"
            )

    def run(self) -> Path:
        """开始记录，直到 Ctrl+C 或到达 duration。"""

        self.check_hardware_idle()

        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now()
        stem = f"power_drift_{started_at.strftime('%Y%m%d_%H%M%S')}"
        csv_path = self.settings.output_dir / f"{stem}.csv"
        metadata_path = self.settings.output_dir / f"{stem}.json"

        metadata_path.write_text(
            json.dumps(
                {
                    "started_at": started_at.isoformat(timespec="seconds"),
                    "csv_file": str(csv_path.resolve()),
                    "settings": _settings_for_json(self.settings),
                    "note": "power_estimate = (mean_v - zero_voltage) * power_per_volt",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print("Power drift monitor")
        print(f"  API:      {self.settings.api_base_url}")
        print(f"  channel:  {self.settings.channel}")
        print(f"  interval: {self.settings.interval:g} s")
        print(f"  burst:    {self.settings.samples} samples @ {self.settings.rate:g} Hz")
        print(f"  CSV:      {csv_path.resolve()}")
        print("  Press Ctrl+C to stop.")
        print()

        start_time = time.time()
        next_start_time = start_time
        row_index = 0

        with csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(PowerDriftPoint.__dataclass_fields__))
            writer.writeheader()

            while True:
                now = time.time()
                if self.settings.duration is not None and now - start_time >= self.settings.duration:
                    break

                # 如果上一轮采集很快，就睡到下一个整齐的记录时刻。
                # 如果上一轮采集已经超过 interval，就不额外等待，直接继续下一轮。
                sleep_s = next_start_time - now
                if sleep_s > 0:
                    time.sleep(sleep_s)

                row_index += 1
                point = self.read_one_point(row_index=row_index, start_time=start_time)
                writer.writerow(asdict(point))
                file.flush()
                _print_point(point)

                next_start_time += self.settings.interval

        print()
        print(f"Stopped. CSV saved to: {csv_path.resolve()}")
        return csv_path

    def read_one_point(self, row_index: int, start_time: float) -> PowerDriftPoint:
        """读取一次小段 AI 数据，并计算这一行 CSV 的统计量。"""

        if self.settings.data_source == "unified_stream":
            return self._read_one_point_from_unified_stream(row_index=row_index, start_time=start_time)

        result = self.daq.read_ai(
            channel=self.settings.channel,
            samples=self.settings.samples,
            rate=self.settings.rate,
            terminal_config=self.settings.terminal_config,
            min_val=self.settings.min_val,
            max_val=self.settings.max_val,
            timeout=self.settings.timeout,
        )
        values = _as_float_list(result["values"])
        if not values:
            raise RuntimeError("底层 API 返回了空数据")

        mean_v = sum(values) / len(values)
        variance = sum((value - mean_v) ** 2 for value in values) / len(values)
        std_v = math.sqrt(variance)
        min_v = min(values)
        max_v = max(values)
        rms_v = math.sqrt(sum(value * value for value in values) / len(values))
        rel_std_percent = None if abs(mean_v) < 1e-30 else std_v / abs(mean_v) * 100.0
        power_estimate = (mean_v - self.settings.zero_voltage) * self.settings.power_per_volt
        timestamp = time.time()

        return PowerDriftPoint(
            iso_time=datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
            unix_time=timestamp,
            elapsed_s=timestamp - start_time,
            index=row_index,
            session_id=None,
            source_frame_id=None,
            channel=str(result.get("channel", self.settings.channel)),
            samples=len(values),
            rate_hz=float(result.get("rate", self.settings.rate)),
            mean_v=mean_v,
            std_v=std_v,
            rel_std_percent=rel_std_percent,
            min_v=min_v,
            max_v=max_v,
            peak_to_peak_v=max_v - min_v,
            rms_v=rms_v,
            power_estimate=power_estimate,
        )

    def _read_one_point_from_unified_stream(self, row_index: int, start_time: float) -> PowerDriftPoint:
        """从统一 AI 流读取统计量。

        这里不会打开新的 NI-DAQmx AI task，只读取 usb6363_core.py 已经采到的缓存。
        因此它可以和双峰查看器同时运行，前提是二者读取的是同一个统一 AI 流。
        """

        stats = self.daq.get_unified_ai_stats(
            channel=self.settings.channel,
            max_samples=self.settings.samples,
        )
        samples = int(stats.get("samples", 0))
        if samples < 1:
            raise RuntimeError("统一 AI 流还没有足够的缓存数据")

        mean_v = float(stats["mean"])
        std_v = float(stats["std"])
        min_v = float(stats["min"])
        max_v = float(stats["max"])
        rms_v = float(stats["rms"])
        rel_std_percent = None if abs(mean_v) < 1e-30 else std_v / abs(mean_v) * 100.0
        power_estimate = (mean_v - self.settings.zero_voltage) * self.settings.power_per_volt
        timestamp = time.time()

        return PowerDriftPoint(
            iso_time=datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
            unix_time=timestamp,
            elapsed_s=timestamp - start_time,
            index=row_index,
            session_id=None,
            source_frame_id=int(stats.get("frame_id", 0)),
            channel=str(stats.get("channel", self.settings.channel)),
            samples=samples,
            rate_hz=float(stats.get("rate", self.settings.rate)),
            mean_v=mean_v,
            std_v=std_v,
            rel_std_percent=rel_std_percent,
            min_v=min_v,
            max_v=max_v,
            peak_to_peak_v=max_v - min_v,
            rms_v=rms_v,
            power_estimate=power_estimate,
        )


def _as_float_list(values: Any) -> list[float]:
    """把底层 API 返回的 values 统一变成 list[float]。

    samples=1 时 values 可能是一个单独的数字；
    samples>1 时 values 是一个列表。
    """

    if isinstance(values, list):
        return [float(value) for value in values]
    return [float(values)]


def _settings_for_json(settings: PowerDriftSettings) -> dict[str, Any]:
    """把设置转换成 JSON 友好的普通字典。"""

    data = asdict(settings)
    data["output_dir"] = str(settings.output_dir.resolve())
    return data


def _print_point(point: PowerDriftPoint) -> None:
    """在终端打印一行简短状态，方便肉眼确认程序还在跑。"""

    rel = "--" if point.rel_std_percent is None else f"{point.rel_std_percent:.4f}%"
    print(
        f"[{point.index:06d}] "
        f"t={point.elapsed_s:9.2f}s  "
        f"mean={point.mean_v:+.8e} V  "
        f"std={point.std_v:.3e} V  "
        f"rel={rel}  "
        f"power={point.power_estimate:+.8e}"
    )


def parse_args() -> PowerDriftSettings:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="Slowly monitor photodetector power drift via USB-6363 AI.")
    parser.add_argument("--channel", default="ai2", help="光电探测器接到的 AI 通道，默认 ai2。")
    parser.add_argument(
        "--data-source",
        default="direct_read",
        choices=["direct_read", "unified_stream"],
        help="数据来源：direct_read 会独占读取 AI；unified_stream 读取已经运行的统一 AI 流。",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="CSV 记录间隔，单位秒，默认 1。")
    parser.add_argument("--samples", type=int, default=1000, help="每次记录读取多少个点，默认 1000。")
    parser.add_argument("--rate", type=float, default=1000.0, help="每次小段采集的采样率 Hz，默认 1000。")
    parser.add_argument("--terminal-config", default="RSE", choices=["RSE", "DIFF", "NRSE"], help="AI 接线方式。")
    parser.add_argument("--min-val", type=float, default=-10.0, help="AI 预期最小电压。")
    parser.add_argument("--max-val", type=float, default=10.0, help="AI 预期最大电压。")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次读取超时时间，单位秒。")
    parser.add_argument("--duration", type=float, default=None, help="总记录时长，单位秒；不填则一直记录到 Ctrl+C。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="CSV 输出目录。")
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL, help="USB-6363 底层 API 地址。")
    parser.add_argument("--power-per-volt", type=float, default=1.0, help="电压到功率的换算系数，未知时保持 1。")
    parser.add_argument("--zero-voltage", type=float, default=0.0, help="零功率本底电压，默认 0。")
    parser.add_argument(
        "--allow-busy-ai",
        action="store_true",
        help="允许后台 AI 采样已经运行时继续。新手一般不要加这个参数。",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        raise ValueError("--interval must be > 0")
    if args.samples < 1:
        raise ValueError("--samples must be >= 1")
    if args.rate <= 0:
        raise ValueError("--rate must be > 0")
    if args.timeout <= 0:
        raise ValueError("--timeout must be > 0")
    if args.duration is not None and args.duration <= 0:
        raise ValueError("--duration must be > 0")

    burst_seconds = args.samples / args.rate
    if burst_seconds > args.interval:
        print(
            "Warning: samples/rate 大于 interval。"
            "程序仍会运行，但实际记录频率会受单次采集耗时限制。"
        )

    return PowerDriftSettings(
        channel=args.channel,
        data_source=args.data_source,
        interval=args.interval,
        samples=args.samples,
        rate=args.rate,
        terminal_config=args.terminal_config,
        min_val=args.min_val,
        max_val=args.max_val,
        timeout=args.timeout,
        duration=args.duration,
        output_dir=args.output_dir,
        api_base_url=args.api_base_url,
        power_per_volt=args.power_per_volt,
        zero_voltage=args.zero_voltage,
        allow_busy_ai=args.allow_busy_ai,
    )


def main() -> int:
    """命令行入口。"""

    settings = parse_args()
    monitor = PowerDriftMonitor(settings)
    try:
        monitor.run()
    except KeyboardInterrupt:
        print()
        print("用户按 Ctrl+C，已停止记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
