"""双峰慢速功率锁定器。

本模块负责把慢漂记录里的 A/B 峰统计值，转换成一个或两个 AO 输出的慢速 PI 修正。
除原来的固定目标锁定外，还支持“采样峰只读、锁定峰维持指定比值”的比例锁定。

重要边界：
- 不直接 import nidaqmx。
- 只通过 Usb6363Client 写 AO。
- 只读取 AreaTrendLogger 已经算好的慢漂统计值，不自己采集波形。
- 第一版只做慢速 PI，不做 D 项，不追逐 shot-to-shot 高频噪声。
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


FIXED_TARGET_MODE = "fixed"
RATIO_TARGET_MODE = "follow_ratio"

# 比例锁定要求两个窗口使用同一种物理统计口径。这里明确列出 A/B 对应关系，
# 防止把 A 峰面积和 B 峰峰高之类量纲不同的数据误组成一个“比例”。
RATIO_REFERENCE_FIELDS = {
    "area_ema": "area2_ema",
    "area2_ema": "area_ema",
    "top_ema": "top2_ema",
    "top2_ema": "top_ema",
    "peak_height_ema": "peak2_height_ema",
    "peak2_height_ema": "peak_height_ema",
}

LOCK_LOG_FIELDS = (
    "iso_time",
    "unix_time",
    "frame_id",
    "window_revision",
    "controller_name",
    "channel",
    "target_mode",
    "feedback_field",
    "reference_field",
    "measured",
    "reference_value",
    "configured_target",
    "dynamic_target",
    "follow_ratio",
    "actual_ratio",
    "relative_error",
    "voltage",
    "command_delta",
    "integral",
    "limited",
    "kp",
    "ki",
)


class PowerLockController:
    """后台慢速 PI 锁定器。"""

    def __init__(
        self,
        daq: Usb6363Client,
        trend_logger: AreaTrendLogger,
        output_dir: Path | None = None,
    ) -> None:
        self._daq = daq
        self._trend_logger = trend_logger
        self._output_dir = output_dir
        self._lock = threading.Lock()
        # 参数更新和一次完整的双路 AO 更新不能交叉执行。这个锁不用于状态查询，
        # 因此 WebUI 轮询不会被普通计算阻塞。
        self._cycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._wake_event: threading.Event | None = None
        self._running = False
        self._error: str | None = None
        self._settings: dict[str, Any] = {}
        self._controllers: list[dict[str, Any]] = []
        self._states: list[dict[str, Any]] = []
        self._iterations = 0
        self._last_update = 0.0
        self._parameter_revision = 0
        self._last_parameter_update = 0.0
        self._last_stats_frame_id = 0
        self._last_stats_timestamp = 0.0
        self._last_new_stats_monotonic = 0.0
        self._csv_path: Path | None = None
        self._records_written = 0

    def start(self, controllers: list[dict[str, Any]], update_s: float = 1.0) -> dict[str, Any]:
        """启动慢速 PI 锁定。

        controllers 是前端传来的控制配置。每一路至少包含：
        channel, feedback_field, target, initial_voltage, min_voltage, max_voltage,
        direction, max_step_v, kp, ki。比例模式还包含 target_mode、
        reference_field 和 follow_ratio。
        """

        if not math.isfinite(update_s) or update_s <= 0:
            raise ValueError("update_s must be > 0")

        validated = _validate_controllers(controllers)

        trend_status = self._trend_logger.status()
        if trend_status.get("running") is not True:
            raise RuntimeError("请先开始面积慢漂记录，再启动功率锁定")

        # 先确认日志目录可创建，再把控制器切换为 running。这样磁盘权限错误
        # 不会留下“状态显示正在锁定、实际上后台线程根本没启动”的半初始化状态。
        csv_path = self._new_csv_path()

        with self._lock:
            if self._running:
                raise RuntimeError("power lock is already running")

            states = []
            for controller in validated:
                states.append(
                    {
                        "name": controller["name"],
                        "channel": controller["channel"],
                        "feedback_field": controller["feedback_field"],
                        "target_mode": controller["target_mode"],
                        "reference_field": controller["reference_field"],
                        "follow_ratio": controller["follow_ratio"],
                        "reference_value": None,
                        "actual_ratio": None,
                        "configured_target": controller["target"],
                        "target": controller["target"],
                        "voltage": controller["initial_voltage"],
                        "integral": 0.0,
                        "measurement_revision": 0,
                        "stats_frame_id": 0,
                        "measured": None,
                        "relative_error": None,
                        "command_delta": 0.0,
                        "limited": False,
                    }
                )

            stop_event = threading.Event()
            wake_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(stop_event, wake_event),
                daemon=True,
                name="two-peak-power-lock",
            )
            self._stop_event = stop_event
            self._wake_event = wake_event
            self._thread = thread
            self._running = True
            self._error = None
            self._settings = {"update_s": float(update_s)}
            self._controllers = [dict(controller) for controller in validated]
            self._states = states
            self._iterations = 0
            self._last_update = 0.0
            self._parameter_revision = 1
            self._last_parameter_update = time.time()
            self._last_stats_frame_id = 0
            self._last_stats_timestamp = 0.0
            self._last_new_stats_monotonic = time.monotonic()
            self._records_written = 0
            self._csv_path = csv_path
            thread.start()

        return self.status()

    def update_parameters(
        self,
        controllers: list[dict[str, Any]],
        update_s: float,
    ) -> dict[str, Any]:
        """在锁定运行中原子更新慢速 PI 参数。

        热更新只允许改变固定目标值、跟随比例、Kp、Ki、单步限幅和更新周期。
        硬件通道、反馈字段、目标模式、参考字段、方向、安全范围与初始电压
        会改变控制器的物理含义，必须停锁后修改。
        更新时保留当前 AO 电压，但清零积分，避免旧积分与新参数组合造成突跳。
        """

        if not math.isfinite(update_s) or update_s <= 0:
            raise ValueError("update_s must be > 0")

        validated = _validate_controllers(controllers)
        with self._cycle_lock:
            with self._lock:
                if not self._running:
                    raise RuntimeError("power lock is not running")
                if len(validated) != len(self._controllers):
                    raise ValueError("锁定运行中不能启用或停用控制回路")

                updated_controllers: list[dict[str, Any]] = []
                updated_states: list[dict[str, Any]] = []
                for index, (current, requested) in enumerate(
                    zip(self._controllers, validated, strict=True)
                ):
                    _validate_runtime_identity(current, requested)
                    updated = dict(current)
                    for field in ("target", "follow_ratio", "max_step_v", "kp", "ki"):
                        updated[field] = requested[field]
                    updated_controllers.append(updated)

                    previous = dict(self._states[index])
                    # 当前 AO 电压和最近测量值全部保留；积分与上一步命令清零。
                    previous["target"] = updated["target"]
                    previous["configured_target"] = updated["target"]
                    previous["follow_ratio"] = updated["follow_ratio"]
                    previous["integral"] = 0.0
                    previous["command_delta"] = 0.0
                    updated_states.append(previous)

                self._controllers = updated_controllers
                self._states = updated_states
                self._settings = {"update_s": float(update_s)}
                self._parameter_revision += 1
                self._last_parameter_update = time.time()
                self._error = None
                wake_event = self._wake_event

        # 如果控制线程正按旧周期休眠，立即叫醒，让新参数从下一轮开始生效。
        if wake_event is not None:
            wake_event.set()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """停止 PI 锁定。停止时不改 AO，保持最后输出。"""

        with self._lock:
            stop_event = self._stop_event
            wake_event = self._wake_event
            thread = self._thread
            if stop_event is not None:
                stop_event.set()
            if wake_event is not None:
                wake_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
                self._wake_event = None
                self._running = False

        return self.status()

    def status(self) -> dict[str, Any]:
        """返回 WebUI 需要显示的锁定状态。"""

        with self._lock:
            return {
                "running": self._running,
                "error": self._error,
                "settings": dict(self._settings),
                "controllers": [dict(controller) for controller in self._controllers],
                "states": [dict(state) for state in self._states],
                "iterations": self._iterations,
                "last_update": self._last_update,
                "parameter_revision": self._parameter_revision,
                "last_parameter_update": self._last_parameter_update,
                "last_stats_frame_id": self._last_stats_frame_id,
                "csv_file": None if self._csv_path is None else str(self._csv_path.resolve()),
                "records_written": self._records_written,
            }

    def _new_csv_path(self) -> Path | None:
        """为一次锁定建立独立 CSV；测试环境可以不提供输出目录。"""

        if self._output_dir is None:
            return None
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self._output_dir / f"power_lock_{stamp}.csv"

    def _set_error(self, message: str) -> None:
        """记录后台线程里可以恢复的错误，方便前端显示。"""

        with self._lock:
            self._error = message

    def _worker(
        self,
        stop_event: threading.Event,
        wake_event: threading.Event,
    ) -> None:
        """锁定线程主体。"""

        log_file = None
        log_writer: csv.DictWriter[str] | None = None
        try:
            if self._csv_path is not None:
                log_file = self._csv_path.open("w", newline="", encoding="utf-8")
                log_writer = csv.DictWriter(log_file, fieldnames=LOCK_LOG_FIELDS)
                log_writer.writeheader()
                log_file.flush()

            # 启动锁定时先写一次初始电压，确保软件状态和硬件状态一致。
            # cycle_lock 保证首次写入结束前，运行中更新接口不会插入中间。
            with self._cycle_lock:
                with self._lock:
                    initial_controllers = [dict(item) for item in self._controllers]
                for controller in initial_controllers:
                    self._daq.write_ao(
                        channel=controller["channel"],
                        value=controller["initial_voltage"],
                        min_val=controller["min_voltage"],
                        max_val=controller["max_voltage"],
                    )

            while not stop_event.is_set():
                trend_status = self._trend_logger.status()
                latest = trend_status.get("latest_stats") or {}
                if trend_status.get("running") is not True:
                    self._set_error("area trend logger is not running")
                    _sleep_until_stop_or_wake(
                        stop_event,
                        wake_event,
                        self._current_update_period(),
                    )
                    continue

                stats_frame_id = int(latest.get("frame_id", 0) or 0)
                if stats_frame_id <= 0:
                    self._set_error("面积慢漂记录还没有产生可用于锁定的新统计点")
                    _sleep_until_stop_or_wake(
                        stop_event,
                        wake_event,
                        self._current_update_period(),
                    )
                    continue

                with self._lock:
                    last_stats_frame_id = self._last_stats_frame_id
                    last_new_stats_monotonic = self._last_new_stats_monotonic
                    update_s_for_stale = float(self._settings.get("update_s", 1.0))

                if stats_frame_id < last_stats_frame_id:
                    raise RuntimeError(
                        "面积慢漂统计 frame_id 倒退，锁定已停止以避免使用错序数据："
                        f"last={last_stats_frame_id}, current={stats_frame_id}"
                    )
                if stats_frame_id == last_stats_frame_id:
                    # WebUI 的锁定检查周期可以快于慢漂记录频率。同一条统计值只能
                    # 使用一次，否则积分项会把一个 1 Hz 数据点错误地重复累计多次。
                    record_hz = float((trend_status.get("settings") or {}).get("record_hz", 1.0))
                    expected_period = 1.0 / max(record_hz, 1e-9)
                    stale_after = max(5.0, expected_period * 3.0, update_s_for_stale * 3.0)
                    if time.monotonic() - last_new_stats_monotonic > stale_after:
                        self._set_error(
                            f"面积慢漂统计超过 {stale_after:.1f} s 没有更新，AO 保持最后输出"
                        )
                    _sleep_until_stop_or_wake(
                        stop_event,
                        wake_event,
                        self._current_update_period(),
                    )
                    continue

                with self._cycle_lock:
                    with self._lock:
                        controllers = [dict(item) for item in self._controllers]
                        previous_states = [dict(item) for item in self._states]
                        update_s = float(self._settings.get("update_s", 1.0))
                        last_stats_timestamp = self._last_stats_timestamp

                    stats_timestamp = float(latest.get("unix_time", time.time()) or time.time())
                    dt = (
                        update_s
                        if last_stats_timestamp <= 0
                        else max(1e-6, stats_timestamp - last_stats_timestamp)
                    )

                    next_states: list[dict[str, Any]] = []
                    for index, controller in enumerate(controllers):
                        previous = previous_states[index] if index < len(previous_states) else {}
                        state = self._update_one_controller(controller, previous, latest, dt)
                        next_states.append(state)

                    if log_writer is not None:
                        for controller, state in zip(controllers, next_states, strict=True):
                            log_writer.writerow(_lock_log_row(latest, controller, state))
                        log_file.flush()

                    with self._lock:
                        self._states = next_states
                        self._iterations += 1
                        self._last_update = time.time()
                        self._last_stats_frame_id = stats_frame_id
                        self._last_stats_timestamp = stats_timestamp
                        self._last_new_stats_monotonic = time.monotonic()
                        self._records_written += len(next_states) if log_writer is not None else 0
                        self._error = None

                _sleep_until_stop_or_wake(stop_event, wake_event, update_s)

        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            if log_file is not None:
                log_file.close()
            with self._lock:
                self._running = False
                self._thread = None
                self._stop_event = None
                self._wake_event = None

    def _current_update_period(self) -> float:
        """线程安全地读取当前更新周期。"""

        with self._lock:
            return float(self._settings.get("update_s", 1.0))

    def _update_one_controller(
        self,
        controller: dict[str, Any],
        previous: dict[str, Any],
        latest: dict[str, Any],
        dt: float,
    ) -> dict[str, Any]:
        """根据某一路的反馈值更新一路 AO。"""

        field = controller["feedback_field"]
        measured = _read_feedback_value(latest, field)
        if measured is None:
            raise RuntimeError(f"feedback field {field!r} is not available")
        if not math.isfinite(measured):
            raise RuntimeError(f"feedback field {field!r} is not finite")

        target_mode = str(controller["target_mode"])
        configured_target = float(controller["target"])
        reference_field = str(controller["reference_field"])
        follow_ratio = float(controller["follow_ratio"])
        reference_value: float | None = None
        actual_ratio: float | None = None

        if target_mode == RATIO_TARGET_MODE:
            reference_value = _read_feedback_value(latest, reference_field)
            if reference_value is None or not math.isfinite(reference_value):
                raise RuntimeError(f"reference field {reference_field!r} is not available")
            if reference_value <= 1e-12:
                raise RuntimeError(
                    f"采样峰 {reference_field}={reference_value:.6e} 接近零或为负，"
                    "比例锁定已停止，AO 保持最后输出"
                )
            target = follow_ratio * reference_value
            actual_ratio = measured / reference_value
        else:
            target = configured_target

        if abs(target) < 1e-30:
            raise RuntimeError(f"{controller['name']} target is too close to zero")

        old_voltage = float(previous.get("voltage", controller["initial_voltage"]))
        integral = float(previous.get("integral", 0.0))
        measurement_revision = int(latest.get("window_revision", 0) or 0)
        previous_measurement_revision = int(
            previous.get("measurement_revision", measurement_revision) or 0
        )
        if measurement_revision != previous_measurement_revision:
            # 手动改变面积边界后，反馈量的物理定义已经变化。旧积分不能继续沿用，
            # 否则可能在新窗口刚生效时推动 AO 突然跳变。
            integral = 0.0
        relative_error = (target - measured) / abs(target)
        integral += relative_error * dt

        direction = float(controller["direction"])
        raw_delta = direction * (
            float(controller["kp"]) * relative_error
            + float(controller["ki"]) * integral
        )
        step = _clamp(raw_delta, -abs(controller["max_step_v"]), abs(controller["max_step_v"]))
        requested_voltage = old_voltage + step
        voltage = _clamp(requested_voltage, controller["min_voltage"], controller["max_voltage"])
        limited = abs(voltage - requested_voltage) > 1e-12

        # 如果已经顶到边界，并且积分还在继续把输出往边界外推，就撤销本次积分，
        # 这是最简单的抗积分饱和处理。
        if limited:
            integral -= relative_error * dt

        self._daq.write_ao(
            channel=controller["channel"],
            value=voltage,
            min_val=controller["min_voltage"],
            max_val=controller["max_voltage"],
        )

        return {
            "name": controller["name"],
            "channel": controller["channel"],
            "feedback_field": field,
            "target_mode": target_mode,
            "reference_field": reference_field,
            "reference_value": reference_value,
            "follow_ratio": follow_ratio,
            "actual_ratio": actual_ratio,
            "configured_target": configured_target,
            "target": target,
            "measured": measured,
            "relative_error": relative_error,
            "voltage": voltage,
            "command_delta": voltage - old_voltage,
            "integral": integral,
            "measurement_revision": measurement_revision,
            "stats_frame_id": int(latest.get("frame_id", 0) or 0),
            "limited": limited,
        }


def _validate_controller(controller: dict[str, Any]) -> dict[str, Any] | None:
    """校验并标准化一路控制参数。"""

    if controller.get("enabled", True) is False:
        return None

    normalized = {
        "name": str(controller.get("name", "")),
        "channel": str(controller.get("channel", "")).strip(),
        "feedback_field": str(controller.get("feedback_field", "")).strip(),
        "target_mode": str(controller.get("target_mode", FIXED_TARGET_MODE)).strip(),
        "reference_field": str(controller.get("reference_field", "")).strip(),
        "follow_ratio": float(controller.get("follow_ratio", 1.0)),
        "target": float(controller.get("target")),
        "initial_voltage": float(controller.get("initial_voltage")),
        "min_voltage": float(controller.get("min_voltage")),
        "max_voltage": float(controller.get("max_voltage")),
        "direction": float(controller.get("direction")),
        "max_step_v": float(controller.get("max_step_v")),
        "kp": float(controller.get("kp")),
        "ki": float(controller.get("ki")),
    }

    name = normalized["name"] or normalized["channel"] or "controller"
    for field in (
        "target",
        "follow_ratio",
        "initial_voltage",
        "min_voltage",
        "max_voltage",
        "direction",
        "max_step_v",
        "kp",
        "ki",
    ):
        if not math.isfinite(normalized[field]):
            raise ValueError(f"{name} {field} must be finite")
    if not normalized["channel"]:
        raise ValueError(f"{name} channel is empty")
    if not normalized["feedback_field"]:
        raise ValueError(f"{name} feedback_field is empty")
    if normalized["target_mode"] not in (FIXED_TARGET_MODE, RATIO_TARGET_MODE):
        raise ValueError(f"{name} target_mode must be fixed or follow_ratio")
    if normalized["min_voltage"] >= normalized["max_voltage"]:
        raise ValueError(f"{name} min_voltage must be smaller than max_voltage")
    if normalized["initial_voltage"] < normalized["min_voltage"]:
        raise ValueError(f"{name} initial_voltage is below min_voltage")
    if normalized["initial_voltage"] > normalized["max_voltage"]:
        raise ValueError(f"{name} initial_voltage is above max_voltage")
    if normalized["direction"] not in (-1.0, 1.0):
        raise ValueError(f"{name} direction must be -1 or 1")
    if normalized["max_step_v"] < 0:
        raise ValueError(f"{name} max_step_v must be >= 0")
    if normalized["target_mode"] == FIXED_TARGET_MODE and abs(normalized["target"]) < 1e-30:
        raise ValueError(f"{name} target is too close to zero")
    if normalized["target_mode"] == RATIO_TARGET_MODE:
        expected_reference = RATIO_REFERENCE_FIELDS.get(normalized["feedback_field"])
        if expected_reference is None:
            raise ValueError(
                f"{name} feedback_field {normalized['feedback_field']!r} "
                "does not support ratio locking"
            )
        if normalized["reference_field"] != expected_reference:
            raise ValueError(
                f"{name} reference_field must be {expected_reference!r} "
                f"when feedback_field is {normalized['feedback_field']!r}"
            )
        if normalized["follow_ratio"] <= 0:
            raise ValueError(f"{name} follow_ratio must be > 0")
    return normalized


def _validate_controllers(controllers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验控制器列表，并移除前端明确关闭的回路。"""

    if not isinstance(controllers, list):
        raise ValueError("controllers must be a list")
    validated: list[dict[str, Any]] = []
    for controller in controllers:
        if not isinstance(controller, dict):
            raise ValueError("each controller must be an object")
        normalized = _validate_controller(controller)
        if normalized is not None:
            validated.append(normalized)
    if not validated:
        raise ValueError("controllers must contain at least one enabled controller")
    return validated


