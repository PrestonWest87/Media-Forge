"""GPU-only media health checks and transactional H.265 transcoding."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog

logger = logging.getLogger(__name__)

NVDEC_CODECS = {"h264", "hevc", "mpeg2video", "mpeg4", "vc1", "vp8", "vp9", "av1"}
TRANSCODE_CODECS = {"h264", "mpeg2video", "vc1", "mpeg4", "vp8", "vp9"}
ACTIVE_STATUSES = {"CHECKING", "TRANSCODING", "VERIFYING_OUTPUT", "PAUSED_YIELD"}


@dataclass
class CommandResult:
    returncode: int
    stderr: str


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return int(parsed)


def parse_probe_data(payload: dict[str, Any]) -> dict[str, Any] | None:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        return None
    video = video_streams[0]
    format_info = payload.get("format") or {}
    duration = _number(format_info.get("duration"))
    if duration is None:
        duration = _number(video.get("duration"))
    bit_rate = _integer(video.get("bit_rate"))
    if bit_rate is None:
        bit_rate = _integer(format_info.get("bit_rate"))
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    return {
        "codec_name": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "width": _integer(video.get("width")),
        "height": _integer(video.get("height")),
        "duration": duration,
        "bit_rate": bit_rate,
        "audio_streams": len(audio),
        "sub_streams": len(subtitles),
        "video_index": _integer(video.get("index")) or 0,
        "subtitle_streams": [
            {"index": _integer(stream.get("index")), "codec_name": stream.get("codec_name")}
            for stream in subtitles
        ],
    }


def metadata_db_fields(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "codec": info.get("codec_name"),
        "pix_fmt": info.get("pix_fmt"),
        "bit_rate": info.get("bit_rate"),
        "width": info.get("width"),
        "height": info.get("height"),
        "audio_streams": int(info.get("audio_streams") or 0),
        "sub_streams": int(info.get("sub_streams") or 0),
        "duration": info.get("duration"),
    }


def is_h264_10_bit(info: dict[str, Any]) -> bool:
    if str(info.get("codec_name") or "").lower() != "h264":
        return False
    pixel_format = str(info.get("pix_fmt") or "").lower()
    return "10le" in pixel_format or "10be" in pixel_format


def efficiency_floor(height: int | None) -> int:
    if height is None:
        return 2_200_000
    if height <= 480:
        return 600_000
    if height <= 720:
        return 1_100_000
    return 2_200_000


def route_media(info: dict[str, Any], settings: Settings) -> str:
    """Return the health-only or transcode route from source metadata."""

    codec = str(info.get("codec_name") or "").lower()
    if settings.operation_mode != "transcode" or codec in {"hevc", "av1"}:
        return "health"
    if codec not in TRANSCODE_CODECS:
        return "health"
    bit_rate = info.get("bit_rate")
    if bit_rate is None or int(bit_rate) <= efficiency_floor(info.get("height")):
        return "health"
    return "transcode"


def build_decode_command(file_path: str, progress: bool = True, gpu_index: int = 0) -> list[str]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-hwaccel",
        "cuda",
        "-hwaccel_device",
        str(gpu_index),
        "-hwaccel_output_format",
        "cuda",
    ]
    if progress:
        command.extend(["-progress", "pipe:1", "-nostats"])
    command.extend(
        [
            "-i",
            file_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            "-f",
            "null",
            "-",
        ]
    )
    return command


def _quality_value(info: dict[str, Any], settings: Settings) -> int:
    height = int(info.get("height") or 0)
    if settings.quality_profile == "balanced":
        return 22 if height <= 720 else 23
    return 19 if height <= 720 else 20


def build_transcode_command(
    file_path: str,
    temp_path: str,
    info: dict[str, Any],
    settings: Settings,
    progress: bool = True,
) -> list[str]:
    source_bitrate = int(info["bit_rate"])
    extension = Path(file_path).suffix.lower()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-xerror",
        "-hwaccel",
        "cuda",
        "-hwaccel_device",
        str(settings.gpu_index),
        "-hwaccel_output_format",
        "cuda",
        "-extra_hw_frames",
        "16",
    ]
    if progress:
        command.extend(["-progress", "pipe:1", "-nostats"])
    command.extend(["-i", file_path, "-map", "0:v:0", "-map", "0:a?"])

    if extension in {".mp4", ".m4v"}:
        # Only mov_text is known to be valid for MP4. In particular, bitmap
        # subtitles are deliberately omitted rather than causing a mux error.
        for subtitle in info.get("subtitle_streams", []):
            if subtitle.get("codec_name") == "mov_text" and subtitle.get("index") is not None:
                command.extend(["-map", f"0:{subtitle['index']}"])
        extra_container_flags = ["-tag:v", "hvc1", "-movflags", "+faststart"]
    else:
        command.extend(["-map", "0:s?", "-map", "0:t?"])
        extra_container_flags = ["-c:t", "copy"]

    command.extend(
        [
            "-c:v",
            "hevc_nvenc",
            "-gpu",
            str(settings.gpu_index),
            "-preset",
            settings.nvenc_preset,
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(_quality_value(info, settings)),
            "-b:v",
            "0",
            "-maxrate",
            str(int(source_bitrate * 0.85)),
            "-bufsize",
            str(int(source_bitrate * 1.70)),
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-aq-strength",
            "8",
            "-rc-lookahead",
            "20",
            "-multipass",
            "qres",
            "-bf",
            "4",
            "-b_ref_mode",
            "middle",
            *extra_container_flags,
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-max_muxing_queue_size",
            "4096",
            temp_path,
        ]
    )
    return command


def output_temp_path(file_path: str) -> str:
    extension = Path(file_path).suffix.lower()
    target_extension = ".mp4" if extension in {".mp4", ".m4v"} else ".mkv"
    return f"{file_path}.gpuforge_tmp{target_extension}"


def final_output_path(file_path: str) -> str:
    """Use an extension that agrees with the selected output container."""

    extension = Path(file_path).suffix.lower()
    if extension in {".mp4", ".m4v", ".mkv"}:
        return file_path
    return str(Path(file_path).with_suffix(".mkv"))


def target_has_hwaccel(command: list[str]) -> bool:
    """Small guard used by acceptance checks and before launching FFmpeg."""

    return (
        "-xerror" in command
        and "-hwaccel" in command
        and command[command.index("-hwaccel") + 1] == "cuda"
        and "-hwaccel_output_format" in command
        and command[command.index("-hwaccel_output_format") + 1] == "cuda"
    )


class MediaWorker:
    def __init__(self, database: Database, settings_store: SettingsStore, watchdog: GpuWatchdog) -> None:
        self.database = database
        self.settings_store = settings_store
        self.watchdog = watchdog
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._test_queue: asyncio.Queue[int] = asyncio.Queue()
        self._work_available = asyncio.Event()
        self._queued: set[int] = set()
        self._skip_normal_ids: set[int] = set()
        self._test_only_ids: set[int] = set()
        self._full_test_ids: set[int] = set()
        self._test_previous_status: dict[int, str] = {}
        self.queue_enabled = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._active_process: asyncio.subprocess.Process | None = None
        self._current_file_id: int | None = None
        self._current_path: str | None = None
        self._current_phase = "IDLE"
        self._yielding = False
        self._yield_reason: str | None = None
        self._started_at: float | None = None
        self._current_duration = 0.0
        self._current_progress_base = 0.0
        self._current_progress_scale = 100.0
        self._last_progress_at: float | None = None
        self._last_progress_frame = 0
        self.current_fps = 0.0
        self.current_progress_percent = 0.0
        self.state = "IDLE"

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run_queue(), name="gpu-forge-worker")

    async def start_queue(self) -> int:
        self.queue_enabled = True
        self._work_available.set()
        return await self.enqueue_pending()

    def pause_queue(self) -> None:
        # A currently running file finishes safely; the remaining queue waits.
        self.queue_enabled = False

    async def stop(self) -> None:
        self._stopping = True
        process = self._active_process
        if process and process.returncode is None:
            try:
                os.kill(process.pid, signal.SIGCONT)
                process.terminate()
            except ProcessLookupError:
                pass
            except OSError:
                logger.warning("Unable to terminate FFmpeg PID %s", process.pid, exc_info=True)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def enqueue_pending(self) -> int:
        if not self.queue_enabled:
            return 0
        added = 0
        for row in await self.database.pending_files():
            file_id = int(row["id"])
            if file_id in self._queued:
                continue
            self._queued.add(file_id)
            self._queue.put_nowait(file_id)
            added += 1
        if added:
            self._work_available.set()
        return added

    async def enqueue_test(self, file_id: int, *, full_pipeline: bool = False) -> bool:
        """Prioritize one selected file for a health-only or full pipeline test."""

        if self._current_file_id == file_id or file_id in self._test_only_ids:
            return False
        row = await self.database.get_file(file_id)
        if row is None or (row.get("backup_path") and os.path.exists(row["backup_path"])):
            return False
        # The active ID is set before the worker's first await, but check again
        # after the DB read to close the selection/start race.
        if self._current_file_id == file_id or file_id in self._test_only_ids:
            return False

        was_queued = file_id in self._queued
        if was_queued:
            self._skip_normal_ids.add(file_id)
        else:
            self._queued.add(file_id)
        self._test_only_ids.add(file_id)
        if full_pipeline:
            self._full_test_ids.add(file_id)
        self._test_previous_status[file_id] = str(row["status"])
        try:
            await self.database.update_file(file_id, status="PENDING", error_log=None)
        except Exception:
            self._test_only_ids.discard(file_id)
            self._full_test_ids.discard(file_id)
            self._test_previous_status.pop(file_id, None)
            self._skip_normal_ids.discard(file_id)
            if not was_queued:
                self._queued.discard(file_id)
            raise

        self._test_queue.put_nowait(file_id)
        self._work_available.set()
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": (
                "PAUSED_THERMAL"
                if self._yielding and self._current_file_id and self._yield_reason == "thermal"
                else "PAUSED_YIELD"
                if self._yielding and self._current_file_id
                else self.state
            ),
            "current_file_id": self._current_file_id,
            "current_path": self._current_path,
            "current_phase": self._current_phase,
            "current_fps": round(self.current_fps, 1),
            "current_progress_percent": round(self.current_progress_percent, 1),
            "queued": self._queue.qsize() + self._test_queue.qsize(),
            "queue_enabled": self.queue_enabled,
            "current_elapsed_sec": round(time.monotonic() - self._started_at, 1) if self._started_at else 0,
        }

    async def set_yielding(self, paused: bool, reason: str | None = None) -> None:
        self._yielding = paused
        self._yield_reason = reason if paused else None
        if self._current_file_id is None:
            return
        if paused:
            await self.database.update_file(self._current_file_id, status="PAUSED_YIELD")
            self.state = "PAUSED_THERMAL" if reason == "thermal" else "PAUSED_YIELD"
        else:
            resume_status = self._current_phase
            await self.database.update_file(self._current_file_id, status=resume_status)
            self.state = "VERIFYING_OUTPUT" if resume_status == "VERIFYING_OUTPUT" else "ACTIVE"

    async def _run_queue(self) -> None:
        while not self._stopping:
            is_test = False
            if not self._test_queue.empty():
                file_id = self._test_queue.get_nowait()
                is_test = True
            elif self.queue_enabled and not self._queue.empty():
                file_id = self._queue.get_nowait()
            else:
                self._work_available.clear()
                if not self._test_queue.empty() or (self.queue_enabled and not self._queue.empty()):
                    continue
                await self._work_available.wait()
                continue

            if not is_test and file_id in self._skip_normal_ids:
                self._skip_normal_ids.discard(file_id)
                self._queued.discard(file_id)
                self._queue.task_done()
                continue

            # Mark work active before process_file's first database await so a
            # concurrent test request cannot duplicate a file already started.
            self._current_file_id = file_id
            try:
                if is_test:
                    previous_status = self._test_previous_status.get(file_id, "PENDING")
                    full_pipeline = file_id in self._full_test_ids
                    await self.process_file(
                        file_id,
                        test_only=not full_pipeline,
                        full_test=full_pipeline,
                        previous_status=previous_status,
                    )
                else:
                    await self.process_file(file_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled worker error for media row %s", file_id)
            finally:
                if is_test:
                    self._test_queue.task_done()
                    self._test_only_ids.discard(file_id)
                    self._full_test_ids.discard(file_id)
                    self._test_previous_status.pop(file_id, None)
                    if file_id not in self._skip_normal_ids:
                        self._queued.discard(file_id)
                else:
                    self._queue.task_done()
                    self._queued.discard(file_id)
                if self._current_file_id == file_id:
                    self._current_file_id = None
                    self._current_path = None
                    self._current_phase = "IDLE"
                    self._started_at = None
                    self.current_fps = 0.0
                    self.current_progress_percent = 0.0
                    self._current_duration = 0.0
                    self._current_progress_base = 0.0
                    self._current_progress_scale = 100.0
                    self.state = "IDLE"

    async def process_file(
        self,
        file_id: int,
        *,
        test_only: bool = False,
        full_test: bool = False,
        previous_status: str | None = None,
    ) -> str | None:
        row = await self.database.get_file(file_id)
        if row is None:
            return None

        file_path = row["file_path"]
        self._current_file_id = file_id
        self._current_path = file_path
        self._started_at = time.monotonic()
        self._current_duration = 0.0
        self._current_progress_base = 0.0
        self._current_progress_scale = 100.0
        self.current_fps = 0.0
        self.current_progress_percent = 0.0
        started = time.monotonic()
        temp_path: str | None = None
        try:
            if not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
                await self._finish(
                    file_id,
                    "CORRUPT",
                    "Source file is missing or empty.",
                    processing_time=time.monotonic() - started,
                )
                return "CORRUPT"

            source_stat = os.stat(file_path, follow_symlinks=False)
            original_size = source_stat.st_size
            await self._set_phase(file_id, "CHECKING")
            info, probe_error = await self._probe(file_path)
            if probe_error or info is None:
                detail = probe_error or "ffprobe returned no usable stream metadata."
                await self._finish(
                    file_id,
                    "CORRUPT",
                    f"Container/Header unreadable: {detail}",
                    processing_time=time.monotonic() - started,
                )
                return "CORRUPT"

            await self.database.update_file(file_id, **metadata_db_fields(info))
            codec = str(info.get("codec_name") or "").lower()
            self._current_duration = float(info.get("duration") or 0.0)
            if not codec:
                await self._finish(file_id, "CORRUPT", "Video stream has no codec_name.", processing_time=time.monotonic() - started)
                return "CORRUPT"

            if is_h264_10_bit(info):
                reason = "NVDEC does not support 10-bit H.264; software fallback is disabled."
                await self._finish(file_id, "SKIPPED_HW_UNSUPPORTED", reason, processing_time=time.monotonic() - started)
                return "SKIPPED_HW_UNSUPPORTED"
            if codec not in NVDEC_CODECS:
                reason = f"Codec {codec!r} is outside the NVDEC hardware whitelist; software fallback is disabled."
                await self._finish(file_id, "SKIPPED_HW_UNSUPPORTED", reason, processing_time=time.monotonic() - started)
                return "SKIPPED_HW_UNSUPPORTED"
            if not self.watchdog.available:
                reason = self.watchdog.error or "NVML/GPU device is unavailable; software fallback is disabled."
                await self._finish(file_id, "SKIPPED_HW_UNSUPPORTED", reason, processing_time=time.monotonic() - started)
                return "SKIPPED_HW_UNSUPPORTED"

            settings = await self.settings_store.load()
            if test_only:
                route = "health"
            elif full_test:
                route = "transcode" if codec in TRANSCODE_CODECS and int(info.get("bit_rate") or 0) > 0 else "health"
            else:
                route = route_media(info, settings)

            if full_test and route == "transcode":
                # The full test intentionally runs a standalone source health
                # check before the fused NVENC pass and final output decode.
                self._current_progress_base = 0.0
                self._current_progress_scale = 25.0
                self.current_progress_percent = 0.0
                source_health = await self._execute_ffmpeg(
                    build_decode_command(file_path, gpu_index=settings.gpu_index)
                )
                if source_health.returncode != 0:
                    detail = source_health.stderr or f"Source NVDEC health check exited with code {source_health.returncode}."
                    status = "SKIPPED_HW_UNSUPPORTED" if self._looks_like_hw_unsupported(detail) else "CORRUPT"
                    await self._finish(file_id, status, detail, processing_time=time.monotonic() - started)
                    return status
                self.current_progress_percent = 25.0
                # A full selected-file test keeps its source backup regardless
                # of the global cleanup policy so the user can inspect or roll
                # back the test replacement.
                settings = settings.model_copy(update={"operation_mode": "transcode", "backup_policy": "keep"})
            if route == "health":
                result = await self._execute_ffmpeg(build_decode_command(file_path, gpu_index=settings.gpu_index))
                elapsed = time.monotonic() - started
                if result.returncode == 0:
                    if full_test:
                        reason = (
                            "Full pipeline transcode test requires a known source video bitrate; NVDEC health check passed."
                            if codec in TRANSCODE_CODECS
                            else f"Full pipeline transcode test is not applicable to source codec {codec!r}; NVDEC health check passed."
                        )
                        await self._finish(file_id, "HEALTHY", reason, processing_time=elapsed)
                        return "HEALTHY"
                    if test_only and previous_status == "PENDING":
                        note = "Selected-file NVDEC test passed; file remains pending for the full queue."
                        await self._finish(file_id, "PENDING", note, processing_time=elapsed)
                        return "TESTED_PENDING"
                    status = (
                        previous_status
                        if test_only and previous_status in {"HEALTHY", "SKIPPED_OPTIMAL", "TRANSCODED"}
                        else "HEALTHY"
                    )
                    await self._finish(file_id, status, None, processing_time=elapsed)
                    return status
                status = "SKIPPED_HW_UNSUPPORTED" if self._looks_like_hw_unsupported(result.stderr) else "CORRUPT"
                reason = result.stderr or f"NVDEC health check exited with code {result.returncode}."
                await self._finish(file_id, status, reason, processing_time=elapsed)
                return status

            temp_path = output_temp_path(file_path)
            if os.path.lexists(temp_path):
                os.unlink(temp_path)
            await self._set_phase(
                file_id,
                "TRANSCODING",
                progress_base=25.0 if full_test else 0.0,
                progress_scale=50.0 if full_test else 70.0,
            )
            command = build_transcode_command(file_path, temp_path, info, settings)
            if not target_has_hwaccel(command):
                raise RuntimeError("Refusing to start an FFmpeg command without mandatory CUDA decode flags.")
            result = await self._execute_ffmpeg(command)
            if result.returncode != 0:
                self._remove_temp(temp_path)
                detail = result.stderr or f"FFmpeg exited with code {result.returncode}."
                if self._looks_like_hw_unsupported(detail):
                    status = "SKIPPED_HW_UNSUPPORTED"
                elif self._looks_like_source_corruption(detail):
                    status = "CORRUPT"
                else:
                    # The fused path can fail in either the decoder or NVENC.
                    # A source-only NVDEC pass distinguishes corrupt input from
                    # an encoder/output failure without ever using software.
                    source_check = await self._execute_ffmpeg(
                        build_decode_command(file_path, gpu_index=settings.gpu_index)
                    )
                    if source_check.returncode != 0:
                        if self._looks_like_hw_unsupported(source_check.stderr):
                            status = "SKIPPED_HW_UNSUPPORTED"
                        else:
                            status = "CORRUPT"
                        detail += "\nSource NVDEC check after transcode failure:\n" + (source_check.stderr or f"exit code {source_check.returncode}")
                    else:
                        status = "TRANSCODE_FAILED_RESTORED"
                await self._finish(file_id, status, detail, processing_time=time.monotonic() - started)
                return status

            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
                reason = "FFmpeg exited successfully but did not produce a non-empty temporary output."
                self._remove_temp(temp_path)
                await self._finish(file_id, "TRANSCODE_FAILED_RESTORED", reason, processing_time=time.monotonic() - started)
                return "TRANSCODE_FAILED_RESTORED"

            current_stat = os.stat(file_path, follow_symlinks=False)
            if current_stat.st_size != source_stat.st_size or current_stat.st_mtime != source_stat.st_mtime:
                reason = "Source file changed while FFmpeg was processing; replacement was not attempted."
                self._remove_temp(temp_path)
                await self._finish(file_id, "TRANSCODE_FAILED_RESTORED", reason, processing_time=time.monotonic() - started)
                return "TRANSCODE_FAILED_RESTORED"

            new_size = os.path.getsize(temp_path)
            if new_size >= original_size * 0.93 and not full_test:
                self._remove_temp(temp_path)
                await self._finish(
                    file_id,
                    "SKIPPED_OPTIMAL",
                    None,
                    processing_time=time.monotonic() - started,
                )
                return "SKIPPED_OPTIMAL"

            output_info, output_probe_error = await self._probe(temp_path)
            if output_probe_error or output_info is None:
                reason = f"Output metadata audit failed: {output_probe_error or 'no video stream found'}"
                self._remove_temp(temp_path)
                await self._finish(
                    file_id,
                    "TRANSCODE_FAILED_RESTORED",
                    reason,
                    processing_time=time.monotonic() - started,
                )
                return "TRANSCODE_FAILED_RESTORED"

            audit_error = self._audit_output(info, output_info, file_path)
            if audit_error:
                self._remove_temp(temp_path)
                await self._finish(
                    file_id,
                    "TRANSCODE_FAILED_RESTORED",
                    audit_error,
                    processing_time=time.monotonic() - started,
                )
                return "TRANSCODE_FAILED_RESTORED"

            await self._set_phase(
                file_id,
                "VERIFYING_OUTPUT",
                progress_base=75.0 if full_test else 70.0,
                progress_scale=25.0 if full_test else 30.0,
            )
            verification = await self._execute_ffmpeg(build_decode_command(temp_path, gpu_index=settings.gpu_index))
            if verification.returncode != 0:
                self._remove_temp(temp_path)
                reason = "Full NVDEC decode verification of the new file failed.\n" + (verification.stderr or "No FFmpeg stderr was produced.")
                await self._finish(
                    file_id,
                    "TRANSCODE_FAILED_RESTORED",
                    reason,
                    processing_time=time.monotonic() - started,
                )
                return "TRANSCODE_FAILED_RESTORED"

            if full_test and new_size >= original_size * 0.93:
                self._remove_temp(temp_path)
                reason = (
                    "Full pipeline test passed the output metadata and NVDEC checks, "
                    "but saved less than 7%; the original was left untouched and no backup swap was made."
                )
                await self._finish(
                    file_id,
                    "SKIPPED_OPTIMAL",
                    reason,
                    processing_time=time.monotonic() - started,
                )
                return "SKIPPED_OPTIMAL"

            status, _error = await self._transactional_swap(
                row=row,
                source_stat=source_stat,
                output_info=output_info,
                temp_path=temp_path,
                new_size=new_size,
                settings=settings,
            )
            return status
        except asyncio.CancelledError:
            self._remove_temp(temp_path)
            await self._recover_cancelled_file(file_id, file_path)
            raise
        except Exception as exc:
            logger.exception("Media processing failed for %s", file_path)
            self._remove_temp(temp_path)
            await self._recover_failed_file(file_id, file_path, str(exc), time.monotonic() - started)
            return "TRANSCODE_FAILED_RESTORED"
        finally:
            self._active_process = None

    async def _probe(self, file_path: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return None, f"Unable to execute ffprobe: {exc}"
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            return None, stderr.decode(errors="replace") or f"ffprobe exited with code {process.returncode}."
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"ffprobe returned invalid JSON: {exc}"
        info = parse_probe_data(payload)
        if info is None:
            return None, "No video stream was found."
        return info, None

    async def _execute_ffmpeg(self, command: list[str]) -> CommandResult:
        if not target_has_hwaccel(command):
            raise RuntimeError("Refusing to execute FFmpeg without CUDA decode and -xerror flags.")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active_process = process
        self.watchdog.register_child(process.pid)
        self._last_progress_at = None
        self._last_progress_frame = 0
        stdout_task = asyncio.create_task(self._consume_progress(process.stdout))
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            returncode = await process.wait()
            stdout_data, stderr_data = await asyncio.gather(stdout_task, stderr_task)
            del stdout_data
            return CommandResult(returncode, stderr_data.decode(errors="replace"))
        except asyncio.CancelledError:
            try:
                os.kill(process.pid, signal.SIGCONT)
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except ProcessLookupError:
                pass
            except TimeoutError:
                process.kill()
                await process.wait()
            except OSError:
                logger.warning("Unable to stop FFmpeg PID %s during shutdown", process.pid, exc_info=True)
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            self.watchdog.unregister_child(process.pid)
            if self._active_process is process:
                self._active_process = None

    async def _consume_progress(self, stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        captured = bytearray()
        while True:
            line = await stream.readline()
            if not line:
                break
            captured.extend(line)
            if line.startswith(b"out_time=") and self._current_duration > 0:
                elapsed = self._parse_ffmpeg_time(line.split(b"=", 1)[1].decode(errors="ignore").strip())
                if elapsed is not None:
                    stage_percent = min(100.0, max(0.0, elapsed / self._current_duration * 100.0))
                    overall = self._current_progress_base + stage_percent * self._current_progress_scale / 100.0
                    self.current_progress_percent = max(self.current_progress_percent, min(99.5, overall))
            if line.startswith(b"frame="):
                try:
                    frame = int(line.split(b"=", 1)[1].strip())
                except ValueError:
                    continue
                now = time.monotonic()
                if self._last_progress_at is not None and now > self._last_progress_at:
                    frames = max(0, frame - self._last_progress_frame)
                    self.current_fps = frames / (now - self._last_progress_at)
                self._last_progress_at = now
                self._last_progress_frame = frame
        return bytes(captured)

    @staticmethod
    def _parse_ffmpeg_time(value: str) -> float | None:
        try:
            hours, minutes, seconds = value.split(":", 2)
            parsed = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    async def _set_phase(
        self,
        file_id: int,
        phase: str,
        *,
        progress_base: float = 0.0,
        progress_scale: float = 100.0,
    ) -> None:
        self._current_phase = phase
        self._current_progress_base = progress_base
        self._current_progress_scale = progress_scale
        self.current_progress_percent = progress_base
        self.state = "VERIFYING_OUTPUT" if phase == "VERIFYING_OUTPUT" else "ACTIVE"
        await self.database.update_file(file_id, status=phase, error_log=None)
        if self._yielding:
            await self.database.update_file(file_id, status="PAUSED_YIELD")
            self.state = "PAUSED_YIELD"

    async def _finish(
        self,
        file_id: int,
        status: str,
        error: str | None,
        processing_time: float,
    ) -> None:
        await self.database.update_file(
            file_id,
            status=status,
            error_log=error,
            processing_time_sec=max(0.0, processing_time),
        )

    @staticmethod
    def _audit_output(source: dict[str, Any], output: dict[str, Any], file_path: str) -> str | None:
        if output.get("codec_name") != "hevc":
            return f"Output codec audit failed: expected 'hevc', received {output.get('codec_name')!r}."
        source_duration = source.get("duration")
        output_duration = output.get("duration")
        if source_duration is None or output_duration is None:
            return "Output duration audit failed: source or output duration is unavailable."
        if abs(float(output_duration) - float(source_duration)) > 0.5:
            return (
                "Output duration audit failed: "
                f"source={source_duration}s, output={output_duration}s (tolerance 0.5s)."
            )
        if int(output.get("audio_streams") or 0) != int(source.get("audio_streams") or 0):
            return (
                "Output audio stream audit failed: "
                f"source={source.get('audio_streams')}, output={output.get('audio_streams')}."
            )
        if Path(file_path).suffix.lower() == ".mkv" and int(output.get("sub_streams") or 0) != int(source.get("sub_streams") or 0):
            return (
                "Output subtitle stream audit failed: "
                f"source={source.get('sub_streams')}, output={output.get('sub_streams')}."
            )
        return None

    async def _transactional_swap(
        self,
        row: dict[str, Any],
        source_stat: os.stat_result,
        output_info: dict[str, Any],
        temp_path: str,
        new_size: int,
        settings: Settings,
    ) -> tuple[str, str | None]:
        file_path = row["file_path"]
        backup_path = file_path + ".gpuforge_bak"
        destination_path = final_output_path(file_path)
        directory = os.path.dirname(file_path) or "."
        source_now = os.stat(file_path, follow_symlinks=False)
        if source_now.st_size != source_stat.st_size or source_now.st_mtime != source_stat.st_mtime:
            self._remove_temp(temp_path)
            error = "Source fingerprint changed before atomic replacement; original was left untouched."
            await self._finish(row["id"], "TRANSCODE_FAILED_RESTORED", error, 0)
            return "TRANSCODE_FAILED_RESTORED", error
        if os.path.lexists(backup_path):
            self._remove_temp(temp_path)
            error = f"A verified backup already exists at {backup_path}; refusing to overwrite it."
            await self._finish(row["id"], "TRANSCODE_FAILED_RESTORED", error, 0)
            return "TRANSCODE_FAILED_RESTORED", error
        if destination_path != file_path:
            if os.path.lexists(destination_path):
                self._remove_temp(temp_path)
                error = f"Matroska destination already exists; refusing to overwrite {destination_path}."
                await self._finish(row["id"], "TRANSCODE_FAILED_RESTORED", error, 0)
                return "TRANSCODE_FAILED_RESTORED", error
            destination_row = await self.database.get_by_path(destination_path)
            if destination_row and destination_row["id"] != row["id"]:
                self._remove_temp(temp_path)
                error = f"Matroska destination is already tracked by another library row: {destination_path}."
                await self._finish(row["id"], "TRANSCODE_FAILED_RESTORED", error, 0)
                return "TRANSCODE_FAILED_RESTORED", error

        try:
            os.chown(temp_path, source_stat.st_uid, source_stat.st_gid, follow_symlinks=False)
            os.chmod(temp_path, stat.S_IMODE(source_stat.st_mode), follow_symlinks=False)
            self._fsync_file(temp_path)
        except OSError as exc:
            self._remove_temp(temp_path)
            error = f"Could not copy source ownership/permissions to verified output: {exc}"
            await self._finish(row["id"], "TRANSCODE_FAILED_RESTORED", error, 0)
            return "TRANSCODE_FAILED_RESTORED", error

        await self.database.update_file(row["id"], backup_path=backup_path, status="TRANSCODING", error_log=None)
        backup_created = False
        committed = False
        try:
            os.replace(file_path, backup_path)
            backup_created = True
            self._fsync_directory(directory)
            os.replace(temp_path, destination_path)
            self._fsync_directory(directory)
            if not os.path.exists(destination_path) or os.path.getsize(destination_path) != new_size:
                raise OSError("replacement file size does not match the verified temporary output")

            replacement_stat = os.stat(destination_path, follow_symlinks=False)
            terminal_status = "TRANSCODED_BACKUP_KEPT" if settings.backup_policy == "keep" else "TRANSCODED"
            fields = metadata_db_fields(output_info)
            fields.update(
                {
                    "file_path": destination_path,
                    "file_name": os.path.basename(destination_path),
                    "directory": os.path.dirname(destination_path) or ".",
                    "size_bytes": source_stat.st_size,
                    "new_size_bytes": new_size,
                    "mtime": replacement_stat.st_mtime,
                    "backup_path": backup_path,
                    "status": terminal_status,
                    "error_log": None,
                    "processing_time_sec": max(0.0, time.monotonic() - (self._started_at or time.monotonic())),
                }
            )
            await self.database.update_file(row["id"], **fields)
            committed = True

            if settings.backup_policy == "auto_delete":
                try:
                    os.unlink(backup_path)
                except OSError as exc:
                    if os.path.exists(backup_path):
                        # The replacement has already passed every gate. Keep
                        # and expose the backup if cleanup cannot run.
                        await self.database.update_file(
                            row["id"],
                            status="TRANSCODED_BACKUP_KEPT",
                            error_log=f"Automatic backup cleanup failed; verified backup retained: {exc}",
                        )
                        return "TRANSCODED_BACKUP_KEPT", str(exc)
                    await self.database.update_file(row["id"], backup_path=None)
                else:
                    try:
                        self._fsync_directory(directory)
                    except OSError:
                        logger.warning("Could not sync backup cleanup for %s", backup_path, exc_info=True)
                    try:
                        await self.database.update_file(row["id"], backup_path=None)
                    except Exception:
                        # The verified replacement is committed and the
                        # backup has already been removed. A stale DB pointer
                        # is safe and can be cleared by later maintenance.
                        logger.exception("Could not clear purged backup path for %s", file_path)
            return terminal_status, None
        except asyncio.CancelledError:
            self._remove_temp(temp_path)
            if backup_created and os.path.exists(backup_path):
                try:
                    if destination_path != file_path:
                        self._remove_temp(destination_path)
                    os.replace(backup_path, file_path)
                    self._fsync_directory(directory)
                    await self.database.update_file(
                        row["id"],
                        file_path=file_path,
                        file_name=os.path.basename(file_path),
                        directory=os.path.dirname(file_path) or ".",
                        backup_path=None,
                        size_bytes=source_stat.st_size,
                        new_size_bytes=None,
                        mtime=source_stat.st_mtime,
                        status="TRANSCODE_FAILED_RESTORED",
                        error_log="Worker shutdown interrupted the replacement; original restored.",
                    )
                except OSError:
                    logger.exception("Could not roll back %s during worker shutdown", file_path)
            raise
        except Exception as exc:
            if committed:
                # File replacement and its terminal database state were both
                # committed. Do not roll back a verified output for a later
                # cleanup bookkeeping error.
                logger.exception("Post-commit cleanup failed for %s", file_path)
                return terminal_status, str(exc)
            self._remove_temp(temp_path)
            error = f"Transactional backup-and-swap failed: {exc}"
            if backup_created and os.path.exists(backup_path):
                try:
                    if destination_path != file_path:
                        self._remove_temp(destination_path)
                    os.replace(backup_path, file_path)
                    self._fsync_directory(directory)
                    original_stat = os.stat(file_path, follow_symlinks=False)
                    await self.database.update_file(
                        row["id"],
                        file_path=file_path,
                        file_name=os.path.basename(file_path),
                        directory=os.path.dirname(file_path) or ".",
                        backup_path=None,
                        size_bytes=original_stat.st_size,
                        new_size_bytes=None,
                        mtime=original_stat.st_mtime,
                        status="TRANSCODE_FAILED_RESTORED",
                        error_log=error,
                    )
                except Exception as rollback_error:
                    error += f"\nImmediate rollback failed; startup recovery will retry: {rollback_error}"
                    logger.exception("Rollback failed for %s", file_path)
                    try:
                        await self.database.update_file(
                            row["id"],
                            backup_path=backup_path,
                            status="TRANSCODING",
                            error_log=error,
                        )
                    except Exception:
                        logger.exception("Unable to persist pending rollback for %s", file_path)
            else:
                try:
                    await self.database.update_file(
                        row["id"],
                        backup_path=None,
                        status="TRANSCODE_FAILED_RESTORED",
                        error_log=error,
                    )
                except Exception:
                    logger.exception("Unable to persist failed swap for %s", file_path)
            return "TRANSCODE_FAILED_RESTORED", error

    async def _recover_cancelled_file(self, file_id: int, file_path: str) -> None:
        row = await self.database.get_file(file_id)
        if row is None or row.get("status") in {"TRANSCODED", "TRANSCODED_BACKUP_KEPT"}:
            return
        backup_path = file_path + ".gpuforge_bak"
        if os.path.exists(backup_path):
            try:
                os.replace(backup_path, file_path)
                stat_result = os.stat(file_path, follow_symlinks=False)
                await self.database.update_file(
                    file_id,
                    backup_path=None,
                    size_bytes=stat_result.st_size,
                    new_size_bytes=None,
                    mtime=stat_result.st_mtime,
                    status="TRANSCODE_FAILED_RESTORED",
                    error_log="Worker shutdown interrupted the replacement; original restored.",
                )
                return
            except OSError:
                logger.exception("Unable to restore %s after cancellation", file_path)
                return
        if os.path.isfile(file_path) and os.path.getsize(file_path) > 0 and row.get("status") in ACTIVE_STATUSES:
            await self.database.update_file(file_id, status="PENDING", error_log=None, new_size_bytes=None)

    async def _recover_failed_file(self, file_id: int, file_path: str, error: str, elapsed: float) -> None:
        row = await self.database.get_file(file_id)
        if row is not None and row.get("status") in {"TRANSCODED", "TRANSCODED_BACKUP_KEPT"}:
            return
        backup_path = file_path + ".gpuforge_bak"
        if os.path.exists(backup_path):
            try:
                os.replace(backup_path, file_path)
                stat_result = os.stat(file_path, follow_symlinks=False)
                await self._finish(
                    file_id,
                    "TRANSCODE_FAILED_RESTORED",
                    f"{error}\nOriginal restored from .gpuforge_bak.",
                    elapsed,
                )
                await self.database.update_file(
                    file_id,
                    backup_path=None,
                    size_bytes=stat_result.st_size,
                    new_size_bytes=None,
                    mtime=stat_result.st_mtime,
                )
                return
            except OSError:
                logger.exception("Unable to restore source after processing failure: %s", file_path)
                await self.database.update_file(file_id, backup_path=backup_path, status="TRANSCODING", error_log=error)
                return
        await self._finish(file_id, "TRANSCODE_FAILED_RESTORED", error, elapsed)

    @staticmethod
    def _looks_like_hw_unsupported(message: str) -> bool:
        lower = message.lower()
        return any(
            marker in lower
            for marker in (
                "no capable devices found",
                "cannot load libcuda",
                "device type cuda needed",
                "invalid cuda device",
                "cannot init cuda",
                "failed to create cuda",
                "failed to initialise cuda",
                "failed to initialize cuda",
                "failed to create device context",
                "failed to initialize hardware device",
                "failed to initialise hardware device",
                "operation not supported",
                "unsupported device",
                "unsupported profile",
                "unsupported bit depth",
                "no suitable device",
                "no cuda-capable device",
                "nvenc is not available",
                "cannot load libnvidia-encode",
                "unknown encoder 'hevc_nvenc'",
                "no nvenc capable devices",
            )
        )

    @staticmethod
    def _looks_like_source_corruption(message: str) -> bool:
        lower = message.lower()
        return any(
            marker in lower
            for marker in (
                "invalid data found when processing input",
                "corrupt",
                "error while decoding",
                "decoding error",
                "invalid nal",
                "packet corrupt",
                "could not find codec parameters",
            )
        )

    @staticmethod
    def _remove_temp(temp_path: str | None) -> None:
        if not temp_path:
            return
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Unable to remove temporary output %s", temp_path, exc_info=True)

    @staticmethod
    def _fsync_file(file_path: str) -> None:
        descriptor = os.open(file_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
