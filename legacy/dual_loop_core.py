"""
================================================================================
NI USB-6363 四峰功率锁定 — 双环路核心算法模块 (v4)
================================================================================

【本文件在整个项目中的角色】
  这是整个功率锁定系统的算法核心。所有信号处理、峰识别、PI 控制、
  脱锁检测的数学逻辑都在这里实现。

  本文件是"纯 Python 算法模块" —— 它不依赖 LabVIEW，不依赖任何硬件，
  不依赖任何特定的调用框架。它只依赖 numpy 这一个第三方库。
  这使得它可以在以下任何环境中运行：
    - 命令行独立测试（通过 test_dual_loop.py）
    - LabVIEW 2019 的 Python Node 调用（通过 power_lock_bridge.py）
    - 任何其他 Python 脚本导入使用

【与其他文件的关系】
  被调用方:
    power_lock_bridge.py  ← 导入本文件的类和函数，暴露给 LabVIEW
    test_dual_loop.py     ← 导入本文件，做独立仿真验证

  调用方（本文件不调用项目内其他文件）

【运行依赖】
  - Python 3.6+ （LabVIEW 2019 Python Node 推荐 3.7）
  - numpy （用于数组运算和最小二乘拟合）
  - 不需要 matplotlib（那只是 test_dual_loop.py 的可视化需要）

【数据流方向】
  输入方向:
    AI0 数据 (FP 腔透射信号) ← LabVIEW DAQmx Read 或仿真生成器
    AI1 数据 (PZT 电压监视) ← LabVIEW DAQmx Read 或仿真生成器
    控制参数 (setpoint, Kp, Ki 等) ← LabVIEW 前面板控件

  输出方向:
    AO0 电压 (110M VVA 控制, 4-6V) → LabVIEW DAQmx Write 或仿真记录
    AO1 电压 (80M RF 控制, 4-6V)   → LabVIEW DAQmx Write 或仿真记录
    峰高度/位置 (4 个峰的诊断数据) → LabVIEW 前面板指示器
    锁定状态 (2 个布尔标志)       → LabVIEW Lock LED 指示器

【物理系统背景 —— 理解这些算法为什么这样设计】

  光学系统:
    808nm 激光 → 分两路 →
      路径A: 110MHz AOM → 取一级衍射光 (f0 + 110MHz)
      路径B: 80MHz AOM → 取零级光 → EOM 调制 (± 边带)
    两路合束 → FP 腔 → 透射光探测器

    PZT 扫描 FP 腔长时，探测器信号上出现 4 个目标峰:
      峰1: 110M AOM 一级光   ← 只受 AO0 控制 (正比关系)
      峰2: 80M AOM 零级载波  ← 受 AO1 控制 (反比关系!)
      峰3: EOM 下边带        ← 随峰2 联动 (同在零级光路)
      峰4: EOM 上边带        ← 随峰2 联动 (同在零级光路)

    此外还有大量高阶边带/残余模式产生的"虚假峰"，
    这就是为什么需要位置窗口法而非简单地取最高4个峰。

  控制关系:
    环路1 (AO0 → 110M VVA → 峰1): 正比
      AO0↑ → VVA 透过率↑ → 110M 一级光↑ → 峰1↑
      所以当峰1太低时, PI 应提高 AO0

    环路2 (AO1 → 80M RF 功率 → 峰2,3,4): 反比
      AO1↑ → RF 功率↑ → 80M AOM 衍射增强 → 零级光↓ → 峰2,3,4↓
      所以当峰2太低时, PI 应降低 AO1
      物理上这是反比关系，但在代码中我们这样处理:
        - PI 内部用正比公式算出一个"raw"值
        - 外部做反比映射: AO1 = 2 * bias - raw
        - 这样当 error>0 (峰低) 时 raw>bias, AO1<bias (RF减少, 零级增加, 峰升高)

【本文件包含的模块（按处理流水线顺序）】

  第〇部分: 信号平滑
    smooth_moving_average() — 滑动窗口移动平均，抑制探测器噪声

  第一部分: 峰检测底层函数
    find_all_local_maxima()  — 全信号局部极大值搜索（校准用）
    find_peak_in_window()    — 限定窗口内的最高峰搜索（运行用）
    refine_peak_parabolic()  — 抛物线拟合亚采样精度峰位置

  第二部分: 多峰跟踪器
    PeakTracker 类 — 校准 + EMA 位置跟踪 + 峰丢失保持

  第三部分: 双环路 PI 控制器
    DualPIController 类 — 两路独立 PI (110M正比 + 80M反比)

  第四部分: 脱锁检测器
    LockDetector 类 — 每环路独立 5 周期历史 + 安全输出队列

  第五部分: 全局校准
    calibrate_global() — 扫描周期全信号寻峰，选出最高 N 个作为目标

  第六部分: 单帧完整处理
    track_and_control() — 串联以上所有模块，处理一帧 PZT 扫描数据
================================================================================
"""

# ============================================================================
# 导入依赖
# ============================================================================
# numpy 是本模块唯一的外部依赖。
# 用于: 数组运算、卷积平滑、最小二乘拟合、统计分析
import numpy as np
import gc                                             # 垃圾回收: LabVIEW 嵌入式 Python 不自动 GC


# ---- 帧计数器 (用于周期性 GC) ----
_frame_count = 0                                      # 模块级帧计数
_GC_INTERVAL = 200                                    # 每 200 帧强制一次 GC (预分配后极少需要)


# ############################################################################
# 第〇部分：信号平滑
# ############################################################################
# 目的:
#   FP 腔透射信号中的探测器噪声会干扰峰检测（特别是局部极大值判断）。
#   在寻峰之前必须先做平滑。这里用移动平均（最简单的线性平滑），
#   因为它计算快、效果足够好，且不会像 Savitzky-Golay 那样可能引入伪峰。
#
#   为什么不用其他方法？
#   - S-G 滤波: 更好保留峰形，但计算量大，5000点信号上一帧要多 ~2ms
#   - 中值滤波: 对脉冲噪声好，但会压低峰高（对 PI 控制致命）
#   - FFT 低通: 计算量大，且 PZT 扫描周期非严格周期信号
#   移动平均是最朴实无华但最稳妥的选择。
# ############################################################################

# ---- 模块级预分配缓冲区 (避免每帧创建新 numpy 数组) ----
_smooth_buf = None                                     # 平滑输出缓冲区
_cumsum_buf = None                                     # 累积和缓冲区 (len+1, 含前置零)

def _ensure_buffers(size):
    """确保预分配的缓冲区尺寸正确。只在信号长度变化时重新分配。"""
    global _smooth_buf, _cumsum_buf
    if _smooth_buf is None or len(_smooth_buf) != size:
        _smooth_buf = np.empty(size, dtype=float)
        _cumsum_buf = np.empty(size + 1, dtype=float)   # +1 存放前置零

def smooth_moving_average(signal, window=7):
    """
    【目的】
      对输入信号做滑动窗口移动平均，消除探测器高频噪声。
      使用 cumsum 算法 (O(N)) + 预分配缓冲区 —— 零新数组分配。

    【算法】
      cumsum padded: Cs = [0, cumsum(signal)]
      中心部分: ma[i] = (Cs[i+half+1] - Cs[i-half]) / w
      边界部分: 截断窗口平均

    【参数】
      signal : np.ndarray (1D, float64)
          原始 FP 腔透射信号。长度 = PZT 扫描周期采样点数。
      window : int — 平滑窗口宽度，必须为奇数。

    【返回值】
      np.ndarray (1D, float64) — 指向预分配缓冲区 _smooth_buf 的视图。
      ⚠️ 调用者不应持有此引用超过一帧（下次调用会覆盖）。
    """
    global _smooth_buf, _cumsum_buf
    n = len(signal)
    window = int(window)

    # ---- 1. 参数安全检查 ----
    if window < 3 or n < window:
        _ensure_buffers(n)
        np.copyto(_smooth_buf[:n], signal)
        return _smooth_buf[:n]

    # ---- 2. 确保缓冲区尺寸 ----
    _ensure_buffers(n)

    half = window // 2

    # ---- 3. cumsum 前置零填充 ----
    _cumsum_buf[0] = 0.0                                # Cs[0] = 0
    np.cumsum(signal, out=_cumsum_buf[1:])              # Cs[1:] = cumsum(signal)  ← 写入预分配缓冲

    # ---- 4. 中心部分: 向量化 cumsum 差值 (零临时分配) ----
    # ma[i] = (Cs[i+half+1] - Cs[i-half]) / window  , i ∈ [half, n-half-1]
    # 用 np.subtract(out=) + 原地除法 /= 消除中间临时数组
    np.subtract(
        _cumsum_buf[2 * half + 1:],                   # Cs[i+half+1]
        _cumsum_buf[:-(2 * half + 1)],                # Cs[i-half]
        out=_smooth_buf[half:-half]                   # 直接写入目标缓冲区
    )
    _smooth_buf[half:-half] /= window                  # 原地除法, 零新分配

    # ---- 5. 边界修正: 截断窗口平均 ----
    for i in range(half):
        # 左边界: ma[i] = mean(signal[0 : i+half+1])
        _smooth_buf[i] = np.sum(signal[:i + half + 1]) / (i + half + 1)
        # 右边界: ma[-(i+1)] = mean(signal[-(i+half+1):])
        _smooth_buf[-(i + 1)] = np.sum(signal[-(i + half + 1):]) / (i + half + 1)

    return _smooth_buf


# ############################################################################
# 第一部分：峰检测底层函数
# ############################################################################
# 目的:
#   提供两个层级的峰检测能力:
#     全局级: find_all_local_maxima() — 在整条信号上找所有局部极大值
#     窗口级: find_peak_in_window() — 在指定位置附近找最高的局部极大值
#   还有抛物线拟合 refine_peak_parabolic() 提供亚采样精度。
#
#   为什么要两层？
#   - 校准阶段: 用全局法找出所有峰，按高度排序选目标峰
#   - 运行阶段: 用窗口法在已知位置附近搜索（排除虚假峰，计算快）
# ############################################################################

