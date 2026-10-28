"""Startup cleanup and rollback for interrupted file swaps."""

from __future__ import annotations

import logging
import os

from app.db import Database
from app.scanner import iter_artifact_files

logger = logging.getLogger(__name__)
BACKUP_SUFFIX = ".gpuforge_bak"
TEMP_MARKER = ".gpuforge_tmp"
INTERRUPTED_STATUSES = {
    "CHECKING",
    "TRANSCODING",
    "VERIFYING_OUTPUT",
    "AUDIO_REMUXING",
    "AUDIO_VERIFYING",
    "AUDIO_TRANSCODING",
    "AUDIO_RESTORING",
    "PAUSED_YIELD",
    "UPSCALING",
}


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

    for row in rows:
        swap_path = row.get("audio_swap_backup_path")
        if not swap_path:
            continue
        previous_status = row.get("audio_previous_status") or row.get("status") or "PENDING"
        if row.get("audio_status") == "PROCESSING":
            source_path = swap_path.removesuffix(".gpuforge_audio_bak")
            destination_path = (
                source_path
                if source_path.lower().endswith((".mp4", ".m4v", ".mkv"))
                else os.path.splitext(source_path)[0] + ".mkv"
            )
            try:
                if destination_path != source_path and os.path.exists(destination_path):
                    os.unlink(destination_path)
                if os.path.exists(swap_path):
                    if source_path != destination_path and os.path.exists(source_path):
                        os.unlink(source_path)
                    os.replace(swap_path, source_path)
                restored_stat = os.stat(source_path, follow_symlinks=False)
                await database.update_file(
                    row["id"],
                    file_path=source_path,
                    file_name=os.path.basename(source_path),
                    directory=os.path.dirname(source_path) or ".",
                    mtime=restored_stat.st_mtime,
                    status=previous_status,
                    audio_status="PENDING",
                    audio_error="Startup recovery rolled back an interrupted audio remux.",
                    audio_previous_status=None,
                    audio_swap_backup_path=None,
                )
                notice = f"Restored current media after interrupted audio remux: {source_path}"
                notices.append(notice)
                logger.warning(notice)
            except OSError as exc:
                notice = f"Could not restore audio rollback file {swap_path}: {exc}"
                notices.append(notice)
                logger.exception(notice)
        else:
            try:
                os.unlink(swap_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Unable to remove committed audio rollback file %s: %s", swap_path, exc)
            await database.update_file(row["id"], audio_swap_backup_path=None)

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

        if row and row.get("audio_status") == "RESTORING":
            try:
                if current_path != original_path and os.path.exists(current_path):
                    os.unlink(current_path)
                if os.path.exists(backup_path):
                    os.replace(backup_path, original_path)
                restored_stat = os.stat(original_path, follow_symlinks=False)
                previous_status = row.get("audio_previous_status") or "HEALTHY"
                await database.update_file(
                    row["id"],
                    file_path=original_path,
                    file_name=os.path.basename(original_path),
                    directory=os.path.dirname(original_path) or ".",
                    backup_path=None,
                    new_size_bytes=restored_stat.st_size if row.get("transcoded_at") else None,
                    mtime=restored_stat.st_mtime,
                    status=previous_status,
                    audio_status="PENDING",
                    audio_error="Startup recovery completed an interrupted audio-backup restore.",
                    audio_previous_status=None,
                )
                notice = f"Recovered audio-backup restore: {original_path}"
                notices.append(notice)
                logger.warning(notice)
            except OSError as exc:
                notice = f"Could not recover audio-backup restore {backup_path}: {exc}"
                notices.append(notice)
                logger.exception(notice)
            continue

        if row and status == "AUDIO_TRANSCODING":
            try:
                destination_path = os.path.splitext(current_path)[0] + ".mkv" if not current_path.lower().endswith((".mp4", ".m4v", ".mkv")) else current_path
                if destination_path != original_path and os.path.exists(destination_path):
                    os.unlink(destination_path)
                if current_path != original_path and os.path.exists(current_path):
                    os.unlink(current_path)
                os.replace(backup_path, original_path)
                restored_stat = os.stat(original_path, follow_symlinks=False)
                previous_status = row.get("audio_previous_status") or "PENDING"
                await database.update_file(
                    row["id"],
                    file_path=original_path,
                    file_name=os.path.basename(original_path),
                    directory=os.path.dirname(original_path) or ".",
                    backup_path=None,
                    mtime=restored_stat.st_mtime,
                    status=previous_status,
                    audio_status="PENDING",
                    audio_error="Startup recovery rolled back an interrupted audio replacement.",
                    audio_previous_status=None,
                    audio_swap_backup_path=None,
                )
                notice = f"Restored media after interrupted audio replacement: {original_path}"
                notices.append(notice)
                logger.warning(notice)
            except OSError as exc:
                notice = f"Could not recover interrupted audio replacement {backup_path}: {exc}"
                notices.append(notice)
                logger.exception(notice)
            continue

        if (
            row
            and status in INTERRUPTED_STATUSES
            and row.get("new_size_bytes") is not None
            and primary_ok
        ):
            try:
                output_stat = os.stat(current_path, follow_symlinks=False)
            except OSError:
                output_stat = None
            if (
                output_stat is not None
                and output_stat.st_size == row["new_size_bytes"]
                and float(output_stat.st_mtime) == float(row["mtime"])
            ):
                kept_backup = os.path.exists(backup_path)
                recovered_status = "TRANSCODED_BACKUP_KEPT" if kept_backup else "TRANSCODED"
                await database.update_file(
                    row["id"],
                    status=recovered_status,
                    backup_path=backup_path if kept_backup else None,
                    error_log=None,
                )
                notice = f"Restored committed transcode state after interruption: {current_path}"
                notices.append(notice)
                logger.info(notice)
                continue

        if row and row.get("audio_status") == "FALLBACK_BACKUP_KEPT" and primary_ok:
            continue

        if status in {"TRANSCODED_BACKUP_KEPT", "UPSCALED_BACKUP_KEPT"} and primary_ok:
            continue

        # An in-progress row means the rename transaction did not reach its
        # durable terminal state. Prefer the original over an uncommitted output.
        should_restore = (
            not primary_ok
            or status in INTERRUPTED_STATUSES
            or status == "UPSCALE_FAILED"
        )
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
                    upscale_recovery = status in {
                        "UPSCALING",
                        "UPSCALE_FAILED",
                        "UPSCALED",
                        "UPSCALED_BACKUP_KEPT",
                    }
                    await database.update_file(
                        row["id"],
                        file_path=original_path,
                        file_name=os.path.basename(original_path),
                        directory=os.path.dirname(original_path) or ".",
                        backup_path=None,
                        size_bytes=stat_result.st_size,
                        new_size_bytes=None,
                        mtime=stat_result.st_mtime,
                        status="UPSCALE_FAILED" if upscale_recovery else "TRANSCODE_FAILED_RESTORED",
                        error_log=(
                            "Startup recovery restored the original from an interrupted or failed upscale."
                            if upscale_recovery
                            else "Startup recovery restored the original file after an interrupted replacement."
                        ),
                        audio_status="PENDING",
                        audio_error=None,
                        audio_previous_status=None,
                        audio_swap_backup_path=None,
                        transcoded_at=None,
                        upscaled_at=None,
                        upscale_request_id=None,
                        upscale_target_height=None,
                    )
            except OSError as exc:
                notice = f"Could not restore backup {backup_path}: {exc}"
                notices.append(notice)
                logger.exception(notice)
            continue

        if status == "UPSCALED" and primary_ok:
            await database.update_file(
                row["id"],
                status="UPSCALED_BACKUP_KEPT",
                backup_path=backup_path,
                error_log=None,
            )
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
        if row.get("audio_status") == "FALLBACK_BACKUP_KEPT":
            backup_path = row.get("backup_path")
            if backup_path and not os.path.exists(backup_path) and _is_present_nonempty(row["file_path"]):
                await database.update_file(
                    row["id"],
                    backup_path=None,
                    audio_status="FALLBACK_ADDED",
                    audio_error=None,
                )
                notice = f"Cleared missing audio-operation backup reference: {backup_path}"
                notices.append(notice)
                logger.info(notice)
            continue
        if row.get("audio_status") == "RESTORING":
            backup_path = row.get("backup_path")
            original_path = (
                backup_path.removesuffix(BACKUP_SUFFIX)
                if backup_path and backup_path.endswith(BACKUP_SUFFIX)
                else row["file_path"]
            )
            if not backup_path or not os.path.exists(backup_path):
                if _is_present_nonempty(original_path):
                    restored_stat = os.stat(original_path, follow_symlinks=False)
                    await database.update_file(
                        row["id"],
                        file_path=original_path,
                        file_name=os.path.basename(original_path),
                        directory=os.path.dirname(original_path) or ".",
                        backup_path=None,
                        new_size_bytes=restored_stat.st_size if row.get("transcoded_at") else None,
                        mtime=restored_stat.st_mtime,
                        status=row.get("audio_previous_status") or row["status"],
                        audio_status="PENDING",
                        audio_error="Startup recovery completed an interrupted audio-backup restore.",
                        audio_previous_status=None,
                    )
            continue
        if row["status"] not in {
            "TRANSCODED",
            "TRANSCODED_BACKUP_KEPT",
            "UPSCALED",
            "UPSCALED_BACKUP_KEPT",
        }:
            continue
        if not backup_path or os.path.exists(backup_path) or not _is_present_nonempty(row["file_path"]):
            continue
        terminal_status = "UPSCALED" if row["status"] == "UPSCALED_BACKUP_KEPT" else "TRANSCODED"
        await database.update_file(row["id"], backup_path=None, status=terminal_status, error_log=None)
        notice = f"Cleared missing committed backup reference: {backup_path}"
        notices.append(notice)
        logger.info(notice)

    # Jobs interrupted before the source was moved have no backup to restore.
    for row in await database.all_files():
        if row["status"] in {"UPSCALE_QUEUED", "UPSCALING", "UPSCALE_CANCEL_REQUESTED"}:
            if not _is_present_nonempty(row["file_path"]):
                await database.update_file(
                    row["id"],
                    status="UPSCALE_FAILED",
                    error_log="Selected source is missing after restart; the manual upscale was not resumed.",
                )
            # The worker restores these persisted manual requests after startup.
            continue
        if row.get("audio_status") == "PROCESSING":
            if (
                row.get("status") == "AUDIO_TRANSCODING"
                and row.get("backup_path")
                and os.path.exists(row["backup_path"])
            ):
                # A failed recovery above leaves the row untouched for the next
                # startup; never convert it into ordinary pending video work.
                continue
            await database.update_file(
                row["id"],
                status=row.get("audio_previous_status") or "PENDING",
                audio_status="PENDING",
                audio_error="Startup recovery reset an interrupted audio check; it can be retried.",
                audio_previous_status=None,
                audio_swap_backup_path=None,
            )
            continue
        if row["status"] not in INTERRUPTED_STATUSES:
            continue
        if row.get("backup_path") and os.path.exists(row["backup_path"]):
            continue
        if (
            (not row.get("backup_path") or not os.path.exists(row["backup_path"]))
            and row.get("new_size_bytes") is not None
            and _is_present_nonempty(row["file_path"])
        ):
            try:
                output_stat = os.stat(row["file_path"], follow_symlinks=False)
            except OSError:
                output_stat = None
            if (
                output_stat is not None
                and output_stat.st_size == row["new_size_bytes"]
                and float(output_stat.st_mtime) == float(row["mtime"])
            ):
                await database.update_file(
                    row["id"],
                    status="TRANSCODED",
                    backup_path=None,
                    error_log=None,
                )
                notice = f"Restored committed transcode state after interruption: {row['file_path']}"
                notices.append(notice)
                logger.info(notice)
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
                    container_type=None,
                    format_name=None,
                    tagged_upscaled=0,
                    bit_rate=None,
                    width=None,
                    height=None,
                    audio_streams=0,
                    audio_status="PENDING",
                    audio_error=None,
                    audio_previous_status=None,
                    audio_swap_backup_path=None,
                    sub_streams=0,
                    duration=None,
                    status="HEALTHY",
                    error_log="Startup finalized a manual restore from a previously verified backup.",
                    transcoded_at=None,
                    upscaled_at=None,
                    upscale_request_id=None,
                    upscale_target_height=None,
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
            transcoded_at=None,
        )
        notice = f"Requeued interrupted job: {row['file_path']}"
        notices.append(notice)
        logger.warning(notice)

    return notices
