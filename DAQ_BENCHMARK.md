# DAQmx 独立采集测试

`daq_benchmark.py` 直接访问设备，不依赖原项目服务器，不产生 AO/DO 输出。
每组使用一个连续 AI Task，启动一次后循环读取，不使用外部触发。
运行前停止原有 AI 采集，避免占用同一硬件资源。

## 运行

在安装了 NI-DAQmx 驱动的采集电脑上，使用该电脑的 Python：

```powershell
python -m pip install numpy nidaqmx
python daq_benchmark.py
```

默认 `Dev2`、`ai0,ai1,ai2`、DIFF、-10 至 +10 V、100000 S/s/通道。
按实际接线修改端接方式、量程和设备名，不能用这些默认值判断输入信号是否正确。
默认运行 10 组：task/reader/callback 各 10、5、1 ms 三组，available 单独一组。
每组预热 2 秒，再测量至少 30 秒；总计约 5 分多钟。

先只比较最重要的两种读取方式，约 3 分钟：

```powershell
python daq_benchmark.py --methods task reader
```

只快速试读一组，或对候选方案重复测试：

```powershell
python daq_benchmark.py --methods reader --chunks-ms 10 --seconds 10
python daq_benchmark.py --methods reader --chunks-ms 5 --seconds 300 --repeat 3
```

修改设备和参数：

```powershell
python daq_benchmark.py --device Dev1 --channels ai0,ai1,ai2 --rate 100000 --terminal DIFF --min-v -10 --max-v 10
```

## 四种路线

| 参数 | 实现 |
|---|---|
| task | `Task.read(N)`，返回 Python 列表 |
| reader | `AnalogMultiChannelReader`，复用预分配 float64 数组 |
| callback | Every-N 回调内用预分配 reader 读取；主线程等待 |
| available | 查询当前可读点数，用预分配 reader 读取，不超过数组容量；每轮 sleep |

available 不使用固定 chunk，默认 `--poll-ms 1`。Windows 的 sleep 不保证精确 1 ms。
脚本记录实际批次大小，不能把它和固定块方案的单次读取耗时直接等同。
它使用缓冲容量大小的数组限制单次读取量，不调用会返回任意长度数组的 READ_ALL_AVAILABLE。

## 看结果

每组结束后打印一行摘要，并更新 `data/daq_benchmark/benchmark_时间.csv` 和同名 JSON。
CSV 可直接用 Excel 打开。JSON 额外包含软件版本和约每秒一次的缓冲记录。
Ctrl+C 保存当前已累计的统计后退出；组内错误会记录，并继续下一组。

| 字段 | 含义 |
|---|---|
| status / error_code / error | 是否执行完毕、错误码、错误详情 |
| actual_rate_per_channel | DAQmx 返回的配置采样率，不是独立测量的硬件时钟频率 |
| delivered_samples_per_channel_s | 预热后读取点数 / 两次读取完成边界间的测量时间 |
| return_interval_ms_p99 | 连续两次成功读取完成之间的间隔 P99，含调度及统计开销 |
| read_duration_ms_p99 | 读取调用耗时 P99，含等数据时间 |
| backlog_after_read_max | 读取后观察到的最大未读点数/通道 |
| backlog_slope_samples_per_channel_s | 按读取后观测点拟合的积压斜率，结合 JSON 时间序列判断 |
| cpu_percent_one_core | 整个 Python 进程 CPU 时间 / 测量时间；100% 表示一个逻辑核 |
| end_unread_per_channel | 关闭 Task 前的未读点数，与最后一次统计不一定同一时刻 |
| acquired_per_channel_including_warmup | 驱动累计采样数，包含预热及收尾，不与测量期读取数直接相减 |

`completed` 只表示执行结束，不自动判为无丢样或低延迟。
有 DAQ 错误、积压持续上涨时先定位原因；积压稳定且 CPU 有余量时，再比较返回间隔。
均值和斜率按每次成功读取观测统计，不是等时间采样统计；不同路线应结合时间序列判断。
分位数最多保留最近 200000 次读取；超出时 `distribution_truncated=true`。
总点数、最大值和积压回归仍覆盖全部测量期，约每秒一条的时间序列随测试时长增长。

本脚本只测主机数据交付，不测物理输入到算法或输出的端到端延迟，也不校验波形连续性。
四种路线均不做下游环形缓冲复制、算法、记录或 HTTP；正式方案还需加入真实消费者复测。
每次读取都查询缓冲并统计，存在相同类别的测量开销，小块测试尤其应考虑该开销。

## 离线检查

仅需 NumPy，不访问硬件：

```powershell
python -m unittest discover -s tests -p test_daq_benchmark.py -v
```
