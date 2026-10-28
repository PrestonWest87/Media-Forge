"""CPU-only acceptance coverage for GPU command and transaction safeguards."""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog
from app.main import (
    cancel_upscale_file,
    cleanup_upscale_preview,
    purge_verified_backups,
    restore_backup,
    upscale_preview_displayed,
    upscale_preview_image,
    upscale_preview_status,
)
from app.probe import parse_probe_data
from app.recovery import recover_startup
from app.routing import recommend_action
from app.scanner import MediaScanner, iter_tree_files
from app.upscale_client import UpscaleForgeClient
from app.worker import (
    CommandResult,
    MediaWorker,
    build_audio_decode_command,
    build_audio_remux_command,
    build_decode_command,
    build_transcode_command,
    final_output_path,
    output_temp_path,
    plan_audio_fallback,
    route_media,
    target_has_hwaccel,
)


class FakeWatchdog:
    available = True
    error = None

    def register_child(self, _pid: int) -> None:
        pass

    def unregister_child(self, _pid: int) -> None:
        pass


class GateThreeFailureWorker(MediaWorker):
    def __init__(self, *args: Any, fail_output_verification: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_output_verification = fail_output_verification
        self.source_info = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "width": 1920,
            "height": 1080,
            "duration": 60.0,
            "bit_rate": 5_000_000,
            "audio_streams": 1,
            "sub_streams": 0,
            "subtitle_streams": [],
        }
        self.output_info = {
            "codec_name": "hevc",
            "pix_fmt": "yuv420p",
            "width": 1920,
            "height": 1080,
            "duration": 60.0,
            "bit_rate": 2_000_000,
            "audio_streams": 1,
            "sub_streams": 0,
            "subtitle_streams": [],
        }

    async def _probe(self, file_path: str):
        if ".gpuforge_tmp" in file_path:
            return self.output_info, None
        return self.source_info, None

    async def _execute_ffmpeg(self, command: list[str]) -> CommandResult:
        if "-f" in command and command[command.index("-f") + 1] == "null":
            input_path = command[command.index("-i") + 1]
            if ".gpuforge_tmp" in input_path:
                if self.fail_output_verification:
                    return CommandResult(1, "simulated full NVDEC output verification failure")
                return CommandResult(0, "")
            return CommandResult(0, "")
        Path(command[-1]).write_bytes(b"verified-sized-output" * 40)
        return CommandResult(0, "")


class AudioCompatibilityWorker(MediaWorker):
    source_info = {
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "width": 1920,
        "height": 1080,
        "duration": 60.0,
        "bit_rate": 5_000_000,
        "audio_streams": 2,
        "audio_stream_details": [
            {
                "index": 1,
                "codec_name": "ac3",
                "profile": "Dolby Digital",
                "channels": 6,
                "channel_layout": "5.1",
                "sample_rate": 48_000,
                "language": "eng",
                "title": "Main",
                "disposition": {"default": True, "original": True},
            },
            {
                "index": 2,
                "codec_name": "aac",
                "profile": "LC",
                "channels": 2,
                "channel_layout": "stereo",
                "sample_rate": 48_000,
                "language": "jpn",
                "title": "Alternate",
                "disposition": {"default": False},
            },
        ],
        "sub_streams": 0,
        "subtitle_streams": [],
    }
    output_info = {
        **source_info,
        "audio_streams": 3,
        "audio_stream_details": [
            {
                **source_info["audio_stream_details"][0],
                "disposition": {"default": False, "original": True},
            },
            source_info["audio_stream_details"][1],
            {
                "index": 3,
                "codec_name": "aac",
                "profile": "LC",
                "channels": 2,
                "channel_layout": "stereo",
                "sample_rate": 48_000,
                "language": "eng",
                "title": "GPU-Forge AAC Compatibility",
                "disposition": {"default": True, "original": True},
            },
        ],
    }

    async def _probe(self, file_path: str):
        if ".gpuforge_tmp" in file_path:
            return self.output_info, None
        return self.source_info, None

    async def _execute_ffmpeg(self, command: list[str]) -> CommandResult:
        if ".gpuforge_tmp" in command[-1]:
            Path(command[-1]).write_bytes(b"verified audio remux output" * 80)
        return CommandResult(0, "")


class UpscaleCompletionWorker(MediaWorker):
    output_info = {
        "codec_name": "hevc",
        "pix_fmt": "yuv420p10le",
        "container_type": "mkv",
        "format_name": "matroska,webm",
        "tagged_upscaled": True,
        "width": 1280,
        "height": 720,
        "duration": 60.0,
        "bit_rate": 2_000_000,
        "audio_streams": 1,
        "sub_streams": 0,
        "subtitle_streams": [],
    }

    async def _probe(self, _file_path: str):
        return self.output_info, None


