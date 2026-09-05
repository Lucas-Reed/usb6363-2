"""动态窗口逐帧电压的分块二进制记录器。

这个模块只负责把 8766 已经拿到的窗口数据写入磁盘，不访问采集卡，也不参与找峰。
每个 NPZ 块保存固定数量的帧；停止时会把不足一个完整块的数据也写出来。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np


VALID_MODES = {"none", "a", "b", "both"}

# 写盘线程短暂落后时允许采集线程等待；超过这个时间说明继续运行已经无法保证完整。
WINDOW_QUEUE_PUT_TIMEOUT_SECONDS = 5.0

# Windows 上杀毒软件、索引器或其他读取进程可能短暂占用 manifest.json。
# 原子替换在约 5 秒内重试，避免一次瞬时 WinError 5/32 终止整场实验。
MANIFEST_REPLACE_TIMEOUT_SECONDS = 5.0
MANIFEST_REPLACE_INITIAL_DELAY_SECONDS = 0.02


class WindowVoltageRecorder:
    """用后台线程把动态窗口和可选的完整分析通道逐帧写成 NPZ。"""

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        mode: str,
        metadata: dict[str, Any],
        record_full_frame: bool = False,
        chunk_frames: int = 100,
        queue_frames: int = 500,
    ) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in VALID_MODES:
            raise ValueError("window voltage mode must be none, a, b or both")
        if normalized_mode == "none" and not record_full_frame:
            raise ValueError("至少需要选择一个动态窗口，或者启用完整分析通道记录")
        if chunk_frames < 1:
            raise ValueError("window voltage chunk_frames must be >= 1")
        if queue_frames < chunk_frames:
            raise ValueError("window voltage queue_frames must be >= chunk_frames")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("session_id 只能包含字母、数字、下划线和连字符")

        self._lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_frames)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_dir = output_dir / f"window_voltage_{session_id}"
        self._manifest_path = self._session_dir / "manifest.json"
        self._active_manifest_path = self._manifest_path
        self._session_id = session_id
        self._mode = normalized_mode
        self._record_full_frame = bool(record_full_frame)
        self._chunk_frames = int(chunk_frames)
        self._metadata = dict(metadata)
        self._started_at = time.time()
        self._finished_at: float | None = None
        self._running = False
        self._error: str | None = None
        self._frames_received = 0
        self._frames_written = 0
        self._source_gap_frames = 0
        self._queue_dropped_frames = 0
        baseline_frame_id = int(metadata.get("start_after_frame_id", 0) or 0)
        # 同步触发时把基线当作上一帧，这样第一帧若已经跳号也能准确计入缺口。
        self._last_received_frame_id: int | None = (
            baseline_frame_id if baseline_frame_id > 0 else None
        )
        self._first_written_frame_id: int | None = None
        self._last_written_frame_id: int | None = None
        self._chunks: list[dict[str, Any]] = []
        self._bytes_written = 0
        self._manifest_warning: str | None = None
        self._manifest_write_failures = 0

    def start(self) -> dict[str, Any]:
        """创建会话目录并启动写盘线程。"""

        self._session_dir.mkdir(parents=True, exist_ok=False)
        self._running = True
        self._write_manifest(completed=False)
        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="two-peak-window-voltage-writer",
        )
        self._thread.start()
        return self.status()

    def append(self, record: dict[str, Any]) -> None:
        """把一帧窗口数据放入写盘队列；队列满时最多等待 5 秒。"""

        frame_id = int(record["frame_id"])
        with self._lock:
            if not self._running or self._error:
                return

        try:
            # 这里主动施加背压：宁可短暂等写盘，也不能悄悄扔掉实验帧。
            self._queue.put(record, timeout=WINDOW_QUEUE_PUT_TIMEOUT_SECONDS)
        except queue.Full:
            with self._lock:
                self._error = (
                    f"窗口电压写入队列持续满 {WINDOW_QUEUE_PUT_TIMEOUT_SECONDS:g} 秒，"
                    f"frame_id={frame_id} 未能写入；记录已停止。"
                )
                self._queue_dropped_frames += 1
            self._stop_event.set()
            return

        with self._lock:
            if self._last_received_frame_id is not None and frame_id > self._last_received_frame_id + 1:
                self._source_gap_frames += frame_id - self._last_received_frame_id - 1
            self._last_received_frame_id = frame_id
            self._frames_received += 1

    def stop(self) -> dict[str, Any]:
        """停止接收新帧，等待队列写完并刷新最后一个不完整块。"""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15.0)
        with self._lock:
            if thread is not None and thread.is_alive() and self._error is None:
                self._error = "窗口电压写盘线程未能在 15 秒内停止"
            self._running = False
            self._finished_at = self._finished_at or time.time()
        # 正常退出时 worker 已经写过最终清单；只有线程未能退出时才由 stop 补写。
        if thread is None or thread.is_alive():
            self._write_manifest(
                completed=self._error is None and not (thread and thread.is_alive()),
                final=True,
            )
        return self.status()

    def status(self) -> dict[str, Any]:
        """返回 WebUI 可以安全显示的小体积状态，不返回原始电压数组。"""

        with self._lock:
            return {
                "enabled": True,
                "running": self._running,
                "error": self._error,
                "mode": self._mode,
                "record_full_frame": self._record_full_frame,
                "session_id": self._session_id,
                "directory": str(self._session_dir.resolve()),
                "manifest_file": str(self._active_manifest_path.resolve()),
                "manifest_warning": self._manifest_warning,
                "manifest_write_failures": self._manifest_write_failures,
                "chunk_frames": self._chunk_frames,
                "frames_received": self._frames_received,
                "frames_written": self._frames_written,
                "source_gap_frames": self._source_gap_frames,
                "queue_dropped_frames": self._queue_dropped_frames,
                "chunks_written": len(self._chunks),
                "bytes_written": self._bytes_written,
                "first_frame_id": self._first_written_frame_id,
                "last_frame_id": self._last_written_frame_id,
            }

    def _worker(self) -> None:
        """在后台聚合数据并写块。"""

        pending: list[dict[str, Any]] = []
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    pending.append(self._queue.get(timeout=0.1))
                except queue.Empty:
                    pass
                if len(pending) >= self._chunk_frames:
                    self._write_chunk(pending[: self._chunk_frames])
                    del pending[: self._chunk_frames]

            if pending:
                self._write_chunk(pending)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            with self._lock:
                self._running = False
                self._finished_at = time.time()
            self._write_manifest(completed=self._error is None, final=True)

    def _write_chunk(self, records: list[dict[str, Any]]) -> None:
        """把一批同宽窗口写成一个未压缩 NPZ，并用原子改名完成落盘。"""

        chunk_index = len(self._chunks) + 1
        name = f"chunk_{chunk_index:06d}.npz"
        final_path = self._session_dir / name
        temp_path = self._session_dir / f".{name}.tmp"

        arrays: dict[str, np.ndarray] = {
            "frame_id": np.asarray([row["frame_id"] for row in records], dtype=np.int64),
            "finished_at": np.asarray([row["finished_at"] for row in records], dtype=np.float64),
        }
        # 完整波形模式额外保存帧起止时间和 PFI 重同步分段信息。
        # 这些元数据可以让离线分析把采样点索引换算成真实实验时间。
        for field, dtype in (
            ("started_at", np.float64),
            ("segment_id", np.int64),
            ("segment_frame_id", np.int64),
        ):
            if all(field in row for row in records):
                arrays[field] = np.asarray([row[field] for row in records], dtype=dtype)
        if self._mode in ("a", "both"):
            arrays.update(_window_arrays(records, "a"))
        if self._mode in ("b", "both"):
            arrays.update(_window_arrays(records, "b"))
        if self._record_full_frame:
            arrays["full_values"] = _full_frame_array(records)

        with temp_path.open("wb") as file:
            np.savez(file, **arrays)
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(final_path)

        byte_count = final_path.stat().st_size
        first_frame_id = int(records[0]["frame_id"])
        last_frame_id = int(records[-1]["frame_id"])
        with self._lock:
            self._frames_written += len(records)
            self._first_written_frame_id = self._first_written_frame_id or first_frame_id
            self._last_written_frame_id = last_frame_id
            self._bytes_written += byte_count
            self._chunks.append(
                {
                    "file": name,
                    "frames": len(records),
                    "first_frame_id": first_frame_id,
                    "last_frame_id": last_frame_id,
                    "bytes": byte_count,
                }
            )
        self._write_manifest(completed=False)

    def _manifest_payload(self, completed: bool) -> dict[str, Any]:
        """生成 manifest 内容；调用者随后负责原子写入。"""

        with self._lock:
            return {
                "format": (
                    "two_peak_window_voltage_npz_v2"
                    if self._record_full_frame
                    else "two_peak_window_voltage_npz_v1"
                ),
                "session_id": self._session_id,
                "mode": self._mode,
                "record_full_frame": self._record_full_frame,
                "dtype": "float32",
                "chunk_frames": self._chunk_frames,
                "started_at": datetime.fromtimestamp(self._started_at).isoformat(timespec="milliseconds"),
                "finished_at": (
                    datetime.fromtimestamp(self._finished_at).isoformat(timespec="milliseconds")
                    if self._finished_at is not None
                    else None
                ),
                "completed": bool(completed),
                "error": self._error,
                "frames_received": self._frames_received,
                "frames_written": self._frames_written,
                "source_gap_frames": self._source_gap_frames,
                "queue_dropped_frames": self._queue_dropped_frames,
                "manifest_warning": self._manifest_warning,
                "manifest_write_failures": self._manifest_write_failures,
                "first_frame_id": self._first_written_frame_id,
                "last_frame_id": self._last_written_frame_id,
                "bytes_written": self._bytes_written,
                "chunks": list(self._chunks),
                "settings": dict(self._metadata),
            }

    def _write_manifest(self, completed: bool, final: bool = False) -> bool:
        """更新清单；临时占用只产生 warning，不中断 NPZ 数据写入。

        final=True 表示记录正在收尾。若固定的 manifest.json 始终被占用，
        会改写一个独立的 manifest_final_*.json，确保最终元数据仍可恢复。
        """

        with self._manifest_lock:
            payload = self._manifest_payload(completed)
            # 本次若成功，旧 warning 已经恢复，因此写入文件时清空它。
            payload["manifest_warning"] = None
            try:
                self._atomic_write_json(self._manifest_path, payload)
            except OSError as exc:
                warning = f"manifest.json 更新失败：{exc}"
                with self._lock:
                    self._manifest_write_failures += 1
                    self._manifest_warning = warning

                if not final:
                    return False

                fallback_path = self._session_dir / (
                    "manifest_final_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json"
                )
                fallback_payload = self._manifest_payload(completed)
                fallback_payload["manifest_warning"] = (
                    warning + f"；最终清单已改写到 {fallback_path.name}"
                )
                try:
                    self._atomic_write_json(fallback_path, fallback_payload)
                except OSError as fallback_exc:
                    with self._lock:
                        self._manifest_write_failures += 1
                        self._manifest_warning = (
                            warning + f"；备用最终清单也写入失败：{fallback_exc}"
                        )
                    return False

                with self._lock:
                    self._active_manifest_path = fallback_path
                    self._manifest_warning = fallback_payload["manifest_warning"]
                return True

            with self._lock:
                self._active_manifest_path = self._manifest_path
                self._manifest_warning = None
            return True

    def _atomic_write_json(self, final_path: Path, payload: dict[str, Any]) -> None:
        """用唯一临时文件写 JSON，并对 Windows 文件占用进行退避重试。"""

        temp_path = final_path.with_name(
            f".{final_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())

            deadline = time.monotonic() + MANIFEST_REPLACE_TIMEOUT_SECONDS
            delay = MANIFEST_REPLACE_INITIAL_DELAY_SECONDS
            while True:
                try:
                    os.replace(temp_path, final_path)
                    return
                except OSError as exc:
                    # WinError 5 是拒绝访问，WinError 32 是文件正被其他进程使用。
                    # PermissionError 在不同 Python/Windows 版本上可能只提供 errno。
                    winerror = getattr(exc, "winerror", None)
                    retryable = isinstance(exc, PermissionError) or winerror in (5, 32)
                    if not retryable or time.monotonic() >= deadline:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2.0, 0.5)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                # 临时文件清理失败不应覆盖真正的 manifest 写入错误。
                pass


def _window_arrays(records: list[dict[str, Any]], prefix: str) -> dict[str, np.ndarray]:
    """把同一个窗口的逐帧字典整理成 NPZ 数组。"""

    values = [np.asarray(row[f"{prefix}_values"], dtype=np.float32) for row in records]
    widths = {int(item.size) for item in values}
    if len(widths) != 1:
        raise RuntimeError(f"窗口 {prefix.upper()} 的点数在同一记录中发生了变化")
    return {
        f"{prefix}_values": np.stack(values, axis=0),
        f"{prefix}_left": np.asarray([row[f"{prefix}_left"] for row in records], dtype=np.int32),
        f"{prefix}_right": np.asarray([row[f"{prefix}_right"] for row in records], dtype=np.int32),
        f"{prefix}_track_peak_index": np.asarray(
            [_optional_index(row.get(f"{prefix}_track_peak_index")) for row in records],
            dtype=np.int32,
        ),
        f"{prefix}_peak_height_index": np.asarray(
            [_optional_index(row.get(f"{prefix}_peak_height_index")) for row in records],
            dtype=np.int32,
        ),
    }


def _full_frame_array(records: list[dict[str, Any]]) -> np.ndarray:
    """整理完整分析通道波形，并拒绝一次记录中途改变每帧点数。"""

    values = [np.asarray(row["full_values"], dtype=np.float32) for row in records]
    sample_counts = {int(item.size) for item in values}
    if len(sample_counts) != 1:
        raise RuntimeError("完整分析通道的每帧点数在同一记录中发生了变化")
    return np.stack(values, axis=0)


def _optional_index(value: Any) -> int:
    """NPZ 的整数数组不能保存 None，因此用 -1 表示没有该索引。"""

    return -1 if value is None else int(value)
