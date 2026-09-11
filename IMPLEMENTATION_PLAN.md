# 近期功能实施计划

本计划针对三个近期目标，不推进完整平台化重构：

1. 只替换已经验证的连续AI读取方式，尽量保持现有双峰、慢漂和功率锁定接口。
2. 基于EOM/FP物理关系增加载频与AOM一级边带识别。自动寻峰先保留占位符，本文只定义实现和验证路线。
3. 增加PFI1触发窗口，并用指定通道的窗口电压做独立慢漂反馈；含PFI1触发的双峰业务帧跳过识别和锁定。

## 一、已知物理约束

### 扫描拐点

当前扫描坐标默认有两个固定拐点：2500和7500。它们应当是配置，而不是写死在算法中：

```python
@dataclass
class ScanGeometry:
    breakpoint_1: int = 2500
    breakpoint_2: int = 7500
```

校验：

```text
0 < breakpoint_1 < breakpoint_2 < frame_length
```

默认分段为`[0,2500)`、`[2500,7500)`、`[7500,end)`。正程/回程方向也作为配置或标定结果保存。后续如果扫描点数或拐点改变，在线识别使用新配置并记录revision。

### FSR和EOM频率

FSR是固定实验参数，但允许修改：

```python
@dataclass
class CavityCalibration:
    fsr_mhz: float = 2500.0
    eom_frequency_mhz: float = 6800.0
    revision: int = 1
```

当前的`fit_sawtooth_model.py`使用`FSR=2500 MHz`、`RF=6800 MHz`以及人工峰锚点。它适合作为离线标定和候选验证工具，不能直接作为在线识别器，因为它依赖固定`KNOWN`、`ANCHOR`和一次扫描的人工位置。RF=6800 MHz是默认值，但不是固定值，这个值也可能在实验中发生变动

### 两点频率标定

界面允许用户在同一扫描分段内选择两个点，输入频率差：

```text
point_1 = x1
point_2 = x2
delta_frequency_mhz = df
```

得到局部线性关系：

```text
slope_mhz_per_sample = df / (x2 - x1)
```

标定结果必须保存点位、频率差、所属分段、方向和revision。两点跨越2500/7500拐点时默认拒绝；不同段分别标定。该结果只能解释局部关系，不能直接用一条直线解释整个锯齿扫描。

## 二、目标A：只替换AI读取

### 目标

现有上层继续收到原来的unified frame。只把内部读取替换为已实测的：

```text
连续AI Task只创建/启动一次
Every-N callback
AnalogMultiChannelReader
预分配NumPy数组
```

底层读取块可为5 ms；如果旧上层仍要求100 ms帧，adapter拼接20个5 ms块再发布旧frame。

### 实施边界

允许修改：

```text
usb6363/nidaqmx_driver.py  增加经过验证的reader/callback封装
usb6363_core.py             只替换unified worker的读取段
```

第一阶段不改：

```text
two_peak/signal.py
two_peak/power_lock.py
two_peak/trend_logger.py
AO/PFI旧接口
WebUI字段和旧frame结构
```

回调禁止执行算法、JSON、写盘、HTTP、AO和等待慢消费者。回调只读取、复制/交接并发布诊断信息。

### 保持的frame字段

```text
frame_id
channels
values
samples_per_channel
rate_per_channel
frame_duration_seconds
```

新增诊断字段可包括：`session_id`、`start_sample`、`end_sample`、`read_duration_ms`、`backlog`，旧消费者可以忽略。

### 验收

- 3通道、100 kS/s/通道、5 ms读取块连续运行30秒和300秒。
- 无DAQ错误，读取样本数等于理论值，backlog无长期增长。
- 旧100 ms frame的通道顺序和点数不变。
- 双峰、慢漂、功率锁定在同一输入下结果可比较。
- 读取错误不发布半帧，状态明确报错。

## 三、目标B：基于相对间距的峰识别

### 原有脚本的复用

