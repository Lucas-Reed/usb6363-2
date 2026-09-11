"""Offline EOM carrier/AOM peak identification helpers.

This module is deliberately independent of acquisition and locking.
"""
from dataclasses import dataclass
from typing import Iterable, Optional
import math


@dataclass(frozen=True)
class PeakCandidate:
    index: int
    height: float
    prominence: float
    width_samples: float = 0.0


@dataclass(frozen=True)
class SpacingModel:
    nominal_samples: float
    tolerance_samples: float
    revision: int = 0


@dataclass(frozen=True)
class IdentifiedPair:
    carrier_index: int
    aom_index: int
    spacing_samples: float
    confidence: float
    valid: bool
    reason: str = ""


def find_peak_candidates(signal: Iterable[float], *, min_distance: int = 1,
                         prominence_threshold: float = 0.0) -> list[PeakCandidate]:
    values = list(signal)
    if len(values) < 3:
        return []
    candidates = []
    last = -min_distance
    for i in range(1, len(values) - 1):
        if i - last < min_distance or values[i] <= values[i - 1] or values[i] < values[i + 1]:
            continue
        prominence = values[i] - max(min(values[:i + 1]), min(values[i:]))
        if prominence < prominence_threshold:
            continue
        candidates.append(PeakCandidate(i, float(values[i]), float(prominence)))
        last = i
    return candidates


def match_eom_aom_pair(candidates: Iterable[PeakCandidate], model: SpacingModel,
                       *, previous_pair: Optional[IdentifiedPair] = None) -> IdentifiedPair:
    items = list(candidates)
    best = None
    for left in items:
        for right in items:
            if right.index <= left.index:
                continue
            spacing = right.index - left.index
            error = abs(spacing - model.nominal_samples)
            if error <= model.tolerance_samples and (best is None or error < best[0]):
                best = (error, left, right, spacing)
    if best is None:
        return IdentifiedPair(0, 0, 0.0, 0.0, False, "PAIR_NOT_CONFIDENT")
    error, left, right, spacing = best
    confidence = max(0.0, 1.0 - error / max(model.tolerance_samples, 1e-9))
    return IdentifiedPair(left.index, right.index, float(spacing), confidence, True)
