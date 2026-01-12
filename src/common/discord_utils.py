import discord
import asyncio
import json
import logging
from typing import Union, Optional, List, Dict, Any

from src.common.rate_limiter import RateLimiter
from src.common.error_handler import handle_errors

@handle_errors("safe_send_message")
async def safe_send_message(
    bot: Union[discord.Client, discord.ext.commands.Bot],
    channel: discord.abc.Messageable,
    rate_limiter: RateLimiter,
    logger: logging.Logger,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    file: Optional[discord.File] = None,
    files: Optional[List[discord.File]] = None,
    reference: Optional[Union[discord.Message, discord.MessageReference, discord.PartialMessage]] = None,
    view: Optional[discord.ui.View] = None
) -> Optional[discord.Message]:
    """
    Safely sends a message to a given channel, handling rate limits and common errors.

    Args:
        bot: The bot instance (for admin error notification via @handle_errors).
        channel: The Discord channel, thread, or user to send the message to.
        rate_limiter: An instance of the RateLimiter.
        logger: A logger instance for logging specific operational details.
        content: The content of the message.
        embed: The embed to send with the message.
        file: A file to send with the message.
        files: A list of files to send with the message.
        reference: A message to reply to.
        view: A discord.ui.View to send with the message.

    Returns:
        The sent discord.Message object if successful, None otherwise.
        Raises exceptions on failure after retries or for critical errors.
    """
    try:
        if rate_limiter:
            # Key for rate limiting can be channel.id
            # The rate_limiter now expects a factory
            coroutine_factory = lambda: channel.send(
                content=content, embed=embed, file=file, files=files, reference=reference, view=view
            )
            return await rate_limiter.execute(channel.id, coroutine_factory)
        else:
            # Fallback if no rate limiter is provided (though generally expected)
            logger.warning(f"Sending message to {getattr(channel, 'name', channel.id)} without a rate limiter.")
            send_task = channel.send(
                content=content, embed=embed, file=file, files=files, reference=reference, view=view
            )
            return await asyncio.wait_for(send_task, timeout=30)

    except discord.HTTPException as e:
        # Logged by @handle_errors, but specific logging here can be useful too.
        logger.error(f"HTTP error during safe_send_message to {getattr(channel, 'name', channel.id)}: {e.status} {e.text}")
        raise # Re-raise for @handle_errors and further up the call stack
    except asyncio.TimeoutError:
        # Only for the direct asyncio.wait_for in the else block
        logger.error(f"Timeout during direct safe_send_message to {getattr(channel, 'name', channel.id)} (no rate_limiter).")
        raise # Re-raise
    except (OSError, ConnectionError, TimeoutError) as e:
        # Handle network connectivity issues
        logger.error(f"Network connectivity error during safe_send_message to {getattr(channel, 'name', channel.id)}: {e}")
        raise
    except Exception as e:
        # Catch any other unexpected error during the send logic itself.
        # @handle_errors will catch this too if it's not caught here.
        logger.error(f"Unexpected error in safe_send_message logic for {getattr(channel, 'name', channel.id)}: {e}", exc_info=True)
        raise # Re-raise


async def refresh_media_url(
    bot: Union[discord.Client, discord.ext.commands.Bot],
    channel_id: int,
    message_id: int,
    logger: Optional[logging.Logger] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch a message from Discord API to get fresh attachment URLs.
    
    Discord CDN URLs expire after a period of time. This function fetches the
    message from the Discord API to get current, non-expired URLs for all attachments.
    
    Args:
        bot: The Discord bot client
        channel_id: The channel ID where the message is located
        message_id: The message ID to refresh
        logger: Optional logger instance
        
    Returns:
        Dict containing:
            - 'message_id': The message ID
            - 'channel_id': The channel ID  
            - 'attachments': List of attachment dicts with refreshed URLs
            - 'success': True if URLs were refreshed
        None if the message could not be fetched
    """
    log = logger or logging.getLogger('DiscordBot')
    
    try:
        # Get channel
        channel = bot.get_channel(channel_id)
        if not channel:
            log.debug(f"Channel {channel_id} not in cache, fetching...")
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                log.error(f"Channel {channel_id} not found")
                return None
            except discord.Forbidden:
                log.error(f"No permission to access channel {channel_id}")
                return None
        
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.error(f"Channel {channel_id} is not a text channel or thread")
            return None
        
        # Fetch message
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            log.warning(f"Message {message_id} not found in channel {channel_id}")
            return None
        except discord.Forbidden:
            log.error(f"No permission to fetch message {message_id}")
            return None
        
        # Extract fresh attachment data
        fresh_attachments = []
        for att in message.attachments:
            fresh_attachments.append({
                'id': att.id,
                'filename': att.filename,
                'url': att.url,
                'proxy_url': att.proxy_url,
                'size': att.size,
                'content_type': att.content_type,
                'height': att.height,
                'width': att.width,
            })
        
        log.info(f"Refreshed {len(fresh_attachments)} attachment URL(s) for message {message_id}")
        
        return {
            'message_id': message_id,
            'channel_id': channel_id,
            'attachments': fresh_attachments,
            'success': True
        }
        
    except Exception as e:
        log.error(f"Error refreshing media URLs for message {message_id}: {e}", exc_info=True)
        return None


async def refresh_and_update_message_urls(
    bot: Union[discord.Client, discord.ext.commands.Bot],
    db_handler: 'DatabaseHandler',
    channel_id: int,
    message_id: int,
    logger: Optional[logging.Logger] = None
) -> bool:
    """
    Refresh expired Discord media URLs and update them in the database.
    
    This fetches the message from Discord API to get fresh attachment URLs,
    then updates the stored attachments in the database.
    
    Args:
        bot: The Discord bot client
        db_handler: Database handler instance
        channel_id: The channel ID where the message is located
        message_id: The message ID to refresh
        logger: Optional logger instance
        
    Returns:
        True if URLs were successfully refreshed and updated in DB
    """
    log = logger or logging.getLogger('DiscordBot')
    
    # Fetch fresh URLs from Discord
    result = await refresh_media_url(bot, channel_id, message_id, logger)
    
    if not result or not result.get('success'):
        return False
    
    fresh_attachments = result['attachments']
    
    if not fresh_attachments:
        log.info(f"Message {message_id} has no attachments to refresh")
        return True
    
    # Update in database
    try:
        message_data = {
            'message_id': message_id,
            'channel_id': channel_id,
            'attachments': fresh_attachments
        }
        
        success = db_handler.update_message(message_data)
        
        if success:
            log.info(f"Updated {len(fresh_attachments)} attachment URL(s) in database for message {message_id}")
        else:
            log.error(f"Failed to update attachments in database for message {message_id}")
        
        return success
        
    except Exception as e:
        log.error(f"Error updating message {message_id} in database: {e}", exc_info=True)
        return False