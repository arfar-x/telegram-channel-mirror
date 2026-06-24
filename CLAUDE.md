# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telethon-based (logged-in user account, not a bot token) daemon that mirrors every message from one Telegram source channel into a destination channel, **without using `forward()`** — every message is downloaded and re-uploaded from scratch to bypass forwarding restrictions on the source. It runs as a long-lived process (`main.py`), backed by PostgreSQL for state, intended to run forever under `docker compose` / `systemd` (`restart: always` / `Restart=on-failure`).

Read [README.md](README.md) for setup steps and the full environment variable reference, and [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for Telegram API constraints that shape several design decisions below (quiz polls, media-swap edits, delete event reliability, channel title/photo changes, etc.) — check there before treating platform-imposed behavior as a bug.

## File map

```text
main.py                          entry point — wires everything together, see "Startup sequence" below
db/database.py                   Database — async Postgres wrapper (asyncpg), schema + all queries
handlers/sender.py                MessageSender — re-creates every content type in the destination
handlers/historical.py            HistoricalSync — one-shot resumable backfill, oldest -> newest
handlers/events.py                EventDispatcher — live Telethon event handlers + asyncio.Queue consumer
utils/config.py                   Config dataclass + load_config() — all settings come from env/.env
utils/media.py                    MediaHandler — download/upload helpers, content-type classification
utils/retry.py                    with_retry decorator (FloodWait/backoff) + cooperative shutdown plumbing
utils/logging_setup.py            one-line logging.basicConfig wrapper
scripts/get_message.py            fetch one source message by id (debugging)
scripts/get_profile_id.py         list numeric ids of all your dialogs/channels
scripts/migrate_sqlite_to_postgres.py   one-time legacy SQLite -> Postgres migration
scripts/recover_message_map.py    disaster recovery — rebuild message_map after DB loss, see below
scripts/backup_postgres.sh        pg_dump -> gzip -> rclone upload -> prune by BACKUP_RETENTION_DAYS
docker/backup.Dockerfile          image for the `backup` compose service (postgres-client + rclone + crond)
docker/backup-entrypoint.sh       writes container env to /etc/backup.env, renders crontab from BACKUP_CRON_SCHEDULE, execs crond
docs/LIMITATIONS.md               Telegram API limitations and how each is handled
```

## Commands

There is no test suite, linter, or build step in this repo — verification is manual (run it against real test channels).

```bash
# Install deps — dependencies live in requirements.txt, NOT pyproject.toml
# (pyproject.toml has an empty `dependencies = []`). Running `uv sync` will
# actually UNINSTALL everything since it syncs against that empty list —
# use `uv pip install` instead:
uv pip install -r requirements.txt

# Run the daemon (first run prompts interactively for phone/OTP/2FA login,
# then reuses the saved <SESSION_NAME>.session file on every subsequent run)
uv run python main.py

# Docker (docker-compose.yml also starts a postgres:16-alpine service)
# `mirror` runs detached with no TTY, so it can't answer login prompts — do the
# first login interactively once (writes the session into the ./session_data
# bind mount, per SESSION_NAME=session_data/mirror_bot set in the compose file)
# before bringing the stack up normally:
docker compose run --rm -it mirror python main.py   # Ctrl+C once past login
docker compose up -d

# One-off scripts (all under scripts/, run with `uv run python scripts/<name>.py`)
scripts/get_message.py <message_id>           # fetch one source message by id
scripts/get_profile_id.py                      # list numeric ids of all dialogs/channels
scripts/migrate_sqlite_to_postgres.py          # one-time legacy SQLite -> Postgres migration
scripts/recover_message_map.py [...]           # see "Disaster recovery" below
```

Any script that needs a Telethon session while `main.py` may also be running clones the session file to `<SESSION_NAME>_query.session` first (see `get_message.py`, `recover_message_map.py`) — opening the same `.session` sqlite file that a running process already holds open raises "database is locked".

## Configuration (`utils/config.py`)

Everything comes from environment variables (`.env` loaded via `python-dotenv`). `load_config()` raises `EnvironmentError` immediately if a required var is missing — there is no partial/default config.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `API_ID` / `API_HASH` | yes | — | from [my.telegram.org/apps](https://my.telegram.org/apps) |
| `SESSION_NAME` | no | `mirror_bot` | Telethon session file prefix; overridden to `session_data/mirror_bot` in `docker-compose.yml` so the session persists via the `./session_data` bind mount across container restarts/rebuilds |
| `SOURCE_CHANNEL` / `DESTINATION_CHANNEL` | yes | — | numeric ids, include the `-100` prefix |
| `DATABASE_URL` | no | `postgresql://postgres:postgres@localhost:5432/mirror` | read directly in `db/database.py`, not part of `Config` |
| `ENABLE_DELETE_SYNC` | no | `false` | see "Soft vs hard delete" below |
| `MAX_CONCURRENT_DOWNLOADS` | no | `3` | semaphore in `MediaHandler` |
| `HISTORICAL_SEND_DELAY` | no | `0.5` | seconds slept between sends during backfill |
| `TEMP_MEDIA_DIR` | no | `temp_media` | created on startup if missing |
| `LOG_LEVEL` | no | `INFO` | |

Finding numeric channel ids: forward a message to `@userinfobot`, read it off the web client URL, or run `scripts/get_profile_id.py`.

## Architecture

### Startup sequence (`main.py`)

Built by hand, no DI framework: `TelegramClient` → `Database.connect()` → `MediaHandler` → `MessageSender` → `EventDispatcher` + `HistoricalSync`, then inside `async with client:`:

1. Login (`client.get_me()`).
2. **Recovery guard** — if `sync_progress.historical_min_id` is unset *and* the destination channel already has at least one message, this is a lost-database scenario, not a fresh deployment: log an error pointing at `scripts/recover_message_map.py --bootstrap-cursor` and `sys.exit(1)` rather than silently re-mirroring (and duplicating) the whole channel. See "Disaster recovery" below.
3. `dispatcher.register()` + `await dispatcher.start_consumer()` — live handlers attached **before** backfill starts, so any message posted during the backfill window queues up instead of being missed.
4. `historical.run()` in a retry loop with exponential backoff (1s → 300s cap) on unexpected exceptions; `ShutdownRequested` breaks the loop instead of retrying.
5. Block on `shutdown_event.wait()`; on shutdown, drain the event queue (`dispatcher.stop_consumer()`), then `db.close()`.

Shutdown is cooperative via an `asyncio.Event` set from SIGINT/SIGTERM handlers (`utils/retry.py: set_shutdown_event`/`is_shutdown_requested`), never task cancellation — an in-flight send is always allowed to finish.

### Database (`db/database.py`), Postgres via asyncpg

Schema is plain SQL with `CREATE TABLE IF NOT EXISTS`, executed on every `connect()` — no migration framework. Three tables:

- **`message_map`** — `source_id BIGINT PRIMARY KEY`, `dest_id`, `grouped_id` (album group, nullable), `media_type` (`'photo'|'video'|'document'|'gif'|'voice'|'audio'|'sticker'|'poll'|'text'|'unknown'`), `is_deleted INTEGER` (soft-delete flag), `created_at DOUBLE PRECISION`. This is the single source of truth correlating source and destination messages — `get_dest_id()`, `is_processed()`, `get_mapping()`, `get_group_dest_ids()` are all lookups against it (`db/database.py:98-126`). `upsert_mapping()` (`db/database.py:77`) is idempotent via `ON CONFLICT (source_id) DO UPDATE`, which several other parts of the system rely on (safe to re-run, safe to retry).
- **`sync_progress`** — plain key/value. `historical_min_id` is the live backfill cursor `HistoricalSync` reads/advances. `recovery_boundary_id` is written exactly once by the recovery script and never touched by normal operation (see below).
- **`pending_edits`** — `source_id` → `queued_at`. An edit arriving for a `source_id` not yet in `message_map` (race during backfill) is queued here (`MessageSender.edit_message`, `handlers/sender.py:131`) rather than dropped; `remove_pending_edit` exists but nothing in the current code path calls it to replay queued edits — if you need that behavior, it isn't implemented yet.

### Message dispatch (`handlers/sender.py: MessageSender`)

`_dispatch()` (`handlers/sender.py:185`) is the canonical "what kind of thing is this message" routing table, checked in this order: `MessageMediaPoll` → sticker (`message.sticker`) → any other media → text → unhandled (service message — silently skipped, **no `message_map` row is ever created for it**, which the recovery script's pairing logic depends on). **This classification is duplicated in three places** — `_dispatch` itself, `MediaHandler.media_type()` (`utils/media.py:79`) for the type label, and `classify()` in `scripts/recover_message_map.py:78`. If you add a new content type, update all three or recovery pairing will silently miscount it as a service message.

Each `_send_*` method (`_send_text:210`, `_send_media:234`, `_send_sticker:293`, `_send_album_group:320`, `_send_poll:378`) downloads media if needed, sends via the appropriate Telethon call, and ends with `db.upsert_mapping()`. On failure, `send_message()` (`:80`) stores `dest_id=None` so the message isn't retried forever on every subsequent backfill pass. `_resolve_reply()` (`:447`) maps a source `reply_to` through `message_map`, degrading to no-reply-to (never failing the send) if the target was never mirrored — see LIMITATIONS.md #8.

Edits (`edit_message:131`) can only update caption/text, never swap media — a Telegram API restriction, not a bug (LIMITATIONS.md #2). Deletes (`delete_message:145` vs `hard_delete_message:153`) — see "Soft vs hard delete" below. Pins (`pin_message:165`) and album sends (`send_album:113`, falls back to per-message `send_message` on group-send failure) round out the public API that `EventDispatcher` and `HistoricalSync` call into.

### Historical sync (`handlers/historical.py: HistoricalSync.run()`, line 61)

Runs once at startup, resumable via `sync_progress.historical_min_id`. Iterates `client.iter_messages(reverse=True, min_id=cursor)` oldest → newest. Telethon's `iter_messages` does not emit `Album` events the way live updates do — it yields individual messages — so albums are detected by buffering consecutive messages sharing `grouped_id` and flushing the buffer when the group changes (look-ahead-by-one, `_flush_album:138`). Shutdown (`is_shutdown_requested()`) is checked **before** touching the next message/album, never mid-send, and the cursor only advances after a message/album is actually sent (`_advance_cursor:156`) — so a crash never loses or duplicates a message, it just resumes at the same point.

### Live sync (`handlers/events.py: EventDispatcher`)

`register()` (`:74`) attaches Telethon handlers scoped to `source_channel`: `NewMessage` (skips grouped messages — those are handled by `Album`), `Album` (Telethon's own coalesced grouped-media event), `MessageEdited`, `MessageDeleted`, and a `Raw` handler (`_handle_raw:188`) that intercepts `MessageActionPinMessage`/`ChatEditTitle`/`ChatEditPhoto` service actions, since Telegram doesn't expose those as high-level events. Every handler just pushes `(kind, payload)` onto a single `asyncio.Queue`; one consumer coroutine (`_consume:141`) drains it FIFO and routes via `_handle()` (`:167`). This single-consumer design is deliberate: it guarantees ordering across bursts and avoids two handlers racing on the same `message_map` row. `MessageDeleted` is best-effort per Telegram's own guarantees (LIMITATIONS.md #3) — not a reliability gap in this code.

`stop_consumer()` (`:117`) drains everything already queued before returning (so nothing queued is lost on shutdown), with a 30s timeout as a last-resort safety net only — hitting it can abort a mid-send item, so it's not the normal path.

### Retry / shutdown interaction (`utils/retry.py`)

`with_retry()` (`:63`) decorates the `_send_*` methods to handle `FloodWaitError` (sleeps the *exact* demanded duration, `wait = exc.seconds + 1`) and `ServerError`/`TimedOutError` (exponential backoff, capped, max 5 attempts by default). `ShutdownRequested` (`:40`) is deliberately a `BaseException`, not `Exception` — it must propagate through `MessageSender`'s `except Exception` failure handlers (which would otherwise mark the message permanently failed with `dest_id=None`) so the item is retried on the next run instead of lost. `_sleep_or_abort()` (`:51`) is what turns a shutdown signal into this exception mid-wait.

### Soft vs hard delete

`ENABLE_DELETE_SYNC=false` (default): `delete_message()` (`handlers/sender.py:145`) only sets `is_deleted=1` in `message_map` — the row and the destination copy both stay. `ENABLE_DELETE_SYNC=true`: `hard_delete_message()` (`:153`) additionally deletes the destination message. This default-soft behavior is *why* the recovery script's pairing has to handle orphans at all — a soft-deleted source message simply stops appearing when the source channel is re-iterated, but its destination copy is still sitting there with nothing to pair against.

### Disaster recovery (`scripts/recover_message_map.py`)

**Problem:** if `message_map`/the database is lost while the destination channel still holds everything previously mirrored, starting `main.py` naively would treat `historical_min_id=0` as "nothing has ever been mirrored" and re-send the entire source channel, duplicating everything already in the destination. `main.py`'s recovery guard (see step 2 of the startup sequence above) blocks this path automatically.

**Fix — a two-step recovery built around one invariant: it must be safe to run concurrently with `main.py`, with no locking.** This is achieved by partitioning source message ids around a fixed snapshot boundary stored in `sync_progress.recovery_boundary_id` (distinct from the live `historical_min_id` cursor, which keeps advancing):

1. **`scripts/recover_message_map.py --bootstrap-cursor`** (`bootstrap_cursor:246`) — run once, before starting/restarting `main.py`. Fetches the current latest source message id and writes it to *both* `historical_min_id` (unblocks `main.py` — it will only ever mirror/write ids above this) and `recovery_boundary_id` (frozen from here on; refuses to run a second time to prevent silently moving the partition).
2. **Default mode**, dry-run unless `--apply` (`run_pairing:267`, `pair:162`) — walks the source channel from the start up to `recovery_boundary_id` and the destination channel from the start, grouping each into albums the same way `HistoricalSync` does (`group_into_units:119`), and pairs them positionally:
   - A source unit that classifies to `None` (service message) is skipped — no destination unit consumed, matching `_dispatch`'s behavior of never producing one.
   - Otherwise it searches a sliding lookahead window on the destination side (`deque`, `--lookahead`, default 10) for the first unit matching shape + per-item `classify()`. A match deeper in the window means the skipped destination units are presumed-deleted-source orphans (see "Soft vs hard delete") — logged and discarded, not fatal.
   - No match anywhere in the window is treated as genuine ambiguity: pairing aborts and prints the full window for manual inspection. Nothing already written is rolled back (`upsert_mapping` is idempotent) and nothing past the abort point is touched, so a fix + rerun is always safe.

Because `main.py` only ever mirrors/writes ids strictly above `recovery_boundary_id` and the recovery script only ever reads/writes ids at or below it, the two ranges never overlap — there is nothing to coordinate at runtime beyond not opening the same `.session` file twice (handled by `_query_session_name:308` cloning it).

## Known inconsistencies worth knowing about (not yet fixed)

- `pyproject.toml` declares `dependencies = []`; real dependencies live in `requirements.txt`. Running `uv sync` will *uninstall* everything — use `uv pip install -r requirements.txt`.
- Python version is inconsistent across files: `pyproject.toml` requires `>=3.11`, `README.md` says "3.12+", and `Dockerfile` uses `python:3.11-slim`.
