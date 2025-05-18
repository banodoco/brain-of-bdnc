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
        "Would you be up for sharing it on [OpenMuse](<https://openmuse.ai/>)? 🙏\n\n"
        f"This messages was triggered by {user.mention}, who hugely appreciates any OpenMuse feedback!"
    )
    try:
        await safe_send_message(
            bot=bot,
            channel=reaction.message.author, # Send to the original message author
            rate_limiter=rate_limiter,
            logger=logger,
            content=message_content
        )
        logger.info(f"[OpenMuseMessenger] Sent OpenMuse share request DM to {reaction.message.author.name} ({reaction.message.author.id}) for message {message_link} via safe_send_message.")
    except discord.Forbidden: # This might be less likely if safe_send_message handles it, but good for defense
        logger.warning(f"[OpenMuseMessenger] Could not send OpenMuse share request DM to {reaction.message.author.name} ({reaction.message.author.id}) - DM disabled or bot blocked (even with safe_send_message).")
    except Exception as e: # safe_send_message might raise exceptions for specific unhandled HTTP errors or timeouts
        logger.error(f"[OpenMuseMessenger] Error sending OpenMuse share request DM to {reaction.message.author.name} ({reaction.message.author.id}) using safe_send_message: {e}")
        logger.error(traceback.format_exc()) 