"""CPU-only acceptance coverage for GPU command and transaction safeguards."""

from __future__ import annotations

import asyncio
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog
from app.main import restore_backup
from app.recovery import recover_startup
from app.worker import (
    CommandResult,
    MediaWorker,
    build_decode_command,
    build_transcode_command,
    final_output_path,
    output_temp_path,
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


if __name__ == "__main__":
    unittest.main()
