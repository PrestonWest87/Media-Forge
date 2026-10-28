"""SQLite handoff to the sibling, manually queued Upscale-Forge daemon."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class UpscaleServiceUnavailable(RuntimeError):
    pass


class UpscaleForgeClient:
    """Coordinate selected upscale jobs through Upscale-Forge's shared state DB."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        heartbeat_max_age_sec: float = 30.0,
    ) -> None:
        self.path = Path(
            path or os.environ.get("GPU_FORGE_UPSCALE_DB_PATH", "/upscale-forge-config/state.db")
        )
        self.heartbeat_max_age_sec = float(heartbeat_max_age_sec)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise UpscaleServiceUnavailable("Upscale-Forge state database is not mounted or initialized.")
        connection = sqlite3.connect(self.path, timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=3000")
        return connection

    def is_online(self) -> bool:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT value FROM service_state WHERE key='heartbeat_at'"
                ).fetchone()
            heartbeat = float(row["value"]) if row else 0.0
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False
        return 0.0 <= time.time() - heartbeat <= self.heartbeat_max_age_sec

    def enqueue(
        self,
        file_path: str,
        *,
        device_id: int,
        inode: int,
        size_bytes: int,
        mtime_ns: int,
        target_height: int = 1080,
    ) -> int:
        if int(target_height) not in {720, 1080, 1440}:
            raise UpscaleServiceUnavailable("Target resolution must be 720p, 1080p, or 1440p.")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT request_id, target_height FROM manual_jobs
                    WHERE device_id=? AND inode=? AND size_bytes=? AND mtime_ns=?
                      AND status IN ('PENDING', 'PROCESSING', 'CANCEL_REQUESTED')
                    ORDER BY request_id DESC LIMIT 1
                    """,
                    (int(device_id), int(inode), int(size_bytes), int(mtime_ns)),
                ).fetchone()
                if existing:
                    if int(existing["target_height"]) != int(target_height):
                        raise UpscaleServiceUnavailable(
                            "This source already has a selected upscale at another target height."
                        )
                    connection.commit()
                    return int(existing["request_id"])
                now = time.time()
                cursor = connection.execute(
                    """
                    INSERT INTO manual_jobs (
                        filepath, device_id, inode, size_bytes, mtime_ns, target_height,
                        status, requested_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        file_path,
                        int(device_id),
                        int(inode),
                        int(size_bytes),
                        int(mtime_ns),
                        int(target_height),
                        now,
                        now,
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)
        except (OSError, sqlite3.Error) as exc:
            raise UpscaleServiceUnavailable(f"Could not enqueue Upscale-Forge job: {exc}") from exc

    def get_job(self, request_id: int) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM manual_jobs WHERE request_id=?", (int(request_id),)
                ).fetchone()
                if row is None:
                    return None
                job = dict(row)
                job["progress_percent"] = 0.0
                job["phase"] = "QUEUED"
                job["phase_detail"] = None
                job["wait_reason"] = None
                job["frames_done"] = 0.0
                job["frames_total"] = 0
                job["total_segments"] = 0
                job["completed_segments"] = 0
                job["active_segment_index"] = None
                job["active_segment_progress_percent"] = 0.0
                job["phase_elapsed_sec"] = 0.0
                if job["status"] == "PROCESSING":
                    media = connection.execute(
                        """
                        SELECT status, updated_at FROM media_files
                        WHERE filepath=? AND inode=? AND size_bytes=?
                        """,
                        (job.get("resolved_filepath"), job["inode"], job["size_bytes"]),
                    ).fetchone()
                    if media and media["status"] == "VERIFIED":
                        job["progress_percent"] = 96.0
                        job["phase"] = "VERIFYING_OUTPUT"
                        job["phase_detail"] = (
                            "Output passed verification; preparing the verified replacement."
                        )
                    else:
                        segments = connection.execute(
                            """
                            SELECT COUNT(*) AS total_segments,
                                   COALESCE(SUM(total_frames), 0) AS total_frames,
                                   COALESCE(SUM(
                                       CASE WHEN status='DONE' THEN total_frames
                                            ELSE total_frames * MIN(100.0, MAX(0.0, progress_percent)) / 100.0
                                       END
                                   ), 0) AS completed_frames,
                                   COALESCE(SUM(duration_seconds), 0.0) AS total_duration,
                                    COALESCE(SUM(
                                        CASE WHEN status='DONE' THEN COALESCE(duration_seconds, 0.0)
                                             ELSE COALESCE(duration_seconds, 0.0)
                                                  * MIN(100.0, MAX(0.0, progress_percent)) / 100.0
                                        END
                                    ), 0.0) AS completed_duration,
                                    COALESCE(SUM(CASE WHEN status='DONE' THEN 1 ELSE 0 END), 0) AS completed_segments,
                                    COALESCE(MAX(CASE WHEN status='INITIALIZING_MODEL' THEN 1 ELSE 0 END), 0) AS initializing_model,
                                    MAX(CASE WHEN status IN ('INITIALIZING_MODEL', 'ENCODING') THEN segment_index END) AS active_segment_index,
                                    MAX(CASE WHEN status IN ('INITIALIZING_MODEL', 'ENCODING') THEN progress_percent END) AS active_segment_progress_percent,
                                    MAX(CASE WHEN status IN ('INITIALIZING_MODEL', 'ENCODING') THEN updated_at END) AS active_segment_updated_at
                             FROM segment_progress
                            WHERE filepath=? AND inode=? AND size_bytes=? AND target_height=?
                            """,
                            (
                                job.get("resolved_filepath"),
                                job["inode"],
                                job["size_bytes"],
                                job["target_height"],
                            ),
                        ).fetchone()
                        total = int(segments["total_segments"] or 0)
                        total_frames = int(segments["total_frames"] or 0)
                        frames_done = float(segments["completed_frames"] or 0.0)
                        total_duration = float(segments["total_duration"] or 0.0)
                        completed_duration = float(segments["completed_duration"] or 0.0)
                        completed = int(segments["completed_segments"] or 0)
                        job["frames_done"] = frames_done
                        job["frames_total"] = total_frames
                        job["total_segments"] = total
                        job["completed_segments"] = completed
                        if segments["active_segment_index"] is not None:
                            job["active_segment_index"] = int(segments["active_segment_index"])
                        if segments["active_segment_progress_percent"] is not None:
                            job["active_segment_progress_percent"] = float(
                                segments["active_segment_progress_percent"]
                            )
                        phase_updated_at = segments["active_segment_updated_at"]
                        if phase_updated_at is None and media is not None:
                            phase_updated_at = media["updated_at"]
                        if phase_updated_at is not None:
                            job["phase_elapsed_sec"] = max(
                                0.0, time.time() - float(phase_updated_at)
                            )
                        if total_frames > 0:
                            fraction = frames_done / total_frames
                        elif total_duration > 0:
                            fraction = completed_duration / total_duration
                        elif total > 0:
                            fraction = completed / total
                        else:
                            fraction = None
                        job["progress_percent"] = (
                            min(94.0, max(0.0, 94.0 * fraction))
                            if fraction is not None
                            else 0.0
                        )
                        if media and media["status"] == "WAITING_GPU":
                            job["phase"] = "WAITING_GPU"
                            readiness = connection.execute(
                                "SELECT value FROM service_state WHERE key='gpu_readiness'"
                            ).fetchone()
                            if readiness is not None:
                                try:
                                    gpu_status = json.loads(readiness["value"])
                                except (TypeError, json.JSONDecodeError):
                                    gpu_status = None
                                if isinstance(gpu_status, dict):
                                    job["wait_reason"] = gpu_status.get("reason")
                                    job["phase_detail"] = gpu_status.get("reason")
                        elif bool(segments["initializing_model"]):
                            job["phase"] = "INITIALIZING_MODEL"
                            job["phase_detail"] = (
                                "Starting the VapourSynth/TensorRT inference engine; "
                                "first-run setup can take several minutes."
                            )
                        elif total == 0:
                            job["phase"] = "PREPARING_SEGMENTS"
                            job["phase_detail"] = (
                                "Probing source frames and preparing upscale segments."
                            )
                        elif completed >= total:
                            job["phase"] = "FINALIZING_OUTPUT"
                            job["phase_detail"] = (
                                "All segments are encoded; assembling and verifying the output."
                            )
                        elif job["active_segment_index"] is not None:
                            job["phase"] = "ENCODING"
                            position = int(job["active_segment_index"]) + 1
                            segment_progress = float(
                                job["active_segment_progress_percent"] or 0.0
                            )
                            job["phase_detail"] = (
                                f"Encoding segment {position} of {total} "
                                f"({segment_progress:.1f}% of this segment)."
                            )
                        else:
                            job["phase"] = "PREPARING_SEGMENTS"
                            job["phase_detail"] = "Preparing the next upscale segment."
                elif job["status"] == "CANCEL_REQUESTED":
                    job["phase"] = "CANCELLING"
                    job["phase_detail"] = "Waiting for Upscale-Forge to stop the active segment safely."
                elif job["status"] == "CANCELLED":
                    job["phase"] = "CANCELLED"
                    job["phase_detail"] = job.get("last_error")
                elif job["status"] == "COMPLETED":
                    job["progress_percent"] = 100.0
                    job["phase"] = "COMPLETED"
                    job["phase_detail"] = "Verified output installed; original retained as backup."
                elif job["status"] == "FAILED":
                    job["phase"] = "FAILED"
                    job["phase_detail"] = job.get("last_error")
        except (OSError, sqlite3.Error) as exc:
            raise UpscaleServiceUnavailable(f"Could not read Upscale-Forge job: {exc}") from exc
        return job

    def preview_directory(self, request_id: int) -> Path:
        """Return the request-scoped JPEG preview directory shared with Upscale-Forge."""

        return self.path.parent / "previews" / f"job_{int(request_id)}"

    def find_active_job(
        self,
        *,
        device_id: int,
        inode: int,
        size_bytes: int,
        mtime_ns: int,
    ) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT * FROM manual_jobs
                    WHERE device_id=? AND inode=? AND size_bytes=? AND mtime_ns=?
                      AND status IN ('PENDING', 'PROCESSING', 'CANCEL_REQUESTED')
                    ORDER BY request_id DESC LIMIT 1
                    """,
                    (int(device_id), int(inode), int(size_bytes), int(mtime_ns)),
                ).fetchone()
                return dict(row) if row else None
        except (OSError, sqlite3.Error) as exc:
            raise UpscaleServiceUnavailable(f"Could not find Upscale-Forge job: {exc}") from exc

    def cancel_job(self, request_id: int) -> dict[str, Any] | None:
        """Cancel a queued job or request cooperative cancellation of a running one."""

        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT request_id, status, last_error FROM manual_jobs WHERE request_id=?",
                    (int(request_id),),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                status = str(row["status"])
                now = time.time()
                if status == "PENDING":
                    connection.execute(
                        """
                        UPDATE manual_jobs SET status='CANCELLED',
                            last_error='Cancelled by user in GPU-Forge.',
                            completed_at=?, updated_at=?
                        WHERE request_id=? AND status='PENDING'
                        """,
                        (now, now, int(request_id)),
                    )
                elif status == "PROCESSING":
                    connection.execute(
                        """
                        UPDATE manual_jobs SET status='CANCEL_REQUESTED',
                            last_error='Cancellation requested by user in GPU-Forge.',
                            updated_at=?
                        WHERE request_id=? AND status='PROCESSING'
                        """,
                        (now, int(request_id)),
                    )
                connection.commit()
                result = connection.execute(
                    "SELECT * FROM manual_jobs WHERE request_id=?", (int(request_id),)
                ).fetchone()
                return dict(result) if result else None
        except (OSError, sqlite3.Error) as exc:
            raise UpscaleServiceUnavailable(f"Could not cancel Upscale-Forge job: {exc}") from exc
