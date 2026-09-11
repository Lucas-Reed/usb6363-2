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
            timeout=self.config.timeout,
        )

    @staticmethod
    def should_exclude_double_peak_frame(*, pfi1_triggered: bool) -> bool:
        return bool(pfi1_triggered)
