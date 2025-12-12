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
from src.features.curating.curator_cog import CuratorCog
from src.features.summarising.summariser_cog import SummarizerCog
from src.features.logging.logger_cog import LoggerCog
from src.features.sharing.sharing_cog import SharingCog
from src.features.sharing.sharer import Sharer
from src.features.reacting.reactor import Reactor
from src.features.reacting.reactor_cog import ReactorCog
from src.features.relaying.relayer import Relayer

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

async def run_archive_script(days, dev_mode=False, logger=None):
    """Run the archive_discord.py script with the specified number of days"""
    if logger is None:
        logger = logging.getLogger(__name__)
    
    from src.common.archive_runner import ArchiveRunner
    
    logger.info(f"Starting archive process for {days} days")
    
    # Use the centralized ArchiveRunner
    archive_runner = ArchiveRunner()
    success = await archive_runner.run_archive(days, dev_mode, in_depth=True)
    
    if not success:
        raise RuntimeError("Archive script failed")

async def main_async(args):
    logger = setup_logging(dev_mode=args.dev)
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
        # Store the command-line flags on the bot instance so cogs can access them
        bot.summary_now = args.summary_now
        bot.archive_days = args.archive_days
        bot.run_archive_script = run_archive_script  # Make the function available to cogs

        # ---- END BASIC EVENT TEST ----

        # ---- Initialize Core Components ----
        logger.info("Initializing core components (DB, Sharer, Reactor, Relayer)...")

        # 1. Database Handler
        bot.db_handler = DatabaseHandler(dev_mode=args.dev)
        logger.info("DatabaseHandler initialized and attached to bot.")

        # 2. Claude Client (NEW - for Reactor and potentially other direct uses)
        claude_client_instance = ClaudeClient()
        bot.claude_client = claude_client_instance # Attach to bot if needed by other cogs directly, or for general access
        logger.info("ClaudeClient initialized and attached to bot.")

        # 3. Sharing Cog & Sharer Instance
        logger.info("About to load SharingCog...")
        from src.features.sharing.sharing_cog import SharingCog
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

        # 5. Relayer Instance (New)
        relayer_instance = Relayer(bot=bot, logger=logger, dev_mode=args.dev)
        bot.relayer_instance = relayer_instance
        logger.info("Relayer instance created and attached to bot.")

        # ---- ADD OTHER COGS ----
        logger.info("Adding remaining cogs...")

        # Summarizer Cog
        from src.features.summarising.summariser_cog import SummarizerCog
        from src.features.summarising.summariser import ChannelSummarizer
        
        # Create ChannelSummarizer instance
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
        from src.features.curating.curator_cog import CuratorCog
        await bot.add_cog(CuratorCog(bot, logger, args.dev))
        logger.info("CuratorCog loaded.")

        # Logger Cog
        from src.features.logging.logger_cog import LoggerCog
        await bot.add_cog(LoggerCog(bot, logger, args.dev))
        logger.info("LoggerCog loaded.")
        
        # Admin Cog (New) - Skip for now due to import issues
        try:
            logger.info("Attempting to load AdminCog...")
            from src.features.admin.admin_cog import AdminCog
            await bot.add_cog(AdminCog(bot))
            logger.info("AdminCog successfully loaded and added to bot")
        except Exception as e:
            logger.warning(f"Failed to load AdminCog (skipping): {e}")
            # Don't raise - continue without AdminCog for now

        # Reactor Cog (Needs bot.reactor_instance)
        from src.features.reacting.reactor_cog import ReactorCog
        await bot.add_cog(ReactorCog(bot, logger, args.dev))
        logger.info("ReactorCog loaded.")

        # Relaying Cog (New - Needs bot.relayer_instance)
        from src.features.relaying.relaying_cog import RelayingCog
        await bot.add_cog(RelayingCog(bot, logger, args.dev))
        logger.info("RelayingCog loaded.")

        # Archive Cog (Handles standalone --archive-days operations)
        from src.features.archive.archive_cog import ArchiveCog
        await bot.add_cog(ArchiveCog(bot))
        logger.info("ArchiveCog loaded.")

        # ---- SETUP HOURLY MESSAGE FETCHING ----
        @tasks.loop(hours=1)
        async def hourly_message_fetch():
            """Fetch new messages every hour instead of real-time processing"""
            try:
                logger.info("Starting hourly message fetch...")
                # Fetch messages from the last 1 day to ensure we don't miss any
                # Using 1 day instead of hours to be safe with the archive script's day-based logic
                await run_archive_script(days=1, dev_mode=args.dev, logger=logger)
                logger.info("Hourly message fetch completed successfully")
            except Exception as e:
                logger.error(f"Error in hourly message fetch: {e}", exc_info=True)

        @hourly_message_fetch.before_loop
        async def before_hourly_fetch():
            """Wait for bot to be ready and for any --summary-now to complete before starting hourly fetch"""
            await bot.wait_until_ready()
            
            # If --summary-now was specified, wait for it to complete first
            if hasattr(bot, 'summary_now') and bot.summary_now:
                logger.info("Detected --summary-now flag. Waiting for summary to complete before starting hourly fetch...")
                # Wait for the summary to complete (check every 5 seconds)
                while not hasattr(bot, '_summary_now_completed'):
                    await asyncio.sleep(5)
                logger.info("Summary completed. Now starting hourly message fetch loop")
            else:
                logger.info("Bot is ready, starting hourly message fetch loop")

        # Start the hourly fetch task
        hourly_message_fetch.start()
        logger.info("Hourly message fetch task started")

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

    # Handle the combined flag
    if args.summary_with_archive:
        args.summary_now = True
        args.archive_days = 1

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