def find_all_local_maxima(signal, min_height_ratio=0.02):
    """
    【目的】
      在整条 FP 腔透射信号中找出所有局部极大值点。
      用于系统初始化时的"全局校准"——此时我们还不知道
      4 个目标峰在哪里，需要先看到全貌。

    【算法】
      一阶差分过零检测: diff[i-1]>0 且 diff[i]<=0 → 局部极大值
      然后用高度阈值过滤掉太小的噪声峰。

    【为什么不用 scipy.signal.find_peaks】
      为了保持零外部依赖（除了 numpy）。而且这个简单算法对于
      Lorentzian 峰形足够有效。

    【参数】
      signal : np.ndarray (1D, float64)
          已平滑的 FP 腔透射信号。
          注意: 调用者应确保传入的是平滑后的信号。

      min_height_ratio : float
          最小峰高相对于全局最高峰的比例。
          默认 0.02 = 2%，即高度不到最高峰 2% 的候选峰被丢弃。
          这主要过滤掉纯噪声引起的微小波动。

    【返回值】
      indices : list[int]
          所有满足条件的局部极大值的采样点索引，从小到大排序。
      heights : list[float]
          对应的高度值（信号在该索引处的原始值）。

    【调用位置】
      - calibrate_global() 第2步: 校准阶段全信号寻峰
    """
    # ---- 1. 计算一阶差分 ----
    n = len(signal)                                    # 信号总长度（采样点数）
    diff = np.diff(signal)                             # 一阶差分: diff[i] = signal[i+1] - signal[i]
                                                       # 长度 = n-1

    # ---- 2. 遍历检测局部极大值 ----
    # 局部极大值条件: diff[i-1] > 0 (前面在上升) 且 diff[i] <= 0 (后面在下降或持平)
    candidates = []                                    # 候选列表: [(index, height), ...]
    for i in range(1, n - 1):                          # 从第1个到倒数第2个（边界不检测）
        if diff[i - 1] > 0 and diff[i] <= 0:           #   上升→下降/持平 → 局部极大值
            candidates.append((i, signal[i]))           #   记录 (索引, 高度)

    # ---- 3. 如果连一个峰都没找到 ----
    if not candidates:                                 # 空列表 → 信号完全没有峰
        return [], []                                  #   返回两个空列表

    # ---- 4. 高度过滤 ----
    # 找到所有候选峰中的最大高度
    max_h = max(h for _, h in candidates)              # 全局最高峰的高度
    threshold = min_height_ratio * max_h               # 高度阈值 = 最高峰 * 2%
    # 过滤: 只保留高度 >= 阈值的候选峰
    candidates = [(i, h) for i, h in candidates if h >= threshold]

    # ---- 5. 按索引排序 ----
    # 确保返回的峰按从左到右（采样点从小到大的）顺序排列
    candidates.sort(key=lambda x: x[0])                # 按索引排序

    # ---- 6. 分离索引和高度 ----
    return [c[0] for c in candidates], [c[1] for c in candidates]


def find_peak_in_window(signal, center_idx, half_window):
    """
    【目的】
      在信号的指定窗口 [center-half_window, center+half_window] 内，
      找到最大的局部极大值。这是"位置窗口法"的核心——
      校准阶段确定了每个目标峰的位置后，运行阶段只在
      该位置附近搜索，从而排除虚假峰的干扰。

    【为什么用窗口搜索而不是全局搜索】
      FP 腔透射信号中大约有 30-50 个峰（含高阶边带），但只有 4 个是目标。
      如果每帧都做全局搜索再排序取前4，可能会把很强的虚假峰误判为目标峰。
      位置窗口法利用"峰在 PZT 扫描中的位置基本不变"这一事实，
      只在预期位置附近找，有效排除虚假峰。

    【参数】
      signal : np.ndarray (1D, float64)
          已平滑的 FP 腔透射信号。
      center_idx : int / float
          窗口中心的采样点索引。来自 EMA 跟踪后的 cal_positions。
      half_window : int
          窗口半宽（采样点数）。
          典型值 250 → 窗口总宽 500 采样点 → ±5% PZT 扫描范围。

    【返回值】
      (index, height) : (int, float) — 找到的最高局部极大值
      (None, None) — 窗口内没找到任何峰

    【调用位置】
      - PeakTracker.track(): 每帧对每个目标峰调用一次
    """
    # ---- 1. 计算窗口边界 ----
    # 确保窗口不越界
    lo = max(0, int(center_idx - half_window))         # 窗口左边界（最小为 0）
    hi = min(len(signal) - 1, int(center_idx + half_window)) # 窗口右边界（最大为 N-1）

    # ---- 2. 安全检查: 窗口必须至少包含 3 个点 ----
    if hi - lo < 3:                                    # 窗口太窄 → 无法做极大值检测
        return None, None                              #   返回"未找到"

    # ---- 3. 遍历窗口内所有点 ----
    best_idx = None                                    # 当前找到的最佳峰索引
    best_h = -1.0                                      # 当前找到的最佳峰高度（初始化为负值保证第一峰被记录）
    for j in range(lo + 1, hi):                        # 从 lo+1 到 hi-1（留出边界做差分判断）
        # 局部极大值判断: 比左边高 且 不比右边低
        if signal[j - 1] < signal[j] and signal[j] >= signal[j + 1]:
            if signal[j] > best_h:                     #   这个峰比之前找到的都高
                best_h = signal[j]                     #     更新最佳高度
                best_idx = j                           #     更新最佳索引

    # ---- 4. 返回结果 ----
    if best_idx is None:                               # 窗口内完全没有局部极大值
        return None, None                              #   (可能是峰漂出了窗口范围)
    return best_idx, best_h                            # 返回最佳峰的位置和高度


def refine_peak_parabolic(signal, idx, half_window=3):
    """
    【目的】
      用抛物线最小二乘拟合来细化峰位置和高度，达到亚采样精度。
      离散采样只能得到整数索引的峰位置，但实际峰顶点可能在两个采样点之间。
      抛物线拟合利用峰顶点附近的曲率信息，推算亚采样级的位置。

    【算法】
      对 idx 附近 2*half_window+1 个采样点 (默认7个)，
      拟合抛物线方程: y = a*x^2 + b*x + c
      顶点位置: x_peak = -b/(2a)
      顶点高度: y_peak = c - b^2/(4a)

      为什么用抛物线而不是高斯或 Lorentzian？
      - 峰顶点附近，任何平滑的峰形都可以用抛物线近似（泰勒展开到二阶）
      - 抛物线拟合是线性最小二乘 (对参数 a,b,c 是线性的)，计算快且稳定
      - 拟合窗口很小 (7 点), 抛物线近似足够精确

    【参数】
      signal : np.ndarray (1D, float64)
          已平滑的信号。
      idx : int
          粗检测得到的峰顶点索引 (来自 find_peak_in_window)。
      half_window : int
          拟合窗口半宽。默认 3 → 使用 idx-3 到 idx+3 共 7 个点。

    【返回值】
      (peak_position, peak_height) : (float, float)
          peak_position: 亚采样精度的峰位置（可为小数索引）
          peak_height: 抛物线拟合的峰高度

    【调用位置】
      - PeakTracker.track(): 对每个找到的峰做精细拟合
    """
    # ---- 1. 边界保护 ----
    # 如果 idx 太靠近信号边界，无法取到完整的拟合窗口
    if idx < half_window or idx >= len(signal) - half_window:
        return float(idx), float(signal[idx])          #   直接返回原始索引和高度，不做拟合

    # ---- 2. 准备拟合数据 ----
    # x: 相对于 idx 的位置偏移 [-3, -2, -1, 0, 1, 2, 3]
    x = np.arange(idx - half_window, idx + half_window + 1, dtype=float) - idx
    # y: 对应的信号值 (直接取切片, signal 已是 float64, 无需拷贝)
    y = signal[idx - half_window: idx + half_window + 1]

    # ---- 3. 构建设计矩阵并做最小二乘拟合 ----
    # A = [x^2, x, 1] — 抛物线的三个基函数
    A = np.column_stack([x**2, x, np.ones_like(x)])    # 形状 (7, 3)
    try:
        # np.linalg.lstsq: 最小二乘求解 A * [a,b,c]^T = y
        # rcond=None 使用默认的奇异值截断
        a, b, c = np.linalg.lstsq(A, y, rcond=None)[0] # a,b,c 是抛物线参数

        # ---- 4. 提取顶点 ----
        # a ≈ 0 → 峰顶太平 → 不做修正
        if abs(a) < 1e-12:                             # 抛物线几乎退化为直线
            return float(idx), float(signal[idx])       #   返回原始位置和高度

        # 顶点相对偏移: x_peak = -b/(2a)
        peak_pos_rel = -b / (2.0 * a)                   # 相对于 idx 的偏移量（可为小数）
        # 顶点高度: y_peak = c - b^2/(4a)
        peak_height = float(c - b**2 / (4.0 * a))       # 抛物线拟合高度

        # 绝对位置 = idx + 相对偏移
        # 高度不能为负（物理上探测器信号 ≥ 0）
        return float(idx + peak_pos_rel), max(0.0, peak_height)

    except np.linalg.LinAlgError:                       # 矩阵奇异（极少见）
        return float(idx), float(signal[idx])           #   降级返回原始值


# ############################################################################
# 第二部分：多峰位置窗口跟踪器 (PeakTracker)
# ############################################################################
# 目的:
#   这是整个系统的心脏——负责在每一帧 PZT 扫描数据中找到 4 个目标峰。
#   它维护跨帧状态 (cal_positions)，用 EMA 平滑跟踪温度漂移。
#
#   工作原理:
#     1. 校准: 记录 4 个目标峰的初始位置 (cal_positions[4])
#     2. 每帧: 在 cal_positions[i] ± window_half 范围内搜索最高局部极大值
#     3. 如果找到: 用抛物线拟合精确位置，用 EMA 更新 cal_positions 跟踪漂移
#     4. 如果丢失: 保持上一帧的值 (hold-last-value)
#
#   为什么用 EMA (指数移动平均) 跟踪峰位置？
#     - 温度漂移是缓慢的（秒级），EMA 的低通特性天然适合
#     - alpha=0.1 意味着新观测只占 10%，旧位置占 90%
#       这等价于 ~20 个周期的等效平均窗口
#     - 好处: 不需要存储历史数据，只需维护一个浮点数
# ############################################################################

