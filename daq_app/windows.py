from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .contracts import TriggerEvent, Window
from .sample_buffer import Expired, NotYetAvailable, SampleBuffer


@dataclass(frozen=True)
class WindowRoute:
    route_id: str
    source_id: str | None
    pre_s: float
    post_s: float
    channels: tuple[str, ...]


class WindowScheduler:
    def __init__(self, session, store: SampleBuffer):
        self.session = session
        self.store = store
        self._pending: list[tuple[TriggerEvent, WindowRoute, int, int]] = []

    def add_trigger(self, event: TriggerEvent, route: WindowRoute) -> None:
        if event.session_id != self.session.session_id:
            return
        pre = round(route.pre_s * self.session.rate_hz)
        post = round(route.post_s * self.session.rate_hz)
        if pre < 0 or post <= 0:
            raise ValueError("window durations must be pre >= 0 and post > 0")
        self._pending.append((event, route, event.anchor_sample - pre, event.anchor_sample + post))

    def poll(self) -> list[Window]:
        completed = []
        pending = []
        for event, route, start, end in self._pending:
            try:
                values = self.store.copy_range(event.session_id, start, end)
            except NotYetAvailable:
                pending.append((event, route, start, end))
                continue
            except Expired:
                continue
            indices = [self.session.channels.index(ch) for ch in route.channels]
            completed.append(Window(
                session_id=self.session.session_id,
                window_id=f"{route.route_id}:{event.source_seq}",
                start_sample=start,
                end_sample=end,
                rate_hz=self.session.rate_hz,
                channels=route.channels,
                values=values[indices, :],
                metadata={"source_id": event.source_id, "source_seq": event.source_seq},
            ))
        self._pending = pending
        return completed

    def add_periodic(self, route: WindowRoute, next_end: int) -> tuple[int, int]:
        width = round((route.pre_s + route.post_s) * self.session.rate_hz)
        if width <= 0:
            raise ValueError("periodic window must be positive")
        return next_end - width, next_end
