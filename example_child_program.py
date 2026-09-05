"""子 Python 程序调用 USB-6363 的推荐示例。

运行这个脚本前，请先启动：
    1. python usb6363_server.py
    2. python ai_stream_console.py

然后在统一 AI 控制台里启动 unified stream，例如 channels=ai0,ai1,ai2。

重点：
- 子程序只 import usb6363_client。
- 子程序不 import nidaqmx。
- 子程序不自己启动新的 AI task。
- 子程序只读取已经运行的 unified AI stream，避免和双峰、功率慢漂互相抢 USB-6363。
"""

from __future__ import annotations

from usb6363_client import Usb6363Client


def main() -> int:
    """演示一个普通子程序应该怎样读取统一 AI 流。"""

    daq = Usb6363Client()

    # 先确认底层 API 服务在线，并查看目标设备。
    print("device:", daq.get_device())

    # 查询统一 AI 流状态。
    status = daq.get_unified_ai_stream_status()
    print("unified running:", status.get("running"))
    print("unified channels:", (status.get("settings") or {}).get("channels"))

    if not status.get("running"):
        raise RuntimeError(
            "统一 AI 流没有运行。请先打开 ai_stream_console.py，设置 channels 并启动统一流。"
        )

    # 读取某个通道最近一个点。这个请求很小，适合实时状态显示。
    latest_ai0 = daq.get_unified_ai_latest("ai0")
    print(f"latest ai0 = {latest_ai0['value']:.6f} V")

    # 读取某个通道最近一段缓存的统计量。这个比返回大数组更适合长期监测和反馈。
    stats_ai1 = daq.get_unified_ai_stats("ai1", max_samples=1000)
    print(
        "ai1 stats:",
        f"mean={stats_ai1['mean']:.6e}",
        f"std={stats_ai1['std']:.6e}",
        f"samples={stats_ai1['samples']}",
    )

    # 如果确实需要小段原始数据，可以读取有限长度的 buffer。
    # 不要把高速长时间数据塞进 JSON；那类数据以后应走专门的文件记录模式。
    buffer_ai2 = daq.get_unified_ai_buffer("ai2", max_samples=20)
    print("ai2 recent values:", buffer_ai2["values"])

    # AO/PFI 不属于 AI task，本例保留一个 AO 和 PFI 示例。
    # 注意：写 AO 会真实改变输出电压，请确认接线安全。
    ao_result = daq.write_ao(channel="ao0", value=0.0)
    print(f"Set {ao_result['channel']} to {ao_result['value']:.3f} V")

    pfi_result = daq.read_pfi(line="PFI0")
    print(f"PFI0 = {pfi_result['value']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
