"""SQLite persistence for scanned media and transactional job state."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

MEDIA_FILE_COLUMNS = {
    "file_path",
    "backup_path",
    "file_name",
    "directory",
    "size_bytes",
    "new_size_bytes",
    "mtime",
    "codec",
    "pix_fmt",
    "container_type",
    "format_name",
    "tagged_upscaled",
    "bit_rate",
    "width",
    "height",
    "audio_streams",
    "audio_status",
    "audio_error",
    "audio_previous_status",
    "audio_swap_backup_path",
    "sub_streams",
    "duration",
    "status",
    "error_log",
    "processing_time_sec",
    "action_tag",
    "action_reason",
    "transcoded_at",
    "upscale_request_id",
    "upscale_queue_kind",
    "upscaled_at",
    "upscale_target_height",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_files (
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
    container_type TEXT DEFAULT NULL,
    format_name TEXT DEFAULT NULL,
    tagged_upscaled INTEGER NOT NULL DEFAULT 0,
    bit_rate INTEGER DEFAULT NULL,
    width INTEGER DEFAULT NULL,
    height INTEGER DEFAULT NULL,
    audio_streams INTEGER DEFAULT 0,
    audio_status TEXT NOT NULL DEFAULT 'PENDING',
    audio_error TEXT DEFAULT NULL,
    audio_previous_status TEXT DEFAULT NULL,
    audio_swap_backup_path TEXT DEFAULT NULL,
    sub_streams INTEGER DEFAULT 0,
    duration REAL DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_log TEXT DEFAULT NULL,
    processing_time_sec REAL DEFAULT NULL,
    action_tag TEXT NOT NULL DEFAULT 'LEAVE_ALONE',
    action_reason TEXT DEFAULT NULL,
    transcoded_at TIMESTAMP DEFAULT NULL,
    upscale_request_id INTEGER DEFAULT NULL,
    upscale_queue_kind TEXT NOT NULL DEFAULT 'manual',
    upscaled_at TIMESTAMP DEFAULT NULL,
    upscale_target_height INTEGER DEFAULT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_media_files_status ON media_files(status);
CREATE INDEX IF NOT EXISTS idx_media_files_directory ON media_files(directory);
CREATE INDEX IF NOT EXISTS idx_media_files_fingerprint ON media_files(file_path, mtime, size_bytes);
"""


