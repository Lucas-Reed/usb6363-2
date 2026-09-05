"""双峰锁定的参数定义。

实验室版本的原则：
    只要这里出现的参数，将来都尽量在 WebUI 里可调。

这样做的好处是：参数集中在一个地方，不会散落在很多文件里。
后面做 WebUI 时，可以直接读取这些 dataclass 的字段作为默认值。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class DaqSettings:
    """采集卡和 AI 采样相关参数。"""

    ai_channels: list[str]
    ao_channels: list[str]
    sample_rate: float = 50_000.0
    samples_per_frame: int = 5000
    terminal_config: str = "DIFF"
    ai_min_val: float = -5.0
    ai_max_val: float = 5.0
    timeout: float = 10.0


@dataclass
class AoSettings:
    """AO 输出范围和默认偏置。

    旧程序里 am_mod_0_5 的注释和实际范围不完全一致。
    这里先保留旧程序实际用的默认值：bias=0, 范围 -2.5 到 2.5 V。
    后面需要根据你的接线和信号源输入方式确认。
    """

    ao_min: float = -2.5
    ao_max: float = 2.5
    bias_ao0: float = 0.0
    bias_ao1: float = 0.0
    max_step_per_frame: float = 0.03


@dataclass
class PeakSettings:
    """双峰位置和测量方式。"""

    default_indices: list[int]
    window_half: int = 20
    smooth_window: int = 25
    peak_mode: str = "height"  # 可选：height 或 area
    peak_avg_half: int = 5


@dataclass
class PiLoopSettings:
    """单个环路的 PI 参数。"""

    setpoint: float
    kp: float
    ki: float
    inverted: bool = False


@dataclass
class FilterSettings:
    """控制前的慢滤波和死区参数。"""

    deadband_rel: float = 0.01
    meas_ema_alpha: float = 0.15
    error_lp_frames: int = 20
    control_update_every: int = 3


@dataclass
class TwoPeakSettings:
    """双峰锁定的总参数容器。"""

    daq: DaqSettings
    ao: AoSettings
    peaks: PeakSettings
    loop0: PiLoopSettings
    loop1: PiLoopSettings
    filters: FilterSettings

    @classmethod
    def defaults(cls) -> "TwoPeakSettings":
        """返回从旧 v12 程序整理出来的默认参数。"""

        return cls(
            daq=DaqSettings(
                ai_channels=["ai0", "ai1"],
                ao_channels=["ao0", "ao1"],
            ),
            ao=AoSettings(),
            peaks=PeakSettings(default_indices=[642, 754]),
            loop0=PiLoopSettings(setpoint=0.026, kp=5.0, ki=0.0, inverted=False),
            loop1=PiLoopSettings(setpoint=0.012, kp=5.0, ki=0.0, inverted=True),
            filters=FilterSettings(),
        )

    def to_web_parameters(self) -> dict[str, Any]:
        """把嵌套参数展开成 WebUI 容易使用的字典。

        WebUI 不一定喜欢嵌套结构，所以这里同时保留分组和扁平字段。
        """

        return {
            "groups": asdict(self),
            "ai_channels": list(self.daq.ai_channels),
            "ao_channels": list(self.daq.ao_channels),
            "sample_rate": self.daq.sample_rate,
            "samples_per_frame": self.daq.samples_per_frame,
            "terminal_config": self.daq.terminal_config,
            "ai_min_val": self.daq.ai_min_val,
            "ai_max_val": self.daq.ai_max_val,
            "ao_min": self.ao.ao_min,
            "ao_max": self.ao.ao_max,
            "bias_ao0": self.ao.bias_ao0,
            "bias_ao1": self.ao.bias_ao1,
            "max_step_per_frame": self.ao.max_step_per_frame,
            "peak_indices": list(self.peaks.default_indices),
            "window_half": self.peaks.window_half,
            "smooth_window": self.peaks.smooth_window,
            "peak_mode": self.peaks.peak_mode,
            "peak_avg_half": self.peaks.peak_avg_half,
            "setpoint_ao0": self.loop0.setpoint,
            "kp_ao0": self.loop0.kp,
            "ki_ao0": self.loop0.ki,
            "setpoint_ao1": self.loop1.setpoint,
            "kp_ao1": self.loop1.kp,
            "ki_ao1": self.loop1.ki,
            "ao1_inverted": self.loop1.inverted,
            "deadband_rel": self.filters.deadband_rel,
            "meas_ema_alpha": self.filters.meas_ema_alpha,
            "error_lp_frames": self.filters.error_lp_frames,
            "control_update_every": self.filters.control_update_every,
        }

