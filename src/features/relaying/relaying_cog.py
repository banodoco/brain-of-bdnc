import discord
from discord.ext import commands
import logging
import asyncio
from aiohttp import web
import json
import os # Added for environment variables

# Import the core logic class
from .relayer import Relayer

# Configuration
# Use environment variables or fall back to defaults
WEBHOOK_HOST = os.getenv('RELAY_WEBHOOK_HOST', '0.0.0.0')  # Listen on all interfaces by default
WEBHOOK_PORT = int(os.getenv('RELAY_WEBHOOK_PORT', 8080)) # Default port 8080
# Load the expected auth token from environment variables
EXPECTED_AUTH_TOKEN = os.getenv('RELAY_AUTH_TOKEN')

class RelayingCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.web_server_task = None
        self.runner = None
        # Relayer instance will be set during cog_load after it's created in main.py
        self.relayer_instance: Relayer | None = None
        # Load the expected auth token from environment
        self.expected_auth_token = os.getenv('RELAY_AUTH_TOKEN')
        if not self.expected_auth_token:
             self.logger.warning("[RelayingCog] RELAY_AUTH_TOKEN not set in environment! Webhook will be insecure.")
        else:
             self.logger.info("[RelayingCog] RELAY_AUTH_TOKEN loaded.")

        self.logger.info(f"Initializing RelayingCog. Webhook server pending startup. Dev Mode: {self.dev_mode}")

    async def cog_load(self):
        """Called when the Cog is loaded. Checks for Relayer instance and starts the web server."""
        # Check if the bot has the relayer instance created in main.py
        if not hasattr(self.bot, 'relayer_instance') or self.bot.relayer_instance is None:
            self.logger.error("[RelayingCog] Relayer instance not found on bot object during RelayingCog load! Web server NOT started.")
            # Decide if you want to prevent cog loading or just log the error
            # raise commands.ExtensionFailed("RelayingCog requires a Relayer instance on the bot object.")
            return # Don't start the server if the instance is missing
        else:
            self.relayer_instance = self.bot.relayer_instance
            self.logger.info("[RelayingCog] Relayer instance found on bot object.")
            # Start the web server after the relayer instance is confirmed
            self.web_server_task = asyncio.create_task(self.start_webhook_server())
            self.logger.info("[RelayingCog] Web server startup task created.")

    async def cog_unload(self):
        """Cleanly shuts down the web server when the cog is unloaded."""
        if self.web_server_task and not self.web_server_task.done():
            self.logger.info("[RelayingCog] Attempting to shut down webhook server...")
            self.web_server_task.cancel()
            try:
                # Give cancellation a chance to propagate
                await asyncio.wait_for(self.web_server_task, timeout=5.0) 
            except asyncio.CancelledError:
                self.logger.info("[RelayingCog] Web server task cancelled successfully.")
            except asyncio.TimeoutError:
                self.logger.warning("[RelayingCog] Web server task did not cancel within timeout during unload.")
            except Exception as e:
                 self.logger.error(f"[RelayingCog] Error during web_server_task await on unload: {e}", exc_info=True)
                 
            # Ensure runner cleanup happens even if the task had issues
            if self.runner:
                 try:
                     await self.runner.cleanup()
                     self.logger.info("[RelayingCog] Web server runner cleaned up.")
                 except Exception as e:
                     self.logger.error(f"[RelayingCog] Error cleaning up web server runner: {e}", exc_info=True)
        else:
             self.logger.info("[RelayingCog] Web server task was not running or already finished during unload.")

    async def start_webhook_server(self):
        """Starts the aiohttp web server to listen for incoming webhooks."""
        app = web.Application(logger=self.logger) # Pass logger for better visibility
        app.router.add_post('/webhook/openmuse_featuring', self.handle_openmuse_featuring)
        # Add more routes here for other webhooks, e.g.:
        # app.router.add_post('/webhook/another_action', self.handle_another_action)

        self.runner = web.AppRunner(app)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, WEBHOOK_HOST, WEBHOOK_PORT)
            await site.start()
            self.logger.info(f"[RelayingCog] Webhook server started successfully on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}")
            # Keep the server running indefinitely until the task is cancelled
            await asyncio.Future() # This waits forever
        except asyncio.CancelledError:
            self.logger.info("[RelayingCog] Webhook server stopping due to cancellation request.")
            # Cancellation is handled, cleanup happens in finally
        except OSError as e:
            # Specifically catch address-in-use errors
            if e.errno == 98: # errno.EADDRINUSE
                 self.logger.error(f"[RelayingCog] Failed to start webhook server: Port {WEBHOOK_PORT} is already in use on {WEBHOOK_HOST}.")
            else:
                 self.logger.error(f"[RelayingCog] Failed to start webhook server due to OS error: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"[RelayingCog] An unexpected error occurred during webhook server startup or runtime: {e}", exc_info=True)
        finally:
            self.logger.info("[RelayingCog] Starting web server cleanup...")
            if self.runner:
                 await self.runner.cleanup()
                 self.logger.info("[RelayingCog] Web server runner cleaned up.")
            else:
                 self.logger.info("[RelayingCog] No runner instance to clean up.")

    async def handle_openmuse_featuring(self, request: web.Request):
        """Handles incoming POST requests for the 'openmuse_featuring' webhook."""
        remote_ip = request.remote
        self.logger.debug(f"[RelayingCog] Received POST request on /webhook/openmuse_featuring from {remote_ip}")

        # --- Authentication Check --- Added
        if self.expected_auth_token:
            auth_header = request.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Bearer '):
                self.logger.warning(f"[RelayingCog] Missing or invalid Authorization header from {remote_ip}.")
                return web.Response(status=401, text="Unauthorized: Missing or invalid Authorization header.")
            
            provided_token = auth_header.split('Bearer ', 1)[1]
            if provided_token != self.expected_auth_token:
                 self.logger.warning(f"[RelayingCog] Invalid token received from {remote_ip}.")
                 # Do not reveal token details in the error message
                 return web.Response(status=401, text="Unauthorized: Invalid token.")
            self.logger.debug(f"[RelayingCog] Authentication successful for request from {remote_ip}.")
        else:
             # If no token is configured in .env, log a warning but allow the request (adjust if needed)
             self.logger.warning(f"[RelayingCog] No RELAY_AUTH_TOKEN configured. Allowing unauthenticated request from {remote_ip}. This is insecure!")
             # To enforce authentication even if the token isn't set, uncomment the next line:
             # return web.Response(status=500, text="Internal Server Error: Authentication token not configured.")

        # Check if relayer instance is available *after* authentication
        if not self.relayer_instance:
             self.logger.error("[RelayingCog] Relayer instance not available. Cannot handle webhook request.")
             # 503 Service Unavailable is appropriate here
             return web.Response(status=503, text="Service Unavailable: Relayer component not ready.")

        # --- Payload Handling --- 
        try:
            # Check content type before parsing
            if request.content_type != 'application/json':
                 self.logger.warning(f"[RelayingCog] Received non-JSON request ({request.content_type}) on /webhook/openmuse_featuring from {remote_ip}. Denying.")
                 return web.Response(status=415, text="Unsupported Media Type: Expected application/json")
                 
            data = await request.json()
            self.logger.debug(f"[RelayingCog] Webhook payload received from {remote_ip}: {data}")
        except json.JSONDecodeError:
            self.logger.warning(f"[RelayingCog] Invalid JSON received from {remote_ip} on /webhook/openmuse_featuring.")
            return web.Response(status=400, text="Bad Request: Invalid JSON format.")
        except Exception as e:
            # Catch potential errors during body reading
            self.logger.error(f"[RelayingCog] Error reading request body from {remote_ip}: {e}", exc_info=True)
            return web.Response(status=400, text="Bad Request: Could not read request payload.")

        # --- Data Validation --- 
        username = data.get('username')
        content_type = data.get('type') # Key is 'type' as requested
        url = data.get('url')

        # Check for presence and basic type (string)
        missing_or_invalid = []
        if not username or not isinstance(username, str):
             missing_or_invalid.append("username (string)")
        if not content_type or not isinstance(content_type, str):
             missing_or_invalid.append("type (string)")
        if not url or not isinstance(url, str):
            missing_or_invalid.append("url (string)")
            
        # Add more specific validation if needed (e.g., URL format)

        if missing_or_invalid:
            error_msg = f"Bad Request: Missing or invalid fields - {', '.join(missing_or_invalid)}"
            self.logger.warning(f"[RelayingCog] {error_msg} in payload from {remote_ip}: {data}")
            return web.Response(status=400, text=error_msg)

        # --- Action Dispatch --- 
        try:
            # Call the Relayer action method asynchronously without blocking the response
            asyncio.create_task(self.relayer_instance.action_openmuse_featuring(username, content_type, url))
            self.logger.info(f"[RelayingCog] Task created to handle 'openmuse_featuring' action for user '{username}' from {remote_ip}.")

            # Respond immediately to the webhook sender
            # 202 Accepted: Request received, processing initiated (but not necessarily complete)
            return web.Response(status=202, text="Accepted")
        except Exception as e:
             # This catch is unlikely if create_task is used, but good practice
             self.logger.error(f"[RelayingCog] Unexpected error dispatching task for 'openmuse_featuring' from {remote_ip}: {e}", exc_info=True)
             return web.Response(status=500, text="Internal Server Error: Failed to process request.")

# Setup function required by discord.py to load the cog
async def setup(bot):
    # Ideally, get logger and dev_mode from a shared bot attribute or config
    logger = getattr(bot, 'logger', logging.getLogger('RelayingCog'))
    dev_mode = getattr(bot, 'dev_mode', False)
    
    # Instantiate the Cog and add it to the bot
    await bot.add_cog(RelayingCog(bot, logger, dev_mode))
    logger.info("RelayingCog has been loaded.") 