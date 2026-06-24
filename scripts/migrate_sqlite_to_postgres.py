"""
One-off migration: copy all rows from the legacy SQLite database into
PostgreSQL.

Usage
-----
    python scripts/migrate_sqlite_to_postgres.py [--sqlite-path mirror.db]

Reads DATABASE_URL from the environment (same variable the app uses) for
the PostgreSQL target. Safe to re-run: existing rows are upserted, so a
second run after fixing a connection issue won't duplicate or lose data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from db.database import DATABASE_URL, SCHEMA

TABLES = ("message_map", "sync_progress", "pending_edits")


def read_sqlite(path: Path) -> dict[str, list[sqlite3.Row]]:
    if not path.exists():
        raise SystemExit(f"SQLite database not found: {path}")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        data: dict[str, list[sqlite3.Row]] = {}
        for table in TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            data[table] = (
                conn.execute(f"SELECT * FROM {table}").fetchall() if exists else []
            )
        return data
    finally:
        conn.close()


async def write_postgres(data: dict[str, list[sqlite3.Row]]) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL)
    try:
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)

            async with conn.transaction():
                for row in data["message_map"]:
                    await conn.execute(
                        """
                        INSERT INTO message_map
                            (source_id, dest_id, grouped_id, media_type, is_deleted, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (source_id) DO UPDATE SET
                            dest_id    = excluded.dest_id,
                            grouped_id = excluded.grouped_id,
                            media_type = excluded.media_type,
                            is_deleted = excluded.is_deleted,
                            created_at = excluded.created_at
                        """,
                        row["source_id"],
                        row["dest_id"],
                        row["grouped_id"],
                        row["media_type"],
                        row["is_deleted"],
                        row["created_at"],
                    )

                for row in data["sync_progress"]:
                    await conn.execute(
                        """
                        INSERT INTO sync_progress (key, value)
                        VALUES ($1, $2)
                        ON CONFLICT (key) DO UPDATE SET value = excluded.value
                        """,
                        row["key"],
                        row["value"],
                    )

                for row in data["pending_edits"]:
                    await conn.execute(
                        """
                        INSERT INTO pending_edits (source_id, queued_at)
                        VALUES ($1, $2)
                        ON CONFLICT (source_id) DO UPDATE SET queued_at = excluded.queued_at
                        """,
                        row["source_id"],
                        row["queued_at"],
                    )
    finally:
        await pool.close()


async def verify(data: dict[str, list[sqlite3.Row]]) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL)
    try:
        async with pool.acquire() as conn:
            for table in TABLES:
                expected = len(data[table])
                actual = await conn.fetchval(f"SELECT count(*) FROM {table}")
                if actual < expected:
                    raise SystemExit(
                        f"Verification failed for {table}: expected at least "
                        f"{expected} rows, found {actual} in PostgreSQL."
                    )
                print(f"{table}: {actual} rows in PostgreSQL (source had {expected}).")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path",
        default=os.environ.get("DB_PATH", "mirror.db"),
        help="Path to the legacy SQLite database file (default: %(default)s)",
    )
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite_path)
    print(f"Reading from SQLite: {sqlite_path}")
    data = read_sqlite(sqlite_path)
    for table, rows in data.items():
        print(f"  {table}: {len(rows)} rows")

    print(f"Writing to PostgreSQL: {DATABASE_URL}")
    asyncio.run(write_postgres(data))

    print("Verifying row counts...")
    asyncio.run(verify(data))

    print("Migration complete. No rows were deleted from the SQLite source file.")


if __name__ == "__main__":
    main()
