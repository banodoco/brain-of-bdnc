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
from src.features.curating.curator_cog import CuratorCog
from src.features.summarising.summariser_cog import SummarizerCog
from src.features.logging.logger_cog import LoggerCog

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
        print("=== TOKEN DEBUG ===")
        print(f"Raw token value: {token}")
        print("=================")
        
        if not token:
            raise ValueError("Discord bot token not found in environment variables")
            
        # Add debug logging for token
        logger.debug(f"Token length: {len(token) if token else 0}")
        logger.debug(f"Token starts with: {token[:6]}..." if token else "No token found")
        logger.debug(f"Environment variable name used: DISCORD_BOT_TOKEN")

        # Create a single bot instance
        intents = discord.Intents.all()
        bot = BaseDiscordBot(
            command_prefix="!",
            logger=logger,
            dev_mode=args.dev,
            intents=intents
        )

        # ---- ADD COGS ----
        # Summarizer Cog
        summarizer_cog = SummarizerCog(bot, logger=logger, dev_mode=args.dev, run_now=args.summary_now)
        await bot.add_cog(summarizer_cog)

        # Curator Cog
        curator_cog = CuratorCog(bot, logger=logger, dev_mode=args.dev)
        await bot.add_cog(curator_cog)

        # Logger Cog
        logger_cog = LoggerCog(bot, logger=logger, dev_mode=args.dev)
        await bot.add_cog(logger_cog)

        # ---- RUN ----
        bot.logger.info("All cogs added. Running the bot...")
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

    print("=== BEFORE LOADING .ENV ===")
    print(f"Token before .env: {os.getenv('DISCORD_BOT_TOKEN')}")
    print("=========================")

    # Get the directory containing main.py
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(current_dir, '.env')
    load_dotenv(dotenv_path=env_path, override=True)
    
    print("=== AFTER LOADING .ENV ===")
    print(f"Token after .env: {os.getenv('DISCORD_BOT_TOKEN')}")
    print("=========================")

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