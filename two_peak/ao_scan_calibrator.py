"""AO 扫描标定器。

这个模块只做一件事：让 AO 输出按电压点扫描，同时读取双峰面积慢漂记录器
已经算好的面积和。这样标定得到的是：

    AO 电压 -> A 峰面积 + B 峰面积

后续 PID 功率稳定也应该使用同一个量，避免“标定量”和“反馈量”不一致。

重要边界：
- 本模块不直接 import nidaqmx。
- AO 输出通过 Usb6363Client 发送给 8765 底层服务。
- 面积测量通过 AreaTrendLogger.status() 读取，要求用户先在 WebUI 里开始面积慢漂记录。
"""

from __future__ import annotations

import csv
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from two_peak.trend_logger import AreaTrendLogger
from usb6363_client import Usb6363Client


class AoScanCalibrator:
    """后台 AO 扫描标定器。

    WebUI 点击“开始 AO 扫描”后，本类会启动一个后台线程。
    线程逐个设置 AO 电压，然后等待面积慢漂记录器给出新的面积和统计值。
    """

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
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._running = False
        self._error: str | None = None
        self._csv_path: Path | None = None
        self._settings: dict[str, Any] = {}
        self._points: list[dict[str, Any]] = []
        self._current_voltage: float | None = None
        self._current_index = 0

    def start(
        self,
        channel: str = "ao0",
        start_voltage: float = 0.0,
        stop_voltage: float = 1.0,
        step_voltage: float = 0.05,
        min_val: float = -10.0,
        max_val: float = 10.0,
        settle_s: float = 0.5,
        dwell_s: float = 2.0,
        measure_field: str = "area_sum_ema",
        restore_voltage: float | None = None,
    ) -> dict[str, Any]:
        """启动 AO 扫描。

        参数说明：
        - channel：AO 通道，例如 ao0。
        - start_voltage/stop_voltage/step_voltage：扫描起点、终点、步长。
        - min_val/max_val：传给 NI-DAQmx 的 AO 安全范围。
        - settle_s：每次改电压后先等多久，让 AOM/光路稳定。
        - dwell_s：稳定后在这个时间内收集面积统计，求平均。
        - measure_field：使用哪个面积字段，默认 area_sum_ema。
        - restore_voltage：扫描结束后可选地回到某个电压；留空则保持最后一个扫描电压。
        """

        if step_voltage == 0:
            raise ValueError("step_voltage must not be 0")
        if settle_s < 0:
            raise ValueError("settle_s must be >= 0")
        if dwell_s <= 0:
            raise ValueError("dwell_s must be > 0")
        if min_val >= max_val:
            raise ValueError("min_val must be smaller than max_val")

        voltages = _build_voltage_points(start_voltage, stop_voltage, step_voltage)
        if len(voltages) > 1000:
            raise ValueError("AO scan has too many points; please use a larger step")

        trend_status = self._trend_logger.status()
        if trend_status.get("running") is not True:
            raise RuntimeError("请先开始面积慢漂记录，再开始 AO 扫描标定")

        with self._lock:
            if self._running:
                raise RuntimeError("AO scan is already running")

            self._output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = self._output_dir / f"ao_scan_{timestamp}.csv"

            settings = {
                "channel": str(channel),
                "start_voltage": float(start_voltage),
                "stop_voltage": float(stop_voltage),
                "step_voltage": float(step_voltage),
                "min_val": float(min_val),
                "max_val": float(max_val),
                "settle_s": float(settle_s),
                "dwell_s": float(dwell_s),
                "measure_field": str(measure_field),
                "restore_voltage": None if restore_voltage is None else float(restore_voltage),
                "voltages": voltages,
            }

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(settings, csv_path, stop_event),
                daemon=True,
                name="two-peak-ao-scan-calibrator",
            )
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
            self._error = None
            self._csv_path = csv_path
            self._settings = dict(settings)
            self._points = []
            self._current_voltage = None
            self._current_index = 0
            thread.start()

        return self.status()

    def stop(self) -> dict[str, Any]:
        """请求停止 AO 扫描。"""

        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            if stop_event is not None:
                stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
                self._running = False

        return self.status()

    def status(self) -> dict[str, Any]:
        """返回当前扫描状态，供 WebUI 轮询显示。"""

        with self._lock:
            return {
                "running": self._running,
                "error": self._error,
                "csv_file": str(self._csv_path.resolve()) if self._csv_path else None,
                "settings": dict(self._settings),
                "current_voltage": self._current_voltage,
                "current_index": self._current_index,
                "points_written": len(self._points),
                "points": [dict(point) for point in self._points],
            }

    def _worker(
        self,
        settings: dict[str, Any],
        csv_path: Path,
        stop_event: threading.Event,
    ) -> None:
        """扫描线程主体。"""

        try:
            with csv_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=_csv_fieldnames())
                writer.writeheader()

                for index, voltage in enumerate(settings["voltages"]):
                    if stop_event.is_set():
                        break

                    with self._lock:
                        self._current_index = index + 1
                        self._current_voltage = float(voltage)

                    self._daq.write_ao(
                        channel=settings["channel"],
                        value=float(voltage),
                        min_val=float(settings["min_val"]),
                        max_val=float(settings["max_val"]),
                    )

                    if _sleep_until_stop(stop_event, float(settings["settle_s"])):
                        break

                    stats_samples = self._collect_latest_stats(
                        dwell_s=float(settings["dwell_s"]),
                        stop_event=stop_event,
                    )
                    row = _build_point_row(
                        index=index,
                        voltage=float(voltage),
                        stats_samples=stats_samples,
                        settings=settings,
                    )
                    writer.writerow(row)
                    file.flush()

                    with self._lock:
                        self._points.append(dict(row))
                        self._error = None

                restore_voltage = settings.get("restore_voltage")
                if restore_voltage is not None:
                    self._daq.write_ao(
                        channel=settings["channel"],
                        value=float(restore_voltage),
                        min_val=float(settings["min_val"]),
                        max_val=float(settings["max_val"]),
                    )

        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            with self._lock:
                self._running = False
                self._thread = None
                self._stop_event = None

    def _collect_latest_stats(
        self,
        dwell_s: float,
        stop_event: threading.Event,
    ) -> list[dict[str, Any]]:
        """在一个电压点停留期间收集面积统计行。

        每一行来自 AreaTrendLogger.status()["latest_stats"]。
        这里保存整行，而不是只保存一个字段，是为了 AO 标定能同时得到
        A、B、A+B 三个反馈量，后续分别锁定两个峰时不需要重扫。
        """

        rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, Any]] = set()
        deadline = time.time() + dwell_s

        while time.time() < deadline and not stop_event.is_set():
            status = self._trend_logger.status()
            latest = status.get("latest_stats") or {}

            key = (latest.get("unix_time"), latest.get("frame_id"))
            if latest and key not in seen_keys:
                rows.append(dict(latest))
                seen_keys.add(key)

            time.sleep(0.1)

        if not rows:
            raise RuntimeError("AO scan did not receive area statistics during dwell time")
        return rows


