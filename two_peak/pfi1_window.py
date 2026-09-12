"""PFI1 finite-window acquisition coordinator.

Uses the existing Usb6363Client capture path and does not alter continuous AI.
"""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Pfi1WindowConfig:
    channel: str
    samples: int
    rate_per_channel: float
    delay_seconds: float = 0.0
    timeout: float = 10.0


class Pfi1WindowCapture:
    def __init__(self, capture: Callable[..., Any], config: Pfi1WindowConfig):
        if config.samples <= 0 or config.rate_per_channel <= 0:
            raise ValueError("samples and rate_per_channel must be positive")
        self._capture = capture
        self.config = config

    def capture_once(self) -> Any:
        return self._capture(
            channels=[self.config.channel],
            samples=self.config.samples,
            rate_per_channel=self.config.rate_per_channel,
            trigger_enabled=True,
            trigger_source="PFI1",
            trigger_edge="FALLING",
            timeout=self.config.timeout,
        )

    @staticmethod
    def should_exclude_double_peak_frame(*, pfi1_triggered: bool) -> bool:
        return bool(pfi1_triggered)


@dataclass(frozen=True)
class PulseAnalysis:
    values: tuple[float, ...]
    pulse_values: tuple[float, ...]
    mean_value: float
    pulse_mean: float
    rising_index: int | None
    falling_index: int | None
    has_pulse: bool


class ExponentialMovingAverage:
    def __init__(self, alpha: float):
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = float(alpha)
        self.value: float | None = None

    def update(self, value: float) -> float:
        value = float(value)
        self.value = value if self.value is None else self.alpha * value + (1.0 - self.alpha) * self.value
        return self.value


def analyze_pulse(values: Any, *, threshold: float, hysteresis: float = 0.0) -> PulseAnalysis:
    """Detect a positive pulse with fixed threshold and return its mean segment."""
    data = tuple(float(v) for v in values)
    if not data:
        return PulseAnalysis((), (), 0.0, 0.0, None, None, False)
    rising = next((i for i, v in enumerate(data) if v >= threshold), None)
    falling = None
    if rising is not None:
        off_threshold = threshold - abs(float(hysteresis))
        falling = next((i for i in range(rising + 1, len(data)) if data[i] < off_threshold), None)
        end = falling if falling is not None else len(data)
        pulse = data[rising:end]
    else:
        pulse = ()
    mean = sum(data) / len(data)
    pulse_mean = sum(pulse) / len(pulse) if pulse else mean
    return PulseAnalysis(data, tuple(pulse), mean, pulse_mean, rising, falling, bool(pulse))


class PiFeedback:
    """Same incremental PI semantics as the existing power lock controller."""
    def __init__(self, *, target: float, initial_voltage: float, min_voltage: float,
                 max_voltage: float, direction: int = 1, max_step_v: float = 0.01,
                 kp: float = 0.0, ki: float = 0.0):
        if min_voltage >= max_voltage or direction not in (-1, 1):
            raise ValueError("invalid voltage range or direction")
        self.target = float(target)
        self.voltage = min(max(float(initial_voltage), min_voltage), max_voltage)
        self.min_voltage, self.max_voltage = float(min_voltage), float(max_voltage)
        self.direction, self.max_step_v = direction, abs(float(max_step_v))
        self.kp, self.ki, self.integral = float(kp), float(ki), 0.0

    def update(self, measured: float, dt: float) -> float:
        if abs(self.target) <= 1e-12:
            return self.voltage
        error = (self.target - float(measured)) / abs(self.target)
        increment = error * float(dt)
        self.integral += increment
        delta = self.direction * (self.kp * error + self.ki * self.integral)
        delta = max(-self.max_step_v, min(self.max_step_v, delta))
        new_voltage = max(self.min_voltage, min(self.max_voltage, self.voltage + delta))
        if new_voltage != self.voltage + delta:
            self.integral -= increment
        self.voltage = new_voltage
        return self.voltage
