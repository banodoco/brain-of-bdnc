# src/features/logging/logger_cog.py

import asyncio
import traceback
from discord.ext import commands

class LoggerCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.logger.info("Initializing LoggerCog...")

        # Any further setup: database connections, file IO, etc.

    async def cog_load(self):
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        self.logger.info("LoggerCog is ready.")

    @commands.Cog.listener()
    async def on_message(self, message):
        """
        Example message logging. 
        If your original `MessageLogger` had more sophisticated logic,
        replicate it here (like ignoring certain channels, storing DB, etc.).
        """
        if message.author.bot:
            return  # skip bot messages if desired

        # Example: print to console or log to a file
        self.logger.info(f"Message from {message.author}: {message.content}")
        # Additional logic to store in DB, etc.
