# Telegram Channel Mirror

A production-grade Telegram channel mirroring script using **Telethon** (Python 3.12+).

Mirrors everything from a source channel into a destination channel **without using `forward()`** — all content is re-uploaded manually to bypass forwarding restrictions.

---

## Features

| Feature | Status |
|---|---|
| Text messages with full formatting (bold, italic, code, links, spoilers, custom emoji) | ✅ |
| Photos, Videos, Documents, Audio, Voice notes, GIFs | ✅ |
| Albums / grouped media (correct order, correct caption) | ✅ |
| Stickers | ✅ |
| Polls (regular, anonymous, multiple-choice, quiz*) | ✅ |
| Message edits (text + caption) | ✅ |
| Message deletes (soft + optional hard delete) | ✅ |
| Pinned messages | ✅ |
| Reply chains | ✅ |
| Historical sync (oldest → newest, resumable) | ✅ |
| Duplicate detection | ✅ |
| FloodWait + retry | ✅ |
| PostgreSQL state persistence | ✅ |
| Graceful shutdown | ✅ |
| Channel title / photo changes | ⚠️ Logged only (see LIMITATIONS.md) |
| Quiz correct answers | ⚠️ Not mirrored (API restriction) |

---

## Project Structure

```
tg_mirror/
├── main.py                  # Entry point
├── requirements.txt
├── .env.example
├── README.md
├── LIMITATIONS.md
├── db/
│   ├── __init__.py
│   └── database.py          # Async PostgreSQL wrapper (asyncpg)
├── handlers/
│   ├── __init__.py
│   ├── sender.py            # MessageSender — re-creates all content types
│   ├── events.py            # EventDispatcher — live event handlers + queue
│   └── historical.py        # HistoricalSync — bulk backfill
├── utils/
│   ├── __init__.py
│   ├── config.py            # Environment config loader
│   ├── logging_setup.py     # Structured logging
│   ├── media.py             # Media download/upload helpers
│   └── retry.py             # FloodWait-aware retry decorator
└── temp_media/              # Transient download directory (auto-created)
```

---

## Setup

### 1. Prerequisites

- Python **3.12+**
- A Telegram account (user account, not a bot token)
- Admin rights or at least **Send Messages** permission in the **destination** channel

### 2. Get API credentials

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. Log in with your phone number
3. Create an app (any name/platform)
4. Copy `api_id` and `api_hash`

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Note:** `cryptg` is optional but strongly recommended — it uses a C extension for fast AES encryption, dramatically speeding up large media uploads/downloads.

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
API_ID=123456
API_HASH=your_api_hash_here
SESSION_NAME=mirror_bot

# Use numeric channel IDs (include -100 prefix for supergroups/channels)
SOURCE_CHANNEL=-1001234567890
DESTINATION_CHANNEL=-1009876543210

# PostgreSQL connection string
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mirror

# Optional
ENABLE_DELETE_SYNC=false
LOG_LEVEL=INFO
```

You need a running PostgreSQL instance. The included `docker-compose.yml` starts one for you, or point `DATABASE_URL` at any existing server. The app creates its tables automatically on first connect.

#### Migrating from a previous SQLite install

If you're upgrading from a version that used SQLite (`mirror.db`), copy your existing data into PostgreSQL before starting the new version:

```bash
python scripts/migrate_sqlite_to_postgres.py --sqlite-path mirror.db
```

This reads every row out of the SQLite file and upserts it into the PostgreSQL database pointed to by `DATABASE_URL`, then verifies row counts match. The SQLite file itself is never modified or deleted — keep it around until you've confirmed the app runs correctly against PostgreSQL.

#### Finding numeric channel IDs

Option A — Forward a message from the channel to [@userinfobot](https://t.me/userinfobot).

Option B — Use the Telegram web client: open the channel, the URL is `https://web.telegram.org/k/#-1001234567890` (that number IS the channel ID including the `-100` prefix).

Option C — Run this one-liner after logging in:
```python
from telethon.sync import TelegramClient
with TelegramClient('tmp', API_ID, API_HASH) as c:
    for d in c.get_dialogs():
        print(d.id, d.name)
```

### 5. First run (interactive login)

```bash
python main.py
```

Telethon will prompt for your phone number and an OTP code (and 2FA password if enabled). The session is saved to `<SESSION_NAME>.session` and reused on subsequent runs.

### 6. Running as a service (systemd)

This repo ships a unit file at [systemd/telegram-channel-mirror.service](systemd/telegram-channel-mirror.service). It expects the *interactive* first run (step 5 above, which creates `<SESSION_NAME>.session`) to already have happened — `systemd` has no terminal to answer the OTP/2FA prompts, so do that login before enabling the service. It also assumes `DATABASE_URL` points at a PostgreSQL instance that's already reachable (run `docker compose up -d postgres`, or your own Postgres) — `systemd` here only manages the Python daemon, not Postgres.

Edit `User=`, `WorkingDirectory=`, and the `.venv` path in the unit to match your deployment, then install it:

```bash
sudo cp systemd/telegram-channel-mirror.service /etc/systemd/system/telegram-channel-mirror.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-channel-mirror
sudo journalctl -u telegram-channel-mirror -f
```

