from discord.ext import commands
from datetime import datetime, timedelta
import asyncio
import logging
from collections import deque
import traceback
from typing import Optional, Deque, Any, Dict
import os

class BaseDiscordBot(commands.Bot):
    """Base class for all Discord bots with connection monitoring."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger = kwargs.get('logger') or logging.getLogger(__name__)

        # Connection health monitoring
        self._last_heartbeat: Optional[datetime] = None
        self._connection_healthy: bool = False
        self._heartbeat_timeout: float = 60.0
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_health_check: datetime = datetime.now()
        self._health_check_lock: asyncio.Lock = asyncio.Lock()
        self._state_lock: asyncio.Lock = asyncio.Lock()

        # Session management
        self._last_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._failed_session_count: int = 0

        # Connection attempt history
        self._connection_history: Deque[datetime] = deque()
        self._connection_window: timedelta = timedelta(seconds=60)

        # Used by the summarizer scheduling to know when to shut down
        self._shutdown_flag: bool = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def setup_hook(self):
        """Called before the bot starts running."""
        try:
            loop = asyncio.get_running_loop()
            if not self._health_check_task:
                self._health_check_task = loop.create_task(self._run_health_checks())
        except Exception as e:
            self.logger.error(f"Error in setup_hook: {e}")
            raise

    async def start(self, *args, **kwargs):
        """Start the bot."""
        try:
            await super().start(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"Error in bot start: {e}")
            if self._health_check_task:
                self._health_check_task.cancel()
            raise

    async def close(self):
        """Clean up resources on shutdown."""
        try:
            async with self._state_lock:
                self._connection_healthy = False
                self._connection_history.clear()

            if self._health_check_task and not self._health_check_task.done():
                self._health_check_task.cancel()
                try:
                    await self._health_check_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self.logger.error(f"Error cancelling health check task: {str(e)}")
                    self.logger.debug(traceback.format_exc())

            # Ensure HTTP session is cleaned up
            if hasattr(self.http, '_session') and self.http._session:
                await self.http._session.close()

            await super().close()
        except Exception as e:
            self.logger.error(f"Error during bot shutdown: {str(e)}")
            self.logger.debug(traceback.format_exc())
            raise

    async def _run_health_checks(self):
        """Run periodic health checks; rely on discord.py's built-in reconnect."""
        while not self.is_closed():
            try:
                await asyncio.sleep(30)
                # Simple check if we’ve had a heartbeat recently
                if not await self._is_connection_healthy():
                    self.logger.warning(
                        "Connection appears unhealthy. Relying on discord.py's built-in reconnect mechanism."
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in health check loop: {str(e)}")
                self.logger.debug(traceback.format_exc())

    async def _is_connection_healthy(self) -> bool:
        """Check if the connection is healthy based on last heartbeat."""
        async with self._state_lock:
            if not self._last_heartbeat:
                return False
            time_since_heartbeat = (datetime.now() - self._last_heartbeat).total_seconds()
            is_healthy = time_since_heartbeat < self._heartbeat_timeout
            if not is_healthy and self._connection_healthy:
                self.logger.warning(
                    f"Connection appears unhealthy - no heartbeat for {time_since_heartbeat:.1f}s. "
                    f"Last heartbeat: {self._last_heartbeat.isoformat()}"
                )
                self._connection_healthy = False
            return is_healthy

    async def _add_connection_attempt(self):
        """Record a connection attempt in the rolling window."""
        async with self._state_lock:
            now = datetime.now()
            self._connection_history.append(now)
            while self._connection_history and now - self._connection_history[0] > self._connection_window:
                self._connection_history.popleft()

    async def _get_connection_count(self) -> int:
        """Count how many connection attempts we’ve made in the rolling window."""
        async with self._state_lock:
            now = datetime.now()
            return sum(1 for t in self._connection_history if now - t <= self._connection_window)

    async def on_socket_raw_receive(self, msg: str) -> None:
        """Track heartbeat acknowledgements."""
        if not isinstance(msg, str):
            return
        try:
            if '"op":11' in msg:  # Heartbeat ACK
                async with self._state_lock:
                    self._last_heartbeat = datetime.now()
                    self._connection_healthy = True
                    self.logger.debug(f"Heartbeat ACK received at {self._last_heartbeat.isoformat()}")
        except Exception as e:
            self.logger.error(f"Error processing socket message: {str(e)}")
            self.logger.debug(traceback.format_exc())

    async def on_socket_response(self, msg: Dict[str, Any]) -> None:
        """Handle WebSocket responses for errors/resumptions."""
        if not isinstance(msg, dict):
            return
        try:
            op_code = msg.get('op')
            event_type = msg.get('t')

            if op_code == 9:  # Invalid session
                self.logger.error(f"Invalid session detected - Full message: {msg}")
                async with self._state_lock:
                    self._connection_healthy = False
                    self._failed_session_count += 1
                    self._last_session_id = None

            elif msg.get('code') == 4004:  # Auth failure
                self.logger.critical(
                    "Authentication failed - bot token may be invalid. "
                    "Please check your token and try again."
                )
                async with self._state_lock:
                    self._connection_healthy = False
                await self.close()

            elif event_type == 'READY':  # New session
                session_id = msg.get('session_id')
                async with self._state_lock:
                    self._last_session_id = session_id
                    self._session_start_time = datetime.now()
                    self._failed_session_count = 0
                    self._connection_healthy = True
                self.logger.info(
                    f"New session established - ID: {session_id}, "
                    f"Start time: {self._session_start_time.isoformat()}"
                )

            elif event_type == 'RESUMED':
                async with self._state_lock:
                    self.logger.info(
                        f"Session resumed successfully - ID: {self._last_session_id}, "
                        f"Failed attempts: {self._failed_session_count}"
                    )
                    self._connection_healthy = True
                    self._failed_session_count = 0

        except Exception as e:
            self.logger.error(f"Error processing socket response: {str(e)}")
            self.logger.debug(f"Message that caused error: {msg}")
            self.logger.debug(traceback.format_exc())

    async def on_ready(self):
        """When the bot is fully connected."""
        self.logger.info(f"Bot {self.user} successfully connected to Discord")
        async with self._state_lock:
            if self._last_session_id:
                self.logger.info(f"Session ID: {self._last_session_id}, Connected since: {self._session_start_time}")
            self._connection_healthy = True
            self._last_heartbeat = datetime.now()

            # Initialize error handler (if you use one)
            from src.common.error_handler import ErrorHandler
            self.error_handler = ErrorHandler(self)

            # Attempt to verify admin user
            try:
                admin_id = int(os.getenv('ADMIN_USER_ID'))
                admin_user = await self.fetch_user(admin_id)
                self.logger.info(f"Successfully connected and can notify admin: {admin_user.name}")
            except Exception as e:
                self.logger.error(f"Failed to verify admin notification capability: {e}")
