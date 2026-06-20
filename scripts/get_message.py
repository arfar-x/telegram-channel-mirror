"""
Fetch a single message by id from the configured source channel.

Usage:
    uv run scripts/get_message.py <message_id>
"""

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from telethon.sync import TelegramClient

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "mirror_bot")
SOURCE_CHANNEL = int(os.getenv("SOURCE_CHANNEL"))

# Use a separate session file cloned from the bot's session. The main mirror
# process holds the original session's sqlite file open, so querying through
# it directly while the bot runs raises "database is locked".
QUERY_SESSION = f"{SESSION_NAME}_query"


def _ensure_query_session() -> None:
    src = Path(f"{SESSION_NAME}.session")
    dst = Path(f"{QUERY_SESSION}.session")
    if not dst.exists() and src.exists():
        shutil.copyfile(src, dst)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <message_id>")
        sys.exit(1)

    message_id = int(sys.argv[1])
    _ensure_query_session()

    with TelegramClient(QUERY_SESSION, API_ID, API_HASH) as client:
        message = client.get_messages(SOURCE_CHANNEL, ids=message_id)

    if message is None:
        print(f"No message with id={message_id} found in source channel {SOURCE_CHANNEL}.")
        sys.exit(1)

    print(message.stringify())


if __name__ == "__main__":
    main()