def _validate_runtime_identity(
    current: dict[str, Any],
    requested: dict[str, Any],
) -> None:
    """拒绝运行中改变控制回路的硬件身份或物理定义。"""

    immutable_fields = (
        "name",
        "channel",
        "feedback_field",
        "target_mode",
        "reference_field",
        "initial_voltage",
        "min_voltage",
        "max_voltage",
        "direction",
    )
    for field in immutable_fields:
        if requested[field] != current[field]:
            name = current.get("name") or current.get("channel") or "controller"
            raise ValueError(f"{name} 的 {field} 不能在锁定运行中修改，请先停止锁定")


def _read_feedback_value(latest: dict[str, Any], field: str) -> float | None:
    """读取反馈字段。EMA 关闭时自动退回到对应的均值字段。"""

    value = latest.get(field)
    if value is None:
        fallback = {
            "area_ema": "area_mean",
            "area2_ema": "area2_mean",
            "area_sum_ema": "area_sum_mean",
            # Top 与面积沿用相同约定：EMA alpha=0 时，锁定器退回到
            # 最近 N 帧的 Top 均值，而不是因为滤波关闭而失去反馈。
            "top_ema": "top_mean",
            "top2_ema": "top2_mean",
            "peak_height_ema": "peak_height_mean",
            "peak2_height_ema": "peak2_height_mean",
        }.get(field)
        if fallback is not None:
            value = latest.get(fallback)
    if value is None:
        return None
    return float(value)


