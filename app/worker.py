"""GPU-only media health checks and transactional H.265 transcoding."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog
from app.probe import metadata_db_fields, parse_probe_data, probe_file
from app.routing import (
    TRANSCODE_CODECS,
    UPSCALE_MAX_HEIGHT,
    UPSCALE_MAX_WIDTH,
    UPSCALE_TARGET_HEIGHTS,
    efficiency_floor,
    recommend_action,
    route_media,
)
from app.upscale_client import UpscaleForgeClient, UpscaleServiceUnavailable

logger = logging.getLogger(__name__)

NVDEC_CODECS = {"h264", "hevc", "mpeg2video", "mpeg4", "vc1", "vp8", "vp9", "av1"}
ACTIVE_STATUSES = {
    "CHECKING",
    "TRANSCODING",
    "VERIFYING_OUTPUT",
    "AUDIO_REMUXING",
    "AUDIO_VERIFYING",
    "AUDIO_TRANSCODING",
    "AUDIO_RESTORING",
    "PAUSED_YIELD",
}
AUDIO_FALLBACK_TITLE = "GPU-Forge AAC Compatibility"
AUDIO_FALLBACK_BITRATE = "192k"
AUDIO_FALLBACK_SAMPLE_RATE = 48_000


@dataclass
class CommandResult:
    returncode: int
    stderr: str


@dataclass(frozen=True)
class AudioPlan:
    action: str
    source_ordinal: int | None = None
    source_index: int | None = None
    fallback_ordinal: int | None = None


def _audio_profile_is_lc(stream: dict[str, Any]) -> bool:
    profile = str(stream.get("profile") or "").strip().casefold()
    if not profile:
        return True
    return profile in {"lc", "aac lc", "aac-lc", "low complexity", "aac low"}


def _audio_stream_is_compatible(stream: dict[str, Any]) -> bool:
    channels = stream.get("channels")
    sample_rate = stream.get("sample_rate")
    return bool(
        str(stream.get("codec_name") or "").casefold() == "aac"
        and _audio_profile_is_lc(stream)
        and channels is not None
        and int(channels) <= 2
        and (sample_rate is None or int(sample_rate) <= AUDIO_FALLBACK_SAMPLE_RATE)
    )


def plan_audio_fallback(info: dict[str, Any]) -> AudioPlan:
    """Choose one default audio stream and decide whether it needs an AAC copy."""

    streams = [
        stream for stream in info.get("audio_stream_details", []) if isinstance(stream, dict)
    ]
    if not streams:
        return AudioPlan("no_audio")

    for ordinal, stream in enumerate(streams):
        title = str(stream.get("title") or "").casefold()
        if AUDIO_FALLBACK_TITLE.casefold() in title and str(stream.get("codec_name") or "").casefold() == "aac":
            defaults = [
                i for i, candidate in enumerate(streams)
                if bool((candidate.get("disposition") or {}).get("default"))
            ]
            action = "already_added" if defaults == [ordinal] else "normalize_default"
            return AudioPlan(
                action,
                source_ordinal=ordinal,
                source_index=stream.get("index"),
                fallback_ordinal=ordinal,
            )

    primary_ordinal = next(
        (
            ordinal
            for ordinal, stream in enumerate(streams)
            if bool((stream.get("disposition") or {}).get("default"))
        ),
        0,
    )
    primary = streams[primary_ordinal]
    if _audio_stream_is_compatible(primary):
        return AudioPlan(
            "compatible",
            source_ordinal=primary_ordinal,
            source_index=primary.get("index"),
        )
    return AudioPlan(
        "add_fallback",
        source_ordinal=primary_ordinal,
        source_index=primary.get("index"),
        fallback_ordinal=len(streams),
    )


def _disposition_value(stream: dict[str, Any], *, default: bool) -> str:
    disposition = stream.get("disposition") or {}
    flags = [
        str(name)
        for name, enabled in disposition.items()
        if enabled and str(name).casefold() != "default"
    ]
    if default:
        flags.append("default")
    return "+".join(flags) if flags else "0"


def build_audio_remux_command(
    file_path: str,
    temp_path: str,
    info: dict[str, Any],
    plan: AudioPlan,
) -> list[str]:
    """Copy video and source audio, adding or normalizing the single AAC fallback."""

    if plan.action not in {"add_fallback", "normalize_default"}:
        raise ValueError(f"Audio remux is not required for plan {plan.action!r}.")
    streams = [
        stream for stream in info.get("audio_stream_details", []) if isinstance(stream, dict)
    ]
    if not streams or plan.source_ordinal is None:
        raise ValueError("An audio remux requires a selected source audio stream.")

    extension = Path(file_path).suffix.lower()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-xerror",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        file_path,
        "-map",
        "0:v?",
        "-map",
        "0:a?",
    ]
    if plan.action == "add_fallback":
        command.extend(["-map", f"0:a:{plan.source_ordinal}"])

    if extension in {".mp4", ".m4v"}:
        for subtitle in info.get("subtitle_streams", []):
            if subtitle.get("codec_name") == "mov_text" and subtitle.get("index") is not None:
                command.extend(["-map", f"0:{subtitle['index']}"])
        extra_container_flags = ["-movflags", "+faststart"]
    else:
        command.extend(["-map", "0:s?", "-map", "0:t?"])
        extra_container_flags = ["-c:t", "copy"]

    command.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"])
    for ordinal, stream in enumerate(streams):
        command.extend(
            ["-disposition:a:" + str(ordinal), _disposition_value(stream, default=False)]
        )

    if plan.action == "add_fallback":
        assert plan.fallback_ordinal is not None
        ordinal = plan.fallback_ordinal
        source = streams[plan.source_ordinal]
        command.extend(
            [
                f"-c:a:{ordinal}",
                "aac",
                f"-profile:a:{ordinal}",
                "aac_low",
                f"-ac:a:{ordinal}",
                "2",
                f"-ar:a:{ordinal}",
                str(AUDIO_FALLBACK_SAMPLE_RATE),
                f"-b:a:{ordinal}",
                AUDIO_FALLBACK_BITRATE,
                f"-metadata:s:a:{ordinal}",
                f"title={AUDIO_FALLBACK_TITLE}",
                f"-metadata:s:a:{ordinal}",
                f"handler_name={AUDIO_FALLBACK_TITLE}",
                f"-disposition:a:{ordinal}",
                _disposition_value(source, default=True),
            ]
        )
        language = source.get("language")
        if language:
            command.extend([f"-metadata:s:a:{ordinal}", f"language={language}"])
    else:
        assert plan.fallback_ordinal is not None
        ordinal = plan.fallback_ordinal
        source = streams[ordinal]
        command.extend(
            [
                f"-metadata:s:a:{ordinal}",
                f"title={AUDIO_FALLBACK_TITLE}",
                f"-metadata:s:a:{ordinal}",
                f"handler_name={AUDIO_FALLBACK_TITLE}",
                f"-disposition:a:{ordinal}",
                _disposition_value(source, default=True),
            ]
        )
        language = source.get("language")
        if language:
            command.extend([f"-metadata:s:a:{ordinal}", f"language={language}"])

    command.extend([*extra_container_flags, "-max_muxing_queue_size", "4096", temp_path])
    return command


def build_audio_decode_command(file_path: str) -> list[str]:
    """Decode all audio streams without starting a video decoder."""

    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        file_path,
        "-map",
        "0:a?",
        "-vn",
        "-c:a",
        "pcm_s16le",
        "-f",
        "null",
        "-",
    ]


def _safe_without_video_decode(command: list[str]) -> bool:
    if "-xerror" not in command:
        return False
    if "-vn" in command:
        return True
    if "-c:v" in command:
        index = command.index("-c:v")
        return index + 1 < len(command) and command[index + 1] == "copy"
    return False


def is_h264_10_bit(info: dict[str, Any]) -> bool:
    if str(info.get("codec_name") or "").lower() != "h264":
        return False
    pixel_format = str(info.get("pix_fmt") or "").lower()
    return "10le" in pixel_format or "10be" in pixel_format


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
        self._upscale_queue: asyncio.Queue[int] = asyncio.Queue()
        self._bulk_upscale_queue: asyncio.Queue[int] = asyncio.Queue()
        self._audio_queue: asyncio.Queue[int] = asyncio.Queue()
        self._work_available = asyncio.Event()
        self._queued: set[int] = set()
        self._audio_queued: set[int] = set()
        self._upscale_ids: set[int] = set()
        self._bulk_upscale_ids: set[int] = set()
        self._upscale_cancel_requested: set[int] = set()
        self._upscale_cancel_sent: set[int] = set()
        self._upscale_cancelled_ids: set[int] = set()
        self._skip_normal_ids: set[int] = set()
        self._test_only_ids: set[int] = set()
        self._full_test_ids: set[int] = set()
        self._test_previous_status: dict[int, str] = {}
        self.queue_enabled = False
        self.bulk_upscale_queue_enabled = False
        self._bulk_upscale_target_height = 1080
        self._bulk_upscale_deferred: dict[int, int] = {}
        self.audio_queue_enabled = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._active_process: asyncio.subprocess.Process | None = None
        self._yield_state_lock = asyncio.Lock()
        self.upscale_client = UpscaleForgeClient()
        self._current_file_id: int | None = None
        self._current_task_kind: str | None = None
        self._current_path: str | None = None
        self._current_phase = "IDLE"
        self._current_detail: str | None = None
        self._current_target_height: int | None = None
        self._current_phase_elapsed_sec = 0.0
        self._current_wait_reason: str | None = None
        self._yielding = False
        self._yield_reason: str | None = None
        self._started_at: float | None = None
        self._current_duration = 0.0
        self._current_progress_base = 0.0
        self._current_progress_scale = 100.0
        self._last_progress_at: float | None = None
        self._last_progress_frame = 0
        self._last_upscale_frames: float | None = None
        self._last_upscale_progress_at: float | None = None
        self.current_fps = 0.0
        self.current_progress_percent = 0.0
        self.state = "IDLE"

    async def start(self) -> None:
        self._stopping = False
        for row in await self.database.upscale_work_files():
            file_id = int(row["id"])
            if file_id not in self._upscale_ids:
                self._upscale_ids.add(file_id)
                if row.get("upscale_queue_kind") == "bulk" and row.get("status") != "UPSCALE_CANCEL_REQUESTED":
                    self._bulk_upscale_ids.add(file_id)
                    self._bulk_upscale_queue.put_nowait(file_id)
                else:
                    self._upscale_queue.put_nowait(file_id)
        if not self._upscale_queue.empty():
            self._work_available.set()
        if not self._bulk_upscale_queue.empty():
            self._work_available.set()
        self._task = asyncio.create_task(self._run_queue(), name="gpu-forge-worker")
        logger.info(
            "GPU-Forge media worker started",
            extra={
                "recovered_manual_upscales": self._upscale_queue.qsize(),
                "recovered_bulk_upscales": self._bulk_upscale_queue.qsize(),
                "bulk_queue_enabled": self.queue_enabled,
            },
        )

    async def start_queue(self) -> int:
        self.queue_enabled = True
        self._work_available.set()
        enqueued = await self.enqueue_pending()
        logger.info("GPU-Forge bulk queue started", extra={"newly_queued": enqueued})
        return enqueued

    async def start_audio_queue(self) -> int:
        self.audio_queue_enabled = True
        enqueued = await self.enqueue_pending_audio()
        self._work_available.set()
        logger.info("GPU-Forge audio compatibility queue started", extra={"newly_queued": enqueued})
        return enqueued

    async def start_bulk_upscale_queue(self, target_height: int = 1080) -> int:
        if target_height not in UPSCALE_TARGET_HEIGHTS:
            raise ValueError(f"Target resolution must be one of {UPSCALE_TARGET_HEIGHTS}p")
        candidates = await self.database.upscale_candidates()
        if (candidates or not self._bulk_upscale_queue.empty()) and not await self.upscale_service_online():
            raise UpscaleServiceUnavailable(
                "Upscale-Forge is not running or its shared state database is unavailable."
            )
        self._bulk_upscale_target_height = target_height
        enqueued = await self.enqueue_bulk_upscale_candidates(target_height, candidates=candidates)
        self.bulk_upscale_queue_enabled = True
        self._work_available.set()
        logger.info(
            "GPU-Forge bulk upscale queue started",
            extra={"target_height": target_height, "newly_queued": enqueued},
        )
        return self._bulk_upscale_queue.qsize() + len(self._bulk_upscale_deferred)

    def pause_queue(self) -> None:
        # A currently running file finishes safely; the remaining queue waits.
        self.queue_enabled = False
        logger.info("GPU-Forge bulk queue paused", extra={"active_file_id": self._current_file_id})

    def pause_audio_queue(self) -> None:
        self.audio_queue_enabled = False
        logger.info(
            "GPU-Forge audio compatibility queue paused",
            extra={"active_file_id": self._current_file_id},
        )

    def pause_bulk_upscale_queue(self) -> None:
        self.bulk_upscale_queue_enabled = False
        logger.info(
            "GPU-Forge bulk upscale queue paused",
            extra={"active_file_id": self._current_file_id},
        )

    async def stop(self) -> None:
        self._stopping = True
        logger.info("GPU-Forge media worker stopping", extra={"active_file_id": self._current_file_id})
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
        active_ids = {self._current_file_id} if self._current_file_id is not None else set()
        await self.database.requeue_stale_yield_pauses(active_ids)
        settings = await self.settings_store.load()
        if settings.operation_mode == "transcode":
            await self.database.requeue_transcode_candidates(active_ids)
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

    async def enqueue_pending_audio(self) -> int:
        if not self.audio_queue_enabled:
            return 0
        added = 0
        for row in await self.database.pending_audio_files():
            file_id = int(row["id"])
            if file_id in self._audio_queued:
                continue
            self._audio_queued.add(file_id)
            self._audio_queue.put_nowait(file_id)
            added += 1
        if added:
            self._work_available.set()
        return added

    async def enqueue_test(self, file_id: int, *, full_pipeline: bool = False) -> bool:
        """Prioritize one selected file for a health-only or full pipeline test."""

        if (
            self._current_file_id == file_id
            or file_id in self._test_only_ids
            or file_id in self._upscale_ids
        ):
            return False
        row = await self.database.get_file(file_id)
        if row is None or (row.get("backup_path") and os.path.exists(row["backup_path"])):
            return False
        # The active ID is set before the worker's first await, but check again
        # after the DB read to close the selection/start race.
        if (
            self._current_file_id == file_id
            or file_id in self._test_only_ids
            or file_id in self._upscale_ids
        ):
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

    async def upscale_service_online(self) -> bool:
        return await asyncio.to_thread(self.upscale_client.is_online)

    async def enqueue_bulk_upscale_candidates(
        self,
        target_height: int | None = None,
        *,
        candidates: list[dict[str, Any]] | None = None,
    ) -> int:
        """Queue eligible low-resolution candidates in the user-started bulk upscale queue."""

        target = self._bulk_upscale_target_height if target_height is None else int(target_height)
        if target not in UPSCALE_TARGET_HEIGHTS:
            raise ValueError(f"Target resolution must be one of {UPSCALE_TARGET_HEIGHTS}p")
        rows = candidates if candidates is not None else await self.database.upscale_candidates()
        added = 0
        for row in rows:
            file_id = int(row["id"])
            if self._current_file_id == file_id:
                self._bulk_upscale_deferred[file_id] = target
                continue
            if file_id in self._upscale_ids or row.get("action_tag") != "UPSCALE":
                continue
            if row.get("status") in {
                "UPSCALE_QUEUED",
                "UPSCALING",
                "UPSCALE_CANCEL_REQUESTED",
                "UPSCALED",
                "UPSCALED_BACKUP_KEPT",
            }:
                continue
            if row.get("backup_path") and os.path.exists(row["backup_path"]):
                continue
            if (
                not row.get("width")
                or not row.get("height")
                or int(row["width"]) > UPSCALE_MAX_WIDTH
                or int(row["height"]) > UPSCALE_MAX_HEIGHT
            ):
                continue
            try:
                source_stat = os.stat(row["file_path"], follow_symlinks=False)
            except OSError:
                continue
            expected_size = (
                row["new_size_bytes"] if row["new_size_bytes"] is not None else row["size_bytes"]
            )
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_size != expected_size
                or float(source_stat.st_mtime) != float(row["mtime"])
            ):
                continue

            was_video_queued = file_id in self._queued
            if was_video_queued:
                self._skip_normal_ids.add(file_id)
            self._upscale_ids.add(file_id)
            self._bulk_upscale_ids.add(file_id)
            try:
                await self.database.update_file(
                    file_id,
                    status="UPSCALE_QUEUED",
                    upscale_request_id=None,
                    upscale_queue_kind="bulk",
                    upscale_target_height=target,
                    error_log=None,
                )
            except Exception:
                self._upscale_ids.discard(file_id)
                self._bulk_upscale_ids.discard(file_id)
                if was_video_queued:
                    self._skip_normal_ids.discard(file_id)
                raise
            self._bulk_upscale_queue.put_nowait(file_id)
            added += 1
        if added:
            self._work_available.set()
        return added

    async def enqueue_upscale(self, file_id: int, target_height: int = 1080) -> bool:
        if target_height not in UPSCALE_TARGET_HEIGHTS:
            raise ValueError(f"Target resolution must be one of {UPSCALE_TARGET_HEIGHTS}p")
        if (
            self._current_file_id == file_id
            or file_id in self._test_only_ids
            or file_id in self._upscale_ids
        ):
            return False
        row = await self.database.get_file(file_id)
        if row is None:
            return False
        if row.get("backup_path") and os.path.exists(row["backup_path"]):
            return False
        if row.get("action_tag") != "UPSCALE":
            return False
        if (
            not row.get("width")
            or not row.get("height")
            or int(row["width"]) > UPSCALE_MAX_WIDTH
            or int(row["height"]) > UPSCALE_MAX_HEIGHT
        ):
            return False
        if row.get("status") in {
            "UPSCALE_QUEUED",
            "UPSCALING",
            "UPSCALE_CANCEL_REQUESTED",
            "UPSCALED",
            "UPSCALED_BACKUP_KEPT",
        }:
            return False
        try:
            source_stat = os.stat(row["file_path"], follow_symlinks=False)
        except OSError:
            return False
        expected_size = (
            row["new_size_bytes"] if row["new_size_bytes"] is not None else row["size_bytes"]
        )
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != expected_size:
            return False
        if float(source_stat.st_mtime) != float(row["mtime"]):
            return False
        if not await self.upscale_service_online():
            raise UpscaleServiceUnavailable(
                "Upscale-Forge is not running or its shared state database is unavailable."
            )

        was_queued = file_id in self._queued
        if was_queued:
            self._skip_normal_ids.add(file_id)
        self._upscale_ids.add(file_id)
        try:
            await self.database.update_file(
                file_id,
                status="UPSCALE_QUEUED",
                upscale_request_id=None,
                upscale_queue_kind="manual",
                upscale_target_height=target_height,
                error_log=None,
            )
        except Exception:
            self._upscale_ids.discard(file_id)
            if was_queued:
                self._skip_normal_ids.discard(file_id)
            raise
        self._upscale_queue.put_nowait(file_id)
        self._work_available.set()
        logger.info(
            "Manual upscale queued in GPU-Forge",
            extra={
                "file_id": file_id,
                "file_path": row["file_path"],
                "target_height": target_height,
                "priority_position": self.upscale_queue_position(file_id),
                "bulk_queue_enabled": self.queue_enabled,
            },
        )
        return True

    async def cancel_upscale(self, file_id: int) -> dict[str, Any]:
        row = await self.database.get_file(file_id)
        if row is None:
            raise ValueError("Media file not found")
        status = str(row.get("status") or "")
        cancellable = {"UPSCALE_QUEUED", "UPSCALING", "UPSCALE_CANCEL_REQUESTED"}
        logger.info(
            "Upscale cancellation requested",
            extra={
                "file_id": file_id,
                "file_path": row["file_path"],
                "request_id": row.get("upscale_request_id"),
                "status": status,
            },
        )
        if status == "UPSCALE_CANCELLED":
            return {"status": status, "request_id": row.get("upscale_request_id")}
        if status not in cancellable:
            raise ValueError("This upscale is no longer queued or running")

        request_id = row.get("upscale_request_id")
        is_current = self._current_file_id == file_id
        file_path = str(row["file_path"])
        backup_path = file_path + ".gpuforge_bak"
        if request_id is None and status == "UPSCALE_CANCEL_REQUESTED" and not is_current:
            try:
                source_stat = os.stat(file_path, follow_symlinks=False)
            except OSError:
                source_stat = None
            if source_stat is not None:
                active_job = await asyncio.to_thread(
                    self.upscale_client.find_active_job,
                    device_id=source_stat.st_dev,
                    inode=source_stat.st_ino,
                    size_bytes=source_stat.st_size,
                    mtime_ns=source_stat.st_mtime_ns,
                )
                if active_job is not None:
                    request_id = int(active_job["request_id"])
                    await self.database.update_file(file_id, upscale_request_id=request_id)
        if request_id is None:
            if is_current:
                self._upscale_cancel_requested.add(file_id)
                self._current_phase = "CANCELLING_UPSCALE"
                self.state = "CANCELLING_UPSCALE"
                await self.database.update_file(
                    file_id,
                    status="UPSCALE_CANCEL_REQUESTED",
                    error_log=None,
                )
                return {"status": "UPSCALE_CANCEL_REQUESTED", "request_id": None}
            self._remove_queued_upscale(file_id)
            self._upscale_ids.discard(file_id)
            self._bulk_upscale_ids.discard(file_id)
            self._upscale_cancel_requested.discard(file_id)
            self._upscale_cancel_sent.discard(file_id)
            self._release_cancelled_queue(file_id)
            await self.database.update_file(
                file_id,
                status="UPSCALE_CANCELLED",
                backup_path=backup_path if os.path.exists(backup_path) else None,
                error_log=None,
            )
            logger.info("Queued manual upscale cancelled before submission", extra={"file_id": file_id})
            return {"status": "UPSCALE_CANCELLED", "request_id": None}

        job = await asyncio.to_thread(self.upscale_client.cancel_job, int(request_id))
        if job is None:
            raise ValueError(f"Upscale-Forge no longer has request {request_id}")
        remote_status = str(job.get("status") or "")
        if remote_status not in {"CANCELLED", "CANCEL_REQUESTED"}:
            raise ValueError("The upscale has already completed or failed and can no longer be cancelled")

        self._upscale_cancel_requested.add(file_id)
        self._upscale_cancel_sent.add(file_id)
        if remote_status == "CANCELLED":
            await self.database.update_file(
                file_id,
                status="UPSCALE_CANCELLED",
                backup_path=backup_path if os.path.exists(backup_path) else None,
                error_log=None,
            )
            if not is_current:
                self._remove_queued_upscale(file_id)
                self._upscale_ids.discard(file_id)
                self._bulk_upscale_ids.discard(file_id)
                self._upscale_cancel_requested.discard(file_id)
                self._upscale_cancel_sent.discard(file_id)
                self._release_cancelled_queue(file_id)
            else:
                self._upscale_cancelled_ids.add(file_id)
            logger.info(
                "Upscale-Forge cancelled the manual upscale immediately",
                extra={"file_id": file_id, "request_id": int(request_id)},
            )
            return {"status": "UPSCALE_CANCELLED", "request_id": int(request_id)}

        await self.database.update_file(
            file_id,
            status="UPSCALE_CANCEL_REQUESTED",
            backup_path=row.get("backup_path") or backup_path,
            upscale_queue_kind="manual" if not is_current else row.get("upscale_queue_kind", "manual"),
            error_log=None,
        )
        if is_current:
            self._current_phase = "CANCELLING_UPSCALE"
            self.state = "CANCELLING_UPSCALE"
            self._current_detail = "Upscale-Forge is stopping the active segment safely."
        logger.info(
            "Upscale-Forge accepted manual upscale cancellation request",
            extra={"file_id": file_id, "request_id": int(request_id), "active": is_current},
        )
        if not is_current:
            was_bulk = file_id in self._bulk_upscale_ids
            if was_bulk:
                self._remove_queued_upscale(file_id)
                self._bulk_upscale_ids.discard(file_id)
            if file_id not in self._upscale_ids or was_bulk:
                self._upscale_ids.add(file_id)
                self._upscale_queue.put_nowait(file_id)
                self._work_available.set()
        return {"status": "UPSCALE_CANCEL_REQUESTED", "request_id": int(request_id)}

    def _remove_queued_upscale(self, file_id: int) -> bool:
        for queue in (self._upscale_queue, self._bulk_upscale_queue):
            try:
                queue._queue.remove(file_id)
            except ValueError:
                continue
            queue.task_done()
            if queue is self._bulk_upscale_queue:
                self._bulk_upscale_ids.discard(file_id)
            return True
        return False

    def _release_cancelled_queue(self, file_id: int) -> None:
        was_bulk_queued = file_id in self._skip_normal_ids
        self._skip_normal_ids.discard(file_id)
        if not was_bulk_queued:
            self._queued.discard(file_id)

    def upscale_queue_position(self, file_id: int) -> int:
        try:
            queued_position = list(self._upscale_queue._queue).index(file_id) + 1
        except ValueError:
            queued_position = 1
        current_upscale = self._current_phase in {
            "UPSCALING",
            "WAITING_UPSCALE_FORGE",
            "WAITING_UPSCALE_GPU",
            "VERIFYING_UPSCALE",
            "CANCELLING_UPSCALE",
        }
        return queued_position + int(current_upscale)

    def snapshot(self) -> dict[str, Any]:
        process = self._active_process
        process_live = process is not None and process.returncode is None
        actively_yielded = bool(self._yielding and self._current_file_id and process_live)
        visible_state = self.state
        if actively_yielded:
            visible_state = "PAUSED_THERMAL" if self._yield_reason == "thermal" else "PAUSED_YIELD"
        elif visible_state in {"PAUSED_THERMAL", "PAUSED_YIELD"}:
            visible_state = "VERIFYING_OUTPUT" if self._current_phase == "VERIFYING_OUTPUT" else "ACTIVE"
        return {
            "state": visible_state,
            "current_file_id": self._current_file_id,
            "current_path": self._current_path,
            "current_phase": self._current_phase,
            "current_detail": self._current_detail,
            "current_target_height": self._current_target_height,
            "current_phase_elapsed_sec": round(self._current_phase_elapsed_sec, 1),
            "current_progress_indeterminate": self._current_phase
            in {"PREPARING_UPSCALE_SEGMENTS", "INITIALIZING_UPSCALE_MODEL"},
            "current_wait_reason": self._current_wait_reason,
            "current_fps": round(self.current_fps, 1),
            "current_progress_percent": round(self.current_progress_percent, 2),
            "queued": (
                self._queue.qsize()
                + self._audio_queue.qsize()
                + self._test_queue.qsize()
                + self._upscale_queue.qsize()
                + self._bulk_upscale_queue.qsize()
            ),
            "bulk_queued": self._queue.qsize(),
            "bulk_upscale_queued": self._bulk_upscale_queue.qsize(),
            "audio_queued": self._audio_queue.qsize(),
            "manual_queued": self._test_queue.qsize() + self._upscale_queue.qsize(),
            "queue_enabled": self.queue_enabled,
            "upscale_queue_enabled": self.bulk_upscale_queue_enabled,
            "audio_queue_enabled": self.audio_queue_enabled,
            "current_elapsed_sec": round(time.monotonic() - self._started_at, 1) if self._started_at else 0,
        }

    async def queue_preview(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return upcoming work across manual/bulk upscales, video, and audio queues."""

        settings = await self.settings_store.load()
        ordered: list[tuple[int, str]] = []
        seen: set[int] = set()

        def append_queue(queue: asyncio.Queue[int], kind_for_id) -> None:
            for file_id in list(queue._queue):
                if file_id in seen:
                    continue
                if queue is self._queue and file_id in self._skip_normal_ids:
                    continue
                kind = kind_for_id(int(file_id))
                if queue is self._queue and file_id in self._audio_queued:
                    kind = "Bulk queue · audio queued after video"
                ordered.append((int(file_id), kind))
                seen.add(int(file_id))

        append_queue(self._upscale_queue, lambda _file_id: "Upscale · manual priority")
        append_queue(self._bulk_upscale_queue, lambda _file_id: "Upscale · bulk priority")
        if self.bulk_upscale_queue_enabled and len(ordered) < limit:
            for row in await self.database.upscale_candidates():
                file_id = int(row["id"])
                if file_id in seen or row.get("status") in {
                    "UPSCALE_QUEUED",
                    "UPSCALING",
                    "UPSCALE_CANCEL_REQUESTED",
                    "UPSCALED",
                    "UPSCALED_BACKUP_KEPT",
                }:
                    continue
                ordered.append((file_id, "Upscale · bulk priority"))
                seen.add(file_id)
                if len(ordered) >= limit:
                    break
        append_queue(
            self._test_queue,
            lambda file_id: "Full pipeline test" if file_id in self._full_test_ids else "NVDEC test",
        )
        append_queue(self._queue, lambda _file_id: "Bulk queue")
        append_queue(self._audio_queue, lambda _file_id: "Audio compatibility")

        if self.queue_enabled and len(ordered) < limit:
            for row in await self.database.pending_preview(max(limit * 2, limit)):
                file_id = int(row["id"])
                if file_id in seen:
                    continue
                action = row.get("action_tag")
                kind = self._bulk_task_label(action, settings.operation_mode)
                ordered.append((file_id, kind))
                seen.add(file_id)
                if len(ordered) >= limit:
                    break

        if self.audio_queue_enabled and len(ordered) < limit:
            for row in await self.database.pending_audio_preview(max(limit * 2, limit)):
                file_id = int(row["id"])
                if file_id in seen:
                    continue
                ordered.append((file_id, "Audio compatibility"))
                seen.add(file_id)
                if len(ordered) >= limit:
                    break

        selected = ordered[:limit]
        files = await self.database.files_by_ids([file_id for file_id, _kind in selected])
        return [
            {
                "id": file_id,
                "file_name": files[file_id]["file_name"],
                "file_path": files[file_id]["file_path"],
                "target_height": files[file_id].get("upscale_target_height"),
                "kind": (
                    f"Upscale · {int(files[file_id].get('upscale_target_height') or 1080)}p · bulk"
                    if kind == "Upscale · bulk priority"
                    else f"Upscale · {int(files[file_id].get('upscale_target_height') or 1080)}p · manual priority"
                    if kind.startswith("Upscale ·")
                    else self._bulk_task_label(files[file_id].get("action_tag"), settings.operation_mode)
                    + (" · audio queued after video" if "audio queued after video" in kind else "")
                    if kind.startswith("Bulk queue")
                    else kind
                ),
                "status": files[file_id]["status"],
            }
            for file_id, kind in selected
            if file_id in files
        ]

    @staticmethod
    def _bulk_task_label(action_tag: str | None, operation_mode: str) -> str:
        if action_tag == "UPSCALE":
            return "Health check · upscale candidate"
        if action_tag == "TRANSCODE" and operation_mode == "transcode":
            return "Transcode"
        return "Health check"

    async def set_yielding(self, paused: bool, reason: str | None = None) -> None:
        async with self._yield_state_lock:
            self._yielding = paused
            self._yield_reason = reason if paused else None
            file_id = self._current_file_id
            if file_id is None:
                return

            process = self._active_process
            process_live = process is not None and process.returncode is None
            if paused:
                # A GPU yield only pauses FFmpeg children. Do not persist a paused
                # media row while the worker is between subprocesses or has
                # already completed its child process.
                if not process_live:
                    return
                marked = await self.database.mark_yield_paused(file_id)
                if marked and self._current_file_id == file_id and self._active_process is process:
                    self.state = "PAUSED_THERMAL" if reason == "thermal" else "PAUSED_YIELD"
                return

            resume_status = self._current_phase
            if resume_status not in {
                "CHECKING",
                "TRANSCODING",
                "VERIFYING_OUTPUT",
                "AUDIO_REMUXING",
                "AUDIO_VERIFYING",
            }:
                return
            resumed = await self.database.resume_yield_paused(file_id, resume_status)
            if resumed or self.state in {"PAUSED_THERMAL", "PAUSED_YIELD"}:
                self.state = "VERIFYING_OUTPUT" if resume_status == "VERIFYING_OUTPUT" else "ACTIVE"

    async def _run_queue(self) -> None:
        while not self._stopping:
            is_test = False
            is_upscale = False
            is_bulk_upscale = False
            is_audio = False
            if not self._upscale_queue.empty():
                file_id = self._upscale_queue.get_nowait()
                is_upscale = True
            elif self.bulk_upscale_queue_enabled and not self._bulk_upscale_queue.empty():
                file_id = self._bulk_upscale_queue.get_nowait()
                is_bulk_upscale = True
            elif not self._test_queue.empty():
                file_id = self._test_queue.get_nowait()
                is_test = True
            elif self.queue_enabled and not self._queue.empty():
                file_id = self._queue.get_nowait()
            elif self.audio_queue_enabled and not self._audio_queue.empty():
                file_id = self._audio_queue.get_nowait()
                is_audio = True
            else:
                self._work_available.clear()
                if (
                    not self._upscale_queue.empty()
                    or not self._test_queue.empty()
                    or (self.bulk_upscale_queue_enabled and not self._bulk_upscale_queue.empty())
                    or (self.queue_enabled and not self._queue.empty())
                    or (self.audio_queue_enabled and not self._audio_queue.empty())
                ):
                    continue
                await self._work_available.wait()
                continue

            if not is_upscale and not is_bulk_upscale and not is_test and file_id in self._skip_normal_ids:
                self._skip_normal_ids.discard(file_id)
                self._queued.discard(file_id)
                self._queue.task_done()
                continue

            # Mark work active before process_file's first database await so a
            # concurrent test request cannot duplicate a file already started.
            self._current_file_id = file_id
            self._current_task_kind = (
                "manual_upscale"
                if is_upscale
                else "bulk_upscale"
                if is_bulk_upscale
                else "selected_test"
                if is_test
                else "audio"
                if is_audio
                else "video"
            )
            logger.info(
                "GPU-Forge worker selected media task",
                extra={
                    "file_id": file_id,
                    "task_type": self._current_task_kind,
                    "queued_after_selection": (
                        self._queue.qsize()
                        + self._audio_queue.qsize()
                        + self._test_queue.qsize()
                        + self._upscale_queue.qsize()
                        + self._bulk_upscale_queue.qsize()
                    ),
                },
            )
            try:
                if is_upscale or is_bulk_upscale:
                    await self._process_upscale(file_id)
                elif is_test:
                    previous_status = self._test_previous_status.get(file_id, "PENDING")
                    full_pipeline = file_id in self._full_test_ids
                    await self.process_file(
                        file_id,
                        test_only=not full_pipeline,
                        full_test=full_pipeline,
                        previous_status=previous_status,
                    )
                elif is_audio:
                    await self.process_audio_file(file_id)
                else:
                    await self.process_file(file_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled worker error for media row %s", file_id)
            finally:
                if is_upscale or is_bulk_upscale:
                    if is_bulk_upscale:
                        self._bulk_upscale_queue.task_done()
                        self._bulk_upscale_ids.discard(file_id)
                    else:
                        self._upscale_queue.task_done()
                    self._upscale_ids.discard(file_id)
                    self._upscale_cancel_requested.discard(file_id)
                    self._upscale_cancel_sent.discard(file_id)
                    was_cancelled = file_id in self._upscale_cancelled_ids
                    if was_cancelled:
                        self._upscale_cancelled_ids.discard(file_id)
                        self._release_cancelled_queue(file_id)
                    elif file_id not in self._skip_normal_ids:
                        self._queued.discard(file_id)
                elif is_test:
                    self._test_queue.task_done()
                    self._test_only_ids.discard(file_id)
                    self._full_test_ids.discard(file_id)
                    self._test_previous_status.pop(file_id, None)
                    if file_id not in self._skip_normal_ids:
                        self._queued.discard(file_id)
                elif is_audio:
                    self._audio_queue.task_done()
                    self._audio_queued.discard(file_id)
                else:
                    self._queue.task_done()
                    self._queued.discard(file_id)
                if self._current_file_id == file_id:
                    self._current_file_id = None
                    self._current_task_kind = None
                    self._current_path = None
                    self._current_phase = "IDLE"
                    self._current_detail = None
                    self._current_target_height = None
                    self._current_phase_elapsed_sec = 0.0
                    self._current_wait_reason = None
                    self._started_at = None
                    self.current_fps = 0.0
                    self.current_progress_percent = 0.0
                    self._last_upscale_frames = None
                    self._last_upscale_progress_at = None
                    self._current_duration = 0.0
                    self._current_progress_base = 0.0
                    self._current_progress_scale = 100.0
                    self.state = "IDLE"
                deferred_target = self._bulk_upscale_deferred.pop(file_id, None)
                if deferred_target is not None and self.bulk_upscale_queue_enabled:
                    try:
                        deferred_row = await self.database.get_file(file_id)
                        if deferred_row is not None:
                            await self.enqueue_bulk_upscale_candidates(
                                deferred_target,
                                candidates=[deferred_row],
                            )
                    except Exception:
                        logger.exception("Could not enqueue deferred bulk upscale for row %s", file_id)

    async def _finish_upscale_cancelled(
        self,
        file_id: int,
        file_path: str,
        backup_path: str,
        started: float,
    ) -> str:
        logger.info(
            "Manual upscale cancelled before source replacement",
            extra={"file_id": file_id, "file_path": file_path},
        )
        await self.database.update_file(
            file_id,
            status="UPSCALE_CANCELLED",
            backup_path=backup_path if os.path.exists(backup_path) else None,
            error_log=None,
            processing_time_sec=max(0.0, time.monotonic() - started),
        )
        self._upscale_cancelled_ids.add(file_id)
        return "UPSCALE_CANCELLED"

    async def _process_upscale(self, file_id: int) -> str | None:
        row = await self.database.get_file(file_id)
        if row is None:
            return None
        if row.get("status") == "UPSCALE_CANCELLED":
            self._upscale_cancelled_ids.add(file_id)
            return "UPSCALE_CANCELLED"

        file_path = row["file_path"]
        backup_path = file_path + ".gpuforge_bak"
        self._current_path = file_path
        self._current_phase = (
            "CANCELLING_UPSCALE"
            if row.get("status") == "UPSCALE_CANCEL_REQUESTED"
            else "WAITING_UPSCALE_FORGE"
        )
        self._current_target_height = int(row.get("upscale_target_height") or 1080)
        self._current_wait_reason = None
        self._current_detail = "Waiting for Upscale-Forge to accept the selected job."
        self._current_phase_elapsed_sec = 0.0
        self._started_at = time.monotonic()
        self._last_upscale_frames = None
        self._last_upscale_progress_at = None
        self.current_fps = 0.0
        self.current_progress_percent = 0.0
        self.state = self._current_phase
        started = time.monotonic()
        request_id = row.get("upscale_request_id")
        target_height = int(self._current_target_height)
        logger.info(
            "Upscale worker started",
            extra={
                "file_id": file_id,
                "file_path": file_path,
                "request_id": request_id,
                "target_height": target_height,
                "queue_kind": row.get("upscale_queue_kind", "manual"),
                "source_status": row.get("status"),
                "source_size_bytes": row.get("new_size_bytes") or row.get("size_bytes"),
            },
        )
        try:
            if target_height not in UPSCALE_TARGET_HEIGHTS:
                raise ValueError(f"Stored upscale target {target_height}p is unsupported.")
            if request_id is None and row.get("status") == "UPSCALE_CANCEL_REQUESTED":
                self._upscale_cancel_requested.add(file_id)
                while not self._stopping:
                    if not await self.upscale_service_online():
                        await asyncio.sleep(3.0)
                        continue
                    try:
                        source_stat = os.stat(file_path, follow_symlinks=False)
                    except OSError:
                        return await self._finish_upscale_cancelled(
                            file_id,
                            file_path,
                            backup_path,
                            started,
                        )
                    try:
                        active_job = await asyncio.to_thread(
                            self.upscale_client.find_active_job,
                            device_id=source_stat.st_dev,
                            inode=source_stat.st_ino,
                            size_bytes=source_stat.st_size,
                            mtime_ns=source_stat.st_mtime_ns,
                        )
                    except UpscaleServiceUnavailable:
                        await asyncio.sleep(3.0)
                        continue
                    if active_job is None:
                        return await self._finish_upscale_cancelled(
                            file_id,
                            file_path,
                            backup_path,
                            started,
                        )
                    request_id = int(active_job["request_id"])
                    await self.database.update_file(
                        file_id,
                        upscale_request_id=request_id,
                        status="UPSCALE_CANCEL_REQUESTED",
                    )
                    cancel_job = await asyncio.to_thread(
                        self.upscale_client.cancel_job,
                        request_id,
                    )
                    self._upscale_cancel_sent.add(file_id)
                    if cancel_job is not None and cancel_job.get("status") == "CANCELLED":
                        return await self._finish_upscale_cancelled(
                            file_id,
                            file_path,
                            backup_path,
                            started,
                        )
                    break
            if request_id is None:
                if row.get("status") != "UPSCALE_QUEUED":
                    raise ValueError("Queued upscale lost its source fingerprint; rescan and select it again.")
                if row.get("action_tag") != "UPSCALE":
                    raise ValueError("File is no longer an Upscale-Forge candidate.")
                expected_size = (
                    row["new_size_bytes"]
                    if row["new_size_bytes"] is not None
                    else row["size_bytes"]
                )
                while request_id is None and not self._stopping:
                    if file_id in self._upscale_cancel_requested:
                        return await self._finish_upscale_cancelled(
                            file_id,
                            file_path,
                            backup_path,
                            started,
                        )
                    if not await self.upscale_service_online():
                        self._current_phase = "WAITING_UPSCALE_FORGE"
                        self.state = "WAITING_UPSCALE_FORGE"
                        self._current_detail = "Waiting for the Upscale-Forge heartbeat."
                        await asyncio.sleep(3.0)
                        continue
                    try:
                        source_stat = os.stat(file_path, follow_symlinks=False)
                    except OSError as exc:
                        raise ValueError(f"Selected source is unavailable: {exc}") from exc
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise ValueError("Selected source is no longer a regular file.")
                    if source_stat.st_size != expected_size or float(source_stat.st_mtime) != float(row["mtime"]):
                        raise ValueError("Selected source changed after it was queued; rescan and select it again.")
                    try:
                        request_id = await asyncio.to_thread(
                            self.upscale_client.enqueue,
                            file_path,
                            device_id=source_stat.st_dev,
                            inode=source_stat.st_ino,
                            size_bytes=source_stat.st_size,
                            mtime_ns=source_stat.st_mtime_ns,
                            target_height=target_height,
                        )
                    except UpscaleServiceUnavailable:
                        await asyncio.sleep(3.0)
                        continue
                    if file_id in self._upscale_cancel_requested:
                        await self.database.update_file(
                            file_id,
                            status="UPSCALE_CANCEL_REQUESTED",
                            upscale_request_id=request_id,
                            backup_path=backup_path,
                            error_log=None,
                        )
                        cancel_job = await asyncio.to_thread(
                            self.upscale_client.cancel_job,
                            int(request_id),
                        )
                        self._upscale_cancel_sent.add(file_id)
                        if cancel_job is not None and cancel_job.get("status") == "CANCELLED":
                            return await self._finish_upscale_cancelled(
                                file_id,
                                file_path,
                                backup_path,
                                started,
                            )
                    else:
                        await self.database.update_file(
                            file_id,
                            status="UPSCALE_QUEUED",
                            upscale_request_id=request_id,
                            backup_path=file_path + ".gpuforge_bak",
                            error_log=None,
                        )
                    logger.info(
                        "Upscale request submitted to Upscale-Forge",
                        extra={
                            "file_id": file_id,
                            "request_id": int(request_id),
                            "file_path": file_path,
                            "target_height": target_height,
                            "source_device_id": source_stat.st_dev,
                            "source_inode": source_stat.st_ino,
                            "source_size_bytes": source_stat.st_size,
                        },
                    )
                    if (
                        file_id in self._upscale_cancel_requested
                        and file_id not in self._upscale_cancel_sent
                    ):
                        await self.database.update_file(
                            file_id,
                            status="UPSCALE_CANCEL_REQUESTED",
                            upscale_request_id=request_id,
                            backup_path=backup_path,
                            error_log=None,
                        )
                        cancel_job = await asyncio.to_thread(
                            self.upscale_client.cancel_job,
                            int(request_id),
                        )
                        self._upscale_cancel_sent.add(file_id)
                        if cancel_job is not None and cancel_job.get("status") == "CANCELLED":
                            return await self._finish_upscale_cancelled(
                                file_id,
                                file_path,
                                backup_path,
                                started,
                            )
                if request_id is None:
                    return None
            else:
                await self.database.update_file(
                    file_id,
                    status=str(row.get("status") or "UPSCALE_QUEUED"),
                    backup_path=row.get("backup_path") or file_path + ".gpuforge_bak",
                    error_log=None,
                )

            offline_since: float | None = None
            last_remote_status: str | None = None
            last_remote_phase: str | None = None
            last_progress_log_at = time.monotonic()
            last_logged_progress = -1.0
            last_initialization_warning_at = 0.0
            while not self._stopping:
                try:
                    job = await asyncio.to_thread(self.upscale_client.get_job, int(request_id))
                except UpscaleServiceUnavailable:
                    # Keep this priority slot until the external worker returns;
                    # allowing lower-priority GPU work to pass could overlap jobs.
                    if offline_since is None:
                        offline_since = time.monotonic()
                        logger.warning(
                            "Upscale-Forge state database became unavailable while a job is active",
                            extra={"file_id": file_id, "request_id": int(request_id)},
                        )
                    elif time.monotonic() - offline_since > 120.0:
                        raise UpscaleServiceUnavailable("Upscale-Forge stayed offline for two minutes.")
                    await asyncio.sleep(3.0)
                    continue
                if job is None:
                    raise UpscaleServiceUnavailable(
                        f"Upscale-Forge no longer has queued request {request_id}."
                    )
                remote_status = str(job.get("status") or "")
                remote_phase = str(job.get("phase") or "")
                phase_detail = str(job.get("phase_detail") or "") or None
                if remote_status != last_remote_status or remote_phase != last_remote_phase:
                    logger.info(
                        "Upscale-Forge job state changed",
                        extra={
                            "file_id": file_id,
                            "request_id": int(request_id),
                            "remote_status": remote_status,
                            "remote_phase": remote_phase,
                            "phase_detail": phase_detail,
                            "progress_percent": job.get("progress_percent"),
                            "active_segment_index": job.get("active_segment_index"),
                            "total_segments": job.get("total_segments"),
                        },
                    )
                if remote_status == "CANCELLED":
                    return await self._finish_upscale_cancelled(
                        file_id,
                        file_path,
                        backup_path,
                        started,
                    )
                if remote_status == "CANCEL_REQUESTED":
                    self._current_phase = "CANCELLING_UPSCALE"
                    self.state = "CANCELLING_UPSCALE"
                    self._current_wait_reason = None
                    self._current_detail = phase_detail or "Waiting for the active segment to stop safely."
                    if last_remote_status != remote_status:
                        await self.database.update_file(
                            file_id,
                            status="UPSCALE_CANCEL_REQUESTED",
                            error_log=None,
                        )
                elif remote_status == "PENDING":
                    self._current_phase = "WAITING_UPSCALE_FORGE"
                    self.state = "WAITING_UPSCALE_FORGE"
                    self._current_wait_reason = None
                    self._current_detail = phase_detail or "Waiting in Upscale-Forge's manual queue."
                    self.current_progress_percent = 0.0
                    if last_remote_status != remote_status:
                        await self.database.update_file(file_id, status="UPSCALE_QUEUED")
                elif remote_status == "PROCESSING":
                    self.current_progress_percent = min(
                        94.0,
                        max(0.0, float(job.get("progress_percent") or 0.0)),
                    )
                    self._current_phase_elapsed_sec = max(
                        0.0, float(job.get("phase_elapsed_sec") or 0.0)
                    )
                    frames_done = float(job.get("frames_done") or 0.0)
                    progress_at = time.monotonic()
                    if remote_phase != "ENCODING":
                        self.current_fps = 0.0
                        self._last_upscale_frames = frames_done
                        self._last_upscale_progress_at = progress_at
                    elif self._last_upscale_frames is None or self._last_upscale_progress_at is None:
                        self.current_fps = 0.0
                        self._last_upscale_frames = frames_done
                        self._last_upscale_progress_at = progress_at
                    elif (
                        frames_done > self._last_upscale_frames
                        and progress_at > self._last_upscale_progress_at
                    ):
                        self.current_fps = (
                            frames_done - self._last_upscale_frames
                        ) / (progress_at - self._last_upscale_progress_at)
                        self._last_upscale_frames = frames_done
                        self._last_upscale_progress_at = progress_at
                    self._current_detail = phase_detail
                    if remote_phase == "WAITING_GPU":
                        self._current_phase = "WAITING_UPSCALE_GPU"
                        self.state = "WAITING_UPSCALE_GPU"
                        self._current_wait_reason = str(job.get("wait_reason") or "GPU safety hold")
                        self._current_detail = self._current_wait_reason
                    elif remote_phase == "VERIFYING_OUTPUT":
                        self._current_phase = "VERIFYING_UPSCALE"
                        self.state = "VERIFYING_UPSCALE"
                        self._current_wait_reason = None
                    elif remote_phase == "INITIALIZING_MODEL":
                        self._current_phase = "INITIALIZING_UPSCALE_MODEL"
                        self.state = "UPSCALING"
                        self._current_wait_reason = None
                    elif remote_phase == "PREPARING_SEGMENTS":
                        self._current_phase = "PREPARING_UPSCALE_SEGMENTS"
                        self.state = "UPSCALING"
                        self._current_wait_reason = None
                    elif remote_phase == "FINALIZING_OUTPUT":
                        self._current_phase = "FINALIZING_UPSCALE"
                        self.state = "UPSCALING"
                        self._current_wait_reason = None
                    else:
                        self._current_phase = "UPSCALING"
                        self.state = "UPSCALING"
                        self._current_wait_reason = None
                    if (
                        self.current_progress_percent - last_logged_progress >= 0.5
                        or progress_at - last_progress_log_at >= 60.0
                    ):
                        logger.info(
                            "Manual upscale progress update",
                            extra={
                                "file_id": file_id,
                                "request_id": int(request_id),
                                "progress_percent": round(self.current_progress_percent, 2),
                                "frames_done": round(frames_done, 1),
                                "frames_total": job.get("frames_total"),
                                "current_fps": round(self.current_fps, 2),
                                "phase": remote_phase,
                                "phase_elapsed_sec": job.get("phase_elapsed_sec"),
                                "active_segment_index": job.get("active_segment_index"),
                                "total_segments": job.get("total_segments"),
                            },
                        )
                        last_logged_progress = self.current_progress_percent
                        last_progress_log_at = progress_at
                    phase_elapsed = float(job.get("phase_elapsed_sec") or 0.0)
                    if (
                        remote_phase == "INITIALIZING_MODEL"
                        and phase_elapsed >= 120.0
                        and progress_at - last_initialization_warning_at >= 120.0
                    ):
                        logger.warning(
                            "Upscale-Forge is still initializing the AI inference engine",
                            extra={
                                "file_id": file_id,
                                "request_id": int(request_id),
                                "phase_elapsed_sec": round(phase_elapsed, 1),
                                "active_segment_index": job.get("active_segment_index"),
                                "total_segments": job.get("total_segments"),
                                "progress_percent": round(self.current_progress_percent, 2),
                            },
                        )
                        last_initialization_warning_at = progress_at
                    if last_remote_status != remote_status:
                        await self.database.update_file(file_id, status="UPSCALING")
                if remote_status == "COMPLETED":
                    output_stat = os.stat(file_path, follow_symlinks=False)
                    if not os.path.isfile(backup_path) or os.path.getsize(backup_path) <= 0:
                        raise RuntimeError("Upscale-Forge completed without retaining the original backup.")
                    backup_stat = os.stat(backup_path, follow_symlinks=False)
                    expected_source_size = (
                        row["new_size_bytes"] if row["new_size_bytes"] is not None else row["size_bytes"]
                    )
                    if (
                        backup_stat.st_size != expected_source_size
                        or float(backup_stat.st_mtime) != float(row["mtime"])
                    ):
                        raise RuntimeError("Retained upscale backup does not match the selected source fingerprint.")
                    output_info, probe_error = await self._probe(file_path)
                    if probe_error or output_info is None:
                        raise RuntimeError(f"Upscaled output could not be probed: {probe_error}")
                    if int(output_info.get("height") or 0) != target_height:
                        raise RuntimeError(
                            f"Upscale-Forge output height was {output_info.get('height')}p; "
                            f"the selected target was {target_height}p."
                        )

                    fields = metadata_db_fields(output_info)
                    action_tag, action_reason = recommend_action(output_info)
                    fields.update(
                        {
                            "size_bytes": int(row["size_bytes"]),
                            "new_size_bytes": output_stat.st_size,
                            "mtime": output_stat.st_mtime,
                            "backup_path": backup_path,
                            "status": "UPSCALED_BACKUP_KEPT",
                            "error_log": None,
                            "action_tag": action_tag,
                            "action_reason": action_reason,
                            "upscale_request_id": int(request_id),
                            "upscale_target_height": target_height,
                            "upscaled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "processing_time_sec": max(0.0, time.monotonic() - started),
                        }
                    )
                    await self.database.update_file(file_id, **fields)
                    logger.info(
                        "Upscale completed and verified",
                        extra={
                            "file_id": file_id,
                            "request_id": int(request_id),
                            "file_path": file_path,
                            "backup_path": backup_path,
                            "target_height": target_height,
                            "output_bytes": output_stat.st_size,
                            "elapsed_seconds": round(time.monotonic() - started, 2),
                        },
                    )
                    return "UPSCALED_BACKUP_KEPT"

                if remote_status == "FAILED":
                    message = str(job.get("last_error") or "Upscale-Forge rejected the manual job.")
                    logger.error(
                        "Upscale-Forge reported manual job failure",
                        extra={
                            "file_id": file_id,
                            "request_id": int(request_id),
                            "file_path": file_path,
                            "phase": remote_phase,
                            "error": message,
                        },
                    )
                    update_fields: dict[str, Any] = {
                        "status": "UPSCALE_FAILED",
                        "backup_path": backup_path if os.path.exists(backup_path) else None,
                        "error_log": message,
                        "processing_time_sec": max(0.0, time.monotonic() - started),
                    }
                    await self.database.update_file(
                        file_id,
                        **update_fields,
                    )
                    return "UPSCALE_FAILED"
                last_remote_status = remote_status
                last_remote_phase = remote_phase
                online = await self.upscale_service_online()
                if online:
                    if offline_since is not None:
                        logger.info(
                            "Upscale-Forge state database connection recovered",
                            extra={
                                "file_id": file_id,
                                "request_id": int(request_id),
                                "offline_seconds": round(time.monotonic() - offline_since, 2),
                            },
                        )
                    offline_since = None
                elif offline_since is None:
                    offline_since = time.monotonic()
                    logger.warning(
                        "Upscale-Forge heartbeat is unavailable while a job is active",
                        extra={"file_id": file_id, "request_id": int(request_id)},
                    )
                elif time.monotonic() - offline_since > 120.0:
                    raise UpscaleServiceUnavailable("Upscale-Forge stayed offline for two minutes.")
                await asyncio.sleep(2.0)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Upscale-Forge job failed",
                extra={
                    "file_id": file_id,
                    "request_id": request_id,
                    "file_path": file_path,
                    "target_height": target_height,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "error": str(exc),
                },
            )
            await self.database.update_file(
                file_id,
                status="UPSCALE_FAILED",
                backup_path=(
                    file_path + ".gpuforge_bak"
                    if os.path.exists(file_path + ".gpuforge_bak")
                    else None
                ),
                error_log=str(exc),
                processing_time_sec=max(0.0, time.monotonic() - started),
            )
            return "UPSCALE_FAILED"

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
        logger.info(
            "GPU-Forge media processing started",
            extra={
                "file_id": file_id,
                "file_path": file_path,
                "source_status": row.get("status"),
                "source_bytes": row.get("new_size_bytes") or row.get("size_bytes"),
                "selected_test": test_only,
                "full_pipeline_test": full_test,
            },
        )
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

            action_tag, action_reason = recommend_action(info)
            await self.database.update_file(
                file_id,
                **metadata_db_fields(info),
                action_tag=action_tag,
                action_reason=action_reason,
            )
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
                route = "health" if action_tag == "UPSCALE" else route_media(info, settings)

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

    async def process_audio_file(self, file_id: int) -> str | None:
        """Add one AAC compatibility stream without changing the video encode."""

        row = await self.database.get_file(file_id)
        if row is None:
            return None

        file_path = row["file_path"]
        previous_status = str(row.get("status") or "PENDING")
        self._current_file_id = file_id
        self._current_path = file_path
        self._current_task_kind = "audio"
        self._started_at = time.monotonic()
        self._current_duration = 0.0
        self._current_progress_base = 0.0
        self._current_progress_scale = 100.0
        self.current_fps = 0.0
        self.current_progress_percent = 0.0
        self._current_phase = "AUDIO_CHECKING"
        self.state = "ACTIVE"
        temp_path: str | None = None

        try:
            if not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
                await self.database.update_file(
                    file_id,
                    audio_status="FAILED",
                    audio_error="Source file is missing or empty.",
                )
                return "AUDIO_FAILED"

            source_stat = os.stat(file_path, follow_symlinks=False)
            info, probe_error = await self._probe(file_path)
            if probe_error or info is None:
                reason = probe_error or "ffprobe returned no usable audio metadata."
                await self.database.update_file(
                    file_id,
                    audio_status="FAILED",
                    audio_error=f"Audio metadata probe failed: {reason}",
                )
                return "AUDIO_FAILED"

            await self.database.update_file(file_id, **metadata_db_fields(info))
            plan = plan_audio_fallback(info)
            if plan.action == "no_audio":
                await self.database.update_file(
                    file_id,
                    audio_status="NO_AUDIO",
                    audio_error=None,
                    audio_previous_status=None,
                )
                return "AUDIO_NO_AUDIO"
            if plan.action == "compatible":
                await self.database.update_file(
                    file_id,
                    audio_status="COMPATIBLE",
                    audio_error=None,
                    audio_previous_status=None,
                )
                return "AUDIO_COMPATIBLE"
            if plan.action == "already_added":
                await self.database.update_file(
                    file_id,
                    audio_status="FALLBACK_ADDED",
                    audio_error=None,
                    audio_previous_status=None,
                )
                return "AUDIO_FALLBACK_ADDED"

            retained_backup = row.get("backup_path")
            main_backup_path = file_path + ".gpuforge_bak"
            if not (retained_backup and os.path.isfile(retained_backup)) and os.path.lexists(main_backup_path):
                reason = f"Audio remux skipped because a retained backup exists at {main_backup_path}."
                await self.database.update_file(
                    file_id,
                    audio_status="BLOCKED_BACKUP",
                    audio_error=reason,
                )
                return "AUDIO_BLOCKED_BACKUP"

            settings = await self.settings_store.load()
            if not self.watchdog.available:
                reason = self.watchdog.error or "NVML/GPU device is unavailable for output verification."
                await self.database.update_file(
                    file_id,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            temp_path = output_temp_path(file_path)
            if os.path.lexists(temp_path):
                os.unlink(temp_path)
            await self.database.update_file(
                file_id,
                audio_status="PROCESSING",
                audio_error=None,
                audio_previous_status=previous_status,
            )
            self._current_duration = float(info.get("duration") or 0.0)
            self._current_phase = "AUDIO_REMUXING"
            self._current_progress_base = 0.0
            self._current_progress_scale = 65.0
            self.current_progress_percent = 0.0
            self.state = "ACTIVE"
            await self.database.update_file(file_id, status="AUDIO_REMUXING")
            command = build_audio_remux_command(file_path, temp_path, info, plan)
            result = await self._execute_ffmpeg(command)
            if result.returncode != 0:
                detail = result.stderr or f"FFmpeg exited with code {result.returncode}."
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=detail,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
                reason = "FFmpeg exited successfully but did not produce a non-empty audio remux."
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            current_stat = os.stat(file_path, follow_symlinks=False)
            if current_stat.st_size != source_stat.st_size or current_stat.st_mtime != source_stat.st_mtime:
                reason = "Source file changed during audio remux; replacement was not attempted."
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            new_size = os.path.getsize(temp_path)
            output_info, output_probe_error = await self._probe(temp_path)
            if output_probe_error or output_info is None:
                reason = f"Audio output metadata audit failed: {output_probe_error or 'no video stream found'}"
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            audit_error = self._audit_audio_output(info, output_info, file_path, plan)
            if audit_error:
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=audit_error,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            self._current_phase = "AUDIO_VERIFYING"
            self._current_progress_base = 65.0
            self._current_progress_scale = 35.0
            self.current_progress_percent = 65.0
            await self.database.update_file(file_id, status="AUDIO_VERIFYING")
            audio_verification = await self._execute_ffmpeg(build_audio_decode_command(temp_path))
            if audio_verification.returncode != 0:
                reason = "Audio decode verification failed.\n" + (
                    audio_verification.stderr or "No FFmpeg stderr was produced."
                )
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            self._current_phase = "VERIFYING_OUTPUT"
            self.current_progress_percent = 80.0
            await self.database.update_file(file_id, status="VERIFYING_OUTPUT")
            video_verification = await self._execute_ffmpeg(
                build_decode_command(temp_path, gpu_index=settings.gpu_index)
            )
            if video_verification.returncode != 0:
                reason = "Full NVDEC verification of the audio-remuxed file failed.\n" + (
                    video_verification.stderr or "No FFmpeg stderr was produced."
                )
                self._remove_temp(temp_path)
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

            return await self._transactional_audio_swap(
                row=row,
                source_stat=source_stat,
                output_info=output_info,
                temp_path=temp_path,
                new_size=new_size,
                settings=settings,
                previous_status=previous_status,
            )
        except asyncio.CancelledError:
            self._remove_temp(temp_path)
            latest = await self.database.get_file(file_id)
            if (
                latest
                and latest.get("audio_status") == "PROCESSING"
                and latest.get("status") != "AUDIO_TRANSCODING"
                and not latest.get("audio_swap_backup_path")
            ):
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="PENDING",
                    audio_error="Audio job was interrupted before replacement; it can be retried.",
                    audio_previous_status=None,
                )
            raise
        except Exception as exc:
            logger.exception("Audio compatibility processing failed for %s", file_path)
            self._remove_temp(temp_path)
            latest = await self.database.get_file(file_id)
            if (
                latest
                and latest.get("audio_status") not in {"FALLBACK_ADDED", "FALLBACK_BACKUP_KEPT"}
                and latest.get("status") != "AUDIO_TRANSCODING"
                and not latest.get("audio_swap_backup_path")
            ):
                await self.database.update_file(
                    file_id,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=str(exc),
                    audio_previous_status=None,
                    backup_path=row.get("backup_path"),
                )
            return "AUDIO_FAILED"
        finally:
            self._active_process = None

    async def _probe(self, file_path: str) -> tuple[dict[str, Any] | None, str | None]:
        return await probe_file(file_path)

    async def _execute_ffmpeg(self, command: list[str]) -> CommandResult:
        if not target_has_hwaccel(command) and not _safe_without_video_decode(command):
            raise RuntimeError(
                "Refusing to execute FFmpeg without CUDA decode flags unless the command "
                "explicitly avoids video decoding."
            )
        process_started = time.monotonic()
        input_path = command[command.index("-i") + 1] if "-i" in command else None
        if "hevc_nvenc" in command:
            operation = "video encode"
        elif "aac" in command:
            operation = "audio encode/remux"
        elif "-vn" in command:
            operation = "audio decode verification"
        elif _safe_without_video_decode(command):
            operation = "stream-copy remux"
        else:
            operation = "decode/verify"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(
            "GPU-Forge FFmpeg process started",
            extra={
                "file_id": self._current_file_id,
                "pid": process.pid,
                "operation": operation,
                "input_path": input_path,
                "output_path": command[-1] if command else None,
                "phase": self._current_phase,
                "gpu_index": (
                    command[command.index("-hwaccel_device") + 1]
                    if "-hwaccel_device" in command
                    else None
                ),
            },
        )
        self._active_process = process
        self.watchdog.register_child(process.pid)
        self._last_progress_at = None
        self._last_progress_frame = 0
        stdout_task = asyncio.create_task(self._consume_progress(process.stdout))
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            async with self._yield_state_lock:
                file_id = self._current_file_id
                if self._yielding and file_id is not None and process.returncode is None:
                    marked = await self.database.mark_yield_paused(file_id)
                    if marked and self._active_process is process:
                        self.state = (
                            "PAUSED_THERMAL" if self._yield_reason == "thermal" else "PAUSED_YIELD"
                        )
            returncode = await process.wait()
            stdout_data, stderr_data = await asyncio.gather(stdout_task, stderr_task)
            del stdout_data
            stderr_text = stderr_data.decode(errors="replace")
            logger.log(
                logging.INFO if returncode == 0 else logging.WARNING,
                "GPU-Forge FFmpeg process finished",
                extra={
                    "file_id": self._current_file_id,
                    "pid": process.pid,
                    "operation": operation,
                    "phase": self._current_phase,
                    "returncode": returncode,
                    "elapsed_seconds": round(time.monotonic() - process_started, 2),
                    "current_progress_percent": round(self.current_progress_percent, 2),
                    "current_fps": round(self.current_fps, 2),
                    "stderr_tail": stderr_text[-2000:] if returncode != 0 else None,
                },
            )
            return CommandResult(returncode, stderr_text)
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
        except Exception:
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
                logger.warning("Unable to stop FFmpeg PID %s after worker error", process.pid, exc_info=True)
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            self.watchdog.unregister_child(process.pid)
            async with self._yield_state_lock:
                if self._active_process is process:
                    self._active_process = None
                file_id = self._current_file_id
                if file_id is not None:
                    try:
                        resumed = await self.database.resume_yield_paused(
                            file_id,
                            self._current_phase,
                        )
                    except Exception:
                        logger.exception("Unable to clear yield pause for media row %s", file_id)
                    else:
                        if resumed or self.state in {"PAUSED_THERMAL", "PAUSED_YIELD"}:
                            self.state = (
                                "VERIFYING_OUTPUT"
                                if self._current_phase == "VERIFYING_OUTPUT"
                                else "ACTIVE"
                            )

    async def _consume_progress(self, stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        captured = bytearray()
        last_progress_log_at = time.monotonic()
        last_logged_progress = self.current_progress_percent
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
                    now = time.monotonic()
                    if (
                        self.current_progress_percent - last_logged_progress >= 5.0
                        or now - last_progress_log_at >= 60.0
                    ):
                        logger.info(
                            "GPU-Forge FFmpeg progress update",
                            extra={
                                "file_id": self._current_file_id,
                                "phase": self._current_phase,
                                "progress_percent": round(self.current_progress_percent, 2),
                                "current_fps": round(self.current_fps, 2),
                                "out_time_seconds": round(elapsed, 2),
                                "duration_seconds": round(self._current_duration, 2),
                            },
                        )
                        last_logged_progress = self.current_progress_percent
                        last_progress_log_at = now
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
        previous_phase = self._current_phase
        self._current_phase = phase
        self._current_progress_base = progress_base
        self._current_progress_scale = progress_scale
        self.current_progress_percent = progress_base
        self.state = "VERIFYING_OUTPUT" if phase == "VERIFYING_OUTPUT" else "ACTIVE"
        await self.database.update_file(file_id, status=phase, error_log=None)
        logger.info(
            "GPU-Forge media processing phase changed",
            extra={
                "file_id": file_id,
                "file_path": self._current_path,
                "previous_phase": previous_phase,
                "phase": phase,
                "progress_base": progress_base,
                "progress_scale": progress_scale,
            },
        )

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
        logger.info(
            "GPU-Forge media processing finished",
            extra={
                "file_id": file_id,
                "file_path": self._current_path,
                "status": status,
                "processing_time_sec": round(max(0.0, processing_time), 2),
                "error": error,
            },
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

    @staticmethod
    def _audit_audio_output(
        source: dict[str, Any],
        output: dict[str, Any],
        file_path: str,
        plan: AudioPlan,
    ) -> str | None:
        if output.get("codec_name") != source.get("codec_name"):
            return (
                "Audio-remux video codec audit failed: "
                f"source={source.get('codec_name')!r}, output={output.get('codec_name')!r}."
            )
        for dimension in ("width", "height"):
            if source.get(dimension) is not None and output.get(dimension) != source.get(dimension):
                return (
                    f"Audio-remux video {dimension} audit failed: "
                    f"source={source.get(dimension)!r}, output={output.get(dimension)!r}."
                )
        source_duration = source.get("duration")
        output_duration = output.get("duration")
        if source_duration is None or output_duration is None:
            return "Audio-remux duration audit failed: source or output duration is unavailable."
        if abs(float(output_duration) - float(source_duration)) > 0.5:
            return (
                "Audio-remux duration audit failed: "
                f"source={source_duration}s, output={output_duration}s (tolerance 0.5s)."
            )

        source_video = [
            stream for stream in source.get("video_stream_details", []) if isinstance(stream, dict)
        ]
        output_video = [
            stream for stream in output.get("video_stream_details", []) if isinstance(stream, dict)
        ]
        if source_video and len(source_video) != len(output_video):
            return (
                "Audio-remux video stream-count audit failed: "
                f"source={len(source_video)}, output={len(output_video)}."
            )
        for ordinal, source_stream in enumerate(source_video):
            for key in ("codec_name", "pix_fmt", "width", "height"):
                expected = source_stream.get(key)
                actual = output_video[ordinal].get(key)
                if expected is not None and actual != expected:
                    return (
                        f"Audio-remux video stream {ordinal} {key} audit failed: "
                        f"source={expected!r}, output={actual!r}."
                    )

        source_audio = [
            stream for stream in source.get("audio_stream_details", []) if isinstance(stream, dict)
        ]
        output_audio = [
            stream for stream in output.get("audio_stream_details", []) if isinstance(stream, dict)
        ]
        expected_count = len(source_audio) + int(plan.action == "add_fallback")
        if len(output_audio) != expected_count:
            return (
                "Audio-remux stream-count audit failed: "
                f"expected {expected_count} audio streams, received {len(output_audio)}."
            )
        for ordinal, source_stream in enumerate(source_audio):
            output_stream = output_audio[ordinal]
            for key in ("codec_name", "channels", "sample_rate", "language"):
                expected = source_stream.get(key)
                actual = output_stream.get(key)
                if expected is not None and actual != expected:
                    return (
                        f"Original audio stream {ordinal} {key} audit failed: "
                        f"source={expected!r}, output={actual!r}."
                    )

        target_ordinal = (
            len(source_audio) if plan.action == "add_fallback" else plan.fallback_ordinal
        )
        if target_ordinal is None or target_ordinal >= len(output_audio):
            return "Audio-remux audit failed: the compatibility stream could not be identified."
        target = output_audio[target_ordinal]
        if AUDIO_FALLBACK_TITLE.casefold() not in str(target.get("title") or "").casefold():
            return "Audio-remux audit failed: compatibility-track title is missing."
        if not _audio_stream_is_compatible(target):
            return (
                "Audio-remux audit failed: compatibility stream is not AAC-LC stereo at "
                "48 kHz or lower."
            )
        if source_audio[plan.source_ordinal or 0].get("language") and target.get("language") != source_audio[
            plan.source_ordinal or 0
        ].get("language"):
            return "Audio-remux audit failed: compatibility stream language was not preserved."
        default_ordinals = [
            ordinal
            for ordinal, stream in enumerate(output_audio)
            if bool((stream.get("disposition") or {}).get("default"))
        ]
        if default_ordinals != [target_ordinal]:
            return (
                "Audio-remux default-track audit failed: "
                f"expected only stream {target_ordinal} to be default, received {default_ordinals}."
            )

        if Path(file_path).suffix.lower() == ".mkv" and int(
            output.get("sub_streams") or 0
        ) != int(source.get("sub_streams") or 0):
            return (
                "Audio-remux subtitle stream audit failed: "
                f"source={source.get('sub_streams')}, output={output.get('sub_streams')}."
            )
        return None

    async def _transactional_audio_swap(
        self,
        *,
        row: dict[str, Any],
        source_stat: os.stat_result,
        output_info: dict[str, Any],
        temp_path: str,
        new_size: int,
        settings: Settings,
        previous_status: str,
    ) -> str:
        file_path = row["file_path"]
        backup_path = file_path + ".gpuforge_bak"
        destination_path = final_output_path(file_path)
        directory = os.path.dirname(file_path) or "."
        source_now = os.stat(file_path, follow_symlinks=False)
        if source_now.st_size != source_stat.st_size or source_now.st_mtime != source_stat.st_mtime:
            self._remove_temp(temp_path)
            reason = "Source fingerprint changed before audio replacement; original was left untouched."
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="FAILED",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_FAILED"
        retained_backup = row.get("backup_path")
        if retained_backup and os.path.isfile(retained_backup):
            return await self._transactional_audio_swap_with_retained_backup(
                row=row,
                source_stat=source_stat,
                output_info=output_info,
                temp_path=temp_path,
                new_size=new_size,
                previous_status=previous_status,
                retained_backup=retained_backup,
            )
        if os.path.lexists(backup_path):
            self._remove_temp(temp_path)
            reason = f"A retained backup appeared before audio replacement: {backup_path}."
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="BLOCKED_BACKUP",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_BLOCKED_BACKUP"
        if destination_path != file_path:
            if os.path.lexists(destination_path):
                self._remove_temp(temp_path)
                reason = f"Matroska destination already exists; refusing to overwrite {destination_path}."
                await self.database.update_file(
                    row["id"],
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"
            destination_row = await self.database.get_by_path(destination_path)
            if destination_row and destination_row["id"] != row["id"]:
                self._remove_temp(temp_path)
                reason = f"Matroska destination is already tracked by another library row: {destination_path}."
                await self.database.update_file(
                    row["id"],
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

        try:
            os.chown(temp_path, source_stat.st_uid, source_stat.st_gid, follow_symlinks=False)
            os.chmod(temp_path, stat.S_IMODE(source_stat.st_mode), follow_symlinks=False)
            self._fsync_file(temp_path)
        except OSError as exc:
            self._remove_temp(temp_path)
            reason = f"Could not copy source ownership/permissions to audio output: {exc}"
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="FAILED",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_FAILED"

        await self.database.update_file(
            row["id"],
            status="AUDIO_TRANSCODING",
            backup_path=backup_path,
            audio_status="PROCESSING",
            audio_previous_status=previous_status,
            audio_error=None,
        )
        backup_created = False
        committed = False
        try:
            os.replace(file_path, backup_path)
            backup_created = True
            self._fsync_directory(directory)
            os.replace(temp_path, destination_path)
            self._fsync_directory(directory)
            if not os.path.isfile(destination_path) or os.path.getsize(destination_path) != new_size:
                raise OSError("audio replacement size does not match the verified temporary output")

            replacement_stat = os.stat(destination_path, follow_symlinks=False)
            fields = metadata_db_fields(output_info)
            fields.update(
                {
                    "file_path": destination_path,
                    "file_name": os.path.basename(destination_path),
                    "directory": os.path.dirname(destination_path) or ".",
                    "size_bytes": int(row.get("size_bytes") or source_stat.st_size),
                    "new_size_bytes": new_size,
                    "mtime": replacement_stat.st_mtime,
                    "backup_path": backup_path,
                    "status": previous_status,
                    "audio_status": "FALLBACK_BACKUP_KEPT",
                    "audio_error": None,
                    "audio_previous_status": None,
                    "audio_swap_backup_path": None,
                    "error_log": row.get("error_log"),
                    "action_tag": row.get("action_tag"),
                    "action_reason": row.get("action_reason"),
                    "transcoded_at": row.get("transcoded_at"),
                    "upscaled_at": row.get("upscaled_at"),
                    "upscale_request_id": row.get("upscale_request_id"),
                    "upscale_target_height": row.get("upscale_target_height"),
                }
            )
            await self.database.update_file(row["id"], **fields)
            committed = True

            if settings.backup_policy == "auto_delete":
                try:
                    os.unlink(backup_path)
                except OSError as exc:
                    if os.path.exists(backup_path):
                        await self.database.update_file(
                            row["id"],
                            audio_error=f"Audio output committed; backup cleanup failed and the backup was retained: {exc}",
                        )
                        return "AUDIO_FALLBACK_BACKUP_KEPT"
                    await self.database.update_file(
                        row["id"],
                        status=previous_status,
                        backup_path=None,
                        audio_status="FALLBACK_ADDED",
                        audio_previous_status=None,
                    )
                else:
                    try:
                        self._fsync_directory(directory)
                    except OSError:
                        logger.warning("Could not sync audio backup cleanup for %s", backup_path, exc_info=True)
                    await self.database.update_file(
                        row["id"],
                        status=previous_status,
                        backup_path=None,
                        audio_status="FALLBACK_ADDED",
                        audio_previous_status=None,
                    )
            return (
                "AUDIO_FALLBACK_BACKUP_KEPT"
                if settings.backup_policy == "keep"
                else "AUDIO_FALLBACK_ADDED"
            )
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
                        status=previous_status,
                        audio_status="PENDING",
                        audio_error="Worker shutdown interrupted the audio replacement; original restored.",
                        audio_previous_status=None,
                    )
                except OSError:
                    logger.exception("Could not roll back audio replacement for %s", file_path)
            raise
        except Exception as exc:
            if committed:
                logger.exception("Post-commit audio backup cleanup failed for %s", file_path)
                return "AUDIO_FALLBACK_BACKUP_KEPT"
            self._remove_temp(temp_path)
            reason = f"Audio replacement transaction failed: {exc}"
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
                        status=previous_status,
                        audio_status="FAILED",
                        audio_error=reason,
                        audio_previous_status=None,
                    )
                except Exception as rollback_error:
                    reason += f" Immediate rollback failed; startup recovery will retry: {rollback_error}"
                    logger.exception("Audio rollback failed for %s", file_path)
                    await self.database.update_file(
                        row["id"],
                        backup_path=backup_path,
                        status="AUDIO_TRANSCODING",
                        audio_status="PROCESSING",
                        audio_error=reason,
                        audio_previous_status=previous_status,
                    )
            else:
                await self.database.update_file(
                    row["id"],
                    backup_path=None,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
            return "AUDIO_FAILED"

    async def _transactional_audio_swap_with_retained_backup(
        self,
        *,
        row: dict[str, Any],
        source_stat: os.stat_result,
        output_info: dict[str, Any],
        temp_path: str,
        new_size: int,
        previous_status: str,
        retained_backup: str,
    ) -> str:
        """Replace the current output while preserving an older retained backup."""

        file_path = row["file_path"]
        destination_path = final_output_path(file_path)
        rollback_path = file_path + ".gpuforge_audio_bak"
        directory = os.path.dirname(file_path) or "."
        current_stat = os.stat(file_path, follow_symlinks=False)
        if current_stat.st_size != source_stat.st_size or current_stat.st_mtime != source_stat.st_mtime:
            self._remove_temp(temp_path)
            reason = "Source fingerprint changed before audio replacement; current output was left untouched."
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="FAILED",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_FAILED"
        if os.path.lexists(rollback_path):
            self._remove_temp(temp_path)
            reason = f"A previous audio rollback file needs startup recovery: {rollback_path}."
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="FAILED",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_FAILED"
        if destination_path != file_path:
            if os.path.lexists(destination_path):
                self._remove_temp(temp_path)
                reason = f"Matroska destination already exists; refusing to overwrite {destination_path}."
                await self.database.update_file(
                    row["id"],
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"
            destination_row = await self.database.get_by_path(destination_path)
            if destination_row and destination_row["id"] != row["id"]:
                self._remove_temp(temp_path)
                reason = f"Matroska destination is already tracked by another library row: {destination_path}."
                await self.database.update_file(
                    row["id"],
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                )
                return "AUDIO_FAILED"

        try:
            os.chown(temp_path, source_stat.st_uid, source_stat.st_gid, follow_symlinks=False)
            os.chmod(temp_path, stat.S_IMODE(source_stat.st_mode), follow_symlinks=False)
            self._fsync_file(temp_path)
        except OSError as exc:
            self._remove_temp(temp_path)
            reason = f"Could not copy source ownership/permissions to audio output: {exc}"
            await self.database.update_file(
                row["id"],
                status=previous_status,
                audio_status="FAILED",
                audio_error=reason,
                audio_previous_status=None,
            )
            return "AUDIO_FAILED"

        await self.database.update_file(
            row["id"],
            status=previous_status,
            audio_status="PROCESSING",
            audio_previous_status=previous_status,
            audio_swap_backup_path=rollback_path,
            audio_error=None,
        )
        rollback_created = False
        committed = False
        try:
            os.replace(file_path, rollback_path)
            rollback_created = True
            self._fsync_directory(directory)
            os.replace(temp_path, destination_path)
            self._fsync_directory(directory)
            if not os.path.isfile(destination_path) or os.path.getsize(destination_path) != new_size:
                raise OSError("audio replacement size does not match the verified temporary output")

            replacement_stat = os.stat(destination_path, follow_symlinks=False)
            fields = metadata_db_fields(output_info)
            fields.update(
                {
                    "file_path": destination_path,
                    "file_name": os.path.basename(destination_path),
                    "directory": os.path.dirname(destination_path) or ".",
                    "size_bytes": int(row.get("size_bytes") or source_stat.st_size),
                    "new_size_bytes": new_size,
                    "mtime": replacement_stat.st_mtime,
                    "backup_path": retained_backup,
                    "status": previous_status,
                    "audio_status": "FALLBACK_ADDED",
                    "audio_error": None,
                    "audio_previous_status": None,
                    "audio_swap_backup_path": rollback_path,
                    "error_log": row.get("error_log"),
                    "action_tag": row.get("action_tag"),
                    "action_reason": row.get("action_reason"),
                    "transcoded_at": row.get("transcoded_at"),
                    "upscaled_at": row.get("upscaled_at"),
                    "upscale_request_id": row.get("upscale_request_id"),
                    "upscale_target_height": row.get("upscale_target_height"),
                }
            )
            await self.database.update_file(row["id"], **fields)
            committed = True
            try:
                os.unlink(rollback_path)
                self._fsync_directory(directory)
            except OSError as exc:
                await self.database.update_file(
                    row["id"],
                    audio_error=f"Audio output committed; temporary rollback cleanup failed: {exc}",
                )
                return "AUDIO_FALLBACK_ADDED"
            await self.database.update_file(row["id"], audio_swap_backup_path=None)
            return "AUDIO_FALLBACK_ADDED"
        except asyncio.CancelledError:
            self._remove_temp(temp_path)
            if rollback_created and os.path.exists(rollback_path):
                try:
                    if destination_path != file_path:
                        self._remove_temp(destination_path)
                    os.replace(rollback_path, file_path)
                    self._fsync_directory(directory)
                    await self.database.update_file(
                        row["id"],
                        file_path=file_path,
                        file_name=os.path.basename(file_path),
                        directory=os.path.dirname(file_path) or ".",
                        backup_path=retained_backup,
                        status=previous_status,
                        audio_status="PENDING",
                        audio_error="Worker shutdown interrupted audio replacement; current output restored.",
                        audio_previous_status=None,
                        audio_swap_backup_path=None,
                    )
                except OSError:
                    logger.exception("Could not roll back audio replacement for %s", file_path)
            raise
        except Exception as exc:
            if committed:
                logger.exception("Post-commit audio rollback cleanup failed for %s", file_path)
                return "AUDIO_FALLBACK_ADDED"
            self._remove_temp(temp_path)
            reason = f"Audio replacement transaction failed: {exc}"
            if rollback_created and os.path.exists(rollback_path):
                try:
                    if destination_path != file_path:
                        self._remove_temp(destination_path)
                    os.replace(rollback_path, file_path)
                    self._fsync_directory(directory)
                    await self.database.update_file(
                        row["id"],
                        file_path=file_path,
                        file_name=os.path.basename(file_path),
                        directory=os.path.dirname(file_path) or ".",
                        backup_path=retained_backup,
                        status=previous_status,
                        audio_status="FAILED",
                        audio_error=reason,
                        audio_previous_status=None,
                        audio_swap_backup_path=None,
                    )
                except Exception as rollback_error:
                    reason += f" Immediate rollback failed; startup recovery will retry: {rollback_error}"
                    logger.exception("Audio rollback failed for %s", file_path)
                    await self.database.update_file(
                        row["id"],
                        audio_status="PROCESSING",
                        audio_error=reason,
                        audio_previous_status=previous_status,
                        audio_swap_backup_path=rollback_path,
                    )
            else:
                await self.database.update_file(
                    row["id"],
                    backup_path=retained_backup,
                    status=previous_status,
                    audio_status="FAILED",
                    audio_error=reason,
                    audio_previous_status=None,
                    audio_swap_backup_path=None,
                )
            return "AUDIO_FAILED"

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
            output_action, output_action_reason = recommend_action(output_info)
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
                    "action_tag": output_action,
                    "action_reason": output_action_reason,
                    "transcoded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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
