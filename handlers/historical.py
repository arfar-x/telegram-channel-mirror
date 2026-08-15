"""
Historical sync
===============
Fetches ALL existing messages from the source channel and mirrors them
OLDEST → NEWEST, preserving ordering.

Algorithm
---------
1. Determine the highest already-processed source_id from DB (resume cursor).
2. Iterate messages from min_id=cursor+1 with reverse=True (oldest first).
3. For each message:
   a. Skip if already in DB.
   b. Detect albums: buffer messages sharing the same grouped_id and send
      as a group when the group is complete (next grouped_id differs or we
      hit a non-grouped message).
   c. Send, store mapping, advance cursor.
4. Flush any remaining album buffer at end.
5. Re-sync pinned messages (see below).

Albums in historical sync
--------------------------
Telethon's iter_messages does NOT fire Album events; it yields individual
messages. We detect album membership by grouped_id and batch them manually.
We send the batch once we see a different grouped_id — i.e., we look ahead
by one message. This works because Telegram stores album messages consecutively.

Pinned messages
----------------
MessageActionPinMessage (handled live in EventDispatcher) only fires for a
pin action that happens *while the daemon is connected* — a message already
pinned before the daemon's first run produces no event at all, so it would
otherwise never get pinned in the destination. To cover that, every run()
finishes (once it isn't stopping early for shutdown) with a pass that lists
the source channel's *current* pinned messages and pins their mirrored
counterparts in the destination. This is cheap and idempotent, so it doubles
as self-healing for a live pin event that arrived before its target message
had been mirrored yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from telethon.tl.types import InputMessagesFilterPinned

from utils.retry import is_shutdown_requested

if TYPE_CHECKING:
    from telethon import TelegramClient
    from db import Database
    from handlers.sender import MessageSender
    from utils.config import Config

logger = logging.getLogger(__name__)

PROGRESS_KEY = "historical_min_id"


class HistoricalSync:
    def __init__(
        self,
        client: "TelegramClient",
        sender: "MessageSender",
        db: "Database",
        config: "Config",
    ) -> None:
        self._client = client
        self._sender = sender
        self._db = db
        self._cfg = config

    async def run(self) -> None:
        """Full historical sync. Safe to call on restart (resumes)."""
        cursor = await self._get_cursor()
        logger.info(
            "Starting historical sync from min_id=%d (0 = full sync)", cursor
        )

        total = 0
        album_buffer: list = []  # buffer of messages sharing a grouped_id
        current_group_id: int | None = None
        stopped_early = False

        async for message in self._client.iter_messages(
            self._cfg.source_channel,
            reverse=True,       # oldest first
            min_id=cursor,
        ):
            # Checked before touching the next message — never abandons one
            # already mid-send. Anything left unsent here keeps its place
            # (cursor untouched) and gets picked up again on the next run.
            if is_shutdown_requested():
                logger.info(
                    "Shutdown requested — stopping historical sync; "
                    "will resume from current cursor next run."
                )
                stopped_early = True
                break

            # Skip already-processed messages (handles resume after crash)
            if await self._db.is_processed(message.id):
                continue

            gid = message.grouped_id

            if gid is not None:
                # This message belongs to an album
                if gid == current_group_id:
                    album_buffer.append(message)
                else:
                    # New group encountered — flush previous buffer first
                    if album_buffer:
                        await self._flush_album(album_buffer)
                        total += len(album_buffer)
                    album_buffer = [message]
                    current_group_id = gid
            else:
                # Not an album message — flush pending album first
                if album_buffer:
                    await self._flush_album(album_buffer)
                    total += len(album_buffer)
                    album_buffer = []
                    current_group_id = None

                await self._sender.send_message(
                    message, delay=self._cfg.historical_send_delay
                )
                await self._advance_cursor(message.id)
                total += 1

                if total % 50 == 0:
                    logger.info("Historical sync progress: %d messages processed.", total)

        # Flush any trailing album — but not if we stopped early for shutdown;
        # those items haven't been touched and should wait for the next run.
        if album_buffer and not stopped_early:
            await self._flush_album(album_buffer)
            total += len(album_buffer)

        if stopped_early:
            logger.info("Historical sync paused. Messages processed this run: %d", total)
        else:
            logger.info("Historical sync complete. Total messages processed: %d", total)
            await self._sync_pinned_messages()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _sync_pinned_messages(self) -> None:
        """
        Pin every currently-pinned source message in the destination.

        Best-effort ordering: Telegram doesn't expose a separate "pin time"
        for messages returned by InputMessagesFilterPinned, so pins are
        (re-)applied oldest source id -> newest, which makes the
        highest-id pinned message the most-recently-pinned (topmost) one in
        the destination. This matches the common case but can differ from
        the true pin order if an older message was pinned more recently
        than a newer one.

        Safe to run every startup: pinning an already-pinned destination
        message is a no-op, and messages not yet mirrored are simply
        skipped (and picked up on a later run once they are).
        """
        pinned_source_ids = sorted(
            [
                message.id
                async for message in self._client.iter_messages(
                    self._cfg.source_channel, filter=InputMessagesFilterPinned
                )
            ]
        )
        if not pinned_source_ids:
            return

        logger.info("Syncing %d pinned message(s) to destination.", len(pinned_source_ids))
        for source_id in pinned_source_ids:
            if is_shutdown_requested():
                logger.info(
                    "Shutdown requested — stopping pin sync; will resume next run."
                )
                return

            dest_id = await self._db.get_dest_id(source_id)
            if not dest_id:
                logger.debug(
                    "Pinned source_id=%d not yet mirrored — will sync its pin "
                    "on a later run.",
                    source_id,
                )
                continue

            await self._sender.pin_message(source_id)

    async def _flush_album(self, messages: list) -> None:
        """Send a buffered album group and advance cursor."""
        if not messages:
            return
        logger.debug(
            "Flushing album grouped_id=%s (%d items)",
            messages[0].grouped_id,
            len(messages),
        )
        await self._sender.send_album(
            messages, delay=self._cfg.historical_send_delay
        )
        await self._advance_cursor(messages[-1].id)

    async def _get_cursor(self) -> int:
        val = await self._db.get_progress(PROGRESS_KEY)
        return int(val) if val else 0

    async def _advance_cursor(self, message_id: int) -> None:
        await self._db.set_progress(PROGRESS_KEY, str(message_id))