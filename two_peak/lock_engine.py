"""双峰锁定的一帧处理引擎。

这个文件是算法层的“编排者”：
    输入：一帧 ai0/ai1 波形
    输出：峰位置、峰值、误差、建议 AO

它不直接读取 USB-6363，也不直接写 AO。
真正写 AO 的程序必须通过 usb6363_client 调用底层服务。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import TwoPeakSettings
from .pi import RelativePiLoop
from .signal import PeakMeasurement, locate_and_measure_two_peaks


@dataclass
class LockFrameResult:
    """处理一帧后的结果。"""

    peak_measurements: list[PeakMeasurement]
    ao0: float
    ao1: float
    rel_error0: float
    rel_error1: float
    rel_error0_slow: float
    rel_error1_slow: float
    control_enabled: bool


class TwoPeakLockEngine:
    """双峰锁定算法引擎。

    这个类只保存算法状态，例如 PI 积分项和当前峰位。
    """

    def __init__(self, settings: TwoPeakSettings | None = None) -> None:
        self.settings = TwoPeakSettings.defaults() if settings is None else settings
        self.peak_indices = list(self.settings.peaks.default_indices)
        self.frame_count = 0

        self.loop0 = RelativePiLoop(
            setpoint=self.settings.loop0.setpoint,
            kp=self.settings.loop0.kp,
            ki=self.settings.loop0.ki,
            bias=self.settings.ao.bias_ao0,
            ao_min=self.settings.ao.ao_min,
            ao_max=self.settings.ao.ao_max,
            max_step_per_frame=self.settings.ao.max_step_per_frame,
            inverted=self.settings.loop0.inverted,
            deadband_rel=self.settings.filters.deadband_rel,
            error_average_frames=self.settings.filters.error_lp_frames,
        )
        self.loop1 = RelativePiLoop(
            setpoint=self.settings.loop1.setpoint,
            kp=self.settings.loop1.kp,
            ki=self.settings.loop1.ki,
            bias=self.settings.ao.bias_ao1,
            ao_min=self.settings.ao.ao_min,
            ao_max=self.settings.ao.ao_max,
            max_step_per_frame=self.settings.ao.max_step_per_frame,
            inverted=self.settings.loop1.inverted,
            deadband_rel=self.settings.filters.deadband_rel,
            error_average_frames=self.settings.filters.error_lp_frames,
        )
        self.loop0.reset()
        self.loop1.reset()

    def set_manual_peak_indices(self, peak_indices: list[int]) -> None:
        """手动设置两个峰的位置。"""

        if len(peak_indices) != 2:
            raise ValueError("peak_indices must contain exactly two indices")
        self.peak_indices = [int(peak_indices[0]), int(peak_indices[1])]

    def process_frame(
        self,
        ai0: list[float] | np.ndarray,
        ai1: list[float] | np.ndarray | None = None,
        control_enabled: bool = False,
    ) -> LockFrameResult:
        """处理一帧数据。

        ai1 目前先保留参数位置，后面的自动双峰识别会用到 PZT 监视信号。
        """

        _ = ai1
        settings = self.settings
        self.frame_count += 1

        _, measurements = locate_and_measure_two_peaks(
            ai0=np.asarray(ai0, dtype=float),
            peak_indices=self.peak_indices,
            smooth_window=settings.peaks.smooth_window,
            search_window_half=settings.peaks.window_half,
            measure_half=settings.peaks.peak_avg_half,
            mode=settings.peaks.peak_mode,
        )

        dt = settings.daq.samples_per_frame / settings.daq.sample_rate
        should_update = (
            control_enabled
            and self.frame_count % max(1, settings.filters.control_update_every) == 0
        )
        loop0 = self.loop0.update(measurements[0].value, dt, allow_output_update=should_update)
        loop1 = self.loop1.update(measurements[1].value, dt, allow_output_update=should_update)

        return LockFrameResult(
            peak_measurements=measurements,
            ao0=loop0["ao"],
            ao1=loop1["ao"],
            rel_error0=loop0["relative_error"],
            rel_error1=loop1["relative_error"],
            rel_error0_slow=loop0["relative_error_slow"],
            rel_error1_slow=loop1["relative_error_slow"],
            control_enabled=control_enabled,
        )

