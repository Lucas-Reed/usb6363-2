from __future__ import annotations

import numpy as np

from .contracts import BlockMeta, SessionInfo


class BufferError(RuntimeError):
    pass


class NotYetAvailable(BufferError):
    pass


class Expired(BufferError):
    pass


class SessionMismatch(BufferError):
    pass


class SampleBuffer:
    """Bounded per-channel sample store; ranges are [start, end)."""

    def __init__(self, session: SessionInfo, capacity: int):
        if capacity <= 0 or not session.channels or session.rate_hz <= 0:
            raise ValueError("invalid session or capacity")
        self.session = session
        self.capacity = int(capacity)
        self._data = np.empty((len(session.channels), self.capacity), dtype=np.float64)
        self._oldest = 0
        self._end = 0

    @property
    def oldest_sample(self) -> int:
        return self._oldest

    @property
    def end_sample(self) -> int:
        return self._end

    def bounds(self) -> tuple[int, int]:
        return self._oldest, self._end

    def append(self, meta: BlockMeta, values: np.ndarray) -> None:
        array = np.asarray(values, dtype=np.float64)
        if meta.session_id != self.session.session_id:
            raise SessionMismatch("block belongs to another session")
        if array.shape != (len(self.session.channels), meta.sample_count):
            raise ValueError(f"expected {(len(self.session.channels), meta.sample_count)}, got {array.shape}")
        if meta.start_sample != self._end:
            raise BufferError(f"non-contiguous block: expected {self._end}, got {meta.start_sample}")
        if meta.quality.value != "valid":
            raise BufferError(f"cannot append invalid block: {meta.quality}")
        for source_start in range(0, meta.sample_count, self.capacity):
            chunk = array[:, source_start:source_start + self.capacity]
            positions = (self._end + source_start + np.arange(chunk.shape[1])) % self.capacity
            self._data[:, positions] = chunk
        self._end += meta.sample_count
        self._oldest = max(0, self._end - self.capacity)

    def copy_range(self, session_id, start: int, end: int) -> np.ndarray:
        if session_id != self.session.session_id:
            raise SessionMismatch("wrong session")
        if end <= start:
            raise ValueError("end must be greater than start")
        if start < self._oldest:
            raise Expired(f"range starts at {start}; oldest is {self._oldest}")
        if end > self._end:
            raise NotYetAvailable(f"range ends at {end}; available end is {self._end}")
        result = np.empty((self._data.shape[0], end - start), dtype=np.float64)
        positions = np.arange(start, end) % self.capacity
        result[:] = self._data[:, positions]
        return result
