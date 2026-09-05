"""双峰锁定的信号处理函数。

这个文件只处理 numpy 数组，不知道 USB-6363、HTTP、WebUI。
后面双峰识别算法主要就在这里讨论和替换。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class PeakMeasurement:
    """单个峰的测量结果。"""

    index: int
    value: float
    mode: str
    window_left: int
    window_right: int


@dataclass
class ManualAreaMeasurement:
    """手动面积测量结果。

    这是最朴素的第一版面积算法：
    用户手动给出左零点和右零点，程序只把这两个点之间的原始采样值相加。
    这里暂时不做滤波、不扣本底、不自动找峰，目的是先观察面积本身的抖动。
    """

    left_index: int
    right_index: int
    point_count: int
    value: float
    mode: str = "raw_sum"
    peak_index: int | None = None


@dataclass
class TopFractionMeasurement:
    """窗口内最高百分比采样点的平均值。

    这里保存本次计算真正使用的窗口、点数和比例，方便 WebUI、CSV 与离线分析
    核对统计口径。计算始终使用传入的原始波形，不在这里做任何平滑或本底扣除。
    """

    left_index: int
    right_index: int
    point_count: int
    selected_point_count: int
    percentage: float
    value: float


def smooth_moving_average(signal: np.ndarray, window: int) -> np.ndarray:
    """移动平均平滑。

    window 越大，抗噪声越强，但峰会变钝。
    """

    data = np.asarray(signal, dtype=float)
    window = int(window)
    if window < 3 or data.size < window:
        return data.copy()

    kernel = np.ones(window, dtype=float) / float(window)
    return np.convolve(data, kernel, mode="same")


def find_peak_near(signal: np.ndarray, center_index: int, window_half: int) -> int:
    """在给定中心附近寻找最高点。

    这是“手动峰位 + 小窗口跟踪”的第一版实现。
    它不负责判断这个峰在物理上是不是正确的双峰，只负责在窗口里找局部最大。
    """

    data = np.asarray(signal, dtype=float)
    if data.size == 0:
        raise ValueError("signal must not be empty")

    center = int(round(center_index))
    half = max(1, int(window_half))
    left = max(0, center - half)
    right = min(data.size, center + half + 1)
    if left >= right:
        raise ValueError("invalid peak search window")

    local_index = int(np.argmax(data[left:right]))
    return left + local_index


def measure_peak(
    signal: np.ndarray,
    peak_index: int,
    half_width: int,
    mode: str = "height",
) -> PeakMeasurement:
    """测量一个峰的高度或面积。

    mode="height"：取峰顶附近若干点的平均高度。
    mode="area"：取窗口内面积，适合峰变宽但总能量更稳定的情况。
    """

    data = np.asarray(signal, dtype=float)
    if data.size == 0:
        raise ValueError("signal must not be empty")

    index = int(round(peak_index))
    half = max(0, int(half_width))
    left = max(0, index - half)
    right = min(data.size, index + half + 1)
    window = data[left:right]
    if window.size == 0:
        raise ValueError("empty measurement window")

    mode_key = mode.strip().lower()
    if mode_key == "height":
        value = float(np.mean(window))
    elif mode_key == "area":
        value = float(np.trapz(window))
    else:
        raise ValueError("peak mode must be 'height' or 'area'")

    return PeakMeasurement(
        index=index,
        value=value,
        mode=mode_key,
        window_left=left,
        window_right=right,
    )


def measure_manual_area(
    signal: np.ndarray,
    left_index: int,
    right_index: int,
) -> ManualAreaMeasurement:
    """按用户手动指定的左右零点，直接求原始面积。

    注意：
    - left_index/right_index 是样本点索引，不是时间。
    - 返回的 value 是 sum(y[left:right+1])，单位近似是“V * 点数”。
    - 这里故意不用 trapz，因为你现在想先看最简单的逐点相加是否稳定。
    """

    data = np.asarray(signal, dtype=float)
    if data.size == 0:
        raise ValueError("signal must not be empty")

    left = int(round(left_index))
    right = int(round(right_index))
    if right < left:
        left, right = right, left

    left = max(0, min(left, data.size - 1))
    right = max(0, min(right, data.size - 1))
    if right <= left:
        raise ValueError("manual area right index must be greater than left index")

    window = data[left : right + 1]
    return ManualAreaMeasurement(
        left_index=left,
        right_index=right,
        point_count=int(window.size),
        value=float(np.sum(window)),
    )


def measure_top_fraction_mean(
    signal: np.ndarray,
    left_index: int,
    right_index: int,
    percentage: float = 10.0,
) -> TopFractionMeasurement:
    """计算指定窗口内最高若干百分比原始采样点的平均值。

    例如窗口有 251 点、percentage=10 时，选择点数为 round(25.1)=25。
    至少选择 1 点；percentage=100 时等价于整个窗口的算术平均值。

    使用 ``np.partition`` 只划分出最大的 K 个点，不需要完整排序整个窗口。
    这不会改变计算结果，但在窗口较大时比完整排序更直接。
    """

    data = np.asarray(signal, dtype=float)
    if data.size == 0:
        raise ValueError("signal must not be empty")

    percent = float(percentage)
    if not np.isfinite(percent) or percent <= 0 or percent > 100:
        raise ValueError("percentage must be > 0 and <= 100")

    left = int(round(left_index))
    right = int(round(right_index))
    if right < left:
        left, right = right, left

    left = max(0, min(left, data.size - 1))
    right = max(0, min(right, data.size - 1))
    if right <= left:
        raise ValueError("top fraction right index must be greater than left index")

    window = data[left : right + 1]
    if not np.all(np.isfinite(window)):
        raise ValueError("top fraction window contains non-finite values")

    selected_count = max(1, int(round(window.size * percent / 100.0)))
    selected_count = min(selected_count, int(window.size))
    split_index = int(window.size) - selected_count
    selected = np.partition(window, split_index)[split_index:]

    return TopFractionMeasurement(
        left_index=left,
        right_index=right,
        point_count=int(window.size),
        selected_point_count=selected_count,
        percentage=percent,
        value=float(np.mean(selected)),
    )


def track_and_measure_manual_area(
    signal: np.ndarray,
    left_index: int,
    right_index: int,
    smooth_window: int = 9,
    max_shift_per_frame: int = 5,
) -> ManualAreaMeasurement:
    """自动跟随峰位置的手动面积测量。

    用户先给一个初始面积窗口。程序在这个窗口内部寻找平滑后的最高点，
    然后保持窗口宽度不变，把窗口中心移动到这个最高点附近。

    为了避免被单个毛刺拖走，第一版做两个保守限制：
    - 找最高点前可以做一个很轻的移动平均平滑。
    - 每一帧窗口最多移动 max_shift_per_frame 个点。
    """

    data = np.asarray(signal, dtype=float)
    if data.size == 0:
        raise ValueError("signal must not be empty")

    left = int(round(left_index))
    right = int(round(right_index))
    if right < left:
        left, right = right, left

    left = max(0, min(left, data.size - 1))
    right = max(0, min(right, data.size - 1))
    if right <= left:
        raise ValueError("manual area right index must be greater than left index")

    width = right - left
    smoothed = smooth_moving_average(data, smooth_window)
    local = smoothed[left : right + 1]
    peak_index = left + int(np.argmax(local))

    target_left = peak_index - width // 2
    target_left = max(0, min(target_left, data.size - width - 1))

    max_shift = max(0, int(max_shift_per_frame))
    if max_shift > 0:
        shift = target_left - left
        if shift > max_shift:
            target_left = left + max_shift
        elif shift < -max_shift:
            target_left = left - max_shift

    target_right = target_left + width
    measurement = measure_manual_area(
        data,
        left_index=target_left,
        right_index=target_right,
    )
    measurement.peak_index = peak_index
    return measurement


def locate_and_measure_two_peaks(
    ai0: np.ndarray,
    peak_indices: list[int],
    smooth_window: int,
    search_window_half: int,
    measure_half: int,
    mode: str,
) -> tuple[np.ndarray, list[PeakMeasurement]]:
    """根据已知峰位，定位并测量两个峰。

    这是新系统的第一版稳定基线：先把“手动峰位锁定”做好。
    自动双峰识别算法以后会产出 peak_indices，再交给这个函数处理。
    """

    if len(peak_indices) != 2:
        raise ValueError("peak_indices must contain exactly two indices")

    smoothed = smooth_moving_average(ai0, smooth_window)
    measurements: list[PeakMeasurement] = []
    for peak_index in peak_indices:
        found_index = find_peak_near(smoothed, peak_index, search_window_half)
        measurements.append(measure_peak(smoothed, found_index, measure_half, mode))
    return smoothed, measurements


def auto_identify_two_peaks_placeholder() -> None:
    """自动双峰识别的占位函数。

    这里故意不实现旧 v12 的复杂算法。
    自动识别策略需要结合你的真实波形截图/数据再讨论，避免再次越改越乱。
    """

    raise NotImplementedError("auto two-peak identification needs a reviewed algorithm")
