import discord
from discord.ext import commands
import asyncio
import logging
from dotenv import load_dotenv
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

# Add parent directory to Python path BEFORE importing from src
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.base_bot import BaseDiscordBot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Disable discord.py debug logging
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.WARNING)

class MessageDeleter(BaseDiscordBot):
    def __init__(self, target_user_id: int, hours: int, dry_run: bool = True):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.guilds = True
        intents.voice_states = False
        super().__init__(
            command_prefix="!",
            intents=intents,
            heartbeat_timeout=120.0,
            guild_ready_timeout=30.0,
            gateway_queue_size=512,
            logger=logger
        )
        self.target_user_id = target_user_id
        self.hours = hours
        self.dry_run = dry_run
        self.cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

    async def find_and_delete_messages(self):
        """Find and delete (or list in dry run) messages from target user."""
        logger.info(f"{'DRY RUN: ' if self.dry_run else ''}Looking for messages from user {self.target_user_id}")
        logger.info(f"Time range: Past {self.hours} hours (since {self.cutoff_time.strftime('%Y-%m-%d %H:%M:%S UTC')})")
        
        total_found = 0
        total_deleted = 0
        messages_by_channel = {}
        
        # Iterate through all guilds the bot is in
        for guild in self.guilds:
            logger.info(f"\nChecking guild: {guild.name}")
            
            # Check all text channels
            for channel in guild.text_channels:
                try:
                    messages = []
                    async for message in channel.history(limit=None, after=self.cutoff_time):
                        if message.author.id == self.target_user_id:
                            messages.append(message)
                    
                    if messages:
                        messages_by_channel[channel.name] = messages
                        total_found += len(messages)
                        logger.info(f"  #{channel.name}: Found {len(messages)} messages")
                        
                        if self.dry_run:
                            # In dry run, just show what would be deleted
                            for msg in messages[:5]:  # Show first 5 as sample
                                content_preview = msg.content[:50] + "..." if len(msg.content) > 50 else msg.content
                                logger.info(f"    - [{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {content_preview}")
                            if len(messages) > 5:
                                logger.info(f"    ... and {len(messages) - 5} more messages")
                        else:
                            # Actually delete messages
                            deleted = await self.delete_messages_from_channel(channel, messages)
                            total_deleted += deleted
                            
                except discord.Forbidden:
                    logger.warning(f"  No permission to access #{channel.name}")
                except Exception as e:
                    logger.error(f"  Error checking #{channel.name}: {e}")
            
            # Check all threads
            try:
                active_threads = await guild.active_threads()
                for thread in active_threads:
                    try:
                        messages = []
                        async for message in thread.history(limit=None, after=self.cutoff_time):
                            if message.author.id == self.target_user_id:
                                messages.append(message)
                        
                        if messages:
                            thread_name = f"[THREAD] {thread.name}"
                            messages_by_channel[thread_name] = messages
                            total_found += len(messages)
                            logger.info(f"  {thread_name}: Found {len(messages)} messages")
                            
                            if self.dry_run:
                                for msg in messages[:5]:
                                    content_preview = msg.content[:50] + "..." if len(msg.content) > 50 else msg.content
                                    logger.info(f"    - [{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {content_preview}")
                                if len(messages) > 5:
                                    logger.info(f"    ... and {len(messages) - 5} more messages")
                            else:
                                deleted = await self.delete_messages_from_channel(thread, messages)
                                total_deleted += deleted
                                
                    except Exception as e:
                        logger.error(f"  Error checking thread {thread.name}: {e}")
            except Exception as e:
                logger.error(f"Error fetching threads: {e}")
        
        # Print summary
        logger.info("\n" + "="*60)
        if self.dry_run:
            logger.info(f"DRY RUN COMPLETE - Found {total_found} messages that WOULD be deleted")
            logger.info(f"Channels/threads affected: {len(messages_by_channel)}")
            logger.info("\nTo actually delete these messages, run with --execute flag:")
            logger.info("  python scripts/delete_user_messages.py --user 1316765722738688030 --hours 3 --execute")
        else:
            logger.info(f"DELETION COMPLETE - Deleted {total_deleted} out of {total_found} messages")
            logger.info(f"Channels/threads affected: {len(messages_by_channel)}")
        logger.info("="*60)

    async def delete_messages_from_channel(self, channel, messages):
        """Delete messages from a channel or thread."""
        deleted_count = 0
        
        try:
            # Try bulk deletion first (for messages less than 14 days old)
            if len(messages) > 1:
                try:
                    chunks = [messages[i:i + 100] for i in range(0, len(messages), 100)]
                    for chunk in chunks:
                        # Filter out messages older than 14 days for bulk delete
                        recent_messages = [m for m in chunk 
                                         if (datetime.now(timezone.utc) - m.created_at).days < 14]
                        old_messages = [m for m in chunk 
                                      if (datetime.now(timezone.utc) - m.created_at).days >= 14]
                        
                        # Bulk delete recent messages
                        if recent_messages:
                            await channel.delete_messages(recent_messages)
                            deleted_count += len(recent_messages)
                            logger.info(f"    Bulk deleted {len(recent_messages)} messages")
                        
                        # Individual delete for old messages
                        for message in old_messages:
                            try:
                                await message.delete()
                                deleted_count += 1
                            except Exception as e:
                                logger.error(f"    Failed to delete old message {message.id}: {e}")
                        
                        await asyncio.sleep(1)  # Rate limit protection
                        
                except discord.HTTPException as e:
                    logger.warning(f"    Bulk deletion failed: {e}, falling back to individual deletion")
                    # Fall back to individual deletion
                    deleted_count = 0
                    for message in messages:
                        try:
                            await message.delete()
                            deleted_count += 1
                            await asyncio.sleep(1)  # Rate limit protection
                        except Exception as e:
                            logger.error(f"    Failed to delete message {message.id}: {e}")
            else:
                # Single message case
                await messages[0].delete()
                deleted_count = 1
                logger.info(f"    Deleted 1 message")
            
            return deleted_count
            
        except Exception as e:
            logger.error(f"    Error deleting messages: {e}")
            return deleted_count

