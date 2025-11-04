# src/features/summarising/summariser_cog.py

import asyncio
import os
import traceback
from datetime import datetime, timedelta, time, timezone
import time as standard_time # Rename standard time library if needed to avoid conflict
from discord.ext import commands
import logging
import discord
from discord.ext import tasks

from .summariser import ChannelSummarizer
# Import SharerCog to check for its instance
from src.features.sharing.sharing_cog import SharingCog 

MAX_RETRIES = 3
READY_TIMEOUT = 30
INITIAL_RETRY_DELAY = 5
MAX_RETRY_WAIT = 300  # 5 minutes

logger = logging.getLogger('DiscordBot')

class SummarizerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, channel_summarizer: ChannelSummarizer):
        self.bot = bot
        self.channel_summarizer = channel_summarizer
        self.run_daily_summary.start()

    def cog_unload(self):
        self.run_daily_summary.cancel()

    @tasks.loop(time=time(hour=10, minute=0, tzinfo=timezone.utc))
    async def run_daily_summary(self):
        """Daily task to run the summary generation at 10:00 UTC."""
        logger.info("Scheduled daily summary time reached (10:00 UTC). Starting...")
        try:
            await self.channel_summarizer.generate_summary()
            logger.info("Scheduled daily summary finished.")
        except Exception as e:
            logger.error(f"Error during scheduled summary run: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Handles the --summary-now flag on bot startup.
        
        If --archive-days is also specified, runs archive first, then summary.
        This ensures the summary uses the most up-to-date archived data.
        """
        if not hasattr(self, '_ran_summary_now_check'):
            self._ran_summary_now_check = True
            run_now_flag = getattr(self.bot, 'summary_now', False)
            archive_days = getattr(self.bot, 'archive_days', None)
            
            logger.info(f"SummarizerCog on_ready: summary_now={run_now_flag}, archive_days={archive_days}")
            
            if run_now_flag:
                logger.info("Detected --summary-now flag on startup.")
                
                # If archive_days is specified, run archive first
                if archive_days:
                    logger.info(f"Archive days specified ({archive_days}). Running archive process first.")
                    try:
                        from src.common.archive_runner import ArchiveRunner
                        
                        dev_mode = getattr(self.bot, 'dev_mode', False)
                        archive_runner = ArchiveRunner()
                        success = await archive_runner.run_archive(archive_days, dev_mode, in_depth=True)
                        
                        if success:
                            logger.info("Archive process completed successfully. Now starting summary.")
                        else:
                            logger.warning("Archive process failed. Continuing with summary anyway.")
                            
                    except Exception as e:
                        logger.error(f"Error during archive process: {e}", exc_info=True)
                        logger.info("Continuing with summary despite archive error.")
                
                # Run the summary
                try:
                    if hasattr(self, 'channel_summarizer'):
                         await self.channel_summarizer.generate_summary()
                         logger.info("Initial --summary-now run finished.")
                         # Signal that summary is complete so hourly fetch can start
                         self.bot._summary_now_completed = True
                    else:
                         logger.error("ChannelSummarizer not found during on_ready for --summary-now run.")
                except Exception as e:
                    logger.error(f"Error during initial --summary-now run: {e}", exc_info=True)
                    # Even if there's an error, signal completion so hourly fetch can start
                    self.bot._summary_now_completed = True
            else:
                logger.debug("No --summary-now flag detected on startup.")

    @commands.command(name="summarynow")
    @commands.is_owner() # Or check for specific admin role/ID
    async def summary_now_command(self, ctx):
        """Manually triggers the summary generation process."""
        logger.info(f"Manual summary triggered by {ctx.author.name}")
        await ctx.send("Starting manual summary generation...")
        try:
            # Access the generate_summary method from the stored instance
            await self.channel_summarizer.generate_summary()
            await ctx.send("Manual summary generation complete.")
        except Exception as e:
            logger.error(f"Error during manual summary run: {e}", exc_info=True)
            await ctx.send(f"An error occurred during summary generation: {e}")

async def setup(bot: commands.Bot):
    logger.info("Setting up SummarizerCog...")
    # Fetch logger and dev_mode from the bot instance
    cog_logger = getattr(bot, 'logger', logging.getLogger('SummarizerCog')) 
    dev_mode = getattr(bot, 'dev_mode', False)

    # --- Crucial Change: Get Sharer Instance --- 
    sharing_cog = bot.get_cog("SharingCog")
    if not sharing_cog or not hasattr(sharing_cog, 'sharer_instance'):
        cog_logger.critical("Failed to get Sharer instance from SharingCog. SummarizerCog cannot be loaded.")
        raise RuntimeError("Sharer instance not found for SummarizerCog setup")
    
    sharer_instance = sharing_cog.sharer_instance
    cog_logger.info("Successfully retrieved Sharer instance.")
    # ---------------------------------------------

    # Initialize ChannelSummarizer, passing the retrieved sharer_instance
    try:
        channel_summarizer_instance = ChannelSummarizer(
            bot=bot, # Pass the bot instance here
            logger=cog_logger, 
            dev_mode=dev_mode, 
            command_prefix=bot.command_prefix, # Get prefix from bot
            sharer_instance=sharer_instance # Pass the Sharer instance here
        )
        cog_logger.info("ChannelSummarizer instance created successfully.")
    except Exception as e:
        cog_logger.critical(f"Failed to initialize ChannelSummarizer for SummarizerCog: {e}", exc_info=True)
        raise # Re-raise the exception to prevent cog loading

    # Add the cog to the bot
    try:
        await bot.add_cog(SummarizerCog(bot, channel_summarizer_instance))
        cog_logger.info("SummarizerCog added to bot.")
    except Exception as e:
        cog_logger.critical(f"Failed to add SummarizerCog to bot: {e}", exc_info=True)
        raise
