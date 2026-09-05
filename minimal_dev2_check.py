"""USB-6363 最小通信检查脚本。

运行方式：
    python minimal_dev2_check.py

用途：
1. 检查 NI-DAQmx 是否能看到 Dev2。
2. 从 Dev2/ai0 读取一个电压值，确认基本通信正常。

注意：
这个脚本会直接使用底层 DaqController，适合硬件检查。
未来多个子程序同时运行时，推荐启动 usb6363_server.py，
然后让子程序通过 usb6363_client.py 调用。
"""

from __future__ import annotations

from usb6363_core import DaqController


def main() -> int:
    # 创建底层控制器。默认设备名是 usb6363_core.py 里的 Dev2。
    controller = DaqController()

    # 先列出电脑上 NI-DAQmx 能看到的设备，方便确认设备名。
    devices = controller.list_devices()

    print("NI-DAQmx devices found:")
    if not devices:
        print("  (none)")
    for device in devices:
        print(f"  {device.name}: {device.product_type}, serial={device.serial_num}")

    try:
        # 确认 Dev2 存在，并读取它的型号/序列号。
        device = controller.get_device_info()
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return 1

    print(f"\nConnected to {device.name}")
    print(f"  Product type: {device.product_type}")
    print(f"  Serial number: {device.serial_num}")

    print("\nReading one sample from Dev2/ai0...")

    # 从 ai0 读一个瞬时电压。这个值会随输入端接线状态变化。
    result = controller.read_ai_voltage(channel="ai0")
    print(f"  {result['channel']} = {result['values']:.6f} V")
    print("\nCommunication check complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
