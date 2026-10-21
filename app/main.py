"""FastAPI entrypoint, dashboard API, and ordered startup recovery."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import Settings, SettingsStore
from app.db import Database
from app.gpu_watchdog import GpuWatchdog
from app.recovery import recover_startup
from app.scanner import MediaScanner
from app.worker import MediaWorker

logger = logging.getLogger("gpu_forge")
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

FILTER_STATUSES = {
    "active": ["PENDING", "CHECKING", "TRANSCODING", "VERIFYING_OUTPUT", "PAUSED_YIELD"],
    "corrupt": ["CORRUPT"],
    "transcoded": ["TRANSCODED", "TRANSCODED_BACKUP_KEPT"],
    "skipped": ["HEALTHY", "SKIPPED_OPTIMAL"],
    "unsupported": ["SKIPPED_HW_UNSUPPORTED"],
    "failed": ["TRANSCODE_FAILED_RESTORED"],
}


@asynccontextmanager
async def lifespan(application: FastAPI):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings_store = SettingsStore()
    settings = await settings_store.load()
    database = Database()
    await database.initialize()

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
        await worker.stop()
        await watchdog.stop()
        if not startup_scan.done():
            startup_scan.cancel()
            await asyncio.gather(startup_scan, return_exceptions=True)


async def _run_startup_scan(scanner: MediaScanner, roots: list[str]) -> None:
    try:
        result = await scanner.scan(roots)
        logger.info("Startup scan indexed %d media file(s)", result["scanned"])
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Initial media scan failed")


app = FastAPI(title="GPU-Forge", version="2.0.0", lifespan=lifespan)


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
    result = await scanner.scan(settings.media_roots)
    result["enqueued"] = await worker.enqueue_pending()
    result["total_files"] = await database.total_files()
    result["queue_enabled"] = worker.queue_enabled
    return result


@app.post("/api/queue/start")
async def start_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    enqueued = await worker.start_queue()
    return {"ok": True, "queue_enabled": True, "enqueued": enqueued}


@app.post("/api/queue/pause")
async def pause_queue(request: Request) -> dict[str, Any]:
    _database, _settings_store, _scanner, _watchdog, worker = _service(request)
    worker.pause_queue()
    return {"ok": True, "queue_enabled": False}


@app.get("/api/telemetry")
async def telemetry(request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, watchdog, worker = _service(request)
    counts = await database.status_counts()
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
    if not work["queue_enabled"]:
        work["queued"] = max(counts.get("PENDING", 0), int(work["queued"]))
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
    if filter not in FILTER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown file filter: {filter}")
    rows = await database.list_files(FILTER_STATUSES[filter], q, limit, offset)
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
    database, _settings_store, _scanner, _watchdog, _worker = _service(request)
    purged: list[str] = []
    failures: list[dict[str, str]] = []
    for row in await database.backup_files():
        if row["status"] != "TRANSCODED_BACKUP_KEPT":
            continue
        backup_path = row.get("backup_path")
        if not backup_path:
            continue
        try:
            os.unlink(backup_path)
            await database.update_file(row["id"], backup_path=None, status="TRANSCODED", error_log=None)
            purged.append(backup_path)
        except FileNotFoundError:
            await database.update_file(row["id"], backup_path=None, status="TRANSCODED", error_log=None)
        except OSError as exc:
            failures.append({"path": backup_path, "error": str(exc)})
    return {"purged": purged, "failures": failures}


@app.post("/api/files/{file_id}/restore")
async def restore_backup(file_id: int, request: Request) -> dict[str, Any]:
    database, _settings_store, _scanner, _watchdog, worker = _service(request)
    row = await database.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    backup_path = row.get("backup_path")
    if row["status"] != "TRANSCODED_BACKUP_KEPT" or not backup_path:
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
    await database.update_file(file_id, status="TRANSCODING", error_log="Manual backup restore is in progress.")
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
            status="TRANSCODED_BACKUP_KEPT",
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
    await database.update_file(
        file_id,
        file_path=original_path,
        file_name=os.path.basename(original_path),
        directory=os.path.dirname(original_path) or ".",
        backup_path=None,
        size_bytes=restored_stat.st_size,
        new_size_bytes=None,
        mtime=restored_stat.st_mtime,
        status="HEALTHY",
        error_log="Restored from retained verified backup by user.",
        codec=source_info.get("codec_name"),
        pix_fmt=source_info.get("pix_fmt"),
        bit_rate=source_info.get("bit_rate"),
        width=source_info.get("width"),
        height=source_info.get("height"),
        audio_streams=source_info.get("audio_streams", 0),
        sub_streams=source_info.get("sub_streams", 0),
        duration=source_info.get("duration"),
    )
    return {"ok": True, "file_path": original_path, "status": "HEALTHY"}
