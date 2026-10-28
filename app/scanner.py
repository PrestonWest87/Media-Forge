"""Fast, non-following filesystem traversal for media roots."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.db import Database
from app.probe import metadata_db_fields, probe_file
from app.routing import recommend_action

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {
    ".3gp",
    ".asf",
    ".avi",
    ".flv",
    ".m2ts",
    ".m2v",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".mxf",
    ".ogv",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}
ARTIFACT_MARKERS = (
    ".gpuforge_tmp",
    ".gpuforge_bak",
    ".gpuforge_audio_bak",
    ".upscale_staging",
)


def is_gpu_forge_artifact(name: str) -> bool:
    """Identify temp/backup paths from either media-processing project."""
    return any(marker in name for marker in ARTIFACT_MARKERS)


def iter_tree_files(
    roots: list[str], media_only: bool = False, include_artifacts: bool = False
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield regular files without following directory or file symlinks."""

    pending = [os.path.abspath(root) for root in roots]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not include_artifacts and is_gpu_forge_artifact(entry.name):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if is_gpu_forge_artifact(entry.name):
                                continue
                            pending.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if media_only and Path(entry.name).suffix.lower() not in VIDEO_EXTENSIONS:
                            continue
                        yield os.path.abspath(entry.path), entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        logger.warning("Unable to inspect %s: %s", entry.path, exc)
        except (FileNotFoundError, NotADirectoryError):
            logger.warning("Media root is unavailable: %s", directory)
        except PermissionError as exc:
            logger.warning("Permission denied while scanning %s: %s", directory, exc)
        except OSError as exc:
            logger.warning("Unable to scan %s: %s", directory, exc)


def iter_artifact_files(roots: list[str]) -> Iterator[tuple[str, os.stat_result]]:
    """Yield only GPU-Forge temp/backup files during startup recovery scans."""

    pending = [os.path.abspath(root) for root in roots]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_gpu_forge_artifact(entry.name):
                                pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and is_gpu_forge_artifact(entry.name):
                            yield os.path.abspath(entry.path), entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        logger.warning("Unable to inspect %s: %s", entry.path, exc)
        except (FileNotFoundError, NotADirectoryError):
            logger.warning("Media root is unavailable: %s", directory)
        except PermissionError as exc:
            logger.warning("Permission denied while scanning %s: %s", directory, exc)
        except OSError as exc:
            logger.warning("Unable to scan %s: %s", directory, exc)


