"""
Event handlers for live (realtime) sync.

Handler choice rationale
------------------------
* NewMessage       — catches every new post in the source channel
* Album            — Telethon coalesces grouped media into one event;
                     without this, Album messages arrive as many NewMessage
                     events and we'd have to reassemble them ourselves.
* MessageEdited    — fired when content or caption changes
* MessageDeleted   — fired when messages are removed; note: Telegram
                     does NOT reliably deliver this for channels you are not
                     admin of; we handle it best-effort.
* Raw (UpdateChannel, UpdateChatUserTyping, etc.) are handled via a
  raw update handler for service events (pin, title, photo changes).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from telethon import events
from telethon.tl.types import (
    MessageActionChannelMigrateFrom,
    MessageActionChatEditPhoto,
    MessageActionChatEditTitle,
    MessageActionPinMessage,
    UpdateChannel,
)

from utils.retry import ShutdownRequested, is_shutdown_requested

if TYPE_CHECKING:
    from telethon import TelegramClient
    from utils.config import Config
    from db import Database
    from handlers.sender import MessageSender

logger = logging.getLogger(__name__)


class EventDispatcher:
    """
    Registers all Telethon event handlers and routes them to MessageSender.

    Internal queue
    --------------
    Live events are placed on an asyncio.Queue and processed by a single
    consumer coroutine. This guarantees FIFO ordering even during bursts
    and prevents race conditions between concurrent handler calls.
    """

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
        self._queue: asyncio.Queue = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(self) -> None:
        """Attach all event handlers to the Telethon client."""
        src = self._cfg.source_channel

        @self._client.on(events.NewMessage(chats=src))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            # Album events are handled by on_album; skip grouped here
            if event.message.grouped_id:
                return
            logger.debug("Queuing NewMessage source_id=%d", event.message.id)
            await self._queue.put(("new", event.message))

        @self._client.on(events.Album(chats=src))
        async def on_album(event: events.Album.Event) -> None:
            logger.debug(
                "Queuing Album with %d items (grouped_id=%s)",
                len(event.messages),
                event.messages[0].grouped_id if event.messages else "?",
            )
            await self._queue.put(("album", list(event.messages)))

        @self._client.on(events.MessageEdited(chats=src))
        async def on_edited(event: events.MessageEdited.Event) -> None:
            logger.debug("Queuing Edit source_id=%d", event.message.id)
            await self._queue.put(("edit", event.message))

        @self._client.on(events.MessageDeleted(chats=src))
        async def on_deleted(event: events.MessageDeleted.Event) -> None:
            for msg_id in event.deleted_ids:
                logger.debug("Queuing Delete source_id=%d", msg_id)
                await self._queue.put(("delete", msg_id))

        @self._client.on(events.Raw)
        async def on_raw(update) -> None:
            await self._handle_raw(update)

        logger.info("All event handlers registered for source channel %d.", src)

    async def start_consumer(self) -> None:
        """Start the background consumer coroutine."""
        self._consumer_task = asyncio.create_task(self._consume())
        logger.info("Event consumer started.")

    async def stop_consumer(self) -> None:
        """
        Gracefully drain the queue: every event already queued is processed
        to completion (so nothing is missed), then the consumer exits on its
        own. The timeout below is only a last-resort safety net for a truly
        stuck handler — hitting it can abort whatever message is mid-send, so
        it should not be relied on as the normal shutdown path.
        """
        if self._consumer_task:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._consumer_task, timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    "Event consumer did not stop within 30s; forcing cancellation "
                    "(in-flight item may need to be retried/replayed next run)."
                )
                self._consumer_task.cancel()
            logger.info("Event consumer stopped.")

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            kind, payload = item
            try:
                await self._handle(kind, payload)
            except ShutdownRequested:
                # Mid-handle wait (e.g. a long FloodWait) was interrupted by
                # shutdown. This item was never marked processed, so it will
                # be picked up again by the next historical sync — stop here
                # rather than racing through the rest of the backlog.
                logger.info(
                    "Shutdown requested while handling [%s]; remaining queued "
                    "events will be retried on the next run.", kind,
                )
                self._queue.task_done()
                break
            except Exception as exc:
                logger.error("Consumer error [%s]: %s", kind, exc, exc_info=True)
                self._queue.task_done()
            else:
                self._queue.task_done()

    async def _handle(self, kind: str, payload) -> None:
        if kind == "new":
            await self._sender.send_message(payload)
        elif kind == "album":
            await self._sender.send_album(payload)
        elif kind == "edit":
            await self._sender.edit_message(payload.id, payload)
        elif kind == "delete":
            if self._cfg.enable_delete_sync:
                await self._sender.hard_delete_message(payload)
            else:
                await self._sender.delete_message(payload)
        elif kind == "pin":
            await self._sender.pin_message(payload)
        else:
            logger.debug("Unknown queue item kind: %s", kind)

    # ------------------------------------------------------------------
    # Raw update handler (pins, title, photo changes)
    # ------------------------------------------------------------------

    async def _handle_raw(self, update) -> None:
        """
        Handle raw Telegram updates for service events.

        Telegram does not expose pin / title-change / photo-change events
        through the high-level event system, so we intercept them here.
        """
        # We only care about updates originating from our source channel
        peer_id = getattr(update, "peer_id", None) or getattr(update, "channel_id", None)
        if peer_id is None:
            return

        # Normalise to bare channel id
        channel_id = getattr(peer_id, "channel_id", None) or peer_id
        src_bare = abs(self._cfg.source_channel) % (10**12)  # strip -100 prefix
        if channel_id != src_bare:
            return

        # ------ Message service actions ------
        message = getattr(update, "message", None)
        if message is None:
            return

        action = getattr(message, "action", None)
        if action is None:
            return

        if isinstance(action, MessageActionPinMessage):
            # pin event carries the id of the pinned message
            pinned_src_id = message.reply_to.reply_to_msg_id if message.reply_to else None
            if pinned_src_id:
                logger.info("Pin event for source_id=%d", pinned_src_id)
                await self._queue.put(("pin", pinned_src_id))

        elif isinstance(action, MessageActionChatEditTitle):
            logger.info(
                "Channel title changed to '%s' — not mirrored (service event).",
                action.title,
            )

        elif isinstance(action, MessageActionChatEditPhoto):
            logger.info(
                "Channel photo changed — not mirrored (would require admin rights)."
            )

        else:
            logger.debug("Unhandled service action: %s", type(action).__name__)