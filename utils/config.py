"""
Environment-based configuration.
All values come from environment variables (or a .env file via python-dotenv).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"Required environment variable '{name}' is not set.")
    return val


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    source_channel: int
    destination_channel: int

    enable_delete_sync: bool = False
    max_concurrent_downloads: int = 3
    historical_send_delay: float = 0.5
    temp_media_dir: Path = field(default_factory=lambda: Path("temp_media"))
    log_level: str = "INFO"


def load_config() -> Config:
    temp_dir = Path(os.environ.get("TEMP_MEDIA_DIR", "temp_media"))
    temp_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        session_name=os.environ.get("SESSION_NAME", "mirror_bot"),
        source_channel=int(_require("SOURCE_CHANNEL")),
        destination_channel=int(_require("DESTINATION_CHANNEL")),
        enable_delete_sync=_bool_env("ENABLE_DELETE_SYNC", False),
        max_concurrent_downloads=_int_env("MAX_CONCURRENT_DOWNLOADS", 3),
        historical_send_delay=_float_env("HISTORICAL_SEND_DELAY", 0.5),
        temp_media_dir=temp_dir,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )