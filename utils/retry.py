"""
Retry helpers for Telegram FloodWait and transient errors.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Callable, TypeVar

from telethon.errors import (
    FloodWaitError,
    ServerError,
    TimedOutError,
    BadRequestError,
)

logger = logging.getLogger(__name__)

F = TypeVar("F")

# Errors that are safe to retry
_RETRYABLE = (FloodWaitError, ServerError, TimedOutError)

# Set once from main() so retry waits can be interrupted on shutdown without
# being mistaken for a real send failure (see ShutdownRequested below).
_shutdown_event: asyncio.Event | None = None


def set_shutdown_event(event: asyncio.Event) -> None:
    global _shutdown_event
    _shutdown_event = event


def is_shutdown_requested() -> bool:
    return _shutdown_event is not None and _shutdown_event.is_set()


class ShutdownRequested(BaseException):
    """
    Raised instead of a normal exception when a retry wait is interrupted by
    a shutdown request. Deliberately a BaseException (like CancelledError) so
    it is never caught by the `except Exception` handlers in MessageSender —
    those would otherwise permanently mark the message as processed with
    dest_id=None, losing it for good instead of letting it be retried on the
    next run.
    """


async def _sleep_or_abort(seconds: float) -> None:
    """Sleep, but wake up early and raise ShutdownRequested if shutdown fires."""
    if _shutdown_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(_shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise ShutdownRequested()


def with_retry(
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> Callable:
    """
    Decorator that retries an async function on transient Telegram errors.
    Handles FloodWaitError by sleeping the exact duration Telegram demands.
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            attempt = 0
            delay = base_delay
            while True:
                if is_shutdown_requested():
                    raise ShutdownRequested()
                try:
                    return await fn(*args, **kwargs)
                except FloodWaitError as exc:
                    wait = exc.seconds + 1
                    logger.warning(
                        "FloodWait: sleeping %ds before retry (attempt %d/%d)",
                        wait,
                        attempt + 1,
                        max_attempts,
                    )
                    await _sleep_or_abort(wait)
                except (ServerError, TimedOutError) as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error("Max retries reached: %s", exc)
                        raise
                    logger.warning(
                        "Transient error (%s), retry %d/%d in %.1fs",
                        exc,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    await _sleep_or_abort(delay)
                    delay = min(delay * 2, max_delay)

        return wrapper

    return decorator
