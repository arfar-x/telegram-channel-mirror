"""
MessageSender
=============
Responsible for recreating any Telegram message in the destination channel
WITHOUT using forward(). Every content type is re-uploaded or re-created from scratch.

Design decisions
----------------
* We never call client.forward_messages() because the source channel has
  forwarding disabled.
* Instead we download media, upload it fresh, and reconstruct text with
  its original formatting entities.
* Polls are recreated with InputPollOption. Quiz polls lose their
  correct_answers because the Bot API exposes that field only to bots;
  user accounts cannot set correct_answers via MTProto either — documented
  in LIMITATIONS.md.
* Custom emoji are preserved by copying the MessageEntityCustomEmoji
  entities as-is; the emoji will render on clients that support them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Sequence

from telethon import TelegramClient
from telethon.tl.functions.messages import (
    SendMediaRequest,
    UpdatePinnedMessageRequest,
)
from telethon.tl.types import (
    InputMediaPoll,
    InputReplyToMessage,
    Message,
    MessageMediaPoll,
    Poll,
    PollAnswer,
)

from db import Database
from utils.links import rewrite_self_links
from utils.media import MediaHandler
from utils.retry import with_retry

logger = logging.getLogger(__name__)


class MessageSender:
    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        media_handler: MediaHandler,
        source_channel: int,
        dest_channel: int,
    ) -> None:
        self._client = client
        self._db = db
        self._mh = media_handler
        self._dest = dest_channel
        # Bare (no -100 prefix) ids, matching how t.me/c/<bare_id>/<msg_id>
        # links encode a channel.
        self._source_bare = abs(source_channel) % (10**12)
        self._dest_bare = abs(dest_channel) % (10**12)

    async def _rewrite_links(self, text, entities):
        """Rewrite self-referential t.me/c/<source>/<id> links to the mirrored dest id."""
        return await rewrite_self_links(
            text,
            entities,
            source_bare_id=self._source_bare,
            dest_bare_id=self._dest_bare,
            resolve_dest_id=self._db.get_dest_id,
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def send_message(self, message: Message, *, delay: float = 0.0) -> int | None:
        """
        Mirror a single message. Returns the destination message id, or None
        if the message is a legitimate no-op (a service message, or media
        with no downloadable file and no text). Skips if already processed.

        Exactly-once contract: a genuine failure (flood wait, transient
        Telegram/network error, ...) is never swallowed into a permanent
        "done, dest_id=None" row — it propagates so the caller (HistoricalSync
        via main.py's retry loop, or EventDispatcher's consumer) retries the
        whole call again later instead of silently skipping the message.
        """
        if await self._db.is_processed(message.id):
            logger.debug("Already processed source_id=%d, skipping.", message.id)
            return await self._db.get_dest_id(message.id)

        if delay:
            await asyncio.sleep(delay)

        dest_id = await self._dispatch(message)

        logger.info(
            "Mirrored source_id=%d → dest_id=%s  (type=%s)",
            message.id,
            dest_id,
            MediaHandler.media_type(message) or "text",
        )
        return dest_id

    async def send_album(self, messages: Sequence[Message], *, delay: float = 0.0) -> None:
        """Mirror a grouped (album) set of messages as a single album."""
        # Filter already-processed
        to_send = [m for m in messages if not await self._db.is_processed(m.id)]
        if not to_send:
            return

        if delay:
            await asyncio.sleep(delay)

        try:
            await self._send_album_group(to_send)
        except Exception as exc:
            logger.error("Failed to mirror album: %s", exc, exc_info=True)
            # Fallback: send individually
            for msg in to_send:
                await self.send_message(msg)

    async def edit_message(self, source_id: int, new_message: Message) -> None:
        """
        Apply an edit from source to the mirrored destination message.
        Genuine failures propagate (see send_message's exactly-once contract
        above) instead of being logged and dropped.
        """
        dest_id = await self._db.get_dest_id(source_id)
        if not dest_id:
            logger.debug("Edit for unknown source_id=%d — queuing for later.", source_id)
            await self._db.add_pending_edit(source_id, time.time())
            return

        await self._apply_edit(dest_id, new_message)
        logger.info("Edited dest_id=%d (source_id=%d)", dest_id, source_id)

    async def delete_message(self, source_id: int) -> None:
        """
        Soft-delete: mark source as deleted in DB.
        Optionally hard-delete the destination message if ENABLE_DELETE_SYNC=true.
        Called from the event handler which handles the enable/disable logic.
        """
        await self._db.mark_deleted(source_id)

    async def hard_delete_message(self, source_id: int) -> None:
        """
        Hard-delete the mirrored destination message. Genuine failures
        propagate (see send_message's exactly-once contract above); the
        db.mark_deleted() call is idempotent so a retried call is safe.
        """
        dest_id = await self._db.get_dest_id(source_id)
        await self._db.mark_deleted(source_id)
        if not dest_id:
            return
        await self._do_hard_delete(dest_id)
        logger.info("Deleted dest_id=%d (source_id=%d)", dest_id, source_id)

    @with_retry()
    async def _do_hard_delete(self, dest_id: int) -> None:
        await self._client.delete_messages(self._dest, [dest_id])

    async def pin_message(self, source_id: int) -> None:
        """
        Pin the mirrored message in the destination channel. Genuine
        failures propagate (see send_message's exactly-once contract above);
        pinning an already-pinned message is a harmless no-op, so a retried
        call is always safe.

        A live pin event can race ahead of the NewMessage event for its
        target (both land on the same queue, but Telegram doesn't guarantee
        delivery order across update types) — if there's no mapping yet,
        raise so the caller retries with backoff instead of dropping the pin
        for good. HistoricalSync._sync_pinned_messages() pre-checks the
        mapping itself and never calls in here for an unmapped id, so this
        path is only reached from the live queue.
        """
        dest_id = await self._db.get_dest_id(source_id)
        if not dest_id:
            raise LookupError(
                f"No dest mapping yet for source_id={source_id}; "
                "its target hasn't been mirrored yet."
            )
        await self._do_pin(dest_id)
        logger.info("Pinned dest_id=%d (source_id=%d)", dest_id, source_id)

    @with_retry()
    async def _do_pin(self, dest_id: int) -> None:
        await self._client(
            UpdatePinnedMessageRequest(peer=self._dest, id=dest_id)
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, message: Message) -> int | None:
        """Route a message to the appropriate send method."""
        media = message.media

        if isinstance(media, MessageMediaPoll):
            return await self._send_poll(message)

        if message.sticker:
            return await self._send_sticker(message)

        if media:
            return await self._send_media(message)

        if message.text or message.message:
            return await self._send_text(message)

        # Service messages / other — log and skip
        logger.debug("Unhandled message type for source_id=%d", message.id)
        return None

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    @with_retry()
    async def _send_text(self, message: Message) -> int | None:
        reply_to = await self._resolve_reply(message)
        text, entities = await self._rewrite_links(message.message, message.entities)

        sent = await self._client.send_message(
            entity=self._dest,
            message=text or "",
            formatting_entities=entities,
            reply_to=reply_to,
            link_preview=False,
        )
        dest_id = sent.id
        await self._db.upsert_mapping(
            source_id=message.id,
            dest_id=dest_id,
            media_type="text",
            created_at=time.time(),
        )
        return dest_id

    # ------------------------------------------------------------------
    # Single media
    # ------------------------------------------------------------------

    @with_retry()
    async def _send_media(self, message: Message) -> int | None:
        path = await self._mh.download(message)
        if not path:
            # No downloadable file (contact/geo/venue/dice/game, or a bare
            # webpage preview) — not a failure, just nothing to re-upload.
            # Fall back to text so a caption isn't lost; if there's neither
            # a file nor text, there's nothing to mirror at all.
            if message.text or message.message:
                logger.info(
                    "No downloadable file for source_id=%d (media=%s) — "
                    "sending as text instead.",
                    message.id, type(message.media).__name__,
                )
                return await self._send_text(message)
            logger.warning(
                "No downloadable file and no text for source_id=%d (media=%s) "
                "— nothing to mirror; skipping.",
                message.id, type(message.media).__name__,
            )
            return None

        reply_to = await self._resolve_reply(message)
        caption, entities = await self._rewrite_links(message.message, message.entities)
        caption = caption or ""

        # Voice notes and video notes need special flags
        is_voice = MediaHandler.is_voice(message)
        is_video_note = MediaHandler.is_video_note(message)
        spoiler = MediaHandler.supports_spoiler(message)

        try:
            if is_video_note:
                sent = await self._client.send_file(
                    self._dest,
                    file=path,
                    video_note=True,
                    reply_to=reply_to,
                )
            elif is_voice:
                sent = await self._client.send_file(
                    self._dest,
                    file=path,
                    voice_note=True,
                    caption=caption,
                    formatting_entities=entities,
                    reply_to=reply_to,
                )
            else:
                sent = await self._client.send_file(
                    self._dest,
                    file=path,
                    caption=caption,
                    formatting_entities=entities,
                    reply_to=reply_to,
                    spoiler=spoiler,
                )
        finally:
            MediaHandler.cleanup(path)

        dest_id = sent.id if not isinstance(sent, list) else sent[0].id
        await self._db.upsert_mapping(
            source_id=message.id,
            dest_id=dest_id,
            grouped_id=message.grouped_id,
            media_type=MediaHandler.media_type(message),
            created_at=time.time(),
        )
        return dest_id

    # ------------------------------------------------------------------
    # Stickers
    # ------------------------------------------------------------------

    @with_retry()
    async def _send_sticker(self, message: Message) -> int | None:
        # message.sticker implies a MessageMediaDocument, so download() here
        # always returns a Path or raises — never a legitimate None.
        path = await self._mh.download(message)
        reply_to = await self._resolve_reply(message)
        try:
            sent = await self._client.send_file(
                self._dest,
                file=path,
                reply_to=reply_to,
            )
        finally:
            MediaHandler.cleanup(path)

        dest_id = sent.id
        await self._db.upsert_mapping(
            source_id=message.id,
            dest_id=dest_id,
            media_type="sticker",
            created_at=time.time(),
        )
        return dest_id

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------

    async def _send_album_group(self, messages: Sequence[Message]) -> None:
        """
        Download all album items, upload them as a single grouped send.
        The first message's caption becomes the album caption.
        """
        paths: list[tuple[Message, object]] = []

        for msg in messages:
            if msg.media:
                p = await self._mh.download(msg)
                if p:
                    paths.append((msg, p))

        if not paths:
            return

        try:
            files = [p for _, p in paths]
            # Caption goes on first item only (Telegram album behaviour)
            first_msg = paths[0][0]
            caption, entities = await self._rewrite_links(
                first_msg.message, first_msg.entities
            )
            caption = caption or ""

            reply_to = await self._resolve_reply(first_msg)

            sent_list = await self._client.send_file(
                self._dest,
                file=files,
                caption=caption,
                formatting_entities=entities,
                reply_to=reply_to,
            )
            if not isinstance(sent_list, list):
                sent_list = [sent_list]

            # Map each source message to its dest counterpart
            for (src_msg, _), dest_msg in zip(paths, sent_list):
                await self._db.upsert_mapping(
                    source_id=src_msg.id,
                    dest_id=dest_msg.id,
                    grouped_id=src_msg.grouped_id,
                    media_type=MediaHandler.media_type(src_msg),
                    created_at=time.time(),
                )
                logger.info(
                    "Mirrored album item source_id=%d → dest_id=%d",
                    src_msg.id,
                    dest_msg.id,
                )
        finally:
            for _, p in paths:
                MediaHandler.cleanup(p)

    # ------------------------------------------------------------------
    # Polls
    # ------------------------------------------------------------------

    @with_retry()
    async def _send_poll(self, message: Message) -> int | None:
        """
        Recreate a poll. Quiz correct_answers CANNOT be set via user MTProto
        (only bots can). The poll will appear as a regular quiz without highlighting
        the correct answer — see LIMITATIONS.md.
        """
        poll_media: MessageMediaPoll = message.media
        original: Poll = poll_media.poll

        answers = [
            PollAnswer(text=a.text, option=a.option)
            for a in original.answers
        ]

        reply_to = await self._resolve_reply(message)

        poll = Poll(
            id=0,
            question=original.question,
            answers=answers,
            public_voters=original.public_voters,
            multiple_choice=bool(original.multiple_choice),
            quiz=bool(original.quiz),
        )

        entity = await self._client.get_input_entity(self._dest)
        request = SendMediaRequest(
            peer=entity,
            media=InputMediaPoll(poll=poll),
            message="",
            reply_to=InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None,
        )
        result = await self._client(request)
        sent = self._client._get_response_message(request, result, entity)
        dest_id = sent.id
        await self._db.upsert_mapping(
            source_id=message.id,
            dest_id=dest_id,
            media_type="poll",
            created_at=time.time(),
        )
        return dest_id

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------

    @with_retry()
    async def _apply_edit(self, dest_id: int, new_message: Message) -> None:
        # Media edits only allow caption/text changes anyway (Telegram API
        # restriction — see LIMITATIONS.md #2), so both branches send the
        # same thing; kept separate to mirror the original media-vs-text
        # distinction for readability.
        text, entities = await self._rewrite_links(
            new_message.message, new_message.entities
        )
        if new_message.media and not isinstance(new_message.media, MessageMediaPoll):
            # Media edit — Telegram only allows caption edits, not media swap
            await self._client.edit_message(
                entity=self._dest,
                message=dest_id,
                text=text or "",
                formatting_entities=entities,
            )
        else:
            await self._client.edit_message(
                entity=self._dest,
                message=dest_id,
                text=text or "",
                formatting_entities=entities,
            )

    # ------------------------------------------------------------------
    # Reply resolution
    # ------------------------------------------------------------------

    async def _resolve_reply(self, message: Message) -> int | None:
        """
        Map the source reply_to_msg_id to the destination message id.
        Returns None if not found (reply chain breaks gracefully).
        """
        if not message.reply_to:
            return None
        src_reply_id = message.reply_to.reply_to_msg_id
        dest_reply_id = await self._db.get_dest_id(src_reply_id)
        if dest_reply_id is None:
            logger.debug(
                "Reply target source_id=%d not yet mirrored; reply chain broken.",
                src_reply_id,
            )
        return dest_reply_id
