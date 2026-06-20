"""
Media download and upload helpers.

Strategy
--------
* Download media to a temp file, upload it, then delete the temp file.
* Never forward — always re-upload to bypass forwarding restrictions.
* A semaphore limits concurrent downloads to avoid memory pressure.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeAnimated,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MediaHandler:
    def __init__(
        self,
        client: TelegramClient,
        temp_dir: Path,
        max_concurrent: int = 3,
    ) -> None:
        self._client = client
        self._temp_dir = temp_dir
        self._sem = asyncio.Semaphore(max_concurrent)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download(self, message: Message) -> Path | None:
        """Download message media to a temp file; return the path or None."""
        if not message.media:
            return None

        async with self._sem:
            path = await self._client.download_media(
                message,
                file=self._temp_dir,
            )
            if path:
                logger.debug("Downloaded media → %s", path)
                return Path(path)
            return None

    @staticmethod
    def cleanup(path: Path | None) -> None:
        """Delete a temp file silently."""
        if path and path.exists():
            try:
                path.unlink()
                logger.debug("Cleaned temp file: %s", path)
            except OSError as exc:
                logger.warning("Could not delete temp file %s: %s", path, exc)

    @staticmethod
    def media_type(message: Message) -> str | None:
        """Return a human-readable media type string for the message."""
        media = message.media
        if media is None:
            return None
        if isinstance(media, MessageMediaPhoto):
            return "photo"
        if isinstance(media, MessageMediaDocument):
            doc = media.document
            attrs = {type(a): a for a in doc.attributes}
            if DocumentAttributeSticker in attrs:
                return "sticker"
            if DocumentAttributeAnimated in attrs:
                return "gif"
            if DocumentAttributeVideo in attrs:
                attr = attrs[DocumentAttributeVideo]
                return "gif" if getattr(attr, "round_message", False) else "video"
            if DocumentAttributeAudio in attrs:
                attr = attrs[DocumentAttributeAudio]
                return "voice" if getattr(attr, "voice", False) else "audio"
            return "document"
        return "unknown"

    @staticmethod
    def is_sticker(message: Message) -> bool:
        media = message.media
        if not isinstance(media, MessageMediaDocument):
            return False
        return any(
            isinstance(a, DocumentAttributeSticker)
            for a in media.document.attributes
        )

    @staticmethod
    def is_voice(message: Message) -> bool:
        media = message.media
        if not isinstance(media, MessageMediaDocument):
            return False
        for a in media.document.attributes:
            if isinstance(a, DocumentAttributeAudio) and getattr(a, "voice", False):
                return True
        return False

    @staticmethod
    def is_video_note(message: Message) -> bool:
        """Round video messages."""
        media = message.media
        if not isinstance(media, MessageMediaDocument):
            return False
        for a in media.document.attributes:
            if isinstance(a, DocumentAttributeVideo) and getattr(
                a, "round_message", False
            ):
                return True
        return False

    @staticmethod
    def supports_spoiler(message: Message) -> bool:
        """Check if the media has a spoiler flag."""
        media = message.media
        if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
            return bool(getattr(media, "spoiler", False))
        return False