def _build_voltage_points(start: float, stop: float, step: float) -> list[float]:
    """根据起点、终点、步长生成包含终点附近的电压列表。"""

    direction = 1.0 if stop >= start else -1.0
    step_abs = abs(step) * direction
    points: list[float] = []
    value = float(start)

    for _ in range(1001):
        if direction > 0 and value > stop + abs(step) * 1e-9:
            break
        if direction < 0 and value < stop - abs(step) * 1e-9:
            break
        points.append(round(value, 10))
        value += step_abs

    if not points:
        raise ValueError("AO scan has no voltage points")
    return points


def _sleep_until_stop(stop_event: threading.Event, seconds: float) -> bool:
    """可被 stop_event 打断的 sleep。返回 True 表示被停止。"""

    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_event.is_set():
            return True
        time.sleep(min(0.05, max(0.0, deadline - time.time())))
    return stop_event.is_set()


def _build_point_row(
    index: int,
    voltage: float,
    stats_samples: list[dict[str, Any]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """把一个扫描点的测量值整理成 CSV 行。"""

    selected_stats = _field_stats(
        stats_samples,
        field=str(settings["measure_field"]),
        fallback_field=_fallback_field(str(settings["measure_field"])),
    )
    area_a_stats = _field_stats(stats_samples, "area_mean")
    area_a_ema_stats = _field_stats(stats_samples, "area_ema", fallback_field="area_mean")
    area_b_stats = _field_stats(stats_samples, "area2_mean")
    area_b_ema_stats = _field_stats(stats_samples, "area2_ema", fallback_field="area2_mean")
    area_sum_stats = _field_stats(stats_samples, "area_sum_mean")
    area_sum_ema_stats = _field_stats(stats_samples, "area_sum_ema", fallback_field="area_sum_mean")

    return {
        "iso_time": datetime.now().isoformat(timespec="seconds"),
        "point_index": index + 1,
        "ao_channel": settings["channel"],
        "ao_voltage": voltage,
        "measure_field": settings["measure_field"],
        "sample_count": len(stats_samples),
        "area_value_mean": selected_stats["mean"],
        "area_value_std": selected_stats["std"],
        "area_value_rel_std_percent": selected_stats["rel_std_percent"],
        "area_value_first": selected_stats["first"],
        "area_value_last": selected_stats["last"],
        "area_a_mean": area_a_stats["mean"],
        "area_a_std": area_a_stats["std"],
        "area_a_ema_mean": area_a_ema_stats["mean"],
        "area_b_mean": area_b_stats["mean"],
        "area_b_std": area_b_stats["std"],
        "area_b_ema_mean": area_b_ema_stats["mean"],
        "area_sum_mean": area_sum_stats["mean"],
        "area_sum_std": area_sum_stats["std"],
        "area_sum_ema_mean": area_sum_ema_stats["mean"],
        "area_window_a_left": stats_samples[-1].get("area_left"),
        "area_window_a_right": stats_samples[-1].get("area_right"),
        "area_window_a_peak_index": stats_samples[-1].get("area_peak_index"),
        "area_window_b_left": stats_samples[-1].get("area2_left"),
        "area_window_b_right": stats_samples[-1].get("area2_right"),
        "area_window_b_peak_index": stats_samples[-1].get("area2_peak_index"),
        "settle_s": settings["settle_s"],
        "dwell_s": settings["dwell_s"],
    }


def _fallback_field(field: str) -> str | None:
    """EMA 关闭时使用对应的未滤波字段作为后备。"""

    if field == "area_ema":
        return "area_mean"
    if field == "area2_ema":
        return "area2_mean"
    if field == "area_sum_ema":
        return "area_sum_mean"
    return None


def _field_stats(
    rows: list[dict[str, Any]],
    field: str,
    fallback_field: str | None = None,
) -> dict[str, Any]:
    """从若干 latest_stats 行里提取一个字段并计算统计量。"""

    values: list[float] = []
    for row in rows:
        value = row.get(field)
        if value is None and fallback_field is not None:
            value = row.get(fallback_field)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue

    if not values:
        return {
            "mean": None,
            "std": None,
            "rel_std_percent": None,
            "first": None,
            "last": None,
        }

    mean = sum(values) / len(values)
    if len(values) >= 2:
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        std = math.sqrt(variance)
    else:
        std = 0.0
    rel_std = None if mean == 0 else std / abs(mean) * 100.0

    return {
        "mean": mean,
        "std": std,
        "rel_std_percent": rel_std,
        "first": values[0],
        "last": values[-1],
    }


def _csv_fieldnames() -> list[str]:
    """AO 标定 CSV 的列名。"""

    return [
        "iso_time",
        "point_index",
        "ao_channel",
        "ao_voltage",
        "measure_field",
        "sample_count",
        "area_value_mean",
        "area_value_std",
        "area_value_rel_std_percent",
        "area_value_first",
        "area_value_last",
        "area_a_mean",
        "area_a_std",
        "area_a_ema_mean",
        "area_b_mean",
        "area_b_std",
        "area_b_ema_mean",
        "area_sum_mean",
        "area_sum_std",
        "area_sum_ema_mean",
        "area_window_a_left",
        "area_window_a_right",
        "area_window_a_peak_index",
        "area_window_b_left",
        "area_window_b_right",
        "area_window_b_peak_index",
        "settle_s",
        "dwell_s",
    ]