def _lock_log_row(
    latest: dict[str, Any],
    controller: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """把一次实际 AO 更新整理成便于离线分析的 CSV 行。"""

    timestamp = float(latest.get("unix_time", time.time()) or time.time())
    return {
        "iso_time": datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds"),
        "unix_time": timestamp,
        "frame_id": int(latest.get("frame_id", 0) or 0),
        "window_revision": int(latest.get("window_revision", 0) or 0),
        "controller_name": controller["name"],
        "channel": controller["channel"],
        "target_mode": controller["target_mode"],
        "feedback_field": controller["feedback_field"],
        "reference_field": controller["reference_field"],
        "measured": state.get("measured"),
        "reference_value": state.get("reference_value"),
        "configured_target": state.get("configured_target"),
        "dynamic_target": state.get("target"),
        "follow_ratio": state.get("follow_ratio"),
        "actual_ratio": state.get("actual_ratio"),
        "relative_error": state.get("relative_error"),
        "voltage": state.get("voltage"),
        "command_delta": state.get("command_delta"),
        "integral": state.get("integral"),
        "limited": state.get("limited"),
        "kp": controller["kp"],
        "ki": controller["ki"],
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sleep_until_stop_or_wake(
    stop_event: threading.Event,
    wake_event: threading.Event,
    seconds: float,
) -> bool:
    """等待下一轮；停止或参数更新都可以立即唤醒控制线程。"""

    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if wake_event.wait(timeout=min(0.05, remaining)):
            wake_event.clear()
            return stop_event.is_set()
    return True
