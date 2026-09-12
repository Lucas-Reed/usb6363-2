"""Coordinate PFI counter observations with one-frame-delayed admission."""
from dataclasses import dataclass
from typing import Any, Callable

from .frame_admission import FrameAdmissionGate
from .pfi_monitor import PfiMonitorEvent


@dataclass(frozen=True)
class FrameDecision:
    frame_id: int
    accepted: bool
    pfi1_count_start: int
    pfi1_count_boundary: int
    reason: str


class PfiFrameCoordinator:
    """Keep frame admission separate from signal processing and locking."""

    def __init__(self, on_accepted: Callable[[Any], None] | None = None,
                 on_rejected: Callable[[Any, str], None] | None = None,
                 enabled: bool = False):
        self.gate = FrameAdmissionGate(enabled=enabled)
        self.on_accepted = on_accepted
        self.on_rejected = on_rejected
        self.latest_pfi1_count = 0
        self.decisions: list[FrameDecision] = []

    def on_counter_event(self, event: PfiMonitorEvent) -> None:
        self.latest_pfi1_count = event.pfi1_count

    def submit_frame(self, frame: Any) -> list[Any]:
        accepted = self.gate.push(frame, pfi1_count=self.latest_pfi1_count)
        for item in accepted:
            if self.on_accepted:
                self.on_accepted(item)
        if self.on_rejected and self.gate.rejected_frames:
            self.on_rejected(self.gate.rejected_frames[-1], "PFI1_COUNT_CHANGED")
        return accepted

    def flush(self) -> None:
        self.gate.flush()