class MediaScanner:
    def __init__(
        self,
        database: Database,
        *,
        ffprobe: str = "ffprobe",
        probe_concurrency: int = 8,
    ) -> None:
        self.database = database
        self.ffprobe = ffprobe
        self.probe_concurrency = max(1, int(probe_concurrency))
        self._scan_lock = asyncio.Lock()

    async def scan(self, roots: list[str]) -> dict[str, Any]:
        async with self._scan_lock:
            return await self._scan_locked(roots)

    async def _scan_locked(self, roots: list[str]) -> dict[str, Any]:
        scan_started = time.monotonic()
        logger.info("GPU-Forge library scan started", extra={"roots": roots})
        existing = {row["file_path"]: row for row in await self.database.all_files()}
        candidates: list[dict[str, Any]] = []
        for file_path, stat_result in iter_tree_files(roots, media_only=True):
            previous = existing.get(file_path)
            recorded_size = None
            if previous:
                recorded_size = (
                    previous["new_size_bytes"]
                    if previous["new_size_bytes"] is not None
                    else previous["size_bytes"]
                )
            fingerprint_matches = bool(
                previous
                and recorded_size == stat_result.st_size
                and float(previous["mtime"]) == stat_result.st_mtime
            )
            has_probe_metadata = bool(
                previous
                and previous.get("codec")
                and previous.get("container_type")
                and previous.get("width") is not None
                and previous.get("height") is not None
            )
            candidates.append(
                {
                    "file_path": file_path,
                    "file_name": os.path.basename(file_path),
                    "directory": os.path.dirname(file_path),
                    "size_bytes": stat_result.st_size,
                    "mtime": stat_result.st_mtime,
                    "previous": previous,
                    "probe_required": not (fingerprint_matches and has_probe_metadata),
                }
            )

        semaphore = asyncio.Semaphore(self.probe_concurrency)
        probed = 0
        probe_failed = 0
        action_counts = {"TRANSCODE": 0, "LEAVE_ALONE": 0, "UPSCALE": 0}

        async def prepare(
            item: dict[str, Any],
        ) -> tuple[dict[str, Any] | None, bool, bool, str | None]:
            previous = item.get("previous")
            if previous and (
                previous.get("status") == "UPSCALING"
                or previous.get("status") == "AUDIO_TRANSCODING"
                or previous.get("status") == "AUDIO_RESTORING"
                or previous.get("audio_status") == "PROCESSING"
                or previous.get("audio_status") == "RESTORING"
            ):
                # An external upscale or the audio worker owns replacement
                # while active. A concurrent scan must not reset its row.
                return None, False, False, str(previous.get("action_tag") or "UPSCALE")
            previous = item.pop("previous")
            probe_required = bool(item.pop("probe_required"))
            if probe_required:
                async with semaphore:
                    logger.debug("probing media metadata", extra={"file_path": item["file_path"]})
                    info, error = await probe_file(item["file_path"], ffprobe=self.ffprobe)
                did_probe = True
            else:
                info = self._info_from_row(previous)
                error = None
                did_probe = False

            if info is not None:
                item.update(metadata_db_fields(info))
                action_tag, action_reason = recommend_action(info)
                item["action_tag"] = action_tag
                item["action_reason"] = action_reason
            else:
                item.update(
                    {
                        "action_tag": "LEAVE_ALONE",
                        "action_reason": "Metadata probe failed; the worker will retry before processing.",
                    }
                )
            if error:
                item["error_log"] = f"Scan metadata probe failed: {error}"
                logger.warning(
                    "media metadata probe failed",
                    extra={"file_path": item["file_path"], "error": error},
                )
            return item, did_probe, bool(error), None

        inserted_or_updated = 0
        prepared_batch: list[dict[str, Any]] = []
        tasks = [asyncio.create_task(prepare(item)) for item in candidates]
        try:
            for completed in asyncio.as_completed(tasks):
                row, did_probe, failed, held_action = await completed
                if row is None:
                    action_counts[held_action or "UPSCALE"] += 1
                    continue
                prepared_batch.append(row)
                probed += int(did_probe)
                probe_failed += int(failed)
                action_counts[row["action_tag"]] += 1
                if len(prepared_batch) >= 500:
                    inserted_or_updated += await self.database.upsert_batch(prepared_batch)
                    prepared_batch.clear()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if prepared_batch:
            inserted_or_updated += await self.database.upsert_batch(prepared_batch)
        result = {
            "scanned": len(candidates),
            "queued_candidates": inserted_or_updated,
            "probed": probed,
            "probe_failed": probe_failed,
            "action_counts": action_counts,
        }
        logger.info(
            "GPU-Forge library scan completed",
            extra={
                **result,
                "roots": roots,
                "elapsed_seconds": round(time.monotonic() - scan_started, 2),
            },
        )
        return result

    @staticmethod
    def _info_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "codec_name": row.get("codec"),
            "pix_fmt": row.get("pix_fmt"),
            "container_type": row.get("container_type"),
            "format_name": row.get("format_name"),
            "tagged_upscaled": bool(row.get("tagged_upscaled")),
            "bit_rate": row.get("bit_rate"),
            "width": row.get("width"),
            "height": row.get("height"),
            "audio_streams": row.get("audio_streams") or 0,
            "sub_streams": row.get("sub_streams") or 0,
            "duration": row.get("duration"),
        }
