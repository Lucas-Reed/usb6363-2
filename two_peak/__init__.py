"""双峰锁定的新实现包。

这个包只放“双峰锁定”相关逻辑，不直接调用 NI-DAQmx。
硬件访问必须通过 usb6363_client.Usb6363Client 走本地 API 服务。
"""

