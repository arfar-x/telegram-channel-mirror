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
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import (
    EditMessageRequest,
    SendMediaRequest,
    SendMultiMediaRequest,
    UpdatePinnedMessageRequest,
)
from telethon.tl.types import (
    InputMediaPoll,
    InputMediaUploadedDocument,
    InputMediaUploadedPhoto,
    InputReplyToMessage,
    InputSingleMedia,
    Message,
    MessageEntityCustomEmoji,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaPoll,
    Poll,
    PollAnswer,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    DocumentAttributeImageSize,
)

from db import Database
from utils.media import MediaHandler
from utils.retry import with_retry

logger = logging.getLogger(__name__)


class MessageSender:
    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        media_handler: MediaHandler,
        dest_channel: int,
    ) -> None:
        self._client = client
        self._db = db
        self._mh = media_handler
        self._dest = dest_channel

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def send_message(self, message: Message, *, delay: float = 0.0) -> int | None:
        """
        Mirror a single message. Returns destination message id or None on failure.
        Skips if already processed.
        """
        if await self._db.is_processed(message.id):
            logger.debug("Already processed source_id=%d, skipping.", message.id)
            return await self._db.get_dest_id(message.id)

        if delay:
            await asyncio.sleep(delay)

        try:
            dest_id = await self._dispatch(message)
        except Exception as exc:
            logger.error("Failed to mirror message %d: %s", message.id, exc, exc_info=True)
            # Store with dest_id=None so we don't retry indefinitely
            await self._db.upsert_mapping(
                source_id=message.id,
                dest_id=None,
                media_type=MediaHandler.media_type(message),
                created_at=time.time(),
            )
            return None

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
        """Apply an edit from source to the mirrored destination message."""
        dest_id = await self._db.get_dest_id(source_id)
        if not dest_id:
            logger.debug("Edit for unknown source_id=%d — queuing for later.", source_id)
            await self._db.add_pending_edit(source_id, time.time())
            return

        try:
            await self._apply_edit(dest_id, new_message)
            logger.info("Edited dest_id=%d (source_id=%d)", dest_id, source_id)
        except Exception as exc:
            logger.error("Edit failed for dest_id=%d: %s", dest_id, exc, exc_info=True)

    async def delete_message(self, source_id: int) -> None:
        """
        Soft-delete: mark source as deleted in DB.
        Optionally hard-delete the destination message if ENABLE_DELETE_SYNC=true.
        Called from the event handler which handles the enable/disable logic.
        """
        await self._db.mark_deleted(source_id)

    async def hard_delete_message(self, source_id: int) -> None:
        """Hard-delete the mirrored destination message."""
        dest_id = await self._db.get_dest_id(source_id)
        await self._db.mark_deleted(source_id)
        if not dest_id:
            return
        try:
            await self._client.delete_messages(self._dest, [dest_id])
            logger.info("Deleted dest_id=%d (source_id=%d)", dest_id, source_id)
        except Exception as exc:
            logger.warning("Could not delete dest_id=%d: %s", dest_id, exc)

    async def pin_message(self, source_id: int) -> None:
        """Pin the mirrored message in the destination channel."""
        dest_id = await self._db.get_dest_id(source_id)
        if not dest_id:
            logger.warning(
                "Pin event for source_id=%d but no dest mapping found.", source_id
            )
            return
        try:
            await self._client(
                UpdatePinnedMessageRequest(peer=self._dest, id=dest_id)
            )
            logger.info("Pinned dest_id=%d (source_id=%d)", dest_id, source_id)
        except Exception as exc:
            logger.warning("Could not pin dest_id=%d: %s", dest_id, exc)

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

        sent = await self._client.send_message(
            entity=self._dest,
            message=message.message or "",
            formatting_entities=message.entities,
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
            logger.warning("Could not download media for source_id=%d", message.id)
            return None

        reply_to = await self._resolve_reply(message)
        caption = message.message or ""
        entities = message.entities

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
        path = await self._mh.download(message)
        if not path:
            return None
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
            caption = first_msg.message or ""
            entities = first_msg.entities

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
        if new_message.media and not isinstance(new_message.media, MessageMediaPoll):
            # Media edit — Telegram only allows caption edits, not media swap
            await self._client.edit_message(
                entity=self._dest,
                message=dest_id,
                text=new_message.message or "",
                formatting_entities=new_message.entities,
            )
        else:
            await self._client.edit_message(
                entity=self._dest,
                message=dest_id,
                text=new_message.message or "",
                formatting_entities=new_message.entities,
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