"""
================================================================================
NI USB-6363 双峰功率锁定 — 浏览器 GUI v12 — 宽线宽滤波版
================================================================================

这个版本只跟踪/锁定两个峰：
  P1: 110M 峰 -> AO0
  P2: EOM 一阶峰 / 80M 路反馈峰 -> AO1

新增 v12 — 宽线宽滤波版：
  加强信号平滑（移动平均窗口 15→25）、峰区平均取高（用峰顶点 ±5 点均值代替单点取高）、
  PI 深度阻尼（EMA α 0.35→0.15、误差平均 10→20 帧、AO更新间隔 2→3 帧、步进限幅 0.06→0.03）、
  死区扩大（0.5%→1%）。专门针对宽线宽激光器导致的 FP 透射峰高频抖动/毛刺问题。

v11 特性（继承）：
  触发式采集（网页按钮触发一帧 FINITE 采集）、自动寻峰算法（auto_find_two_peaks）、
  AO 电压保持（模式切换不断电）。

相比 v2：
  1. 从四峰改成双峰，避免 EOM 0 级弱/消失时导致校准和跟踪混乱。
  2. 校准和闭环继续分离：校准后不会自动推 AO。
  3. 支持手动点击选择 P1/P2 峰位。
  4. 白色背景界面，布局更清爽。
  5. 前端 SP 实时更新、峰位实时更新、AO 每帧真实写入。

用法：
  python main_web_two_peak_v12.py
  浏览器打开 http://localhost:8765

依赖：
  pip install fastapi uvicorn nidaqmx numpy
================================================================================
"""
import sys
import os
import time
import csv
import gc
import json
import queue
import threading
import asyncio
from collections import deque
from io import BytesIO
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
import uvicorn

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import nidaqmx
from nidaqmx.constants import AcquisitionType, TerminalConfiguration
from nidaqmx.errors import DaqError

from dual_loop_core import (
    PeakTracker,
    DualPIController,
    LockDetector,
    calibrate_global,
    track_and_control,
    smooth_moving_average,
    auto_find_two_peaks,
)

# =============================================================================
# 用户配置
# =============================================================================

DEVICE_NAME = "Dev2"
AI_CHANNELS = "ai0:1"
AO_CHANNELS = "ao0,ao1"

SAMPLE_RATE = 50000
SAMPLES_PER_SCAN = 5000
DT = SAMPLES_PER_SCAN / SAMPLE_RATE

# 控制器使用"相对误差"增益：
#   output_change = Kp * (SP - measured) / SP
# 这样小光电压信号（例如 0.012 V）也能产生足够的 AO 修正。
# Kp 单位可理解为：100% 相对误差对应多少 V 的 AO 修正。
SETPOINT_110M = 0.026
KP_110M = 5.0
KI_110M = 0.0

# 这里的 EOM1 表示 AO1 对应那一路；反馈峰用 EOM 一阶峰，不用 EOM 0 级。
SETPOINT_EOM1 = 0.012
KP_EOM1 = 5.0
KI_EOM1 = 0.0

# AO 输出模式：
#   am_mod_0_5: 0~5 V，适合"5 V -> 幅度 170%"这类信号源 AM 输入。
#               默认 bias=2.5 V，闭环可上下调。请在 bias 已输出后再点"SP=当前峰高"。
#   am_mod_pm1: -1~+1 V，旧版本调试用。
#   vva:        4~6 V，适合 VVA 压控输入。
OUTPUT_MODE = "am_mod_0_5"
if OUTPUT_MODE == "am_mod_0_5":
    BIAS_110M, BIAS_EOM1 = 0, 0
    AO_MIN, AO_MAX = -2.5, 2.5
elif OUTPUT_MODE == "am_mod_pm1":
    BIAS_110M, BIAS_EOM1 = 0.0, 0.0
    AO_MIN, AO_MAX = -2.5, 2.5
else:
    BIAS_110M, BIAS_EOM1 = 5.0, 5.0
    AO_MIN, AO_MAX = 4.0, 6.0

# === 宽线宽滤波参数 ===
# 专门针对宽线宽激光器导致的 FP 透射峰高频抖动/毛刺问题。
#
# SMOOTH_WINDOW: 移动平均窗口，宽线宽激光需要更大值 (默认25，原先15)
# PEAK_AVG_HALF: 峰高测量时取峰顶点附近 ±N 个点的平均值，代替单点取高
#                默认 5 → 用峰顶点附近 11 个点的均值作为峰高，抑制单点抖动
# MEAS_EMA_ALPHA: 峰高 EMA 滤波系数，宽线宽需要更强滤波 (0.15 比 0.35 更钝)
# ERROR_LP_FRAMES: PI误差平均帧数，宽线宽需要更长平均 (20 比 10 更钝)
# CONTROL_UPDATE_EVERY: AO更新间隔，减小更新频率避免追着噪声打 (3 比 2)
# MAX_AO_STEP_PER_FRAME: AO步进限幅更小 (0.03 比 0.06)
# DEAD_BAND_REL: 相对误差死区更大 (0.01 = 1% 比 0.005 = 0.5%)
SMOOTH_WINDOW = 25
PEAK_AVG_HALF = 5

# PEAK_MODE: "height" = 峰区平均高度, "area" = 窗口积分面积
# 宽线宽激光推荐 "area"，因为展宽时峰面积基本不变（能量守恒）
PEAK_MODE = "height"
DEAD_BAND_REL = 0.01
MEAS_EMA_ALPHA = 0.15
MAX_AO_STEP_PER_FRAME = 0.03
ERROR_LP_FRAMES = 20
CONTROL_UPDATE_EVERY = 3

# 自动调参：使用 Pattern Search（坐标/模式搜索）优化公共 Kp/Ki。
# 每测试一组 Kp/Ki：先等待 settle 秒，再采 measure 秒，按 score 评价。
# score 会同时惩罚：每帧绝对相对误差均值、RMS、AO 抖动、撞边界、误差振荡。
AUTO_TUNE_SETTLE_SEC = 1.0
AUTO_TUNE_MEASURE_SEC = 2.0
AUTO_TUNE_MIN_MEASURE_FRAMES = 100
AUTO_TUNE_MAX_ROUNDS = 6
AUTO_TUNE_MIN_STEP_KP = 0.05
AUTO_TUNE_MIN_STEP_KI = 0.005

# 峰值长期记录：只记录两个峰的高度/位置/误差/AO，不保存整条波形，适合过夜。
PEAK_LOG_DIR = "peak_logs"
PEAK_LOG_EVERY_N_FRAMES = 10     # 1=每帧都记录；10=每10帧记录一次，文件小很多
PEAK_LOG_FLUSH_EVERY_ROWS = 50   # 每写 N 行 flush 一次，降低断电丢数据风险


# 只跟踪两个峰。按你截图，110M 大概在 642，EOM 一阶大概在 754。
# 如果实际不对，直接在网页上点击峰位即可覆盖。
DEFAULT_PEAK_INDICES = [642, 754]
N_PEAKS = 2

# 两个峰之间如果很近，不要用太大窗口。程序会自动限制到安全范围。
WINDOW_HALF_DEFAULT = 30
EMA_ALPHA = 0.05

# AO1 环路方向：
# True：使用 dual_loop_core 里的反比逻辑，error>0 时 AO1 下降。
# False：把 AO1 再绕 bias 翻转一次，error>0 时 AO1 上升。
LOOP_EOM1_INVERTED = True

# 调试阶段建议 True，避免脱锁保护把 PI 输出替换成历史安全值。
BYPASS_SAFETY = True


def clamp(x, lo, hi):
    return max(lo, min(hi, float(x)))


def relative_gain_to_absolute(k_rel, sp, min_sp=1e-4):
    """把相对误差增益转换成 dual_loop_core 需要的绝对误差增益。

    dual_loop_core 使用 output = bias + Kp_abs * (SP - measured)。
    这里希望用户调的是相对误差：output = bias + Kp_rel * (SP - measured) / SP。
    因此 Kp_abs = Kp_rel / max(abs(SP), min_sp)。
    """
    return float(k_rel) / max(abs(float(sp)), min_sp)


def peak_average_height(signal, peak_idx, half_window=5):
    """取峰顶点附近 ±half_window 个点的平均值作为峰高。

    对于宽线宽激光，单点取高受高频抖动影响严重。
    取峰区平均可以显著抑制抖动，给出更稳定的峰高估计。

    参数:
      signal: 平滑后的信号
      peak_idx: 峰顶点索引 (来自 find_peak_in_window)
      half_window: 平均窗口半宽 (默认5 → 用 11 个点)

    返回: 平均高度
    """
    n = len(signal)
    lo = max(0, int(peak_idx) - half_window)
    hi = min(n - 1, int(peak_idx) + half_window)
    return float(np.mean(signal[lo:hi + 1]))


def peak_area(signal, peak_idx, half_window=20):
    """用窗口积分 + 线性基线扣除计算峰面积。

    峰面积 ≈ 峰高 × 峰宽，对宽线宽展宽天然不敏感（能量守恒）。
    积分本身就是低通滤波，抗高频噪声能力远强于点测高。

    算法:
      1. 取 peak_idx ± half_window 的窗口
      2. 用窗口两端各 EDGE_PTS 个点的均值做线性基线
      3. 信号减基线后梯形积分 = 峰面积

    参数:
      signal: 平滑后的信号
      peak_idx: 峰顶点索引
      half_window: 积分窗口半宽 (默认20 → 用 41 个点)

    返回: 峰面积 (V·samples)
    """
    EDGE_PTS = 4  # 基线用边缘点数
    n = len(signal)
    lo = max(0, int(peak_idx) - half_window)
    hi = min(n - 1, int(peak_idx) + half_window)
    if hi - lo < EDGE_PTS * 2 + 2:
        return float(np.mean(signal[lo:hi + 1])) * (hi - lo)  # 窗口太小，退化为矩形面积

    # 线性基线：左边缘均值和右边缘均值连成的直线
    left_base = float(np.mean(signal[lo:lo + EDGE_PTS]))
    right_base = float(np.mean(signal[hi - EDGE_PTS + 1:hi + 1]))
    x = np.arange(lo, hi + 1, dtype=float)
    baseline = left_base + (right_base - left_base) * (x - lo) / (hi - lo)

    # 减基线后梯形积分
    signal_window = signal[lo:hi + 1].astype(float)
    corrected = signal_window - baseline
    corrected = np.maximum(corrected, 0.0)  # 负值置零（物理上峰面积非负）
    area = float(np.trapz(corrected, x))
    return area


def measure_peak(signal, peak_idx, half_window, mode="height"):
    """统一峰测量接口：根据 mode 返回峰高或峰面积。

    mode: "height" = 峰区平均高度, "area" = 窗口积分面积
    """
    if mode == "area":
        return peak_area(signal, peak_idx, half_window)
    else:
        return peak_average_height(signal, peak_idx, half_window)


def safe_peak_indices(indices, n_samples):
    out = []
    for x in indices:
        try:
            xi = int(round(float(x)))
        except Exception:
            xi = 0
        out.append(max(1, min(n_samples - 2, xi)))
    while len(out) < N_PEAKS:
        out.append(0)
    return out[:N_PEAKS]


