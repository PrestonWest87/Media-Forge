"""Startup cleanup and rollback for interrupted file swaps."""

from __future__ import annotations

import logging
import os

from app.db import Database
from app.scanner import iter_artifact_files

logger = logging.getLogger(__name__)
BACKUP_SUFFIX = ".gpuforge_bak"
TEMP_MARKER = ".gpuforge_tmp"
INTERRUPTED_STATUSES = {"CHECKING", "TRANSCODING", "VERIFYING_OUTPUT", "PAUSED_YIELD"}


def _is_present_nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


async def recover_startup(database: Database, roots: list[str]) -> list[str]:
    """Delete abandoned temps and restore any interrupted source-file swap."""

    notices: list[str] = []
    rows = await database.all_files()
    artifact_files = {path for path, _stat in iter_artifact_files(roots)}
    # A configured media root may have changed since an interrupted job. Use
    # stored paths too, so its temp output is still cleaned on the next boot.
    for row in rows:
        parent = os.path.dirname(row["file_path"]) or "."
        prefix = os.path.basename(row["file_path"]) + TEMP_MARKER
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.name.startswith(prefix) and entry.is_file(follow_symlinks=False):
                        artifact_files.add(os.path.abspath(entry.path))
        except OSError:
            pass

    for path in artifact_files:
        if TEMP_MARKER not in os.path.basename(path):
            continue
        try:
            os.unlink(path)
            notice = f"Removed abandoned temporary output: {path}"
            notices.append(notice)
            logger.warning(notice)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("Unable to remove abandoned temporary output %s: %s", path, exc)

    by_path = {row["file_path"]: row for row in rows}
    by_backup = {row["backup_path"]: row for row in rows if row.get("backup_path")}
    backup_paths = {path for path, _stat in iter_artifact_files(roots) if path.endswith(BACKUP_SUFFIX)}
    backup_paths.update(path for path in by_backup if path and os.path.exists(path))

    for backup_path in sorted(backup_paths):
        if not os.path.isfile(backup_path):
            continue
        original_path = backup_path[: -len(BACKUP_SUFFIX)]
        row = by_backup.get(backup_path) or by_path.get(original_path)
        status = row.get("status") if row else None
        current_path = row["file_path"] if row else original_path
        primary_ok = _is_present_nonempty(current_path)

        if status == "TRANSCODED_BACKUP_KEPT" and primary_ok:
            continue

        # An in-progress row means the rename transaction did not reach its
        # durable terminal state. Prefer the original over an uncommitted output.
        should_restore = not primary_ok or status in INTERRUPTED_STATUSES
        if should_restore:
            try:
                if current_path != original_path and os.path.exists(current_path):
                    # The new Matroska destination may have been installed
                    # before the process crashed. It was not committed in the
                    # database, so discard it while restoring the source name.
                    os.unlink(current_path)
                os.replace(backup_path, original_path)
                stat_result = os.stat(original_path)
                notice = f"Restored original media from interrupted backup: {original_path}"
                notices.append(notice)
                logger.warning(notice)
                if row:
                    await database.update_file(
                        row["id"],
                        file_path=original_path,
                        file_name=os.path.basename(original_path),
                        directory=os.path.dirname(original_path) or ".",
                        backup_path=None,
                        size_bytes=stat_result.st_size,
                        new_size_bytes=None,
                        mtime=stat_result.st_mtime,
                        status="TRANSCODE_FAILED_RESTORED",
                        error_log="Startup recovery restored the original file after an interrupted replacement.",
                    )
            except OSError as exc:
                notice = f"Could not restore backup {backup_path}: {exc}"
                notices.append(notice)
                logger.exception(notice)
            continue

        if status == "TRANSCODED" and primary_ok:
            # The database reached the committed state; this is an auto-clean
            # backup left behind by a crash between the status update and unlink.
            try:
                os.unlink(backup_path)
                if row:
                    await database.update_file(row["id"], backup_path=None)
                notice = f"Removed committed auto-clean backup: {backup_path}"
                notices.append(notice)
                logger.info(notice)
            except OSError as exc:
                logger.error("Unable to remove committed backup %s: %s", backup_path, exc)

    # Finish backup bookkeeping if a purge or auto-clean unlink completed just
    # before the process stopped, but its following database write did not.
    for row in await database.all_files():
        backup_path = row.get("backup_path")
        if row["status"] not in {"TRANSCODED", "TRANSCODED_BACKUP_KEPT"}:
            continue
        if not backup_path or os.path.exists(backup_path) or not _is_present_nonempty(row["file_path"]):
            continue
        await database.update_file(
            row["id"],
            backup_path=None,
            status="TRANSCODED",
            error_log=None,
        )
        notice = f"Cleared missing committed backup reference: {backup_path}"
        notices.append(notice)
        logger.info(notice)

    # Jobs interrupted before the source was moved have no backup to restore.
    for row in await database.all_files():
        if row["status"] not in INTERRUPTED_STATUSES:
            continue
        if row.get("backup_path") and os.path.exists(row["backup_path"]):
            continue
        if row.get("backup_path"):
            original_path = row["backup_path"][: -len(BACKUP_SUFFIX)] if row["backup_path"].endswith(BACKUP_SUFFIX) else None
            if original_path and original_path != row["file_path"] and _is_present_nonempty(original_path):
                original_stat = os.stat(original_path)
                await database.update_file(
                    row["id"],
                    file_path=original_path,
                    file_name=os.path.basename(original_path),
                    directory=os.path.dirname(original_path) or ".",
                    backup_path=None,
                    size_bytes=original_stat.st_size,
                    new_size_bytes=None,
                    mtime=original_stat.st_mtime,
                    codec=None,
                    pix_fmt=None,
                    bit_rate=None,
                    width=None,
                    height=None,
                    audio_streams=0,
                    sub_streams=0,
                    duration=None,
                    status="HEALTHY",
                    error_log="Startup finalized a manual restore from a previously verified backup.",
                )
                notice = f"Finalized interrupted manual restore: {original_path}"
                notices.append(notice)
                logger.warning(notice)
                continue
        if not _is_present_nonempty(row["file_path"]):
            continue
        await database.update_file(
            row["id"],
            backup_path=None,
            status="PENDING",
            new_size_bytes=None,
            error_log=None,
        )
        notice = f"Requeued interrupted job: {row['file_path']}"
        notices.append(notice)
        logger.warning(notice)

    return notices
