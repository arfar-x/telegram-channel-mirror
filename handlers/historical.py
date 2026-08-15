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

Pending edits
--------------
An edit arriving for a source_id not yet mirrored (a race during backfill)
is queued in the pending_edits table by MessageSender.edit_message() rather
than dropped. Every run() replays anything still queued there once its
target has since been mirrored (see _sync_pending_edits below), instead of
leaving it stuck forever.

Exactly-once / never-skip
---------------------------
Nothing in this module ever marks a message, pin, or edit permanently
"done" on failure. A genuine failure (flood wait, transient Telegram/network
error, ...) propagates out of run() so main.py's outer retry loop calls
run() again with backoff — forever, until it succeeds — rather than the
item being silently skipped. The trade-off: since messages are mirrored
strictly oldest -> newest, a message that can genuinely never succeed (not
just rate-limited, but permanently undeliverable) blocks everything behind
it in the historical backlog until someone investigates; live sync keeps
running independently in the meantime (see EventDispatcher).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon.tl.types import InputMessagesFilterPinned

from utils.retry import ShutdownRequested, is_shutdown_requested

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
            return

        logger.info("Historical sync complete. Total messages processed: %d", total)

        # Both passes always run, independently of one another, so a failure
        # in one never blocks the other from being attempted this pass. If
        # either reports failures, we raise at the end so main.py's outer
        # retry loop calls run() again — nothing here is ever given up on.
        failed_passes: list[str] = []
        for label, pass_fn in (
            ("pin sync", self._sync_pinned_messages),
            ("pending-edit replay", self._sync_pending_edits),
        ):
            try:
                await pass_fn()
            except ShutdownRequested:
                raise
            except Exception as exc:
                logger.error("%s failed this pass: %s", label, exc, exc_info=True)
                failed_passes.append(label)
        if failed_passes:
            raise RuntimeError(
                f"{', '.join(failed_passes)} had unresolved failures this "
                "pass; will retry."
            )

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
        skipped (and picked up on a later run once they are). A failure
        pinning one message doesn't stop the rest from being attempted —
        failures are collected and raised together at the end so the whole
        pass gets retried without one bad pin blocking its siblings.
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
        failures: list[int] = []
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

            try:
                await self._sender.pin_message(source_id)
            except ShutdownRequested:
                raise
            except Exception as exc:
                logger.error("Failed to pin source_id=%d: %s", source_id, exc, exc_info=True)
                failures.append(source_id)

        if failures:
            raise RuntimeError(f"Failed to pin {len(failures)} message(s): {failures}")

    async def _sync_pending_edits(self) -> None:
        """
        Replay edits queued in pending_edits (MessageSender.edit_message()
        queues an edit there when it arrives before its target has been
        mirrored). Once the target is mirrored, refetch its current state
        from the source and apply the edit — this is what actually
        implements the replay the pending_edits table exists for.

        Same continue-on-failure-then-raise pattern as _sync_pinned_messages:
        one stuck edit doesn't block the rest from being retried this pass.
        """
        pending_ids = await self._db.get_pending_edit_ids()
        if not pending_ids:
            return

        failures: list[int] = []
        for source_id in pending_ids:
            if is_shutdown_requested():
                logger.info(
                    "Shutdown requested — stopping pending-edit replay; "
                    "will resume next run."
                )
                return

            if not await self._db.is_processed(source_id):
                continue  # target still not mirrored; keep waiting

            try:
                fresh = await self._client.get_messages(
                    self._cfg.source_channel, ids=source_id
                )
                if fresh is None:
                    # Source message is gone — nothing left to replay.
                    await self._db.remove_pending_edit(source_id)
                    continue
                await self._sender.edit_message(source_id, fresh)
                await self._db.remove_pending_edit(source_id)
                logger.info("Replayed queued edit for source_id=%d.", source_id)
            except ShutdownRequested:
                raise
            except Exception as exc:
                logger.error(
                    "Failed to replay queued edit for source_id=%d: %s",
                    source_id, exc, exc_info=True,
                )
                failures.append(source_id)

        if failures:
            raise RuntimeError(
                f"Failed to replay {len(failures)} queued edit(s): {failures}"
            )

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