class PeakTracker:
    """
    【目的】
      位置窗口峰跟踪器。这是 v3/v4 系统区别于简单"取最高4个峰"方法的核心。

    【解决的物理问题】
      FP 腔透射信号中有 ~30-50 个峰（4 个目标 + 大量高阶边带/残余模式）。
      简单的"按高度排序取前4"会被虚假峰干扰。
      位置窗口法利用一个物理事实: 在 PZT 扫描中，每个峰的"出现位置"
      （PZT 电压对应的采样点索引）是固定的（只随温度缓慢漂移）。
      因此可以:
        校准阶段: 记录 4 个目标峰的位置
        运行阶段: 只在各自的位置窗口内搜索 → 自动排除虚假峰

    【跨帧状态 (persistent state)】
      这些变量在每一帧调用 track() 后更新，在所有帧之间保持:

      cal_positions : np.ndarray (n_peaks,), float64
          当前 EMA 跟踪后的校准位置（采样点索引，可为小数）。
          每帧: cal_positions[i] = alpha * 新观测位置 + (1-alpha) * cal_positions[i]
          这是 EMA 的核心——追踪温度漂移引起的峰位置缓慢变化。

      last_heights : list[float]
          各峰最近一次成功检测到的高度。
          当峰丢失时用此值"填充"——这避免了峰短暂丢失时的零值噪声。

      last_positions : list[float]
          各峰最近一次成功检测到的亚采样位置。

      peak_lost : list[bool]
          各峰在当前帧是否丢失。用于诊断和 found_count 统计。

      calibrated : bool
          是否已完成校准。未校准时 track() 返回初始值。

    【参数】
      n_peaks : int (默认 4)
          目标峰数量。
      window_half : int (默认 250)
          搜索窗口半宽（采样点数）。
          window_half=250 → 窗口总宽 500 点 → 占 5000 点扫描周期的 ±5%
      alpha : float (默认 0.1)
          EMA 平滑系数。0 < alpha < 1。
          越小 → 越抗噪声，但跟踪越慢。
          越大 → 越快跟踪漂移，但越容易被噪声带偏。
          0.1 是经验值，对应 ~20 周期的等效平均。
    """

    def __init__(self, n_peaks=4, window_half=250, alpha=0.1):
        """
        【目的】
          创建峰跟踪器并设置参数。此时尚未校准，cal_positions 为 None。

        【参数详见类文档字符串】
        """
        # ---- 存储配置参数 ----
        self.n_peaks = int(n_peaks)                    # 目标峰数，强制整数（防御浮点误传）
        self.window_half = int(window_half)             # 窗口半宽，强制整数
        self.alpha = float(alpha)                       # EMA 系数，强制浮点

        # ---- 初始化状态为"未校准" ----
        self.calibrated = False                        # 必须调用 calibrate() 才会变为 True
        self.cal_positions = None                      # 校准位置数组，calibrate() 时创建
        self.last_heights = [0.01] * n_peaks           # 初始高度 = 0.01V (非零避免除零)
        self.last_positions = [0.0] * n_peaks          # 初始位置 = 0.0
        self.peak_lost = [False] * n_peaks             # 初始状态: 全部"丢失中"

    def calibrate(self, cal_positions):
        """
        【目的】
          记录校准位置并标记已校准。由外部在校准阶段调用。

        【参数】
          cal_positions : list[float] or np.ndarray
              来自 calibrate_global() 的 n_peaks 个采样点索引。
              顺序必须对应峰1→峰2→峰3→峰4（从左到右）。
              索引可为浮点数（来自抛物线拟合的亚采样位置）。

        【副作用】
          设置 self.calibrated = True
          初始化所有 last_* 值为校准状态
        """
        # 将校准位置转为 float64 数组，便于后续 EMA 运算
        self.cal_positions = np.array(cal_positions, dtype=float)

        # 标记已校准 → track() 开始真正工作
        self.calibrated = True

        # 初始化"上一帧"值为校准值
        # 这样即使第一帧找不到峰也不会返回全零
        self.last_positions = list(self.cal_positions) # 位置初始化为校准位置
        self.last_heights = [0.01] * self.n_peaks      # 高度初始化为 0.01 (非零安全值)
        self.peak_lost = [False] * self.n_peaks        # 初始全部标记为"正常"

    def track(self, signal):
        """
        【目的】
          在一个 PZT 扫描周期的平滑信号中跟踪所有已校准的目标峰。
          这是每帧都要调用的核心函数。

        【处理流程 (对每个目标峰 i)】
          1. 取 cal_positions[i] 作为搜索窗口中心
          2. 调用 find_peak_in_window() 在窗口内找最高局部极大值
          3. 如果找到:
             a. 调用 refine_peak_parabolic() 做亚采样细化
             b. EMA 更新 cal_positions[i] 跟踪漂移
             c. 更新 last_heights[i] 和 last_positions[i]
             d. 标记 peak_lost[i] = False
          4. 如果没找到:
             a. 返回 last_heights[i] 和 last_positions[i] (保持上一帧值)
             b. 标记 peak_lost[i] = True

        【参数】
          signal : np.ndarray (1D, float64)
              已平滑的 FP 腔透射信号。调用者负责先做平滑。
              长度 = PZT 扫描周期采样点数。

        【返回值】
          heights : list[float] (长度 n_peaks)
              各峰的抛物线拟合高度（物理量: 探测器电压 V）。
              如果某峰丢失，返回上一帧保持值。
          positions : list[float] (长度 n_peaks)
              各峰的亚采样位置（采样点索引）。
          found_count : int
              当前帧成功找到的峰数 (0 ~ n_peaks)。
        """
        # ---- 0. 安全检查: 是否已校准 ----
        if not self.calibrated:                        # 还没校准 → 无目标峰位置
            # 返回上一帧的值和 found_count=0
            return (list(self.last_heights), list(self.last_positions), 0)

        # ---- 初始化输出容器 ----
        heights_out = []                               # 将要返回的高度列表
        positions_out = []                             # 将要返回的位置列表
        found_count = 0                                # 成功计数

        # ---- 逐个峰处理 ----
        for i in range(self.n_peaks):
            # ---- 1. 取当前搜索窗口中心 ----
            center = int(self.cal_positions[i])        # EMA跟踪后的当前位置 → 整数索引

            # ---- 2. 窗口内搜索最高局部极大值 ----
            best_idx, best_h = find_peak_in_window(signal, center, self.window_half)

            # ---- 3. 如果找到 → 更新状态 ----
            if best_idx is not None:
                # ---- 3a. 抛物线拟合细化 ----
                pos_ref, h_ref = refine_peak_parabolic(signal, best_idx)

                # 记录输出
                heights_out.append(float(h_ref))       # 拟合高度
                positions_out.append(float(pos_ref))   # 亚采样位置

                # ---- 3b. EMA 更新校准位置 ----
                # 核心公式: new_cal = alpha * observed + (1-alpha) * old_cal
                # alpha=0.1 → 新观测占10%，旧值占90% → 平滑缓慢漂移
                self.cal_positions[i] = (
                    self.alpha * pos_ref
                    + (1.0 - self.alpha) * self.cal_positions[i]
                )

                # ---- 3c. 更新"上一帧"缓存 ----
                self.last_heights[i] = float(h_ref)    # 存为 float (防御 numpy 类型)
                self.last_positions[i] = float(pos_ref)
                self.peak_lost[i] = False              # 标记: 本帧成功找到
                found_count += 1

            # ---- 4. 如果没找到 → 保持上一帧值 ----
            else:
                # 峰丢失的处理策略:
                #   保持 last_heights/last_positions 不变，不做 EMA 更新
                #   原因: 没观测到就不能更新位置估计
                #   但如果连续多帧丢失 → 可能是真的漂出窗口了
                #   → 由脱锁检测在更上层处理（触发重新校准）
                heights_out.append(self.last_heights[i])
                positions_out.append(self.last_positions[i])
                self.peak_lost[i] = True               # 标记: 本峰丢失

        # ---- 5. 返回 ----
        return heights_out, positions_out, found_count

    def reset(self, n_peaks=None, window_half=None, alpha=None):
        """
        【目的】
          重置所有状态到初始未校准态。用于:
          - 系统重新校准前
          - 异常恢复 (峰持续丢失后手动重置)
          - 运行中修改参数 (n_peaks, window_half, alpha)

        【参数】
          所有参数可选，不传则保持当前值。
        """
        if n_peaks is not None:
            self.n_peaks = int(n_peaks)                # 更新目标峰数
        if window_half is not None:
            self.window_half = int(window_half)         # 更新窗口半宽
        if alpha is not None:
            self.alpha = float(alpha)                   # 更新 EMA 系数

        # 重置所有状态变量
        self.calibrated = False                        # 回到未校准
        self.cal_positions = None                      # 清除校准位置
        self.last_heights = [0.01] * self.n_peaks      # 重置高度缓存
        self.last_positions = [0.0] * self.n_peaks     # 重置位置缓存
        self.peak_lost = [False] * self.n_peaks        # 重置丢失标记


# ############################################################################
# 第三部分：双环路 PI 控制器 (DualPIController)
# ############################################################################
# 目的:
#   这是 v4 区别于 v3 的最大变化——从单环路变为双环路独立 PI 控制。
#
#   物理背景:
#     v3 只控制 110M AOM (AO0)，假设 80M 路径的功率是稳定的。
#     但实际上 80M AOM 的 RF 驱动功率也会波动，导致零级光强度变化，
#     进而影响峰2、峰3、峰4。
#     v4 为 80M AOM 添加了独立的 PI 环路 (AO1)，形成双环路控制。
#
#   控制关系:
#     环路1 (110M 正比): AO0 → 110M VVA → AOM一级光 → 峰1
#       AO0↑ → 峰1↑  (正比关系，直接反馈)
#
#     环路2 (80M 反比): AO1 → 80M RF功率 → AOM衍射 → 零级光 → 峰2
#       AO1↑ → RF↑ → 衍射↑ → 零级↓ → 峰2↓  (反比关系!)
#       在代码中: PI 内部算正比，外部做 AO1 = 2*bias - raw 反转
#
#   PI 算法 (标准并行形式):
#     output = bias + Kp * error + Ki * integral(error) * dt
#     带积分抗饱和 (anti-windup) 和输出限幅
#
#   跨帧状态:
#     i_sum_110M: 环路1 积分累积项
#     i_sum_80M:  环路2 积分累积项
#     这两个值在帧之间保持，实现"积分"的物理意义。
# ############################################################################

