"""
Self-referential link rewriting.

A source channel post can link to *another post in the same channel*, e.g.
"see t.me/c/2135767632/7134". Mirrored verbatim, that link is wrong on two
counts: it points at the source channel's message id, which means nothing
in the destination channel's own numbering, and the source channel is
usually private, so a destination-only reader can't open it at all. This
module rewrites such links to point at the mirrored counterpart in the
destination channel instead. Links to any other channel/chat are untouched.

Two independent things can carry a link and need separate handling:
* MessageEntityTextUrl — a hyperlink whose displayed text differs from its
  URL. The URL lives entirely in `entity.url`; the message text itself has
  no visible link characters to touch, so only the entity's `.url` changes.
* A plain autolinked URL — the literal characters appear in the message
  text. Rewriting it means slicing/re-assembling the text itself, which
  requires offset arithmetic in Telegram's own units (UTF-16 code units,
  not Python string indices) — see the surrogate-pair note below.

Degrades gracefully, never corrupts a send: a link whose target hasn't been
mirrored yet (no dest_id in message_map) is left exactly as-is rather than
blocking or breaking the message it's part of. A genuine lookup failure
(e.g. a DB error) is NOT swallowed here — it propagates like any other
failure in this codebase, so the caller's retry machinery handles it
(see "Exactly-once / retry philosophy" in CLAUDE.md).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Awaitable, Callable

from telethon import helpers
from telethon.tl.types import MessageEntityTextUrl, TypeMessageEntity

logger = logging.getLogger(__name__)

ResolveDestId = Callable[[int], Awaitable["int | None"]]

# Group 1: optional scheme: "https://" / "http://"
# Group 2: the literal "t.me/c/" (case-insensitive)
# Group 3: bare channel id (digits, no -100 prefix)
# Group 4: message id
# Group 5: optional trailing topic segment / query string, kept verbatim
_SELF_LINK_RE = re.compile(
    r"(https?://)?(t\.me/c/)(\d+)/(\d+)((?:/\d+)?(?:\?[\w=&%.\-]*)?)",
    re.IGNORECASE,
)


async def rewrite_self_links(
    text: "str | None",
    entities: "list[TypeMessageEntity] | None",
    *,
    source_bare_id: int,
    dest_bare_id: int,
    resolve_dest_id: ResolveDestId,
) -> "tuple[str | None, list[TypeMessageEntity] | None]":
    """Rewrite any t.me/c/<source_bare_id>/<id> link to the mirrored dest id."""
    text, entities = await _rewrite_text_url_entities(
        text, entities, source_bare_id, dest_bare_id, resolve_dest_id
    )
    text, entities = await _rewrite_plaintext_links(
        text, entities, source_bare_id, dest_bare_id, resolve_dest_id
    )
    return text, entities


def _match_self_link(url: str, source_bare_id: int):
    m = _SELF_LINK_RE.match(url)
    if not m or int(m.group(3)) != source_bare_id:
        return None
    return m


async def _rewrite_text_url_entities(
    text, entities, source_bare_id, dest_bare_id, resolve_dest_id
):
    if not entities:
        return text, entities

    new_entities = None
    for i, entity in enumerate(entities):
        if not isinstance(entity, MessageEntityTextUrl):
            continue
        m = _match_self_link(entity.url, source_bare_id)
        if m is None:
            continue
        source_msg_id = int(m.group(4))
        dest_id = await resolve_dest_id(source_msg_id)
        if dest_id is None:
            logger.debug(
                "Self-link to source_id=%d in hidden hyperlink not yet "
                "mirrored — leaving it as-is.", source_msg_id,
            )
            continue
        if new_entities is None:
            new_entities = list(entities)
        new_entity = copy.copy(entity)
        new_entity.url = (
            f"{m.group(1) or ''}{m.group(2)}{dest_bare_id}/{dest_id}{m.group(5) or ''}"
        )
        new_entities[i] = new_entity

    return text, (new_entities if new_entities is not None else entities)


async def _rewrite_plaintext_links(
    text, entities, source_bare_id, dest_bare_id, resolve_dest_id
):
    if not text:
        return text, entities

    stext = helpers.add_surrogate(text)
    matches = list(_SELF_LINK_RE.finditer(stext))
    if not matches:
        return text, entities

    replacements = []  # (start, end, new_substr), left-to-right order
    for m in matches:
        if int(m.group(3)) != source_bare_id:
            continue
        source_msg_id = int(m.group(4))
        dest_id = await resolve_dest_id(source_msg_id)
        if dest_id is None:
            logger.debug(
                "Self-link to source_id=%d in message text not yet "
                "mirrored — leaving it as-is.", source_msg_id,
            )
            continue
        new_substr = (
            f"{m.group(1) or ''}{m.group(2)}{dest_bare_id}/{dest_id}{m.group(5) or ''}"
        )
        replacements.append((m.start(), m.end(), new_substr))

    if not replacements:
        return text, entities

    new_entities = [copy.copy(e) for e in entities] if entities else []

    # Apply right-to-left so earlier (start, end) positions stay valid while
    # later ones are rewritten.
    for start, end, new_substr in reversed(replacements):
        delta = len(new_substr) - (end - start)
        kept = []
        for e in new_entities:
            e_start, e_end = e.offset, e.offset + e.length
            if e_end <= start:
                kept.append(e)  # fully before the rewritten span — untouched
            elif e_start >= end:
                e.offset += delta  # fully after — shift with it
                kept.append(e)
            else:
                # Overlaps the rewritten span (e.g. an autolink entity over
                # the exact old URL). Dropped rather than risk emitting a
                # misaligned entity — Telegram clients still auto-linkify a
                # plain http(s) URL in the text with no entity required, so
                # the new link still renders as clickable.
                logger.debug(
                    "Dropping entity %s overlapping a rewritten self-link.",
                    type(e).__name__,
                )
        new_entities = kept
        stext = stext[:start] + new_substr + stext[end:]

    return helpers.del_surrogate(stext), (new_entities or None)
