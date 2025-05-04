# Standard library imports
import asyncio

import io
import json
import logging
import os
import re

import traceback
from datetime import datetime, timedelta
from typing import List, Tuple, Set, Dict, Optional, Any, Union
import sqlite3

import time

# Third-party imports
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Local imports
from src.common.db_handler import DatabaseHandler
from src.common.errors import *
from src.common.error_handler import ErrorHandler, handle_errors
from src.common.rate_limiter import RateLimiter
from src.common.log_handler import LogHandler
# from src.common.base_bot import BaseDiscordBot 

# Import the new summarizer that handles queries/Claude calls
from src.features.summarising.subfeatures.news_summary import NewsSummarizer
from src.features.summarising.subfeatures.top_generations import TopGenerations
from src.features.summarising.subfeatures.top_art_sharing import TopArtSharing

# --- Import Sharer ---
from src.features.sharing.sharer import Sharer

# Optional imports for media processing
try:
    from PIL import Image
    import moviepy.editor as mp
    MEDIA_PROCESSING_AVAILABLE = True
except ImportError:
    MEDIA_PROCESSING_AVAILABLE = False

################################################################################
# You may already have a scheduling function somewhere, but here is a simple stub:
################################################################################
async def schedule_daily_summary(bot):
    """
    Example stub for daily scheduled runs. 
    Adjust logic and scheduling library as appropriate to your environment.
    """
    first_run = True
    while not bot._shutdown_flag:
        now_utc = datetime.utcnow()
        # Suppose we run at 10:00 UTC daily
        run_time = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
        if run_time < now_utc:
            run_time += timedelta(days=1)
        sleep_duration = (run_time - now_utc).total_seconds()
        await asyncio.sleep(sleep_duration)

        if bot._shutdown_flag:
            break

        if first_run:
            if bot.summary_now:
                bot.logger.info("'--summary-now' flag detected. Running initial summary now.")
            else:
                bot.logger.info("Skipping initial summary run because '--summary-now' flag was not provided.")
                first_run = False
                continue
            first_run = False

        try:
            await bot.generate_summary()
        except Exception as e:
            bot.logger.error(f"Scheduled summary run failed: {e}")

        # Sleep 24h until next scheduled run:
        await asyncio.sleep(86400)

################################################################################

class ChannelSummarizerError(Exception):
    """Base exception class for ChannelSummarizer"""
    pass

class APIError(ChannelSummarizerError):
    """Raised when API calls fail"""
    pass

class DiscordError(ChannelSummarizerError):
    """Raised when Discord operations fail"""
    pass

class SummaryError(ChannelSummarizerError):
    """Raised when summary generation fails"""
    pass

class Attachment:
    def __init__(self, filename: str, data: bytes, content_type: str, reaction_count: int, username: str, content: str = ""):
        self.filename = filename
        self.data = data
        self.content_type = content_type
        self.reaction_count = reaction_count
        self.username = username
        self.content = content

class AttachmentHandler:
    def __init__(self, logger: logging.Logger, max_size: int = 25 * 1024 * 1024):
        self.max_size = max_size
        self.attachment_cache: Dict[str, Dict[str, Any]] = {}
        self.logger = logger
        
    def clear_cache(self):
        """Clear the attachment cache"""
        self.attachment_cache.clear()
        
    async def process_attachment(self, attachment: discord.Attachment, message: discord.Message, session: aiohttp.ClientSession) -> Optional[Attachment]:
        """Process a single attachment with size and type validation."""
        try:
            cache_key = f"{message.channel.id}:{message.id}"

            async with session.get(attachment.url, timeout=300) as response:
                if response.status != 200:
                    raise APIError(f"Failed to download attachment: HTTP {response.status}")

                file_data = await response.read()
                if len(file_data) > self.max_size:
                    self.logger.warning(f"Skipping large file {attachment.filename} ({len(file_data)/1024/1024:.2f}MB)")
                    return None

                total_reactions = sum(reaction.count for reaction in message.reactions) if message.reactions else 0
                
                # Get guild display name (nickname) if available, otherwise use display name
                author_name = message.author.display_name
                if hasattr(message.author, 'guild'):
                    member = message.guild.get_member(message.author.id)
                    if member:
                        author_name = member.nick or member.display_name

                processed_attachment = Attachment(
                    filename=attachment.filename,
                    data=file_data,
                    content_type=attachment.content_type,
                    reaction_count=total_reactions,
                    username=author_name,  # Use the determined name
                    content=message.content
                )

                # Ensure the cache key structure is consistent
                if cache_key not in self.attachment_cache:
                    self.attachment_cache[cache_key] = {
                        'attachments': [],
                        'reaction_count': total_reactions,
                        'username': author_name,
                        'channel_id': str(message.channel.id)
                    }
                self.attachment_cache[cache_key]['attachments'].append(processed_attachment)

                return processed_attachment

        except Exception as e:
            self.logger.error(f"Failed to process attachment {attachment.filename}: {e}")
            self.logger.debug(traceback.format_exc())
            return None

    async def prepare_files(self, message_ids: List[str], channel_id: str) -> List[Tuple[discord.File, int, str, str]]:
        """Prepare Discord files from cached attachments."""
        files = []
        for message_id in message_ids:
            # Use composite key to look up attachments
            cache_key = f"{channel_id}:{message_id}"
            if cache_key in self.attachment_cache:
                for attachment in self.attachment_cache[cache_key]['attachments']:
                    try:
                        file = discord.File(
                            io.BytesIO(attachment.data),
                            filename=attachment.filename,
                            description=f"From message ID: {message_id} (🔥 {attachment.reaction_count} reactions)"
                        )
                        files.append((
                            file,
                            attachment.reaction_count,
                            message_id,
                            attachment.username
                        ))
                    except Exception as e:
                        self.logger.error(f"Failed to prepare file {attachment.filename}: {e}")
                        continue

        return sorted(files, key=lambda x: x[1], reverse=True)[:10]

    def get_all_files_sorted(self) -> List[Attachment]:
        """
        Retrieve all attachments sorted by reaction count in descending order.
        """
        all_attachments = []
        for channel_data in self.attachment_cache.values():
            all_attachments.extend(channel_data['attachments'])
        
        # Sort attachments by reaction_count in descending order
        sorted_attachments = sorted(all_attachments, key=lambda x: x.reaction_count, reverse=True)
        return sorted_attachments

