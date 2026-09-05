"""双峰波形查看器的运行状态。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from two_peak.ao_scan_calibrator import AoScanCalibrator
from two_peak.config import TwoPeakSettings
from two_peak.power_lock import PowerLockController
from two_peak.sync_test import SyncTestCoordinator
from two_peak.trend_logger import AreaTrendLogger
from usb6363_client import Usb6363Client


class ViewerState:
    """保存查看器自己的内存状态。

    这里保存最近采到的一帧，方便 WebUI 点击“保存样本”时不用重新采集。
    这不是采集卡状态，也不会直接访问 NI-DAQmx。
    """

    def __init__(self, api_base_url: str, sample_dir: Path) -> None:
        self.daq = Usb6363Client(base_url=api_base_url)
        self.sample_dir = sample_dir
        self.settings = TwoPeakSettings.defaults()
        # 用户在 WebUI 里点击“保存为默认值”后，会写到这个 JSON 文件。
        # 它放在 data/ 下面，属于实验运行时配置，不进入 git。
        self.defaults_path = sample_dir.parent / "two_peak_defaults.json"
        self.user_defaults = self._load_user_defaults()
        self.latest_frame: dict[str, Any] | None = None
        self.latest_measurement: dict[str, Any] | None = None
        # 慢漂记录器会在后端线程里读取底层 frame_stream 最新帧并写 CSV。
        # 它不依赖浏览器是否一直打开。
        self.trend_logger = AreaTrendLogger(
            daq=self.daq,
            output_dir=sample_dir.parent / "two_peak_trends",
        )
        # AO 扫描标定器读取上面的面积慢漂统计，并通过 daq client 写 AO。
        # 它不直接访问 NI-DAQmx，因此不会破坏“只有 8765 底层服务碰硬件”的边界。
        self.ao_scan_calibrator = AoScanCalibrator(
            daq=self.daq,
            trend_logger=self.trend_logger,
            output_dir=sample_dir.parent / "ao_scan_calibrations",
        )
        # 双路慢速功率锁定器读取面积慢漂统计，并通过 daq client 写 AO。
        # 它只在用户点击“启动锁定”后运行，默认不会自动闭环。
        self.power_lock = PowerLockController(
            daq=self.daq,
            trend_logger=self.trend_logger,
            # 每次锁定单独记录采样峰、锁定峰、实际比值、PI 误差和 AO 输出，
            # 便于实验结束后判断稳定效果以及排查达到限幅的时段。
            output_dir=sample_dir.parent / "power_lock_runs",
        )
        # 临时同步测试只协调现有记录器，不直接接触采集卡。
        self.sync_test = SyncTestCoordinator(
            daq=self.daq,
            trend_logger=self.trend_logger,
            output_dir=sample_dir.parent / "sync_tests",
        )

    def factory_web_defaults(self) -> dict[str, Any]:
        """返回 WebUI 可以直接使用的出厂默认值。

        TwoPeakSettings 里保留的是比较结构化的实验参数；
        WebUI 的 input id 更扁平，所以这里额外给出一份方便前端填表的字段。
        """

        parameters = self.settings.to_web_parameters()
        peak_indices = parameters["peak_indices"]
        parameters.update(
            {
                "channels": ", ".join(parameters["ai_channels"]),
                "rate": parameters["sample_rate"],
                "samples": parameters["samples_per_frame"],
                "min_val": parameters["ai_min_val"],
                "max_val": parameters["ai_max_val"],
                "timeout": self.settings.daq.timeout,
                "trigger_enabled": False,
                "trigger_source": "PFI0",
                "trigger_edge": "RISING",
                "peak0": peak_indices[0],
                "peak1": peak_indices[1],
                "analysis_channel_index": 0,
                "search_window_half": parameters["window_half"],
                "measure_half": parameters["peak_avg_half"],
                # 双峰慢漂与逐帧 NPZ 默认记录两小时；WebUI 填 0 可恢复为不限时。
                "duration_minutes": 120.0,
                "top_percent": 10.0,
                "record_full_frame": False,
            }
        )
        return parameters

    def active_web_defaults(self) -> dict[str, Any]:
        """返回当前启动时实际使用的默认值。

        如果用户保存过默认值，就在出厂默认值上覆盖用户默认值；
        这样以后新增字段时，旧的 JSON 文件也不会缺字段。
        """

        parameters = self.factory_web_defaults()
        parameters.update(self.user_defaults)
        parameters["defaults_source"] = "user" if self.user_defaults else "factory"
        parameters["defaults_file"] = str(self.defaults_path.resolve())
        return parameters

    def save_user_defaults(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """把当前 WebUI 参数保存为下次启动时的默认值。"""

        self.defaults_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_defaults = dict(parameters)
        self.defaults_path.write_text(
            json.dumps(self.user_defaults, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.active_web_defaults()

    def reset_user_defaults(self) -> dict[str, Any]:
        """删除用户默认值，恢复到代码里的出厂默认值。"""

        self.user_defaults = {}
        if self.defaults_path.exists():
            self.defaults_path.unlink()
        return self.active_web_defaults()

    def _load_user_defaults(self) -> dict[str, Any]:
        """启动查看器时读取用户默认值文件。"""

        if not self.defaults_path.exists():
            return {}
        try:
            data = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data