`find_eom_peaks_width.py`可直接作为第一层候选检测基础，保留：

- 全波形局部极大值检测。
- 噪声和相对幅值阈值。
- 最小峰间距。
- 半显著性宽度。
- 左右下降沿检查。
- 拒绝候选峰的诊断输出。

但它目前只输出候选峰，不知道峰的物理身份。宽度应是质量分数的一部分；明显无左右下降沿的候选可拒绝，宽度偏离历史范围的峰先降低置信度，不要直接假设都是假峰。

### 核心识别特征

绝对峰位置随FP漂移，主特征是：

```text
abs(aom_first_index - carrier_index) ~= nominal_spacing_samples
```

新增间距模型：

```python
@dataclass
class SpacingModel:
    nominal_samples: float
    tolerance_samples: float
    revision: int
```

候选匹配伪代码：

```python
def match_pairs(candidates, spacing):
    pairs = []
    for carrier in candidates:
        for aom in candidates:
            if carrier is aom:
                continue
            distance = aom.index - carrier.index
            error = abs(abs(distance) - spacing.nominal_samples)
            if error <= spacing.tolerance_samples:
                pairs.append(score_pair(carrier, aom, error))
    return sorted(pairs, key=lambda pair: pair.score, reverse=True)
```

评分顺序建议为：

```text
载频-AOM间距误差       主要因素
峰宽和左右完整性       主要因素
显著性                 辅助因素
FSR/EOM频率模型残差    辅助因素
正程/回程一致性        辅助因素
峰高                   不能单独决定身份
```

### 启动扫描和跨帧跟踪

每次新启动扫描：

```text
丢弃上次绝对峰索引
保留间距、FSR、拐点和宽度模型
全波形找候选峰
重新匹配carrier-AOM pair
```

同一次扫描的后续帧：

```text
用上一帧pair预测当前pair
允许整体平移
继续检查两峰间距
```

匹配失败时不覆盖上一帧有效位置，不执行新的功率锁更新，记录`PAIR_NOT_CONFIDENT`。

### 锯齿模型用途

`fit_sawtooth_model.py`拆成可调用的：

```python
sample_to_scan_coordinate(index, geometry, model)
score_frequency_consistency(carrier, aom, calibration)
```

2500/7500拐点、FSR和两点标定只用于辅助解释和评分，不取代相对间距识别。

### 输出接口

```python
@dataclass
class IdentifiedPair:
    carrier_index: float
    aom_index: float
    spacing_samples: float
    segment: int
    direction: str
    confidence: float
    valid: bool
    reason: str | None
    calibration_revision: int
```

现有双峰算法继续接收动态峰位置；识别模块不写AO、不修改PI、不直接写慢漂统计。

### 验收

使用多次重新启动扫描的数据验证：

- 峰整体左右漂移时仍能匹配。
- 正程和回程不混配。
- 已知假峰不进入pair。
- 间距误差在配置容差内。
- 匹配失败不会驱动功率锁。
- 识别结果能画回波形供人工审阅。

## 四、目标C：PFI1触发窗口和独立反馈

### 推荐实现

不要每次PFI1触发都重新创建AI Task。继续让指定AI通道连续采样，PFI1只产生事件标记，然后按AI样本位置截取窗口：

```text
PFI1事件 -> anchor_sample
         -> delay_samples
         -> fixed window
         -> voltage statistic
         -> independent feedback
```

100 kS/s时：1 ms=100点，2 ms=200点。

### 配置

```python
@dataclass
class Pfi1WindowConfig:
    enabled: bool = False
    channel: str = "ai2"
    delay_s: float = 0.001
    duration_s: float = 0.001
    statistic: str = "mean"
    target_ao: str = "ao2"
```

`delay_s`表示触发后等待多久开始窗口，`duration_s`表示窗口长度。两者独立配置，避免混淆“触发后1ms开始”与“窗口长1ms”。

### PFI1事件定位

