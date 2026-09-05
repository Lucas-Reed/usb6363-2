"""双峰锁定的 PI 控制器。

这个文件只根据“目标值、测量值、上次 AO”计算新的 AO 建议值。
它不写采集卡，也不关心网页。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque


def clamp(value: float, low: float, high: float) -> float:
    """把 value 限制在 [low, high] 范围内。"""

    return max(low, min(high, value))


def relative_error(setpoint: float, measured: float, min_setpoint: float = 1e-9) -> float:
    """计算相对误差：(目标值 - 测量值) / 目标值。"""

    safe_setpoint = max(abs(float(setpoint)), min_setpoint)
    return (float(setpoint) - float(measured)) / safe_setpoint


@dataclass
class RelativePiLoop:
    """一个基于相对误差的 PI 环路。"""

    setpoint: float
    kp: float
    ki: float
    bias: float
    ao_min: float
    ao_max: float
    max_step_per_frame: float
    inverted: bool = False
    deadband_rel: float = 0.01
    error_average_frames: int = 20
    integral: float = 0.0
    last_ao: float | None = None
    error_history: deque[float] = field(default_factory=deque)

    def reset(self, start_ao: float | None = None) -> None:
        """清空积分和误差历史。"""

        self.integral = 0.0
        self.error_history.clear()
        self.last_ao = self.bias if start_ao is None else float(start_ao)

    def update(self, measured: float, dt: float, allow_output_update: bool = True) -> dict[str, float]:
        """根据一次测量值计算新的 AO。

        inverted=True 表示物理关系反向：误差为正时 AO 应该下降。
        """

        if self.last_ao is None:
            self.last_ao = self.bias

        raw_rel = relative_error(self.setpoint, measured)
        self.error_history.append(raw_rel)
        while len(self.error_history) > max(1, int(self.error_average_frames)):
            self.error_history.popleft()

        slow_rel = sum(self.error_history) / len(self.error_history)
        control_rel = 0.0 if abs(slow_rel) < self.deadband_rel else slow_rel

        if allow_output_update:
            direction = -1.0 if self.inverted else 1.0
            self.integral += control_rel * float(dt)
            raw_ao = self.bias + direction * (self.kp * control_rel + self.ki * self.integral)
            limited_ao = clamp(raw_ao, self.ao_min, self.ao_max)
            step = clamp(
                limited_ao - self.last_ao,
                -abs(self.max_step_per_frame),
                abs(self.max_step_per_frame),
            )
            self.last_ao = clamp(self.last_ao + step, self.ao_min, self.ao_max)

        return {
            "ao": float(self.last_ao),
            "relative_error": float(raw_rel),
            "relative_error_slow": float(slow_rel),
            "control_relative_error": float(control_rel),
            "integral": float(self.integral),
        }

