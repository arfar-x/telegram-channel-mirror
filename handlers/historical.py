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

Albums in historical sync
--------------------------
Telethon's iter_messages does NOT fire Album events; it yields individual
messages. We detect album membership by grouped_id and batch them manually.
We send the batch once we see a different grouped_id — i.e., we look ahead
by one message. This works because Telegram stores album messages consecutively.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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