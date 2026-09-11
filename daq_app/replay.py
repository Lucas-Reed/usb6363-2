from __future__ import annotations

import numpy as np

from .contracts import BlockMeta


def synthetic_blocks(session, values, block_size):
    values = np.asarray(values, dtype=float)
    if values.ndim != 2 or values.shape[0] != len(session.channels):
        raise ValueError("values must be (channels, samples)")
    for seq, start in enumerate(range(0, values.shape[1], block_size)):
        chunk = values[:, start:start + block_size]
        yield BlockMeta(session.session_id, seq, start, chunk.shape[1], session.rate_hz), chunk
