# daq_app

这是重构后的最小、无硬件核心。它暂时不导入 `nidaqmx`，不修改现有 `usb6363` 驱动。

当前内容：

- 有界、按绝对样本序号读取的 `SampleBuffer`。
- 1 ms/100 ms均可表达的窗口调度器。
- 双峰测量：已知峰中心、邻域寻峰、height/area；自动寻峰仍明确抛出 `NotImplementedError`。
- 慢漂均值、标准差和EMA。
- 纯PI计算和AO lease所有权检查。
- 合成块重放入口。

运行测试：

```powershell
python -m unittest discover -s tests -p 'test_daq_app_core.py' -v
```

下一阶段才接入现有已验证的连续AI读取路径。接入时应通过单独adapter调用 `usb6363.nidaqmx_driver`，不把DAQmx导入处理模块。
