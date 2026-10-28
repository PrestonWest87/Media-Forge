"""FastAPI entrypoint, dashboard API, and ordered startup recovery."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog
from app.recovery import recover_startup
from app.routing import (
    UPSCALE_MAX_HEIGHT,
    UPSCALE_MAX_WIDTH,
    UPSCALE_TARGET_HEIGHTS,
    recommend_action,
)
from app.scanner import MediaScanner
from app.upscale_client import UpscaleServiceUnavailable
from app.worker import MediaWorker

logger = logging.getLogger("gpu_forge")
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"
UPSCALE_PREVIEW_ID = re.compile(r"frame_\d{4}_\d{8}\Z")

FILTER_STATUSES = {
    "active": [
        "PENDING",
        "CHECKING",
        "TRANSCODING",
        "VERIFYING_OUTPUT",
        "AUDIO_REMUXING",
        "AUDIO_VERIFYING",
        "AUDIO_TRANSCODING",
        "AUDIO_RESTORING",
        "PAUSED_YIELD",
        "UPSCALE_QUEUED",
        "UPSCALING",
        "UPSCALE_CANCEL_REQUESTED",
    ],
    "corrupt": ["CORRUPT"],
    "transcoded": ["TRANSCODED", "TRANSCODED_BACKUP_KEPT"],
    "upscaled": ["UPSCALED", "UPSCALED_BACKUP_KEPT"],
    "skipped": ["HEALTHY", "SKIPPED_OPTIMAL"],
    "unsupported": ["SKIPPED_HW_UNSUPPORTED"],
    "failed": ["TRANSCODE_FAILED_RESTORED", "UPSCALE_FAILED"],
    "cancelled": ["UPSCALE_CANCELLED"],
}
FILTER_ACTION_TAGS = {"upscale_candidates": ["UPSCALE"]}
FILTER_AUDIO_STATUSES = {
    "audio_pending": ["PENDING", "PROCESSING", "RESTORING", "BLOCKED_BACKUP", "FAILED"]
}


def _configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    log_path = os.environ.get("GPU_FORGE_LOG_FILE")
    if log_path:
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_target = str(target.resolve())
        if not any(
            getattr(handler, "baseFilename", None) == resolved_target
            for handler in root.handlers
        ):
            file_handler = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)


@asynccontextmanager
async def lifespan(application: FastAPI):
    _configure_logging()
    settings_store = SettingsStore()
    settings = await settings_store.load()
    database = Database()
    await database.initialize()
    logger.info(
        "GPU-Forge service startup",
        extra={"database_path": str(database.path), "media_roots": settings.media_roots},
    )

    # Recovery must finish before any pending work is put on the worker queue.
    recovery_notices = await recover_startup(database, settings.media_roots)
    if recovery_notices:
        logger.warning("Startup recovery completed %d action(s)", len(recovery_notices))

    scanner = MediaScanner(database)
    watchdog = GpuWatchdog(settings_store)
    worker = MediaWorker(database, settings_store, watchdog)
    watchdog.on_yield = worker.set_yielding
    await watchdog.start()
    application.state.settings_store = settings_store
    application.state.database = database
    application.state.scanner = scanner
    application.state.watchdog = watchdog
    application.state.worker = worker
    application.state.recovery_notices = recovery_notices
    startup_scan = asyncio.create_task(
        _run_startup_scan(scanner, settings.media_roots),
        name="gpu-forge-startup-scan",
    )
    application.state.startup_scan = startup_scan
    await worker.start()

    try:
        yield
    finally:
        logger.info("GPU-Forge service shutdown started")
        await worker.stop()
        await watchdog.stop()
        if not startup_scan.done():
            startup_scan.cancel()
            await asyncio.gather(startup_scan, return_exceptions=True)
        logger.info("GPU-Forge service shutdown completed")


async def _run_startup_scan(scanner: MediaScanner, roots: list[str]) -> None:
    try:
        result = await scanner.scan(roots)
        logger.info("Startup scan indexed %d media file(s)", result["scanned"])
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Initial media scan failed")


app = FastAPI(title="GPU-Forge", version="2.1.0", lifespan=lifespan)


def _service(request: Request) -> tuple[Database, SettingsStore, MediaScanner, GpuWatchdog, MediaWorker]:
    state = request.app.state
    return state.database, state.settings_store, state.scanner, state.watchdog, state.worker


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_INDEX)


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    watchdog = request.app.state.watchdog
    return {"ok": True, "gpu_available": watchdog.available, "gpu_error": watchdog.error}


@app.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    _database, settings_store, _scanner, _watchdog, _worker = _service(request)
    settings = await settings_store.load()
    return settings.model_dump(mode="json")


@app.put("/api/settings")
async def put_settings(settings: Settings, request: Request) -> dict[str, Any]:
    _database, settings_store, _scanner, watchdog, _worker = _service(request)
    previous = await settings_store.load()
    saved = await settings_store.save(settings)
    if previous.gpu_index != saved.gpu_index:
        await watchdog.stop()
        await watchdog.start()
    return saved.model_dump(mode="json")


@app.post("/api/scan")
async def scan(request: Request) -> dict[str, Any]:
    database, settings_store, scanner, _watchdog, worker = _service(request)
    settings = await settings_store.load()
    logger.info("manual library scan requested", extra={"roots": settings.media_roots})
    result = await scanner.scan(settings.media_roots)
    result["enqueued"] = await worker.enqueue_pending()
    result["audio_enqueued"] = await worker.enqueue_pending_audio()
    result["upscale_enqueued"] = (
        await worker.enqueue_bulk_upscale_candidates()
        if worker.bulk_upscale_queue_enabled
        else 0
    )
    result["total_files"] = await database.total_files()
    result["queue_enabled"] = worker.queue_enabled
    result["upscale_queue_enabled"] = worker.bulk_upscale_queue_enabled
    result["audio_queue_enabled"] = worker.audio_queue_enabled
    logger.info("manual library scan request completed", extra=result)
    return result


@app.post("/api/queue/start")
async def start_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    enqueued = await worker.start_queue()
    logger.info("bulk queue start requested", extra={"newly_queued": enqueued})
    return {"ok": True, "queue_enabled": True, "enqueued": enqueued}


@app.post("/api/queue/pause")
async def pause_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    worker.pause_queue()
    logger.info("bulk queue pause requested")
    return {"ok": True, "queue_enabled": False}


@app.post("/api/upscale-queue/start")
async def start_bulk_upscale_queue(
    request: Request,
    target_height: int = Query(default=1080, ge=720, le=1440),
) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    if target_height not in UPSCALE_TARGET_HEIGHTS:
        raise HTTPException(
            status_code=422,
            detail=f"Target resolution must be one of {UPSCALE_TARGET_HEIGHTS}p",
        )
    try:
        queued = await worker.start_bulk_upscale_queue(target_height)
    except UpscaleServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "Bulk upscale queue started",
        extra={"target_height": target_height, "queued": queued},
    )
    return {
        "ok": True,
        "upscale_queue_enabled": True,
        "target_height": target_height,
        "queued": queued,
    }


@app.post("/api/upscale-queue/pause")
async def pause_bulk_upscale_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    worker.pause_bulk_upscale_queue()
    logger.info("Bulk upscale queue paused")
    return {"ok": True, "upscale_queue_enabled": False}


@app.post("/api/audio-queue/start")
async def start_audio_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    enqueued = await worker.start_audio_queue()
    logger.info("Audio compatibility queue start requested", extra={"newly_queued": enqueued})
    return {"ok": True, "audio_queue_enabled": True, "enqueued": enqueued}


@app.post("/api/audio-queue/pause")
async def pause_audio_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    worker.pause_audio_queue()
    logger.info("Audio compatibility queue pause requested")
    return {"ok": True, "audio_queue_enabled": False}


@app.get("/api/telemetry")
async def telemetry(request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, watchdog, worker = _service(request)
    counts = await database.status_counts()
    audio_counts = await database.audio_status_counts()
    total = await database.total_files()
    active_count = sum(counts.get(status, 0) for status in FILTER_STATUSES["active"])
    files_checked = max(0, total - active_count)
    saved_bytes = await database.total_saved_bytes()
    held_backup_bytes = 0
    for row in await database.backup_files():
        backup_path = row.get("backup_path")
        if backup_path:
            try:
                held_backup_bytes += os.path.getsize(backup_path)
            except OSError:
                pass
    gpu = watchdog.snapshot()
    work = worker.snapshot()
    work["up_next"] = await worker.queue_preview(5)
    if not work["queue_enabled"]:
        work["queued"] = max(counts.get("PENDING", 0), int(work["bulk_queued"])) + int(
            work["manual_queued"]
        ) + int(work["audio_queued"]) + int(work["bulk_upscale_queued"])
    if work["audio_queue_enabled"]:
        work["queued"] += max(
            audio_counts.get("PENDING", 0)
            + audio_counts.get("FAILED", 0)
            + audio_counts.get("BLOCKED_BACKUP", 0),
            int(work["audio_queued"]),
        ) - int(work["audio_queued"])
    progress_units = float(files_checked)
    if work["current_file_id"] is not None:
        progress_units += min(100.0, max(0.0, float(work.get("current_progress_percent", 0.0)))) / 100.0
    progress = round(min(100.0, (progress_units / total) * 100.0), 2) if total else 0.0
    state = work["state"]
    if work["current_file_id"] is None:
        state = gpu["gpu_state"]
    return {
        "state": state,
        "gpu": gpu,
        "worker": work,
        "counts": counts,
        "audio_counts": audio_counts,
        "total_files": total,
        "files_checked": files_checked,
        "active_files": active_count,
        "progress_percent": progress,
        "saved_bytes": saved_bytes,
        "saved_gb": round(saved_bytes / (1024**3), 3),
        "held_backup_bytes": held_backup_bytes,
        "held_backup_gb": round(held_backup_bytes / (1024**3), 3),
    }


@app.get("/api/files")
async def list_files(
    request: Request,
    filter: str = Query(default="active"),
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=250, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, _worker = _service(request)
    if filter not in FILTER_STATUSES and filter not in FILTER_ACTION_TAGS and filter not in FILTER_AUDIO_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown file filter: {filter}")
    rows = await database.list_files(
        FILTER_STATUSES.get(filter),
        q,
        limit,
        offset,
        action_tags=FILTER_ACTION_TAGS.get(filter),
        audio_statuses=FILTER_AUDIO_STATUSES.get(filter),
    )
    for row in rows:
        actual_size = row.get("new_size_bytes") or row.get("size_bytes")
        row["actual_size_bytes"] = actual_size
        row["has_backup"] = bool(row.get("backup_path") and os.path.exists(row["backup_path"]))
    return {"files": rows, "filter": filter, "limit": limit, "offset": offset}


@app.post("/api/files/{file_id}/retry")
async def retry_file(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    if row["status"] in {"TRANSCODED", "TRANSCODED_BACKUP_KEPT"}:
        raise HTTPException(status_code=409, detail="Verified transcodes cannot be queued for retry")
    if row["status"] in {
        "UPSCALE_QUEUED",
        "UPSCALING",
        "UPSCALE_CANCEL_REQUESTED",
        "UPSCALE_CANCELLED",
        "UPSCALED",
        "UPSCALED_BACKUP_KEPT",
        "UPSCALE_FAILED",
    }:
        raise HTTPException(status_code=409, detail="Use the manual upscale action to retry or select this file")
    if row.get("backup_path") and os.path.exists(row["backup_path"]):
        raise HTTPException(status_code=409, detail="Resolve or purge the existing backup before retrying")
    await database.update_file(
        file_id,
        status="PENDING",
        backup_path=None,
        new_size_bytes=None,
        error_log=None,
    )
    enqueued = await worker.enqueue_pending()
    return {"ok": True, "enqueued": enqueued, "queue_enabled": worker.queue_enabled}


@app.post("/api/files/{file_id}/test")
async def test_file(file_id: int, request: Request) -> dict[str, Any]:
    return await _queue_selected_test(file_id, request, full_pipeline=False)


@app.post("/api/files/{file_id}/full-test")
async def full_test_file(file_id: int, request: Request) -> dict[str, Any]:
    return await _queue_selected_test(file_id, request, full_pipeline=True)


@app.post("/api/files/{file_id}/upscale")
async def upscale_file(
    file_id: int,
    request: Request,
    target_height: int = Query(default=1080, ge=720, le=1440),
) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    if row.get("backup_path") and os.path.exists(row["backup_path"]):
        raise HTTPException(status_code=409, detail="Resolve the retained backup before upscaling this file")
    if row["status"] in {
        "UPSCALE_QUEUED",
        "UPSCALING",
        "UPSCALE_CANCEL_REQUESTED",
        "UPSCALED",
        "UPSCALED_BACKUP_KEPT",
    }:
        raise HTTPException(status_code=409, detail="This file already has an upscale queued or completed")
    if (
        row.get("action_tag") != "UPSCALE"
        or not row.get("width")
        or not row.get("height")
        or int(row["width"]) > UPSCALE_MAX_WIDTH
        or int(row["height"]) > UPSCALE_MAX_HEIGHT
    ):
        raise HTTPException(status_code=409, detail="Manual upscaling is currently limited to candidates at or below 720×360")
    if target_height not in UPSCALE_TARGET_HEIGHTS:
        raise HTTPException(status_code=422, detail=f"Target resolution must be one of {UPSCALE_TARGET_HEIGHTS}p")
    if not os.path.isfile(row["file_path"]):
        raise HTTPException(status_code=404, detail="Media file is missing from disk")
    if not await worker.upscale_service_online():
        raise HTTPException(status_code=503, detail="Upscale-Forge is not running or its shared state database is unavailable")
    try:
        queued = await worker.enqueue_upscale(file_id, target_height)
    except Exception as exc:
        logger.exception("Could not queue manual upscale for %s", row["file_path"])
        raise HTTPException(status_code=500, detail="Could not queue the manual upscale") from exc
    if not queued:
        raise HTTPException(status_code=409, detail="This file is already active or queued")
    logger.info(
        "manual upscale accepted by API",
        extra={
            "file_id": file_id,
            "file_path": row["file_path"],
            "target_height": target_height,
            "priority_position": worker.upscale_queue_position(file_id),
        },
    )
    return {
        "ok": True,
        "queued": True,
        "priority": "upscale-before-healthcheck-and-transcode",
        "position": worker.upscale_queue_position(file_id),
        "target_height": target_height,
        "queue_enabled": worker.queue_enabled,
    }


@app.post("/api/files/{file_id}/upscale/cancel")
async def cancel_upscale_file(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    try:
        result = await worker.cancel_upscale(file_id)
    except UpscaleServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("manual upscale cancellation API completed", extra={"file_id": file_id, **result})
    return {"ok": True, **result}


@app.get("/api/files/{file_id}/upscale/preview")
async def upscale_preview_status(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    request_id = row.get("upscale_request_id")
    if request_id is None:
        return {"available": False}

    preview_directory = worker.upscale_client.preview_directory(int(request_id))
    if row.get("status") not in {
        "UPSCALE_QUEUED",
        "UPSCALING",
        "UPSCALE_CANCEL_REQUESTED",
    }:
        return {"available": False}
    try:
        preview_directory.mkdir(parents=True, exist_ok=True)
        (preview_directory / ".viewer").touch()
    except OSError:
        logger.warning(
            "could not refresh the upscale preview viewer lease",
            extra={"file_id": file_id, "request_id": int(request_id)},
            exc_info=True,
        )
        return {"available": False}
    before_frames = sorted(preview_directory.glob("frame_*_before.jpg"))
    for before_path in reversed(before_frames):
        preview_id = before_path.name[: -len("_before.jpg")]
        if UPSCALE_PREVIEW_ID.fullmatch(preview_id) and (
            preview_directory / f"{preview_id}_after.jpg"
        ).is_file():
            frame_match = re.fullmatch(r"frame_(\d{4})_(\d{8})", preview_id)
            assert frame_match is not None
            return {
                "available": True,
                "preview_id": preview_id,
                "segment_index": int(frame_match.group(1)),
                "frame_index": int(frame_match.group(2)),
            }
    return {"available": False}


@app.get("/api/files/{file_id}/upscale/preview/{preview_id}/{side}")
async def upscale_preview_image(
    file_id: int,
    preview_id: str,
    side: str,
    request: Request,
) -> Response:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    request_id = row.get("upscale_request_id")
    if request_id is None or side not in {"before", "after"}:
        raise HTTPException(status_code=404, detail="Upscale preview is unavailable")
    if UPSCALE_PREVIEW_ID.fullmatch(preview_id) is None:
        raise HTTPException(status_code=404, detail="Upscale preview is unavailable")

    path = worker.upscale_client.preview_directory(int(request_id)) / f"{preview_id}_{side}.jpg"
    try:
        content = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Upscale preview is no longer available") from exc
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/files/{file_id}/upscale/preview/{preview_id}/displayed")
async def upscale_preview_displayed(
    file_id: int,
    preview_id: str,
    request: Request,
) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    request_id = row.get("upscale_request_id")
    if request_id is None or UPSCALE_PREVIEW_ID.fullmatch(preview_id) is None:
        raise HTTPException(status_code=404, detail="Upscale preview is unavailable")

    preview_directory = worker.upscale_client.preview_directory(int(request_id))

    def mark_displayed() -> bool:
        before_path = preview_directory / f"{preview_id}_before.jpg"
        after_path = preview_directory / f"{preview_id}_after.jpg"
        if not before_path.is_file() or not after_path.is_file():
            return False
        marker = preview_directory / ".displayed"
        temporary_marker = preview_directory / ".displayed.tmp"
        try:
            temporary_marker.write_text(preview_id, encoding="ascii")
            os.replace(temporary_marker, marker)
            candidates = sorted(preview_directory.glob("frame_*_before.jpg"))
            latest_id = (
                candidates[-1].name[: -len("_before.jpg")] if candidates else preview_id
            )
            keep = {preview_id, latest_id}
            for old_before in candidates:
                old_id = old_before.name[: -len("_before.jpg")]
                if old_id not in keep:
                    old_before.unlink(missing_ok=True)
                    (preview_directory / f"{old_id}_after.jpg").unlink(missing_ok=True)
            return True
        except OSError:
            try:
                temporary_marker.unlink(missing_ok=True)
            except OSError:
                pass
            logger.debug("upscale preview was cleaned while acknowledging it", exc_info=True)
            return False

    displayed = await asyncio.to_thread(mark_displayed)
    if not displayed:
        raise HTTPException(status_code=404, detail="Upscale preview is no longer available")
    logger.debug(
        "Upscale preview displayed; unreferenced JPEGs removed",
        extra={"file_id": file_id, "request_id": int(request_id), "preview_id": preview_id},
    )
    return {"ok": True, "preview_id": preview_id}


@app.post("/api/files/{file_id}/upscale/preview/cleanup")
async def cleanup_upscale_preview(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    request_id = row.get("upscale_request_id")
    if request_id is not None:
        preview_directory = worker.upscale_client.preview_directory(int(request_id))
        await asyncio.to_thread(shutil.rmtree, preview_directory, True)
        logger.info(
            "Upscale preview files cleaned up",
            extra={"file_id": file_id, "request_id": int(request_id)},
        )
    return {"ok": True}


async def _queue_selected_test(file_id: int, request: Request, *, full_pipeline: bool) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    if row.get("backup_path") and os.path.exists(row["backup_path"]):
        raise HTTPException(status_code=409, detail="Restore or purge the retained backup before testing this file")
    if not os.path.isfile(row["file_path"]):
        raise HTTPException(status_code=404, detail="Media file is missing from disk")
    try:
        queued = await worker.enqueue_test(file_id, full_pipeline=full_pipeline)
    except Exception as exc:
        logger.exception("Could not queue selected-file test for %s", row["file_path"])
        raise HTTPException(status_code=500, detail="Could not queue the selected-file test") from exc
    if not queued:
        raise HTTPException(status_code=409, detail="This file is already active or has a test queued")
    return {
        "ok": True,
        "queued": True,
        "mode": "full_pipeline" if full_pipeline else "NVDEC health check only",
        "backup_policy": "keep" if full_pipeline else None,
        "queue_enabled": worker.queue_enabled,
    }


@app.post("/api/backups/purge")
async def purge_verified_backups(request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    purged: list[str] = []
    failures: list[dict[str, str]] = []
    for row in await database.backup_files():
        audio_backup = row.get("audio_status") == "FALLBACK_BACKUP_KEPT"
        if not audio_backup and row["status"] not in {
            "TRANSCODED_BACKUP_KEPT",
            "UPSCALED_BACKUP_KEPT",
        }:
            continue
        backup_path = row.get("backup_path")
        if not backup_path:
            continue
        try:
            os.unlink(backup_path)
            if audio_backup:
                await database.update_file(
                    row["id"],
                    backup_path=None,
                    audio_status="FALLBACK_ADDED",
                    audio_error=None,
                    audio_previous_status=None,
                )
            else:
                terminal_status = "UPSCALED" if row["status"] == "UPSCALED_BACKUP_KEPT" else "TRANSCODED"
                await database.update_file(
                    row["id"],
                    backup_path=None,
                    status=terminal_status,
                    error_log=None,
                    audio_status="PENDING" if row.get("audio_status") == "BLOCKED_BACKUP" else row.get("audio_status"),
                    audio_error=None if row.get("audio_status") == "BLOCKED_BACKUP" else row.get("audio_error"),
                )
            purged.append(backup_path)
        except FileNotFoundError:
            if audio_backup:
                await database.update_file(
                    row["id"],
                    backup_path=None,
                    audio_status="FALLBACK_ADDED",
                    audio_error=None,
                    audio_previous_status=None,
                )
            else:
                terminal_status = "UPSCALED" if row["status"] == "UPSCALED_BACKUP_KEPT" else "TRANSCODED"
                await database.update_file(
                    row["id"],
                    backup_path=None,
                    status=terminal_status,
                    error_log=None,
                    audio_status="PENDING" if row.get("audio_status") == "BLOCKED_BACKUP" else row.get("audio_status"),
                    audio_error=None if row.get("audio_status") == "BLOCKED_BACKUP" else row.get("audio_error"),
                )
        except OSError as exc:
            failures.append({"path": backup_path, "error": str(exc)})
    audio_enqueued = await worker.enqueue_pending_audio()
    return {"purged": purged, "failures": failures, "audio_enqueued": audio_enqueued}


@app.post("/api/files/{file_id}/restore")
async def restore_backup(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    backup_path = row.get("backup_path")
    audio_backup = row.get("audio_status") == "FALLBACK_BACKUP_KEPT"
    if not audio_backup and row["status"] not in {
        "TRANSCODED_BACKUP_KEPT",
        "UPSCALED_BACKUP_KEPT",
        "UPSCALE_FAILED",
    } or not backup_path:
        raise HTTPException(status_code=409, detail="This file has no retained verified backup")
    if not os.path.isfile(backup_path):
        raise HTTPException(status_code=404, detail="The retained backup is missing")

    source_info, probe_error = await worker._probe(backup_path)
    if probe_error or source_info is None:
        raise HTTPException(status_code=409, detail=f"Backup metadata could not be read: {probe_error}")

    file_path = row["file_path"]
    original_path = backup_path.removesuffix(".gpuforge_bak")
    restore_temp = file_path + ".gpuforge_tmp.restore"
    if os.path.lexists(restore_temp):
        raise HTTPException(status_code=409, detail="A previous restore temporary file needs startup recovery")
    if original_path != file_path and os.path.lexists(original_path):
        raise HTTPException(status_code=409, detail=f"Restore destination is occupied: {original_path}")
    moved_current = False
    await database.update_file(
        file_id,
        status="AUDIO_RESTORING" if audio_backup else "TRANSCODING",
        audio_status="RESTORING" if audio_backup else row.get("audio_status"),
        audio_previous_status=row["status"] if audio_backup else None,
        error_log="Manual backup restore is in progress.",
    )
    try:
        if os.path.exists(file_path):
            os.replace(file_path, restore_temp)
            moved_current = True
        os.replace(backup_path, original_path)
    except OSError as exc:
        if moved_current and os.path.exists(restore_temp):
            try:
                os.replace(restore_temp, file_path)
            except OSError:
                logger.exception("Could not restore the current transcode after rollback failure")
        await database.update_file(
            file_id,
            status=row["status"],
            audio_status="FALLBACK_BACKUP_KEPT" if audio_backup else row.get("audio_status"),
            audio_previous_status=None,
            error_log=f"Manual backup restore failed; retained backup remains available: {exc}",
        )
        raise HTTPException(status_code=500, detail=f"Backup restore failed: {exc}") from exc

    try:
        worker._fsync_directory(os.path.dirname(original_path) or ".")
    except OSError:
        logger.warning("Could not sync restored directory %s", os.path.dirname(original_path) or ".", exc_info=True)

    try:
        os.unlink(restore_temp)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove displaced transcode %s", restore_temp, exc_info=True)
    restored_stat = os.stat(original_path, follow_symlinks=False)
    action_tag, action_reason = recommend_action(source_info)
    restore_fields: dict[str, Any] = dict(
        file_path=original_path,
        file_name=os.path.basename(original_path),
        directory=os.path.dirname(original_path) or ".",
        backup_path=None,
        mtime=restored_stat.st_mtime,
        error_log="Restored from retained verified backup by user.",
        codec=source_info.get("codec_name"),
        pix_fmt=source_info.get("pix_fmt"),
        container_type=source_info.get("container_type"),
        format_name=source_info.get("format_name"),
        tagged_upscaled=int(bool(source_info.get("tagged_upscaled"))),
        bit_rate=source_info.get("bit_rate"),
        width=source_info.get("width"),
        height=source_info.get("height"),
        audio_streams=source_info.get("audio_streams", 0),
        sub_streams=source_info.get("sub_streams", 0),
        duration=source_info.get("duration"),
        action_tag=action_tag,
        action_reason=action_reason,
        audio_status="PENDING",
        audio_error=None,
        audio_previous_status=None,
        audio_swap_backup_path=None,
    )
    if audio_backup:
        was_video_processed = bool(row.get("transcoded_at") or row.get("upscaled_at"))
        restore_fields.update(
            {
                "size_bytes": row.get("size_bytes") if was_video_processed else restored_stat.st_size,
                "new_size_bytes": restored_stat.st_size if was_video_processed else None,
                "status": row["status"],
                "audio_status": "PENDING",
                "audio_error": None,
                "audio_previous_status": None,
            }
        )
        if row.get("transcoded_at") is not None:
            restore_fields["transcoded_at"] = row["transcoded_at"]
        if row.get("upscaled_at") is not None:
            restore_fields["upscaled_at"] = row["upscaled_at"]
        if row.get("upscale_request_id") is not None:
            restore_fields["upscale_request_id"] = row["upscale_request_id"]
        if row.get("upscale_target_height") is not None:
            restore_fields["upscale_target_height"] = row["upscale_target_height"]
        result_status = str(row["status"])
    else:
        restore_fields.update(
            {
                "size_bytes": restored_stat.st_size,
                "new_size_bytes": None,
                "status": "HEALTHY",
                "transcoded_at": None,
                "upscaled_at": None,
                "upscale_request_id": None,
                "upscale_target_height": None,
            }
        )
        result_status = "HEALTHY"
    await database.update_file(file_id, **restore_fields)
    audio_enqueued = await worker.enqueue_pending_audio()
    logger.info(
        "Retained media backup restored",
        extra={"file_id": file_id, "file_path": original_path, "audio_requeued": audio_enqueued > 0},
    )
    return {
        "ok": True,
        "file_path": original_path,
        "status": result_status,
        "audio_enqueued": audio_enqueued,
    }
