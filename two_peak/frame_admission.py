"""One-frame delayed admission for PFI1-protected processing.

Frames are never exposed to business consumers until the following PFI0
boundary has arrived and the PFI1 counter interval is known.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingFrame:
    frame_id: int
    frame: Any
    pfi1_count_at_start: int
    pfi1_count_at_end: int | None = None


class FrameAdmissionGate:
    """Delay each frame by one frame boundary before accepting it."""

    def __init__(self, *, enabled: bool = True):
        self.enabled = bool(enabled)
        self._pending: PendingFrame | None = None
        self.rejected_frame_ids: list[int] = []
        self.rejected_frames: list[Any] = []

    def push(self, frame: Any, *, pfi1_count: int) -> list[Any]:
        """Push a frame at its start/boundary and return newly accepted frames.

        The first frame is held. On the next boundary, the prior frame is
        accepted only when its PFI1 counter interval did not change.
        """
        frame_id = int(frame["frame_id"])
        count = int(pfi1_count)
        accepted: list[Any] = []
        if not self.enabled:
            return [frame]
        if self._pending is not None:
            previous = self._pending
            if count == previous.pfi1_count_at_start:
                accepted.append(previous.frame)
            else:
                self.rejected_frame_ids.append(previous.frame_id)
                self.rejected_frames.append(previous.frame)
        self._pending = PendingFrame(frame_id, frame, count)
        return accepted

    def flush(self) -> list[Any]:
        """Discard the final unconfirmed frame on shutdown."""
        if self._pending is not None:
            self.rejected_frame_ids.append(self._pending.frame_id)
            self._pending = None
        return []
