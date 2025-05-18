import os
import sys
import argparse
import logging
import asyncio
import time
from datetime import datetime
import traceback

from dotenv import load_dotenv
from discord.ext import commands
import discord

from src.common.log_handler import LogHandler
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
    return logger

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
        # Store the command-line flag on the bot instance so cogs can access it
        bot.summary_now = args.summary_now

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
        await bot.load_extension("src.features.sharing.sharing_cog")
        sharing_cog_instance = bot.get_cog("SharingCog")
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
        await bot.load_extension("src.features.summarising.summariser_cog")
        logger.info("SummarizerCog loaded.")

        # Curator Cog
        await bot.load_extension("src.features.curating.curator_cog")
        logger.info("CuratorCog loaded.")

        # Logger Cog
        await bot.load_extension("src.features.logging.logger_cog")
        logger.info("LoggerCog loaded.")
        
        # Admin Cog (New)
        try:
            logger.info("Attempting to load AdminCog...")
            from src.features.admin.admin_cog import AdminCog
            await bot.add_cog(AdminCog(bot))
            logger.info("AdminCog successfully loaded and added to bot")
        except Exception as e:
            logger.error(f"Failed to load AdminCog: {e}", exc_info=True)
            raise  # Re-raise to prevent bot from starting with missing functionality

        # Reactor Cog (Needs bot.reactor_instance)
        await bot.load_extension("src.features.reacting.reactor_cog")
        logger.info("ReactorCog loaded.")

        # Relaying Cog (New - Needs bot.relayer_instance)
        await bot.load_extension("src.features.relaying.relaying_cog")
        logger.info("RelayingCog loaded.")

        # ---- RUN ----
        # Log the final intents object being used (changed to INFO level)
        logger.info(f"Final bot intents before starting: {bot.intents}") # Ensure this is INFO level and appears only ONCE
        bot.logger.info("All core components initialized and cogs added. Running the bot...")
        await bot.start(token)

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Error running unified bot: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Unified Discord Bot')
    parser.add_argument('--summary-now', action='store_true', help='Run the summary process immediately')
    parser.add_argument('--dev', action='store_true', help='Run in development mode')
    args = parser.parse_args()

    # Get the directory containing main.py
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(current_dir, '.env')
    load_dotenv(dotenv_path=env_path, override=True)
    
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