def dynamic_window_half(indices, user_window):
    try:
        user_window = int(user_window)
    except Exception:
        user_window = WINDOW_HALF_DEFAULT
    indices = sorted(int(x) for x in indices)
    gaps = [b - a for a, b in zip(indices[:-1], indices[1:]) if b > a]
    if not gaps:
        return max(3, min(40, user_window))
    max_safe = max(3, min(60, min(gaps) // 2 - 2))
    return max(3, min(user_window, max_safe))


def rel_error(setpoint, measured, min_sp=1e-9):
    sp = float(setpoint)
    if abs(sp) < min_sp:
        return 0.0
    return (sp - float(measured)) / sp


class RelativePIController:
    """双通道相对误差 PI，控制器只使用低通后的慢误差。

    页面和日志仍显示 raw relative error；PI 内部使用最近 N 帧平均后的 slow error。
    这样 FP 峰高的单帧高频抖动不会直接驱动 AO。
    """

    def __init__(self, bias0, bias1, ao_min, ao_max, dt):
        self.bias0 = float(bias0)
        self.bias1 = float(bias1)
        self.ao_min = float(ao_min)
        self.ao_max = float(ao_max)
        self.dt = float(dt)
        self.reset()

    def reset(self):
        self.i0 = 0.0
        self.i1 = 0.0
        self.y0 = None
        self.y1 = None
        self.ao0 = self.bias0
        self.ao1 = self.bias1
        self.err0_hist = deque(maxlen=max(1, int(ERROR_LP_FRAMES)))
        self.err1_hist = deque(maxlen=max(1, int(ERROR_LP_FRAMES)))
        self.control_tick = 0
        self.last_rel0_raw = 0.0
        self.last_rel1_raw = 0.0
        self.last_rel0_slow = 0.0
        self.last_rel1_slow = 0.0

    def _filter(self, measured, last, alpha):
        alpha = clamp(alpha, 0.02, 1.0)
        measured = float(measured)
        if last is None:
            return measured
        return alpha * measured + (1.0 - alpha) * last

    def _update_error_history(self, hist, value, n_frames):
        n_frames = max(1, int(round(float(n_frames))))
        if hist.maxlen != n_frames:
            old_vals = list(hist)[-(n_frames - 1):]
            hist = deque(old_vals, maxlen=n_frames)
        hist.append(float(value))
        return hist, float(np.mean(hist)) if hist else float(value)

    def _one_loop(self, rel_for_control, kp, ki, direction, integral, bias, last_ao,
                  deadband_rel, max_step):
        # 这里输入的 rel_for_control 已经是慢速低通/移动平均后的相对误差。
        if abs(rel_for_control) <= deadband_rel:
            err_eff = 0.0
        else:
            err_eff = np.sign(rel_for_control) * (abs(rel_for_control) - deadband_rel)

        kp = max(0.0, float(kp))
        ki = max(0.0, float(ki))
        span = max(abs(self.ao_max - bias), abs(bias - self.ao_min), 1e-6)

        candidate_i = integral + err_eff * self.dt
        if ki > 1e-12:
            candidate_i = clamp(candidate_i, -span / ki, span / ki)
        else:
            candidate_i = 0.0

        raw_ao = bias + direction * (kp * err_eff + ki * candidate_i)

        # Anti-windup: 如果已经在边界外还继续往外推，就冻结本帧积分。
        if (raw_ao > self.ao_max and direction * err_eff > 0) or (raw_ao < self.ao_min and direction * err_eff < 0):
            candidate_i = integral
            raw_ao = bias + direction * (kp * err_eff + ki * candidate_i)

        ao = clamp(raw_ao, self.ao_min, self.ao_max)

        max_step = max(0.001, float(max_step))
        ao = clamp(ao, last_ao - max_step, last_ao + max_step)
        ao = clamp(ao, self.ao_min, self.ao_max)
        return ao, candidate_i, err_eff

    def update(self, sp0, meas0, sp1, meas1, params):
        alpha = params.get("meas_ema_alpha", MEAS_EMA_ALPHA)
        deadband = abs(float(params.get("deadband_rel", DEAD_BAND_REL)))
        max_step = abs(float(params.get("max_ao_step", MAX_AO_STEP_PER_FRAME)))
        error_lp_frames = max(1, int(round(float(params.get("error_lp_frames", ERROR_LP_FRAMES)))))
        control_update_every = max(1, int(round(float(params.get("control_update_every", CONTROL_UPDATE_EVERY)))))

        self.y0 = self._filter(meas0, self.y0, alpha)
        self.y1 = self._filter(meas1, self.y1, alpha)

        rel0_raw = rel_error(sp0, self.y0)
        rel1_raw = rel_error(sp1, self.y1)
        self.err0_hist, rel0_slow = self._update_error_history(self.err0_hist, rel0_raw, error_lp_frames)
        self.err1_hist, rel1_slow = self._update_error_history(self.err1_hist, rel1_raw, error_lp_frames)

        self.control_tick += 1
        should_update_ao = (self.control_tick % control_update_every == 0)

        if should_update_ao:
            self.ao0, self.i0, eff0 = self._one_loop(
                rel0_slow, params.get("kp_110M", KP_110M), params.get("ki_110M", KI_110M),
                +1.0, self.i0, self.bias0, self.ao0, deadband, max_step
            )

            eom_direction = -1.0 if LOOP_EOM1_INVERTED else +1.0
            self.ao1, self.i1, eff1 = self._one_loop(
                rel1_slow, params.get("kp_eom1", KP_EOM1), params.get("ki_eom1", KI_EOM1),
                eom_direction, self.i1, self.bias1, self.ao1, deadband, max_step
            )
        else:
            eff0 = 0.0
            eff1 = 0.0

        self.last_rel0_raw = rel0_raw
        self.last_rel1_raw = rel1_raw
        self.last_rel0_slow = rel0_slow
        self.last_rel1_slow = rel1_slow

        return {
            "ao0": self.ao0,
            "ao1": self.ao1,
            "rel0_raw": rel0_raw,
            "rel1_raw": rel1_raw,
            "rel0_slow": rel0_slow,
            "rel1_slow": rel1_slow,
            "rel0_filt": rel0_slow,
            "rel1_filt": rel1_slow,
            "eff0": eff0,
            "eff1": eff1,
            "filt0": self.y0,
            "filt1": self.y1,
            "should_update_ao": bool(should_update_ao),
            "error_lp_frames": int(error_lp_frames),
            "control_update_every": int(control_update_every),
        }


def make_empty_result(control_enabled=False):
    return {
        "ao0": BIAS_110M,
        "ao1": BIAS_EOM1,
        "heights": [0.0] * N_PEAKS,
        "positions": [0.0] * N_PEAKS,
        "found_count": 0,
        "lock_110M": False,
        "lock_eom1": False,
        "error_110M": 0.0,
        "error_eom1": 0.0,
        "rel_error_110M": 0.0,
        "rel_error_eom1": 0.0,
        "rel_error_110M_slow": 0.0,
        "rel_error_eom1_slow": 0.0,
        "control_enabled": bool(control_enabled),
    }


def track_only(ai0_data, tracker, setpoint_110M, setpoint_eom1):
    """只寻峰和算误差，不更新 PI，不推 AO。"""
    signal_smooth = smooth_moving_average(ai0_data, SMOOTH_WINDOW)
    heights, positions, found_count = tracker.track(signal_smooth)
    # 峰区平均取高，抑制宽线宽抖动
    avg_heights = []
    for i, pos in enumerate(positions):
        if i < found_count:
            avg_heights.append(measure_peak(signal_smooth, int(round(pos)), PEAK_AVG_HALF, PEAK_MODE))
        else:
            avg_heights.append(0.0)
    measured_110M = avg_heights[0] if len(avg_heights) > 0 else 0.0
    measured_eom1 = avg_heights[1] if len(avg_heights) > 1 else 0.0
    return {
        "ao0": BIAS_110M,
        "ao1": BIAS_EOM1,
        "heights": [float(h) for h in avg_heights],
        "positions": [float(p) for p in positions],
        "found_count": int(found_count),
        "lock_110M": False,
        "lock_eom1": False,
        "error_110M": float(setpoint_110M - measured_110M),
        "error_eom1": float(setpoint_eom1 - measured_eom1),
        "rel_error_110M": float(rel_error(setpoint_110M, measured_110M)),
        "rel_error_eom1": float(rel_error(setpoint_eom1, measured_eom1)),
        "rel_error_110M_slow": float(rel_error(setpoint_110M, measured_110M)),
        "rel_error_eom1_slow": float(rel_error(setpoint_eom1, measured_eom1)),
        "control_enabled": False,
    }


def evaluate_pi_score(samples, ao_min, ao_max):
    """把一段测试数据变成自动调参 score。samples: [rel0, rel1, ao0, ao1].

    rel0/rel1 是相对误差的小数单位，例如 0.01 = 1%。score 越小越好。
    评价目标不是单纯 RMS 最小，而是避免选到"误差小但 AO 猛抖/撞边界"的激进参数。
    """
    if not samples:
        return 999.0, {
            "rms_rel": 999.0,
            "mean_rel": 999.0,
            "mean_abs_rel": 999.0,
            "rail_frac": 1.0,
            "ao_rms_norm": 999.0,
            "oscillation": 999.0,
        }

    arr = np.asarray(samples, dtype=float)
    if arr.ndim != 2 or arr.shape[0] < 5 or arr.shape[1] < 4:
        return 999.0, {
            "rms_rel": 999.0,
            "mean_rel": 999.0,
            "mean_abs_rel": 999.0,
            "rail_frac": 1.0,
            "ao_rms_norm": 999.0,
            "oscillation": 999.0,
        }

    rel0 = arr[:, 0]
    rel1 = arr[:, 1]
    ao0 = arr[:, 2]
    ao1 = arr[:, 3]
    err = np.concatenate([rel0, rel1])

    # 这里的"平均误差"按每一帧误差的绝对值计算：
    #   mean_abs_rel = mean(|rel_error|)
    # RMS 也按误差幅度计算：sqrt(mean(|rel_error|^2))；这与平方后符号消失的
    # 普通 RMS 数值相同，但语义上明确是"波动幅度"，不是 signed mean。
    abs_err = np.abs(err)
    rms_rel = float(np.sqrt(np.mean(abs_err ** 2)))
    mean_rel = float(np.mean(err))          # 仅作为 signed bias 诊断，不作为主要目标
    mean_abs_rel = float(np.mean(abs_err))  # 自动调参和图上统计使用这个

    ao_span = max(float(ao_max - ao_min), 1e-9)
    ao0_rms = float(np.sqrt(np.mean((ao0 - np.mean(ao0)) ** 2)))
    ao1_rms = float(np.sqrt(np.mean((ao1 - np.mean(ao1)) ** 2)))
    ao_rms_norm = float(np.sqrt(ao0_rms ** 2 + ao1_rms ** 2) / ao_span)

    rail0 = np.logical_or(ao0 <= ao_min + 0.03 * ao_span, ao0 >= ao_max - 0.03 * ao_span)
    rail1 = np.logical_or(ao1 <= ao_min + 0.03 * ao_span, ao1 >= ao_max - 0.03 * ao_span)
    rail_frac = float(np.mean(np.logical_or(rail0, rail1)))

    # 振荡惩罚：一阶差分 RMS。它会阻止优化器选择让误差来回跳的参数。
    d0 = np.diff(rel0)
    d1 = np.diff(rel1)
    if len(d0) > 0 and len(d1) > 0:
        oscillation = float(np.sqrt(np.mean(0.5 * (d0 ** 2 + d1 ** 2))))
    else:
        oscillation = 0.0

    # 权重经验值：优先压每帧误差幅度 mean(|err|) 和 RMS；
    # signed mean 只反映长期偏置，不再作为主要目标，避免正负误差互相抵消。
    score = (
        0.60 * mean_abs_rel
        + 0.40 * rms_rel
        + 0.05 * abs(mean_rel)
        + 0.08 * ao_rms_norm
        + 0.45 * rail_frac
        + 0.25 * oscillation
    )

    return float(score), {
        "rms_rel": rms_rel,
        "mean_rel": mean_rel,
        "mean_abs_rel": mean_abs_rel,
        "rail_frac": rail_frac,
        "ao_rms_norm": ao_rms_norm,
        "oscillation": oscillation,
    }


class PatternSearchPIOptimizer:
    """无梯度 Pattern Search，用实验数据寻找公共 Kp/Ki。

    每一轮测试中心点和 Kp/Ki 正负方向的候选点，选 score 最小者作为新中心；
    如果没有改善，就缩小步长。相比固定网格扫描，它会围绕当前最优点自适应搜索。
    """

    def __init__(self, kp0, ki0,
                 step_kp=None, step_ki=None,
                 kp_bounds=(0.0, 50.0), ki_bounds=(0.0, 5.0),
                 max_rounds=AUTO_TUNE_MAX_ROUNDS,
                 min_step_kp=AUTO_TUNE_MIN_STEP_KP,
                 min_step_ki=AUTO_TUNE_MIN_STEP_KI):
        self.kp_bounds = tuple(kp_bounds)
        self.ki_bounds = tuple(ki_bounds)
        self.max_rounds = int(max_rounds)
        self.min_step_kp = float(min_step_kp)
        self.min_step_ki = float(min_step_ki)

        kp0 = self._clip_kp(kp0)
        ki0 = self._clip_ki(ki0)
        self.center = np.array([kp0, ki0], dtype=float)

        self.step = np.array([
            float(step_kp) if step_kp is not None else max(0.5, 0.5 * max(kp0, 1.0)),
            float(step_ki) if step_ki is not None else max(0.02, 0.5 * max(ki0, 0.02)),
        ], dtype=float)

        self.round = 0
        self.best_point = self.center.copy()
        self.best_score = None
        self.best_metrics = None
        self.candidates = []
        self.results = []
        self.index = 0
        self.done = False
        self._build_candidates()

    def _clip_kp(self, x):
        return float(np.clip(float(x), self.kp_bounds[0], self.kp_bounds[1]))

    def _clip_ki(self, x):
        return float(np.clip(float(x), self.ki_bounds[0], self.ki_bounds[1]))

    def _clip_point(self, p):
        return np.array([self._clip_kp(p[0]), self._clip_ki(p[1])], dtype=float)

    def _unique_points(self, points):
        out = []
        seen = set()
        for p in points:
            p = self._clip_point(p)
            key = (round(float(p[0]), 6), round(float(p[1]), 6))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _build_candidates(self):
        c = self.center.copy()
        skp, ski = self.step
        self.candidates = self._unique_points([
            c,
            c + np.array([skp, 0.0]),
            c - np.array([skp, 0.0]),
            c + np.array([0.0, ski]),
            c - np.array([0.0, ski]),
            c + np.array([skp, ski]),
            c + np.array([skp, -ski]),
            c + np.array([-skp, ski]),
            c + np.array([-skp, -ski]),
        ])
        self.results = []
        self.index = 0

    def current(self):
        if self.done or not self.candidates:
            return tuple(self.best_point)
        return tuple(self.candidates[self.index])

    def tell(self, score, metrics=None):
        if self.done:
            return None
        point = self.candidates[self.index].copy()
        score = float(score)
        self.results.append({"point": point, "score": score, "metrics": metrics or {}})

        if self.best_score is None or score < self.best_score:
            self.best_score = score
            self.best_point = point.copy()
            self.best_metrics = metrics or {}

        self.index += 1
        if self.index < len(self.candidates):
            return self.current()

        # 当前轮结束：找本轮最优。
        round_best = min(self.results, key=lambda r: r["score"])
        improved = np.linalg.norm(round_best["point"] - self.center) > 1e-9 and (
            self.best_score is None or round_best["score"] <= self.best_score * 1.02
        )

        if improved:
            self.center = round_best["point"].copy()
            # 有改善时保留步长，继续沿着附近搜索。
        else:
            self.step *= 0.5

        self.round += 1
        if (self.round >= self.max_rounds or
                (self.step[0] < self.min_step_kp and self.step[1] < self.min_step_ki)):
            self.done = True
            return None

        self._build_candidates()
        return self.current()

    def summary(self):
        kp, ki = tuple(self.best_point)
        score = self.best_score if self.best_score is not None else float("nan")
        return f"Kp={kp:.4g}, Ki={ki:.4g}, score={score:.4g}, round={self.round}/{self.max_rounds}"


class DAQController:
    def __init__(self):
        self.data_queue = queue.Queue(maxsize=200)
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.running = False
        self.is_calibrated = False
        self.control_enabled = False
        self.status_text = "就绪"

        # AO 保持状态：停止锁定后，不回 bias，而是固定在停止瞬间的输出电压。
        # last_ao0/last_ao1 永远记录最近一次真正写给 DAQ 的 AO 电压。
        self.last_ao0 = float(BIAS_110M)
        self.last_ao1 = float(BIAS_EOM1)
        self.hold_ao0 = float(BIAS_110M)
        self.hold_ao1 = float(BIAS_EOM1)
        self.ao_hold_enabled = False

        self.cal_requested = False
        self.manual_cal_requested = False
        self.reset_requested = False
        self.lock_on_requested = False
        self.lock_off_requested = False
        self.auto_tune_requested = False
        self.auto_tune_running = False
        self.tuner = None

        self.trigger_active = False  # True: 持续循环采集; False: 等待触发
        self.trigger_event = threading.Event()  # 用于唤醒等待中的 DAQ 循环
        self.trigger_mode = "software"  # "software" for PC trigger, "pfi" for PFI hardware trigger

        self.log_start_requested = False
        self.log_stop_requested = False
        self.peak_log_active = False
        self.peak_log_path = ""
        self.peak_log_file = None
        self.peak_log_writer = None
        self.peak_log_count = 0
        self.peak_log_flush_counter = 0
        self.peak_log_started_at = None

        self.err_history = deque(maxlen=1200)

        self.params = {
            "setpoint_110M": SETPOINT_110M,
            "kp_110M": KP_110M,
            "ki_110M": KI_110M,
            "setpoint_eom1": SETPOINT_EOM1,
            "kp_eom1": KP_EOM1,
            "ki_eom1": KI_EOM1,
            "window_half": WINDOW_HALF_DEFAULT,
            "peak_indices": list(DEFAULT_PEAK_INDICES),
            "deadband_rel": DEAD_BAND_REL,
            "meas_ema_alpha": MEAS_EMA_ALPHA,
            "max_ao_step": MAX_AO_STEP_PER_FRAME,
            "error_lp_frames": ERROR_LP_FRAMES,
            "control_update_every": CONTROL_UPDATE_EVERY,
            "smooth_window": SMOOTH_WINDOW,
            "peak_mode": PEAK_MODE,
            "peak_avg_half": PEAK_AVG_HALF,
            "log_every_n_frames": PEAK_LOG_EVERY_N_FRAMES,
        }

    def update_params(self, params):
        alias = {
            "sp_110M": "setpoint_110M",
            "sp_eom1": "setpoint_eom1",
            "sp_80M": "setpoint_eom1",        # 兼容旧前端名字
            "setpoint_80M": "setpoint_eom1",  # 兼容旧前端名字
            "kp_80M": "kp_eom1",
            "ki_80M": "ki_eom1",
            "idx_p1": "idx_p1",
            "idx_p2": "idx_p2",
        }
        with self._lock:
            for k, v in params.items():
                kk = alias.get(k, k)
                if kk in ("idx_p1", "idx_p2"):
                    peaks = list(self.params.get("peak_indices", DEFAULT_PEAK_INDICES))
                    i = int(kk[-1]) - 1
                    peaks[i] = int(round(float(v)))
                    self.params["peak_indices"] = peaks
                elif kk == "peak_indices":
                    if isinstance(v, list):
                        self.params["peak_indices"] = [int(round(float(x))) for x in v[:N_PEAKS]]
                elif kk == "peak_mode":
                    self.params[kk] = str(v)  # "height" or "area"
                else:
                    self.params[kk] = float(v)

    def get_params_copy(self):
        with self._lock:
            p = dict(self.params)
            p["peak_indices"] = list(self.params.get("peak_indices", DEFAULT_PEAK_INDICES))
            return p

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.clear_err_history()
        self._thread = threading.Thread(target=self._daq_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.status_text = "DAQ 线程无响应"
            self._thread = None

    def _push(self, msg):
        try:
            self.data_queue.put_nowait(msg)
        except queue.Full:
            try:
                self.data_queue.get_nowait()
                self.data_queue.put_nowait(msg)
            except queue.Empty:
                pass

    def _write_ao(self, task_ao, ao0, ao1):
        """写 AO，同时更新最近一次真实输出电压。"""
        ao0 = clamp(ao0, AO_MIN, AO_MAX)
        ao1 = clamp(ao1, AO_MIN, AO_MAX)
        task_ao.write([ao0, ao1])
        self.last_ao0 = float(ao0)
        self.last_ao1 = float(ao1)
        return self.last_ao0, self.last_ao1

    def _write_bias_ao(self, task_ao):
        self.ao_hold_enabled = False
        return self._write_ao(task_ao, BIAS_110M, BIAS_EOM1)

    def _enable_ao_hold(self):
        self.hold_ao0 = float(self.last_ao0)
        self.hold_ao1 = float(self.last_ao1)
        self.ao_hold_enabled = True

    def clear_err_history(self):
        with self._lock:
            self.err_history.clear()

    def append_err_history(self, frame, rel0, rel1, control_enabled):
        with self._lock:
            self.err_history.append({
                "frame": int(frame),
                "rel0": float(rel0),
                "rel1": float(rel1),
                "control_enabled": bool(control_enabled),
            })

    def get_err_history(self):
        with self._lock:
            return list(self.err_history)

    def _make_peak_log_path(self):
        os.makedirs(PEAK_LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(PEAK_LOG_DIR, f"peak_log_{ts}.csv")

    def _start_peak_log(self):
        if self.peak_log_active:
            self.status_text = f"峰值记录已在进行：{self.peak_log_path}"
            self._push({"type": "status", "message": self.status_text, "running": True})
            return
        path = self._make_peak_log_path()
        f = open(path, "w", newline="", encoding="utf-8")
        fieldnames = [
            "timestamp", "elapsed_s", "frame", "control_enabled", "auto_tune_running",
            "found_count", "p1_height_v", "p2_height_v", "p1_position", "p2_position",
            "p1_error_v", "p2_error_v", "p1_rel_error", "p2_rel_error",
            "ao0_v", "ao1_v", "setpoint_p1_v", "setpoint_p2_v",
            "window_half", "cal_p1", "cal_p2", "lock_p1", "lock_p2",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
        self.peak_log_file = f
        self.peak_log_writer = writer
        self.peak_log_path = path
        self.peak_log_count = 0
        self.peak_log_flush_counter = 0
        self.peak_log_started_at = time.time()
        self.peak_log_active = True
        self.status_text = f"开始记录两个峰值：{path}"
        self._push({"type": "status", "message": self.status_text, "running": True})

    def _stop_peak_log(self):
        if not self.peak_log_active:
            return
        path = self.peak_log_path
        try:
            if self.peak_log_file is not None:
                self.peak_log_file.flush()
                try:
                    os.fsync(self.peak_log_file.fileno())
                except Exception:
                    pass
                self.peak_log_file.close()
        finally:
            self.peak_log_file = None
            self.peak_log_writer = None
            self.peak_log_active = False
            self.peak_log_flush_counter = 0
        self.status_text = f"峰值记录已停止：{path}，共 {self.peak_log_count} 行"
        self._push({"type": "status", "message": self.status_text, "running": True})

    def _write_peak_log_row(self, frame, result, tracker, params):
        if not self.peak_log_active or self.peak_log_writer is None:
            return
        try:
            every = max(1, int(round(float(params.get("log_every_n_frames", PEAK_LOG_EVERY_N_FRAMES)))))
        except Exception:
            every = PEAK_LOG_EVERY_N_FRAMES
        if frame % every != 0:
            return

        heights = list(result.get("heights", []))
        positions = list(result.get("positions", []))
        cal_positions = []
        if tracker.cal_positions is not None:
            cal_positions = [float(x) for x in tracker.cal_positions[:N_PEAKS]]
        started = self.peak_log_started_at or time.time()
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}",
            "elapsed_s": f"{time.time() - started:.6f}",
            "frame": int(frame),
            "control_enabled": int(bool(self.control_enabled)),
            "auto_tune_running": int(bool(self.auto_tune_running)),
            "found_count": int(result.get("found_count", 0)),
            "p1_height_v": f"{float(heights[0]) if len(heights) > 0 else 0.0:.9f}",
            "p2_height_v": f"{float(heights[1]) if len(heights) > 1 else 0.0:.9f}",
            "p1_position": f"{float(positions[0]) if len(positions) > 0 else 0.0:.4f}",
            "p2_position": f"{float(positions[1]) if len(positions) > 1 else 0.0:.4f}",
            "p1_error_v": f"{float(result.get('error_110M', 0.0)):.9f}",
            "p2_error_v": f"{float(result.get('error_eom1', 0.0)):.9f}",
            "p1_rel_error": f"{float(result.get('rel_error_110M', 0.0)):.9f}",
            "p2_rel_error": f"{float(result.get('rel_error_eom1', 0.0)):.9f}",
            "ao0_v": f"{float(result.get('ao0', BIAS_110M)):.9f}",
            "ao1_v": f"{float(result.get('ao1', BIAS_EOM1)):.9f}",
            "setpoint_p1_v": f"{float(params.get('setpoint_110M', SETPOINT_110M)):.9f}",
            "setpoint_p2_v": f"{float(params.get('setpoint_eom1', SETPOINT_EOM1)):.9f}",
            "window_half": int(getattr(tracker, 'window_half', 0)),
            "cal_p1": f"{cal_positions[0]:.4f}" if len(cal_positions) > 0 else "",
            "cal_p2": f"{cal_positions[1]:.4f}" if len(cal_positions) > 1 else "",
            "lock_p1": int(bool(result.get("lock_110M", False))),
            "lock_p2": int(bool(result.get("lock_eom1", False))),
        }
        self.peak_log_writer.writerow(row)
        self.peak_log_count += 1
        self.peak_log_flush_counter += 1
        if self.peak_log_flush_counter >= PEAK_LOG_FLUSH_EVERY_ROWS:
            self.peak_log_flush_counter = 0
            self.peak_log_file.flush()
            try:
                os.fsync(self.peak_log_file.fileno())
            except Exception:
                pass

    def _start_autotune(self, controller):
        frames_settle = max(3, int(round(AUTO_TUNE_SETTLE_SEC / DT)))
        frames_measure = max(AUTO_TUNE_MIN_MEASURE_FRAMES, int(round(AUTO_TUNE_MEASURE_SEC / DT)))
        p = self.get_params_copy()
        kp0 = float(0.5 * (p.get("kp_110M", KP_110M) + p.get("kp_eom1", KP_EOM1)))
        ki0 = float(0.5 * (p.get("ki_110M", KI_110M) + p.get("ki_eom1", KI_EOM1)))

        optimizer = PatternSearchPIOptimizer(kp0=kp0, ki0=ki0)
        kp, ki = optimizer.current()

        self.auto_tune_running = True
        self.control_enabled = True
        self.tuner = {
            "optimizer": optimizer,
            "settle": frames_settle,
            "measure": frames_measure,
            "frame": 0,
            "samples": [],
            "candidate_count": 1,
        }

        self.update_params({"kp_110M": kp, "kp_eom1": kp, "ki_110M": ki, "ki_eom1": ki})
        start_ao0 = float(self.last_ao0)
        start_ao1 = float(self.last_ao1)
        controller.reset()
        controller.ao0 = clamp(start_ao0, AO_MIN, AO_MAX)
        controller.ao1 = clamp(start_ao1, AO_MIN, AO_MAX)
        self.ao_hold_enabled = False
        self.status_text = (
            f"Pattern Search 自动调参启动：测试 Kp={kp:.3g}, Ki={ki:.3g} "
            f"(settle={frames_settle}帧, measure={frames_measure}帧)"
        )
        self._push({"type": "status", "message": self.status_text, "running": True})

    def _finish_candidate(self, controller):
        t = self.tuner
        if not t or "optimizer" not in t:
            return

        optimizer = t["optimizer"]
        samples = t.get("samples", [])
        score, metrics = evaluate_pi_score(samples, AO_MIN, AO_MAX)

        prev_kp, prev_ki = optimizer.current()
        next_point = optimizer.tell(score, metrics)

        if next_point is None:
            best_kp, best_ki = tuple(optimizer.best_point)
            self.update_params({
                "kp_110M": best_kp,
                "kp_eom1": best_kp,
                "ki_110M": best_ki,
                "ki_eom1": best_ki,
            })
            start_ao0 = float(self.last_ao0)
            start_ao1 = float(self.last_ao1)
            controller.reset()
            controller.ao0 = clamp(start_ao0, AO_MIN, AO_MAX)
            controller.ao1 = clamp(start_ao1, AO_MIN, AO_MAX)
            self.auto_tune_running = False
            self.tuner = None
            rms_pct = 100.0 * float((optimizer.best_metrics or {}).get("rms_rel", 0.0))
            mean_abs_pct = 100.0 * float((optimizer.best_metrics or {}).get("mean_abs_rel", 0.0))
            bias_pct = 100.0 * float((optimizer.best_metrics or {}).get("mean_rel", 0.0))
            self.status_text = (
                f"Pattern Search 自动调参完成：{optimizer.summary()}；"
                f"mean|err|={mean_abs_pct:.3f}%, RMS={rms_pct:.3f}%, bias={bias_pct:+.3f}%。"
                f"若短期更抖，请把 Kp 或 Ki 减半。"
            )
            self._push({"type": "status", "message": self.status_text, "running": True})
            return

        kp, ki = tuple(next_point)
        self.update_params({"kp_110M": kp, "kp_eom1": kp, "ki_110M": ki, "ki_eom1": ki})
        start_ao0 = float(self.last_ao0)
        start_ao1 = float(self.last_ao1)
        controller.reset()
        controller.ao0 = clamp(start_ao0, AO_MIN, AO_MAX)
        controller.ao1 = clamp(start_ao1, AO_MIN, AO_MAX)
        t["frame"] = 0
        t["samples"] = []
        t["candidate_count"] = int(t.get("candidate_count", 1)) + 1

        self.status_text = (
            f"Pattern Search 自动调参：上一个 Kp={prev_kp:.3g}, Ki={prev_ki:.3g}, "
            f"score={score:.4g}; 现在测试 Kp={kp:.3g}, Ki={ki:.3g}; "
            f"当前最佳 {optimizer.summary()}"
        )
        self._push({"type": "status", "message": self.status_text, "running": True})

    def _autotune_step(self, result, controller):
        if not self.auto_tune_running or self.tuner is None:
            return
        t = self.tuner
        t["frame"] += 1
        if t["frame"] > t["settle"]:
            t["samples"].append([
                float(result.get("rel_error_110M", 0.0)),
                float(result.get("rel_error_eom1", 0.0)),
                float(result.get("ao0", BIAS_110M)),
                float(result.get("ao1", BIAS_EOM1)),
            ])
        if t["frame"] >= t["settle"] + t["measure"]:
            self._finish_candidate(controller)

    def _apply_calibration(self, tracker, pi, lock_det, cal_idx, n_samples, source):
        cal_idx = safe_peak_indices(cal_idx, n_samples)
        p = self.get_params_copy()
        wh = dynamic_window_half(cal_idx, p.get("window_half", WINDOW_HALF_DEFAULT))
        tracker.reset(n_peaks=N_PEAKS, window_half=wh, alpha=EMA_ALPHA)
        tracker.calibrate(cal_idx)
        pi.reset()
        lock_det.reset()
        self.control_enabled = False
        self.is_calibrated = True
        self.status_text = f"{source}校准成功: 双峰={cal_idx}, 窗口半宽={wh}; 闭环未启动"
        return cal_idx, wh

    def _daq_loop(self):
        n_samples = SAMPLES_PER_SCAN
        device = DEVICE_NAME

        tracker = PeakTracker(n_peaks=N_PEAKS, window_half=WINDOW_HALF_DEFAULT, alpha=EMA_ALPHA)
        pi = RelativePIController(
            bias0=BIAS_110M, bias1=BIAS_EOM1,
            ao_min=AO_MIN, ao_max=AO_MAX, dt=DT,
        )
        lock_det = LockDetector(
            history_length=5, threshold_ratio=0.3, safety_queue_length=50,
            bias_110M=BIAS_110M, bias_80M=BIAS_EOM1,
        )

        ai_buf = np.empty((2, n_samples), dtype=float)  # 只有 AI0:1，无 AI2
        frame_count = 0
        last_cal_indices = []
        task_ai = None
        task_ao = None

        try:
            task_ai = nidaqmx.Task("AI_Web_2Peak_v12")
            task_ao = nidaqmx.Task("AO_Web_2Peak_v12")

            task_ai.ai_channels.add_ai_voltage_chan(
                f"{device}/{AI_CHANNELS}",
                min_val=-5.0, max_val=5.0,
                terminal_config=TerminalConfiguration.DIFF,
            )
            # CONTINUOUS 采集模式：保持与 PZT 扫描同步，峰位不漂移
            # 通过 trigger_active 标志控制是否执行 PI 反馈，而非控制采集本身
            task_ai.timing.cfg_samp_clk_timing(
                rate=SAMPLE_RATE,
                sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=n_samples,
            )

            task_ao.ao_channels.add_ao_voltage_chan(
                f"{device}/{AO_CHANNELS.replace(',', f',{device}/')}",
                min_val=AO_MIN, max_val=AO_MAX,
            )
            task_ao.start()
            self._write_bias_ao(task_ao)
            task_ai.start()  # 启动连续采集，与 PZT 扫描同步

            self.running = True
            self.is_calibrated = False
            self.control_enabled = False
            self.status_text = "DAQ 运行中 — 波形持续显示。请校准双峰后点击触发采集开始锁定"
            self._push({"type": "status", "message": self.status_text, "running": True})

            # 启动后不自动触发 PI 反馈，但波形始终持续刷新
            self.trigger_active = False

            while not self._stop_event.is_set():
                # ---- 处理外部请求 (不依赖触发) ----
                p = self.get_params_copy()

                if self.log_start_requested:
                    self.log_start_requested = False
                    self._start_peak_log()
                if self.log_stop_requested:
                    self.log_stop_requested = False
                    self._stop_peak_log()

                if self.reset_requested:
                    self.reset_requested = False
                    self.is_calibrated = False
                    self.control_enabled = False
                    self.auto_tune_running = False
                    self.trigger_active = False
                    self.tuner = None
                    tracker.reset(n_peaks=N_PEAKS, window_half=WINDOW_HALF_DEFAULT, alpha=EMA_ALPHA)
                    pi.reset()
                    lock_det.reset()
                    self._write_bias_ao(task_ao)
                    self.clear_err_history()
                    last_cal_indices = []
                    self.status_text = "已重置 — AO 回到 bias，采集已停止，请重新触发"
                    self._push({"type": "status", "message": self.status_text, "running": True})
                    continue

                if self.lock_off_requested:
                    self.lock_off_requested = False
                    self._enable_ao_hold()
                    self.control_enabled = False
                    self.auto_tune_running = False
                    self.tuner = None
                    pi.reset()
                    pi.ao0 = float(self.hold_ao0)
                    pi.ao1 = float(self.hold_ao1)
                    lock_det.reset()
                    self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                    self.status_text = (
                        f"闭环已停止 — AO 保持在停止瞬间电压："
                        f"AO0={self.hold_ao0:.4f} V, AO1={self.hold_ao1:.4f} V"
                    )
                    self._push({"type": "status", "message": self.status_text, "running": True})

                if self.lock_on_requested:
                    self.lock_on_requested = False
                    if self.is_calibrated:
                        self.auto_tune_running = False
                        self.tuner = None
                        start_ao0 = float(self.last_ao0)
                        start_ao1 = float(self.last_ao1)
                        pi.reset()
                        pi.ao0 = clamp(start_ao0, AO_MIN, AO_MAX)
                        pi.ao1 = clamp(start_ao1, AO_MIN, AO_MAX)
                        lock_det.reset()
                        self.ao_hold_enabled = False
                        self.control_enabled = True
                        self.trigger_active = True
                        self.status_text = f"闭环已启动 — 从当前 AO0={pi.ao0:.4f} V, AO1={pi.ao1:.4f} V 接管"
                    else:
                        self.control_enabled = False
                        self.status_text = "不能开始锁定：尚未校准"
                    self._push({"type": "status", "message": self.status_text, "running": True})

                if self.auto_tune_requested:
                    self.auto_tune_requested = False
                    if self.is_calibrated:
                        self._start_autotune(pi)
                    else:
                        self.status_text = "不能自动调参：尚未校准"
                        self._push({"type": "status", "message": self.status_text, "running": True})

                # ---- 校准请求：使用新的自动寻峰算法 ----
                if self.cal_requested:
                    self.cal_requested = False
                    # 用 auto_find_two_peaks，传入 AI1 用于确定 PZT 中心
                    cal_idx, found = auto_find_two_peaks(
                        signal=ai_buf[0] if frame_count > 0 else np.zeros(n_samples),
                        ai1_signal=ai_buf[1] if frame_count > 0 else None,
                    )
                    if found and len(cal_idx) >= N_PEAKS:
                        last_cal_indices, wh = self._apply_calibration(
                            tracker, pi, lock_det, cal_idx[:N_PEAKS], n_samples, "自动寻峰"
                        )
                        self.update_params({"peak_indices": last_cal_indices, "window_half": wh})
                        # 校准不重置 AO，保持当前电压
                        self._enable_ao_hold()
                        self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                        self._push({
                            "type": "calibration",
                            "success": True,
                            "message": self.status_text,
                            "ai0": ai_buf[0].tolist() if frame_count > 0 else [],
                            "ai1": ai_buf[1].tolist() if frame_count > 0 else [],
                            "cal_indices": [int(x) for x in last_cal_indices],
                            "all_indices": [int(x) for x in cal_idx],
                            "window_half": int(wh),
                        })
                    else:
                        self.is_calibrated = False
                        self.control_enabled = False
                        # 校准失败也保持 AO
                        self._enable_ao_hold()
                        self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                        self.status_text = "自动寻峰失败: 未找到足够的峰"
                        self._push({
                            "type": "calibration",
                            "success": False,
                            "message": self.status_text,
                            "ai0": ai_buf[0].tolist() if frame_count > 0 else [],
                            "ai1": ai_buf[1].tolist() if frame_count > 0 else [],
                            "cal_indices": [],
                            "all_indices": [],
                        })
                    continue

                if self.manual_cal_requested:
                    self.manual_cal_requested = False
                    manual_idx = p.get("peak_indices", DEFAULT_PEAK_INDICES)
                    last_cal_indices, wh = self._apply_calibration(
                        tracker, pi, lock_det, manual_idx, n_samples, "手动"
                    )
                    self.update_params({"peak_indices": last_cal_indices, "window_half": wh})
                    # 手动校准不重置 AO，保持当前电压
                    self._enable_ao_hold()
                    self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                    self._push({
                        "type": "calibration",
                        "success": True,
                        "message": self.status_text,
                        "ai0": ai_buf[0].tolist() if frame_count > 0 else [],
                        "ai1": ai_buf[1].tolist() if frame_count > 0 else [],
                        "cal_indices": [int(x) for x in last_cal_indices],
                        "all_indices": [],
                        "window_half": int(wh),
                    })
                    continue

                # ---- 采集一帧 (CONTINUOUS 模式，与 PZT 扫描同步) ----
                t0 = time.perf_counter()
                data = task_ai.read(number_of_samples_per_channel=n_samples)
                ai_buf[0, :] = data[0]
                ai_buf[1, :] = data[1]

                # ---- 窗口更新 ----
                if self.is_calibrated and tracker.cal_positions is not None:
                    tracker.window_half = dynamic_window_half(
                        list(tracker.cal_positions), p.get("window_half", WINDOW_HALF_DEFAULT)
                    )

                # ---- 根据状态处理数据 ----
                if not self.is_calibrated:
                    self._write_bias_ao(task_ao)
                    result = make_empty_result(control_enabled=False)
                elif not self.trigger_active:
                    # 触发未激活：只显示波形和峰位，AO 保持不动
                    if self.ao_hold_enabled:
                        ao0_hold, ao1_hold = self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                    else:
                        ao0_hold, ao1_hold = self._write_bias_ao(task_ao)
                    result = track_only(
                        ai_buf[0], tracker,
                        p.get("setpoint_110M", SETPOINT_110M),
                        p.get("setpoint_eom1", SETPOINT_EOM1),
                    )
                    result["ao0"] = float(ao0_hold)
                    result["ao1"] = float(ao1_hold)
                elif not self.control_enabled:
                    if self.ao_hold_enabled:
                        ao0_hold, ao1_hold = self._write_ao(task_ao, self.hold_ao0, self.hold_ao1)
                    else:
                        ao0_hold, ao1_hold = self._write_bias_ao(task_ao)
                    result = track_only(
                        ai_buf[0], tracker,
                        p.get("setpoint_110M", SETPOINT_110M),
                        p.get("setpoint_eom1", SETPOINT_EOM1),
                    )
                    result["ao0"] = float(ao0_hold)
                    result["ao1"] = float(ao1_hold)
                else:
                    sp110 = p.get("setpoint_110M", SETPOINT_110M)
                    speom = p.get("setpoint_eom1", SETPOINT_EOM1)

                    signal_smooth = smooth_moving_average(ai_buf[0], int(p.get("smooth_window", SMOOTH_WINDOW)))
                    heights, positions, found_count = tracker.track(signal_smooth)
                    # 峰测量：高度或面积，抑制宽线宽抖动
                    peak_mode = str(p.get("peak_mode", PEAK_MODE))
                    peak_avg_half = int(p.get("peak_avg_half", PEAK_AVG_HALF))
                    avg_heights = []
                    for i in range(N_PEAKS):
                        if i < found_count:
                            avg_heights.append(measure_peak(signal_smooth, int(round(positions[i])), peak_avg_half, peak_mode))
                        else:
                            avg_heights.append(0.0)
                    measured_110M = float(avg_heights[0]) if len(avg_heights) > 0 else 0.0
                    measured_eom1 = float(avg_heights[1]) if len(avg_heights) > 1 else 0.0

                    ctrl = pi.update(sp110, measured_110M, speom, measured_eom1, p)
                    ao0_out = clamp(ctrl["ao0"], AO_MIN, AO_MAX)
                    ao1_out = clamp(ctrl["ao1"], AO_MIN, AO_MAX)
                    self.ao_hold_enabled = False
                    self._write_ao(task_ao, ao0_out, ao1_out)

                    rel0 = rel_error(sp110, measured_110M)
                    rel1 = rel_error(speom, measured_eom1)
                    slow_rel0 = float(ctrl.get("rel0_slow", rel0))
                    slow_rel1 = float(ctrl.get("rel1_slow", rel1))
                    result = {
                        "ao0": float(ao0_out), "ao1": float(ao1_out),
                        "heights": [float(h) for h in avg_heights],
                        "positions": [float(x) for x in positions],
                        "found_count": int(found_count),
                        "lock_110M": bool(found_count >= 1 and abs(rel0) < 0.05),
                        "lock_eom1": bool(found_count >= 2 and abs(rel1) < 0.05),
                        "error_110M": float(sp110 - measured_110M),
                        "error_eom1": float(speom - measured_eom1),
                        "rel_error_110M": float(rel0),
                        "rel_error_eom1": float(rel1),
                        "rel_error_110M_slow": float(slow_rel0),
                        "rel_error_eom1_slow": float(slow_rel1),
                        "ao_updated_this_frame": bool(ctrl.get("should_update_ao", True)),
                        "control_enabled": True,
                    }
                    self._autotune_step(result, pi)

                loop_ms = (time.perf_counter() - t0) * 1000.0
                frame_count += 1
                self.append_err_history(
                    frame_count,
                    float(result.get("rel_error_110M", 0.0)),
                    float(result.get("rel_error_eom1", 0.0)),
                    bool(self.control_enabled),
                )
                self._write_peak_log_row(frame_count, result, tracker, p)

                msg = {
                    "type": "frame",
                    "ai0": ai_buf[0].tolist(),
                    "ai1": ai_buf[1].tolist(),
                    "ao0": round(float(result["ao0"]), 4),
                    "ao1": round(float(result["ao1"]), 4),
                    "ao_hold_enabled": bool(self.ao_hold_enabled),
                    "heights": [round(float(h), 5) for h in result["heights"][:N_PEAKS]],
                    "positions": [round(float(x), 1) for x in result["positions"][:N_PEAKS]],
                    "found_count": int(result["found_count"]),
                    "lock_110M": bool(result["lock_110M"]),
                    "lock_eom1": bool(result["lock_eom1"]),
                    "error_110M": round(float(result["error_110M"]), 6),
                    "error_eom1": round(float(result["error_eom1"]), 6),
                    "rel_error_110M": round(float(result.get("rel_error_110M", 0.0)), 5),
                    "rel_error_eom1": round(float(result.get("rel_error_eom1", 0.0)), 5),
                    "rel_error_110M_slow": round(float(result.get("rel_error_110M_slow", result.get("rel_error_110M", 0.0))), 5),
                    "rel_error_eom1_slow": round(float(result.get("rel_error_eom1_slow", result.get("rel_error_eom1", 0.0))), 5),
                    "ao_updated_this_frame": bool(result.get("ao_updated_this_frame", False)),
                    "control_enabled": bool(self.control_enabled),
                    "trigger_active": bool(self.trigger_active),
                    "auto_tune_running": bool(self.auto_tune_running),
                    "params": self.get_params_copy(),
                    "is_calibrated": bool(self.is_calibrated),
                    "window_half": int(tracker.window_half),
                    "cal_indices": [int(round(x)) for x in (tracker.cal_positions if tracker.cal_positions is not None else last_cal_indices)],
                    "loop_ms": round(loop_ms, 1),
                    "frame": frame_count,
                    "peak_log_active": bool(self.peak_log_active),
                    "peak_log_path": self.peak_log_path,
                    "peak_log_count": int(self.peak_log_count),
                }
                self._push(msg)

                if frame_count % 200 == 0:
                    gc.collect()

        except DaqError as e:
            self.status_text = f"DAQ 错误: {e}"
            print(f"\n[DAQ ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()
            self._push({"type": "error", "message": self.status_text})
            time.sleep(0.2)
            self._push({"type": "status", "message": "DAQ 已停止"})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.status_text = f"Python 错误: {e}\n{tb[-400:]}"
            print(f"\n[PYTHON ERROR]\n{tb}", flush=True)
            self._push({"type": "error", "message": self.status_text})
            time.sleep(0.2)
            self._push({"type": "status", "message": "DAQ 已停止"})
        finally:
            self.running = False
            self.control_enabled = False
            try:
                if task_ao is not None:
                    task_ao.write([BIAS_110M, BIAS_EOM1])
            except Exception:
                pass
            try:
                self._stop_peak_log()
            except Exception:
                pass
            for task in (task_ai, task_ao):
                if task is not None:
                    try:
                        task.stop()
                    except Exception:
                        pass
                    try:
                        task.close()
                    except Exception:
                        pass
            self._push({"type": "status", "message": "DAQ 已停止"})


app = FastAPI(title="NI USB-6363 Two-Peak Power Lock")
daq = DAQController()


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NI USB-6363 双峰功率锁定</title>
<style>
  :root {
    --bg:#f5f7fb; --panel:#ffffff; --panel2:#f8fafc; --text:#0f172a; --muted:#64748b;
    --border:#dbe3ef; --accent:#2563eb; --accent2:#7c3aed; --green:#16a34a;
    --red:#dc2626; --orange:#ea580c; --yellow:#ca8a04; --cyan:#0891b2;
    --shadow:0 10px 28px rgba(15,23,42,0.08);
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Inter','Segoe UI',system-ui,sans-serif;
         height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { background:rgba(255,255,255,0.92); backdrop-filter:blur(12px); padding:10px 14px;
           display:flex; align-items:center; justify-content:space-between; gap:12px;
           border-bottom:1px solid var(--border); box-shadow:0 2px 14px rgba(15,23,42,0.05); }
  header h1 { font-size:18px; font-weight:750; white-space:nowrap; letter-spacing:-0.02em; }
  #status { font-size:13px; color:var(--muted); max-width:540px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  main { flex:1; display:flex; padding:12px; gap:12px; min-height:0; }
  .plots { flex:1; display:flex; flex-direction:column; gap:12px; min-width:0; }
  .plot-box { flex:1; background:var(--panel); border-radius:16px; position:relative; overflow:hidden;
              border:1px solid var(--border); box-shadow:var(--shadow); }
  .plot-box .title { position:absolute; top:12px; left:14px; font-size:12px; color:var(--muted); z-index:2;
                     background:rgba(255,255,255,0.84); padding:4px 8px; border-radius:999px; border:1px solid var(--border); }
  canvas { width:100%; height:100%; display:block; cursor:crosshair; }
  .sidebar { width:360px; display:flex; flex-direction:column; gap:12px; flex-shrink:0; overflow:auto; padding-right:2px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:16px; padding:14px; box-shadow:var(--shadow); }
  .card h3 { font-size:14px; color:#1e293b; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }
  .buttonbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  button { border:none; border-radius:10px; padding:8px 13px; font-size:13px; cursor:pointer; font-weight:700; color:white;
           transition:transform 0.08s ease, filter 0.18s ease, box-shadow 0.18s ease; box-shadow:0 4px 12px rgba(15,23,42,0.12); }
  button:hover { filter:brightness(1.05); transform:translateY(-1px); }
  button:active { transform:translateY(0); }
  button:disabled { opacity:0.42; cursor:not-allowed; transform:none; box-shadow:none; }
  .btn-start { background:var(--green); }
  .btn-cal { background:var(--accent); }
  .btn-manual { background:var(--accent2); }
  .btn-lock { background:var(--red); }
  .btn-unlock { background:#475569; }
  .btn-reset { background:var(--orange); }
  .btn-quit { background:#991b1b; }
  .btn-log { background:#0f766e; }
  .btn-soft { background:#e2e8f0; color:#0f172a; box-shadow:none; border:1px solid var(--border); }
  .pill { display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; }
  .led-row { display:flex; align-items:center; gap:9px; margin:5px 0; }
  .led { width:12px; height:12px; border-radius:50%; background:#cbd5e1; border:1px solid rgba(15,23,42,0.08); }
  .led.on { background:var(--green); box-shadow:0 0 0 4px rgba(22,163,74,0.12); }
  .led.off { background:var(--red); box-shadow:0 0 0 4px rgba(220,38,38,0.12); }
  .led.idle { background:#cbd5e1; box-shadow:none; }
  .row { display:flex; justify-content:space-between; align-items:center; margin:6px 0; gap:10px; }
  .row label { color:var(--muted); font-size:12px; }
  .val { font-family:'Consolas','SFMono-Regular',monospace; font-size:13px; font-weight:650; }
  .grid2 { display:grid; grid-template-columns:1fr 1.4fr; gap:8px 10px; align-items:center; }
  .grid3 { display:grid; grid-template-columns:76px 1fr 1fr; gap:8px 8px; align-items:center; }
  .grid2 span, .grid3 span { font-size:12px; color:#334155; }
  .pv { font-family:'Consolas','SFMono-Regular',monospace; text-align:right; font-weight:650; color:#0f172a; }
  input, select { width:100%; background:#ffffff; color:var(--text); border:1px solid #cbd5e1;
    border-radius:10px; padding:7px 9px; font-family:'Consolas','SFMono-Regular',monospace; font-size:12px; }
  input:focus, select:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(37,99,235,0.14); }
  .param-section { font-size:12px; color:var(--accent); margin:10px 0 6px; font-weight:750; }
  .hint { color:var(--muted); font-size:11.5px; line-height:1.45; margin-top:8px; }
  .inline-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:10px; }
  .plot-img { width:100%; height:100%; object-fit:contain; display:block; background:#fff; }
</style>
</head>
<body>
<header>
  <h1>NI USB-6363 双峰功率锁定</h1>
  <div class="buttonbar">
    <button class="btn-start" id="btnStart" onclick="sendCmd('start')">▶ 启动 DAQ</button>
    <button class="btn-cal" id="btnTrigger" onclick="sendCmd('trigger')" disabled>⚡ 触发采集</button>
    <button class="btn-cal" id="btnCal" onclick="sendCmd('calibrate')" disabled>◎ 自动双峰</button>
    <button class="btn-manual" id="btnManual" onclick="sendCmd('manual_calibrate')" disabled>✓ 应用手动峰位</button>
    <button class="btn-lock" id="btnLock" onclick="sendCmd('lock_on')" disabled>● 开始锁定</button>
    <button class="btn-cal" id="btnTune" onclick="sendCmd('auto_tune')" disabled>⚙ Pattern Search 调参</button>
    <button class="btn-log" id="btnLogStart" onclick="sendCmd('log_start')" disabled>⬤ 记录峰值</button>
    <button class="btn-unlock" id="btnLogStop" onclick="sendCmd('log_stop')" disabled>□ 停止峰值</button>
    <button class="btn-unlock" id="btnUnlock" onclick="sendCmd('lock_off')" disabled>■ 停止锁定</button>
    <button class="btn-reset" id="btnReset" onclick="sendCmd('reset')" disabled>↺ 重置</button>
    <span id="status">未连接</span>
    <button class="btn-quit" onclick="sendCmd('quit')">× 退出</button>
  </div>
</header>

<main>
  <div class="plots">
    <div class="plot-box">
      <span class="title">AI0 — FP 透射信号：点击选择 P1/P2</span>
      <canvas id="cAI0"></canvas>
    </div>
    <div class="plot-box">
      <span class="title">AI1 — PZT 电压监视</span>
      <canvas id="cAI1"></canvas>
    </div>
    <div class="plot-box">
      <span class="title">Python 生成图：相对误差（%）随帧变化</span>
      <img id="errPlotImg" class="plot-img" alt="Err 百分比波动图">
    </div>
  </div>

  <div class="sidebar">
    <div class="card">
      <h3>运行状态</h3>
      <div class="led-row"><div class="led idle" id="ledControl"></div><span>闭环输出</span></div>
      <div class="led-row"><div class="led" id="led110"></div><span>110M 环路</span></div>
      <div class="led-row"><div class="led" id="ledEom1"></div><span>EOM 一阶环路</span></div>
      <div class="row"><label>AO0</label><span class="val" id="ao0">-- V</span></div>
      <div class="row"><label>AO1</label><span class="val" id="ao1">-- V</span></div>
      <div class="row"><label>Err110M</label><span class="val" id="err0">--</span></div>
      <div class="row"><label>RelErr110M raw</label><span class="val" id="relerr0">-- %</span></div>
      <div class="row"><label>RelErr110M slow</label><span class="val" id="relerr0slow">-- %</span></div>
      <div class="row"><label>ErrEOM1</label><span class="val" id="err1">--</span></div>
      <div class="row"><label>RelErrEOM1 raw</label><span class="val" id="relerr1">-- %</span></div>
      <div class="row"><label>RelErrEOM1 slow</label><span class="val" id="relerr1slow">-- %</span></div>
      <div class="row"><label>找到峰</label><span class="val" id="peaks">--/2</span></div>
      <div class="row"><label>窗口半宽</label><span class="val" id="wh">--</span></div>
      <div class="row"><label>Loop</label><span class="val" id="loop">-- ms</span></div>
      <div class="row"><label>帧</label><span class="val" id="frame">#0</span></div>
      <div class="row"><label>峰值记录</label><span class="val" id="logStatus">未记录</span></div>
    </div>

    <div class="card">
      <h3>双峰反馈</h3>
      <div class="grid3">
        <span></span><span class="pv">高度</span><span class="pv">位置</span>
        <span>P1 110M</span><span class="pv" id="p1">--</span><span class="pv" id="x1">--</span>
        <span>P2 EOM1</span><span class="pv" id="p2">--</span><span class="pv" id="x2">--</span>
      </div>
      <div class="inline-actions">
        <button class="btn-soft" onclick="setSPToCurrent()">SP = 当前峰高</button>
        <button class="btn-soft" onclick="zeroAO()">停止锁定/保持 AO</button>
      </div>
    </div>

    <div class="card">
      <h3>手动峰位</h3>
      <div class="row"><label>点击赋给</label><select id="pickTarget"><option value="1">P1 110M</option><option value="2">P2 EOM 一阶</option></select></div>
      <div class="grid2">
        <span>P1 110M</span><input id="idx_p1" type="number" step="1" value="642">
        <span>P2 EOM1</span><input id="idx_p2" type="number" step="1" value="754">
        <span>窗口半宽</span><input id="window_half" type="number" step="1" value="20">
        <span>峰值记录间隔帧</span><input id="log_every_n_frames" type="number" step="1" value="1">
      </div>
      <div class="hint">推荐流程：点击 AI0 图上的 110M 峰设为 P1，再点击 EOM 一阶峰设为 P2，然后点"应用手动峰位"。峰值记录只保存 P1/P2 的高度、位置、误差和 AO，不保存整条波形。若过夜文件太大，可把记录间隔帧设为 5 或 10。</div>
    </div>

    <div class="card">
      <h3>PI 参数</h3>
      <div class="param-section">环路1：110M → AO0</div>
      <div class="grid2">
        <span>SP</span><input id="sp_110M" type="number" step="0.001" value="0.026">
        <span>Kp</span><input id="kp_110M" type="number" step="0.5" value="5.0">
        <span>Ki</span><input id="ki_110M" type="number" step="0.05" value="0.0">
      </div>
      <div class="param-section">环路2：EOM 一阶 / 80M 路 → AO1</div>
      <div class="grid2">
        <span>SP</span><input id="sp_eom1" type="number" step="0.001" value="0.012">
        <span>Kp</span><input id="kp_eom1" type="number" step="0.5" value="5.0">
        <span>Ki</span><input id="ki_eom1" type="number" step="0.05" value="0.0">
      </div>
      <div class="param-section">稳定性 / 抗噪声 — 宽线宽滤波版</div>
      <div class="grid2">
        <span>峰测量模式</span><select id="peak_mode"><option value="height">峰高(平均)</option><option value="area">峰面积(积分)</option></select>
        <span>平滑窗口</span><input id="smooth_window" type="number" step="2" value="25">
        <span>积分/平均半径</span><input id="peak_avg_half" type="number" step="1" value="5">
        <span>相对误差死区</span><input id="deadband_rel" type="number" step="0.001" value="0.01">
        <span>峰高EMA α</span><input id="meas_ema_alpha" type="number" step="0.05" value="0.15">
        <span>AO步进限幅</span><input id="max_ao_step" type="number" step="0.01" value="0.03">
        <span>误差平均帧数</span><input id="error_lp_frames" type="number" step="1" value="20">
        <span>AO更新间隔帧</span><input id="control_update_every" type="number" step="1" value="3">
      </div>
      <div class="hint">宽线宽版：峰面积模式抗展宽（能量守恒），峰高模式用区域均值。平滑窗口 25、积分半径 5~25（面积模式建议 20~30）。EMA α 0.15、误差平均 20 帧、AO 每 3 帧、死区 1%。</div>
    </div>
  </div>
</main>

<script>
let ws = null;
let latestAI0 = null, latestAI1 = null;
let latestHeights = [0,0];
let latestPositions = [0,0];
let latestCalIndices = [642,754];

const canvases = { ai0: document.getElementById('cAI0'), ai1: document.getElementById('cAI1') };
let lastErrPlotRefresh = 0;

function refreshErrPlot(frame) {
  const img = document.getElementById('errPlotImg');
  if (!img) return;
  const now = Date.now();
  if (frame != null && frame - lastErrPlotRefresh < 5 && now - (img._ts || 0) < 1200) return;
  img._ts = now;
  if (frame != null) lastErrPlotRefresh = frame;
  img.src = `/err_plot.png?t=${now}`;
}

function getParams() {
  return {
    setpoint_110M: parseFloat(document.getElementById('sp_110M').value),
    kp_110M: parseFloat(document.getElementById('kp_110M').value),
    ki_110M: parseFloat(document.getElementById('ki_110M').value),
    setpoint_eom1: parseFloat(document.getElementById('sp_eom1').value),
    kp_eom1: parseFloat(document.getElementById('kp_eom1').value),
    ki_eom1: parseFloat(document.getElementById('ki_eom1').value),
    idx_p1: parseInt(document.getElementById('idx_p1').value),
    idx_p2: parseInt(document.getElementById('idx_p2').value),
    window_half: parseInt(document.getElementById('window_half').value),
    deadband_rel: parseFloat(document.getElementById('deadband_rel').value),
    meas_ema_alpha: parseFloat(document.getElementById('meas_ema_alpha').value),
    max_ao_step: parseFloat(document.getElementById('max_ao_step').value),
    error_lp_frames: parseInt(document.getElementById('error_lp_frames').value),
    control_update_every: parseInt(document.getElementById('control_update_every').value),
    peak_mode: document.getElementById('peak_mode').value,
    smooth_window: parseInt(document.getElementById('smooth_window').value),
    peak_avg_half: parseInt(document.getElementById('peak_avg_half').value),
    log_every_n_frames: parseInt(document.getElementById('log_every_n_frames').value),
  };
}

function updateParamInputs(params) {
  if (!params) return;
  const map = {
    setpoint_110M: 'sp_110M', kp_110M: 'kp_110M', ki_110M: 'ki_110M',
    setpoint_eom1: 'sp_eom1', kp_eom1: 'kp_eom1', ki_eom1: 'ki_eom1',
    deadband_rel: 'deadband_rel', meas_ema_alpha: 'meas_ema_alpha', max_ao_step: 'max_ao_step', error_lp_frames: 'error_lp_frames', control_update_every: 'control_update_every',
    peak_mode: 'peak_mode',
    smooth_window: 'smooth_window', peak_avg_half: 'peak_avg_half',
    log_every_n_frames: 'log_every_n_frames',
    window_half: 'window_half'
  };
  const active = document.activeElement ? document.activeElement.id : '';
  for (const [k, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el || active === id || params[k] == null) continue;
    const val = Number(params[k]);
    if (Number.isFinite(val)) {
      el.value = (Math.abs(val) < 1 ? val.toFixed(4) : val.toFixed(3));
    } else {
      el.value = params[k];  // 非数值参数（如 peak_mode 的 "height"/"area"）
    }
  }
}

function setStatus(text, color) {
  const st = document.getElementById('status');
  st.textContent = text;
  st.style.color = color || '#64748b';
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    setStatus('已连接', '#16a34a');
    document.getElementById('btnStart').disabled = false;
    refreshErrPlot(null);
  };
  ws.onmessage = (event) => handleMessage(JSON.parse(event.data));
  ws.onclose = () => {
    setStatus('断开 — 3秒后重连...', '#dc2626');
    document.getElementById('btnStart').disabled = false;
    ['btnCal','btnManual','btnLock','btnTune','btnLogStart','btnLogStop','btnTrigger','btnUnlock','btnReset'].forEach(id => document.getElementById(id).disabled = true);
    setTimeout(connect, 3000);
  };
  ws.onerror = () => setStatus('连接错误', '#dc2626');
}

function sendCmd(cmd) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (cmd === 'start') {
    document.getElementById('btnStart').disabled = true;
    ['btnCal','btnManual','btnReset','btnTrigger'].forEach(id => document.getElementById(id).disabled = false);
  } else if (cmd === 'trigger') {
    // trigger is a one-shot command, no state change needed
  } else if (cmd === 'quit') {
    ws.send(JSON.stringify({cmd:'stop'}));
    return;
  }
  ws.send(JSON.stringify({cmd:cmd, params:getParams()}));
}

function zeroAO() {
  sendCmd('lock_off');
}

['sp_110M','kp_110M','ki_110M','sp_eom1','kp_eom1','ki_eom1','idx_p1','idx_p2','window_half','log_every_n_frames','deadband_rel','meas_ema_alpha','max_ao_step','error_lp_frames','control_update_every','smooth_window','peak_avg_half','peak_mode'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({cmd:'params', params:getParams()}));
    }
  });
});

function setSPToCurrent() {
  if (latestHeights[0] > 0) document.getElementById('sp_110M').value = latestHeights[0].toFixed(5);
  if (latestHeights[1] > 0) document.getElementById('sp_eom1').value = latestHeights[1].toFixed(5);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({cmd:'params', params:getParams()}));
  }
}

function resizeCanvases() {
  for (const [key, c] of Object.entries(canvases)) {
    const box = c.parentElement;
    c.width = Math.floor(box.clientWidth * devicePixelRatio);
    c.height = Math.floor(box.clientHeight * devicePixelRatio);
    canvases[key + '_w'] = c.width;
    canvases[key + '_h'] = c.height;
  }
}
window.addEventListener('resize', resizeCanvases);
resizeCanvases();

function drawWaveform(canvas, data, color, w, h, marks) {
  if (!data || data.length < 2) return;
  const ctx = canvas.getContext('2d');
  const dpr = devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  ctx.save();
  ctx.scale(dpr, dpr);
  const cw = w / dpr, ch = h / dpr;

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, cw, ch);

  ctx.strokeStyle = 'rgba(148,163,184,0.28)';
  ctx.lineWidth = 0.6;
  for (let x = 0; x < cw; x += 60) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, ch); ctx.stroke(); }
  for (let y = 0; y < ch; y += 45) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cw, y); ctx.stroke(); }

  let ymin = Infinity, ymax = -Infinity;
  for (const v of data) { if (v < ymin) ymin = v; if (v > ymax) ymax = v; }
  if (ymax - ymin < 0.001) { ymin -= 0.5; ymax += 0.5; }
  const margin = (ymax - ymin) * 0.1;
  ymin -= margin; ymax += margin;
  const xScale = cw / (data.length - 1);
  const yScale = ch / (ymax - ymin);

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.7;
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = i * xScale;
    const y = ch - (data[i] - ymin) * yScale;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.fillStyle = '#64748b';
  ctx.font = '11px Consolas, monospace';
  ctx.fillText(ymax.toFixed(3), 8, 22);
  ctx.fillText(ymin.toFixed(3), 8, ch - 8);

  if (marks && marks.length) {
    const labels = ['P1 110M','P2 EOM1'];
    const cols = ['#16a34a','#7c3aed'];
    marks.forEach((idx, i) => {
      if (idx == null || isNaN(idx)) return;
      const x = idx * xScale;
      ctx.strokeStyle = cols[i % cols.length];
      ctx.lineWidth = 2.0;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, ch); ctx.stroke();
      ctx.fillStyle = cols[i % cols.length];
      ctx.font = 'bold 12px Consolas, monospace';
      ctx.fillText(labels[i] + ':' + Math.round(idx), x + 5, 46 + 18*i);
    });
  }
  ctx.restore();
}

