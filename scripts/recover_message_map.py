"""
Recovery: rebuild message_map after the database (or just message_map) is lost,
without re-sending anything and without disrupting a running main.py.

Background
----------
If the database is lost but the destination channel already holds every
previously mirrored message, the source and destination channels are the
only remaining record of the source_id -> dest_id mapping. There is no
built-in correlation (forwarding is disabled), so this script pairs the two
channels positionally: HistoricalSync always mirrors oldest -> newest,
one-for-one, so walking both channels oldest -> newest should line them up.

This is split into two modes that are safe to run at the same time as
main.py, because they partition source message ids around a fixed snapshot
boundary (see `recovery_boundary_id` below) that main.py never crosses
backwards over and this script never crosses forwards over:

1. --bootstrap-cursor
   Run this FIRST, immediately after restoring the DB schema, before
   starting main.py. It snapshots "the current latest source message id"
   into both `historical_min_id` (the live cursor main.py's HistoricalSync
   already reads) and `recovery_boundary_id` (read-only from here on out).
   This unblocks main.py immediately: it will only mirror messages newer
   than the snapshot, never touching the already-mirrored history, so it
   can safely run as a daemon while you backfill message_map at your leisure.

2. (default) pairing mode, dry-run unless --apply is passed
   Walks the source channel from the beginning up to recovery_boundary_id
   and the destination channel from the beginning, pairing them up and
   writing message_map rows for the old range. Never touches ids above the
   boundary, so it can be run anytime, repeatedly, while main.py keeps
   mirroring new messages above the boundary -- there is no overlap to
   coordinate.

Usage
-----
    python scripts/recover_message_map.py --bootstrap-cursor
    python scripts/recover_message_map.py              # dry run
    python scripts/recover_message_map.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.tl.types import Message, MessageMediaPoll

from db import Database
from utils import load_config, setup_logging
from utils.media import MediaHandler

logger = logging.getLogger(__name__)

Unit = Union[Message, list[Message]]

DEFAULT_LOOKAHEAD = 10
PROGRESS_CURSOR_KEY = "historical_min_id"
PROGRESS_BOUNDARY_KEY = "recovery_boundary_id"


# ----------------------------------------------------------------------
# Classification — mirrors MessageSender._dispatch's routing exactly,
# without sending anything.
# ----------------------------------------------------------------------

def classify(message: Message) -> str | None:
    if isinstance(message.media, MessageMediaPoll):
        return "poll"
    if message.sticker:
        return "sticker"
    if message.media:
        return MediaHandler.media_type(message)
    if message.text or message.message:
        return "text"
    return None  # service message — _dispatch never produced a dest message for these


def unit_messages(unit: Unit) -> list[Message]:
    return unit if isinstance(unit, list) else [unit]


def unit_ids(unit: Unit) -> list[int]:
    return [m.id for m in unit_messages(unit)]


def unit_classify(unit: Unit) -> list[str | None]:
    return [classify(m) for m in unit_messages(unit)]


def unit_snippet(unit: Unit) -> str:
    msg = unit_messages(unit)[0]
    return (msg.message or "").replace("\n", " ")[:60]


def units_match(src: Unit, dest: Unit) -> bool:
    src_msgs, dest_msgs = unit_messages(src), unit_messages(dest)
    if len(src_msgs) != len(dest_msgs):
        return False
    return all(classify(s) == classify(d) for s, d in zip(src_msgs, dest_msgs))


# ----------------------------------------------------------------------
# Grouping — same album-buffering algorithm as handlers/historical.py,
# applied to either channel.
# ----------------------------------------------------------------------

async def group_into_units(
    client: TelegramClient, channel: int, *, max_id: int | None = None
) -> AsyncIterator[Unit]:
    kwargs = {"reverse": True}
    if max_id is not None:
        kwargs["max_id"] = max_id + 1  # Telethon's max_id is exclusive

    buffer: list[Message] = []
    current_gid: int | None = None

    async for message in client.iter_messages(channel, **kwargs):
        gid = message.grouped_id
        if gid is not None:
            if gid == current_gid:
                buffer.append(message)
            else:
                if buffer:
                    yield buffer
                buffer = [message]
                current_gid = gid
        else:
            if buffer:
                yield buffer
                buffer = []
                current_gid = None
            yield message

    if buffer:
        yield buffer


# ----------------------------------------------------------------------
# Pairing
# ----------------------------------------------------------------------

async def _fill_window(dest_gen: AsyncIterator[Unit], window: deque, size: int) -> None:
    while len(window) < size:
        try:
            window.append(await dest_gen.__anext__())
        except StopAsyncIteration:
            break


async def pair(
    client: TelegramClient,
    db: Database,
    source_channel: int,
    destination_channel: int,
    boundary_id: int,
    lookahead: int,
    apply: bool,
) -> tuple[int, int, int, int | None, bool]:
    """Returns (paired, skipped_service, skipped_orphans, last_paired_source_id, aborted)."""
    src_gen = group_into_units(client, source_channel, max_id=boundary_id)
    dest_gen = group_into_units(client, destination_channel)

    window: deque[Unit] = deque()
    paired = 0
    skipped_service = 0
    skipped_orphans = 0
    last_paired_source_id: int | None = None
    aborted = False

    async for src_unit in src_gen:
        if all(c is None for c in unit_classify(src_unit)):
            skipped_service += 1
            continue

        await _fill_window(dest_gen, window, lookahead)

        match_idx = None
        for i, dest_unit in enumerate(window):
            if units_match(src_unit, dest_unit):
                match_idx = i
                break

        if match_idx is None:
            if not window:
                logger.info(
                    "Destination exhausted — stopping cleanly. "
                    "%d paired, %d skipped service, %d presumed-deleted orphans.",
                    paired, skipped_service, skipped_orphans,
                )
                break

            logger.error(
                "No match in lookahead window for source unit ids=%s classify=%s snippet=%r",
                unit_ids(src_unit), unit_classify(src_unit), unit_snippet(src_unit),
            )
            for i, dest_unit in enumerate(window):
                logger.error(
                    "  window[%d]: dest ids=%s classify=%s snippet=%r",
                    i, unit_ids(dest_unit), unit_classify(dest_unit), unit_snippet(dest_unit),
                )
            aborted = True
            break

        for _ in range(match_idx):
            orphan = window.popleft()
            skipped_orphans += 1
            logger.info(
                "Skipped presumed-deleted orphan: dest ids=%s classify=%s snippet=%r",
                unit_ids(orphan), unit_classify(orphan), unit_snippet(orphan),
            )

        dest_unit = window.popleft()
        for s, d in zip(unit_messages(src_unit), unit_messages(dest_unit)):
            if apply:
                await db.upsert_mapping(
                    source_id=s.id,
                    dest_id=d.id,
                    grouped_id=s.grouped_id,
                    media_type=classify(s),
                    created_at=d.date.timestamp() if d.date else time.time(),
                )
            paired += 1
            last_paired_source_id = s.id
    else:
        logger.info("Reached end of source range cleanly.")

    return paired, skipped_service, skipped_orphans, last_paired_source_id, aborted


# ----------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------

async def bootstrap_cursor(client: TelegramClient, db: Database, source_channel: int) -> None:
    existing = await db.get_progress(PROGRESS_BOUNDARY_KEY)
    if existing is not None:
        raise SystemExit(
            f"{PROGRESS_BOUNDARY_KEY} is already set to {existing} — bootstrap is a "
            "one-time snapshot and refuses to run twice (re-running it later would "
            "move the partition and risk overlapping with messages main.py has "
            "already mirrored)."
        )

    latest = await client.get_messages(source_channel, limit=1)
    if not latest:
        raise SystemExit("Source channel has no messages — nothing to bootstrap.")

    latest_id = latest[0].id
    await db.set_progress(PROGRESS_CURSOR_KEY, str(latest_id))
    await db.set_progress(PROGRESS_BOUNDARY_KEY, str(latest_id))
    print(f"Bootstrapped: {PROGRESS_CURSOR_KEY} = {PROGRESS_BOUNDARY_KEY} = {latest_id}")
    print("main.py can now be started safely — it will only mirror messages newer than this.")


async def run_pairing(
    client: TelegramClient,
    db: Database,
    source_channel: int,
    destination_channel: int,
    lookahead: int,
    apply: bool,
    force: bool,
) -> None:
    raw_boundary = await db.get_progress(PROGRESS_BOUNDARY_KEY)
    if raw_boundary is None:
        raise SystemExit(
            f"{PROGRESS_BOUNDARY_KEY} is not set. Run with --bootstrap-cursor first."
        )
    boundary_id = int(raw_boundary)

    if apply and await db.has_mappings() and not force:
        raise SystemExit(
            "message_map already has rows. This tool rebuilds from scratch; "
            "pass --force to proceed anyway."
        )

    print(f"Pairing source messages up to id={boundary_id} against the destination channel...")
    paired, skipped_service, skipped_orphans, last_id, aborted = await pair(
        client, db, source_channel, destination_channel, boundary_id, lookahead, apply
    )

    mode = "APPLY" if apply else "DRY RUN"
    print(
        f"[{mode}] paired={paired} skipped_service={skipped_service} "
        f"skipped_orphans={skipped_orphans} last_paired_source_id={last_id}"
    )
    if aborted:
        print("Aborted on an ambiguous mismatch — see log above. Nothing past that point was touched.")
        sys.exit(1)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def _query_session_name(session_name: str) -> str:
    """Clone the session file so this can run alongside main.py without
    'database is locked' on the Telethon session (same trick as get_message.py)."""
    src = Path(f"{session_name}.session")
    dst_name = f"{session_name}_query"
    dst = Path(f"{dst_name}.session")
    if not dst.exists() and src.exists():
        shutil.copyfile(src, dst)
    return dst_name


async def main_async(args: argparse.Namespace) -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)

    query_session = _query_session_name(cfg.session_name)
    client = TelegramClient(query_session, cfg.api_id, cfg.api_hash)
    db = Database()
    await db.connect()

    try:
        async with client:
            if args.bootstrap_cursor:
                await bootstrap_cursor(client, db, cfg.source_channel)
                return

            await run_pairing(
                client,
                db,
                cfg.source_channel,
                cfg.destination_channel,
                args.lookahead,
                args.apply,
                args.force,
            )
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--bootstrap-cursor",
        action="store_true",
        help="One-time: snapshot the current latest source id as the recovery boundary, "
        "unblocking main.py immediately.",
    )
    parser.add_argument("--apply", action="store_true", help="Write message_map rows (default is dry-run).")
    parser.add_argument("--force", action="store_true", help="Proceed even if message_map already has rows.")
    parser.add_argument(
        "--lookahead",
        type=int,
        default=DEFAULT_LOOKAHEAD,
        help=f"Destination-side lookahead window for skipping presumed-deleted orphans (default: {DEFAULT_LOOKAHEAD}).",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