class DualPIController:
    """
    【目的】
      双环路并行 PI 控制器。同时控制两个独立的 AOM 通道的 VVA/RF 驱动电压。

    【两个环路的控制逻辑差异】

      环路1 — 110M AOM 一级光 (AO0 通道):
        关系: 正比。AO0 ↑ → VVA 透过率 ↑ → AOM 一级光 ↑ → 峰1 ↑
        公式: AO0 = bias_110M + Kp_110M * error + Ki_110M * I_sum_110M
        输出: [output_min, output_max] = [4.0, 6.0] V (VVA 线性工作区)
        error = setpoint_110M - peak1_height
        如果 error > 0 (峰太低): PI 增大 AO0 → VVA 开大 → 峰升高 ✓

      环路2 — 80M AOM 零级光 (AO1 通道):
        关系: 反比。AO1 ↑ → RF 功率 ↑ → AOM 衍射 ↑ → 零级光 ↓ → 峰2 ↓
        公式: pi_raw = bias_80M + Kp_80M * error + Ki_80M * I_sum_80M
              AO1 = 2 * bias_80M - pi_raw   ← 反比映射
        输出: [output_min, output_max] = [4.0, 6.0] V
        error = setpoint_80M - peak2_height
        如果 error > 0 (峰太低): PI 内部 pi_raw > bias, 反比后 AO1 < bias
          → RF 减少 → 零级光增加 → 峰升高 ✓

    【为什么反比不在 PI 内部直接改符号而是做 2*bias - raw】
      如果直接在 PI 内部把 Kp 取负值 (Kp = -0.1)，那么 error>0 → p_term<0 → output<bias，
      在数学上等价。但这样做有两个问题:
        1. 可读性差: Kp 为负值违反直觉，调试时容易出错
        2. 积分项反转: 负 Kp 可以，但负 Ki 不自然
      所以选择"内部正比 + 外部反转"的方式，保持 PI 逻辑一致。

    【积分抗饱和 (Anti-Windup)】
      当输出达到限幅边界时，如果 error 还在把积分往同方向推，
      积分继续累加会导致"饱和"——恢复到正常范围需要很长时间。
      抗饱和措施: 先限幅积分项 (i_min, i_max)，再计算 output，
      然后再限幅 output。双重限幅确保快速恢复。

    【跨帧状态】
      i_sum_110M : float — 环路1 积分累积 (V*s)
      i_sum_80M  : float — 环路2 积分累积 (V*s)
      last_output_110M : float — 用于状态监控和 Hold 恢复
      last_output_80M  : float
      last_error_110M : float
      last_error_80M  : float
    """

    def __init__(self,
                 kp_110M=0.1, ki_110M=2.0, bias_110M=5.0,
                 kp_80M=0.1, ki_80M=2.0, bias_80M=5.0,
                 output_min=4.0, output_max=6.0,
                 i_min=-1.0, i_max=1.0):
        """
        【目的】
          初始化双环路 PI 控制器，设置两个环路的参数。

        【参数】
          kp_110M : float (默认 0.1)
              环路1 比例增益。无量纲。
              含义: error=0.1V → p_term=0.01V → AO0改变 0.01V
          ki_110M : float (默认 2.0)
              环路1 积分增益。单位: 1/s。
              含义: 恒定 error=0.1V → 1秒后 I项=0.2V → AO0改变 0.2V
          bias_110M : float (默认 5.0)
              环路1 偏置电压 (V)。error=0 时的稳态输出。
              VVA 的中心工作点: 5V 对应 ~100% 透过率。
          kp_80M, ki_80M, bias_80M : float
              环路2 对应参数。结构与环路1 完全相同，
              但实际作用于 80M AOM 反比控制。
          output_min : float (默认 4.0)
              AO 输出下限 (V)。VVA 在 4V 时透过率最低(接近 0)。
          output_max : float (默认 6.0)
              AO 输出上限 (V)。VVA 在 6V 时透过率最高(~1.5x)。
          i_min : float (默认 -1.0)
              积分项下限。防止负向饱和。
          i_max : float (默认 1.0)
              积分项上限。PI 修正范围 ±1V，所以 I 项也限制在 ±1V。
        """
        # ---- 环路1 参数 (110M, 正比) ----
        self.kp_110M = float(kp_110M)                  # 比例增益
        self.ki_110M = float(ki_110M)                  # 积分增益 (1/s)
        self.bias_110M = float(bias_110M)              # 偏置电压 = 5.0V

        # ---- 环路2 参数 (80M, 反比) ----
        self.kp_80M = float(kp_80M)                    # 比例增益
        self.ki_80M = float(ki_80M)                    # 积分增益 (1/s)
        self.bias_80M = float(bias_80M)                # 偏置电压 = 5.0V

        # ---- 共同限幅参数 ----
        self.output_min = float(output_min)             # AO 输出下限 = 4.0V
        self.output_max = float(output_max)             # AO 输出上限 = 6.0V
        self.i_min = float(i_min)                       # 积分项下限 = -1.0V
        self.i_max = float(i_max)                       # 积分项上限 = +1.0V

        # ---- 初始化状态变量 ----
        self.i_sum_110M = 0.0                          # 环路1 积分累积, 初始为 0
        self.i_sum_80M = 0.0                           # 环路2 积分累积, 初始为 0

        # 上次输出: 初始化为各自的偏置值
        # 这意味着系统启动时 AO 输出 5V (VVA 中心透过率)
        self.last_output_110M = float(bias_110M)        # 上次 AO0 输出
        self.last_output_80M = float(bias_80M)          # 上次 AO1 输出
        self.last_error_110M = 0.0                      # 上次环路1 误差
        self.last_error_80M = 0.0                       # 上次环路2 误差

    # ==================================================================
    # 环路1: 110M AOM 一级光 — 正比控制
    # ==================================================================

    def update_110M(self, setpoint, measured, dt, hold=False):
        """
        【目的】
          环路1 的单帧 PI 更新——计算新的 AO0 电压来控制 110M AOM 的一级光功率。

        【PI 公式】
          error = setpoint - measured
          p_term = Kp_110M * error
          if not hold: i_sum += error * dt (带限幅)
          i_term = Ki_110M * i_sum
          raw = bias_110M + p_term + i_term
          output = clamp(raw, output_min, output_max)

        【参数】
          setpoint : float
              峰1 的目标高度 (V)。前面板设置值。
              实际意义的物理量: 期望的探测器输出电压。
          measured : float
              峰1 的当前实测高度 (V)。来自 PeakTracker 的输出。
          dt : float
              扫描周期 = 1 / scan_freq。典型值 0.05s (20Hz扫描)。
          hold : bool
              True 时冻结积分项更新。
              使用场景: 主系统脉冲期间 (如 MOT 的 flash 阶段)，
              此时光功率有已知的瞬时扰动，不应让积分项对此做出反应。

        【返回值】
          ao0_output : float
              环路1 的控制输出电压 (4-6V)，用于驱动 110M VVA。
          error : float
              当前误差 = setpoint - measured。用于诊断显示。
        """
        # ---- 1. 计算误差 ----
        # error > 0 → 峰太低 → 需要增大 AO0 (正比关系)
        # error < 0 → 峰太高 → 需要减小 AO0
        error = float(setpoint) - float(measured)      # 当前误差 (V)
        self.last_error_110M = error                    # 记录用于状态监控

        # ---- 2. 判断是否 Hold ----
        if not hold:
            # === 2a. 正常模式 ===

            # P 项: 比例增益 × 当前误差
            p_term = self.kp_110M * error               # P 项贡献 (V)

            # I 项: 积分累积 + 抗饱和
            # 每个周期累加 error * dt，物理意义是误差对时间的积分
            self.i_sum_110M += error * dt               # 积分累积 (V*s)
            # 抗饱和: 限制积分项在 [i_min, i_max] = [-1.0, 1.0] V 内
            if self.i_sum_110M > self.i_max:            #   积分项超过上限
                self.i_sum_110M = self.i_max            #   钳位到上限
            elif self.i_sum_110M < self.i_min:          #   积分项低于下限
                self.i_sum_110M = self.i_min            #   钳位到下限
            i_term = self.ki_110M * self.i_sum_110M     # I 项贡献 (V)

            # 总输出 = 偏置 + P项 + I项
            raw = self.bias_110M + p_term + i_term      # 未经限幅的原始输出
        else:
            # === 2b. Hold 模式 ===
            # 冻结积分 (不累加 error*dt)，但保留已有的积分值
            # 输出 = 偏置 + I项 (没有 P项，因为瞬时误差不可靠)
            raw = self.bias_110M + self.ki_110M * self.i_sum_110M

        # ---- 3. 输出限幅 ----
        # 确保 AO0 在 [4.0, 6.0] V 范围内
        # VVA 线性工作区: 4-6V，超出范围无物理意义
        if raw > self.output_max:                       # 超过上限 6.0V
            raw = self.output_max                       #   钳位到 6.0V
        elif raw < self.output_min:                     # 低于下限 4.0V
            raw = self.output_min                       #   钳位到 4.0V

        # ---- 4. 保存并返回 ----
        self.last_output_110M = raw                     # 更新上次输出缓存
        return raw, error                               # 返回 (AO0电压, 误差)

    # ==================================================================
    # 环路2: 80M AOM 零级光 — 反比控制
    # ==================================================================

    def update_80M(self, setpoint, measured, dt, hold=False):
        """
        【目的】
          环路2 的单帧 PI 更新——计算新的 AO1 电压来控制 80M AOM 的 RF 功率，
          从而间接控制零级光（峰2,3,4）的功率。

        【反比处理策略】
          PI 内部用与环路1 完全相同的正比公式计算 pi_raw。
          然后通过反比映射: AO1 = 2 * bias_80M - pi_raw
          将"正的 PI 修正"反转。

          WHY THIS WORKS:
            峰2太低 → error>0 → pi_raw > bias_80M → AO1 < bias_80M
            → RF功率降低 → 80M AOM 衍射减弱 → 零级光增强 → 峰2升高 ✓

            峰2太高 → error<0 → pi_raw < bias_80M → AO1 > bias_80M
            → RF功率增加 → 80M AOM 衍射增强 → 零级光减弱 → 峰2降低 ✓

        【参数】
          setpoint : float — 峰2 的目标高度 (V)
          measured : float — 峰2 的当前实测高度 (V)
          dt : float — 扫描周期 (s)
          hold : bool — 是否冻结积分

        【返回值】
          ao1_output : float — AO1 电压 (4-6V)，已做反比处理
          error : float — 当前误差
        """
        # ---- 1. 计算误差 ----
        error = float(setpoint) - float(measured)      # 当前误差 (V)
        self.last_error_80M = error                     # 记录用于状态监控

        # ---- 2. PI 计算（正比形式）- 与 update_110M 完全相同 ----
        if not hold:
            # P 项
            p_term = self.kp_80M * error                # 比例项 (V)

            # I 项 + 抗饱和
            self.i_sum_80M += error * dt                # 积分累积
            if self.i_sum_80M > self.i_max:             #   抗饱和上限
                self.i_sum_80M = self.i_max
            elif self.i_sum_80M < self.i_min:           #   抗饱和下限
                self.i_sum_80M = self.i_min
            i_term = self.ki_80M * self.i_sum_80M       # 积分项 (V)

            # 正比形式的原始 PI 输出
            pi_raw = self.bias_80M + p_term + i_term    # 未经反比处理和限幅
        else:
            # Hold: 冻结误差积分
            pi_raw = self.bias_80M + self.ki_80M * self.i_sum_80M

        # ---- 3. ★ 反比处理 ★ ----
        # 这是环路2 与环路1 的关键区别。
        # 数学: ao1 = 2 * bias - pi_raw
        # 当 pi_raw = bias 时, ao1 = bias → RF 不变 → 零级光不变
        # 当 pi_raw > bias 时, ao1 < bias → RF 降低 → 零级光增加 (峰低时需要)
        # 当 pi_raw < bias 时, ao1 > bias → RF 升高 → 零级光减少 (峰高时需要)
        ao1 = 2.0 * self.bias_80M - pi_raw              # 反比映射

        # ---- 4. 输出限幅 ----
        # 限幅在 [4.0, 6.0] V，与环路1 相同
        if ao1 > self.output_max:                       # 超过上限
            ao1 = self.output_max
        elif ao1 < self.output_min:                     # 低于下限
            ao1 = self.output_min

        # ---- 5. 保存并返回 ----
        self.last_output_80M = ao1                      # 更新上次输出缓存
        return ao1, error                               # 返回 (AO1电压, 误差)

    # ==================================================================
    # 批量更新 — 同时处理两个环路的便捷接口
    # ==================================================================

    def update_both(self,
                    setpoint_110M, measured_110M,
                    setpoint_80M, measured_80M,
                    dt, hold_110M=False, hold_80M=False):
        """
        【目的】
          同时更新两个环路。这是最常用的调用接口——
          一次调用完成双环路的全部 PI 计算。
          内部直接调用 update_110M 和 update_80M，没有额外逻辑。

        【返回值】
          ao0, ao1 : float — 两个环路的输出电压
          error_110M, error_80M : float — 两个环路的当前误差
        """
        ao0, err0 = self.update_110M(setpoint_110M, measured_110M, dt, hold_110M)
        ao1, err1 = self.update_80M(setpoint_80M, measured_80M, dt, hold_80M)
        return ao0, ao1, err0, err1

    # ==================================================================
    # 参数热更新 — 运行时调整 PI 参数而不重置积分项
    # ==================================================================

    def set_params_110M(self, kp=None, ki=None, bias=None):
        """
        【目的】
          运行时更新环路1 的 PI 参数。传入 None 的参数保持不变。
          重要: 不重置积分项！积分保持连续，避免更新参数导致的突变。

        【使用场景】
          用户在 LabVIEW 前面板调整 Kp/Ki 旋钮时，实时生效。
        """
        if kp is not None:                              # 如果传了 kp
            self.kp_110M = float(kp)                     #   更新比例增益
        if ki is not None:                              # 如果传了 ki
            self.ki_110M = float(ki)                     #   更新积分增益
        if bias is not None:                            # 如果传了 bias
            self.bias_110M = float(bias)                 #   更新偏置电压

    def set_params_80M(self, kp=None, ki=None, bias=None):
        """
        【目的】
          运行时更新环路2 的 PI 参数。与 set_params_110M 相同逻辑。
        """
        if kp is not None:
            self.kp_80M = float(kp)
        if ki is not None:
            self.ki_80M = float(ki)
        if bias is not None:
            self.bias_80M = float(bias)

    # ==================================================================
    # 重置 — 清零积分项
    # ==================================================================

    def reset_110M(self):
        """
        【目的】
          重置环路1 的积分累积项。在以下场景使用:
          - 系统重新锁定前
          - PI 明显积分饱和需要快速恢复
          - 校准后第一次锁定
        """
        self.i_sum_110M = 0.0                           # 清零积分
        self.last_output_110M = self.bias_110M          # 重置上次输出到偏置值

    def reset_80M(self):
        """
        【目的】
          重置环路2 的积分累积项。
        """
        self.i_sum_80M = 0.0                            # 清零积分
        self.last_output_80M = self.bias_80M            # 重置上次输出到偏置值

    def reset(self):
        """
        【目的】
          同时重置两个环路的积分项。
        """
        self.reset_110M()
        self.reset_80M()


