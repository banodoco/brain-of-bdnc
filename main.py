import os
import sys
import argparse
import logging
import asyncio
import time
import subprocess
from datetime import datetime
import traceback

from dotenv import load_dotenv
from discord.ext import tasks

# Load environment variables BEFORE importing modules that might need them
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, '.env')
load_dotenv(dotenv_path=env_path, override=True)

from discord.ext import commands
import discord

from src.common.log_handler import LogHandler, setup_supabase_logging
from src.common.base_bot import BaseDiscordBot
from src.common.db_handler import DatabaseHandler
from src.common.openmuse_interactor import OpenMuseInteractor
from src.common.llm.claude_client import ClaudeClient
from src.common.health_server import HealthServer
from src.features.curating.curator_cog import CuratorCog
from src.features.summarising.summariser_cog import SummarizerCog
from src.features.summarising.summariser import ChannelSummarizer
from src.features.logging.logger_cog import LoggerCog
from src.features.sharing.sharing_cog import SharingCog
from src.features.sharing.sharer import Sharer
from src.features.reacting.reactor import Reactor
from src.features.reacting.reactor_cog import ReactorCog
from src.features.archive.archive_cog import ArchiveCog

def setup_logging(dev_mode=False):
    """Setup shared logging configuration for all bots"""
    log_handler = LogHandler(
        logger_name='DiscordBot',
        prod_log_file='discord_bot.log',
        dev_log_file='discord_bot_dev.log'
    )
    logger = log_handler.setup_logging(dev_mode)
    if not logger:
        print("ERROR: Failed to create logger")
        sys.exit(1)
    
    # Log all INFO and above to Supabase (for full visibility)
    setup_supabase_logging(
        logger,
        min_level=logging.INFO,  # Capture all INFO, WARNING, ERROR, CRITICAL
        batch_size=25,  # Smaller batches for faster visibility
        flush_interval=10.0  # Flush every 10 seconds
    )
    
    return logger

async def run_archive_script(days, dev_mode=False, logger=None, in_depth=False):
    """Run the archive_discord.py script with the specified number of days"""
    if logger is None:
        logger = logging.getLogger(__name__)

    from src.common.archive_runner import ArchiveRunner

    logger.info(f"Starting archive process for {days} days (in_depth={in_depth})")

    # Use the centralized ArchiveRunner
    archive_runner = ArchiveRunner()
    success = await archive_runner.run_archive(days, dev_mode, in_depth=in_depth)

    if not success:
        raise RuntimeError("Archive script failed")

