from __future__ import annotations

import numpy as np

from .contracts import Measurement, Window


def auto_identify_two_peaks_placeholder(*args, **kwargs):
    raise NotImplementedError("automatic two-peak identification requires reviewed physical model")


def _smooth(values, width):
    values = np.asarray(values, dtype=float)
    if width < 3 or values.size < width:
        return values.copy()
    return np.convolve(values, np.ones(width) / width, mode="same")


class TwoPeakProcessor:
    def __init__(self, channel, centers, search_half=20, smooth_window=25, measure_half=5, mode="height"):
        if len(centers) != 2:
            raise ValueError("exactly two peak centers are required")
        self.channel = channel
        self.centers = tuple(int(x) for x in centers)
        self.search_half = int(search_half)
        self.smooth_window = int(smooth_window)
        self.measure_half = int(measure_half)
        self.mode = mode

    def process(self, window: Window) -> Measurement:
        signal = np.asarray(window.values[window.channels.index(self.channel)], dtype=float)
        smooth = _smooth(signal, self.smooth_window)
        values = {}
        for name, center in zip(("peak_a", "peak_b"), self.centers):
            left = max(0, center - self.search_half)
            right = min(smooth.size, center + self.search_half + 1)
            if left >= right:
                raise ValueError("peak search window is outside the supplied window")
            index = left + int(np.argmax(smooth[left:right]))
            lo = max(0, index - self.measure_half)
            hi = min(signal.size, index + self.measure_half + 1)
            sample = signal[lo:hi]
            value = float(np.mean(sample)) if self.mode == "height" else float(np.trapezoid(sample))
            values[name] = value
            values[f"{name}_index"] = float(index)
        return Measurement(window.session_id, f"two-peak:{window.window_id}", window.window_id,
                           values, window.start_sample, window.end_sample, window.rate_hz)


class SlowDriftProcessor:
    def __init__(self, channel, ema_alpha=0.15):
        self.channel = channel
        self.alpha = float(ema_alpha)
        self.ema = None

    def process(self, window: Window) -> Measurement:
        values = np.asarray(window.values[window.channels.index(self.channel)], dtype=float)
        mean = float(np.mean(values))
        self.ema = mean if self.ema is None else self.alpha * mean + (1 - self.alpha) * self.ema
        result = {"mean": mean, "std": float(np.std(values)), "ema": float(self.ema)}
        return Measurement(window.session_id, f"slow-drift:{window.window_id}", window.window_id,
                           result, window.start_sample, window.end_sample, window.rate_hz)