canvases.ai0.addEventListener('click', ev => {
  if (!latestAI0 || latestAI0.length < 2) return;
  const rect = canvases.ai0.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const idx = Math.max(0, Math.min(latestAI0.length - 1, Math.round(x / rect.width * (latestAI0.length - 1))));
  const target = parseInt(document.getElementById('pickTarget').value);
  document.getElementById('idx_p' + target).value = idx;
  latestCalIndices[target - 1] = idx;
  if (target < 2) document.getElementById('pickTarget').value = String(target + 1);
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({cmd:'params', params:getParams()}));
  drawWaveform(canvases.ai0, latestAI0, '#0891b2', canvases.ai0_w, canvases.ai0_h, latestCalIndices);
});

function handleMessage(msg) {
  if (msg.type === 'ping') return;
  if (msg.type === 'status') {
    setStatus(msg.message, '#64748b');
    if (msg.running) {
      document.getElementById('btnStart').disabled = true;
      ['btnCal','btnManual','btnReset','btnTrigger'].forEach(id => document.getElementById(id).disabled = false);
    }
    return;
  }
  if (msg.type === 'error') {
    setStatus(msg.message, '#dc2626');
    document.getElementById('btnStart').disabled = false;
    ['btnCal','btnManual','btnLock','btnTune','btnLogStart','btnLogStop','btnTrigger','btnUnlock','btnReset'].forEach(id => document.getElementById(id).disabled = true);
    return;
  }
  if (msg.type === 'calibration') {
    latestAI0 = msg.ai0; latestAI1 = msg.ai1;
    latestCalIndices = msg.cal_indices || latestCalIndices;
    for (let i = 0; i < 2; i++) document.getElementById('idx_p'+(i+1)).value = latestCalIndices[i] || 0;
    if (msg.window_half) document.getElementById('window_half').value = msg.window_half;
    drawWaveform(canvases.ai0, latestAI0, '#0891b2', canvases.ai0_w, canvases.ai0_h, latestCalIndices);
    drawWaveform(canvases.ai1, latestAI1, '#ea580c', canvases.ai1_w, canvases.ai1_h, []);
    setStatus(msg.message, msg.success ? '#16a34a' : '#dc2626');
    document.getElementById('btnLock').disabled = !msg.success;
    document.getElementById('btnTune').disabled = !msg.success;
    document.getElementById('btnLogStart').disabled = !msg.success;
    document.getElementById('btnLogStop').disabled = true;
    document.getElementById('btnTrigger').disabled = false;
    document.getElementById('btnUnlock').disabled = false;
    return;
  }
  if (msg.type !== 'frame') return;

  latestAI0 = msg.ai0; latestAI1 = msg.ai1;
  latestHeights = msg.heights || latestHeights;
  latestPositions = msg.positions || latestPositions;
  latestCalIndices = msg.cal_indices || latestCalIndices;

  drawWaveform(canvases.ai0, msg.ai0, '#0891b2', canvases.ai0_w, canvases.ai0_h, latestCalIndices);
  drawWaveform(canvases.ai1, msg.ai1, '#ea580c', canvases.ai1_w, canvases.ai1_h, []);

  document.getElementById('ledControl').className = 'led ' + (msg.control_enabled ? 'on' : 'idle');
  document.getElementById('led110').className = 'led ' + (msg.control_enabled ? (msg.lock_110M ? 'on' : 'off') : 'idle');
  document.getElementById('ledEom1').className = 'led ' + (msg.control_enabled ? (msg.lock_eom1 ? 'on' : 'off') : 'idle');
  document.getElementById('ao0').textContent = Number(msg.ao0).toFixed(4) + ' V';
  document.getElementById('ao1').textContent = Number(msg.ao1).toFixed(4) + ' V';
  document.getElementById('err0').textContent = (msg.error_110M >= 0 ? '+' : '') + Number(msg.error_110M).toFixed(6);
  document.getElementById('relerr0').textContent = (msg.rel_error_110M >= 0 ? '+' : '') + (100 * Number(msg.rel_error_110M)).toFixed(2) + ' %';
  document.getElementById('relerr0slow').textContent = (msg.rel_error_110M_slow >= 0 ? '+' : '') + (100 * Number(msg.rel_error_110M_slow || 0)).toFixed(2) + ' %';
  document.getElementById('err1').textContent = (msg.error_eom1 >= 0 ? '+' : '') + Number(msg.error_eom1).toFixed(6);
  document.getElementById('relerr1').textContent = (msg.rel_error_eom1 >= 0 ? '+' : '') + (100 * Number(msg.rel_error_eom1)).toFixed(2) + ' %';
  document.getElementById('relerr1slow').textContent = (msg.rel_error_eom1_slow >= 0 ? '+' : '') + (100 * Number(msg.rel_error_eom1_slow || 0)).toFixed(2) + ' %';
  document.getElementById('peaks').textContent = msg.found_count + '/2';
  document.getElementById('wh').textContent = msg.window_half;
  document.getElementById('loop').textContent = Number(msg.loop_ms).toFixed(1) + ' ms';
  document.getElementById('frame').textContent = '#' + msg.frame;
  const logActive = !!msg.peak_log_active;
  const logCount = Number(msg.peak_log_count || 0);
  document.getElementById('logStatus').textContent = logActive ? (`记录中 ${logCount} 行`) : (logCount > 0 ? `已停止 ${logCount} 行` : '未记录');
  document.getElementById('btnLogStart').disabled = logActive || !msg.is_calibrated;
  document.getElementById('btnLogStop').disabled = !logActive;

  for (let i = 0; i < 2; i++) {
    document.getElementById('p'+(i+1)).textContent = Number(latestHeights[i] || 0).toFixed(5);
    document.getElementById('x'+(i+1)).textContent = Number(latestPositions[i] || 0).toFixed(1);
  }
  updateParamInputs(msg.params);
  document.getElementById('btnLock').disabled = !msg.is_calibrated || msg.control_enabled || msg.auto_tune_running;
  document.getElementById('btnTune').disabled = !msg.is_calibrated || msg.auto_tune_running;
  document.getElementById('btnUnlock').disabled = !msg.control_enabled && !msg.auto_tune_running;
  // 触发采集 = 开始锁定：锁定后禁用，未锁定时启用（需要已校准）
  const locked = msg.control_enabled || msg.auto_tune_running;
  document.getElementById('btnTrigger').disabled = locked || !msg.is_calibrated;
  document.getElementById('btnTrigger').textContent = locked ? '⏳ 锁定中...' : '⚡ 触发采集';
  refreshErrPlot(msg.frame);
}

connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/err_plot.png")
async def err_plot_png():
    # 只显示最新 500 点；下面 mean|err|/RMS 都按图上每一帧误差幅度计算。
    hist = daq.get_err_history()[-500:]
    fig, ax = plt.subplots(figsize=(9.2, 3.2), dpi=140)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if hist:
        frames = [h["frame"] for h in hist]
        rel0 = np.asarray([100.0 * h["rel0"] for h in hist], dtype=float)
        rel1 = np.asarray([100.0 * h["rel1"] for h in hist], dtype=float)

        abs0 = np.abs(rel0)
        abs1 = np.abs(rel1)
        mean_abs0 = float(np.mean(abs0))
        mean_abs1 = float(np.mean(abs1))
        rms0 = float(np.sqrt(np.mean(abs0 ** 2)))
        rms1 = float(np.sqrt(np.mean(abs1 ** 2)))
        signed_mean0 = float(np.mean(rel0))
        signed_mean1 = float(np.mean(rel1))
        all_rel = np.concatenate([rel0, rel1])
        all_abs = np.abs(all_rel)
        mean_abs_all = float(np.mean(all_abs))
        rms_all = float(np.sqrt(np.mean(all_abs ** 2)))
        signed_mean_all = float(np.mean(all_rel))

        ax.plot(frames, rel0, linewidth=1.6, label="P1 / 110M RelErr (%)")
        ax.plot(frames, rel1, linewidth=1.6, label="P2 / EOM1 RelErr (%)")

        control_frames = [h["frame"] for h in hist if h.get("control_enabled")]
        if control_frames:
            ax.axvspan(min(control_frames), max(control_frames), alpha=0.08, label="Lock enabled")

        stats_text = (
            f"P1 mean|err|={mean_abs0:.3f}%   RMS={rms0:.3f}%   bias={signed_mean0:+.3f}%\n"
            f"P2 mean|err|={mean_abs1:.3f}%   RMS={rms1:.3f}%   bias={signed_mean1:+.3f}%\n"
            f"All mean|err|={mean_abs_all:.3f}%   RMS={rms_all:.3f}%   bias={signed_mean_all:+.3f}%"
        )
        ax.text(
            0.015, 0.965, stats_text,
            transform=ax.transAxes,
            va="top", ha="left", fontsize=8.5,
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.82, edgecolor="0.8"),
        )

        all_vals = list(rel0) + list(rel1)
        if all_vals:
            ymin = min(all_vals)
            ymax = max(all_vals)
            span = max(0.5, ymax - ymin)
            pad = max(0.3, 0.12 * span)
            ax.set_ylim(ymin - pad, ymax + pad)
        ax.set_xlim(frames[0], frames[-1] if frames[-1] > frames[0] else frames[0] + 1)
    else:
        ax.text(0.5, 0.5, "No data yet\nStart DAQ and trigger to show error plot here", ha="center", va="center", fontsize=11, transform=ax.transAxes)
        ax.set_xlim(0, 1)
        ax.set_ylim(-1, 1)

    ax.axhline(0, linestyle="--", linewidth=1.0)
    ax.set_xlabel("Frame")
    ax.set_ylabel("Relative Error (%)")
    ax.set_title("Relative Error vs Frame (latest 500 points)")
    ax.grid(True, alpha=0.25)
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    await ws.send_json({
        "type": "status",
        "message": daq.status_text if daq.running else "就绪 — 请点击启动 DAQ",
        "running": daq.running,
    })

    async def forward_data():
        while True:
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: daq.data_queue.get(timeout=0.05)
                )
                await ws.send_json(msg)
            except queue.Empty:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception as e:
                    break
            except Exception as e:
                import traceback
                traceback.print_exc()
                break

    async def receive_commands():
        while True:
            try:
                data = await ws.receive_text()
                cmd = json.loads(data)
                action = cmd.get("cmd", "")
                params = cmd.get("params", {})
                if params:
                    daq.update_params(params)

                if action == "start":
                    if not daq.running:
                        daq.start()
                elif action == "stop":
                    if daq.running:
                        daq.stop()
                    return
                elif action == "calibrate":
                    daq.cal_requested = True
                elif action == "manual_calibrate":
                    daq.manual_cal_requested = True
                elif action == "reset":
                    daq.reset_requested = True
                elif action == "lock_on":
                    daq.lock_on_requested = True
                elif action == "lock_off":
                    daq.lock_off_requested = True
                elif action == "auto_tune":
                    daq.auto_tune_requested = True
                elif action == "log_start":
                    daq.log_start_requested = True
                elif action == "log_stop":
                    daq.log_stop_requested = True
                elif action == "trigger":
                    # 触发采集 = 开始锁定（启动 PI 反馈循环）
                    daq.lock_on_requested = True
                elif action == "params":
                    pass
                elif action == "quit":
                    if daq.running:
                        daq.stop()
                    await ws.close()
                    return
            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"[WS ERROR] {e}", flush=True)
                break

    try:
        await asyncio.gather(forward_data(), receive_commands())
    except Exception as e:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("=" * 70)
    print("  NI USB-6363 双峰功率锁定 — 浏览器控制面板 v12 — 宽线宽滤波版")
    print("=" * 70)
    print("  打开浏览器访问: http://localhost:8765")
    print(f"  设备: {DEVICE_NAME} | AI: {AI_CHANNELS} | 采样率: {SAMPLE_RATE} Hz | 点/帧: {SAMPLES_PER_SCAN}")
    print(f"  AO 模式: {OUTPUT_MODE} | 范围: [{AO_MIN}, {AO_MAX}] V | bias=({BIAS_110M}, {BIAS_EOM1})")
    print(f"  默认双峰: {DEFAULT_PEAK_INDICES} | 默认窗口半宽: {WINDOW_HALF_DEFAULT}")
    print("  v12 - 宽线宽滤波版: 加强信号平滑 (窗口 25)、峰区平均取高 (+-5)、PI 深度阻尼 (EMA 0.15, 平均 20 帧, 更新间隔 3 帧, 步进 0.03, 死区 1%)")
    print("  v11 新特性 (继承): FINITE 触发式采集、auto_find_two_peaks 自动寻峰、AO 电压保持")
    print("-" * 70)
    try:
        system = nidaqmx.system.System()
        devs = system.devices
        if devs:
            print(f"  检测到 {len(devs)} 个 DAQ 设备:")
            for d in devs:
                print(f"    - {d.name} ({d.product_type})")
        else:
            print("  未检测到任何 NI DAQ 设备")
        if devs and DEVICE_NAME not in [d.name for d in devs]:
            print(f"  警告: 配置设备 {DEVICE_NAME} 不在设备列表里")
    except Exception as e:
        print(f"  无法扫描设备: {e}")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
