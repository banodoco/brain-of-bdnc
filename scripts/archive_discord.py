import os
import sys
import argparse
# Add project root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import asyncio
from datetime import datetime, timezone
import logging
from src.common.db_handler import DatabaseHandler
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('archive_discord.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Thread-local storage for database connections
thread_local = threading.local()

DISCORD_EPOCH_MS = 1420070400000  # Discord epoch in milliseconds

def snowflake_to_datetime(snowflake_id: int) -> datetime:
    """Convert a Discord snowflake ID to a timezone-aware UTC datetime."""
    timestamp_ms = (snowflake_id >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

def to_aware_utc(dt_str: str) -> datetime:
    """Convert an ISO format string to a timezone-aware datetime object."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_db():
    """Get thread-local database connection."""
    if not hasattr(thread_local, "db"):
        thread_local.db = DatabaseHandler()
    return thread_local.db


# ---------------------------------------------------------------------------
# All archive logic now lives in src.features.archiving.archive_task.ArchiveTask.
# The old MessageArchiver / DiscordArchiveBot class has been removed (LD-5).
# This CLI now uses a short-lived discord.Client with a once-guarded on_ready
# handler that delegates to ArchiveTask.
# ---------------------------------------------------------------------------


def main():
    """Main entry point for the standalone archive CLI.

    Uses a short-lived ``discord.Client`` (NOT the main bot's client) with an
    ``on_ready`` handler protected by a once-guard (``asyncio.Event``) to
    prevent double execution on reconnect.  The handler builds an
    ``ArchiveTask``, runs it, and then closes the client.
    """
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Archive Discord messages')
    parser.add_argument('--dev', action='store_true', help='Run in development mode')
    parser.add_argument('--order', choices=['newest', 'oldest'], default='newest',
                      help='Order to process messages (default: newest)')
    parser.add_argument('--days', type=int, help='Number of days of history to fetch (default: all, conflicts with --start-date/--end-date)')
    parser.add_argument('--start-date', type=str, help='Start date for fetching messages (YYYY-MM-DD, requires --end-date, conflicts with --days)')
    parser.add_argument('--end-date', type=str, help='End date for fetching messages (YYYY-MM-DD, requires --start-date, conflicts with --days)')
    parser.add_argument('--batch-size', type=int, default=100,
                      help='Number of messages to process in each batch (default: 100)')
    parser.add_argument('--in-depth', action='store_true',
                      help='Perform thorough message checks, re-processing all messages in the time range')
    parser.add_argument('--channel', type=int,
                      help='ID of a specific channel to archive')
    parser.add_argument('--channels', type=str,
                      help='Comma-separated list of channel IDs to archive (single login)')
    parser.add_argument('--fetch-reactions', action='store_true',
                      help='Fetch reactions for all messages in range, not just new ones')
    parser.add_argument('--fast-fill', action='store_true',
                      help='Fast mode for filling gaps: batches DB checks, skips member updates and reactions')
    parser.add_argument('--guild-id', type=int,
                      help='Override guild ID (for multi-server archiving)')
    args = parser.parse_args()

    # Validate arguments
    if args.days and (args.start_date or args.end_date):
        parser.error("argument --days: not allowed with argument --start-date or --end-date")
    if (args.start_date and not args.end_date) or (not args.start_date and args.end_date):
        parser.error("--start-date and --end-date must be used together")
    # Basic format validation (more robust parsing happens in __init__)
    if args.start_date:
        try:
            datetime.strptime(args.start_date, '%Y-%m-%d')
        except ValueError:
            parser.error("Invalid format for --start-date. Use YYYY-MM-DD.")
    if args.end_date:
        try:
            datetime.strptime(args.end_date, '%Y-%m-%d')
        except ValueError:
             parser.error("Invalid format for --end-date. Use YYYY-MM-DD.")

    if args.dev:
        logger.info("Running in development mode")

    # Parse --channels (comma-separated) into a list of ints
    channel_ids_list = None
    if args.channels:
        channel_ids_list = [int(c.strip()) for c in args.channels.split(',') if c.strip()]

    # Token selection
    token_env = 'DEV_DISCORD_BOT_TOKEN' if args.dev else 'DISCORD_BOT_TOKEN'
    token = os.getenv(token_env)
    if not token:
        logger.error("No token found for %s", token_env)
        sys.exit(1)

    # Once-guard to prevent double execution on reconnect
    _archive_done = asyncio.Event()

    # Short-lived client (NOT the main bot's client)
    intents = discord.Intents.all()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        # Once-guard: only execute once even if on_ready fires again on reconnect
        if _archive_done.is_set():
            return

        logger.info("CLI archive client ready as %s", client.user)
        try:
            from src.features.archiving.archive_task import ArchiveTask

            task = ArchiveTask(
                client,
                dev_mode=args.dev,
                order=args.order,
                days=args.days,
                batch_size=args.batch_size,
                in_depth=args.in_depth,
                channel_id=args.channel,
                channel_ids=channel_ids_list,
                fetch_reactions=args.fetch_reactions,
                start_date_str=args.start_date,
                end_date_str=args.end_date,
                fast_fill=args.fast_fill,
                guild_id=args.guild_id,
                logger=logger,
            )
            result = await task.run()
            logger.info(
                "CLI archive complete: ok=%s msgs=%d dur=%.1fs errors=%d",
                result.success,
                result.messages_archived,
                result.duration_seconds,
                len(result.per_channel_errors),
            )
        except Exception as exc:
            logger.error("CLI archive failed: %s", exc, exc_info=True)
        finally:
            _archive_done.set()
            await client.close()

    client.run(token)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot shutdown initiated by user")
    except Exception as e:
        logger.error(f"Unexpected error in __main__: {e}")
        import traceback
        traceback.print_exc()