现有PFI边沿计数只能得到一段时间内的数量，不能得到每个边沿对应的AI样本位置。正式实现需先验证以下一种路线：

1. PFI1作为同步数字输入，与AI共享已验证的采样时间轴；或
2. 用硬件计数器记录边沿tick，再用已验证的时钟关系映射到AI sample index。

Python收到通知的时间不能作为精确触发位置。第一阶段只测单路PFI1，不同时实现多触发源。

### 窗口流程

```python
def on_pfi1_event(event):
    start = event.anchor_sample + round(delay_s * rate_hz)
    end = start + round(duration_s * rate_hz)
    pending.add(channel, start, end)

def on_watermark(end_sample):
    for job in pending.ready(end_sample):
        values = sample_buffer.copy_range(job.start, job.end, job.channel)
        measurement = statistic(values)
        publish_pfi1_measurement(measurement)
```

窗口未到、过期、事件质量不足时不进入反馈。

### 双峰排除规则

按sample range判断frame是否包含PFI1触发：

```python
frame_contains_event = (
    frame.start_sample <= event.anchor_sample < frame.end_sample
)
```

命中时：

```text
原始AI数据：保留
PFI1窗口：正常处理
双峰识别：跳过
双峰寻峰：跳过
双峰慢漂：跳过
双峰功率锁：跳过
```

默认只跳过包含触发点的旧业务frame。若实验表明触发后的扰动会持续一段时间，再增加`guard_pre/guard_post`保护区间。

### PFI1反馈

PFI1反馈使用独立控制器：

```python
def on_pfi_measurement(m):
    if not m.valid:
        return
    error = target - m.value
    proposed_ao = pfi_pi.update(error, m.dt)
    ao_manager.submit(owner="pfi1_slow_drift", channel=target_ao,
                      value=proposed_ao)
```

PFI1反馈默认使用未被双峰锁定占用的AO，例如AO2。若必须共享AO，必须先定义优先级、叠加关系、限幅、停止策略和故障交接，不能让两个线程直接写同一个AO。

### 验收顺序

1. 只记录PFI1事件数、间隔和sample index。
2. 验证1ms/2ms窗口的起止点和通道。
3. 验证窗口电压统计，不写AO。
4. 验证双峰跳过范围。
5. 确认没有明显漏事件或窗口过期。
6. 最后才接入独立AO负反馈。

## 五、文件修改顺序

### 阶段1：采集adapter

```text
新增/修改：usb6363/nidaqmx_driver.py、usb6363_core.py、对应测试
不改：two_peak算法、power_lock、trend_logger、AO/PFI旧语义
```

### 阶段2：离线EOM识别

```text
新增：two_peak/eom_peak_candidates.py
新增：two_peak/eom_spacing_matcher.py
新增：two_peak/scan_calibration.py
新增：对应离线测试和可视化诊断
```

`find_eom_peaks_width.py`作为候选峰实现参考，`fit_sawtooth_model.py`作为离线模型参考；不直接放进在线功率锁定线程。

### 阶段3：PFI1窗口

```text
先新增独立模块和测试
再增加硬件事件adapter
最后接入指定AO反馈
```

在PFI1 sample定位没有实机验证前，不改变现有PFI接口含义。

## 六、当前状态和下一步

已完成：

- DAQ读取路线性能验证。
- callback + 预分配reader的长测。
- 新的无硬件核心`daq_app/`。
- EOM峰宽检测和锯齿拟合脚本的离线分析。
- 自动寻峰占位符保留。

尚未完成：

- callback读取接入现有unified stream。
- 载频-AOM间距匹配器。
- 2500/7500配置化和FSR配置化。
- 图形两点频率标定。
- PFI1逐事件sample定位。
- PFI1窗口统计和独立反馈。
- 双峰跳过PFI1触发帧。

推荐下一步只做阶段1：先把优化读取接入现有unified stream并跑回归，不同时改峰识别或PFI1。每完成一个阶段单独测试并Git提交。
