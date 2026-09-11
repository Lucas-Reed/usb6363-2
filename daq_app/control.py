from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass
class PiController:
    target: float
    kp: float
    ki: float = 0.0
    bias: float = 0.0
    minimum: float = -2.5
    maximum: float = 2.5
    integral: float = 0.0
    output: float = 0.0

    def update(self, measured: float, dt: float) -> float:
        if dt <= 0:
            raise ValueError("dt must be positive")
        error = (self.target - measured) / self.target if self.target else self.target - measured
        self.integral += error * dt
        self.output = min(self.maximum, max(self.minimum, self.bias + self.kp * error + self.ki * self.integral))
        return self.output


@dataclass(frozen=True)
class Lease:
    owner: str
    channels: tuple[str, ...]
    token: str


class AoManager:
    """Hardware-free AO ownership; an adapter performs the real write later."""

    def __init__(self):
        self._lease: Lease | None = None

    def acquire(self, owner: str, channels: tuple[str, ...]) -> Lease:
        if self._lease is not None:
            raise RuntimeError(f"AO owned by {self._lease.owner}")
        self._lease = Lease(owner, channels, uuid4().hex)
        return self._lease

    def release(self, lease: Lease) -> None:
        self._check(lease)
        self._lease = None

    def proposal(self, lease: Lease, values: dict[str, float]) -> dict[str, object]:
        self._check(lease)
        if set(values) - set(lease.channels):
            raise ValueError("proposal contains channel outside lease")
        return {"owner": lease.owner, "token": lease.token, "values": dict(values)}

    def _check(self, lease):
        if self._lease != lease:
            raise RuntimeError("invalid or expired AO lease")