# ############################################################################
# 第四部分：双环路独立脱锁检测 (LockDetector)
# ############################################################################
# 目的:
#   检测每个 PI 环路是否"脱锁"(loss of lock)。
#   当峰高度偏离 setpoint 超过阈值时，环路可能已脱锁——
#   可能是 AOM 故障、光路遮挡、温度骤变等。
#
#   检测策略:
#     1. 每帧判断 |setpoint - measured| > setpoint * threshold_ratio
#     2. 维护一个长度为 N 的布尔历史队列 (FIFO)
#     3. 只有连续 N 帧都脱锁，才判定为"真正脱锁"
#     4. 脱锁期间，输出切换到"安全值"（历史锁定期间的平均输出）
#
#   为什么需要连续 N 帧确认？
#     单帧误判 (如瞬时噪声尖峰) 不应触发脱锁恢复，
#     只有持续偏离才是真正的脱锁。
#     N=5 @ 20Hz = 250ms 确认窗口。
#
#   安全输出机制:
#     锁定期间持续记录 AO 输出值到 FIFO 队列 (长度 50)。
#     脱锁时返回队列的均值——这是在"正常工况"下的典型输出值，
#     比直接返回 bias (5V) 更接近系统实际需要的控制电压。
# ############################################################################

class LockDetector:
    """
    【目的】
      双环路独立脱锁检测器。每个环路有自己的脱锁历史和判定逻辑，
      一个环路脱锁不会影响另一个环路的状态。

    【脱锁条件】
      连续 history_length 帧满足:
        |setpoint - measured| > |setpoint| * threshold_ratio
      即: 误差超过 setpoint 的 threshold_ratio (例如 30%) 持续 5 帧。

    【跨帧状态】
      lock_history_110M : list[bool] (长度 history_length)
          环路1 的脱锁历史。True = 该帧脱锁。
          新数据从右侧推入，旧数据从左侧弹出 (FIFO)。
      lock_history_80M : list[bool]
          环路2 的脱锁历史。
      safety_outputs_110M : list[float] (长度 safety_queue_length)
          环路1 锁定期间的输出记录队列，脱锁时取均值作为安全输出。
      safety_outputs_80M : list[float]
          环路2 的安全输出队列。
    """

    def __init__(self, history_length=5, threshold_ratio=0.3,
                 safety_queue_length=50, bias_110M=5.0, bias_80M=5.0):
        """
        【参数】
          history_length : int (默认 5)
              连续脱锁帧数阈值。5 帧 @ 20Hz = 250ms。
          threshold_ratio : float (默认 0.3)
              脱锁阈值比例。0.3 = 30%，即误差超过 setpoint 的 30% 判定为脱锁。
          safety_queue_length : int (默认 50)
              安全输出队列长度。50 帧 @ 20Hz = 2.5 秒的历史。
          bias_110M, bias_80M : float (默认 5.0)
              偏置电压。用作初始安全值。
        """
        # ---- 存储配置 ----
        self.history_length = int(history_length)      # 连续判定所需帧数
        self.threshold_ratio = float(threshold_ratio)   # 脱锁阈值比例 (30%)
        self.safety_queue_length = int(safety_queue_length) # 安全队列长度
        self.bias_110M = float(bias_110M)               # 环路1 偏置
        self.bias_80M = float(bias_80M)                 # 环路2 偏置

        # ---- 初始化脱锁历史队列 ----
        # 全部初始化为 False = "正常"，即初始状态认为系统是锁定中的
        self.lock_history_110M = [False] * self.history_length
        self.lock_history_80M = [False] * self.history_length

        # ---- 初始化安全输出队列 ----
        # 预填充偏置值: 脱锁检测器刚启动时没有历史数据，
        # 用 bias 作为初始安全值比用 0 更合理
        self.safety_outputs_110M = [bias_110M] * safety_queue_length
        self.safety_outputs_80M = [bias_80M] * safety_queue_length

    def _check_single(self, setpoint, measured, history):
        """
        【目的】
          单环路的脱锁判断逻辑（内部辅助方法）。
          比较当前误差与阈值。

        【参数】
          setpoint : float — 目标值
          measured : float — 实测值
          history : list[bool] — 该环路的历史队列 (未使用，保留接口统一)

        【返回值】
          lost_now : bool — 本帧是否脱锁 (误差超过阈值)
        """
        # 计算脱锁阈值 = setpoint 绝对值 * ratio
        # 用 abs(setpoint) 处理 setpoint 可能为负的边界情况
        threshold = abs(setpoint) * self.threshold_ratio # 脱锁阈值 (V)

        # 计算绝对误差
        error = abs(setpoint - measured)                 # 当前绝对误差 (V)

        # 判定: 误差是否超过阈值
        lost_now = error > threshold                     # True = 本帧脱锁
        return lost_now

    def check_110M(self, setpoint, measured):
        """
        【目的】
          环路1 脱锁检测。每帧调用一次。

        【逻辑】
          1. 判断本帧是否满足脱锁条件
          2. 更新历史队列 (FIFO: 弹出最老帧, 推入当前帧)
          3. 如果历史队列中所有帧都脱锁 → 判定为脱锁

        【返回值】
          is_locked : bool
              True = 环路1 锁定中 (正常)
              False = 环路1 脱锁
        """
        # Step 1: 本帧脱锁判断
        lost_now = self._check_single(setpoint, measured, self.lock_history_110M)

        # Step 2: 维护 FIFO 历史队列
        self.lock_history_110M.pop(0)                   # 弹出最老的一帧
        self.lock_history_110M.append(lost_now)          # 推入当前帧

        # Step 3: 连续脱锁判定
        # all(history) == True → 所有历史帧都脱锁 → 判定为真正脱锁
        # is_locked = NOT (连续脱锁)
        is_locked = not all(self.lock_history_110M)     # True=锁定, False=脱锁
        return is_locked

    def check_80M(self, setpoint, measured):
        """
        【目的】
          环路2 脱锁检测。逻辑与 check_110M 完全独立。

        【返回值】
          is_locked : bool — 环路2 锁定状态
        """
        # 与 check_110M 完全对称的逻辑
        lost_now = self._check_single(setpoint, measured, self.lock_history_80M)
        self.lock_history_80M.pop(0)
        self.lock_history_80M.append(lost_now)
        is_locked = not all(self.lock_history_80M)
        return is_locked

    def check_both(self,
                   setpoint_110M, measured_110M,
                   setpoint_80M, measured_80M):
        """
        【目的】
          同时检查两个环路的脱锁状态。便捷接口。

        【返回值】
          lock_110M : bool
          lock_80M : bool
        """
        return (self.check_110M(setpoint_110M, measured_110M),
                self.check_80M(setpoint_80M, measured_80M))

    def update_safety_110M(self, output_value):
        """
        【目的】
          环路1 锁定正常时，更新安全输出队列。
          维护一个 FIFO 队列，记录最近的正常输出值。

        【调用时机】
          当 check_110M 返回 True（锁定中）时调用。
        """
        self.safety_outputs_110M.append(float(output_value)) # 推入新值
        if len(self.safety_outputs_110M) > self.safety_queue_length:
            self.safety_outputs_110M.pop(0)                   # 弹出最老值

    def update_safety_80M(self, output_value):
        """
        【目的】
          环路2 的安全输出队列更新。
        """
        self.safety_outputs_80M.append(float(output_value))
        if len(self.safety_outputs_80M) > self.safety_queue_length:
            self.safety_outputs_80M.pop(0)

    def get_safety_110M(self):
        """
        【目的】
          获取环路1 的安全输出值——取队列中所有历史值的平均。

        【原理】
          脱锁时，PI 输出的电压可能已经远离正常值，
          直接用会驱使 VVA 走到错误的极端。
          安全值 = 最近正常工况下的平均输出电压，
          这比简单地返回 bias (5V) 更合理。

        【返回值】
          float — 安全输出电压 (V)
        """
        if self.safety_outputs_110M:                    # 队列非空
            return float(np.mean(self.safety_outputs_110M)) # 返回队列均值
        return self.bias_110M                           # 队列为空 → 返回偏置备用

    def get_safety_80M(self):
        """
        【目的】
          获取环路2 的安全输出值。
        """
        if self.safety_outputs_80M:
            return float(np.mean(self.safety_outputs_80M))
        return self.bias_80M

    def reset(self):
        """
        【目的】
          重置两个环路的脱锁检测状态到初始值。
        """
        # 重置脱锁历史: 全部恢复正常
        self.lock_history_110M = [False] * self.history_length
        self.lock_history_80M = [False] * self.history_length
        # 重置安全队列: 重新填充偏置值
        self.safety_outputs_110M = [self.bias_110M] * self.safety_queue_length
        self.safety_outputs_80M = [self.bias_80M] * self.safety_queue_length