class FakeUpscaleClient:
    def __init__(self) -> None:
        self.request_id = 99
        self.job_status = "COMPLETED"
        self.last_error: str | None = None
        self.target_height: int | None = None
        self.cancel_status = "CANCEL_REQUESTED"
        self.cancelled_request_id: int | None = None
        self.active_job: dict[str, Any] | None = None

    def is_online(self) -> bool:
        return True

    def enqueue(self, file_path: str, *, target_height: int = 1080, **_identity: Any) -> int:
        self.target_height = target_height
        source = Path(file_path)
        source.replace(Path(str(source) + ".gpuforge_bak"))
        source.write_bytes(b"verified 1080p upscale output" * 8)
        return self.request_id

    def get_job(self, request_id: int):
        assert request_id == self.request_id
        return {
            "request_id": request_id,
            "status": self.job_status,
            "last_error": self.last_error,
        }

    def cancel_job(self, request_id: int):
        self.cancelled_request_id = request_id
        return {"request_id": request_id, "status": self.cancel_status}

    def find_active_job(self, **_identity: Any):
        return self.active_job


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "gpu_forge.db")
        await self.database.initialize()
        self.settings_store = SettingsStore(self.root / "settings.json")
        await self.settings_store.save(Settings(operation_mode="transcode", backup_policy="keep"))

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_gate_three_failure_preserves_original_bytes(self) -> None:
        source = self.root / "test.mkv"
        original_bytes = b"original media payload" * 100
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        worker = GateThreeFailureWorker(
            self.database,
            self.settings_store,
            FakeWatchdog(),
        )

        result = await worker.process_file(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "TRANSCODE_FAILED_RESTORED")
        self.assertEqual(updated["status"], "TRANSCODE_FAILED_RESTORED")
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(Path(output_temp_path(str(source))).exists())
        self.assertFalse(Path(str(source) + ".gpuforge_bak").exists())

    async def test_audio_queue_adds_default_aac_after_video_work_without_overwriting_backup(self) -> None:
        source = self.root / "audio-after-video.mkv"
        source.write_bytes(b"verified video output before audio remux" * 20)
        backup = Path(str(source) + ".gpuforge_bak")
        backup_bytes = b"retained pre-video original" * 20
        backup.write_bytes(backup_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="TRANSCODED_BACKUP_KEPT",
            backup_path=str(backup),
            new_size_bytes=source_stat.st_size,
            transcoded_at="2026-01-02 03:04:05",
        )
        worker = AudioCompatibilityWorker(self.database, self.settings_store, FakeWatchdog())

        result = await worker.process_audio_file(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "AUDIO_FALLBACK_ADDED")
        self.assertEqual(updated["audio_status"], "FALLBACK_ADDED")
        self.assertEqual(updated["status"], "TRANSCODED_BACKUP_KEPT")
        self.assertEqual(updated["backup_path"], str(backup))
        self.assertEqual(backup.read_bytes(), backup_bytes)
        self.assertEqual(updated["audio_streams"], 3)
        self.assertIsNone(updated["audio_swap_backup_path"])
        self.assertFalse(Path(str(source) + ".gpuforge_audio_bak").exists())
        self.assertEqual(source.read_bytes(), b"verified audio remux output" * 80)

    async def test_audio_backup_can_be_restored_without_losing_video_status(self) -> None:
        source = self.root / "audio-backup.mkv"
        source.write_bytes(b"audio source before compatibility remux" * 20)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(row["id"], status="HEALTHY")
        worker = AudioCompatibilityWorker(self.database, self.settings_store, FakeWatchdog())

        result = await worker.process_audio_file(row["id"])

        updated = await self.database.get_file(row["id"])
        backup = Path(str(source) + ".gpuforge_bak")
        self.assertEqual(result, "AUDIO_FALLBACK_BACKUP_KEPT")
        self.assertEqual(updated["audio_status"], "FALLBACK_BACKUP_KEPT")
        self.assertEqual(updated["status"], "HEALTHY")
        self.assertTrue(backup.exists())

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    database=self.database,
                    settings_store=self.settings_store,
                    scanner=None,
                    watchdog=FakeWatchdog(),
                    worker=worker,
                )
            )
        )
        restored = await restore_backup(row["id"], request)

        updated = await self.database.get_file(row["id"])
        self.assertEqual(restored["status"], "HEALTHY")
        self.assertEqual(source.read_bytes(), b"audio source before compatibility remux" * 20)
        self.assertEqual(updated["audio_status"], "PENDING")
        self.assertEqual(updated["status"], "HEALTHY")
        self.assertIsNone(updated["backup_path"])

    async def test_audio_queue_auto_deletes_backup_when_policy_requests_it(self) -> None:
        await self.settings_store.save(Settings(operation_mode="health_check", backup_policy="auto_delete"))
        source = self.root / "audio-auto-delete.mkv"
        source.write_bytes(b"audio source for auto-delete policy" * 20)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(row["id"], status="HEALTHY")
        worker = AudioCompatibilityWorker(self.database, self.settings_store, FakeWatchdog())

        result = await worker.process_audio_file(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "AUDIO_FALLBACK_ADDED")
        self.assertEqual(updated["audio_status"], "FALLBACK_ADDED")
        self.assertEqual(updated["status"], "HEALTHY")
        self.assertIsNone(updated["backup_path"])
        self.assertFalse(Path(str(source) + ".gpuforge_bak").exists())

    async def test_startup_recovery_restores_missing_primary_from_backup(self) -> None:
        source = self.root / "test.mkv"
        backup = Path(str(source) + ".gpuforge_bak")
        backup_bytes = b"known-good original"
        backup.write_bytes(backup_bytes)
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": len(backup_bytes),
                    "mtime": backup.stat().st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            backup_path=str(backup),
            status="TRANSCODING",
        )

        notices = await recover_startup(self.database, [str(self.root)])

        recovered = await self.database.get_file(row["id"])
        self.assertTrue(notices)
        self.assertEqual(source.read_bytes(), backup_bytes)
        self.assertFalse(backup.exists())
        self.assertEqual(recovered["status"], "TRANSCODE_FAILED_RESTORED")
        self.assertIsNone(recovered["backup_path"])

    async def test_startup_recovery_rolls_back_interrupted_audio_transaction(self) -> None:
        source = self.root / "audio-recovery.mkv"
        backup = Path(str(source) + ".gpuforge_bak")
        original_bytes = b"verified pre-audio output" * 10
        source.write_bytes(b"partial AAC replacement" * 5)
        backup.write_bytes(original_bytes)
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": len(original_bytes),
                    "mtime": source.stat().st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="AUDIO_TRANSCODING",
            audio_status="PROCESSING",
            audio_previous_status="TRANSCODED",
            backup_path=str(backup),
        )

        notices = await recover_startup(self.database, [str(self.root)])

        recovered = await self.database.get_file(row["id"])
        self.assertTrue(notices)
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(backup.exists())
        self.assertEqual(recovered["status"], "TRANSCODED")
        self.assertEqual(recovered["audio_status"], "PENDING")
        self.assertIsNone(recovered["backup_path"])

    async def test_startup_recovery_restores_audio_swap_while_preserving_older_backup(self) -> None:
        source = self.root / "audio-retained-backup.mkv"
        original_backup = Path(str(source) + ".gpuforge_bak")
        audio_swap = Path(str(source) + ".gpuforge_audio_bak")
        pre_audio_bytes = b"verified video output before audio swap" * 10
        source.write_bytes(b"partial AAC output" * 4)
        original_backup.write_bytes(b"older retained original" * 10)
        audio_swap.write_bytes(pre_audio_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": len(pre_audio_bytes),
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="TRANSCODED_BACKUP_KEPT",
            backup_path=str(original_backup),
            audio_status="PROCESSING",
            audio_previous_status="TRANSCODED_BACKUP_KEPT",
            audio_swap_backup_path=str(audio_swap),
        )

        notices = await recover_startup(self.database, [str(self.root)])

        recovered = await self.database.get_file(row["id"])
        self.assertTrue(notices)
        self.assertEqual(source.read_bytes(), pre_audio_bytes)
        self.assertTrue(original_backup.exists())
        self.assertFalse(audio_swap.exists())
        self.assertEqual(recovered["status"], "TRANSCODED_BACKUP_KEPT")
        self.assertEqual(recovered["audio_status"], "PENDING")
        self.assertEqual(recovered["backup_path"], str(original_backup))
        self.assertIsNone(recovered["audio_swap_backup_path"])

    async def test_startup_recovery_restores_original_from_interrupted_upscale(self) -> None:
        source = self.root / "upscale-recovery.mkv"
        backup = Path(str(source) + ".gpuforge_bak")
        original_bytes = b"original low-resolution source" * 20
        output_bytes = b"partially installed upscale output" * 5
        source.write_bytes(output_bytes)
        backup.write_bytes(original_bytes)
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": len(original_bytes),
                    "mtime": source.stat().st_mtime,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="UPSCALING",
            backup_path=str(backup),
            upscale_request_id=77,
            upscale_target_height=1080,
        )

        notices = await recover_startup(self.database, [str(self.root)])

        recovered = await self.database.get_file(row["id"])
        self.assertTrue(notices)
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(backup.exists())
        self.assertEqual(recovered["status"], "UPSCALE_FAILED")
        self.assertIsNone(recovered["backup_path"])

    async def test_startup_recovery_finishes_legacy_restore_after_backup_rename(self) -> None:
        original = self.root / "restored.avi"
        current_output = self.root / "restored.mkv"
        original_bytes = b"original restored before database commit"
        original.write_bytes(original_bytes)
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(current_output),
                    "file_name": current_output.name,
                    "directory": str(self.root),
                    "size_bytes": len(original_bytes),
                    "mtime": original.stat().st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(current_output))
        await self.database.update_file(
            row["id"],
            backup_path=str(original) + ".gpuforge_bak",
            status="TRANSCODING",
        )

        notices = await recover_startup(self.database, [str(self.root)])

        recovered = await self.database.get_file(row["id"])
        self.assertTrue(notices)
        self.assertEqual(recovered["file_path"], str(original))
        self.assertEqual(recovered["status"], "HEALTHY")
        self.assertEqual(original.read_bytes(), original_bytes)

    async def test_missing_gpu_skips_without_launching_ffmpeg(self) -> None:
        source = self.root / "no-gpu.mkv"
        original_bytes = b"source stays untouched" * 20
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        watchdog = FakeWatchdog()
        watchdog.available = False
        watchdog.error = "NVML unavailable in test; software fallback is disabled"
        worker = GateThreeFailureWorker(self.database, self.settings_store, watchdog)

        result = await worker.process_file(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "SKIPPED_HW_UNSUPPORTED")
        self.assertIn("software fallback is disabled", updated["error_log"])
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(Path(output_temp_path(str(source))).exists())

    async def test_selected_file_test_runs_health_only_while_bulk_queue_is_paused(self) -> None:
        source = self.root / "selected.mkv"
        original_bytes = b"selected source for an NVDEC-only test" * 30
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        worker = GateThreeFailureWorker(self.database, self.settings_store, FakeWatchdog())
        await worker.start()
        self.assertFalse(worker.queue_enabled)

        self.assertTrue(await worker.enqueue_test(row["id"]))
        await asyncio.wait_for(worker._test_queue.join(), timeout=5)
        await worker.stop()

        result = await self.database.get_file(row["id"])
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("NVDEC test passed", result["error_log"])
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(Path(output_temp_path(str(source))).exists())

    async def test_selected_full_pipeline_forces_transcode_and_keeps_verified_backup(self) -> None:
        await self.settings_store.save(Settings(operation_mode="health_check", backup_policy="auto_delete"))
        source = self.root / "full-test.mkv"
        original_bytes = b"full pipeline test source" * 100
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        worker = GateThreeFailureWorker(
            self.database,
            self.settings_store,
            FakeWatchdog(),
            fail_output_verification=False,
        )
        await worker.start()

        self.assertTrue(await worker.enqueue_test(row["id"], full_pipeline=True))
        await asyncio.wait_for(worker._test_queue.join(), timeout=5)
        await worker.stop()

        updated = await self.database.get_file(row["id"])
        backup = Path(str(source) + ".gpuforge_bak")
        self.assertEqual(updated["status"], "TRANSCODED_BACKUP_KEPT")
        self.assertTrue(source.exists())
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.assertEqual(source.stat().st_size, len(b"verified-sized-output" * 40))
        self.assertEqual(updated["backup_path"], str(backup))

    async def test_verified_legacy_transcode_swaps_to_mkv_and_retains_backup(self) -> None:
        source = self.root / "film.avi"
        original_bytes = b"legacy container source" * 100
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        worker = GateThreeFailureWorker(
            self.database,
            self.settings_store,
            FakeWatchdog(),
            fail_output_verification=False,
        )

        result = await worker.process_file(row["id"])

        destination = Path(final_output_path(str(source)))
        backup = Path(str(source) + ".gpuforge_bak")
        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "TRANSCODED_BACKUP_KEPT")
        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.assertEqual(updated["file_path"], str(destination))
        self.assertEqual(updated["backup_path"], str(backup))
        self.assertEqual(updated["status"], "TRANSCODED_BACKUP_KEPT")

    async def test_manual_restore_returns_legacy_file_to_original_extension(self) -> None:
        source = self.root / "restore-me.avi"
        current_output = self.root / "restore-me.mkv"
        backup = Path(str(source) + ".gpuforge_bak")
        original_bytes = b"retained original avi" * 20
        source.write_bytes(original_bytes)
        source.rename(backup)
        current_output.write_bytes(b"verified mkv output" * 8)
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(current_output),
                    "file_name": current_output.name,
                    "directory": str(self.root),
                    "size_bytes": len(original_bytes),
                    "mtime": current_output.stat().st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(current_output))
        await self.database.update_file(
            row["id"],
            backup_path=str(backup),
            status="TRANSCODED_BACKUP_KEPT",
        )
        watchdog = FakeWatchdog()
        worker = GateThreeFailureWorker(self.database, self.settings_store, watchdog)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    database=self.database,
                    settings_store=self.settings_store,
                    scanner=None,
                    watchdog=watchdog,
                    worker=worker,
                )
            )
        )

        result = await restore_backup(row["id"], request)

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result["file_path"], str(source))
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(current_output.exists())
        self.assertFalse(backup.exists())
        self.assertEqual(updated["file_path"], str(source))
        self.assertEqual(updated["status"], "HEALTHY")
        self.assertIsNone(updated["backup_path"])

    async def test_smart_skip_and_gpu_command_requirements(self) -> None:
        settings = Settings(operation_mode="transcode")
        already_hevc = {
            "codec_name": "hevc",
            "height": 1080,
            "bit_rate": 9_000_000,
            "audio_streams": 0,
            "sub_streams": 0,
            "subtitle_streams": [],
        }
        low_bitrate_h264 = {
            "codec_name": "h264",
            "height": 1080,
            "bit_rate": 1_500_000,
            "audio_streams": 0,
            "sub_streams": 0,
            "subtitle_streams": [],
        }
        high_bitrate_h264 = {**low_bitrate_h264, "bit_rate": 5_000_000}

        self.assertEqual(route_media(already_hevc, settings), "health")
        self.assertEqual(route_media(low_bitrate_h264, settings), "health")
        self.assertEqual(route_media(high_bitrate_h264, settings), "transcode")

        decode_command = build_decode_command("/media/example.mkv")
        transcode_command = build_transcode_command(
            "/media/example.mkv",
            "/media/example.mkv.gpuforge_tmp.mkv",
            high_bitrate_h264,
            settings,
        )
        for command in (decode_command, transcode_command):
            self.assertTrue(target_has_hwaccel(command))
            self.assertIn("-xerror", command)
            self.assertEqual(command[command.index("-hwaccel") + 1], "cuda")
            self.assertEqual(command[command.index("-hwaccel_output_format") + 1], "cuda")

    async def test_audio_plan_duplicates_only_default_track_and_marks_aac_default(self) -> None:
        info = AudioCompatibilityWorker.source_info
        plan = plan_audio_fallback(info)

        self.assertEqual(plan.action, "add_fallback")
        self.assertEqual(plan.source_ordinal, 0)
        command = build_audio_remux_command(
            "/media/example.mkv",
            "/media/example.mkv.gpuforge_tmp.mkv",
            info,
            plan,
        )
        maps = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-map"]
        self.assertIn("0:a?", maps)
        self.assertIn("0:a:0", maps)
        self.assertNotIn("0:a:1", maps)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a:2") + 1], "aac")
        self.assertEqual(command[command.index("-disposition:a:0") + 1], "original")
        self.assertEqual(command[command.index("-disposition:a:2") + 1], "original+default")
        self.assertIn("title=GPU-Forge AAC Compatibility", command)
        self.assertIn("-vn", build_audio_decode_command("/media/example.mkv"))
        self.assertFalse(target_has_hwaccel(command))

        self.assertIsNone(
            MediaWorker._audit_audio_output(
                info,
                AudioCompatibilityWorker.output_info,
                "/media/example.mkv",
                plan,
            )
        )
        self.assertEqual(plan_audio_fallback(AudioCompatibilityWorker.output_info).action, "already_added")

        bad_output = {
            **AudioCompatibilityWorker.output_info,
            "audio_stream_details": [
                {**AudioCompatibilityWorker.output_info["audio_stream_details"][0], "disposition": {"default": True}},
                AudioCompatibilityWorker.output_info["audio_stream_details"][1],
                {**AudioCompatibilityWorker.output_info["audio_stream_details"][2], "disposition": {"default": False}},
            ],
        }
        self.assertIn(
            "default-track audit",
            MediaWorker._audit_audio_output(info, bad_output, "/media/example.mkv", plan),
        )

    async def test_audio_plan_falls_back_to_first_track_and_skips_compatible_aac(self) -> None:
        info = AudioCompatibilityWorker.source_info
        no_default = {
            **info,
            "audio_stream_details": [
                {**info["audio_stream_details"][0], "disposition": {"default": False}},
                {**info["audio_stream_details"][1], "disposition": {"default": False}},
            ],
        }
        self.assertEqual(plan_audio_fallback(no_default).source_ordinal, 0)
        compatible = {
            **info,
            "audio_stream_details": [
                {
                    **info["audio_stream_details"][1],
                    "disposition": {"default": True},
                }
            ],
            "audio_streams": 1,
        }
        self.assertEqual(plan_audio_fallback(compatible).action, "compatible")

    async def test_external_gpu_yield_requires_activity_above_configured_threshold(self) -> None:
        self.assertEqual(Settings().external_gpu_utilization_threshold, 8)
        self.assertFalse(GpuWatchdog.exceeds_yield_threshold(5, 5, 0, 8))
        self.assertFalse(GpuWatchdog.exceeds_yield_threshold(8, 0, 0, 8))
        self.assertTrue(GpuWatchdog.exceeds_yield_threshold(2, 9, 0, 8))

    async def test_ffmpeg_progress_updates_per_file_percentage(self) -> None:
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker._current_duration = 100.0
        worker._current_phase = "CHECKING"
        stream = asyncio.StreamReader()
        stream.feed_data(b"frame=10\nout_time=00:00:50.000000\n")
        stream.feed_eof()

        await worker._consume_progress(stream)

        self.assertEqual(worker.current_progress_percent, 50.0)

    async def test_upscale_model_initialization_exposes_indeterminate_progress(self) -> None:
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker._current_phase = "INITIALIZING_UPSCALE_MODEL"
        worker._current_phase_elapsed_sec = 137.0

        snapshot = worker.snapshot()

        self.assertTrue(snapshot["current_progress_indeterminate"])
        self.assertEqual(snapshot["current_phase_elapsed_sec"], 137.0)

    async def test_scan_probes_container_metadata_and_recommends_processing(self) -> None:
        mkv = self.root / "transcode.mkv"
        low = self.root / "upscale.mp4"
        optimal = self.root / "optimal.mkv"
        for path in (mkv, low, optimal):
            path.write_bytes(b"sample media payload")

        def info(codec: str, width: int, height: int, bit_rate: int, container: str) -> dict[str, Any]:
            return {
                "codec_name": codec,
                "pix_fmt": "yuv420p",
                "width": width,
                "height": height,
                "duration": 60.0,
                "bit_rate": bit_rate,
                "audio_streams": 1,
                "sub_streams": 0,
                "subtitle_streams": [],
                "format_name": "matroska,webm" if container == "mkv" else "mov,mp4,m4a,3gp,3g2,mj2",
                "container_type": container,
                "tagged_upscaled": False,
            }

        async def fake_probe(file_path: str, **_kwargs: Any):
            if "upscale" in file_path:
                return info("h264", 640, 360, 450_000, "mp4"), None
            if "optimal" in file_path:
                return info("hevc", 1920, 1080, 5_000_000, "mkv"), None
            return info("h264", 1920, 1080, 5_000_000, "mkv"), None

        scanner = MediaScanner(self.database)
        with patch("app.scanner.probe_file", new=AsyncMock(side_effect=fake_probe)) as probe:
            result = await scanner.scan([str(self.root)])

        transcode_row = await self.database.get_by_path(str(mkv))
        upscale_row = await self.database.get_by_path(str(low))
        leave_row = await self.database.get_by_path(str(optimal))
        self.assertEqual(result["probed"], 3)
        self.assertEqual(result["action_counts"], {"TRANSCODE": 1, "LEAVE_ALONE": 1, "UPSCALE": 1})
        self.assertEqual(probe.await_count, 3)
        self.assertEqual(transcode_row["container_type"], "mkv")
        self.assertEqual(transcode_row["codec"], "h264")
        self.assertEqual((transcode_row["width"], transcode_row["height"]), (1920, 1080))
        self.assertEqual(transcode_row["action_tag"], "TRANSCODE")
        self.assertEqual(upscale_row["container_type"], "mp4")
        self.assertEqual(upscale_row["action_tag"], "UPSCALE")
        self.assertEqual(leave_row["action_tag"], "LEAVE_ALONE")
        upscale_view = await self.database.list_files(action_tags=["UPSCALE"])
        self.assertEqual([row["file_path"] for row in upscale_view], [str(low)])

    async def test_rescan_reoffers_unchanged_vfr_source_for_timestamp_safe_upscale(self) -> None:
        source = self.root / "manual-vfr.mkv"
        source.write_bytes(b"variable frame-rate source")
        vfr_reason = "variable-frame-rate input is left untouched to preserve timestamps and A/V sync"
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source.stat().st_size,
                    "mtime": source.stat().st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "width": 624,
                    "height": 352,
                    "action_tag": "LEAVE_ALONE",
                    "action_reason": vfr_reason,
                    "error_log": vfr_reason,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(row["id"], status="UPSCALE_FAILED")
        scanner = MediaScanner(self.database)

        with patch("app.scanner.probe_file", new=AsyncMock()) as probe:
            result = await scanner.scan([str(self.root)])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result["action_counts"], {"TRANSCODE": 0, "LEAVE_ALONE": 0, "UPSCALE": 1})
        self.assertEqual(probe.await_count, 0)
        self.assertEqual(updated["status"], "UPSCALE_FAILED")
        self.assertEqual(updated["action_tag"], "UPSCALE")
        candidates = await self.database.list_files(action_tags=["UPSCALE"])
        self.assertEqual([candidate["file_path"] for candidate in candidates], [str(source)])

    async def test_library_scan_excludes_upscale_staging_files(self) -> None:
        visible = self.root / "visible.mkv"
        staged = self.root / ".upscale_staging" / "job_123" / "encoded.mkv"
        visible.write_bytes(b"visible")
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"upscale segment")

        scanned = {path for path, _metadata in iter_tree_files([str(self.root)], media_only=True)}

        self.assertEqual(scanned, {str(visible)})

    async def test_rescan_preserves_transcode_history_and_does_not_requeue_output(self) -> None:
        source = self.root / "already-transcoded.mkv"
        source.write_bytes(b"original h264 source bytes" * 10)
        original_size = source.stat().st_size
        source.write_bytes(b"verified hevc output")
        output_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": original_size,
                    "mtime": output_stat.st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "format_name": "matroska,webm",
                    "width": 1920,
                    "height": 1080,
                    "bit_rate": 1_500_000,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        completed_at = "2026-01-02 03:04:05"
        await self.database.update_file(
            row["id"],
            status="TRANSCODED",
            new_size_bytes=output_stat.st_size,
            mtime=output_stat.st_mtime,
            action_tag="LEAVE_ALONE",
            transcoded_at=completed_at,
        )

        scanner = MediaScanner(self.database)
        with patch("app.scanner.probe_file", new=AsyncMock()) as probe:
            result = await scanner.scan([str(self.root)])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result["probed"], 0)
        probe.assert_not_awaited()
        self.assertEqual(updated["status"], "TRANSCODED")
        self.assertEqual(updated["transcoded_at"], completed_at)
        self.assertNotIn(row["id"], {item["id"] for item in await self.database.pending_files()})

    async def test_database_migrates_existing_transcode_rows(self) -> None:
        legacy_path = self.root / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE media_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    backup_path TEXT DEFAULT NULL,
                    file_name TEXT NOT NULL,
                    directory TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    new_size_bytes INTEGER DEFAULT NULL,
                    mtime REAL NOT NULL,
                    codec TEXT DEFAULT NULL,
                    pix_fmt TEXT DEFAULT NULL,
                    bit_rate INTEGER DEFAULT NULL,
                    width INTEGER DEFAULT NULL,
                    height INTEGER DEFAULT NULL,
                    audio_streams INTEGER DEFAULT 0,
                    sub_streams INTEGER DEFAULT 0,
                    duration REAL DEFAULT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    error_log TEXT DEFAULT NULL,
                    processing_time_sec REAL DEFAULT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO media_files (
                    file_path, file_name, directory, size_bytes, new_size_bytes,
                    mtime, codec, status, updated_at
                ) VALUES (
                    '/library/converted.mkv', 'converted.mkv', '/library', 200, 120,
                    1234.5, 'hevc', 'TRANSCODED', '2026-05-06 07:08:09'
                );
                """
            )

        legacy_database = Database(legacy_path)
        await legacy_database.initialize()
        migrated = await legacy_database.get_by_path("/library/converted.mkv")

        self.assertEqual(migrated["action_tag"], "LEAVE_ALONE")
        self.assertIsNone(migrated["container_type"])
        self.assertEqual(migrated["transcoded_at"], "2026-05-06 07:08:09")
        self.assertEqual(migrated["audio_status"], "PENDING")
        self.assertEqual(migrated["upscale_queue_kind"], "manual")
        self.assertEqual([row["id"] for row in await legacy_database.pending_audio_files()], [migrated["id"]])

    async def test_health_checked_transcode_candidate_is_requeued_when_transcoding_starts(self) -> None:
        source = self.root / "health-only.mkv"
        source.write_bytes(b"health checked source")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source.stat().st_size,
                    "mtime": source.stat().st_mtime,
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(row["id"], status="HEALTHY", action_tag="TRANSCODE")

        count = await self.database.requeue_transcode_candidates()

        self.assertEqual(count, 1)
        self.assertEqual((await self.database.get_file(row["id"]))["status"], "PENDING")

    async def test_manual_upscale_keeps_original_backup_and_commits_upscale_status(self) -> None:
        source = self.root / "manual-upscale.mkv"
        original_bytes = b"low resolution source" * 20
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "format_name": "matroska,webm",
                    "width": 640,
                    "height": 360,
                    "bit_rate": 600_000,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(row["id"], status="UPSCALE_QUEUED", upscale_target_height=720)
        worker = UpscaleCompletionWorker(self.database, self.settings_store, FakeWatchdog())
        upscale_client = FakeUpscaleClient()
        worker.upscale_client = upscale_client
        worker._current_file_id = row["id"]

        result = await worker._process_upscale(row["id"])

        updated = await self.database.get_file(row["id"])
        backup = Path(str(source) + ".gpuforge_bak")
        self.assertEqual(result, "UPSCALED_BACKUP_KEPT")
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.assertTrue(source.exists())
        self.assertEqual(updated["status"], "UPSCALED_BACKUP_KEPT")
        self.assertEqual(updated["backup_path"], str(backup))
        self.assertIsNotNone(updated["upscaled_at"])
        self.assertEqual(updated["height"], 720)
        self.assertEqual(updated["upscale_target_height"], 720)
        self.assertEqual(upscale_client.target_height, 720)

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    database=self.database,
                    settings_store=self.settings_store,
                    scanner=None,
                    watchdog=FakeWatchdog(),
                    worker=worker,
                )
            )
        )
        purged = await purge_verified_backups(request)
        self.assertEqual(purged["purged"], [str(backup)])
        self.assertFalse(backup.exists())
        self.assertEqual((await self.database.get_file(row["id"]))["status"], "UPSCALED")

    async def test_manual_upscale_failure_surfaces_remote_reason_without_touching_source(self) -> None:
        source = self.root / "manual-vfr.mkv"
        original_bytes = b"variable frame-rate source" * 20
        source.write_bytes(original_bytes)
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "width": 624,
                    "height": 352,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="UPSCALE_QUEUED",
            upscale_request_id=77,
            upscale_target_height=1080,
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        upscale_client = FakeUpscaleClient()
        upscale_client.request_id = 77
        upscale_client.job_status = "FAILED"
        upscale_client.last_error = (
            "variable-frame-rate input is left untouched to preserve timestamps and A/V sync"
        )
        worker.upscale_client = upscale_client

        result = await worker._process_upscale(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result, "UPSCALE_FAILED")
        self.assertEqual(updated["status"], "UPSCALE_FAILED")
        self.assertEqual(updated["error_log"], upscale_client.last_error)
        self.assertEqual(updated["action_tag"], "UPSCALE")
        self.assertIsNone(updated["backup_path"])
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertFalse(Path(str(source) + ".gpuforge_bak").exists())

    async def test_queued_upscale_can_be_cancelled_without_a_remote_request(self) -> None:
        source = self.root / "queued-cancel.mkv"
        source.write_bytes(b"original queued upscale source")
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="UPSCALE_QUEUED",
            upscale_target_height=1080,
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker.upscale_client = FakeUpscaleClient()
        worker._upscale_queue.put_nowait(row["id"])
        worker._upscale_ids.add(row["id"])
        worker._queued.add(row["id"])
        worker._skip_normal_ids.add(row["id"])

        result = await worker.cancel_upscale(row["id"])

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result["status"], "UPSCALE_CANCELLED")
        self.assertEqual(updated["status"], "UPSCALE_CANCELLED")
        self.assertEqual(worker._upscale_queue.qsize(), 0)
        self.assertNotIn(row["id"], worker._skip_normal_ids)
        self.assertIn(row["id"], worker._queued)
        self.assertEqual(source.read_bytes(), b"original queued upscale source")
        self.assertFalse(Path(str(source) + ".gpuforge_bak").exists())

    async def test_cancel_endpoint_requests_stop_for_active_remote_upscale(self) -> None:
        source = self.root / "active-cancel.mkv"
        source.write_bytes(b"original active upscale source")
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="UPSCALING",
            upscale_request_id=77,
            upscale_target_height=1080,
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        upscale_client = FakeUpscaleClient()
        upscale_client.cancel_status = "CANCEL_REQUESTED"
        worker.upscale_client = upscale_client
        worker._current_file_id = row["id"]
        worker._current_phase = "UPSCALING"
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    database=self.database,
                    settings_store=self.settings_store,
                    scanner=None,
                    watchdog=FakeWatchdog(),
                    worker=worker,
                )
            )
        )

        result = await cancel_upscale_file(row["id"], request)

        updated = await self.database.get_file(row["id"])
        self.assertEqual(result["status"], "UPSCALE_CANCEL_REQUESTED")
        self.assertEqual(updated["status"], "UPSCALE_CANCEL_REQUESTED")
        self.assertEqual(upscale_client.cancelled_request_id, 77)
        self.assertEqual(worker._current_phase, "CANCELLING_UPSCALE")
        self.assertEqual(source.read_bytes(), b"original active upscale source")
        self.assertFalse(Path(str(source) + ".gpuforge_bak").exists())

    async def test_probe_detects_container_and_upscale_forge_tag(self) -> None:
        payload = {
            "format": {
                "format_name": "matroska,webm",
                "tags": {"UPSCALE_FORGE": "1080p_v1"},
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "pix_fmt": "yuv420p10le",
                    "width": 640,
                    "height": 360,
                    "bit_rate": "500000",
                }
            ],
        }

        info = parse_probe_data(payload, "/media/upscaled.mkv")

        self.assertEqual(info["container_type"], "mkv")
        self.assertTrue(info["tagged_upscaled"])
        self.assertEqual(recommend_action(info)[0], "LEAVE_ALONE")

    async def test_probe_captures_audio_codec_and_default_track_metadata(self) -> None:
        payload = {
            "format": {"format_name": "matroska,webm", "duration": "20"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "profile": "Dolby Digital",
                    "channels": 6,
                    "channel_layout": "5.1",
                    "sample_rate": "48000",
                    "tags": {"LANGUAGE": "eng", "TITLE": "Main"},
                    "disposition": {"default": 1, "original": 1},
                },
            ],
        }

        info = parse_probe_data(payload, "/media/audio.mkv")

        self.assertEqual(info["audio_streams"], 1)
        self.assertEqual(
            info["audio_stream_details"][0],
            {
                "index": 1,
                "codec_name": "ac3",
                "profile": "Dolby Digital",
                "channels": 6,
                "channel_layout": "5.1",
                "sample_rate": 48_000,
                "language": "eng",
                "title": "Main",
                "disposition": {"default": True, "original": True},
            },
        )

    async def test_yield_status_only_tracks_live_children_and_stale_jobs_requeue(self) -> None:
        paths = [self.root / name for name in ("stale.mkv", "active.mkv", "complete.mkv")]
        for path in paths:
            path.write_bytes(b"media bytes")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(path),
                    "file_name": path.name,
                    "directory": str(self.root),
                    "size_bytes": path.stat().st_size,
                    "mtime": path.stat().st_mtime,
                }
                for path in paths
            ]
        )
        rows = [await self.database.get_by_path(str(path)) for path in paths]
        for row in rows:
            await self.database.update_file(row["id"], status="PAUSED_YIELD")
        await self.database.update_file(rows[1]["id"], status="CHECKING")
        await self.database.update_file(
            rows[2]["id"],
            new_size_bytes=paths[2].stat().st_size,
            transcoded_at="2026-01-02 03:04:05",
        )

        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker._current_file_id = rows[1]["id"]
        worker._current_phase = "CHECKING"
        worker.state = "ACTIVE"
        await worker.set_yielding(True, "thermal")
        self.assertEqual((await self.database.get_file(rows[1]["id"]))["status"], "CHECKING")
        worker._active_process = SimpleNamespace(returncode=None)
        await worker.set_yielding(True, "thermal")
        self.assertEqual((await self.database.get_file(rows[1]["id"]))["status"], "PAUSED_YIELD")
        await worker.set_yielding(False)
        self.assertEqual((await self.database.get_file(rows[1]["id"]))["status"], "CHECKING")

        requeued = await self.database.requeue_stale_yield_pauses({rows[1]["id"]})
        self.assertEqual(requeued, 2)
        self.assertEqual((await self.database.get_file(rows[0]["id"]))["status"], "PENDING")
        self.assertEqual((await self.database.get_file(rows[1]["id"]))["status"], "CHECKING")
        self.assertEqual((await self.database.get_file(rows[2]["id"]))["status"], "TRANSCODED")

    async def test_manual_upscale_precedes_bulk_work_in_queue_preview(self) -> None:
        upscale_path = self.root / "manual-upscale.mkv"
        bulk_upscale_path = self.root / "bulk-upscale.mkv"
        transcode_path = self.root / "next-transcode.mkv"
        audio_path = self.root / "audio-only.mkv"
        upscale_path.write_bytes(b"upscale source")
        bulk_upscale_path.write_bytes(b"bulk upscale source")
        transcode_path.write_bytes(b"transcode source")
        audio_path.write_bytes(b"audio source")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(upscale_path),
                    "file_name": upscale_path.name,
                    "directory": str(self.root),
                    "size_bytes": upscale_path.stat().st_size,
                    "mtime": upscale_path.stat().st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                },
                {
                    "file_path": str(transcode_path),
                    "file_name": transcode_path.name,
                    "directory": str(self.root),
                    "size_bytes": transcode_path.stat().st_size,
                    "mtime": transcode_path.stat().st_mtime,
                    "codec": "h264",
                    "container_type": "mkv",
                    "width": 1920,
                    "height": 1080,
                    "action_tag": "TRANSCODE",
                },
                {
                    "file_path": str(bulk_upscale_path),
                    "file_name": bulk_upscale_path.name,
                    "directory": str(self.root),
                    "size_bytes": bulk_upscale_path.stat().st_size,
                    "mtime": bulk_upscale_path.stat().st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                },
                {
                    "file_path": str(audio_path),
                    "file_name": audio_path.name,
                    "directory": str(self.root),
                    "size_bytes": audio_path.stat().st_size,
                    "mtime": audio_path.stat().st_mtime,
                    "codec": "hevc",
                    "container_type": "mkv",
                    "width": 1920,
                    "height": 1080,
                    "action_tag": "LEAVE_ALONE",
                },
            ]
        )
        upscale_row = await self.database.get_by_path(str(upscale_path))
        bulk_upscale_row = await self.database.get_by_path(str(bulk_upscale_path))
        transcode_row = await self.database.get_by_path(str(transcode_path))
        audio_row = await self.database.get_by_path(str(audio_path))
        await self.database.update_file(
            bulk_upscale_row["id"],
            status="UPSCALE_QUEUED",
            upscale_queue_kind="bulk",
            upscale_target_height=720,
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker._upscale_queue.put_nowait(upscale_row["id"])
        worker._bulk_upscale_queue.put_nowait(bulk_upscale_row["id"])
        worker._bulk_upscale_ids.add(bulk_upscale_row["id"])
        worker._queue.put_nowait(transcode_row["id"])
        worker._audio_queue.put_nowait(audio_row["id"])
        worker._audio_queued.add(audio_row["id"])
        worker.queue_enabled = True
        worker.bulk_upscale_queue_enabled = True
        worker.audio_queue_enabled = True

        preview = await worker.queue_preview(4)

        self.assertEqual(
            [item["id"] for item in preview],
            [upscale_row["id"], bulk_upscale_row["id"], transcode_row["id"], audio_row["id"]],
        )
        self.assertEqual(preview[0]["kind"], "Upscale · 1080p · manual priority")
        self.assertEqual(preview[1]["kind"], "Upscale · 720p · bulk")
        self.assertEqual(preview[2]["kind"], "Transcode")
        self.assertEqual(preview[3]["kind"], "Audio compatibility")

    async def test_audio_queue_can_start_without_starting_video_queue(self) -> None:
        source = self.root / "audio-pending.mkv"
        source.write_bytes(b"audio queue source")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source.stat().st_size,
                    "mtime": source.stat().st_mtime,
                }
            ]
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())

        enqueued = await worker.start_audio_queue()

        self.assertEqual(enqueued, 1)
        self.assertFalse(worker.queue_enabled)
        self.assertTrue(worker.audio_queue_enabled)
        self.assertEqual(worker._audio_queue.qsize(), 1)

    async def test_bulk_upscale_queue_can_start_independently_at_selected_target(self) -> None:
        source = self.root / "bulk-candidate.mkv"
        source.write_bytes(b"eligible bulk upscale source")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source.stat().st_size,
                    "mtime": source.stat().st_mtime,
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker.upscale_client = FakeUpscaleClient()

        queued = await worker.start_bulk_upscale_queue(720)

        updated = await self.database.get_file(row["id"])
        self.assertEqual(queued, 1)
        self.assertTrue(worker.bulk_upscale_queue_enabled)
        self.assertFalse(worker.queue_enabled)
        self.assertFalse(worker.audio_queue_enabled)
        self.assertEqual(worker._bulk_upscale_queue.qsize(), 1)
        self.assertEqual(updated["status"], "UPSCALE_QUEUED")
        self.assertEqual(updated["upscale_queue_kind"], "bulk")
        self.assertEqual(updated["upscale_target_height"], 720)

        worker.pause_bulk_upscale_queue()
        self.assertFalse(worker.bulk_upscale_queue_enabled)

        recovered_worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        recovered_worker.upscale_client = FakeUpscaleClient()
        await recovered_worker.start()
        self.assertFalse(recovered_worker.bulk_upscale_queue_enabled)
        self.assertEqual(recovered_worker._bulk_upscale_queue.qsize(), 1)
        self.assertEqual(recovered_worker._upscale_queue.qsize(), 0)
        await recovered_worker.start_bulk_upscale_queue(1080)
        self.assertTrue(recovered_worker.bulk_upscale_queue_enabled)
        self.assertEqual(recovered_worker._bulk_upscale_queue.qsize(), 1)
        self.assertEqual((await self.database.get_file(row["id"]))["upscale_target_height"], 720)
        await recovered_worker.stop()

    async def test_worker_runs_manual_upscale_before_bulk_items(self) -> None:
        upscale_path = self.root / "priority-upscale.mkv"
        bulk_upscale_path = self.root / "priority-bulk-upscale.mkv"
        normal_path = self.root / "priority-normal.mkv"
        audio_path = self.root / "priority-audio.mkv"
        for path in (upscale_path, bulk_upscale_path, normal_path, audio_path):
            path.write_bytes(b"queue order source")
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(path),
                    "file_name": path.name,
                    "directory": str(self.root),
                    "size_bytes": path.stat().st_size,
                    "mtime": path.stat().st_mtime,
                }
                for path in (upscale_path, bulk_upscale_path, normal_path, audio_path)
            ]
        )
        upscale_row = await self.database.get_by_path(str(upscale_path))
        bulk_upscale_row = await self.database.get_by_path(str(bulk_upscale_path))
        normal_row = await self.database.get_by_path(str(normal_path))
        audio_row = await self.database.get_by_path(str(audio_path))
        await self.database.update_file(upscale_row["id"], status="UPSCALE_QUEUED")
        await self.database.update_file(
            bulk_upscale_row["id"],
            status="UPSCALE_QUEUED",
            upscale_queue_kind="bulk",
            upscale_target_height=720,
        )
        execution_order: list[tuple[str, int]] = []

        class OrderedWorker(MediaWorker):
            async def _process_upscale(inner_self, file_id: int):
                execution_order.append(("upscale", file_id))
                await inner_self.database.update_file(file_id, status="UPSCALED")

            async def process_file(inner_self, file_id: int, **_kwargs: Any):
                execution_order.append(("video", file_id))
                await inner_self.database.update_file(file_id, status="HEALTHY")

            async def process_audio_file(inner_self, file_id: int):
                execution_order.append(("audio", file_id))
                await inner_self.database.update_file(file_id, audio_status="COMPATIBLE")

        worker = OrderedWorker(self.database, self.settings_store, FakeWatchdog())
        worker._queue.put_nowait(normal_row["id"])
        worker._queued.add(normal_row["id"])
        worker._audio_queue.put_nowait(audio_row["id"])
        worker._audio_queued.add(audio_row["id"])
        worker.queue_enabled = True
        worker.bulk_upscale_queue_enabled = True
        worker.audio_queue_enabled = True
        await worker.start()

        await asyncio.wait_for(
            asyncio.gather(
                worker._upscale_queue.join(),
                worker._bulk_upscale_queue.join(),
                worker._queue.join(),
                worker._audio_queue.join(),
            ),
            timeout=5,
        )
        await worker.stop()

        self.assertEqual(
            execution_order,
            [
                ("upscale", upscale_row["id"]),
                ("upscale", bulk_upscale_row["id"]),
                ("video", normal_row["id"]),
                ("audio", audio_row["id"]),
            ],
        )

    async def test_upscale_client_uses_shared_manual_queue_and_heartbeat(self) -> None:
        shared_path = self.root / "upscale-state.db"
        with sqlite3.connect(shared_path) as connection:
            connection.executescript(
                """
                CREATE TABLE service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE manual_jobs (
                    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filepath TEXT NOT NULL,
                    device_id INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    target_height INTEGER NOT NULL DEFAULT 1080,
                    status TEXT NOT NULL,
                    resolved_filepath TEXT,
                    last_error TEXT,
                    requested_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE media_files (
                    filepath TEXT NOT NULL,
                    inode INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE segment_progress (
                    filepath TEXT NOT NULL,
                    inode INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    segment_index INTEGER NOT NULL,
                    target_height INTEGER NOT NULL,
                    duration_seconds REAL,
                    total_frames INTEGER NOT NULL DEFAULT 0,
                    progress_percent REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                """
            )
            connection.execute(
                "INSERT INTO service_state(key,value,updated_at) VALUES('heartbeat_at',?,?)",
                (str(time.time()), time.time()),
            )

        client = UpscaleForgeClient(shared_path)
        self.assertTrue(client.is_online())
        request_id = client.enqueue(
            "/media/tv/low.mkv",
            device_id=1,
            inode=2,
            size_bytes=300,
            mtime_ns=400,
            target_height=720,
        )
        duplicate = client.enqueue(
            "/media/tv/low.mkv",
            device_id=1,
            inode=2,
            size_bytes=300,
            mtime_ns=400,
            target_height=720,
        )
        self.assertEqual(request_id, duplicate)
        self.assertEqual(client.get_job(request_id)["status"], "PENDING")
        self.assertEqual(client.get_job(request_id)["target_height"], 720)

        with sqlite3.connect(shared_path) as connection:
            connection.execute(
                "UPDATE manual_jobs SET status='PROCESSING', resolved_filepath=? WHERE request_id=?",
                ("/media/tv/low.mkv", request_id),
            )
            connection.execute(
                "INSERT INTO media_files(filepath,inode,size_bytes,status) VALUES(?,?,?,'PROCESSING')",
                ("/media/tv/low.mkv", 2, 300),
            )
            connection.execute(
                "INSERT INTO service_state(key,value,updated_at) VALUES('gpu_readiness',?,?)",
                ('{"ready":false,"reason":"Yielding to an external GPU workload."}', time.time()),
            )
            connection.executemany(
                """
                INSERT INTO segment_progress (
                    filepath,inode,size_bytes,segment_index,target_height,
                    duration_seconds,total_frames,progress_percent,status
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    ("/media/tv/low.mkv", 2, 300, 0, 720, 100.0, 1000, 25.0, "ENCODING"),
                    ("/media/tv/low.mkv", 2, 300, 1, 720, 100.0, 1000, 0.0, "PENDING"),
                ],
            )
            connection.execute(
                "UPDATE segment_progress SET status='INITIALIZING_MODEL', progress_percent=0 WHERE segment_index=0"
            )
        initializing = client.get_job(request_id)
        self.assertEqual(initializing["phase"], "INITIALIZING_MODEL")
        self.assertIn("TensorRT", initializing["phase_detail"])
        self.assertEqual(initializing["progress_percent"], 0.0)
        self.assertEqual(initializing["total_segments"], 2)
        self.assertEqual(initializing["active_segment_index"], 0)

        with sqlite3.connect(shared_path) as connection:
            connection.execute(
                "UPDATE segment_progress SET status='ENCODING', progress_percent=25 WHERE segment_index=0"
            )
        preparing = client.get_job(request_id)
        self.assertEqual(preparing["phase"], "ENCODING")
        self.assertAlmostEqual(preparing["progress_percent"], 11.75)
        self.assertAlmostEqual(preparing["frames_done"], 250.0)
        self.assertEqual(preparing["frames_total"], 2000)

        with sqlite3.connect(shared_path) as connection:
            connection.execute(
                "UPDATE media_files SET status='WAITING_GPU' WHERE filepath=?",
                ("/media/tv/low.mkv",),
            )
        waiting = client.get_job(request_id)
        self.assertEqual(waiting["phase"], "WAITING_GPU")
        self.assertEqual(waiting["wait_reason"], "Yielding to an external GPU workload.")

        with sqlite3.connect(shared_path) as connection:
            connection.execute(
                "UPDATE media_files SET status='PROCESSING' WHERE filepath=?",
                ("/media/tv/low.mkv",),
            )
            connection.execute(
                "UPDATE service_state SET value=?, updated_at=? WHERE key='gpu_readiness'",
                ('{"ready":true,"reason":null}', time.time()),
            )
        self.assertEqual(client.get_job(request_id)["phase"], "ENCODING")

        requested = client.cancel_job(request_id)
        self.assertEqual(requested["status"], "CANCEL_REQUESTED")
        active = client.find_active_job(
            device_id=1,
            inode=2,
            size_bytes=300,
            mtime_ns=400,
        )
        self.assertEqual(active["request_id"], request_id)
        self.assertEqual(client.cancel_job(request_id)["status"], "CANCEL_REQUESTED")
        with sqlite3.connect(shared_path) as connection:
            connection.execute(
                "UPDATE manual_jobs SET status='PENDING' WHERE request_id=?",
                (request_id,),
            )
        cancelled = client.cancel_job(request_id)
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertIsNotNone(cancelled["completed_at"])

    async def test_live_upscale_preview_serves_and_prunes_ephemeral_jpeg_pairs(self) -> None:
        source = self.root / "preview-source.mkv"
        source.write_bytes(b"preview source")
        source_stat = source.stat()
        await self.database.upsert_batch(
            [
                {
                    "file_path": str(source),
                    "file_name": source.name,
                    "directory": str(self.root),
                    "size_bytes": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "width": 640,
                    "height": 360,
                    "action_tag": "UPSCALE",
                }
            ]
        )
        row = await self.database.get_by_path(str(source))
        await self.database.update_file(
            row["id"],
            status="UPSCALING",
            upscale_request_id=42,
            upscale_target_height=1080,
        )
        worker = MediaWorker(self.database, self.settings_store, FakeWatchdog())
        worker.upscale_client = UpscaleForgeClient(self.root / "upscale-state.db")
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    database=self.database,
                    settings_store=self.settings_store,
                    scanner=None,
                    watchdog=FakeWatchdog(),
                    worker=worker,
                )
            )
        )
        preview_directory = worker.upscale_client.preview_directory(42)
        preview_directory.mkdir(parents=True)
        old_id = "frame_0000_00000003"
        current_id = "frame_0001_00000003"
        for preview_id in (old_id, current_id):
            (preview_directory / f"{preview_id}_before.jpg").write_bytes(b"before-jpeg")
            (preview_directory / f"{preview_id}_after.jpg").write_bytes(b"after-jpeg")

        status = await upscale_preview_status(row["id"], request)
        self.assertTrue(status["available"])
        self.assertEqual(status["preview_id"], current_id)
        self.assertTrue((preview_directory / ".viewer").is_file())
        before = await upscale_preview_image(row["id"], current_id, "before", request)
        after = await upscale_preview_image(row["id"], current_id, "after", request)
        self.assertEqual(before.body, b"before-jpeg")
        self.assertEqual(after.body, b"after-jpeg")

        displayed = await upscale_preview_displayed(row["id"], current_id, request)
        self.assertTrue(displayed["ok"])
        self.assertFalse((preview_directory / f"{old_id}_before.jpg").exists())
        self.assertTrue((preview_directory / f"{current_id}_before.jpg").exists())

        cleaned = await cleanup_upscale_preview(row["id"], request)
        self.assertTrue(cleaned["ok"])
        self.assertFalse(preview_directory.exists())

    async def test_ambient_camera_pid_does_not_count_internal_nvdec_load_as_external(self) -> None:
        watchdog = GpuWatchdog(self.settings_store)
        watchdog.device = object()
        watchdog.available = True
        watchdog.internal_pids.add(4242)
        watchdog._observed_external_pids.add(8000)
        watchdog._read_telemetry = lambda: watchdog._telemetry.update(
            core_utilization=5,
            decoder_utilization=100,
            encoder_utilization=0,
        )
        watchdog._external_gpu_pids = lambda _policy: {8000}
        yield_changes: list[bool] = []

        async def record_yield(paused: bool, _reason: str | None) -> None:
            yield_changes.append(paused)

        watchdog.on_yield = record_yield
        await watchdog._poll_once()

        self.assertFalse(watchdog.yielding)
        self.assertFalse(watchdog.snapshot()["external_busy"])
        self.assertEqual(yield_changes, [])

    async def test_thermal_yield_waits_for_cool_temperature_and_dwell(self) -> None:
        settings = await self.settings_store.load()
        await self.settings_store.save(settings.model_copy(update={"thermal_trip_dwell_sec": 0}))
        watchdog = GpuWatchdog(self.settings_store)
        watchdog.device = object()
        watchdog.available = True
        watchdog.internal_pids.add(4242)
        signals = []
        temperatures = {"value": 90}
        watchdog._read_telemetry = lambda: watchdog._telemetry.update(
            core_utilization=0,
            decoder_utilization=0,
            encoder_utilization=0,
            temperature_c=temperatures["value"],
        )
        watchdog._external_gpu_pids = lambda _policy: set()
        watchdog._signal_internal = lambda signum: signals.append(signum)
        yield_changes: list[tuple[bool, str | None]] = []

        async def record_yield(paused: bool, reason: str | None) -> None:
            yield_changes.append((paused, reason))

        watchdog.on_yield = record_yield
        await watchdog._poll_once()
        self.assertTrue(watchdog.yielding)
        self.assertEqual(watchdog.snapshot()["gpu_state"], "PAUSED_THERMAL")
        self.assertEqual(watchdog.snapshot()["thermal_pause_threshold_c"], 89)
        self.assertEqual(watchdog.snapshot()["thermal_resume_threshold_c"], 82)
        self.assertEqual(signals[-1], signal.SIGSTOP)

        temperatures["value"] = 85
        await watchdog._poll_once()
        self.assertTrue(watchdog.yielding)
        self.assertIsNone(watchdog._thermal_cooldown_started)

        temperatures["value"] = 82
        await watchdog._poll_once()
        self.assertTrue(watchdog.yielding)
        watchdog._thermal_cooldown_started = time.monotonic() - 31
        await watchdog._poll_once()

        self.assertFalse(watchdog.yielding)
        self.assertEqual(signals[-1], signal.SIGCONT)
        self.assertEqual(yield_changes, [(True, "thermal"), (False, None)])

    async def test_thermal_pause_waits_for_five_seconds_at_93c(self) -> None:
        settings = await self.settings_store.load()
        await self.settings_store.save(
            settings.model_copy(
                update={
                    "thermal_pause_c": 93,
                    "thermal_trip_dwell_sec": 5,
                    "thermal_cooldown_sec": 15,
                }
            )
        )
        watchdog = GpuWatchdog(self.settings_store)
        watchdog.device = object()
        watchdog.available = True
        watchdog.internal_pids.add(4242)
        temperatures = {"value": 93}
        signals = []
        watchdog._read_telemetry = lambda: watchdog._telemetry.update(
            core_utilization=0,
            decoder_utilization=0,
            encoder_utilization=0,
            temperature_c=temperatures["value"],
            thermal_slowdown_threshold_c=94,
            thermal_shutdown_threshold_c=97,
        )
        watchdog._external_gpu_pids = lambda _policy: set()
        watchdog._signal_internal = lambda signum: signals.append(signum)

        await watchdog._poll_once()

        self.assertFalse(watchdog.yielding)
        self.assertEqual(watchdog.snapshot()["thermal_pause_threshold_c"], 93)
        self.assertEqual(watchdog.snapshot()["thermal_resume_threshold_c"], 86)
        self.assertEqual(watchdog.snapshot()["thermal_trip_dwell_sec"], 5)
        self.assertEqual(signals, [])

        watchdog._thermal_trip_started = time.monotonic() - 5.1
        await watchdog._poll_once()
        self.assertTrue(watchdog.yielding)
        self.assertEqual(signals[-1], signal.SIGSTOP)

        temperatures["value"] = 89
        await watchdog._poll_once()
        self.assertIsNone(watchdog._thermal_cooldown_started)
        temperatures["value"] = 86
        await watchdog._poll_once()
        self.assertTrue(watchdog.yielding)
        watchdog._thermal_cooldown_started = time.monotonic() - 16
        await watchdog._poll_once()
        self.assertFalse(watchdog.yielding)
        self.assertEqual(signals[-1], signal.SIGCONT)


if __name__ == "__main__":
    unittest.main()
