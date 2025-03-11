# src/features/summarising/summariser_cog.py

import asyncio
import traceback
from datetime import datetime, timedelta
import time
from discord.ext import commands

MAX_RETRIES = 3
READY_TIMEOUT = 30
INITIAL_RETRY_DELAY = 5
MAX_RETRY_WAIT = 300  # 5 minutes

class SummarizerCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False, run_now=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.run_now = run_now
        self._shutdown_flag = False
        # If your summarizer logic used to store references to e.g. Claude, DB, etc.
        # you can keep them here. For example:
        self.logger.info("Initializing SummarizerCog...")
        # Any additional setup code that used to be in ChannelSummarizer.__init__:
        # e.g. your environment-based channel checks, etc.

    async def cog_load(self):
        """
        Called when this cog is loaded. By the time `on_ready` is eventually called,
        the bot should be connected. We'll then optionally run immediate summary,
        and start the daily scheduling background task.
        """
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Once the bot is connected, run immediate summary if requested,
        then start the daily summary scheduler.
        """
        # We only do these once:
        if not self.bot.summarizer_ready:
            self.logger.info("SummarizerCog is now handling on_ready...")
            self.bot.summarizer_ready = True

            # If user requested immediate summary via --summary-now
            if self.run_now:
                try:
                    self.logger.info("Running immediate summary generation...")
                    await asyncio.sleep(2)  # slight delay
                    await self.generate_summary()
                except Exception as e:
                    self.logger.error(f"Error during immediate summary: {e}")
                    self.logger.debug(traceback.format_exc())

            # Now that we've done immediate summary, start daily scheduling
            self._shutdown_flag = False
            self.logger.info("Starting scheduled daily summary loop...")
            self.bot.loop.create_task(self.schedule_daily_summary())

    async def schedule_daily_summary(self):
        """
        Daily summary logic that waits until 10:00 UTC and calls generate_summary().
        Loops until we set self._shutdown_flag to True or the bot closes.
        """
        try:
            retry_count = 0
            while not self._shutdown_flag and not self.bot.is_closed():
                now = datetime.utcnow()
                # Next 10:00 UTC
                target = now.replace(hour=10, minute=0, second=0, microsecond=0)
                if now.hour >= 10:
                    target += timedelta(days=1)

                delay = (target - now).total_seconds()
                hours_until_next = delay / 3600
                self.logger.info(
                    f"Next summary scheduled for {target} UTC ({hours_until_next:.1f} hours from now)"
                )

                try:
                    await asyncio.sleep(delay)
                    if not self._shutdown_flag:
                        self.logger.info("Starting scheduled summary generation")
                        await self.generate_summary()
                        retry_count = 0
                        self.logger.info("Scheduled summary generation completed successfully")
                except asyncio.CancelledError:
                    self.logger.info("Summary schedule cancelled - shutting down")
                    break
                except Exception as e:
                    if isinstance(e, RuntimeError) and "Concurrent call to receive() is not allowed" in str(e):
                        self.logger.warning(
                            "Concurrent call to receive() detected during scheduled summary generation. "
                            "Skipping summary generation this cycle."
                        )
                    else:
                        retry_count += 1
                        self.logger.error(f"Summary generation attempt {retry_count}/{MAX_RETRIES} failed: {e}")
                        if retry_count >= MAX_RETRIES:
                            self.logger.error(
                                f"Failed after {MAX_RETRIES} attempts - shutting down scheduler"
                            )
                            self._shutdown_flag = True
                            raise
                        wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
                        self.logger.info(f"Retrying in {wait_time/3600:.1f} hours")
                        await asyncio.sleep(wait_time)

        except Exception as e:
            self.logger.error(f"Fatal error in schedule_daily_summary: {e}")
            self.logger.debug(traceback.format_exc())
            self._shutdown_flag = True
            # In a real scenario, you might want to shut down the bot, or just the scheduling

    async def generate_summary(self):
        """
        The method that actually performs the summarization logic.
        Copied/adapted from your original ChannelSummarizer code, including
        your logic for searching channels, building summary messages, etc.
        """
        try:
            self.logger.info("Generating requested summary...")
            # ... your original summarization logic ...
            # e.g. channel lookup, chat with Claude, post final summary

            # (Example snippet) Suppose you had something like:
            # channel = self.bot.get_channel(summary_channel_id)  # from environment or dev config
            # if not channel:
            #     # handle error, log channels, etc.
            #     ...
            pass

        except Exception as e:
            self.logger.error(f"Error generating summary: {e}")
            self.logger.debug(traceback.format_exc())

    async def cleanup(self):
        """
        Cleanup if needed—this can be called from the outside or if you want a
        manual shutdown for your summarizer tasks.
        """
        self.logger.info("Starting cleanup for SummarizerCog...")
        # e.g. close DB connections, etc.

    @commands.command(name='manual_summary')
    async def manual_summary_command(self, ctx):
        """Optional command to trigger a summary manually."""
        await ctx.send("Manually triggering a summary. Stand by...")
        await self.generate_summary()
        await ctx.send("Summary completed.")
