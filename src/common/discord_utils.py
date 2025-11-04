import discord
import asyncio
import logging
from typing import Union, Optional, List

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