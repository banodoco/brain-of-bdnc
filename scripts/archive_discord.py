import os
import sys
import argparse
# Add project root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from dotenv import load_dotenv
from typing import Optional, List, Dict, Union
import json
from src.common.db_handler import DatabaseHandler
from src.common.constants import get_database_path
from src.common.base_bot import BaseDiscordBot
from src.common.rate_limiter import RateLimiter
import threading
import queue
import time
import dateutil.parser

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

def to_aware_utc(dt_str: str) -> datetime:
    """Convert an ISO format string to a timezone-aware datetime object."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_db(db_path):
    """Get thread-local database connection."""
    if not hasattr(thread_local, "db"):
        thread_local.db = DatabaseHandler(db_path)
        # Only initialize SQLite if we're using it (not in Supabase-only mode)
        storage_backend = os.getenv('STORAGE_BACKEND', 'both')
        if storage_backend in ['sqlite', 'both']:
            thread_local.db._init_db()
    return thread_local.db

class MessageArchiver(BaseDiscordBot):
    def __init__(self, dev_mode=False, order="newest", days=None, batch_size=500, in_depth=False, channel_id=None, fetch_reactions=False, start_date_str=None, end_date_str=None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.reactions = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            heartbeat_timeout=120.0,
            guild_ready_timeout=30.0,
            gateway_queue_size=512,
            logger=logger
        )
        
        # Load environment variables
        load_dotenv()
        
        # Set database path based on mode
        self.db_path = get_database_path(dev_mode)
        
        # Create a queue for database operations
        self.db_queue = queue.Queue()
        
        # Track total messages archived
        self.total_messages_archived = 0
        
        # Check if token exists
        if not os.getenv('DISCORD_BOT_TOKEN'):
            raise ValueError("DISCORD_BOT_TOKEN not found in environment variables")
        
        # Add reconnect settings
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        
        # Get bot user ID from env
        self.bot_user_id = int(os.getenv('BOT_USER_ID'))
        
        # Get guild ID based on mode
        self.guild_id = int(os.getenv('DEV_GUILD_ID' if dev_mode else 'GUILD_ID'))
        
        # Channels to skip
        self.skip_channels = {1076117621407223832}  # Welcome channel
        
        # Default config for all channels
        self.default_config = {
            'batch_size': batch_size,
            'delay': 0.25
        }
        
        # Set message ordering
        self.oldest_first = order.lower() == "oldest"
        logger.info(f"Message ordering: {'oldest to newest' if self.oldest_first else 'newest to oldest'}")
        
        # Set days limit - mutually exclusive with start/end date
        self.days_limit = days if not (start_date_str or end_date_str) else None
        if self.days_limit:
            logger.info(f"Will fetch messages from the last {self.days_limit} days")
        elif not (start_date_str or end_date_str):
            logger.info("Will fetch all available messages (checking DB range)")

        # Set start and end dates
        self.start_date = None
        self.end_date = None
        if start_date_str or end_date_str:
            if not start_date_str or not end_date_str:
                raise ValueError("Both --start-date and --end-date must be provided together.")
            try:
                # Parse start date (beginning of the day, UTC)
                self.start_date = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                # Parse end date and add one day to make it inclusive (beginning of the next day, UTC)
                self.end_date = (datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=timezone.utc)

                if self.start_date >= self.end_date:
                    raise ValueError("Start date must be before end date.")
                logger.info(f"Fetching messages strictly between {self.start_date.strftime('%Y-%m-%d %H:%M:%S %Z')} and {self.end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            except ValueError as e:
                logger.error(f"Invalid date format or range: {e}. Please use YYYY-MM-DD.")
                raise
            
        # Set in-depth mode
        self.in_depth = in_depth
        if in_depth:
            logger.info("Running in in-depth mode - will perform thorough message checks")
            
        # Set fetch reactions mode
        self.fetch_reactions = fetch_reactions
        if fetch_reactions:
            logger.info("Will fetch reactions for all messages in range")
            
        # Set specific channel to archive
        self.target_channel_id = channel_id
        if channel_id:
            logger.info(f"Will only archive channel with ID: {channel_id}")
        
        # Add rate limiting tracking
        self.last_api_call = datetime.now()
        self.api_call_count = 0
        self.rate_limit_reset = datetime.now()
        self.rate_limit_remaining = 50
        
        # Initialize rate limiter
        self.rate_limiter = RateLimiter()
        
        # Add member update cache
        self.member_update_cache = {}
        self.member_update_cache_timeout = 300  # 5 minutes
        
        # Start database worker thread
        self.db_thread = threading.Thread(target=self._db_worker, daemon=True)
        self.db_thread.start()

        # Insert after the super().__init__(...) call
        self._connection_history = []  # Initialize connection history to prevent shutdown errors

        self.total_days_in_range = 0
        if self.start_date and self.end_date:
            # Calculate total days duration for progress reporting
            self.total_days_in_range = (self.end_date - self.start_date).days
            if self.total_days_in_range <= 0: # Should be caught by earlier check, but safety first
                self.total_days_in_range = 1

    def _db_worker(self):
        """Worker thread for database operations."""
        db = get_db(self.db_path)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        while True:
            try:
                # Get the next operation from the queue
                operation = self.db_queue.get()
                if operation is None:
                    break
                
                # Execute the operation
                func, args, kwargs, future = operation
                try:
                    result = func(db, *args, **kwargs)
                    if asyncio.iscoroutine(result):
                        result = loop.run_until_complete(result)

                    # Only try to set result if the future is not done
                    if not future.done():
                        try:
                            # Create a callback to set the result in the event loop
                            def set_result_callback():
                                if not future.done():
                                    future.set_result(result)
                            self.loop.call_soon_threadsafe(set_result_callback)
                        except Exception as e:
                            logger.error(f"Error setting future result: {e}")
                except Exception as exception:
                    # Only try to set exception if the future is not done
                    if not future.done():
                        try:
                            # Create a callback to set the exception in the event loop
                            # Capture the exception in the closure
                            def set_exception_callback(e=exception):
                                if not future.done():
                                    future.set_exception(e)
                            self.loop.call_soon_threadsafe(set_exception_callback)
                        except Exception as e:
                            logger.error(f"Error setting future exception: {e}")
                
                self.db_queue.task_done()
            except Exception as e:
                logger.error(f"Error in database worker: {e}")
                continue
        
        loop.close()

    async def _db_operation(self, func, *args, **kwargs):
        """Execute a database operation in the worker thread."""
        # Make sure we have an event loop
        if not self.loop or self.loop.is_closed():
            self.loop = asyncio.get_event_loop()
        
        future = self.loop.create_future()
        self.db_queue.put((func, args, kwargs, future))
        try:
            return await future
        except Exception as e:
            logger.error(f"Error in database operation: {e}")
            raise

    async def setup_hook(self):
        """Setup hook to initialize database and start archiving."""
        try:
            # Initialize database in the worker thread
            await self._db_operation(lambda db: None)
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    async def _process_message(self, message, channel_id):
        """Process a single message and store it in the database."""
        try:
            message_start_time = datetime.now()
            
            # Calculate total reaction count and get reactors
            reaction_count = sum(reaction.count for reaction in message.reactions) if message.reactions else 0
            reactors = []
            
            if reaction_count > 0 and message.reactions:
                reaction_start_time = datetime.now()
                reactor_ids = set()
                try:
                    message_exists = await self._db_operation(
                        lambda db: db.message_exists(message.id)
                    )
                    if self.in_depth or self.fetch_reactions or not message_exists:
                        logger.debug(f"Processing reactions for message {message.id}: {len(message.reactions)} types, {reaction_count} total reactions")
                        
                        guild = self.get_guild(self.guild_id)
                        
                        for reaction in message.reactions:
                            reaction_process_start = datetime.now()
                            try:
                                async def fetch_users():
                                    async for user in reaction.users(limit=50):
                                        reactor_ids.add(user.id)
                                        # Check if we've recently updated this member
                                        cache_key = f"{user.id}_{user.name}"
                                        cache_time = self.member_update_cache.get(cache_key, 0)
                                        current_time = time.time()
                                        
                                        if current_time - cache_time > self.member_update_cache_timeout:
                                            member = guild.get_member(user.id) if guild else None
                                            role_ids = json.dumps([role.id for role in member.roles]) if member and member.roles else None
                                            guild_join_date = member.joined_at.isoformat() if member and member.joined_at else None
                                            
                                            await self._db_operation(
                                                lambda db: db.create_or_update_member(
                                                    user.id,
                                                    user.name,
                                                    getattr(user, 'display_name', None),
                                                    getattr(user, 'global_name', None),
                                                    str(user.avatar.url) if user.avatar else None,
                                                    getattr(user, 'discriminator', None),
                                                    getattr(user, 'bot', False),
                                                    getattr(user, 'system', False),
                                                    getattr(user, 'accent_color', None),
                                                    str(user.banner.url) if getattr(user, 'banner', None) else None,
                                                    user.created_at.isoformat() if hasattr(user, 'created_at') else None,
                                                    guild_join_date,
                                                    role_ids
                                                )
                                            )
                                            # Update cache
                                            self.member_update_cache[cache_key] = current_time
                                
                                await self.rate_limiter.execute(f"reaction_{message.id}_{reaction}", fetch_users)
                                
                            except Exception as e:
                                logger.warning(f"Error fetching users for reaction {reaction} on message {message.id}: {e}")
                                continue
                        
                        if reactor_ids:
                            reactors = list(reactor_ids)
                except Exception as e:
                    logger.warning(f"Could not fetch reactors for message {message.id}: {e}")
            
            # Process the message author with caching
            if hasattr(message.author, 'id'):
                cache_key = f"{message.author.id}_{message.author.name}"
                cache_time = self.member_update_cache.get(cache_key, 0)
                current_time = time.time()
                
                if current_time - cache_time > self.member_update_cache_timeout:
                    guild = self.get_guild(self.guild_id)
                    member = guild.get_member(message.author.id) if guild else None
                    role_ids = json.dumps([role.id for role in member.roles]) if member and member.roles else None
                    guild_join_date = member.joined_at.isoformat() if member and member.joined_at else None
                    
                    await self._db_operation(
                        lambda db: db.create_or_update_member(
                            message.author.id,
                            message.author.name,
                            getattr(message.author, 'display_name', None),
                            getattr(message.author, 'global_name', None),
                            str(message.author.avatar.url) if message.author.avatar else None,
                            getattr(message.author, 'discriminator', None),
                            getattr(message.author, 'bot', False),
                            getattr(message.author, 'system', False),
                            getattr(message.author, 'accent_color', None),
                            str(message.author.banner.url) if getattr(message.author, 'banner', None) else None,
                            message.author.created_at.isoformat() if hasattr(message.author, 'created_at') else None,
                            guild_join_date,
                            role_ids
                        )
                    )
                    # Update cache
                    self.member_update_cache[cache_key] = current_time

            # Get the actual channel ID and name (use parent forum for threads)
            actual_channel = message.channel
            thread_id = None
            
            if hasattr(message.channel, 'parent') and message.channel.parent:
                actual_channel = message.channel.parent
                if isinstance(message.channel, discord.Thread) and not hasattr(message.channel, 'thread_type'):
                    thread_id = message.channel.id
                elif hasattr(message.channel, 'thread_type'):
                    actual_channel = message.channel

            # Create or update the channel
            category_id = None
            if hasattr(actual_channel, 'category') and actual_channel.category:
                category_id = actual_channel.category.id

            await self._db_operation(
                lambda db: db.create_or_update_channel(
                    channel_id=actual_channel.id,
                    channel_name=actual_channel.name,
                    nsfw=getattr(actual_channel, 'nsfw', False),
                    category_id=category_id
                )
            )
            
            processed_message = {
                'message_id': message.id,
                'channel_id': actual_channel.id,
                'author_id': message.author.id,
                'content': message.content,
                'created_at': message.created_at.isoformat(),
                'attachments': [
                    {
                        'url': attachment.url,
                        'filename': attachment.filename
                    } for attachment in message.attachments
                ],
                'embeds': [embed.to_dict() for embed in message.embeds],
                'reaction_count': reaction_count,
                'reactors': reactors,
                'reference_id': message.reference.message_id if message.reference else None,
                'edited_at': message.edited_at.isoformat() if message.edited_at else None,
                'is_pinned': message.pinned,
                'thread_id': thread_id,
                'message_type': str(message.type),
                'flags': message.flags.value
            }
            
            return processed_message
            
        except Exception as e:
            logger.error(f"Error processing message {message.id}: {e}")
            return None

    async def close(self):
        """Properly close the bot and database connection."""
        try:
            # Signal database worker to stop
            self.db_queue.put(None)
            self.db_thread.join()
            await super().close()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    async def _fetch_archived_threads(self, channel: Union[discord.TextChannel, discord.ForumChannel]) -> List[discord.Thread]:
        """Fetches archived threads with retry logic for Discord API errors."""
        max_retries = 3
        delay = 5  # seconds
        for attempt in range(max_retries):
            try:
                return [t async for t in channel.archived_threads()]
            except discord.DiscordServerError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Discord API error (503) fetching archived threads for #{channel.name}. "
                        f"Retrying in {delay}s... (Attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"Failed to fetch archived threads for #{channel.name} after {max_retries} attempts. Skipping threads for this channel.",
                        exc_info=True
                    )
                    return [] # Return empty list to continue execution
        return []

    async def on_ready(self):
        """Called when bot is ready."""
        try:
            logger.info(f"Logged in as {self.user}")
            
            # Get the guild
            guild = self.get_guild(self.guild_id)
            if not guild:
                logger.error(f"Could not find guild with ID {self.guild_id}")
                await self.close()
                return
            
            # --- Collect all items to process --- 
            items_to_process = [] # List of (item_type, item_object, parent_name_if_thread)

            if self.target_channel_id:
                channel = self.get_channel(self.target_channel_id)
                if channel:
                    # If a category was provided, expand to its child channels
                    if isinstance(channel, discord.CategoryChannel):
                        logger.info(f"Target is a CategoryChannel: #{channel.name}. Expanding to child channels...")
                        try:
                            for child in channel.channels:
                                if isinstance(child, discord.TextChannel):
                                    items_to_process.append(("channel", child, None))
                                    # Include threads for each text channel
                                    archived_threads = await self._fetch_archived_threads(child)
                                    active_threads = child.threads
                                    for thread in archived_threads + active_threads:
                                        items_to_process.append(("thread", thread, child.name))
                                elif isinstance(child, discord.ForumChannel):
                                    # Include all forum threads
                                    archived_threads = await self._fetch_archived_threads(child)
                                    active_threads = child.threads
                                    for thread in archived_threads + active_threads:
                                        items_to_process.append(("forum_thread", thread, child.name))
                                else:
                                    # Skip non-text/forum channel types (voice, stage, etc.)
                                    continue
                        except Exception as e:
                            logger.error(f"Failed to expand CategoryChannel {channel.id}: {e}", exc_info=True)
                    elif isinstance(channel, discord.TextChannel):
                        items_to_process.append(("channel", channel, None))
                        # Add threads within the target text channel
                        archived_threads = await self._fetch_archived_threads(channel)
                        active_threads = channel.threads
                        for thread in archived_threads + active_threads:
                            items_to_process.append(("thread", thread, channel.name))
                    elif isinstance(channel, discord.ForumChannel):
                        # For a forum channel, pull all threads
                        archived_threads = await self._fetch_archived_threads(channel)
                        active_threads = channel.threads
                        for thread in archived_threads + active_threads:
                            items_to_process.append(("forum_thread", thread, channel.name))
                    elif isinstance(channel, discord.Thread):
                        items_to_process.append(("thread", channel, getattr(channel.parent, 'name', None)))
                    else:
                        logger.warning(f"Provided --channel {self.target_channel_id} is an unsupported type. Skipping.")
                else:
                    logger.error(f"Could not find target channel with ID {self.target_channel_id}")
            else:
                # Collect all text channels
                all_text_channels = [c for c in guild.text_channels if c.id not in self.skip_channels]
                for channel in all_text_channels:
                    items_to_process.append(("channel", channel, None))
                    # Collect threads within text channels
                    archived_threads = await self._fetch_archived_threads(channel)
                    active_threads = channel.threads
                    for thread in archived_threads + active_threads:
                        items_to_process.append(("thread", thread, channel.name))
                
                # Collect all forum threads
                all_forums = [f for f in guild.channels if isinstance(f, discord.ForumChannel) and f.id not in self.skip_channels]
                for forum in all_forums:
                    archived_threads = await self._fetch_archived_threads(forum)
                    active_threads = forum.threads
                    for thread in archived_threads + active_threads:
                         items_to_process.append(("forum_thread", thread, forum.name))

            total_items = len(items_to_process)
            logger.info(f"Collected {total_items} total channels/threads to process.")

            # --- Process collected items --- 
            for index, (item_type, item, parent_name) in enumerate(items_to_process):
                item_index = index + 1 # 1-based index for logging
                
                if item_type == "channel":
                    log_prefix = f"Processing Channel {item_index}/{total_items}:"
                    logger.info(f"{log_prefix} #{item.name}")
                elif item_type == "thread":
                     log_prefix = f"Processing Thread {item_index}/{total_items} in #{parent_name}:"
                     logger.info(f"{log_prefix} #{item.name}")
                elif item_type == "forum_thread":
                     log_prefix = f"Processing Forum Thread {item_index}/{total_items} in forum #{parent_name}:"
                     logger.info(f"{log_prefix} #{item.name}")
                else:
                    logger.warning(f"Skipping unknown item type at index {item_index}")
                    continue # Skip unknown types

                # Call archive_channel for the item's ID
                await self.archive_channel(item.id) 
                # Log running total after the item is done
                logger.info(f"📊 Running Total New Messages Archived: {self.total_messages_archived} (Storage: {os.getenv('STORAGE_BACKEND', 'sqlite')})") 
            
            logger.info("Archiving complete, shutting down bot")
            logger.info(f"🎉 FINAL TOTAL: {self.total_messages_archived} new messages archived to {os.getenv('STORAGE_BACKEND', 'sqlite')}") 
            await self.close()

        except Exception as e:
            logger.error(f"Error in on_ready: {e}", exc_info=True) # Add traceback
            await self.close()

    async def _wait_for_rate_limit(self):
        """Handles rate limiting for Discord API calls."""
        now = datetime.now()
        self.api_call_count += 1
        
        time_since_last = (now - self.last_api_call).total_seconds()
        
        # Basic throttling - ensure at least 0.1s between calls
        if time_since_last < 0.1:
            await asyncio.sleep(0.1 - time_since_last)
            logger.debug(f"Basic throttle - {time_since_last:.3f}s since last call")
        
        # Only enforce rate limits if we're actually approaching them
        if self.api_call_count >= 45:  # Conservative buffer before hitting 50
            wait_time = 1.0  # Start with a 1s pause
            logger.debug(f"Rate limit approaching - Current count: {self.api_call_count}, Remaining: {self.rate_limit_remaining}")
            await asyncio.sleep(wait_time)
            self.api_call_count = 0
            self.rate_limit_reset = datetime.now() + timedelta(seconds=60)
            self.rate_limit_remaining = 50
            logger.debug(f"Rate limit reset - New remaining: {self.rate_limit_remaining}, Next reset: {self.rate_limit_reset}")
        
        self.last_api_call = now

    async def archive_channel(self, channel_id: int) -> None:
        """Archive all messages from a channel."""
        channel_start_time = datetime.now(timezone.utc)
        try:
            # Skip welcome channel
            if channel_id in self.skip_channels:
                logger.info(f"Skipping welcome channel {channel_id}")
                return
            
            channel = self.get_channel(channel_id)
            if not channel:
                logger.error(f"Could not find channel {channel_id}")
                return
            
            # Get the actual channel (parent forum if this is a thread)
            actual_channel = channel
            if hasattr(channel, 'parent') and channel.parent:
                actual_channel = channel.parent
                logger.debug(f"Using parent forum #{actual_channel.name} (ID: {actual_channel.id}) for thread #{channel.name}")
                # Update channel_id to use parent forum's ID
                channel_id = actual_channel.id
            
            logger.info(f"Starting archive of #{channel.name} at {channel_start_time}")
            
            # --- Logic based on whether specific dates are provided ---
            if self.start_date and self.end_date:
                # --- Archive Specific Date Range ---
                start_log_msg = f"Starting date-range archive for #{channel.name} ({self.start_date.date()} to { (self.end_date - timedelta(days=1)).date() })"
                if self.total_days_in_range > 0:
                    start_log_msg += f" - Total duration: {self.total_days_in_range} days"
                logger.info(start_log_msg)
                
                message_counter = 0
                new_message_count = 0
                current_batch = []
                last_processed_message_date = None
                last_progress_log_time = time.time()
                
                # Variables for daily timing and ETR
                current_processing_day_date = None
                current_day_start_time = None
                processed_days_count = 0
                total_processing_time_seconds = 0.0
                last_day_duration_str = "N/A"
                etr_str = "Calculating..."
                messages_processed_this_day = 0 # Initialize daily counter

                async for message in channel.history(limit=None, after=self.start_date, before=self.end_date, oldest_first=self.oldest_first):
                    # Skip messages from the bot
                    if message.author.id == self.bot_user_id:
                        continue

                    message_counter += 1
                    msg_date = message.created_at.date()
                    current_time_epoch = time.time()

                    # --- Daily Duration Calculation ---
                    if msg_date != current_processing_day_date:
                        if current_processing_day_date is not None and current_day_start_time is not None:
                            # Finish timing the previous day
                            day_duration_secs = current_time_epoch - current_day_start_time
                            processed_days_count += 1
                            total_processing_time_seconds += day_duration_secs
                            last_day_duration_str = f"{day_duration_secs:.2f}s"
                            # MODIFIED: Add message count to completed log
                            logger.info(f"Completed Day {processed_days_count}/{self.total_days_in_range} ({current_processing_day_date}) for #{channel.name} in {last_day_duration_str} - {messages_processed_this_day} messages processed")
                            messages_processed_this_day = 0 # Reset counter for new day
                        
                        # Start timing the new day
                        current_processing_day_date = msg_date
                        current_day_start_time = current_time_epoch
                        # MODIFIED: Adjust starting log format (omitting count)
                        logger.info(f"Starting Day {processed_days_count + 1}/{self.total_days_in_range} ({current_processing_day_date}) for #{channel.name}...")
                    
                    messages_processed_this_day += 1 # Increment daily counter

                    last_processed_message_date = message.created_at # Keep track of the absolute latest message time

                    # --- Progress Logging & ETR --- 
                    # Log progress roughly every 30 seconds 
                    if (current_time_epoch - last_progress_log_time > 30):
                        if last_processed_message_date and self.total_days_in_range > 0:
                            # Calculate how many whole days have been covered so far
                            if self.oldest_first:
                                # Processing oldest → newest: progress grows as we move forward in time
                                elapsed_total_days = (last_processed_message_date - self.start_date).days + 1
                            else:
                                # Processing newest → oldest: progress grows as we move *back* in time
                                # so measure distance from the *end* of the range
                                elapsed_total_days = (self.end_date - last_processed_message_date).days
                                if elapsed_total_days == 0:
                                    elapsed_total_days = 1  # Ensure we never report 0%

                            elapsed_total_days = max(1, min(elapsed_total_days, self.total_days_in_range))
                            percentage = (elapsed_total_days / self.total_days_in_range) * 100
                            
                            # Calculate ETR based on completed days
                            if processed_days_count > 0:
                                avg_time_per_day = total_processing_time_seconds / processed_days_count
                                remaining_days = self.total_days_in_range - processed_days_count # Use count of fully completed days
                                etr_seconds = avg_time_per_day * remaining_days
                                etr_str = str(timedelta(seconds=int(etr_seconds)))
                            else:
                                etr_str = "Calculating..." # Not enough data yet

                            logger.info(f"Progress #{channel.name}: Overall {percentage:.1f}% ({elapsed_total_days}/{self.total_days_in_range} days). Last Day ({current_processing_day_date}) took {last_day_duration_str}. ETR: {etr_str}")
                            last_progress_log_time = current_time_epoch
                    # --- End Progress Logging ---

                    process_this_message = False
                    if self.in_depth or self.fetch_reactions:
                        process_this_message = True
                    else:
                        message_exists = await self._db_operation(
                            lambda db: db.message_exists(message.id)
                        )
                        if not message_exists:
                            process_this_message = True

                    if process_this_message:
                        current_batch.append(message)

                    # Store batch when it reaches the threshold
                    if len(current_batch) >= 100:
                        try:
                            processed_messages = []
                            for msg in current_batch:
                                processed_msg = await self._process_message(msg, channel_id)
                                if processed_msg:
                                    processed_messages.append(processed_msg)
                            if processed_messages:
                                pre_existing_ids = set()
                                if not self.in_depth:
                                    existing_in_db = await self._db_operation(
                                        lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                    )
                                    pre_existing_ids = {msg['message_id'] for msg in existing_in_db}
                                new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing_ids]
                                new_message_count += len(new_messages)
                                await self._db_operation(
                                    lambda db: db.store_messages(processed_messages)
                                )
                            current_batch = []
                            await asyncio.sleep(0.1)
                        except Exception as e:
                            logger.error(f"Failed to store batch during date range archive: {e}")

                # --- After the loop --- 
                # Log duration for the final day if it was started
                if current_processing_day_date is not None and current_day_start_time is not None:
                    final_day_duration_secs = time.time() - current_day_start_time
                    # Check if this day was already counted (might happen if loop ends exactly on day boundary)
                    if processed_days_count < self.total_days_in_range:
                         processed_days_count += 1 
                    # MODIFIED: Add message count to final completed log
                    logger.info(f"Completed Final Day {processed_days_count}/{self.total_days_in_range} ({current_processing_day_date}) for #{channel.name} in {final_day_duration_secs:.2f}s - {messages_processed_this_day} messages processed")

                # Process any remaining messages in the final batch
                if current_batch:
                    try:
                        processed_messages = []
                        for msg in current_batch:
                            processed_msg = await self._process_message(msg, channel_id)
                            if processed_msg:
                                processed_messages.append(processed_msg)
                        if processed_messages:
                            pre_existing_ids = set()
                            if not self.in_depth:
                                existing_in_db = await self._db_operation(
                                    lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                )
                                pre_existing_ids = {msg['message_id'] for msg in existing_in_db}
                            new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing_ids]
                            new_message_count += len(new_messages)
                            await self._db_operation(
                                lambda db: db.store_messages(processed_messages)
                            )
                    except Exception as e:
                        logger.error(f"Failed to store final date range batch: {e}")

                logger.info(f"✅ Date range archive complete for #{channel.name} - Processed {message_counter} messages, saved {new_message_count} new to {os.getenv('STORAGE_BACKEND', 'sqlite')}")
                # Log final 100% progress
                if self.total_days_in_range > 0:
                     logger.info(f"Progress for #{channel.name}: Day {self.total_days_in_range}/{self.total_days_in_range} (100.0%) - Completed.")

            else:
                # --- Original Archive Logic (using --days or DB range checks) ---
                logger.info(f"Starting incremental/full archive for #{channel.name} at {channel_start_time}")
                # Calculate the cutoff date if days limit is set
                cutoff_date = None
                if self.days_limit:
                    # Make sure to create timezone-aware datetime
                    cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.days_limit)
                    logger.debug(f"Will only fetch messages after {cutoff_date}")
                
                # Initialize dates as None
                earliest_date = None
                latest_date = None
                
                try:
                    # Get date range of archived messages
                    earliest_date, latest_date = await self._db_operation(
                        lambda db: db.get_message_date_range(channel_id)
                    )
                    # Make sure dates are timezone-aware
                    if earliest_date:
                        earliest_date = earliest_date.replace(tzinfo=timezone.utc)
                        logger.info(f"Earliest message in DB for #{channel.name}: {earliest_date}")
                    if latest_date:
                        latest_date = latest_date.replace(tzinfo=timezone.utc)
                        logger.info(f"Latest message in DB for #{channel.name}: {latest_date}")
                except Exception as e:
                    logger.warning(f"Could not get message date range, will fetch all messages: {e}")
                
                message_counter = 0
                new_message_count = 0
                batch = []

                # If no archived messages exist or we're in in-depth mode, get all messages in the time range
                if not earliest_date or not latest_date or self.in_depth:
                    if self.in_depth:
                        logger.debug(f"In-depth mode: Re-checking all messages in time range for #{channel.name}")
                    else:
                        logger.info(f"No existing archives found for #{channel.name}. Getting all messages...")
                    logger.debug(f"Starting message fetch for #{channel.name} from {'oldest to newest' if self.oldest_first else 'newest to oldest'}...")
                    try:
                        # We'll paginate through messages using before/after
                        last_message = None
                        while True:
                            history_kwargs = {
                                'limit': None,  # No limit, we'll control the flow ourselves
                                'oldest_first': self.oldest_first,
                                'before': last_message.created_at if last_message else None,
                                'after': cutoff_date if cutoff_date else None
                            }
                            
                            logger.debug(f"Fetching messages for #{channel.name} with kwargs: {history_kwargs}")
                            current_batch = []
                            
                            try:
                                got_messages = False
                                async for message in channel.history(**{k: v for k, v in history_kwargs.items() if v is not None}):
                                    got_messages = True
                                    last_message = message
                                    
                                    # Process message
                                    message_counter += 1
                                    if message_counter % 25 == 0:
                                        logger.debug(f"Fetched {message_counter} messages so far from #{channel.name}, last message from {message.created_at}")
                                    
                                    try:
                                        # Skip messages from the bot
                                        if message.author.id == self.bot_user_id:
                                            continue
                                        
                                        # In in-depth mode, always process the message
                                        # In normal mode, only process if not already in DB
                                        message_exists = await self._db_operation(
                                            lambda db: db.message_exists(message.id)
                                        )
                                        if self.in_depth or self.fetch_reactions or not message_exists:
                                            current_batch.append(message)
                                        
                                        # Store batch when it reaches the threshold
                                        if len(current_batch) >= 100:
                                            try:
                                                processed_messages = []
                                                for msg in current_batch:
                                                    processed_msg = await self._process_message(msg, channel_id)
                                                    if processed_msg:
                                                        processed_messages.append(processed_msg)
                                                
                                                if processed_messages:
                                                    # Only increment counter for messages that didn't exist before
                                                    pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                                        lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                                    ))
                                                    new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                                    new_message_count += len(new_messages)
                                                    
                                                    logger.info(f"Storing batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new, {len(pre_existing)} existing)")
                                                    await self._db_operation(
                                                        lambda db: db.store_messages(processed_messages)
                                                    )
                                                
                                                current_batch = []
                                                await asyncio.sleep(0.1)
                                            except Exception as e:
                                                logger.error(f"Failed to store batch: {e}")
                                                logger.error(f"Error details: {str(e)}")
                                                
                                    except Exception as e:
                                        logger.error(f"Error processing message {message.id}: {e}")
                                        logger.error(f"Error details: {str(e)}")
                                        continue
                                    
                            except discord.Forbidden:
                                logger.warning(f"Missing permissions to read messages in #{channel.name}")
                                break
                            except Exception as e:
                                logger.error(f"Error fetching messages: {e}")
                                break
                            
                            # Process any remaining messages in the current batch
                            if current_batch:
                                try:
                                    processed_messages = []
                                    for msg in current_batch:
                                        processed_msg = await self._process_message(msg, channel_id)
                                        if processed_msg:
                                            processed_messages.append(processed_msg)
                                    
                                    if processed_messages:
                                        # Only increment counter for messages that didn't exist before
                                        pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                            lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                        ))
                                        new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                        new_message_count += len(new_messages)
                                        
                                        logger.debug(f"Storing final batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new)")
                                        await self._db_operation(
                                            lambda db: db.store_messages(processed_messages)
                                        )
                                except Exception as e:
                                    logger.error(f"Failed to store final batch: {e}")
                                    logger.error(f"Error details: {str(e)}")
                            
                            # If we didn't get any messages in this fetch, break the loop
                            if not got_messages:
                                logger.info(f"No more messages found in #{channel.name} for the current time range")
                                break
                                
                            await asyncio.sleep(0.1)
                        
                        logger.info(f"Finished initial fetch for #{channel.name}: {message_counter} messages fetched, last message from {last_message.created_at if last_message else 'N/A'}")
                    except Exception as e:
                        logger.error(f"Error fetching message history: {e}")
                        logger.error(f"Error details: {str(e)}")
                        raise

                # Still check before earliest and after latest, respecting days limit
                if latest_date:
                    logger.info(f"Searching for newer messages in #{channel.name} (after {latest_date})..." )
                    current_batch = []
                    messages_found = 0
                    async for message in channel.history(limit=None, after=latest_date, oldest_first=self.oldest_first):
                        messages_found += 1
                        if messages_found % 100 == 0:
                            logger.debug(f"Found {messages_found} newer messages in #{channel.name}")
                        if cutoff_date and message.created_at < cutoff_date:
                            logger.debug(f"Reached cutoff date {cutoff_date}, stopping newer message search")
                            break
                        
                        # Skip messages from the bot
                        if message.author.id == self.bot_user_id:
                            continue
                            
                        current_batch.append(message)
                        message_counter += 1
                        
                        # Store batch when it reaches the threshold
                        if len(current_batch) >= 100:
                            try:
                                processed_messages = []
                                for msg in current_batch:
                                    processed_msg = await self._process_message(msg, channel_id)
                                    if processed_msg:
                                        processed_messages.append(processed_msg)
                                
                                if processed_messages:
                                    # Only increment counter for messages that didn't exist before
                                    pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                        lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                    ))
                                    new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                    new_message_count += len(new_messages)
                                    
                                    logger.info(f"Storing batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new, {len(pre_existing)} existing)")
                                    await self._db_operation(
                                        lambda db: db.store_messages(processed_messages)
                                    )
                                current_batch = []
                                await asyncio.sleep(0.1)
                            except Exception as e:
                                logger.error(f"Failed to store batch: {e}")
                                logger.error(f"Error details: {str(e)}")
                    
                    # Process any remaining messages
                    if current_batch:
                        try:
                            processed_messages = []
                            for msg in current_batch:
                                processed_msg = await self._process_message(msg, channel_id)
                                if processed_msg:
                                    processed_messages.append(processed_msg)
                            
                            if processed_messages:
                                # Only increment counter for messages that didn't exist before
                                pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                    lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                ))
                                new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                new_message_count += len(new_messages)
                                
                                logger.info(f"Storing batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new, {len(pre_existing)} existing)")
                                await self._db_operation(
                                    lambda db: db.store_messages(processed_messages)
                                )
                        except Exception as e:
                            logger.error(f"Failed to store batch: {e}")
                            logger.error(f"Error details: {str(e)}")
                
                # Only search for older messages if we're not using --days flag
                if not self.days_limit and earliest_date:
                    logger.info(f"Searching for older messages in #{channel.name} (before {earliest_date})..." )
                    current_batch = []
                    messages_found = 0
                    async for message in channel.history(limit=None, before=earliest_date, oldest_first=self.oldest_first):
                        messages_found += 1
                        if messages_found % 100 == 0:
                            logger.debug(f"Found {messages_found} older messages in #{channel.name}")
                        if cutoff_date and message.created_at < cutoff_date:
                            continue
                            
                        # Skip messages from the bot
                        if message.author.id == self.bot_user_id:
                            continue
                            
                        current_batch.append(message)
                        message_counter += 1
                        
                        # Store batch when it reaches the threshold
                        if len(current_batch) >= 100:
                            try:
                                processed_messages = []
                                for msg in current_batch:
                                    processed_msg = await self._process_message(msg, channel_id)
                                    if processed_msg:
                                        processed_messages.append(processed_msg)
                                
                                if processed_messages:
                                    # Only increment counter for messages that didn't exist before
                                    pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                        lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                    ))
                                    new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                    new_message_count += len(new_messages)
                                    
                                    logger.info(f"Storing batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new, {len(pre_existing)} existing)")
                                    await self._db_operation(
                                        lambda db: db.store_messages(processed_messages)
                                    )
                                current_batch = []
                                await asyncio.sleep(0.1)
                            except Exception as e:
                                logger.error(f"Failed to store batch: {e}")
                                logger.error(f"Error details: {str(e)}")
                    
                    # Process any remaining messages
                    if current_batch:
                        try:
                            processed_messages = []
                            for msg in current_batch:
                                processed_msg = await self._process_message(msg, channel_id)
                                if processed_msg:
                                    processed_messages.append(processed_msg)
                            
                            if processed_messages:
                                # Only increment counter for messages that didn't exist before
                                pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                    lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                ))
                                new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                new_message_count += len(new_messages)
                                
                                logger.debug(f"Storing batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new)")
                                await self._db_operation(
                                    lambda db: db.store_messages(processed_messages)
                                )
                        except Exception as e:
                            logger.error(f"Failed to store batch: {e}")
                            logger.error(f"Error details: {str(e)}")

                # Get all message dates to check for gaps
                message_dates = await self._db_operation(
                    lambda db: db.get_message_dates(channel_id)
                )
                if message_dates:
                    # Filter dates based on cutoff if set
                    if cutoff_date:
                        message_dates = [d for d in message_dates if to_aware_utc(d) >= cutoff_date]
                    
                    # Sort dates based on order setting
                    message_dates.sort(reverse=not self.oldest_first)
                    gaps = []
                    for i in range(len(message_dates) - 1):
                        current = to_aware_utc(message_dates[i])
                        next_date = to_aware_utc(message_dates[i + 1])
                        # Compare dates based on order
                        date_diff = (next_date - current).days if self.oldest_first else (current - next_date).days
                        if date_diff > 7:
                            gaps.append((current, next_date) if self.oldest_first else (next_date, current))
                    
                    if gaps:
                        logger.info(f"Found {len(gaps)} gaps (>1 week) in message history for #{channel.name}")
                        for start, end in gaps:
                            gap_message_count = 0
                            current_batch = []
                            logger.info(f"Searching for messages in #{channel.name} between {start} and {end} (gap of {abs((end - start).days)} days)")
                            async for message in channel.history(limit=None, after=start, before=end, oldest_first=self.oldest_first):
                                # Skip messages from the bot
                                if message.author.id == self.bot_user_id:
                                    continue
                                    
                                current_batch.append(message)
                                gap_message_count += 1
                                
                                # Store batch when it reaches the threshold
                                if len(current_batch) >= 100:
                                    try:
                                        processed_messages = []
                                        for msg in current_batch:
                                            processed_msg = await self._process_message(msg, channel_id)
                                            if processed_msg:
                                                processed_messages.append(processed_msg)
                                        
                                        if processed_messages:
                                            # Only increment counter for messages that didn't exist before
                                            pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                                lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                            ))
                                            new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                            new_message_count += len(new_messages)
                                            
                                            logger.debug(f"Storing batch of {len(processed_messages)} messages from gap in #{channel.name} ({len(new_messages)} new)")
                                            await self._db_operation(
                                                lambda db: db.store_messages(processed_messages)
                                            )
                                            if gap_message_count % 100 == 0:
                                                logger.debug(f"Found {gap_message_count} messages in current gap for #{channel.name}")
                                        
                                        current_batch = []
                                        await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.error(f"Failed to store batch: {e}")
                                        logger.error(f"Error details: {str(e)}")
                            
                            # Process any remaining messages from the gap
                            if current_batch:
                                try:
                                    processed_messages = []
                                    for msg in current_batch:
                                        processed_msg = await self._process_message(msg, channel_id)
                                        if processed_msg:
                                            processed_messages.append(processed_msg)
                                    
                                    if processed_messages:
                                        # Only increment counter for messages that didn't exist before
                                        pre_existing = set(msg['message_id'] for msg in await self._db_operation(
                                            lambda db: db.get_messages_by_ids([msg['message_id'] for msg in processed_messages])
                                        ))
                                        new_messages = [msg for msg in processed_messages if msg['message_id'] not in pre_existing]
                                        new_message_count += len(new_messages)
                                        
                                        logger.debug(f"Storing final gap batch of {len(processed_messages)} messages from #{channel.name} ({len(new_messages)} new)")
                                        await self._db_operation(
                                            lambda db: db.store_messages(processed_messages)
                                        )
                                except Exception as e:
                                    logger.error(f"Failed to store batch: {e}")
                                    logger.error(f"Error details: {str(e)}")
                            
                            logger.info(f"Finished gap search in #{channel.name}, found {gap_message_count} messages")
                
                logger.info(f"Found {new_message_count} new messages to archive in #{channel.name}")
                logger.info(f"✅ Archive complete for #{channel.name} - {new_message_count} new messages saved to {os.getenv('STORAGE_BACKEND', 'sqlite')}")
                self.total_messages_archived += new_message_count
                
                channel_duration = (datetime.now(timezone.utc) - channel_start_time).total_seconds()
                logger.info(f"Finished archive of #{channel.name} in {channel_duration:.2f}s")
                
        except discord.HTTPException as e:
            if e.code == 429:  # Rate limit error
                logger.warning(f"Hit rate limit while processing #{channel.name}: {e}")
                retry_after = e.retry_after if hasattr(e, 'retry_after') else 5
                logger.info(f"Waiting {retry_after}s before continuing")
                await asyncio.sleep(retry_after)
            else:
                logger.error(f"HTTP error in channel {channel.name}: {e}")
        except Exception as e:
            logger.error(f"Error archiving channel {channel.name}: {e}")
        finally:
            # Don't close the connection here as it's reused across channels
            pass

    def _ensure_db_connection(self):
        """Ensure database connection is alive and reconnect if needed."""
        try:
            # Test the connection
            if not self.db or not self.db.conn:
                logger.info("Database connection lost, reconnecting...")
                self.db = DatabaseHandler(self.db_path)
                storage_backend = os.getenv('STORAGE_BACKEND', 'both')
                if storage_backend in ['sqlite', 'both']:
                    self.db._init_db()
                logger.info("Successfully reconnected to database")
            else:
                # Test if connection is actually working
                self.db.cursor.execute("SELECT 1")
        except Exception as e:
            logger.warning(f"Database connection test failed, reconnecting: {e}")
            try:
                if self.db:
                    self.db.close()
                self.db = DatabaseHandler(self.db_path)
                storage_backend = os.getenv('STORAGE_BACKEND', 'both')
                if storage_backend in ['sqlite', 'both']:
                    self.db._init_db()
                logger.info("Successfully reconnected to database")
            except Exception as e:
                logger.error(f"Failed to reconnect to database: {e}")
                raise

def main():
    """Main entry point."""
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
    parser.add_argument('--fetch-reactions', action='store_true',
                      help='Fetch reactions for all messages in range, not just new ones')
    parser.add_argument('--storage-backend', type=str, choices=['sqlite', 'supabase', 'both'],
                      default='both',
                      help='Storage backend: sqlite (local only), supabase (cloud only), or both (default: both)')
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
    
    # Set storage backend (defaults to supabase)
    os.environ['STORAGE_BACKEND'] = args.storage_backend
    logger.info(f"Storage backend set to: {args.storage_backend}")
    
    bot = None
    try:
        # Create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        bot = MessageArchiver(dev_mode=args.dev, order=args.order, days=args.days, 
                            batch_size=args.batch_size, in_depth=args.in_depth,
                            channel_id=args.channel, fetch_reactions=args.fetch_reactions,
                            start_date_str=args.start_date, end_date_str=args.end_date) # Pass date strings
        
        # Start the bot and keep it running until archiving is complete
        async def runner():
            await bot.start(os.getenv('DISCORD_BOT_TOKEN'))
            # Wait for the bot to be ready and complete archiving
            while not bot.is_closed():
                await asyncio.sleep(1)
        
        # Run the bot until it completes
        loop.run_until_complete(runner())
        
    except discord.LoginFailure:
        logger.error("Failed to login. Please check your Discord token.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        # Print traceback for debugging if needed
        import traceback
        traceback.print_exc()
    finally:
        # Ensure everything is cleaned up properly
        if bot:
            if not loop.is_closed():
                try:
                    loop.run_until_complete(bot.close())
                except Exception as e:
                    logger.error(f"Error closing bot: {e}")

        # Clean up the event loop
        try:
            if not loop.is_closed():
                loop.run_until_complete(loop.shutdown_asyncgens())
                remaining_tasks = asyncio.all_tasks(loop)
                if remaining_tasks:
                    # Give tasks a moment to finish
                    loop.run_until_complete(asyncio.wait(remaining_tasks, timeout=5.0))
                    # Cancel any tasks that are still running
                    for task in remaining_tasks:
                        if not task.done():
                            task.cancel()
                    # Wait for cancellations to take effect
                    loop.run_until_complete(asyncio.gather(*remaining_tasks, return_exceptions=True))

        except Exception as e:
            logger.error(f"Error during loop cleanup: {e}")
        finally:
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.close()
            logger.info("Event loop closed.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot shutdown initiated by user")
    except Exception as e:
        logger.error(f"Unexpected error in __main__: {e}")
        import traceback
        traceback.print_exc() 