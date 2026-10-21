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
    "bit_rate",
    "width",
    "height",
    "audio_streams",
    "sub_streams",
    "duration",
    "status",
    "error_log",
    "processing_time_sec",
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
            await connection.commit()

    async def upsert_batch(self, files: Iterable[dict[str, Any]]) -> int:
        rows = [
            (
                item["file_path"],
                item["file_name"],
                item["directory"],
                int(item["size_bytes"]),
                float(item["mtime"]),
            )
            for item in files
        ]
        if not rows:
            return 0

        sql = """
        INSERT INTO media_files (file_path, file_name, directory, size_bytes, mtime)
        VALUES (?, ?, ?, ?, ?)
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
            mtime=excluded.mtime,
            codec=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.codec END,
            pix_fmt=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.pix_fmt END,
            bit_rate=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.bit_rate END,
            width=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.width END,
            height=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.height END,
            audio_streams=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN 0 ELSE media_files.audio_streams END,
            sub_streams=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN 0 ELSE media_files.sub_streams END,
            duration=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.duration END,
            error_log=CASE
                WHEN COALESCE(media_files.new_size_bytes, media_files.size_bytes) != excluded.size_bytes
                  OR media_files.mtime != excluded.mtime
                THEN NULL ELSE media_files.error_log END,
            updated_at=CURRENT_TIMESTAMP
        """
        async with self._connect() as connection:
            await connection.executemany(sql, rows)
            await connection.commit()
        return len(rows)

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
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
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