class Database:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.environ.get("GPU_FORGE_DB_PATH", "/config/gpu_forge.db"))

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path, timeout=30) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA synchronous=NORMAL;")
            yield connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as connection:
            await connection.execute("PRAGMA journal_mode=WAL;")
            await connection.executescript(SCHEMA)
            cursor = await connection.execute("PRAGMA table_info(media_files)")
            existing_columns = {row["name"] for row in await cursor.fetchall()}
            migrations = {
                "container_type": "TEXT DEFAULT NULL",
                "format_name": "TEXT DEFAULT NULL",
                "tagged_upscaled": "INTEGER NOT NULL DEFAULT 0",
                "action_tag": "TEXT NOT NULL DEFAULT 'LEAVE_ALONE'",
                "action_reason": "TEXT DEFAULT NULL",
                "transcoded_at": "TIMESTAMP DEFAULT NULL",
                "upscale_request_id": "INTEGER DEFAULT NULL",
                "upscale_queue_kind": "TEXT NOT NULL DEFAULT 'manual'",
                "upscaled_at": "TIMESTAMP DEFAULT NULL",
                "upscale_target_height": "INTEGER DEFAULT NULL",
                "audio_status": "TEXT NOT NULL DEFAULT 'PENDING'",
                "audio_error": "TEXT DEFAULT NULL",
                "audio_previous_status": "TEXT DEFAULT NULL",
                "audio_swap_backup_path": "TEXT DEFAULT NULL",
            }
            for column, declaration in migrations.items():
                if column not in existing_columns:
                    await connection.execute(
                        f"ALTER TABLE media_files ADD COLUMN {column} {declaration}"
                    )
            await connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_files_audio_status ON media_files(audio_status)"
            )
            await connection.execute(
                """
                UPDATE media_files SET transcoded_at = updated_at
                WHERE (status IN ('TRANSCODED', 'TRANSCODED_BACKUP_KEPT') OR new_size_bytes IS NOT NULL)
                  AND transcoded_at IS NULL
                """
            )
            await connection.commit()

    async def upsert_batch(self, files: Iterable[dict[str, Any]]) -> int:
        rows = [
            (
                item["file_path"],
                item["file_name"],
                item["directory"],
                int(item["size_bytes"]),
                float(item["mtime"]),
                item.get("codec"),
                item.get("pix_fmt"),
                item.get("container_type"),
                item.get("format_name"),
                int(bool(item["tagged_upscaled"])) if item.get("tagged_upscaled") is not None else None,
                item.get("bit_rate"),
                item.get("width"),
                item.get("height"),
                int(item["audio_streams"]) if item.get("audio_streams") is not None else None,
                int(item["sub_streams"]) if item.get("sub_streams") is not None else None,
                item.get("duration"),
                item.get("action_tag"),
                item.get("action_reason"),
                item.get("error_log"),
            )
            for item in files
        ]
        if not rows:
            return 0

        sql = """
        INSERT INTO media_files (
            file_path, file_name, directory, size_bytes, mtime, codec, pix_fmt,
            container_type, format_name, tagged_upscaled, bit_rate, width, height,
            audio_streams, sub_streams, duration, action_tag, action_reason,
            status, error_log
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?, ?,
                COALESCE(?, 0), COALESCE(?, 0), ?, COALESCE(?, 'LEAVE_ALONE'), ?, 'PENDING', ?)
        ON CONFLICT(file_path) DO UPDATE SET
            file_name=excluded.file_name,
            directory=excluded.directory,
            status=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN 'PENDING' ELSE media_files.status END,
            size_bytes=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.size_bytes ELSE media_files.size_bytes END,
            new_size_bytes=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.new_size_bytes END,
            transcoded_at=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.transcoded_at END,
            upscale_request_id=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.upscale_request_id END,
            upscale_queue_kind=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN 'manual' ELSE media_files.upscale_queue_kind END,
            upscaled_at=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.upscaled_at END,
            upscale_target_height=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.upscale_target_height END,
            mtime=excluded.mtime,
            codec=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.codec ELSE COALESCE(excluded.codec, media_files.codec) END,
            pix_fmt=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.pix_fmt ELSE COALESCE(excluded.pix_fmt, media_files.pix_fmt) END,
            container_type=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.container_type ELSE COALESCE(excluded.container_type, media_files.container_type) END,
            format_name=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.format_name ELSE COALESCE(excluded.format_name, media_files.format_name) END,
            tagged_upscaled=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN COALESCE(excluded.tagged_upscaled, 0)
                ELSE COALESCE(excluded.tagged_upscaled, media_files.tagged_upscaled) END,
            bit_rate=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.bit_rate ELSE COALESCE(excluded.bit_rate, media_files.bit_rate) END,
            width=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.width ELSE COALESCE(excluded.width, media_files.width) END,
            height=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.height ELSE COALESCE(excluded.height, media_files.height) END,
            audio_streams=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN COALESCE(excluded.audio_streams, 0)
                ELSE COALESCE(excluded.audio_streams, media_files.audio_streams) END,
            sub_streams=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN COALESCE(excluded.sub_streams, 0)
                ELSE COALESCE(excluded.sub_streams, media_files.sub_streams) END,
            duration=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.duration ELSE COALESCE(excluded.duration, media_files.duration) END,
            action_tag=CASE
                WHEN excluded.codec IS NOT NULL OR excluded.action_reason IS NOT NULL
                THEN excluded.action_tag ELSE media_files.action_tag END,
            action_reason=CASE
                WHEN excluded.codec IS NOT NULL OR excluded.action_reason IS NOT NULL
                THEN excluded.action_reason ELSE media_files.action_reason END,
            audio_status=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN 'PENDING' ELSE media_files.audio_status END,
            audio_error=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.audio_error END,
            audio_previous_status=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.audio_previous_status END,
            audio_swap_backup_path=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.audio_swap_backup_path END,
            error_log=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN excluded.error_log
                WHEN media_files.error_log LIKE 'Scan metadata probe failed:%'
                  AND excluded.codec IS NOT NULL
                THEN excluded.error_log
                ELSE COALESCE(excluded.error_log, media_files.error_log) END,
            updated_at=CURRENT_TIMESTAMP
        """
        async with self._connect() as connection:
            await connection.executemany(sql, rows)
            await connection.commit()
        return len(rows)

    async def requeue_stale_yield_pauses(self, active_ids: set[int] | None = None) -> int:
        """Reconcile orphaned in-progress rows while leaving live/swap rows alone."""

        ids = sorted(active_ids or set())
        active_clause = f"AND id NOT IN ({','.join('?' for _ in ids)})" if ids else ""
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE media_files
                SET status=CASE
                        WHEN audio_status='PROCESSING' THEN COALESCE(audio_previous_status, 'PENDING')
                        WHEN new_size_bytes IS NOT NULL THEN 'TRANSCODED'
                        ELSE 'PENDING'
                    END,
                    audio_status=CASE WHEN audio_status='PROCESSING' THEN 'PENDING' ELSE audio_status END,
                    audio_error=CASE
                        WHEN audio_status='PROCESSING' THEN 'Audio work was interrupted while paused; it can be retried.'
                        ELSE audio_error
                    END,
                    audio_previous_status=NULL,
                    error_log=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('CHECKING', 'TRANSCODING', 'VERIFYING_OUTPUT', 'AUDIO_REMUXING', 'AUDIO_VERIFYING', 'PAUSED_YIELD')
                  AND backup_path IS NULL {active_clause}
                """,
                ids,
            )
            await connection.commit()
        return int(cursor.rowcount)

    async def mark_yield_paused(self, file_id: int) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE media_files SET status='PAUSED_YIELD', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status IN ('CHECKING', 'TRANSCODING', 'VERIFYING_OUTPUT', 'AUDIO_REMUXING', 'AUDIO_VERIFYING')
                  AND backup_path IS NULL
                """,
                (file_id,),
            )
            await connection.commit()
        return cursor.rowcount > 0

    async def resume_yield_paused(self, file_id: int, phase: str) -> bool:
        if phase not in {"CHECKING", "TRANSCODING", "VERIFYING_OUTPUT", "AUDIO_REMUXING", "AUDIO_VERIFYING"}:
            raise ValueError(f"Invalid resume phase: {phase}")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE media_files
                SET status=?, error_log=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='PAUSED_YIELD' AND backup_path IS NULL
                """,
                (phase, file_id),
            )
            await connection.commit()
        return cursor.rowcount > 0

    async def update_file(self, file_id: int, **fields: Any) -> None:
        invalid = set(fields) - MEDIA_FILE_COLUMNS
        if invalid:
            raise ValueError(f"Unsupported media_files columns: {', '.join(sorted(invalid))}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values()) + [file_id]
        async with self._connect() as connection:
            await connection.execute(
                f"UPDATE media_files SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            await connection.commit()

    async def update_path(self, file_path: str, **fields: Any) -> None:
        invalid = set(fields) - MEDIA_FILE_COLUMNS
        if invalid:
            raise ValueError(f"Unsupported media_files columns: {', '.join(sorted(invalid))}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values()) + [file_path]
        async with self._connect() as connection:
            await connection.execute(
                f"UPDATE media_files SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE file_path = ?",
                values,
            )
            await connection.commit()

    async def get_file(self, file_id: int) -> dict[str, Any] | None:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT * FROM media_files WHERE id = ?", (file_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_by_path(self, file_path: str) -> dict[str, Any] | None:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT * FROM media_files WHERE file_path = ?", (file_path,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_files(
        self,
        statuses: list[str] | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
        action_tags: list[str] | None = None,
        audio_statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if action_tags:
            clauses.append(f"action_tag IN ({','.join('?' for _ in action_tags)})")
            params.extend(action_tags)
        if audio_statuses:
            clauses.append(f"audio_status IN ({','.join('?' for _ in audio_statuses)})")
            params.extend(audio_statuses)
        if search:
            clauses.append("(file_name LIKE ? OR file_path LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM media_files {where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def all_files(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT * FROM media_files ORDER BY id")
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def pending_files(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT id, file_path FROM media_files WHERE status = 'PENDING' ORDER BY id"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def pending_audio_files(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT id, file_path FROM media_files WHERE audio_status IN ('PENDING', 'FAILED', 'BLOCKED_BACKUP') ORDER BY id"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def pending_audio_preview(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, file_path, file_name, status, audio_status
                FROM media_files WHERE audio_status IN ('PENDING', 'FAILED', 'BLOCKED_BACKUP') ORDER BY id LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def audio_status_counts(self) -> dict[str, int]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT audio_status, COUNT(*) AS count FROM media_files GROUP BY audio_status"
            )
            rows = await cursor.fetchall()
        return {row["audio_status"]: int(row["count"]) for row in rows}

    async def pending_preview(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, file_path, file_name, status, action_tag
                FROM media_files WHERE status='PENDING' ORDER BY id LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def files_by_ids(self, file_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not file_ids:
            return {}
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM media_files WHERE id IN ({','.join('?' for _ in file_ids)})",
                [int(file_id) for file_id in file_ids],
            )
            rows = await cursor.fetchall()
        return {int(row["id"]): dict(row) for row in rows}

    async def upscale_work_files(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, upscale_queue_kind, status FROM media_files
                WHERE status IN ('UPSCALE_QUEUED', 'UPSCALING', 'UPSCALE_CANCEL_REQUESTED')
                ORDER BY updated_at, id
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def upscale_candidates(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM media_files WHERE action_tag='UPSCALE' ORDER BY id"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def requeue_transcode_candidates(self, active_ids: set[int] | None = None) -> int:
        """Queue untranscoded candidates previously completed in health-only mode."""

        ids = sorted(active_ids or set())
        active_clause = f"AND id NOT IN ({','.join('?' for _ in ids)})" if ids else ""
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE media_files
                SET status='PENDING', error_log=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE status='HEALTHY' AND action_tag='TRANSCODE'
                  AND transcoded_at IS NULL AND new_size_bytes IS NULL AND backup_path IS NULL
                  {active_clause}
                """,
                ids,
            )
            await connection.commit()
        return int(cursor.rowcount)

    async def backup_files(self) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM media_files WHERE backup_path IS NOT NULL ORDER BY id"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def status_counts(self) -> dict[str, int]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT status, COUNT(*) AS count FROM media_files GROUP BY status"
            )
            rows = await cursor.fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    async def total_files(self) -> int:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM media_files")
            row = await cursor.fetchone()
        return int(row[0])

    async def total_saved_bytes(self) -> int:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COALESCE(SUM(size_bytes - new_size_bytes), 0)
                FROM media_files
                WHERE new_size_bytes IS NOT NULL
                  AND status IN ('TRANSCODED', 'TRANSCODED_BACKUP_KEPT')
                """
            )
            row = await cursor.fetchone()
        return int(row[0] or 0)
