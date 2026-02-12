import functools
import logging
import traceback
import os
from typing import Optional
import discord

logger = logging.getLogger('DiscordBot')

def handle_errors(operation_name: str):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {operation_name}: {e}")
                logger.debug(traceback.format_exc())
                # Skip admin DM for 429 rate limits — the DM itself would hit the
                # same rate limit, wasting API calls and prolonging the outage.
                is_rate_limit = isinstance(e, discord.HTTPException) and e.status == 429
                if not is_rate_limit and args and isinstance(args[0], (discord.Client, discord.ext.commands.Bot)):
                    bot_instance = args[0]
                    try:
                        admin_id_env = os.getenv('ADMIN_USER_ID')
                        if not admin_id_env:
                            logger.error("ADMIN_USER_ID not set. Cannot notify admin via handle_errors.")
                            raise # Re-raise original error if admin ID not set
                        admin_id = int(admin_id_env)
                        admin_user = await bot_instance.fetch_user(admin_id)
                        error_msg = f"🚨 **Critical Error in {operation_name}**\n```\n{str(e)}\n```"
                        if len(error_msg) > 1900:  # Discord message length limit
                            error_msg = error_msg[:1900] + "..."

                        # Revert to direct send to break circular import
                        await admin_user.send(error_msg)
                        logger.info(f"Admin directly notified of error in {operation_name} by handle_errors decorator.")

                    except ValueError:
                        logger.error("Invalid ADMIN_USER_ID format. Cannot notify admin via handle_errors.")
                    except discord.NotFound:
                        logger.error("Admin user not found. Cannot notify admin via handle_errors.")
                    except discord.Forbidden:
                        logger.error("Bot forbidden from DMing admin. Cannot notify admin via handle_errors.")
                    except Exception as notify_error:
                        logger.error(f"Failed to notify admin of error (via handle_errors direct send): {notify_error}")
                raise # Re-raise the original error that was caught
        return wrapper
    return decorator

class ErrorHandler:
    def __init__(self, bot: Optional[discord.Client] = None, *args, **kwargs):
        self.bot = bot
        self.logger = logging.getLogger('DiscordBot')
        
    async def notify_admin(self, error: Exception, context: str = ""):
        # This method CAN still use discord_utils.safe_send_message if needed,
        # as ErrorHandler instance is created by the bot, not imported by discord_utils.
        # However, for simplicity and to ensure it ALWAYS works even if rate_limiter is missing on bot,
        # we can also make this a direct send, or keep the current logic which has a fallback.
        # Let's keep its current logic which uses discord_utils but has a fallback.
        # For that, ErrorHandler will need the import if it wasn't global already.
        # Re-adding it here for clarity if it was removed by previous assumption.
        from src.common import discord_utils # Ensure this is here if ErrorHandler.notify_admin uses it

        try:
            if not self.bot:
                self.logger.error("Cannot notify admin: bot instance not provided to ErrorHandler")
                return
            if not hasattr(self.bot, 'fetch_user'):
                self.logger.error("Bot instance in ErrorHandler lacks fetch_user. Cannot notify admin.")
                return
                
            admin_id_str = os.getenv('ADMIN_USER_ID')
            if not admin_id_str:
                self.logger.error("ADMIN_USER_ID not set. Cannot notify admin.")
                return
            admin_id = int(admin_id_str)
            admin_user = await self.bot.fetch_user(admin_id)
            
            error_msg = f"🚨 **Critical Error**\n"
            if context: error_msg += f"**Context:** {context}\n"
            error_msg += f"**Error:** {str(error)}\n"
            error_msg += f"```\n{traceback.format_exc()[:1500]}...\n```"
            
            if not hasattr(self.bot, 'rate_limiter'):
                self.logger.error("Rate limiter not found on self.bot for ErrorHandler.notify_admin. Sending directly.")
                await admin_user.send(error_msg)
            else:
                await discord_utils.safe_send_message(
                    self.bot,
                    admin_user,
                    self.bot.rate_limiter,
                    self.logger,
                    content=error_msg
                )
        except ValueError:
            self.logger.error("Invalid ADMIN_USER_ID. Cannot notify admin.")
        except discord.NotFound:
            self.logger.error("Admin user not found. Cannot notify admin.")
        except discord.Forbidden:
            self.logger.error("Bot is forbidden from DMing admin user. Cannot notify admin.")
        except Exception as e:
            self.logger.error(f"Failed to send error notification to admin (via ErrorHandler.notify_admin): {e}", exc_info=True) 