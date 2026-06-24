"""
Async PostgreSQL database layer.

Tables
------
message_map       : source_id → dest_id mapping + metadata
sync_progress     : cursor tracking for historical sync
pending_edits     : source_ids with edits queued for a not-yet-mirrored message
"""

from __future__ import annotations

import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/mirror"

# Telegram message ids fit comfortably in INTEGER, but grouped_id and channel
# ids can exceed it — BIGINT everywhere avoids silent truncation/overflow.
SCHEMA = """
CREATE TABLE IF NOT EXISTS message_map (
    source_id       BIGINT PRIMARY KEY,
    dest_id         BIGINT,
    grouped_id      BIGINT,            -- album group id (nullable)
    media_type      TEXT,              -- 'photo','video','document','sticker','poll',etc.
    is_deleted      INTEGER DEFAULT 0, -- 1 = source deleted (soft)
    created_at      DOUBLE PRECISION   -- unix timestamp
);

CREATE INDEX IF NOT EXISTS idx_dest_id       ON message_map(dest_id);
CREATE INDEX IF NOT EXISTS idx_grouped_id    ON message_map(grouped_id);

CREATE TABLE IF NOT EXISTS sync_progress (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pending_edits (
    source_id   BIGINT PRIMARY KEY,
    queued_at   DOUBLE PRECISION
);
"""


class Database:
    """Thin async wrapper around an asyncpg connection pool."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("Database connected: %s", self._dsn)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
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
        await self._pool.execute(
            """
            INSERT INTO message_map (source_id, dest_id, grouped_id, media_type, created_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (source_id) DO UPDATE SET
                dest_id    = excluded.dest_id,
                grouped_id = excluded.grouped_id,
                media_type = excluded.media_type
            """,
            source_id, dest_id, grouped_id, media_type, created_at,
        )

    async def get_dest_id(self, source_id: int) -> int | None:
        row = await self._pool.fetchrow(
            "SELECT dest_id FROM message_map WHERE source_id = $1", source_id
        )
        return row["dest_id"] if row else None

    async def is_processed(self, source_id: int) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM message_map WHERE source_id = $1", source_id
        )
        return row is not None

    async def mark_deleted(self, source_id: int) -> None:
        await self._pool.execute(
            "UPDATE message_map SET is_deleted = 1 WHERE source_id = $1", source_id
        )

    async def get_mapping(self, source_id: int) -> asyncpg.Record | None:
        return await self._pool.fetchrow(
            "SELECT * FROM message_map WHERE source_id = $1", source_id
        )

    async def has_mappings(self) -> bool:
        row = await self._pool.fetchrow("SELECT 1 FROM message_map LIMIT 1")
        return row is not None

    async def get_group_dest_ids(self, grouped_id: int) -> list[int]:
        """Return all destination message ids belonging to an album group."""
        rows = await self._pool.fetch(
            "SELECT dest_id FROM message_map WHERE grouped_id = $1 AND dest_id IS NOT NULL",
            grouped_id,
        )
        return [r["dest_id"] for r in rows]

    # ------------------------------------------------------------------
    # Sync progress
    # ------------------------------------------------------------------

    async def get_progress(self, key: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT value FROM sync_progress WHERE key = $1", key
        )
        return row["value"] if row else None

    async def set_progress(self, key: str, value: str) -> None:
        await self._pool.execute(
            "INSERT INTO sync_progress (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            key, value,
        )

    # ------------------------------------------------------------------
    # Pending edits
    # ------------------------------------------------------------------

    async def add_pending_edit(self, source_id: int, queued_at: float) -> None:
        await self._pool.execute(
            "INSERT INTO pending_edits (source_id, queued_at) VALUES ($1, $2) "
            "ON CONFLICT (source_id) DO UPDATE SET queued_at = excluded.queued_at",
            source_id, queued_at,
        )

    async def remove_pending_edit(self, source_id: int) -> None:
        await self._pool.execute(
            "DELETE FROM pending_edits WHERE source_id = $1", source_id
        )