### 7. Running with Docker

`docker compose up -d` runs the `mirror` service detached, with no terminal attached — so it can't answer the interactive phone/OTP/2FA prompts from step 5. The `mirror` service mounts `./session_data` to `/app/session_data` and sets `SESSION_NAME=session_data/mirror_bot`, so the session file persists on the host across container restarts/rebuilds (without this volume, a session created inside the container would be lost the next time the container is recreated).

Do the first login once, with a real TTY attached, before bringing the stack up normally:

```bash
docker compose run --rm -it mirror python main.py
```

Answer the phone/OTP/2FA prompts. Once it logs in and moves on to historical sync (you'll see sync progress in the logs), `Ctrl+C` — the session is already saved to `./session_data/mirror_bot.session` on the host at that point. Then start everything normally:

```bash
docker compose up -d
```

Subsequent restarts/rebuilds reuse the saved session and never prompt.

If you already have a session file from a non-Docker run (step 5), reuse it instead of logging in again:

```bash
mkdir -p session_data
mv mirror_bot.session* session_data/
```

---

## Architecture

```
main.py
  │
  ├─ TelegramClient (Telethon, sequential_updates=True)
  │
  ├─ Database (PostgreSQL via asyncpg)
  │    ├─ message_map       source_id → dest_id + metadata
  │    ├─ sync_progress     historical sync cursor
  │    └─ pending_edits     edits that arrived before original was processed
  │
  ├─ MediaHandler           semaphore-limited downloads, temp file cleanup
  │
  ├─ MessageSender          re-creates all content types without forward()
  │
  ├─ HistoricalSync         iterates messages oldest→newest, album detection
  │    └─ runs once at startup, then exits
  │
  └─ EventDispatcher        registers Telethon handlers, asyncio.Queue consumer
       ├─ NewMessage
       ├─ Album
       ├─ MessageEdited
       ├─ MessageDeleted
       └─ Raw (pins, title/photo changes)
```

### Queue-based live sync

All live events are pushed onto an `asyncio.Queue`. A single consumer coroutine processes them in FIFO order. This:
- Guarantees message ordering during traffic bursts
- Prevents race conditions between concurrent handler invocations
- Makes it trivial to add backpressure / priority later

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_ID` | ✅ | — | Telegram API ID |
| `API_HASH` | ✅ | — | Telegram API hash |
| `SESSION_NAME` | ❌ | `mirror_bot` | Session file name (overridden to `session_data/mirror_bot` in `docker-compose.yml` so the session persists via the mounted volume) |
| `SOURCE_CHANNEL` | ✅ | — | Numeric source channel ID |
| `DESTINATION_CHANNEL` | ✅ | — | Numeric destination channel ID |
| `ENABLE_DELETE_SYNC` | ❌ | `false` | Hard-delete mirrored messages when source deletes |
| `MAX_CONCURRENT_DOWNLOADS` | ❌ | `3` | Parallel media downloads |
| `HISTORICAL_SEND_DELAY` | ❌ | `0.5` | Seconds between sends during historical sync |
| `TEMP_MEDIA_DIR` | ❌ | `temp_media` | Temp directory for downloaded media |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Automated Postgres backups (Google Drive)

`docker compose up -d` also starts a `backup` service (built from [docker/backup.Dockerfile](docker/backup.Dockerfile)) that runs [scripts/backup_postgres.sh](scripts/backup_postgres.sh) on a daily cron schedule inside the container. Each run:

1. `pg_dump`s the database (custom format, gzip-compressed) into a named volume (`mirror_backups`)
2. Uploads the dump to a Google Drive folder via [rclone](https://rclone.org/) — a single CLI that speaks the Google Drive API (and S3, Dropbox, etc.) so we don't have to handle OAuth/API calls ourselves
3. Deletes local and remote dump files older than `BACKUP_RETENTION_DAYS` (default 7)

### One-time setup

1. Install rclone on your host (or anywhere with a browser) and authorize Google Drive:
   ```bash
   rclone config
   # n) New remote -> name it "gdrive" -> type "drive" -> follow the OAuth browser flow
   ```
   This writes `~/.config/rclone/rclone.conf`. Copy that file into the repo root as `rclone.conf` — it's mounted read-only into the `backup` container at `/root/.config/rclone/rclone.conf` (gitignored; treat it like a credential).
2. In `.env`, set `RCLONE_REMOTE=gdrive:<folder-name>` (the folder is created automatically on first upload), and optionally `BACKUP_RETENTION_DAYS` / `BACKUP_CRON_SCHEDULE` (5-field cron expression, default `0 3 * * *`).
3. `docker compose up -d --build backup`

### Manual run / restore

```bash
# Trigger an out-of-schedule backup
docker compose exec backup /usr/local/bin/backup_postgres.sh

# Restore a dump (downloaded via `rclone copy` or already in the mirror_backups volume)
gunzip -c mirror_20260624_030000.dump.gz | pg_restore --dbname="$DATABASE_URL" --clean
```

---

## Limitations

See [LIMITATIONS.md](LIMITATIONS.md) for a full list of Telegram API limitations and their handling.
