"""Fast, non-following filesystem traversal for media roots."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.db import Database

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
ARTIFACT_MARKERS = (".gpuforge_tmp", ".gpuforge_bak")


def is_gpu_forge_artifact(name: str) -> bool:
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
    def __init__(self, database: Database) -> None:
        self.database = database
        self._scan_lock = asyncio.Lock()

    async def scan(self, roots: list[str]) -> dict[str, Any]:
        async with self._scan_lock:
            return await self._scan_locked(roots)

    async def _scan_locked(self, roots: list[str]) -> dict[str, Any]:
        batch: list[dict[str, Any]] = []
        scanned = 0
        inserted_or_updated = 0
        for file_path, stat_result in iter_tree_files(roots, media_only=True):
            scanned += 1
            batch.append(
                {
                    "file_path": file_path,
                    "file_name": os.path.basename(file_path),
                    "directory": os.path.dirname(file_path),
                    "size_bytes": stat_result.st_size,
                    "mtime": stat_result.st_mtime,
                }
            )
            if len(batch) >= 500:
                inserted_or_updated += await self.database.upsert_batch(batch)
                batch.clear()
        if batch:
            inserted_or_updated += await self.database.upsert_batch(batch)
        return {"scanned": scanned, "queued_candidates": inserted_or_updated}
