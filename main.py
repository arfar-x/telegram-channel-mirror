"""
Telegram Channel Mirror
=======================
Entry point. Wires together all components and manages the application lifecycle.

Startup sequence
----------------
1. Load config from environment.
2. Connect to Telegram (interactive login on first run, session reused after).
3. Connect to SQLite.
4. Run historical sync (OLDEST → NEWEST, resumes on restart).
5. Register live event handlers.
6. Start the event consumer queue.
7. Run until interrupted (SIGINT / SIGTERM).
8. Graceful shutdown: drain queue, disconnect, close DB.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telethon import TelegramClient

from db import Database
from handlers import EventDispatcher, HistoricalSync, MessageSender
from utils import load_config, setup_logging, MediaHandler
from utils.retry import ShutdownRequested, set_shutdown_event

logger = logging.getLogger(__name__)


async def main() -> None:
    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    cfg = load_config()
    setup_logging(cfg.log_level)

    logger.info("=== Telegram Channel Mirror starting ===")
    logger.info("Source:      %d", cfg.source_channel)
    logger.info("Destination: %d", cfg.destination_channel)
    logger.info("Delete sync: %s", cfg.enable_delete_sync)

    # ------------------------------------------------------------------
    # Telethon client
    # ------------------------------------------------------------------
    client = TelegramClient(
        cfg.session_name,
        cfg.api_id,
        cfg.api_hash,
        sequential_updates=True,   # Important: prevents out-of-order updates
        # Telethon's default (5 retries, then give up permanently) means a
        # network blip outlasting a few seconds leaves every future request
        # raising "Cannot send requests while disconnected" forever. Retry
        # the underlying connection indefinitely instead — our own retry
        # loops already handle backoff for the higher-level operations.
        connection_retries=None,
        retry_delay=5,
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    db = Database()
    await db.connect()

    # ------------------------------------------------------------------
    # Component wiring
    # ------------------------------------------------------------------
    media_handler = MediaHandler(
        client=client,
        temp_dir=cfg.temp_media_dir,
        max_concurrent=cfg.max_concurrent_downloads,
    )

    sender = MessageSender(
        client=client,
        db=db,
        media_handler=media_handler,
        dest_channel=cfg.destination_channel,
    )

    dispatcher = EventDispatcher(
        client=client,
        sender=sender,
        db=db,
        config=cfg,
    )

    historical = HistoricalSync(
        client=client,
        sender=sender,
        db=db,
        config=cfg,
    )

    # ------------------------------------------------------------------
    # Shutdown handler
    # ------------------------------------------------------------------
    shutdown_event = asyncio.Event()
    set_shutdown_event(shutdown_event)

    def _request_shutdown(*_) -> None:
        logger.info("Shutdown signal received.")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler
            pass

    # ------------------------------------------------------------------
    # Connect & run
    # ------------------------------------------------------------------
    async with client:
        me = await client.get_me()
        logger.info("Logged in as: %s (id=%d)", me.username or me.first_name, me.id)

        # Guard against the disaster case: a lost/recreated database has no
        # historical_min_id, which would make HistoricalSync.run() re-mirror
        # the entire source channel from scratch — duplicating everything
        # already sitting in the destination if it isn't actually empty.
        # A genuine first-time deployment has an empty destination, so that
        # case is left to run the normal full backfill below.
        if await db.get_progress("historical_min_id") is None:
            dest_preview = await client.get_messages(cfg.destination_channel, limit=1)
            if dest_preview:
                logger.error(
                    "historical_min_id is unset but the destination channel "
                    "already has messages — this looks like a recovery "
                    "scenario (database lost after messages were already "
                    "mirrored), not a fresh deployment. Refusing to run "
                    "historical sync, which would re-mirror and duplicate "
                    "everything. Run "
                    "`python scripts/recover_message_map.py --bootstrap-cursor` "
                    "first, then restart."
                )
                await db.close()
                sys.exit(1)

        # Register live handlers BEFORE historical sync so no events are missed
        # during the sync window. Events are buffered in the asyncio.Queue.
        dispatcher.register()
        await dispatcher.start_consumer()

        # Run historical sync — retry with backoff on transient errors instead
        # of abandoning the backfill; progress is persisted, so each retry
        # resumes from the last completed message rather than starting over.
        #
        # Shutdown is handled cooperatively, not by cancelling this task: a
        # message that's actively being sent is always allowed to finish (so
        # we never abort mid-upload), and ShutdownRequested only ever surfaces
        # from a retry *wait* (FloodWait/backoff) that hadn't sent anything
        # yet — so nothing here is lost, just picked up again next run.
        sync_delay = 1.0
        while not shutdown_event.is_set():
            try:
                await historical.run()
                break
            except ShutdownRequested:
                logger.info("Historical sync paused for shutdown; will resume next run.")
                break
            except Exception as exc:
                logger.error(
                    "Historical sync error: %s — retrying in %.0fs",
                    exc,
                    sync_delay,
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=sync_delay)
                except asyncio.TimeoutError:
                    pass
                sync_delay = min(sync_delay * 2, 300.0)

        if not shutdown_event.is_set():
            logger.info("Entering live sync mode. Press Ctrl+C to stop.")

        # Wait until shutdown is requested
        await shutdown_event.wait()

        # Graceful shutdown
        logger.info("Draining event queue…")
        await dispatcher.stop_consumer()

    await db.close()
    logger.info("=== Telegram Channel Mirror stopped ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
