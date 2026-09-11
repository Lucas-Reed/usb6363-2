"""Standalone continuous AI benchmark. No server imports or output tasks.

Examples:
    python daq_benchmark.py --methods task reader
    python daq_benchmark.py --methods task reader callback available
    python daq_benchmark.py --methods reader --chunks-ms 5 --seconds 300

Requires NI-DAQmx, nidaqmx and numpy. Stop other AI acquisition first.
This measures host delivery, not physical input-to-output latency.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
from datetime import datetime
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import sys
import threading
import time


class Measurements:
    """Bounded recent distributions, full-run totals and backlog regression."""

    def __init__(self, warmup, seconds, capacity=200000):
        self.warmup = warmup
        self.seconds = seconds
        self.capacity = capacity
        self.started = time.perf_counter()
        self.anchor = None
        self.last = None
        self.cpu_start = None
        self.cpu_end = None
        self.reads = self.samples = 0
        self.intervals = deque(maxlen=capacity)
        self.durations = deque(maxlen=capacity)
        self.sizes = deque(maxlen=capacity)
        self.max_interval = self.max_duration = 0.0
        self.backlog_sum = self.backlog_max = 0
        self.sx = self.sy = self.sxx = self.sxy = 0.0
        self.timeline = []
        self.next_snapshot = 0.0

    def observe(self, count, begin, end, backlog):
        if self.anchor is None:
            if end - self.started >= self.warmup:
                # Discard the boundary read; all following samples belong to
                # the measured delivery interval between read completions.
                self.anchor = self.last = end
                self.cpu_start = time.process_time()
            return False
        elapsed = end - self.anchor
        interval = (end - self.last) * 1000
        duration = (end - begin) * 1000
        self.last = end
        self.cpu_end = time.process_time()
        self.reads += 1
        self.samples += count
        self.intervals.append(interval)
        self.durations.append(duration)
        self.sizes.append(count)
        self.max_interval = max(self.max_interval, interval)
        self.max_duration = max(self.max_duration, duration)
        self.backlog_sum += backlog
        self.backlog_max = max(self.backlog_max, backlog)
        self.sx += elapsed
        self.sy += backlog
        self.sxx += elapsed * elapsed
        self.sxy += elapsed * backlog
        if elapsed >= self.next_snapshot:
            self.timeline.append({"elapsed_s": elapsed, "unread_per_channel": backlog})
            self.next_snapshot = elapsed + 1.0
        return elapsed >= self.seconds

    def summary(self, np):
        elapsed = self.last - self.anchor if self.reads else 0.0
        denominator = self.reads * self.sxx - self.sx * self.sx
        slope = ((self.reads * self.sxy - self.sx * self.sy) / denominator
                 if denominator > 0 else None)
        result = {
            "measured_seconds": elapsed,
            "reads": self.reads,
            "samples_per_channel": self.samples,
            "delivered_samples_per_channel_s": self.samples / elapsed if elapsed else None,
            "cpu_percent_one_core": ((self.cpu_end - self.cpu_start) / elapsed * 100
                                     if elapsed else None),
            "backlog_after_read_mean": self.backlog_sum / self.reads if self.reads else None,
            "backlog_after_read_max": self.backlog_max if self.reads else None,
            "backlog_slope_samples_per_channel_s": slope,
            "return_interval_max_ms": self.max_interval if self.reads else None,
            "read_duration_max_ms": self.max_duration if self.reads else None,
            "distribution_reads_retained": len(self.intervals),
            "distribution_truncated": self.reads > self.capacity,
        }
        for name, values in (("return_interval_ms", self.intervals),
                             ("read_duration_ms", self.durations),
                             ("batch_samples_per_channel", self.sizes)):
            for percentile in (50, 99):
                result[f"{name}_p{percentile}"] = (
                    float(np.percentile(values, percentile)) if values else None)
        return result


def run_case(args, method, chunk_ms, np, nidaqmx):
    from nidaqmx.constants import AcquisitionType, TaskMode, TerminalConfiguration
    from nidaqmx.stream_readers import AnalogMultiChannelReader

    channel_names = [f"{args.device}/{ch}" for ch in args.channels.split(",")]
    count = max(1, round(args.rate * chunk_ms / 1000)) if chunk_ms else None
    row = {"method": method, "repeat": args.current_repeat,
           "device": args.device, "channels": ",".join(channel_names),
           "terminal": args.terminal, "min_v": args.min_v, "max_v": args.max_v,
           "requested_rate_per_channel": args.rate, "requested_chunk_ms": chunk_ms,
           "chunk_samples_per_channel": count, "requested_seconds": args.seconds,
           "warmup_seconds": args.warmup, "poll_ms": args.poll_ms if method == "available" else None,
           "status": "error", "error_code": None, "error": None}
    metrics = None
    task = None
    lock = threading.Lock()
    finished = threading.Event()
    closing = threading.Event()
    callback_errors = []
    interrupted = False
    try:
        task = nidaqmx.Task()
        for channel in channel_names:
            task.ai_channels.add_ai_voltage_chan(
                channel, terminal_config=getattr(TerminalConfiguration, args.terminal),
                min_val=args.min_v, max_val=args.max_v)
        buffer_samples = max(2, math.ceil(args.rate * args.buffer_seconds))
        if count and buffer_samples < 2 * count:
            raise ValueError("Input buffer must hold at least two chunks; increase --buffer-seconds")
        task.timing.cfg_samp_clk_timing(
            args.rate, sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=buffer_samples)
        task.in_stream.input_buf_size = buffer_samples
        task.control(TaskMode.TASK_COMMIT)
        actual_rate = task.timing.samp_clk_rate
        actual_buffer = task.in_stream.input_buf_size
        row.update(actual_rate_per_channel=actual_rate,
                   input_buffer_samples_per_channel=actual_buffer,
                   actual_chunk_ms=count / actual_rate * 1000 if count else None)
        reader = AnalogMultiChannelReader(task.in_stream)
        data = np.empty((len(channel_names), count or actual_buffer), dtype=np.float64)
        storage = data.reshape(-1)

        def acquire(n):
            begin = time.perf_counter()
            if method == "task":
                # Keep the list alive until after timing, matching the ndarray
                # route; neither route performs an additional downstream copy.
                values = task.read(number_of_samples_per_channel=n, timeout=args.timeout)
                read_count = len(values) if len(channel_names) == 1 else len(values[0])
            else:
                # A column slice of a multichannel array is not contiguous.
                # Reshape a flat prefix to match the exact requested shape
                # without allocating another sample buffer.
                target = (storage[:len(channel_names) * n].reshape(len(channel_names), n)
                          if method == "available" else data)
                read_count = reader.read_many_sample(
                    target, number_of_samples_per_channel=n, timeout=args.timeout)
            end = time.perf_counter()
            backlog = task.in_stream.avail_samp_per_chan
            if read_count != n:
                raise RuntimeError(f"Short read: requested {n}, received {read_count}")
            if metrics.observe(read_count, begin, end, backlog):
                finished.set()

        def callback(handle, event_type, number_of_samples, callback_data):
            with lock:
                if closing.is_set() or finished.is_set():
                    return 0
                try:
                    acquire(number_of_samples)
                except Exception as exc:
                    callback_errors.append(exc)
                    finished.set()
            return 0

        if method == "callback":
            task.register_every_n_samples_acquired_into_buffer_event(count, callback)
        metrics = Measurements(args.warmup, args.seconds)
        task.start()
        # A stopped clock / missing data must not leave callback or polling
        # benchmarks waiting indefinitely. Blocking reads have DAQmx timeout.
        deadline = time.perf_counter() + args.warmup + args.seconds + 2 * args.timeout
        while not finished.is_set():
            if time.perf_counter() > deadline:
                raise TimeoutError("Benchmark deadline exceeded; insufficient data delivered")
            if method == "callback":
                finished.wait(0.05)
            elif method == "available":
                available = task.in_stream.avail_samp_per_chan
                if available:
                    acquire(min(available, data.shape[1]))
                if not finished.is_set():
                    time.sleep(args.poll_ms / 1000)
            else:
                acquire(count)
        if callback_errors:
            raise callback_errors[0]
        row["status"] = "completed"
    except KeyboardInterrupt:
        interrupted = True
        row.update(status="interrupted", error="Interrupted by user")
    except Exception as exc:
        row.update(error=str(exc), error_code=getattr(exc, "error_code", None))
    finally:
        closing.set()
        # Drain an in-flight callback before reading diagnostics. Never hold
        # this lock while stopping DAQmx, which may wait for its callback.
        with lock:
            if task is not None:
                try:
                    row["end_unread_per_channel"] = task.in_stream.avail_samp_per_chan
                    row["acquired_per_channel_including_warmup"] = task.in_stream.total_samp_per_chan_acquired
                except Exception as exc:
                    row["diagnostic_error"] = str(exc)
        if task is not None:
            try:
                task.close()
            except Exception as exc:
                row.update(status="error", error=f"{row['error'] or ''} Close failed: {exc}")
        if metrics:
            row.update(metrics.summary(np))
    return row, metrics.timeline if metrics else [], interrupted


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="Dev2")
    parser.add_argument("--channels", default="ai0,ai1,ai2", help="Comma-separated channel names, without device")
    parser.add_argument("--rate", type=float, default=100000, help="Samples/s/channel")
    parser.add_argument("--terminal", choices=["DIFF", "RSE", "NRSE", "DEFAULT"], default="DIFF")
    parser.add_argument("--min-v", type=float, default=-10)
    parser.add_argument("--max-v", type=float, default=10)
    parser.add_argument("--methods", nargs="+", choices=["task", "reader", "callback", "available"],
                        default=["task", "reader", "callback", "available"])
    parser.add_argument("--chunks-ms", nargs="+", type=float, default=[10, 5, 1])
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--buffer-seconds", type=float, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--poll-ms", type=float, default=1, help="Sleep after each available-data poll")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("data/daq_benchmark"))
    args = parser.parse_args(argv)
    for name in ("rate", "seconds", "buffer_seconds", "timeout", "poll_ms"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if not math.isfinite(args.warmup) or args.warmup < 0 or args.repeat < 1:
        parser.error("warmup must be nonnegative and repeat must be positive")
    if any(not math.isfinite(x) or x <= 0 for x in args.chunks_ms):
        parser.error("chunks must be finite and positive")
    if not (math.isfinite(args.min_v) and math.isfinite(args.max_v) and args.min_v < args.max_v):
        parser.error("Voltage range must be finite with min-v < max-v")
    channels = [ch.strip() for ch in args.channels.split(",")]
    if any(not ch.startswith("ai") or not ch[2:].isdigit() for ch in channels) or len(set(channels)) != len(channels):
        parser.error("Use distinct channel names such as ai0,ai1,ai2")
    args.channels = ",".join(channels)
    return args


def save_results(base, metadata, results):
    with base.with_suffix(".json").open("w", encoding="utf-8") as stream:
        json.dump({"metadata": metadata, "results": results}, stream, indent=2, allow_nan=False)
    rows = [item["summary"] for item in results]
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    args = parse_args(argv)
    try:
        import numpy as np
        import nidaqmx
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run: python -m pip install numpy nidaqmx", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.output / datetime.now().strftime("benchmark_%Y%m%d_%H%M%S_%f")
    metadata = {"python": sys.version, "platform": platform.platform(),
                "numpy": np.__version__, "nidaqmx": importlib.metadata.version("nidaqmx"),
                "notes": "CPU uses 100% per logical core. Quantiles retain last 200000 reads. "
                         "Backlog sampled after each read. Throughput measures host delivery. "
                         "No downstream copy, trigger, output, or waveform validation. "
                         "Completed means execution finished, not a latency or lossless certification."}
    try:
        metadata["daqmx_driver_version"] = str(nidaqmx.system.System.local().driver_version)
    except Exception as exc:
        metadata["driver_query_error"] = str(exc)
    print("Continuous AI only. Stop existing AI acquisition before running this benchmark.")
    print("No external trigger. No physical latency or signal accuracy measurement.")
    print(f"Results: {base}.csv / .json", flush=True)
    results = []
    for repeat in range(1, args.repeat + 1):
        args.current_repeat = repeat
        for method in args.methods:
            for chunk in ([None] if method == "available" else args.chunks_ms):
                print(f"Running {method}, chunk={chunk} ms, repeat={repeat} ...", flush=True)
                row, timeline, interrupted = run_case(args, method, chunk, np, nidaqmx)
                results.append({"summary": row, "backlog_timeline": timeline})
                save_results(base, metadata, results)
                def display(key):
                    value = row.get(key)
                    return f"{value:.3f}" if isinstance(value, (float, int)) else "n/a"
                print(f"  {row['status']} | S/s/ch={display('delivered_samples_per_channel_s')}"
                      f" | interval P99 ms={display('return_interval_ms_p99')}"
                      f" | max unread={display('backlog_after_read_max')}"
                      f" | CPU %={display('cpu_percent_one_core')}", flush=True)
                if row["error"]:
                    print(f"  Error {row['error_code']}: {row['error']}", flush=True)
                if interrupted:
                    return 130
    return 1 if any(x["summary"]["status"] != "completed" for x in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