class MessageFormatter:
    @staticmethod
    def format_usernames(usernames: List[str]) -> str:
        """Format a list of usernames with proper grammar and bold formatting."""
        unique_usernames = list(dict.fromkeys(usernames))
        if not unique_usernames:
            return ""
        
        formatted_usernames = []
        for username in unique_usernames:
            if not username.startswith('**'):
                username = f"**{username}**"
            formatted_usernames.append(username)
        
        if len(formatted_usernames) == 1:
            return formatted_usernames[0]
        
        return f"{', '.join(formatted_usernames[:-1])} and {formatted_usernames[-1]}"

    @staticmethod
    def chunk_content(content: str, max_length: int = 1900) -> List[Tuple[str, Set[str]]]:
        """Split content into chunks while preserving message links."""
        chunks = []
        current_chunk = ""
        current_chunk_links = set()

        for line in content.split('\n'):
            message_links = set(re.findall(r'https://discord\.com/channels/\d+/\d+/(\d+)', line))
            
            # Start new chunk if we hit an emoji or length limit
            if (any(line.startswith(emoji) for emoji in ['🎥', '💻', '🎬', '🤖', '📱', '🔧', '🎨', '📊']) and 
                current_chunk):
                if current_chunk:
                    chunks.append((current_chunk, current_chunk_links))
                current_chunk = ""
                current_chunk_links = set()
                current_chunk += '\n---\n\n'

            if len(current_chunk) + len(line) + 2 <= max_length:
                current_chunk += line + '\n'
                current_chunk_links.update(message_links)
            else:
                if current_chunk:
                    chunks.append((current_chunk, current_chunk_links))
                current_chunk = line + '\n'
                current_chunk_links = set(message_links)

        if current_chunk:
            chunks.append((current_chunk, current_chunk_links))

        return chunks

    def chunk_long_content(self, content: str, max_length: int = 1900) -> List[str]:
        """Split content into chunks that respect Discord's length limits."""
        chunks = []
        current_chunk = ""
        
        lines = content.split('\n')
        
        for line in lines:
            if len(current_chunk) + len(line) + 1 <= max_length:
                current_chunk += line + '\n'
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