async def main_async(args):
    logger = setup_logging(dev_mode=args.dev)
    
    # Start health check server immediately
    health_server = HealthServer(port=8080)
    health_server.start()
    
    # Log deployment info for diagnostics
    deployment_id = os.getenv('RAILWAY_DEPLOYMENT_ID', 'local')
    service_id = os.getenv('RAILWAY_SERVICE_ID', 'local')
    replica_id = os.getenv('RAILWAY_REPLICA_ID', 'local')
    logger.info(f"🚀 Starting deployment {deployment_id[:8]}... (service: {service_id[:8]}..., replica: {replica_id})")
    
    logger.info("Starting unified bot initialization")

    try:
        token = os.getenv('DISCORD_BOT_TOKEN')

        logger.debug(f"Token length: {len(token) if token else 0}")
        logger.debug(f"Token starts with: {token[:6]}..." if token else "No token found")
        logger.debug("Environment variable name used: DISCORD_BOT_TOKEN")

        if not token:
            raise ValueError("Discord bot token not found in environment variables")            

        # Create a single bot instance
        intents = discord.Intents.all()
        bot = BaseDiscordBot(
            command_prefix="!",
            logger=logger,
            dev_mode=args.dev,
            intents=intents
        )
        # Store the health server on the bot so cogs can access it
        bot.health_server = health_server
        
        # Store the command-line flags on the bot instance so cogs can access them
        bot.summary_now = args.summary_now
        bot.combine_only = args.combine_only
        bot.archive_days = args.archive_days
        bot.run_archive_script = run_archive_script  # Make the function available to cogs

        # Event to signal when --summary-now completes (replaces janky hasattr polling)
        bot.summary_completed = asyncio.Event()
        if not args.summary_now:
            # If no summary requested, mark as already complete so hourly fetch starts immediately
            bot.summary_completed.set()

        # ---- END BASIC EVENT TEST ----

        # ---- Initialize Core Components ----
        logger.info("Initializing core components (DB, Sharer, Reactor)...")

        # 1. Database Handler
        bot.db_handler = DatabaseHandler(dev_mode=args.dev)
        logger.info("DatabaseHandler initialized and attached to bot.")

        # 2. Claude Client (NEW - for Reactor and potentially other direct uses)
        claude_client_instance = ClaudeClient()
        bot.claude_client = claude_client_instance # Attach to bot if needed by other cogs directly, or for general access
        logger.info("ClaudeClient initialized and attached to bot.")

        # 3. Sharing Cog & Sharer Instance
        sharing_cog_instance = SharingCog(bot, bot.db_handler)
        await bot.add_cog(sharing_cog_instance)
        logger.info("SharingCog loaded via add_cog")
        if not sharing_cog_instance:
            logger.error("Failed to load SharingCog!")
            return
        # Retrieve the Sharer instance from the cog
        sharer_instance = sharing_cog_instance.sharer_instance
        if sharer_instance:
             logger.info("SharingCog loaded and Sharer instance retrieved.")
        else:
             logger.error("Failed to retrieve Sharer instance from SharingCog!")
             return

        # 4. Reactor Instance
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')

        # Create OpenMuse Interactor instance (needed by Reactor)
        openmuse_interactor_instance = OpenMuseInteractor(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            logger=logger
        )
        bot.openmuse_interactor_instance = openmuse_interactor_instance # Optional: Attach to bot if needed elsewhere
        logger.info("OpenMuseInteractor instance created.")

        # Pass the DB handler, OpenMuse interactor, and LLM client to the Reactor constructor
        reactor_instance = Reactor(
            logger=logger,
            sharer_instance=sharer_instance,
            db_handler=bot.db_handler, # Pass the db_handler instance
            openmuse_interactor=openmuse_interactor_instance, # Pass the interactor instance
            bot_instance=bot,
            llm_client=claude_client_instance, # Pass the ClaudeClient instance
            dev_mode=args.dev,
        )
        bot.reactor_instance = reactor_instance
        logger.info("Reactor instance created and attached to bot.")


        # ---- ADD OTHER COGS ----
        logger.info("Adding remaining cogs...")

        # Summarizer Cog - Create ChannelSummarizer instance
        channel_summarizer_instance = ChannelSummarizer(
            bot=bot,
            logger=logger,
            dev_mode=args.dev,
            command_prefix=bot.command_prefix,
            sharer_instance=sharer_instance
        )
        await bot.add_cog(SummarizerCog(bot, channel_summarizer_instance))
        logger.info("SummarizerCog loaded.")

        # Curator Cog
        await bot.add_cog(CuratorCog(bot, logger, args.dev))
        logger.info("CuratorCog loaded.")

        # Logger Cog
        await bot.add_cog(LoggerCog(bot, logger, args.dev))
        logger.info("LoggerCog loaded.")
        
        # Admin Cog
        try:
            from src.features.admin.admin_cog import AdminCog
            await bot.add_cog(AdminCog(bot))
            logger.info("AdminCog loaded.")
        except Exception as e:
            logger.warning(f"Failed to load AdminCog (skipping): {e}")
        
        # Admin Chat Cog (Claude-powered DM chat for admin)
        try:
            from src.features.admin_chat.admin_chat_cog import AdminChatCog
            await bot.add_cog(AdminChatCog(bot, bot.db_handler, sharer_instance))
            logger.info("AdminChatCog loaded.")
        except Exception as e:
            logger.warning(f"Failed to load AdminChatCog (skipping): {e}")

        # Reactor Cog (Needs bot.reactor_instance)
        await bot.add_cog(ReactorCog(bot, logger, args.dev))
        logger.info("ReactorCog loaded.")


        # Archive Cog (Handles standalone --archive-days operations)
        await bot.add_cog(ArchiveCog(bot))
        logger.info("ArchiveCog loaded.")

        # ---- SETUP HOURLY MESSAGE FETCHING ----
        @tasks.loop(hours=1)
        async def hourly_message_fetch():
            """Fetch new messages every hour. Uses --days 1 as a floor
            but the archive script only fetches messages newer than what's in DB."""
            try:
                logger.info("Starting hourly message fetch...")
                await run_archive_script(days=1, dev_mode=args.dev, logger=logger)
                logger.info("Hourly message fetch completed successfully")
            except Exception as e:
                logger.error(f"Error in hourly message fetch: {e}", exc_info=True)

        @hourly_message_fetch.before_loop
        async def before_hourly_fetch():
            """Wait for bot to be ready and for any --summary-now to complete before starting hourly fetch."""
            await bot.wait_until_ready()
            logger.info("Waiting for summary_completed event before starting hourly fetch...")
            await bot.summary_completed.wait()
            logger.info("Ready to start hourly message fetch loop")

        # Start the hourly fetch task
        hourly_message_fetch.start()
        logger.info("Hourly message fetch task scheduled")

        # Use a Cog listener instead of @bot.event to avoid overriding other on_ready handlers
        class ReadinessListener(commands.Cog):
            @commands.Cog.listener()
            async def on_ready(self_cog):
                health_server.mark_ready()
                health_server.update_heartbeat()
                logger.info(f"✅ Bot is ready! Logged in as {bot.user} (Deployment: {deployment_id[:8]}...)")

        await bot.add_cog(ReadinessListener(bot))
        
        # ---- RUN ----
        # Log the final intents object being used (changed to INFO level)
        logger.info(f"Final bot intents before starting: {bot.intents}") # Ensure this is INFO level and appears only ONCE
        bot.logger.info("All core components initialized and cogs added. Running the bot...")
        await bot.start(token)

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Error running unified bot: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        print(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Unified Discord Bot')
    parser.add_argument('--summary-now', action='store_true', help='Run the summary process immediately')
    parser.add_argument('--dev', action='store_true', help='Run in development mode')
    parser.add_argument('--archive-days', type=int, help='Number of days to archive (can be used standalone or with --summary-now)')
    parser.add_argument('--summary-with-archive', action='store_true', help='Archive past 24 hours FIRST, then run summary immediately')
    parser.add_argument('--combine-only', action='store_true',
                      help='Skip channel summaries, load existing ones from DB, and re-run only the combine+post step')
    parser.add_argument('--clear-today-summaries', action='store_true',
                      help='Delete today\'s summaries from Supabase before running (useful for re-running)')
    args = parser.parse_args()
    
    # Handle --clear-today-summaries flag
    if args.clear_today_summaries:
        print("🗑️  Clearing today's summaries from Supabase...")
        try:
            from supabase import create_client
            from dotenv import load_dotenv
            load_dotenv()
            url = os.getenv('SUPABASE_URL')
            key = os.getenv('SUPABASE_SERVICE_KEY')
            if url and key:
                supabase = create_client(url, key)
                today = datetime.now().strftime('%Y-%m-%d')
                result = supabase.table('daily_summaries').delete().eq('date', today).execute()
                deleted = len(result.data) if result.data else 0
                print(f"✅ Deleted {deleted} summary records for {today}")
            else:
                print("⚠️  SUPABASE_URL or SUPABASE_SERVICE_KEY not set, skipping clear")
        except Exception as e:
            print(f"⚠️  Error clearing summaries: {e}")
    
    # Check for date-based environment variable triggers
    # Priority: SUMMARY_WITH_ARCHIVE_DATE > JUST_SUMMARY_DATE
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # Check SUMMARY_WITH_ARCHIVE_DATE first (archive + summary)
    env_archive_date = os.getenv('SUMMARY_WITH_ARCHIVE_DATE')
    if env_archive_date:
        env_archive_date = env_archive_date.strip()
        try:
            parsed_date = datetime.strptime(env_archive_date, '%Y-%m-%d')
            parsed_date_str = parsed_date.strftime('%Y-%m-%d')
            
            if parsed_date_str == today_str:
                print(f"✓ SUMMARY_WITH_ARCHIVE_DATE={env_archive_date} matches today's date ({today_str}). Triggering archive + summary...")
                args.summary_with_archive = True
            else:
                print(f"ℹ SUMMARY_WITH_ARCHIVE_DATE={env_archive_date} set but doesn't match today ({today_str}). Skipping auto-trigger.")
        except ValueError:
            print(f"⚠ WARNING: SUMMARY_WITH_ARCHIVE_DATE='{env_archive_date}' is not a valid date format (expected YYYY-MM-DD). Ignoring.")
    
    # Check JUST_SUMMARY_DATE only if SUMMARY_WITH_ARCHIVE_DATE didn't trigger
    elif os.getenv('JUST_SUMMARY_DATE'):
        env_summary_date = os.getenv('JUST_SUMMARY_DATE').strip()
        try:
            parsed_date = datetime.strptime(env_summary_date, '%Y-%m-%d')
            parsed_date_str = parsed_date.strftime('%Y-%m-%d')
            
            if parsed_date_str == today_str:
                print(f"✓ JUST_SUMMARY_DATE={env_summary_date} matches today's date ({today_str}). Triggering summary only...")
                args.summary_now = True
            else:
                print(f"ℹ JUST_SUMMARY_DATE={env_summary_date} set but doesn't match today ({today_str}). Skipping auto-trigger.")
        except ValueError:
            print(f"⚠ WARNING: JUST_SUMMARY_DATE='{env_summary_date}' is not a valid date format (expected YYYY-MM-DD). Ignoring.")

    # Handle the combined flags
    if args.summary_with_archive:
        args.summary_now = True
        args.archive_days = 1

    if args.combine_only:
        args.summary_now = True

    # No validation needed - --archive-days can be used standalone or with --summary-now

    # Environment variables already loaded at module import time
    
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("Received keyboard interrupt, shutting down...")
    except Exception as e:
        print(f"Error running unified bot: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()