# ############################################################################
# 第五部分：全局校准
# ############################################################################
# 目的:
#   系统第一次启动（或重新校准）时，需要先确定 4 个目标峰在
#   PZT 扫描周期中的位置。校准函数在一个完整的扫描周期数据上运行，
#   找出所有候选峰，按高度排序选最高的 N 个作为目标峰。
#
#   为什么用"最高N个"而不是固定位置？
#   因为不同光路配置下，目标峰的 PZT 电压位置可能不同。
#   但目标峰总是最高的（比虚假峰高），所以按高度排序是合理的初选策略。
#   初选后，PeakTracker 的位置窗口 EMA 跟踪会处理后续的漂移。
# ############################################################################

def calibrate_global(ai0_data, ai1_data, smooth_window=7, n_peaks=4):
    """
    【目的】
      全局校准：在一个完整的 PZT 扫描周期中找出目标峰的位置。
      这是系统初始化流程的第一步——必须先校准才能开始锁定。

    【处理流程】
      1. 对 AI0 数据做移动平均平滑
      2. 在平滑信号上检测所有局部极大值
      3. 按高度降序排序，取最高的 n_peaks 个
      4. 再按索引升序排序（恢复从左到右顺序）
      5. 返回这些峰的采样点索引和对应的 PZT 电压

    【参数】
      ai0_data : np.ndarray (1D, float)
          FP 腔透射信号。一个完整的 PZT 扫描周期。
          来源: LabVIEW DAQmx Read AI0 通道。
          长度: 取决于采样率和扫描频率 (典型 5000 点 @ 100kS/s, 20Hz)。
      ai1_data : np.ndarray (1D, float)
          PZT 电压监视信号。同周期的 PZT 驱动电压。
          来源: LabVIEW DAQmx Read AI1 通道。
          用于将采样点索引映射到 PZT 电压（便于前面板显示）。
      smooth_window : int (默认 7)
          平滑窗口宽度。
      n_peaks : int (默认 4)
          要选出的目标峰数量。

    【返回值】
      cal_indices : list[int]
          选出的 n_peaks 个峰的采样点索引（从小到大排序）。
          这些值将传给 PeakTracker.calibrate() 作为初始位置。
      cal_pzt_voltages : list[float]
          对应峰的 PZT 电压值（从 ai1_data 中读取）。
      all_indices : list[int]
          所有检测到的峰的索引（调试/诊断用，可以在前面板显示总峰数）。
      all_heights : list[float]
          所有检测到的峰的高度。
    """
    # ---- 1. 平滑信号 ----
    # 先平滑再寻峰，减少噪声引起的假峰
    signal_smooth = smooth_moving_average(ai0_data, int(smooth_window))

    # ---- 2. 全局寻峰 ----
    # 在整条信号上找所有局部极大值
    # min_height_ratio=0.02 = 最高峰的 2%，过滤微小的噪声波动
    all_idx, all_h = find_all_local_maxima(signal_smooth, min_height_ratio=0.02)

    # ---- 3. 候选峰不够时的降级处理 ----
    if len(all_idx) < n_peaks:
        # 峰不够 n_peaks 个 → 返回所有找到的
        # 这是一个异常情况，调用者应检查 success 标志
        cal_idx = list(all_idx)                        # 所有找到的峰都是目标
        # 取对应的 PZT 电压值
        cal_pzt = [float(ai1_data[i]) for i in all_idx] if len(ai1_data) > 0 else cal_idx
        return cal_idx, cal_pzt, list(all_idx), list(all_h)

    # ---- 4. 按高度降序排序，选最高的 n_peaks 个 ----
    # pair 格式: (索引, 高度)
    sorted_pairs = sorted(zip(all_idx, all_h), key=lambda x: x[1], reverse=True)
    top_n = sorted_pairs[:n_peaks]                     # 前 n_peaks 个最高峰

    # ---- 5. 按索引升序排序 ----
    # 恢复从左到右的 PZT 扫描顺序: 峰1→峰2→峰3→峰4
    top_n_sorted = sorted(top_n, key=lambda x: x[0])   # 按索引从小到大排列

    # ---- 6. 提取结果 ----
    cal_idx = [p[0] for p in top_n_sorted]              # 校准索引列表
    # 从 AI1 数据中取对应 PZT 电压
    cal_pzt = [float(ai1_data[i]) for i in cal_idx] if len(ai1_data) > 0 else [float(i) for i in cal_idx]

    return cal_idx, cal_pzt, list(all_idx), list(all_h)


# ############################################################################
# 第 5B 部分：自动双峰寻峰 (auto_find_two_peaks)
# ############################################################################
# 目的:
#   利用 FP 腔透射信号的物理特征自动定位两个目标峰，不依赖固定的
#   采样点位置。这解决了温度漂移导致峰位置移动后无法锁定新位置的问题。
#
#   算法原理:
#     FP 腔透射信号上，全局最小值对应两路光之间的暗区（干涉相消）。
#     110M AOM 峰位于全局最小值一侧约 100 个采样点的位置，
#     EOM 一级边带位于全局最小值另一侧紧邻的峰。
#
#     利用这个固定的空间关系，即使温度漂移导致峰整体移动，
#     只要这三个特征（AOM峰、全局最小、EOM一级边带）的相对位置
#     保持不变，算法就能自动定位。
#
#   处理流程:
#     1. 平滑信号，计算全局最大值和全局最小值位置
#     2. 找到所有有效峰 (高度 > 全局最大峰高度 / 5)
#     3. 在全局最小值左右两侧分别寻找：
#        - AOM 峰: 距离全局最小值约 ~100 采样点的方向
#        - EOM 一级边带: 另一侧紧邻全局最小值的峰
#     4. 按从左到右排序返回 [AOM_index, EOM_index]
# ############################################################################