class ChannelSummarizer:
    # Define constants if they are not already defined
    RATE_LIMIT_CALLS = 10
    RATE_LIMIT_PERIOD = 60  # seconds
    MAX_RETRIES = 3
    INITIAL_RETRY_DELAY = 5  # seconds
    MAX_RETRY_WAIT = 300 # 5 minutes
    MAX_MESSAGE_LENGTH = 1990
    DEFAULT_TIME_DELTA_HOURS = 24
    MAX_ATTACHMENTS_PER_MESSAGE = 10
    MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024 # 25 MB

    # MODIFIED __init__ to accept bot
    def __init__(self, bot: commands.Bot, logger=None, dev_mode=False, command_prefix="!", sharer_instance=None): 
        self.bot = bot # Store the passed bot instance
        
        # Initialize logger (ensure setup_logger exists or handle here)
        self.logger = logger or logging.getLogger('DiscordBot') # Use provided or default
        if logger is None:
             # Minimal logger setup if none provided - adjust as needed
             logging.basicConfig(level=logging.INFO) 
             self.logger.warning("No logger provided to ChannelSummarizer, using basic config.")

        # Set dev_mode - assuming property exists
        self.dev_mode = dev_mode 

        self.command_prefix = command_prefix
        
        # Store Sharer Instance
        if sharer_instance is None:
            self.logger.critical("Sharer instance was not provided to ChannelSummarizer.")
            # Decide whether to raise error or just log
            # raise ValueError("Sharer instance is required") 
        self.sharer = sharer_instance # Use self.sharer consistently

        # Initialize DB Handler
        try:
             self.db_handler = DatabaseHandler(dev_mode=self.dev_mode, logger=self.logger) 
        except TypeError: # If logger arg not supported by DBHandler
             self.logger.warning("DB Handler init does not accept logger. Initializing without.")
             self.db_handler = DatabaseHandler(dev_mode=self.dev_mode)
        self.logger.info(f"DB Handler initialized in ChannelSummarizer. Dev mode: {self.dev_mode}")

        # Initialize sub-features correctly (pass dependencies, NOT bot=self)
        try:
            self.news_summarizer = NewsSummarizer(self.logger, self.dev_mode)
            self.logger.info("NewsSummarizer initialized.")

            self.top_generations = TopGenerations(self)
            self.logger.info("TopGenerations initialized.")

            self.top_art_sharer = TopArtSharing(self, self.sharer)
            self.logger.info("TopArtSharing initialized.")

            self.logger.info("Sub-feature handlers initialized successfully.")

        except Exception as e:
            self.logger.critical(f"Failed to initialize sub-feature handlers: {e}", exc_info=True)
            raise # Re-raise to prevent cog loading if any sub-feature fails

        # Initialize Rate Limiter (takes no arguments)
        self.rate_limiter = RateLimiter()
        self.logger.info("RateLimiter initialized.")

        # Initialize Attachment Handler
        self.attachment_handler = AttachmentHandler(self.logger, max_size=self.MAX_ATTACHMENT_SIZE)
        self.logger.info("Attachment Handler initialized.")

        # Load config AFTER other initializations if it depends on them
        self.load_config() 

        # Other attributes
        self.processed_today = set() 
        self._shutdown_flag = False 
        # Initialize the summary lock
        self.summary_lock = asyncio.Lock()
        self.first_message = None

        self.logger.info(f"ChannelSummarizer initialized successfully.")

    # Keep setup_logger if used by __init__
    def setup_logger(self, dev_mode):
        # ... (ensure this setup logic is appropriate or remove if logger is always passed)
        log_handler = LogHandler(logger_name='ChannelSummarizer', 
                                 prod_log_file='channel_summarizer.log', 
                                 dev_log_file='channel_summarizer_dev.log')
        self.logger = log_handler.setup_logging(dev_mode)
        if self.logger:
            self.logger.info(f"ChannelSummarizer logger setup ({'DEV' if dev_mode else 'PROD'}).")
        return self.logger # Return logger instance

    # Keep dev_mode property and setter
    @property
    def dev_mode(self):
        return self._dev_mode

    @dev_mode.setter
    def dev_mode(self, value):
        if not hasattr(self, '_dev_mode') or self._dev_mode != value:
            self._dev_mode = value
            # Reload config or re-init logger if necessary when mode changes
            # self.setup_logger(value) # Example: Reconfigure logger
            # self.load_config() # Example: Reload config
            self.logger.info(f"ChannelSummarizer dev_mode set to: {value}")

    def load_config(self):
        # ... (load_config implementation as before) ...
        self.logger.debug("Loading configuration...")
        # Simplified logging in load_config for clarity
        try:
             load_dotenv(override=True) # Ensure .env is loaded
             env_prefix = "DEV_" if self.dev_mode else ""
             self.guild_id = int(os.getenv(f'{env_prefix}GUILD_ID'))
             self.summary_channel_id = int(os.getenv(f'{env_prefix}SUMMARY_CHANNEL_ID'))
             monitor_key = f'{env_prefix}CHANNELS_TO_MONITOR'
             channels_str = os.getenv(monitor_key)
             if not channels_str:
                  raise ConfigurationError(f"{monitor_key} not found in environment")
             self.channels_to_monitor = [int(c.strip()) for c in channels_str.split(',') if c.strip()]
             # Load Art Channel ID based on mode
             art_channel_key = f'{env_prefix}ART_CHANNEL_ID'
             self.art_channel_id = int(os.getenv(art_channel_key))
             
             self.logger.info(f"Loaded {'DEV' if self.dev_mode else 'PROD'} config: Guild={self.guild_id}, Summary={self.summary_channel_id}, Monitor={self.channels_to_monitor}, Art={self.art_channel_id}")

        except (ValueError, TypeError) as e:
             self.logger.error(f"Invalid ID format in environment variables: {e}", exc_info=True)
             raise ConfigurationError(f"Invalid ID format: {e}")
        except ConfigurationError as e:
            self.logger.error(f"Configuration Error: {e}", exc_info=True)
            raise
        except Exception as e:
             self.logger.error(f"Unexpected error loading configuration: {e}", exc_info=True)
             raise ConfigurationError(f"Unexpected error loading config: {e}")

    async def _get_channel_with_retry(self, channel_id: int) -> Optional[Union[discord.TextChannel, discord.Thread, discord.ForumChannel]]:
        if not self.bot or not self.bot.is_ready(): # Also check if bot is ready
             self.logger.error("Bot instance not available or not ready in _get_channel_with_retry.")
             return None
        
        self.logger.debug(f"Attempting to get channel {channel_id}. Bot instance: {self.bot}")
        # Use the stored self.bot instance
        channel = self.bot.get_channel(channel_id)
        if channel:
            self.logger.debug(f"Channel {channel_id} found in cache: {channel}")
            return channel
        
        # Retry logic using self.bot.fetch_channel
        self.logger.warning(f"Channel {channel_id} not in cache. Will attempt to fetch via API.")
        delay = self.INITIAL_RETRY_DELAY
        for attempt in range(self.MAX_RETRIES):
             self.logger.warning(f"Attempt {attempt+1}/{self.MAX_RETRIES} to fetch channel {channel_id} via API after {delay:.1f}s delay...")
             await asyncio.sleep(delay)
             try:
                  # Use fetch_channel which makes an API call if not in cache
                  self.logger.debug(f"Calling self.bot.fetch_channel({channel_id})")
                  channel = await self.bot.fetch_channel(channel_id)
                  if channel:
                       self.logger.info(f"Successfully fetched channel {channel_id} via API on attempt {attempt+1}. Channel: {channel}")
                       return channel
                  else:
                       # This case should ideally not happen if fetch_channel doesn't raise an error
                       self.logger.warning(f"fetch_channel({channel_id}) returned None on attempt {attempt+1} without raising error.")
             except discord.NotFound:
                  self.logger.error(f"discord.NotFound error fetching channel {channel_id} on attempt {attempt+1}. The channel likely does not exist or the bot can't see it.")
                  # Don't retry if definitively not found
                  return None 
             except discord.Forbidden:
                  self.logger.error(f"discord.Forbidden error fetching channel {channel_id} on attempt {attempt+1}. Check bot permissions for this channel.")
                  # Don't retry if forbidden
                  return None 
             except discord.HTTPException as e:
                   self.logger.error(f"discord.HTTPException fetching channel {channel_id} on retry {attempt + 1}: Status={e.status}, Code={e.code}, Text={e.text}")
                   if e.status == 429: # Rate limited
                        retry_after = e.retry_after if hasattr(e, 'retry_after') and e.retry_after else delay * 2 # Use retry_after if available
                        self.logger.warning(f"Rate limited fetching channel. Retrying after {retry_after:.2f}s...")
                        await asyncio.sleep(retry_after)
                        delay = retry_after # Adjust delay based on header
                   else:
                        # Exponential backoff for other HTTP errors
                        delay = min(delay * 2, self.MAX_RETRY_WAIT) 
                        self.logger.warning(f"Applying exponential backoff. Next retry in {delay:.1f}s.")
             except Exception as e:
                  self.logger.error(f"Unexpected {type(e).__name__} error fetching channel {channel_id} on retry {attempt + 1}: {e}", exc_info=True)
                  delay = min(delay * 2, self.MAX_RETRY_WAIT) # Exponential backoff
                  self.logger.warning(f"Applying exponential backoff due to unexpected error. Next retry in {delay:.1f}s.")
        
        self.logger.error(f"Failed to get channel {channel_id} after {self.MAX_RETRIES} retries.")
        return None

    async def get_channel_history(self, channel_id: int, db_handler: Optional[DatabaseHandler] = None) -> List[dict]:
        """Get message history for a channel from the database (past 24h)."""
        self.logger.info(f"Getting message history for channel {channel_id} from database")
        
        try:
            yesterday = datetime.utcnow() - timedelta(hours=24)
            
            def get_messages():
                # Create a new connection for this thread
                thread_local_db = DatabaseHandler(dev_mode=self.dev_mode)
                try:
                    # Get total message count
                    cursor = thread_local_db.conn.cursor()
                    cursor.execute("""
                        SELECT COUNT(*) as count 
                        FROM messages 
                        WHERE channel_id = ? 
                        AND created_at > ?
                    """, (channel_id, yesterday.isoformat()))
                    total_count = cursor.fetchone()[0]
                    cursor.close()

                    self.logger.info(f"Found {total_count} messages in past 24h for channel {channel_id}")

                    if total_count == 0:
                        return []

                    # Get all messages
                    thread_local_db.conn.row_factory = sqlite3.Row
                    cursor = thread_local_db.conn.cursor()
                    cursor.execute("""
                        SELECT m.*, 
                               COALESCE(mem.username, 'Unknown') as username,
                               COALESCE(mem.server_nick, mem.global_name, mem.username, 'Unknown') as display_name
                        FROM messages m
                        LEFT JOIN members mem ON m.author_id = mem.member_id
                        WHERE m.channel_id = ?
                        AND m.created_at > ?
                        AND (m.is_deleted IS NULL OR m.is_deleted = FALSE)
                        ORDER BY m.created_at DESC
                    """, (channel_id, yesterday.isoformat()))
                    
                    messages = [dict(row) for row in cursor.fetchall()]
                    cursor.close()
                    
                    formatted_messages = []
                    for msg in messages:
                        try:
                            attachments = json.loads(msg['attachments']) if msg['attachments'] else []
                            reactors = json.loads(msg['reactors']) if msg['reactors'] else []
                            
                            formatted_msg = {
                                'message_id': msg['message_id'],
                                'channel_id': msg['channel_id'],
                                'author_id': msg['author_id'],
                                'content': msg['content'],
                                'created_at': msg['created_at'],
                                'attachments': attachments,
                                'reaction_count': msg['reaction_count'],
                                'reactors': reactors,
                                'reference_id': msg['reference_id'],
                                'thread_id': msg['thread_id'],
                                'author_name': msg['display_name']
                            }
                            formatted_messages.append(formatted_msg)
                        except Exception as e:
                            self.logger.error(f"Error formatting message {msg.get('message_id')}: {e}")
                            self.logger.debug(traceback.format_exc())
                            continue

                    return formatted_messages
                finally:
                    thread_local_db.close()

            return await asyncio.get_event_loop().run_in_executor(None, get_messages)

        except Exception as e:
            self.logger.error(f"Error retrieving message history: {e}")
            self.logger.debug(traceback.format_exc())
            return []

    @handle_errors("safe_send_message")
    async def safe_send_message(self, channel, content=None, embed=None, file=None, files=None, reference=None):
        """Safely send a message with concurrency-limited retry logic."""
        try:
            send_task = self.rate_limiter.execute(
                f"channel_{channel.id}",
                channel.send(
                    content=content,
                    embed=embed,
                    file=file,
                    files=files,
                    reference=reference
                )
            )
            return await asyncio.wait_for(send_task, timeout=10)
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout sending message to channel {channel.id}")
            raise
        except discord.HTTPException as e:
            self.logger.error(f"HTTP error sending message: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Error sending message: {e}")
            raise

    async def create_media_content(self, files: List[Tuple[discord.File, int, str, str]], max_media: int = 4) -> Optional[discord.File]:
        """Create a collage of images or a combined video, depending on attachments."""
        try:
            if not MEDIA_PROCESSING_AVAILABLE:
                self.logger.error("Media processing libraries are not available")
                return None
            
            self.logger.info(f"Starting media content creation with {len(files)} files")
            
            images = []
            videos = []
            has_audio = False
            
            for file_tuple, _, _, _ in files[:max_media]:
                file_tuple.fp.seek(0)
                data = file_tuple.fp.read()
                
                if file_tuple.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    self.logger.debug(f"Processing image: {file_tuple.filename}")
                    img = Image.open(io.BytesIO(data))
                    images.append(img)
                elif file_tuple.filename.lower().endswith(('.mp4', '.mov', '.webm')):
                    self.logger.debug(f"Processing video: {file_tuple.filename}")
                    temp_path = f'temp_{len(videos)}.mp4'
                    with open(temp_path, 'wb') as f:
                        f.write(data)
                    video = mp.VideoFileClip(temp_path)
                    if video.audio is not None:
                        has_audio = True
                        self.logger.debug(f"Video {file_tuple.filename} has audio")
                    videos.append(video)
            
            self.logger.info(f"Processed {len(images)} images and {len(videos)} videos. Has audio: {has_audio}")
                
            if videos and has_audio:
                self.logger.info("Creating combined video with audio")
                final_video = mp.concatenate_videoclips(videos)
                output_path = 'combined_video.mp4'
                final_video.write_videofile(output_path)
                
                for video in videos:
                    video.close()
                final_video.close()
                
                self.logger.info("Video combination complete")
                
                with open(output_path, 'rb') as f:
                    return discord.File(f, filename='combined_video.mp4')
                
            elif images or (videos and not has_audio):
                self.logger.info("Creating image/GIF collage")
                
                # Convert silent videos to GIF
                for i, video in enumerate(videos):
                    self.logger.debug(f"Converting silent video {i+1} to GIF")
                    gif_path = f'temp_gif_{len(images)}.gif'
                    video.write_gif(gif_path)
                    gif_img = Image.open(gif_path)
                    images.append(gif_img)
                    video.close()
                
                if not images:
                    self.logger.warning("No images available for collage")
                    return None
                
                n = len(images)
                if n == 1:
                    cols, rows = 1, 1
                elif n == 2:
                    cols, rows = 2, 1
                else:
                    cols, rows = 2, 2
                
                self.logger.debug(f"Creating {cols}x{rows} collage for {n} images")
                
                target_size = (800 // cols, 800 // rows)
                resized_images = []
                for i, img in enumerate(images):
                    self.logger.debug(f"Resizing image {i+1}/{len(images)} to {target_size}")
                    img = img.convert('RGB')
                    img.thumbnail(target_size)
                    resized_images.append(img)
                
                collage = Image.new('RGB', (800, 800))
                
                for idx, img in enumerate(resized_images):
                    x = (idx % cols) * (800 // cols)
                    y = (idx // cols) * (800 // rows)
                    collage.paste(img, (x, y))
                
                self.logger.info("Collage creation complete")
                
                buffer = io.BytesIO()
                collage.save(buffer, format='JPEG')
                buffer.seek(0)
                return discord.File(buffer, filename='collage.jpg')
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error creating media content: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        finally:
            # Cleanup
            import os
            self.logger.debug("Cleaning up temporary files")
            for f in os.listdir():
                if f.startswith('temp_'):
                    try:
                        os.remove(f)
                        self.logger.debug(f"Removed temporary file: {f}")
                    except Exception as ex:
                        self.logger.warning(f"Failed to remove temporary file {f}: {ex}")

    async def create_summary_thread(self, message, thread_name, is_top_generations=False):
        try:
            self.logger.info(f"Attempting to create thread '{thread_name}' for message {message.id}")
            # If it's already a Thread object
            if isinstance(message, discord.Thread):
                self.logger.warning(f"Message is already a Thread object with ID {message.id}. Returning it directly.")
                return message

            if not message.guild:
                self.logger.error("Cannot create thread: message is not in a guild")
                return None

            bot_member = message.guild.get_member(self.bot.user.id)
            if not bot_member:
                self.logger.error("Cannot find bot member in guild")
                return None

            self.logger.debug(f"Using channel: {message.channel} (ID: {message.channel.id}) for thread creation")
            required_permissions = ['create_public_threads', 'send_messages_in_threads', 'manage_messages']
            missing_permissions = [perm for perm in required_permissions if not getattr(message.channel.permissions_for(bot_member), perm, False)]
            if missing_permissions:
                self.logger.error(f"Missing required permissions in channel {message.channel.id}: {', '.join(missing_permissions)}")
                return None
            
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=1440  # 24 hours
            )
            
            if thread:
                self.logger.info(f"Successfully created thread: {thread.name} (ID: {thread.id})")
                
                # Only pin/unpin if this is not a top generations thread
                if not is_top_generations:
                    try:
                        pinned_messages = await message.channel.pins()
                        for pinned_msg in pinned_messages:
                            if pinned_msg.author.id == self.bot.user.id:
                                await pinned_msg.unpin()
                                self.logger.info(f"Unpinned previous message: {pinned_msg.id}")
                    except Exception as e:
                        self.logger.error(f"Error unpinning previous messages: {e}")
                    
                    try:
                        await message.pin()
                        self.logger.info(f"Pinned new thread starter message: {message.id}")
                    except Exception as e:
                        self.logger.error(f"Error pinning new message: {e}")
                
                return thread
            else:
                self.logger.error("Thread creation returned None")
                return None
                
        except discord.Forbidden as e:
            self.logger.error(f"Forbidden error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        except discord.HTTPException as e:
            self.logger.error(f"HTTP error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        except Exception as e:
            self.logger.error(f"Error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None

    @handle_errors("_execute_db_operation")
    async def _execute_db_operation(self, operation, *args, db_handler=None):
        # ... (_execute_db_operation as before) ...
        pass

    async def _post_summary_with_transaction(self, channel_id: int, summary: str, messages: list, current_date: datetime, db_handler: DatabaseHandler) -> bool:
        """Atomic operation for posting summary and updating database"""
        try:
            # Generate a short summary using the NewsSummarizer
            # NOTE: Assuming generate_short_summary handles its own errors and returns fallback text
            short_summary = await self.news_summarizer.generate_short_summary(summary, len(messages))
            
            # Use the provided db_handler for the operation
            def transaction(db: DatabaseHandler, short_sum: str):
                conn = None # Initialize conn to None
                try:
                    # Get a connection using the handler's method
                    conn = db._get_connection() 
                    
                    # Define the operation to be executed with retry logic if needed
                    def _transaction_operation(_conn):
                        cursor = _conn.cursor()
                        try:
                            self.logger.debug(f"Inserting summary for channel {channel_id} into daily_summaries.")
                            cursor.execute("""
                                INSERT INTO daily_summaries (
                                    date, channel_id, full_summary, short_summary, created_at
                                ) VALUES (?, ?, ?, ?, datetime('now'))
                            """, (
                                current_date.strftime('%Y-%m-%d'),
                                channel_id,
                                summary,
                                short_sum, # Use the passed short_summary
                            ))
                            summary_id = cursor.lastrowid
                            self.logger.debug(f"Inserted summary with ID: {summary_id}.")
                            
                            # Commit within the operation if execute_with_retry doesn't handle it
                            # Depending on _execute_with_retry, commit might be handled there
                            _conn.commit()
                            self.logger.info(f"Successfully saved summary {summary_id} for channel {channel_id} to DB.")
                            return True
                        except Exception as e_inner:
                            self.logger.error(f"Error during DB transaction operation for channel {channel_id}: {e_inner}", exc_info=True)
                            # Rollback within the operation
                            _conn.rollback()
                            raise # Re-raise to be caught by outer try/except or retry logic
                    
                    # Execute the operation, potentially with retry logic if db_handler provides it
                    if hasattr(db, '_execute_with_retry'):
                        return db._execute_with_retry(_transaction_operation) 
                    else:
                        # Execute directly if no retry mechanism
                        return _transaction_operation(conn)
                        
                except Exception as e_outer:
                    # Log error from getting connection or executing operation
                    self.logger.error(f"DB transaction failed for channel {channel_id}: {e_outer}", exc_info=True)
                    # Ensure rollback happens even if connection failed or retry mechanism failed
                    if conn: 
                        try:
                            conn.rollback()
                        except Exception as rb_exc:
                             self.logger.error(f"Error during rollback attempt: {rb_exc}")
                    raise # Re-raise the outer exception
            
            # Execute the transaction in a thread pool executor to avoid blocking
            loop = asyncio.get_running_loop()
            # Pass db_handler and short_summary to the transaction function
            return await loop.run_in_executor(None, transaction, db_handler, short_summary)
            
        except Exception as e:
            # Catch errors from generate_short_summary or the transaction execution
            self.logger.error(f"Error in _post_summary_with_transaction for channel {channel_id}: {e}", exc_info=True)
            return False

    def is_forum_channel(self, channel_id: int) -> bool:
        # ... (is_forum_channel as before) ...
        pass

    async def _wait_for_connection(self, timeout=30):
        # ... (_wait_for_connection as before) ...
        pass

    async def _get_dev_mode_channels(self, db_handler):
        """Get active channels for dev mode"""
        try:
            # Get source channel (where to pull messages from)
            test_channel_str = os.getenv('TEST_DATA_CHANNEL', '')
            if not test_channel_str:
                self.logger.warning("TEST_DATA_CHANNEL not configured")
                return []
                
            test_channel_ids = [int(cid.strip()) for cid in test_channel_str.split(',') if cid.strip()]
            if not test_channel_ids:
                self.logger.warning("No test channels configured")
                return []

            # Get destination channels (where to post summaries)
            dev_channels_str = os.getenv('DEV_CHANNELS_TO_MONITOR', '')
            if not dev_channels_str:
                self.logger.warning("DEV_CHANNELS_TO_MONITOR not configured")
                return []

            dev_channel_ids = [int(cid.strip()) for cid in dev_channels_str.split(',') if cid.strip()]
            if not dev_channel_ids:
                self.logger.warning("No dev channels configured")
                return []
                
            self.logger.debug(f"Source channel IDs (TEST_DATA_CHANNEL): {test_channel_ids}")
            self.logger.debug(f"Destination channel IDs (DEV_CHANNELS_TO_MONITOR): {dev_channel_ids}")
            
            query = (
                "SELECT DISTINCT channel_id "
                "FROM messages "
                "WHERE channel_id IN ({}) "
                "GROUP BY channel_id "
                "HAVING COUNT(*) >= 25"
            ).format(",".join(str(cid) for cid in test_channel_ids))
            
            loop = asyncio.get_running_loop()
            try:
                def db_operation():
                    try:
                        results = db_handler.execute_query(query)
                        # For each source channel that has enough messages,
                        # set its post_channel_id to the first dev channel
                        if results and dev_channel_ids:
                            # Ensure results is a list of dictionaries
                            processed_results = []
                            for row in results:
                                row_dict = dict(row) # Convert row object to dict if needed
                                row_dict['post_channel_id'] = dev_channel_ids[0]
                                processed_results.append(row_dict)
                            return processed_results
                        return [] # Return empty list if no results or no dev_channel_ids
                    except Exception as e:
                        self.logger.error(f"Error in dev channel query db_operation: {e}", exc_info=True)
                        return []
                        
                return await asyncio.wait_for(
                    loop.run_in_executor(None, db_operation),
                    timeout=10 # Adjust timeout as needed
                )
            except asyncio.TimeoutError:
                self.logger.error("Timeout while executing database query for dev channels")
                return []
            except Exception as e:
                self.logger.error(f"Error executing dev channel query: {e}", exc_info=True)
                return []
        except Exception as e:
            self.logger.error(f"Error getting dev channels: {e}", exc_info=True)
            return []

    async def _get_production_channels(self, db_handler):
        """Get active channels for production mode"""
        try:
            channel_ids = ",".join(str(cid) for cid in self.channels_to_monitor)
            if not channel_ids:
                 self.logger.warning("No production channels configured in self.channels_to_monitor")
                 return []
                 
            channel_query = (
                "SELECT c.channel_id, c.channel_name, COALESCE(c2.channel_name, 'Unknown') as source, "
                "COUNT(m.message_id) as msg_count "
                "FROM channels c "
                "LEFT JOIN channels c2 ON c.category_id = c2.channel_id "
                "LEFT JOIN messages m ON c.channel_id = m.channel_id "
                "AND m.created_at > datetime('now', '-24 hours') "
                f"WHERE c.channel_id IN ({channel_ids}) OR c.category_id IN ({channel_ids}) "
                "GROUP BY c.channel_id, c.channel_name, source "
                "HAVING COUNT(m.message_id) >= 25 "
                "ORDER BY msg_count DESC"
            )
            
            loop = asyncio.get_running_loop()
            def db_operation():
                try:
                    # Ensure db_handler.execute_query is used if it exists and handles connection/cursor
                    if hasattr(db_handler, 'execute_query'):
                         results = db_handler.execute_query(channel_query)
                         return results if results else []
                    else:
                         # Fallback to direct connection if needed (adjust based on db_handler)
                         if not db_handler or not hasattr(db_handler, 'conn'):
                              self.logger.error("DB handler or connection not available for production channel query")
                              return []
                         db_handler.conn.execute("PRAGMA busy_timeout = 5000")
                         db_handler.conn.row_factory = sqlite3.Row
                         cursor = db_handler.conn.cursor()
                         cursor.execute(channel_query)
                         results = [dict(row) for row in cursor.fetchall()]
                         cursor.close()
                         db_handler.conn.row_factory = None
                         return results
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        self.logger.error("Database lock timeout exceeded during production channel query")
                    else:
                        self.logger.error(f"Database operational error during production channel query: {e}", exc_info=True)
                    return []
                except Exception as e:
                    self.logger.error(f"Error getting active production channels in db_operation: {e}", exc_info=True)
                    return []
            return await asyncio.wait_for(
                loop.run_in_executor(None, db_operation),
                timeout=10 # Adjust timeout as needed
            )
        except asyncio.TimeoutError:
            self.logger.error("Timeout while executing database query for production channels")
            return []
        except Exception as e:
            self.logger.error(f"Error executing production channel database query: {e}", exc_info=True)
            return []

    # --- Main Summary Generation Logic --- 
    @handle_errors("generate_summary")
    async def generate_summary(self):
        """
        Generate and post summaries following these steps:
        1) Generate individual channel summaries and post to their channels (except for forum channels)
        2) Combine channel summaries for overall summary
        3) Post overall summary to summary channel
        4) Post top generations
        5) Post top art sharing
        """
        try:
            async with self.summary_lock:
                self.logger.info("Generating requested summary...")
                # Use the instance db_handler
                db_handler = self.db_handler 
                
                # Get summary channel first to avoid undefined variable issues
                summary_channel = await self._get_channel_with_retry(self.summary_channel_id)
                if not summary_channel:
                    self.logger.error(f"Could not find summary channel {self.summary_channel_id}")
                    return

                current_date = datetime.utcnow()

                # We'll handle channel picking ourselves:
                if self.dev_mode:
                    active_channels = await self._get_dev_mode_channels(db_handler)
                else:
                    active_channels = await self._get_production_channels(db_handler)
                
                if not active_channels:
                    self.logger.warning("No active channels found")
                    return

                channel_summaries = []
                self.logger.info("Processing individual summaries for channels with 25+ messages:")
                
                for channel_info in active_channels:
                    channel_id = channel_info['channel_id']
                    # Use post_channel_id if available (for dev mode), else use channel_id
                    post_channel_id = channel_info.get('post_channel_id', channel_id)
                    
                    try:
                        messages = await self.get_channel_history(channel_id, db_handler)
                        if not messages or len(messages) < 25:
                             self.logger.info(f"Skipping channel {channel_id}: Not enough messages ({len(messages)}).")
                             continue
                        
                        # === Call into NewsSummarizer to summarize these messages ===
                        channel_summary = await self.news_summarizer.generate_news_summary(messages)
                        if not channel_summary or channel_summary in [
                            "[NOTHING OF NOTE]", 
                            "[NO SIGNIFICANT NEWS]",
                            "[NO MESSAGES TO ANALYZE]"
                        ]:
                            self.logger.info(f"No significant news for channel {channel_id}.")
                            continue
                        
                        # Post to the channel (unless it's a forum)
                        if not self.is_forum_channel(post_channel_id):
                            channel_obj = await self._get_channel_with_retry(post_channel_id)
                            if channel_obj:
                                formatted_summary = self.news_summarizer.format_news_for_discord(channel_summary)
                                loop = asyncio.get_running_loop()
                                def get_existing_thread_id():
                                    # ... (get thread id logic as before) ...
                                    pass
                                
                                # existing_thread_id = await loop.run_in_executor(None, get_existing_thread_id)
                                # For simplicity in restoration, let's always create a new thread for now
                                existing_thread_id = None 
                                thread = None
                                if existing_thread_id:
                                    # ... (fetch thread logic as before) ...
                                    pass
                                
                                # Create new thread if needed
                                if not thread:
                                    self.logger.info(f"Creating new summary thread for channel {channel_id}...")
                                    thread_title = f"#{channel_obj.name} - Summary - {current_date.strftime('%B %d, %Y')}"
                                    summary_message = await self.safe_send_message(channel_obj, f"Summary thread for {current_date.strftime('%B %d, %Y')}")
                                    if summary_message:
                                        thread = await self.create_summary_thread(summary_message, thread_title)
                                        if thread:
                                             self.logger.info(f"Successfully created summary thread for channel {channel_id}: {thread.id}")
                                             # Consider updating DB with thread ID here if needed
                                        else:
                                             self.logger.error(f"Failed to create thread for channel {channel_id}")
                                    else:
                                         self.logger.error(f"Failed to send header message to channel {channel_id}")
                                
                                if thread:
                                    self.logger.info(f"Using summary thread in channel {post_channel_id}: {thread.id}")
                                    date_headline = f"# {current_date.strftime('%A, %B %d, %Y')}\n"
                                    header_msg = await self.safe_send_message(thread, date_headline)
                                    await asyncio.sleep(1)
                                    # Post each portion of the summary
                                    for item in formatted_summary:
                                        # ... (posting formatted summary items, including media references as before) ...
                                        if item.get('type') == 'media_reference':
                                            # --- Start Edit 1 ---
                                            try:
                                                # Extract IDs, converting to int
                                                source_channel_id = int(item['channel_id'])
                                                message_id_to_fetch = int(item['message_id'])
                                                self.logger.debug(f"Processing media reference: Channel={source_channel_id}, Message={message_id_to_fetch}")

                                                # Fetch the source channel
                                                source_channel = await self._get_channel_with_retry(source_channel_id)
                                                if not source_channel:
                                                    self.logger.warning(f"Could not find source channel {source_channel_id} for media message {message_id_to_fetch}")
                                                    continue # Skip this media item

                                                # Fetch the original message
                                                try:
                                                    original_message = await source_channel.fetch_message(message_id_to_fetch)
                                                    self.logger.debug(f"Fetched original message {message_id_to_fetch} from channel {source_channel_id}.")
                                                except discord.NotFound:
                                                    self.logger.warning(f"Original message {message_id_to_fetch} not found in channel {source_channel_id}.")
                                                    continue # Skip this media item
                                                except discord.Forbidden:
                                                    self.logger.error(f"Forbidden to fetch message {message_id_to_fetch} from channel {source_channel_id}.")
                                                    continue # Skip this media item
                                                except discord.HTTPException as e:
                                                    self.logger.error(f"HTTP error fetching message {message_id_to_fetch}: {e}")
                                                    continue # Skip this media item

                                                # Post attachments if they exist
                                                if original_message.attachments:
                                                    self.logger.info(f"Posting {len(original_message.attachments)} attachments from message {message_id_to_fetch}")
                                                    for attachment in original_message.attachments:
                                                        # Post the URL of the attachment
                                                        await self.safe_send_message(thread, attachment.url)
                                                        await asyncio.sleep(0.5) # Small delay between attachments
                                                else:
                                                    self.logger.info(f"Message {message_id_to_fetch} referenced for media has no attachments.")

                                            except KeyError as e:
                                                self.logger.error(f"Missing key in media reference item {item}: {e}")
                                            except ValueError as e:
                                                 self.logger.error(f"Invalid ID format in media reference item {item}: {e}")
                                            except Exception as e:
                                                self.logger.error(f"Unexpected error processing media reference {item}: {e}")
                                                self.logger.debug(traceback.format_exc())
                                            # --- End Edit 1 ---
                                        else:
                                             await self.safe_send_message(thread, item.get('content', ''))
                                             await asyncio.sleep(1)
                                    
                                    # Post top gens for the specific channel into this thread
                                    await self.top_generations.post_top_gens_for_channel(thread, channel_id)
                                    # Generate and post short summary with link back using the header message's id
                                    if header_msg:
                                         short_summary = await self.news_summarizer.generate_short_summary(channel_summary, len(messages))
                                         link = f"https://discord.com/channels/{channel_obj.guild.id}/{thread.id}/{header_msg.id}"
                                         await self.safe_send_message(thread, f"\n---\n\n***Click here to jump to the beginning of today's summary:***{link}")
                                         channel_header = f"**### Channel summary for {current_date.strftime('%A, %B %d, %Y')}**"
                                         await self.safe_send_message(channel_obj, f"{channel_header}{short_summary}\n[Click here to jump to the summary thread]({link})")
                                    else:
                                         self.logger.warning("Header message for thread summary was not sent or found, cannot post short summary link.")
                                else:
                                    self.logger.error(f"Failed to create or fetch thread for channel {channel_id}")
                        
                        # Store result in DB regardless of posting success
                        success = await self._post_summary_with_transaction(
                            channel_id,
                            channel_summary,
                            messages,
                            current_date,
                            db_handler
                        )
                        if success:
                            channel_summaries.append(channel_summary)
                        else:
                            self.logger.error(f"Failed to save summary to DB for channel {channel_id}")

                    except Exception as e:
                        self.logger.error(f"Error processing channel {channel_id}: {e}", exc_info=True)
                        continue # Move to next channel

                # Combine summaries for overall summary
                if channel_summaries:
                    self.logger.info(f"Combining summaries from {len(channel_summaries)} channels...")
                    overall_summary = await self.news_summarizer.combine_channel_summaries(channel_summaries)
                    
                    if overall_summary and overall_summary not in [
                        "[NOTHING OF NOTE]", 
                        "[NO SIGNIFICANT NEWS]",
                        "[NO MESSAGES TO ANALYZE]"
                    ]:
                        formatted_summary = self.news_summarizer.format_news_for_discord(overall_summary)
                        header = await self.safe_send_message(summary_channel, f"\n\n# Daily Summary - {current_date.strftime('%A, %B %d, %Y')}\n\n")
                        if header is not None:
                            self.first_message = header
                        else:
                            self.logger.error("Failed to post header message; first_message remains unset.")
                        
                        self.logger.info("Posting main summary to summary channel")
                        for item in formatted_summary:
                            # ... (posting combined summary items, including media references as before) ...
                             if item.get('type') == 'media_reference':
                                 # --- Start Edit 2 ---
                                 try:
                                     # Extract IDs, converting to int
                                     source_channel_id = int(item['channel_id'])
                                     message_id_to_fetch = int(item['message_id'])
                                     self.logger.debug(f"Processing media reference for main summary: Channel={source_channel_id}, Message={message_id_to_fetch}")

                                     # Fetch the source channel
                                     source_channel = await self._get_channel_with_retry(source_channel_id)
                                     if not source_channel:
                                         self.logger.warning(f"Could not find source channel {source_channel_id} for media message {message_id_to_fetch}")
                                         continue # Skip this media item

                                     # Fetch the original message
                                     try:
                                         original_message = await source_channel.fetch_message(message_id_to_fetch)
                                         self.logger.debug(f"Fetched original message {message_id_to_fetch} from channel {source_channel_id}.")
                                     except discord.NotFound:
                                         self.logger.warning(f"Original message {message_id_to_fetch} not found in channel {source_channel_id}.")
                                         continue # Skip this media item
                                     except discord.Forbidden:
                                         self.logger.error(f"Forbidden to fetch message {message_id_to_fetch} from channel {source_channel_id}.")
                                         continue # Skip this media item
                                     except discord.HTTPException as e:
                                         self.logger.error(f"HTTP error fetching message {message_id_to_fetch}: {e}")
                                         continue # Skip this media item

                                     # Post attachments if they exist
                                     if original_message.attachments:
                                         self.logger.info(f"Posting {len(original_message.attachments)} attachments from message {message_id_to_fetch} to main summary channel")
                                         for attachment in original_message.attachments:
                                             # Post the URL of the attachment
                                             await self.safe_send_message(summary_channel, attachment.url)
                                             await asyncio.sleep(0.5) # Small delay between attachments
                                     else:
                                         self.logger.info(f"Message {message_id_to_fetch} referenced for media has no attachments.")

                                 except KeyError as e:
                                     self.logger.error(f"Missing key in media reference item {item}: {e}")
                                 except ValueError as e:
                                      self.logger.error(f"Invalid ID format in media reference item {item}: {e}")
                                 except Exception as e:
                                     self.logger.error(f"Unexpected error processing media reference {item}: {e}")
                                     self.logger.debug(traceback.format_exc())
                                 # --- End Edit 2 ---
                             else:
                                 await self.safe_send_message(summary_channel, item.get('content', ''))
                                 await asyncio.sleep(1)
                    else:
                        await self.safe_send_message(summary_channel, "_No significant activity to summarize in the last 24 hours._")
                else:
                    await self.safe_send_message(summary_channel, "_No messages found in the last 24 hours for overall summary._")

                # Step 4) Post top generations
                await self.top_generations.post_top_x_generations(summary_channel, limit=4)

                # Step 5) Post top art sharing (which now initiates the sharing process)
                await self.top_art_sharer.post_top_art_share(summary_channel)
                
                # Link back to the start
                self.logger.info("Attempting to send link back to start...")
                if self.first_message:
                    self.logger.info(f"First message exists with ID: {self.first_message.id}")
                    link_to_start = self.first_message.jump_url # Use jump_url property
                    self.logger.info(f"Generated link: {link_to_start}")
                    await self.safe_send_message(summary_channel, f"\n---\n\n***Click here to jump to the beginning of today's summary:*** {link_to_start}")
                    self.logger.info("Sent link back to start message")
                else:
                    self.logger.warning("No first_message found, cannot send link back")

        except Exception as e:
            self.logger.error(f"Critical error in summary generation: {e}", exc_info=True)
            # Consider notifying admin or sending error to summary channel
            try:
                 await self.safe_send_message(summary_channel, f"⚠️ Critical error during summary generation: {e}")
            except Exception:
                 pass # Avoid errors during error reporting
            # Potentially re-raise if this is fatal for the bot run?
            # raise e 
        finally:
            # Ensure lock is released even on error
            if self.summary_lock.locked():
                 self.summary_lock.release()
            # Close DB handler if it was opened specifically for this run
            # (If db_handler is instance attr, close it in bot cleanup)
            # db_handler.close()

    # --- Utility and Helper Methods --- 
    def register_events(self):
        # ... (register_events as before) ...
        pass

    def _get_today_str(self):
        # ... (_get_today_str as before) ...
        pass

    async def cleanup(self):
        # ... (cleanup as before) ...
        pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # ... (__aexit__ as before) ...
        pass

# --- Main Execution / Test Block --- 
if __name__ == "__main__":
    # This block is likely for testing and might not be needed for production Cog use
    def main():
        print("This script is intended to be run as part of a Discord bot Cog.")
        # Example Test Initialization (requires .env)
        # load_dotenv()
        # logger = logging.getLogger('TestSummarizer')
        # logging.basicConfig(level=logging.INFO)
        # test_summarizer = ChannelSummarizer(logger=logger, dev_mode=True)
        # # Mock sharer for testing
        # class MockSharer:
        #     def initiate_sharing_process_from_summary(self, msg): pass
        # test_summarizer.sharer_instance = MockSharer()
        # # Run test logic (e.g., generate summary for dev channels)
        # asyncio.run(test_summarizer.generate_summary())
        pass
    main()
