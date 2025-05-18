# src/common/base_bot.py

import asyncio
import logging
from datetime import datetime
from typing import Optional, Any, Dict
import random
import traceback
import os

import discord
from discord.ext import commands

from src.common.rate_limiter import RateLimiter

class BaseDiscordBot(commands.Bot):
    """
    Base class for all Discord bots, relying on discord.py's built-in
    heartbeat and reconnection logic rather than manual heartbeat checks.
    """

    def __init__(self, command_prefix, logger, dev_mode=False, intents=None, **kwargs):
        super().__init__(command_prefix=command_prefix, intents=intents, **kwargs)
        self.logger = logger
        self.dev_mode = dev_mode
        self.summary_now = False
        self.rate_limiter = RateLimiter()

        # Session management (optional, if you want to track session IDs):
        self._last_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._failed_session_count: int = 0

        # Summarizer or other cogs might set this if needed
        self._shutdown_flag: bool = False

        # For Summarizer Cog to track if we've run the immediate summary
        self.summarizer_ready = False

    async def setup_hook(self):
        """Called when the bot is starting up."""
        # Add the sync command
        @self.command()
        @commands.is_owner()
        async def sync(ctx):
            """Force sync slash commands."""
            try:
                synced = await self.tree.sync()
                await ctx.send(f"Synced {len(synced)} command(s) globally")
                
                if self.dev_mode:
                    for guild in self.guilds:
                        guild_synced = await self.tree.sync(guild=guild)
                        await ctx.send(f"Synced {len(guild_synced)} command(s) to guild {guild.id}")
            except Exception as e:
                await ctx.send(f"Failed to sync commands: {e}")

    async def start(self, *args, **kwargs):
        """Start the bot."""
        try:
            await super().start(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"Error in bot start: {e}")
            raise

    async def close(self):
        """Clean up resources on shutdown."""
        try:
            # Ensure HTTP session is cleaned up (if it's still open).
            if hasattr(self.http, "_session") and self.http._session:
                await self.http._session.close()

            await super().close()
        except Exception as e:
            self.logger.error(f"Error during bot shutdown: {str(e)}")
            self.logger.debug(traceback.format_exc())
            raise

    # -------------------------------------------------------------------------
    # The following method is optional. It logs certain gateway events
    # (op=9, code=4004, etc.), but *no longer* forces reconnections or modifies
    # your bot's connection state. You can remove this entire method if you
    # don't need these logs.
    # -------------------------------------------------------------------------
    async def on_socket_response(self, msg: Dict[str, Any]) -> None:
        """Handle WebSocket responses for errors/resumptions."""
        if not isinstance(msg, dict):
            return
        try:
            op_code = msg.get("op")
            event_type = msg.get("t")

            # Log invalid session
            if op_code == 9:  # Invalid session
                self.logger.error(f"Invalid session detected - Full message: {msg}")
                self._failed_session_count += 1
                self._last_session_id = None

            # Log auth failure
            elif msg.get("code") == 4004:  # Auth failure
                self.logger.critical(
                    "Authentication failed - bot token may be invalid. "
                    "Please check your token and try again."
                )
                await self.close()

            # Log new session
            elif event_type == "READY":
                session_id = msg.get("session_id")
                self._last_session_id = session_id
                self._session_start_time = datetime.now()
                self._failed_session_count = 0
                self.logger.info(
                    f"New session established - ID: {session_id}, "
                    f"Start time: {self._session_start_time.isoformat()}"
                )

            # Log resumed session
            elif event_type == "RESUMED":
                self.logger.info(
                    f"Session resumed successfully - ID: {self._last_session_id}, "
                    f"Failed attempts: {self._failed_session_count}"
                )
                self._failed_session_count = 0

        except Exception as e:
            self.logger.error(f"Error processing socket response: {str(e)}")
            self.logger.debug(f"Message that caused error: {msg}")
            self.logger.debug(traceback.format_exc())

    async def on_ready(self):
        """Called when the bot is ready."""
        self.logger.info(f"Bot is ready! Logged in as {self.user.name} (ID: {self.user.id})")
        self.logger.info(f"Dev mode: {self.dev_mode}")
        self.logger.info(f"Connected to {len(self.guilds)} guilds")
        # Initialize error handler (if you have a custom one)
        try:
            from src.common.error_handler import ErrorHandler
            self.error_handler = ErrorHandler(self)
        except ImportError:
            self.logger.warning("No custom error_handler found or import failed.")

        # Attempt to verify admin user
        try:
            admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
            if admin_id != 0:
                admin_user = await self.fetch_user(admin_id)
                self.logger.info(f"Successfully connected and can notify admin: {admin_user.name}")
        except Exception as e:
            self.logger.error(f"Failed to verify admin notification capability: {e}")

    async def cleanup(self) -> None:
        """Perform any necessary cleanup. Default implementation does nothing."""
        pass

    def is_connected(self) -> bool:
        """
        Return True if the bot has an active websocket connection.
        This is a lightweight utility, but note that discord.py
        handles reconnections automatically, so checking this
        is usually only for debug or informational purposes.
        """
        if not hasattr(self, "ws") or self.ws is None:
            return False
        # Try to use the is_closed() method if available
        is_closed_method = getattr(self.ws, "is_closed", None)
        if callable(is_closed_method):
            return not is_closed_method()
        # Fallback: check a 'close_code' attribute if available
        if hasattr(self.ws, "close_code"):
            return self.ws.close_code is None
        # If no reliable attribute, assume connected
        return True

    @staticmethod
    def generate_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
        """Generate a Discord jump URL for a message."""
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
