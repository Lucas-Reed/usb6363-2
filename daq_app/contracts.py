from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping
from uuid import UUID


class Quality(str, Enum):
    VALID = "valid"
    GAP = "gap"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True)
class SessionInfo:
    session_id: UUID
    channels: tuple[str, ...]
    rate_hz: float
    revision: int = 1


@dataclass(frozen=True)
class BlockMeta:
    session_id: UUID
    block_seq: int
    start_sample: int
    sample_count: int
    rate_hz: float
    quality: Quality = Quality.VALID


@dataclass(frozen=True)
class TriggerEvent:
    session_id: UUID
    source_id: str
    source_seq: int
    anchor_sample: int
    quality: Quality = Quality.VALID


@dataclass(frozen=True)
class Window:
    session_id: UUID
    window_id: str
    start_sample: int
    end_sample: int
    rate_hz: float
    channels: tuple[str, ...]
    values: object
    quality: Quality = Quality.VALID
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Measurement:
    session_id: UUID
    measurement_id: str
    window_id: str
    values: Mapping[str, float]
    start_sample: int
    end_sample: int
    rate_hz: float
    valid: bool = True
    reason: str | None = None