def auto_find_two_peaks(signal, ai1_signal=None, smooth_window=15,
                         min_height_ratio=0.2,
                         pair_dist_min=70, pair_dist_max=220):
    """
    基于物理对称性自动定位 AOM 峰 (P1) 和 EOM 一级边带 (P2)。

    物理结构 (PZT 扫描 FP 腔拍频信号):
      - EOM 零级与 AOM 频率差小 → 距离 70~220 采样点
      - 峰群在 PZT 扫描中左右对称分布，从中心向外:
          二级 → 一级 → AOM → 零级(最外侧)
      - 零级在最外侧，AOM 紧挨零级内侧，一级紧挨 AOM 内侧
      - ★不依赖峰高度★，只用空间位置判定身份

    判定方法 (对称性):
      离 PZT 中心近 = AOM，离中心远 = EOM 零级
      EOM 一级 = AOM 内侧（靠近中心方向）的下一个峰

    算法:
      1. 找所有有效峰 (高度 > 全局最高峰 / 5)
      2. 找距离在 [70, 220] 内的峰对 → (零级, AOM) 候选对
      3. 每对中离中心远的 = 零级, 离中心近的 = AOM
      4. 选 AOM 离中心最近的那对
      5. EOM 一级 = AOM 内侧（向中心方向）的下一个峰

    参数:
      signal: FP 腔透射信号 (AI0)
      ai1_signal: PZT 扫描同步信号 (AI1), 用于确定中心; None 则用屏幕中心
      smooth_window: 平滑窗口
      min_height_ratio: 有效峰最小高度比
      pair_dist_min, pair_dist_max: 零级-AOM 距离范围

    返回:
      (indices, found): indices = [AOM_idx, EOM1st_idx] (从左到右)
    """
    n = len(signal)
    if n < 10:
        return [], False

    signal_smooth = smooth_moving_average(signal, int(smooth_window))
    global_max = float(np.max(signal_smooth))

    # ---- 0. 确定 PZT 对称轴位置及高低电平 ----
    # AI1 是 PZT 扫描同步信号。峰可以出现在高电平或低电平段。
    # 高电平: 中心→外 = 二级→一级→AOM→零级 (零级在最外侧)
    # 低电平: 中心→外 = 零级→AOM→一级→二级 (零级在最内侧)
    # 由于温度漂移，某一段可能峰不完整，两边都试，选峰多的一侧。
    if ai1_signal is not None and len(ai1_signal) >= n:
        ai1_arr = np.asarray(ai1_signal[:n], dtype=float)
        ai1_min = float(np.min(ai1_arr))
        ai1_max = float(np.max(ai1_arr))
        ai1_mid = (ai1_min + ai1_max) / 2.0

        # 找出所有连续的高低电平段
        above = ai1_arr > ai1_mid
        segments = []  # [(start, end, is_high), ...]
        i = 0
        while i < n:
            val = above[i]
            start = i
            while i < n and above[i] == val:
                i += 1
            segments.append((start, i - 1, bool(val)))

        # 分别取最长的高电平段和低电平段
        high_segs = [(s, e) for s, e, h in segments if h]
        low_segs = [(s, e) for s, e, h in segments if not h]

        # 计算两个候选中心
        candidates_center = []
        if high_segs:
            longest = max(high_segs, key=lambda seg: seg[1] - seg[0])
            candidates_center.append(((longest[0] + longest[1]) // 2, True))
        if low_segs:
            longest = max(low_segs, key=lambda seg: seg[1] - seg[0])
            candidates_center.append(((longest[0] + longest[1]) // 2, False))

        if not candidates_center:
            pzt_center = n // 2
            is_high_level = True
        else:
            # 先用第一个候选，后续会验证
            pzt_center, is_high_level = candidates_center[0]

        print(f"[AUTO-FIND] AI1 range=[{ai1_min:.3f},{ai1_max:.3f}] mid={ai1_mid:.3f}, "
              f"high_segs={len(high_segs)} low_segs={len(low_segs)}, "
              f"PZT_center={pzt_center} is_high={is_high_level}",
              flush=True)
    else:
        pzt_center = n // 2
        is_high_level = True
        print(f"[AUTO-FIND] 无 AI1 数据，使用屏幕中心={pzt_center}", flush=True)

    # ---- 1. 找所有有效峰 ----
    all_idx, all_h = find_all_local_maxima(signal_smooth,
                                           min_height_ratio=min_height_ratio)
    if len(all_idx) < 3:
        return [], False

    # ---- 2. 找 (零级, AOM) 候选对 ----
    # 距离在 [pair_dist_min, pair_dist_max] 内即候选
    # 对称性判定取决于高低电平：
    #   高电平: 离中心近的 = AOM, 远的 = 零级 (零级在最外侧)
    #   低电平: 离中心近的 = 零级, 远的 = AOM (零级在最内侧)
    candidates = []
    for i in range(len(all_idx)):
        for j in range(i + 1, len(all_idx)):
            dist = abs(all_idx[i] - all_idx[j])
            if pair_dist_min <= dist <= pair_dist_max:
                dist_i = abs(all_idx[i] - pzt_center)
                dist_j = abs(all_idx[j] - pzt_center)
                if is_high_level:
                    # 高电平: 近中心 = AOM, 远中心 = 零级
                    if dist_i < dist_j:
                        aom_idx, zero_idx = all_idx[i], all_idx[j]
                    else:
                        aom_idx, zero_idx = all_idx[j], all_idx[i]
                else:
                    # 低电平: 近中心 = 零级, 远中心 = AOM (翻转!)
                    if dist_i < dist_j:
                        zero_idx, aom_idx = all_idx[i], all_idx[j]
                    else:
                        zero_idx, aom_idx = all_idx[j], all_idx[i]
                candidates.append({
                    'zero_idx': zero_idx,
                    'aom_idx': aom_idx,
                    'aom_h': all_h[i] if all_idx[i] == aom_idx else all_h[j],
                    'zero_h': all_h[j] if all_idx[i] == aom_idx else all_h[i],
                })

    if not candidates:
        print("[AUTO-FIND] 未找到距离在 {:d}-{:d} 内的峰对".format(
            pair_dist_min, pair_dist_max), flush=True)
        return [], False

    # ---- 2b. 结构验证：AOM 内侧至少还有一个峰 ----
    # (证明这是一个完整峰群而非随机噪声对)
    validated = []
    for c in candidates:
        aom = c['aom_idx']
        zero = c['zero_idx']
        dir_to_center = 1 if pzt_center > aom else -1
        has_inner = False
        for idx in all_idx:
            if idx == aom or idx == zero:
                continue
            if dir_to_center > 0 and idx > aom:
                has_inner = True
                break
            elif dir_to_center < 0 and idx < aom:
                has_inner = True
                break
        if has_inner:
            validated.append(c)

    if not validated:
        validated = candidates

    # ---- 2c. 详细调试：列出所有有效峰 ----
    print(f"[AUTO-FIND] === 所有有效峰 ({len(all_idx)}个) ===", flush=True)
    for idx, h in zip(all_idx, all_h):
        side = "L" if idx < pzt_center else "R"
        print(f"  idx={idx:5d}  h={h:.6f}  dist_to_ctr={abs(idx-pzt_center):5d}  [{side}]",
              flush=True)
    print(f"[AUTO-FIND] === 候选对 ({len(candidates)}个, 结构验证{len(validated)}个) ===", flush=True)
    for i, c in enumerate(candidates[:10]):
        vmark = " ✓" if c in validated else " ✗(内侧无峰)"
        print(f"  [{i}] 零级@{c['zero_idx']}(h={c['zero_h']:.6f}) "
              f"<-> AOM@{c['aom_idx']}(h={c['aom_h']:.6f}) "
              f"gap={abs(c['aom_idx']-c['zero_idx'])} "
              f"AOM_dist_to_ctr={abs(c['aom_idx']-pzt_center)}{vmark}",
              flush=True)

    # ---- 3. 对每个候选对计算 EOM 一级，验证 AOM-一级间距 ----
    # 伪对特征: EOM一级-二级间距 ≈ 零级-AOM间距 (都在 70-220 内)
    # 真对特征: AOM-一级间距 >> 零级-AOM间距 (6.72GHz vs 110MHz)
    # 阈值: AOM-一级 必须 > 2.5 倍零级-AOM间距
    MIN_AOM_1ST_RATIO = 2.5

    scored_candidates = []
    for c in validated:
        aom = c['aom_idx']
        zero = c['zero_idx']
        dir_to_center = 1 if pzt_center > aom else -1

        # 找 EOM 一级边带：
        #   高电平: 一级在 AOM 内侧（向中心），零级在外侧
        #   低电平: 一级在 AOM 外侧（远离中心），零级在内侧 (翻转!)
        if is_high_level:
            search_dir = dir_to_center   # 向中心搜 = 一级方向
        else:
            search_dir = -dir_to_center  # 远离中心搜 = 一级方向

        eom1st_candidate = None
        for idx, h in zip(all_idx, all_h):
            if idx == aom or idx == zero:
                continue
            if search_dir > 0 and idx > aom:
                if eom1st_candidate is None or idx < eom1st_candidate[0]:
                    eom1st_candidate = (idx, h)
            elif search_dir < 0 and idx < aom:
                if eom1st_candidate is None or idx > eom1st_candidate[0]:
                    eom1st_candidate = (idx, h)

        if eom1st_candidate is None:
            continue

        gap_0th_aom = abs(aom - zero)
        gap_aom_1st = abs(eom1st_candidate[0] - aom)

        # 验证：AOM-一级间距必须显著大于零级-AOM间距
        is_valid = gap_aom_1st > MIN_AOM_1ST_RATIO * gap_0th_aom

        scored_candidates.append({
            **c,
            'eom1st_idx': eom1st_candidate[0],
            'eom1st_h': eom1st_candidate[1],
            'gap_0th_aom': gap_0th_aom,
            'gap_aom_1st': gap_aom_1st,
            'is_valid': is_valid,
        })

    # ---- 3b. 调试：列出每个候选对的评分 ----
    print(f"[AUTO-FIND] === 候选对评分 (阈值: gap1/gap0 > {MIN_AOM_1ST_RATIO}) ===", flush=True)
    for i, sc in enumerate(scored_candidates):
        status = "✓真" if sc['is_valid'] else "✗伪"
        print(f"  [{i}] 0th@{sc['zero_idx']} → AOM@{sc['aom_idx']} → 1st@{sc['eom1st_idx']} | "
              f"gap0={sc['gap_0th_aom']} gap1={sc['gap_aom_1st']} "
              f"ratio={sc['gap_aom_1st']/max(sc['gap_0th_aom'],1):.1f} {status}",
              flush=True)

    # 优先选验证通过的，其次选 AOM 离中心近的
    valid_scored = [s for s in scored_candidates if s['is_valid']]
    if not valid_scored:
        print("[AUTO-FIND] 无候选通过 AOM-一级间距验证，降级使用全部候选", flush=True)
        valid_scored = scored_candidates

    if not valid_scored:
        print("[AUTO-FIND] 无有效候选", flush=True)
        return [], False

    # 从验证通过的候选中，选 AOM 离 PZT 中心最近的
    best = min(valid_scored, key=lambda c: abs(c['aom_idx'] - pzt_center))
    aom_idx = best['aom_idx']
    zero_idx = best['zero_idx']
    eom1st_idx = best['eom1st_idx']
    eom1st_h = best['eom1st_h']

    # ---- 4. 按从左到右排序 ----
    indices = sorted([aom_idx, eom1st_idx])

    # ---- 调试输出 ----
    aom_h = float(signal_smooth[aom_idx])
    zero_h_val = float(signal_smooth[zero_idx])
    gap0 = abs(aom_idx - zero_idx)
    gap1 = abs(eom1st_idx - aom_idx)
    ratio = gap1 / max(gap0, 1)
    print(f"[AUTO-FIND] === 最终选择 (gap ratio={ratio:.1f}, 阈值={MIN_AOM_1ST_RATIO}) ===", flush=True)
    print(f"[AUTO-FIND] PZT_center={pzt_center}", flush=True)
    print(f"[AUTO-FIND] EOM零级: idx={zero_idx}, h={zero_h_val:.6f}, "
          f"dist_to_ctr={abs(zero_idx-pzt_center)}",
          flush=True)
    print(f"[AUTO-FIND] AOM(P1): idx={aom_idx}, h={aom_h:.6f}, "
          f"dist_to_ctr={abs(aom_idx-pzt_center)}, gap0={gap0}",
          flush=True)
    print(f"[AUTO-FIND] EOM一级(P2): idx={eom1st_idx}, h={eom1st_h:.6f}, "
          f"dist_to_ctr={abs(eom1st_idx-pzt_center)}, gap1={gap1}",
          flush=True)
    print(f"[AUTO-FIND] → P1=AOM@{aom_idx}, P2=EOM1st@{eom1st_idx}", flush=True)

    return indices, True


# ############################################################################
# 第六部分：单帧完整处理 (track_and_control)
# ############################################################################
# 目的:
#   这是整个算法模块的"主函数"——将上述所有模块串联起来，
#   完成一帧 PZT 扫描数据的完整处理流水线。
#
#   处理流水线 (6 步):
#     1. 移动平均平滑 → 去除探测器噪声
#     2. 位置窗口寻峰 → 在 EMA 跟踪位置附近找 4 个目标峰
#     3. 参数热更新 → 将传入的 PI 参数写入控制器 (支持运行时调参)
#     4. 双 PI 计算 → 110M 正比 + 80M 反比
#     5. 脱锁检测 → 每环路独立判定
#     6. 安全输出 → 脱锁时切换到安全值
#
#   每一帧在 LabVIEW While Loop 中调用一次。
#   有状态对象 (tracker, pi, lock_det) 作为参数传入，
#   它们的状态跨帧保持。
# ############################################################################

def track_and_control(ai0_data, ai1_data,
                      setpoint_110M, kp_110M, ki_110M, bias_110M,
                      setpoint_80M, kp_80M, ki_80M, bias_80M,
                      output_min, output_max, i_min, i_max,
                      window_half, ema_alpha, smooth_window,
                      dt, hold_110M, hold_80M,
                      tracker, pi, lock_det,
                      bypass_safety=False):
    """
    【目的】
      单帧完整处理——将"平滑→寻峰→PI→脱锁检测→安全输出"串联。
      这是 LabVIEW While Loop 中每帧调用的核心函数。

    【为什么设计成函数而不是类的方法】
      将三个有状态对象 (tracker, pi, lock_det) 作为参数传入，
      而不是把所有状态合并到一个大对象中，原因:
        1. 职责分离: 每个对象有独立的关注点
        2. 可测试性: 可以独立测试每个模块
        3. 灵活性: 将来可能只需要替换其中一个模块

    【参数分组说明】

      —— 输入信号 ——
      ai0_data : np.ndarray (1D) — FP 腔透射信号 (一完整扫描周期)
      ai1_data : np.ndarray (1D) — PZT 电压监视信号 (同周期)

      —— 环路1 控制参数 (110M, 正比) ——
      setpoint_110M : float — 峰1 目标高度 (V)
      kp_110M : float — 环路1 比例增益
      ki_110M : float — 环路1 积分增益 (1/s)
      bias_110M : float — 环路1 偏置电压 (V)

      —— 环路2 控制参数 (80M, 反比) ——
      setpoint_80M : float — 峰2 目标高度 (V)
      kp_80M : float — 环路2 比例增益
      ki_80M : float — 环路2 积分增益 (1/s)
      bias_80M : float — 环路2 偏置电压 (V)

      —— 通用限幅参数 ——
      output_min, output_max : float — AO 输出限幅 (典型 4.0, 6.0)
      i_min, i_max : float — PI 积分项限幅 (典型 -1.0, 1.0)

      —— 算法参数 ——
      window_half : int — 寻峰窗口半宽 (采样点, 典型 250)
      ema_alpha : float — EMA 位置跟踪系数 (典型 0.1)
      smooth_window : int — 平滑窗口宽度 (典型 7)
      dt : float — 扫描周期 (s) = 1/scan_freq (典型 0.05)

      —— Hold 信号 ——
      hold_110M : bool — 环路1 是否冻结积分
      hold_80M : bool — 环路2 是否冻结积分

      —— 有状态对象 (在帧之间保持状态) ——
      tracker : PeakTracker — 已校准的峰跟踪器
      pi : DualPIController — 双环路 PI 控制器
      lock_det : LockDetector — 脱锁检测器

    【返回值 (dict)】
      ao0 : float — AO0 输出电压 (V) → LabVIEW DAQmx Write
      ao1 : float — AO1 输出电压 (V) → LabVIEW DAQmx Write
      heights : list[float] — 4 峰高度 (V) → 前面板图表
      positions : list[float] — 4 峰亚采样位置 → 诊断显示
      found_count : int — 找到的峰数 → 前面板指示器
      lock_110M : bool — 110M 锁定状态 → Lock LED
      lock_80M : bool — 80M 锁定状态 → Lock LED
      error_110M : float — 110M 误差 → 前面板
      error_80M : float — 80M 误差 → 前面板
    """
    # ================================================================
    # Step 1: 移动平均平滑
    # ================================================================
    # 平滑窗口典型值 7 → 在 100kS/s 下对应 70us，平滑高频探测器噪声
    signal_smooth = smooth_moving_average(ai0_data, int(smooth_window))

    # ================================================================
    # Step 2: 位置窗口寻峰
    # ================================================================
    # tracker.track() 在 EMA 跟踪位置附近搜索 4 个目标峰
    # 返回: 峰高度(抛物线拟合), 亚采样位置, 成功找到的峰数
    heights, positions, found_count = tracker.track(signal_smooth)

    # ================================================================
    # Step 3: 更新 PI 参数
    # ================================================================
    # 运行时热调参: 将从 LabVIEW 前面板传入的最新参数写入控制器
    # 这允许用户在不停止系统的情况下调整 Kp/Ki
    pi.set_params_110M(kp=kp_110M, ki=ki_110M, bias=bias_110M)
    pi.set_params_80M(kp=kp_80M, ki=ki_80M, bias=bias_80M)
    pi.output_min = output_min                         # 更新输出下限
    pi.output_max = output_max                         # 更新输出上限
    pi.i_min = i_min                                   # 更新积分下限
    pi.i_max = i_max                                   # 更新积分上限

    # ================================================================
    # Step 4: 双环路 PI 计算
    # ================================================================

    # --- 4a. 获取环路1 的实测值 (峰1 = 110M 一级光) ---
    if len(heights) > 0:                               # heights 非空
        measured_110M = heights[0]                      #   取峰1 高度 = heights[0]
    else:                                              # 极少情况: heights 为空
        measured_110M = tracker.last_heights[0]         #   使用上一帧缓存值

    # --- 4b. 获取环路2 的实测值 (峰2 = 80M 零级载波) ---
    if len(heights) > 1:                               # heights 至少有 2 个元素
        measured_80M = heights[1]                       #   取峰2 高度 = heights[1]
    else:                                              # 极少情况
        measured_80M = tracker.last_heights[1]          #   使用上一帧缓存值

    # --- 4c. 环路1 PI 更新 (正比控制) ---
    # error = setpoint_110M - measured_110M
    # error > 0 → 峰太低 → AO0 增大 → VVA 开大 → 峰升高
    ao0_raw, err_110M = pi.update_110M(
        setpoint_110M, measured_110M, dt, hold_110M)

    # --- 4d. 环路2 PI 更新 (反比控制) ---
    # error = setpoint_80M - measured_80M
    # error > 0 → 峰太低 → PI 内部正比输出 > bias → 反比后 AO1 < bias
    # → RF 减小 → 零级光增加 → 峰升高
    ao1_raw, err_80M = pi.update_80M(
        setpoint_80M, measured_80M, dt, hold_80M)

    # ================================================================
    # Step 5: 双环路独立脱锁检测
    # ================================================================
    # 每环路独立判定：检查是否连续 N 帧误差超过阈值
    lock_110M, lock_80M = lock_det.check_both(
        setpoint_110M, measured_110M,                     # 环路1 的 setpoint 和实测
        setpoint_80M, measured_80M)                       # 环路2 的 setpoint 和实测

    # ================================================================
    # Step 6: 安全输出处理
    # ================================================================
    # 锁定中: 使用 PI 输出，并同时更新安全输出队列
    # 脱锁中: 使用安全队列的均值 (最近正常工况的平均输出)

    # --- 6a. 环路1 安全处理 ---
    if bypass_safety:
        ao0 = ao0_raw                                   # 诊断模式: 跳过脱锁检测
    elif lock_110M:                                     # 环路1 锁定正常
        lock_det.update_safety_110M(ao0_raw)            #   记录正常输出到安全队列
        ao0 = ao0_raw                                   #   使用 PI 实时输出
    else:                                              # 环路1 脱锁
        ao0 = lock_det.get_safety_110M()                #   使用安全输出 (历史均值)

    # --- 6b. 环路2 安全处理 ---
    if bypass_safety:
        ao1 = ao1_raw                                   # 诊断模式: 跳过脱锁检测
    elif lock_80M:                                     # 环路2 锁定正常
        lock_det.update_safety_80M(ao1_raw)             #   记录正常输出到安全队列
        ao1 = ao1_raw                                   #   使用 PI 实时输出
    else:                                              # 环路2 脱锁
        ao1 = lock_det.get_safety_80M()                 #   使用安全输出 (历史均值)

    # ================================================================
    # Step 7: 周期性垃圾回收 (防止 LabVIEW 嵌入式 Python 内存累积)
    # ================================================================
    # LabVIEW 的 Python Session 不会主动触发 GC，导致每帧创建的
    # numpy 临时数组 (signal_smooth, convolve result, lstsq 工作数组等)
    # 在 Python 堆中累积。这会逐渐拖慢后续帧的内存分配速度。
    # 每 _GC_INTERVAL 帧强制触发一次 GC，将循环时间稳定性从 "持续增长"
    # 恢复到 "恒定"。
    global _frame_count
    _frame_count += 1
    if _frame_count >= _GC_INTERVAL:
        gc.collect()                                   # 强制回收所有不可达的 numpy 数组
        _frame_count = 0                               # 重置计数器

    # ================================================================
    # 返回所有结果
    # ================================================================
    return {
        'ao0': float(ao0),                             # AO0 电压 (4-6V)
        'ao1': float(ao1),                             # AO1 电压 (4-6V，反比处理后)
        'heights': [float(h) for h in heights],         # 4 峰高度列表
        'positions': [float(p) for p in positions],     # 4 峰位置列表
        'found_count': int(found_count),               # 成功找到的峰数 (0-4)
        'lock_110M': bool(lock_110M),                  # 环路1 锁定状态
        'lock_80M': bool(lock_80M),                    # 环路2 锁定状态
        'error_110M': float(err_110M),                 # 环路1 当前误差
        'error_80M': float(err_80M),                   # 环路2 当前误差
    }
