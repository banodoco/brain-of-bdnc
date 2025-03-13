# src/features/logging/logger_cog.py

import asyncio
import traceback
from discord.ext import commands
from src.common.db_handler import DatabaseHandler

class LoggerCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        if dev_mode:
            self.logger.info(f"Initializing LoggerCog in development mode")
            self.logger.debug(f"Bot intents enabled: {bot.intents}")
        self.db = DatabaseHandler(dev_mode=dev_mode)
        if dev_mode:
            self.logger.debug(f"Database initialized with path: {self.db.db_path}")

    async def cog_load(self):
        if self.dev_mode:
            self.logger.debug("Logger cog loaded")
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.dev_mode:
            self.logger.info("LoggerCog is ready")
            self.logger.debug(f"Message events enabled: {self.bot.intents.message_content}")

    @commands.Cog.listener()
    async def on_message(self, message):
        """
        Example message logging. 
        If your original `MessageLogger` had more sophisticated logic,
        replicate it here (like ignoring certain channels, storing DB, etc.).
        """
        if message.author.bot:
            if self.dev_mode:
                self.logger.debug("Skipping bot message")
            return  # skip bot messages if desired

        if self.dev_mode:
            self.logger.debug(f"Message channel: {message.channel.name} ({message.channel.id})")

        # Log message content in dev mode only
        if self.dev_mode:
            self.logger.info(f"Message from {message.author}: {message.content}")

        # Prepare message data for DB storage
        message_data = {
            'message_id': message.id,
            'channel_id': message.channel.id,
            'author_id': message.author.id,
            'content': message.content,
            'created_at': message.created_at,
            'attachments': [{'url': a.url, 'filename': a.filename} for a in message.attachments],
            'embeds': [embed.to_dict() for embed in message.embeds]
        }

        # Store in database
        try:
            if self.dev_mode:
                self.logger.debug(f"Storing message {message.id}")
            self.db.store_messages([message_data])
        except Exception as e:
            self.logger.error(f"Failed to store message {message.id} in database: {e}")
            self.logger.error(traceback.format_exc())  # Add full traceback for debugging

        # Additional logic to store in DB, etc.
