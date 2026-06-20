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