import os
import sys
import argparse
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
import traceback
import time
import discord
from multiprocessing import Process

from src.features.curating.curator import ArtCurator
from src.features.summarising.summariser import ChannelSummarizer
from src.features.logging.logger import MessageLogger
from src.common.log_handler import LogHandler

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

# Constants
MAX_RETRIES = 3
READY_TIMEOUT = 30
INITIAL_RETRY_DELAY = 5
MAX_RETRY_WAIT = 300  # 5 minutes

async def run_summarizer(bot, token, run_now):
    """Run the summarizer bot with optional immediate summary generation."""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Start the bot
            bot_task = asyncio.create_task(bot.start(token))

            # Wait for the bot to become "ready" or time out
            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Summarizer bot failed to become ready within timeout period")
                await asyncio.sleep(1)

            bot.logger.info("Summarizer bot is ready and fully connected")

            if run_now:
                try:
                    bot.logger.info("Running immediate summary generation...")
                    await asyncio.sleep(2)  # small delay
                    await bot.generate_summary()
                finally:
                    # Even if generation fails, we clean up
                    await bot.cleanup()
                    await bot.close()
                    bot_task.cancel()
                    await cleanup_tasks([bot_task])
            else:
                bot.logger.info("Starting scheduled mode...")
                bot._shutdown_flag = False

                scheduler_task = asyncio.create_task(schedule_daily_summary(bot))

                # Wait for the bot to end or the scheduler to end
                done, pending = await asyncio.wait(
                    [bot_task, scheduler_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                bot._shutdown_flag = True
                await cleanup_tasks(pending)

                for task in done:
                    if task.exception():
                        raise task.exception()

            # If no error occurred, break out of the retry loop
            break

        except Exception as e:
            bot.logger.error(f"Error in run_summarizer: {e}")
            bot.logger.debug(traceback.format_exc())
            retry_count += 1

            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed after {MAX_RETRIES} retries - giving up")
                raise

            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            bot.logger.info(f"Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)

async def run_curator(bot, token):
    """Run the curator bot."""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            bot_task = asyncio.create_task(bot.start(token))

            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Curator bot failed to become ready within timeout period")
                await asyncio.sleep(1)

            bot.logger.info("Curator bot is ready and fully connected")

            done, pending = await asyncio.wait(
                [bot_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            await cleanup_tasks(pending)

            for task in done:
                if task.exception():
                    raise task.exception()

            return  # If we reach here, it connected successfully

        except (TimeoutError, discord.errors.DiscordServerError) as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed to connect after {MAX_RETRIES} attempts")
                raise

            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            bot.logger.warning(
                f"Connection attempt {retry_count} failed: {e}. "
                f"Retrying in {wait_time} seconds..."
            )
            await asyncio.sleep(wait_time)
        except Exception as e:
            bot.logger.error(f"Error running curator bot: {e}")
            bot.logger.debug(traceback.format_exc())
            raise

async def run_logger(bot, token):
    """Run the message logger bot."""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            bot_task = asyncio.create_task(bot.start(token))

            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Logger bot failed to become ready within timeout period")
                await asyncio.sleep(1)

            bot.logger.info("Logger bot is ready and fully connected")

            done, pending = await asyncio.wait(
                [bot_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            await cleanup_tasks(pending)

            for task in done:
                if task.exception():
                    raise task.exception()

            return  # success

        except (TimeoutError, discord.errors.DiscordServerError) as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed to connect after {MAX_RETRIES} attempts")
                raise

            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            bot.logger.warning(
                f"Connection attempt {retry_count} failed: {e}. "
                f"Retrying in {wait_time} seconds..."
            )
            await asyncio.sleep(wait_time)
        except Exception as e:
            bot.logger.error(f"Error running logger bot: {e}")
            bot.logger.debug(traceback.format_exc())
            raise

async def schedule_daily_summary(bot):
    """
    Run daily summaries on schedule. Only exits on error or explicit shutdown.
    """
    try:
        while not bot._shutdown_flag:
            retry_count = 0
            now = datetime.utcnow()

            # Next 10:00 UTC
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now.hour >= 10:
                target += timedelta(days=1)

            delay = (target - now).total_seconds()
            hours_until_next = delay / 3600
            bot.logger.info(
                f"Next summary scheduled for {target} UTC "
                f"({hours_until_next:.1f} hours from now)"
            )

            try:
                await asyncio.sleep(delay)
                if not bot._shutdown_flag:
                    bot.logger.info("Starting scheduled summary generation")
                    await bot.generate_summary()
                    retry_count = 0
                    bot.logger.info("Scheduled summary generation completed successfully")

            except asyncio.CancelledError:
                bot.logger.info("Summary schedule cancelled - shutting down")
                break
            except Exception as e:
                if ("Concurrent call to receive()" in str(e)):
                    bot.logger.warning(
                        "Concurrent call to receive() was triggered. Skipping summary this cycle."
                    )
                else:
                    retry_count += 1
                    bot.logger.error(
                        f"Summary generation attempt {retry_count}/{MAX_RETRIES} failed: {e}"
                    )
                    if retry_count >= MAX_RETRIES:
                        bot.logger.error(
                            f"Failed after {MAX_RETRIES} attempts - shutting down scheduler"
                        )
                        bot._shutdown_flag = True
                        raise
                    wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
                    bot.logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)

    except Exception as e:
        bot.logger.error(f"Fatal error in scheduler: {e}")
        bot.logger.debug(traceback.format_exc())
        bot._shutdown_flag = True
        raise

async def cleanup_tasks(tasks):
    """Properly cleanup any pending tasks."""
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

def main():
    parser = argparse.ArgumentParser(description='Discord Bots')
    parser.add_argument('--summary-now', action='store_true', help='Run the summary process immediately')
    parser.add_argument('--dev', action='store_true', help='Run in development mode')
    args = parser.parse_args()

    load_dotenv()

    logger = setup_logging(args.dev)
    logger.info("Starting bot initialization")

    try:
        token = os.getenv('DISCORD_BOT_TOKEN')
        if not token:
            raise ValueError("Discord bot token not found in environment variables")

        logger.info("Configuration loaded successfully, starting bots in separate processes")

        def start_curator():
            local_logger = setup_logging(dev_mode=args.dev)
            from src.features.curating.curator import ArtCurator
            curator_bot = ArtCurator(logger=local_logger, dev_mode=args.dev)
            asyncio.run(run_curator(curator_bot, token))

        def start_summarizer():
            local_logger = setup_logging(dev_mode=args.dev)
            from src.features.summarising.summariser import ChannelSummarizer
            summarizer_bot = ChannelSummarizer(logger=local_logger, dev_mode=args.dev)
            asyncio.run(run_summarizer(summarizer_bot, token, args.summary_now))

        def start_logger():
            local_logger = setup_logging(dev_mode=args.dev)
            from src.features.logging.logger import MessageLogger
            logger_bot_inst = MessageLogger(dev_mode=args.dev)
            logger_bot_inst.logger = local_logger
            asyncio.run(run_logger(logger_bot_inst, token))

        curator_process = Process(target=start_curator)
        summarizer_process = Process(target=start_summarizer)
        logger_process = Process(target=start_logger)

        curator_process.start()
        summarizer_process.start()
        logger_process.start()

        curator_process.join()
        summarizer_process.join()
        logger_process.join()

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Error running bots: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
