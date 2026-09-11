"""Offline checks; fake DAQ delivery is not a hardware performance test."""

import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

import daq_benchmark as benchmark


class FakeTask:
    instances = []
    fail_read = False

    def __init__(self):
        self.instances.append(self)
        self.closed = False
        self.thread = None
        self.callback = None
        self.stop = threading.Event()
        self.ai_channels = types.SimpleNamespace(add_ai_voltage_chan=lambda *a, **k: None)
        self.timing = types.SimpleNamespace(samp_clk_rate=100000,
                                           cfg_samp_clk_timing=lambda *a, **k: None)
        self.in_stream = types.SimpleNamespace(input_buf_size=100000,
                                              avail_samp_per_chan=100,
                                              total_samp_per_chan_acquired=0)

    def control(self, mode):
        pass

    def register_every_n_samples_acquired_into_buffer_event(self, n, callback):
        self.callback = callback
        self.n = n

    def start(self):
        if self.callback:
            def run():
                while not self.stop.wait(0.001):
                    self.callback(None, None, self.n, None)
            self.thread = threading.Thread(target=run)
            self.thread.start()

    def read(self, number_of_samples_per_channel, timeout):
        time.sleep(0.001)
        if self.fail_read:
            raise RuntimeError("Injected read failure")
        self.in_stream.total_samp_per_chan_acquired += number_of_samples_per_channel
        return [[0] * number_of_samples_per_channel for _ in range(3)]

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
            assert not self.thread.is_alive()
        self.closed = True


class FakeReader:
    buffers = []

    def __init__(self, stream):
        self.task = FakeTask.instances[-1]

    def read_many_sample(self, data, number_of_samples_per_channel, timeout):
        if data.shape != (3, number_of_samples_per_channel):
            raise ValueError("Read array shape must match channels and requested samples")
        if not data.flags.c_contiguous:
            raise ValueError("Read array must be C-contiguous")
        self.buffers.append((data.shape, data.ctypes.data))
        self.task.read(number_of_samples_per_channel, timeout)
        self.task.in_stream.avail_samp_per_chan = (101, 7, 1000)[len(self.buffers) % 3]
        return number_of_samples_per_channel


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        FakeTask.instances = []
        FakeTask.fail_read = False
        FakeReader.buffers = []
        constants = types.SimpleNamespace(
            AcquisitionType=types.SimpleNamespace(CONTINUOUS=1),
            TaskMode=types.SimpleNamespace(TASK_COMMIT=1),
            TerminalConfiguration=types.SimpleNamespace(DIFF=1))
        self.modules = patch.dict("sys.modules", {
            "nidaqmx.constants": constants,
            "nidaqmx.stream_readers": types.SimpleNamespace(AnalogMultiChannelReader=FakeReader),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def run_method(self, method):
        args = benchmark.parse_args(["--warmup", "0.003", "--seconds", "0.012"])
        args.current_repeat = 1
        return benchmark.run_case(args, method, None if method == "available" else 1,
                                  np, types.SimpleNamespace(Task=FakeTask))

    def test_all_routes_complete_and_release_task(self):
        for method in ("task", "reader", "callback", "available"):
            with self.subTest(method=method):
                row, timeline, interrupted = self.run_method(method)
                self.assertEqual(row["status"], "completed", row)
                self.assertGreater(row["samples_per_channel"], 0)
                self.assertGreaterEqual(row["measured_seconds"], 0.012)
                self.assertTrue(timeline)
                self.assertFalse(interrupted)
                self.assertTrue(FakeTask.instances[-1].closed)

    def test_available_reuses_contiguous_storage_for_different_batch_sizes(self):
        row, _, _ = self.run_method("available")
        self.assertEqual(row["status"], "completed", row)
        self.assertGreater(len({shape for shape, _ in FakeReader.buffers}), 1)
        self.assertEqual(len({address for _, address in FakeReader.buffers}), 1)

    def test_reader_and_callback_errors_are_reported_and_released(self):
        FakeTask.fail_read = True
        for method in ("reader", "callback"):
            row, _, _ = self.run_method(method)
            self.assertEqual(row["status"], "error")
            self.assertIn("Injected read failure", row["error"])
            self.assertTrue(FakeTask.instances[-1].closed)

    def test_warmup_counts_bounded_quantiles_and_full_run_slope(self):
        with patch.object(benchmark.time, "perf_counter", return_value=0):
            metrics = benchmark.Measurements(2, 3, capacity=2)
        self.assertFalse(metrics.observe(999, 0, 1, 999))
        self.assertFalse(metrics.observe(999, 1, 2, 999))
        for end in (3, 4, 5):
            done = metrics.observe(100, end - 0.1, end, 10 * (end - 2))
        summary = metrics.summary(np)
        self.assertTrue(done)
        self.assertEqual(summary["samples_per_channel"], 300)
        self.assertEqual(summary["delivered_samples_per_channel_s"], 100)
        self.assertEqual(summary["backlog_slope_samples_per_channel_s"], 10)
        self.assertTrue(summary["distribution_truncated"])
        self.assertEqual(summary["distribution_reads_retained"], 2)

    def test_output_preserves_errors_and_timeline(self):
        results = [{"summary": {"method": "reader", "status": "completed"}, "backlog_timeline": []},
                   {"summary": {"method": "task", "status": "error", "error_code": -200279},
                    "backlog_timeline": []}]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "result"
            benchmark.save_results(base, {}, results)
            self.assertEqual(json.loads(base.with_suffix(".json").read_text())["results"], results)
            with base.with_suffix(".csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[1]["error_code"], "-200279")


if __name__ == "__main__":
    unittest.main()