def main():
    parser = argparse.ArgumentParser(
        description='Delete messages from a specific user within a time range (DRY RUN by default)'
    )
    parser.add_argument(
        '--user', 
        type=int, 
        default=1316765722738688030,
        help='User ID to delete messages from (default: 1316765722738688030)'
    )
    parser.add_argument(
        '--hours', 
        type=int, 
        default=3,
        help='Number of hours to look back (default: 3)'
    )
    parser.add_argument(
        '--execute', 
        action='store_true',
        help='Actually delete messages (without this flag, it\'s a dry run)'
    )
    parser.add_argument(
        '--dev',
        action='store_true',
        help='Use development bot token instead of production'
    )
    
    args = parser.parse_args()
    
    # Load environment variables from .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    load_dotenv(env_path)
    
    # Get the appropriate token
    token_key = 'DEV_DISCORD_BOT_TOKEN' if args.dev else 'DISCORD_BOT_TOKEN'
    token = os.getenv(token_key)
    if not token:
        logger.error(f"No Discord bot token found in environment variables (looking for {token_key})")
        sys.exit(1)
    
    dry_run = not args.execute
    
    if dry_run:
        logger.info("="*60)
        logger.info("DRY RUN MODE - No messages will be deleted")
        logger.info("="*60 + "\n")
    else:
        logger.warning("="*60)
        logger.warning("LIVE MODE - Messages WILL be deleted!")
        logger.warning("="*60 + "\n")
        # Give user a chance to cancel
        import time
        logger.warning("Starting deletion in 5 seconds... Press Ctrl+C to cancel")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("\nCancelled by user")
            sys.exit(0)
    
    # Initialize the bot
    bot = MessageDeleter(
        target_user_id=args.user,
        hours=args.hours,
        dry_run=dry_run
    )
    
    async def run_bot():
        try:
            async with bot:
                await bot.start(token)
        except KeyboardInterrupt:
            logger.info("\nStopping bot...")
        finally:
            if not bot.is_closed():
                await bot.close()
    
    # Add event handler
    @bot.event
    async def on_ready():
        logger.info(f'Bot is ready: {bot.user.name}')
        try:
            await bot.find_and_delete_messages()
        finally:
            await bot.close()
    
    # Run the bot
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("\nScript interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Script failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()

