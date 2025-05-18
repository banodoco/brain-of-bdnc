import discord
import traceback # For detailed error logging
from typing import TYPE_CHECKING

from src.common.discord_utils import safe_send_message
from src.common.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from discord.ext import commands

async def ask_to_share_to_openmuse(bot: 'commands.Bot', reaction: discord.Reaction, user: discord.User, logger, rate_limiter: RateLimiter):
    """Sends a DM asking the user if they want to share their workflow to OpenMuse, using safe_send_message."""
    message_link = reaction.message.jump_url
    message_content = (
        f"Thanks for sharing this workflow: {message_link}\n\n"
        "Would you be up for sharing it on [OpenMuse](<https://openmuse.ai/>) for more visibility? (\U0001F64F)"
    )
    try:
        await safe_send_message(
            bot=bot,
            channel=user, # discord.User is Messageable
            rate_limiter=rate_limiter,
            logger=logger,
            content=message_content
        )
        logger.info(f"[OpenMuseMessenger] Sent OpenMuse share request DM to {user.name} ({user.id}) for message {message_link} via safe_send_message.")
    except discord.Forbidden: # This might be less likely if safe_send_message handles it, but good for defense
        logger.warning(f"[OpenMuseMessenger] Could not send OpenMuse share request DM to {user.name} ({user.id}) - DM disabled or bot blocked (even with safe_send_message).")
    except Exception as e: # safe_send_message might raise exceptions for specific unhandled HTTP errors or timeouts
        logger.error(f"[OpenMuseMessenger] Error sending OpenMuse share request DM to {user.name} ({user.id}) using safe_send_message: {e}")
        logger.error(traceback.format_exc()) 