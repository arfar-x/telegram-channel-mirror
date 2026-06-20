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
| SQLite state persistence | ✅ |
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
│   └── database.py          # Async SQLite wrapper
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

# Optional
ENABLE_DELETE_SYNC=false
LOG_LEVEL=INFO
```

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

```ini
# /etc/systemd/system/tg-mirror.service
[Unit]
Description=Telegram Channel Mirror
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/tg_mirror
EnvironmentFile=/opt/tg_mirror/.env
ExecStart=/opt/tg_mirror/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-mirror
sudo journalctl -u tg-mirror -f
```

---

## Architecture

```
main.py
  │
  ├─ TelegramClient (Telethon, sequential_updates=True)
  │
  ├─ Database (SQLite via aiosqlite)
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
| `SESSION_NAME` | ❌ | `mirror_bot` | Session file name |
| `SOURCE_CHANNEL` | ✅ | — | Numeric source channel ID |
| `DESTINATION_CHANNEL` | ✅ | — | Numeric destination channel ID |
| `ENABLE_DELETE_SYNC` | ❌ | `false` | Hard-delete mirrored messages when source deletes |
| `MAX_CONCURRENT_DOWNLOADS` | ❌ | `3` | Parallel media downloads |
| `HISTORICAL_SEND_DELAY` | ❌ | `0.5` | Seconds between sends during historical sync |
| `TEMP_MEDIA_DIR` | ❌ | `temp_media` | Temp directory for downloaded media |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Limitations

See [LIMITATIONS.md](LIMITATIONS.md) for a full list of Telegram API limitations and their handling.
