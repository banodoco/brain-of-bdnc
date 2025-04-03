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
from src.common.claude_client import ClaudeClient
from src.features.curating.curator_cog import CuratorCog
from src.features.summarising.summariser_cog import SummarizerCog
from src.features.logging.logger_cog import LoggerCog
from src.features.sharing.sharing_cog import SharingCog
from src.features.sharing.sharer import Sharer
from src.features.reacting.reactor import Reactor
from src.features.reacting.reactor_cog import ReactorCog

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

        # ---- BASIC EVENT TEST ----
        @bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            # Log basic payload info
            logger.debug(f"<<< [MAIN.PY] RAW on_raw_reaction_add event received: Emoji={payload.emoji}, UserID={payload.user_id}, MessageID={payload.message_id}, ChannelID={payload.channel_id} >>>")
        # ---- END BASIC EVENT TEST ----

        # ---- Initialize Core Components ----
        logger.info("Initializing core components (DB, Claude, Sharer, Reactor)...")

        # 1. Database Handler
        bot.db_handler = DatabaseHandler(dev_mode=args.dev)
        logger.info("DatabaseHandler initialized and attached to bot.")

        # 2. Claude Client
        bot.claude_client = ClaudeClient()
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
        reactor_instance = Reactor(logger=logger, sharer_instance=sharer_instance, dev_mode=args.dev)
        bot.reactor_instance = reactor_instance
        logger.info("Reactor instance created and attached to bot.")

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

        # Reactor Cog (Needs bot.reactor_instance)
        await bot.load_extension("src.features.reacting.reactor_cog")
        logger.info("ReactorCog loaded.")

        # ---- RUN ----
        # Log the final intents object being used
        logger.debug(f"Final bot intents before starting: {bot.intents}")
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