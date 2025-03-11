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
    # Create log handler with proper mode
    log_handler = LogHandler(
        logger_name='DiscordBot',
        prod_log_file='discord_bot.log',
        dev_log_file='discord_bot_dev.log'
    )
    
    # Setup logging with proper mode
    logger = log_handler.setup_logging(dev_mode)
    
    # Verify logger was created successfully
    if not logger:
        print("ERROR: Failed to create logger")
        sys.exit(1)
        
    return logger

# Constants
MAX_RETRIES = 3
READY_TIMEOUT = 30  # Reduced timeout to 30 seconds for faster failure detection
INITIAL_RETRY_DELAY = 5  # 5 seconds initial delay for retry attempts
MAX_RETRY_WAIT = 300  # Maximum wait of 300 seconds (5 minutes) between retries
HEARTBEAT_CHECK_INTERVAL = 30  # Check connection every 30 seconds

async def run_summarizer(bot, token, run_now):
    """Run the summarizer bot with optional immediate summary generation"""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Create task for bot connection and health monitoring
            bot_task = asyncio.create_task(bot.start(token))
            health_monitor = asyncio.create_task(check_connection_health(bot))
            
            # Wait for bot to be ready
            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Summarizer bot failed to become ready within timeout period")
                await asyncio.sleep(1)
            
            bot.logger.info("Summarizer bot is ready and fully connected")
            
            if run_now:
                try:
                    bot.logger.info("Running immediate summary generation...")
                    await asyncio.sleep(2)  # Extra sleep
                    await bot.generate_summary()
                finally:
                    # Ensure cleanup happens even if summary generation fails
                    await bot.cleanup()
                    await bot.close()
                    bot_task.cancel()
                    health_monitor.cancel()
                    # Clean up tasks
                    await cleanup_tasks([bot_task, health_monitor])
            else:
                bot.logger.info("Starting scheduled mode...")
                bot._shutdown_flag = False  # Ensure shutdown flag is False for scheduled mode
                # Create and start the scheduler task
                scheduler_task = asyncio.create_task(schedule_daily_summary(bot))
                
                # Wait for any task to complete
                done, pending = await asyncio.wait(
                    [bot_task, scheduler_task, health_monitor],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # If we get here, one of the tasks completed or failed
                # Set shutdown flag to ensure proper cleanup
                bot._shutdown_flag = True
                
                # Cancel any pending tasks
                await cleanup_tasks(pending)
                
                # If we're here due to an error, raise it
                for task in done:
                    if task.exception():
                        raise task.exception()
            
            # If we get here without errors, break the retry loop
            break
            
        except Exception as e:
            bot.logger.error(f"Error in run_summarizer: {e}")
            bot.logger.debug(traceback.format_exc())
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed after {MAX_RETRIES} retries - giving up")
                raise
            
            # Wait before retrying, with exponential backoff
            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            if wait_time < 60:
                bot.logger.info(f"Retrying in {wait_time} seconds")
            else:
                bot.logger.info(f"Retrying in {wait_time/3600:.1f} hours")
            await asyncio.sleep(wait_time)

async def check_connection_health(bot):
    """Monitor bot connection health and attempt reconnection if needed"""
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3
    
    while not bot._shutdown_flag:
        try:
            # Check if bot is connected and responding
            if not bot.is_ready() or not bot.latency:
                consecutive_failures += 1
                bot.logger.warning(f"Connection check failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    bot.logger.error("Connection deemed unhealthy. Relying on discord.py's built-in reconnect mechanism.")
                    consecutive_failures = 0
            else:
                consecutive_failures = 0
                
        except Exception as e:
            bot.logger.error(f"Error in connection health check: {e}")
            consecutive_failures += 1
        
        await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)

async def run_curator(bot, token):
    """Run the curator bot"""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Create task for bot connection
            bot_task = asyncio.create_task(bot.start(token))
            
            # Wait for bot to be ready
            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Curator bot failed to become ready within timeout period")
                await asyncio.sleep(1)
                
            bot.logger.info("Curator bot is ready and fully connected")
            
            # Start health monitoring
            health_monitor = asyncio.create_task(check_connection_health(bot))
            
            # Create done, pending sets for task management
            done, pending = await asyncio.wait(
                [bot_task, health_monitor],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Ensure proper cleanup
            await cleanup_tasks(pending)
            
            # Check for exceptions
            for task in done:
                if task.exception():
                    raise task.exception()
            
            return  # Success - exit the retry loop
                
        except (TimeoutError, discord.errors.DiscordServerError) as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed to connect after {MAX_RETRIES} attempts")
                raise
            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            bot.logger.warning(f"Connection attempt {retry_count} failed: {e}. Retrying in {wait_time/3600:.1f} hours")
            await asyncio.sleep(wait_time)
        except Exception as e:
            bot.logger.error(f"Error running curator bot: {e}")
            bot.logger.debug(traceback.format_exc())
            raise

async def schedule_daily_summary(bot):
    """Run daily summaries on schedule. Only exits if there's an error or explicit shutdown."""
    try:
        while not bot._shutdown_flag:
            retry_count = 0  # Reset retry count for each day's attempt
            # Get current UTC time
            now = datetime.utcnow()
            
            # Set target time to 10:00 UTC today
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            
            # If it's already past 10:00 UTC today, schedule for tomorrow
            if now.hour >= 10:
                target += timedelta(days=1)
            
            # Calculate how long to wait
            delay = (target - now).total_seconds()
            hours_until_next = delay/3600
            bot.logger.info(f"Next summary scheduled for {target} UTC ({hours_until_next:.1f} hours from now)")
            
            # Wait until the target time
            try:
                await asyncio.sleep(delay)
                if not bot._shutdown_flag:
                    bot.logger.info("Starting scheduled summary generation")
                    await bot.generate_summary()
                    # Success - clear retry count
                    retry_count = 0
                    bot.logger.info("Scheduled summary generation completed successfully")
            except asyncio.CancelledError:
                bot.logger.info("Summary schedule cancelled - shutting down")
                break
            except Exception as e:
                if isinstance(e, RuntimeError) and "Concurrent call to receive() is not allowed" in str(e):
                    bot.logger.warning("Concurrent call to receive() detected during scheduled summary generation. Skipping summary generation this cycle.")
                else:
                    retry_count += 1
                    bot.logger.error(f"Summary generation attempt {retry_count}/{MAX_RETRIES} failed: {e}")
                    if retry_count >= MAX_RETRIES:
                        bot.logger.error(f"Failed after {MAX_RETRIES} attempts - shutting down scheduler")
                        bot._shutdown_flag = True
                        raise
                    wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
                    bot.logger.info(f"Retrying in {wait_time/3600:.1f} hours")
                    await asyncio.sleep(wait_time)
    except Exception as e:
        bot.logger.error(f"Fatal error in scheduler: {e}")
        bot.logger.debug(traceback.format_exc())
        bot._shutdown_flag = True
        raise

async def cleanup_tasks(tasks):
    """Properly cleanup any pending tasks"""
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                # Add 5 second timeout for task cleanup
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

async def run_logger(bot, token):
    """Run the message logger bot"""
    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Create task for bot connection
            bot_task = asyncio.create_task(bot.start(token))
            
            # Wait for bot to be ready
            start_time = time.time()
            while not bot.is_ready():
                if time.time() - start_time > READY_TIMEOUT:
                    raise TimeoutError("Logger bot failed to become ready within timeout period")
                await asyncio.sleep(1)
                
            bot.logger.info("Logger bot is ready and fully connected")
            
            # Start health monitoring
            health_monitor = asyncio.create_task(check_connection_health(bot))
            
            # Create done, pending sets for task management
            done, pending = await asyncio.wait(
                [bot_task, health_monitor],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Ensure proper cleanup
            await cleanup_tasks(pending)
            
            # Check for exceptions
            for task in done:
                if task.exception():
                    raise task.exception()
            
            return  # Success - exit the retry loop
                
        except (TimeoutError, discord.errors.DiscordServerError) as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                bot.logger.error(f"Failed to connect after {MAX_RETRIES} attempts")
                raise
            wait_time = min(INITIAL_RETRY_DELAY * (2 ** retry_count), MAX_RETRY_WAIT)
            bot.logger.warning(f"Connection attempt {retry_count} failed: {e}. Retrying in {wait_time/3600:.1f} hours")
            await asyncio.sleep(wait_time)
        except Exception as e:
            bot.logger.error(f"Error running logger bot: {e}")
            bot.logger.debug(traceback.format_exc())
            raise

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Discord Bots')
    parser.add_argument('--summary-now', action='store_true', help='Run the summary process immediately')
    parser.add_argument('--dev', action='store_true', help='Run in development mode')
    args = parser.parse_args()
    
    # Load environment variables
    load_dotenv()
    
    # Setup shared logging for all bots
    logger = setup_logging(args.dev)
    logger.info("Starting bot initialization")
    
    try:
        # Get bot token
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