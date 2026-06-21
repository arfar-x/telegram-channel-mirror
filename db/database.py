"""
Async SQLite database layer.

Tables
------
message_map       : source_id → dest_id mapping + metadata
sync_progress     : cursor tracking for historical sync
deleted_messages  : source_ids marked as deleted (soft delete)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "mirror.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS message_map (
    source_id       INTEGER PRIMARY KEY,
    dest_id         INTEGER,
    grouped_id      INTEGER,          -- album group id (nullable)
    media_type      TEXT,             -- 'photo','video','document','sticker','poll',etc.
    is_deleted      INTEGER DEFAULT 0,-- 1 = source deleted (soft)
    created_at      REAL              -- unix timestamp
);

CREATE INDEX IF NOT EXISTS idx_dest_id       ON message_map(dest_id);
CREATE INDEX IF NOT EXISTS idx_grouped_id    ON message_map(grouped_id);

CREATE TABLE IF NOT EXISTS sync_progress (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pending_edits (
    source_id   INTEGER PRIMARY KEY,
    queued_at   REAL
);
"""


class Database:
    """Thin async wrapper around aiosqlite."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database connected: %s", self._path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            logger.info("Database closed.")

    # ------------------------------------------------------------------
    # Message map
    # ------------------------------------------------------------------

    async def upsert_mapping(
        self,
        source_id: int,
        dest_id: int | None,
        *,
        grouped_id: int | None = None,
        media_type: str | None = None,
        created_at: float | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO message_map (source_id, dest_id, grouped_id, media_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                dest_id    = excluded.dest_id,
                grouped_id = excluded.grouped_id,
                media_type = excluded.media_type
            """,
            (source_id, dest_id, grouped_id, media_type, created_at),
        )
        await self._conn.commit()

    async def get_dest_id(self, source_id: int) -> int | None:
        async with self._conn.execute(
            "SELECT dest_id FROM message_map WHERE source_id = ?", (source_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["dest_id"] if row else None

    async def is_processed(self, source_id: int) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM message_map WHERE source_id = ?", (source_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_deleted(self, source_id: int) -> None:
        await self._conn.execute(
            "UPDATE message_map SET is_deleted = 1 WHERE source_id = ?", (source_id,)
        )
        await self._conn.commit()

    async def get_mapping(self, source_id: int) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM message_map WHERE source_id = ?", (source_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_group_dest_ids(self, grouped_id: int) -> list[int]:
        """Return all destination message ids belonging to an album group."""
        async with self._conn.execute(
            "SELECT dest_id FROM message_map WHERE grouped_id = ? AND dest_id IS NOT NULL",
            (grouped_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [r["dest_id"] for r in rows]

    # ------------------------------------------------------------------
    # Sync progress
    # ------------------------------------------------------------------

    async def get_progress(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM sync_progress WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_progress(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO sync_progress (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Pending edits
    # ------------------------------------------------------------------

    async def add_pending_edit(self, source_id: int, queued_at: float) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_edits (source_id, queued_at) VALUES (?, ?)",
            (source_id, queued_at),
        )
        await self._conn.commit()

    async def remove_pending_edit(self, source_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_edits WHERE source_id = ?", (source_id,)
        )
        await self._conn.commit()