"""Configurable PFI0/PFI1 counter monitor.

The monitor is hardware-agnostic: ``read_count(line)`` supplies a cumulative
edge count. PFI1 is sampled more frequently than PFI0 by configuration.
"""
from dataclasses import dataclass
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class PfiMonitorConfig:
    pfi0_poll_interval_s: float = 0.05
    pfi1_poll_interval_s: float = 0.01

    def __post_init__(self) -> None:
        if self.pfi0_poll_interval_s <= 0 or self.pfi1_poll_interval_s <= 0:
            raise ValueError("poll intervals must be positive")
        if self.pfi1_poll_interval_s >= self.pfi0_poll_interval_s:
            raise ValueError("PFI1 must be polled more frequently than PFI0")


@dataclass(frozen=True)
class PfiMonitorEvent:
    timestamp: float
    pfi0_count: int
    pfi1_count: int
    pfi0_changed: bool
    pfi1_changed: bool


class PfiCounterMonitor:
    def __init__(self, read_count: Callable[[str], int],
                 config: PfiMonitorConfig = PfiMonitorConfig(),
                 on_event: Callable[[PfiMonitorEvent], None] | None = None):
        self.read_count = read_count
        self.config = config
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last0: int | None = None
        self._last1: int | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("PFI monitor already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pfi-counter-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        next0 = next1 = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            changed0 = changed1 = False
            if now >= next0:
                count0 = int(self.read_count("PFI0"))
                changed0 = self._last0 is not None and count0 != self._last0
                self._last0 = count0
                next0 = now + self.config.pfi0_poll_interval_s
            else:
                count0 = self._last0 if self._last0 is not None else 0
            if now >= next1:
                count1 = int(self.read_count("PFI1"))
                changed1 = self._last1 is not None and count1 != self._last1
                self._last1 = count1
                next1 = now + self.config.pfi1_poll_interval_s
            else:
                count1 = self._last1 if self._last1 is not None else 0
            if changed0 or changed1:
                if self.on_event:
                    self.on_event(PfiMonitorEvent(time.time(), count0, count1, changed0, changed1))
            self._stop.wait(min(next0, next1) - time.monotonic())
