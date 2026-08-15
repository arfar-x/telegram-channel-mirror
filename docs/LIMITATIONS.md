# Known Telegram API Limitations

## 1. Polls — Quiz Correct Answers

**Issue:** Quiz-mode polls have a `correct_answers` field. The MTProto API only allows bots (not user accounts) to set this field when creating polls. When recreating a quiz poll from a user session, the correct answer highlight is lost.

**Behaviour:** The poll is recreated faithfully (question, options, quiz flag, anonymous flag, multiple choice) but without a highlighted correct answer. Participants can still vote but won't see green/red result highlights.

**Workaround:** None available for user accounts. If you run this via a bot token (not supported by this project as-is), `correct_answers` can be set.

---

## 2. Media Edits (Swapping Media)

**Issue:** Telegram's `editMessage` endpoint does not allow changing the media file itself — only the caption/text. If a source message's media is replaced, we can only update the caption in the destination.

**Behaviour:** Caption edits are applied; media-swap edits are silently ignored (caption only is updated).

---

## 3. MessageDeleted Events

**Issue:** Telegram does not guarantee delivery of delete events for channels where the user/bot is not an admin. Delete events are best-effort.

**Behaviour:** When received, the source message is marked `is_deleted=1` in the DB. If `ENABLE_DELETE_SYNC=true`, the destination message is also deleted.

---

## 4. Channel Photo Changes

**Issue:** Updating a channel's profile photo requires admin rights on the **destination** channel with the "Change channel info" permission. The script logs the event but does not attempt to mirror it.

**Workaround:** Mirror this manually or grant the account admin rights.

---

## 5. Channel Title Changes

**Issue:** Same as photo changes — requires admin rights. Logged, not mirrored.

---

## 6. Custom Emoji

**Issue:** Custom emoji (Telegram Premium) are stored as `MessageEntityCustomEmoji` entities with an `document_id`. These are preserved in outgoing messages and will render correctly **if the destination channel's viewers also have Telegram Premium**. There is no API limitation preventing the send itself.

---

## 7. Round Videos (Video Notes)

**Issue:** Round video messages (`video_note=True`) cannot have captions. Any caption on the source round video is silently dropped.

---

## 8. Reply Chains on Historical Sync

**Issue:** If a reply references a message that was sent before the bot started (or that we failed to mirror), the reply chain is broken. The mirrored message is sent without a reply-to rather than failing.

---

## 9. Albums Edited After Initial Send

**Issue:** Telegram does not allow editing which messages belong to an album after it's been sent. If a source album is edited (e.g. a photo is removed from the album), we can only update the caption of the already-sent album in the destination; the album structure itself cannot change.

---

## 10. Sticker Packs (Animated/Premium)

**Issue:** Premium stickers and some animated stickers may fail to re-upload if the file ID is not accessible or the sticker is from a restricted pack. In this case, the upload is silently skipped and an error is logged.

---

## 11. Service Messages (Joins, Leaves)

**Issue:** Join/leave events are user-membership events and are not meaningful to mirror to a channel. They are not handled and not logged.

---

## 12. Forwarding Attribution

**Issue:** Since we never use `forward()`, all mirrored messages appear as if sent directly by your account. There is no "Forwarded from: [source]" header. This is intentional per the requirements.

---

## 13. Media With No Downloadable File

**Issue:** Some media kinds have no attached file at all — a shared contact, a location/venue, a dice/game roll, or a plain webpage preview with no photo/document attached. `download_media()` correctly returns nothing for these; it isn't a failure.

**Behaviour:** If the message has a caption, it's mirrored as a plain text message (the media itself is dropped). If there's neither a file nor any text, there's nothing to mirror and the message is skipped — same as a service message, permanently, since retrying can never produce a file that was never there.

**Distinguishing from a real failure:** This is different from a photo/document that genuinely fails to download (network issue, expired file reference, ...) — that case *is* treated as a failure and retried indefinitely; see #14.

---

## 14. Exactly-Once / Never-Skip Retry Policy

**Behaviour:** A genuine failure anywhere in the pipeline — a flood wait, a transient Telegram/network error, a photo/document that fails to download — is never swallowed into a permanent "processed, but failed" record. It's retried with backoff until it succeeds: `HistoricalSync` lets the failure propagate out of `run()`, and main.py's outer loop keeps calling `run()` again (backoff capped at 300s, forever); `EventDispatcher`'s live consumer retries a queued item the same way before moving to the next one.

**Trade-off:** Because messages are mirrored strictly oldest → newest, a message that is *genuinely* undeliverable (not just rate-limited, but permanently broken — e.g. its file reference can never be resolved) will block every message behind it in the historical backlog until someone investigates and manually resolves it (e.g. via direct DB surgery to mark it done, or by fixing the underlying cause). Live sync keeps mirroring new messages independently in the meantime — it isn't blocked by a stuck historical backfill. Watch the logs (`Consumer error` / `Historical sync error`) for a message stuck retrying in a loop.

**Live delete/pin events across a shutdown:** these have no cursor-based resumption path of their own (unlike new messages and edits, which a future historical pass will always re-discover). If the daemon shuts down mid-retry for one of these specific event kinds, that individual event is lost — consistent with #3's existing best-effort guarantee for deletes. Pins get an extra safety net: `HistoricalSync` re-derives the destination's pinned state from the source's *current* pinned list on every startup, so a missed pin is picked up on the next run even if the live event itself was lost.

---

## 15. Self-Referential Link Rewriting

**Issue:** A source post can link to another post in the same channel (e.g. "see t.me/c/&lt;source&gt;/1234"). Mirrored verbatim, the link is wrong twice over: the message id means nothing in the destination's own numbering, and the source channel is usually private, so a destination-only reader can't open it at all.

**Behaviour:** `utils/links.py` rewrites any `t.me/c/<source_channel>/<id>` link (with or without an `https://` prefix) found in message text or a hidden hyperlink's URL to point at the mirrored destination message instead. Links to any other channel/chat are left untouched.

**Known edge cases:**
- If the linked message hasn't been mirrored yet, the link is left as-is (same graceful-degrade precedent as reply-chains, #8) — it is *not* retried or fixed up later once the target is mirrored.
- A forum-topic link suffix (`t.me/c/<id>/<msg>/<topic>`) is preserved verbatim as trailing text but the topic id itself isn't translated — this project doesn't mirror forum topics.
- If a formatting entity (bold, italic, spoiler, ...) overlaps the exact span of a rewritten plain-text URL, that entity is dropped rather than risk emitting a misaligned one. The new URL still renders as a clickable link regardless (Telegram clients auto-link plain `http(s)://` text independently of explicit entities) — only the extra formatting on that exact span is lost.