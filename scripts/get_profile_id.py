import os
from dotenv import load_dotenv
load_dotenv()
from telethon.sync import TelegramClient

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

with TelegramClient("tmp", API_ID, API_HASH) as client:
    for dialog in client.get_dialogs():
        if dialog.is_channel:
            print(f"{dialog.id}  →  {dialog.name}")