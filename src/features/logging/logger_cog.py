# src/features/logging/logger_cog.py

import asyncio
import traceback
import json
from discord.ext import commands
from src.common.db_handler import DatabaseHandler
import discord
import os

class LoggerCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        if dev_mode:
            self.logger.info(f"Initializing LoggerCog in development mode")
            self.logger.debug(f"Bot intents enabled: {bot.intents}")
        self.db = DatabaseHandler(dev_mode=dev_mode)
        try:
            self.bot_user_id = int(os.getenv('BOT_USER_ID'))
            self.logger.debug(f"Retrieved BOT_USER_ID: {self.bot_user_id}")
        except Exception as e:
            self.logger.error(f"Error retrieving BOT_USER_ID: {e}")
            self.bot_user_id = None

    async def cog_load(self):
        if self.dev_mode:
            self.logger.debug("Logger cog loaded")
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.dev_mode:
            self.logger.info("LoggerCog is ready")
            self.logger.debug(f"Message events enabled: {self.bot.intents.message_content}")
            self.logger.debug(f"Reaction events enabled: {self.bot.intents.reactions}")
            self.logger.debug(f"Guild reaction events enabled: {self.bot.intents.guild_reactions}")

    # @commands.Cog.listener()
    # async def on_reaction_add(self, reaction, user):
    #     """Handle reaction add events"""
    #     try:
    #         self.logger.info(f"Reaction add detected - Emoji: {reaction.emoji}, User: {user.name} ({user.id}), Message: {reaction.message.id}")
            
    #         # Ignore bot reactions
    #         if user.bot:
    #             self.logger.debug(f"Ignoring reaction from bot user: {user.id}")
    #             return

    #         # Get current message data from database
    #         try:
    #             self.logger.debug(f"Querying database for message {reaction.message.id}")
    #             results = self.db.execute_query("""
    #                 SELECT reaction_count, reactors
    #                 FROM messages
    #                 WHERE message_id = ?
    #             """, (reaction.message.id,))

    #             if not results:
    #                 self.logger.warning(f"Message {reaction.message.id} not found in database for reaction update")
    #                 return

    #             self.logger.debug(f"Database query results: {results[0]}")
    #             current_count = results[0].get('reaction_count', 0) or 0
    #             current_reactors_json = results[0].get('reactors')
    #             current_reactors = json.loads(current_reactors_json) if current_reactors_json else []
                
    #             self.logger.debug(f"Current reaction state - Count: {current_count}, Reactors: {current_reactors}")

    #             # Add new reactor if not already in list
    #             if user.id not in current_reactors:
    #                 current_reactors.append(user.id)
    #                 self.logger.debug(f"Added new reactor {user.id} to reactors list")

    #             # Update database
    #             self.logger.debug(f"Updating database - New count: {current_count + 1}, New reactors: {current_reactors}")
    #             self.db.execute_query("""
    #                 UPDATE messages
    #                 SET reaction_count = ?, reactors = ?
    #                 WHERE message_id = ?
    #             """, (current_count + 1, json.dumps(current_reactors), reaction.message.id))
    #             self.logger.info(f"Successfully updated reaction in database for message {reaction.message.id}")

    #         except Exception as e:
    #             self.logger.error(f"Error updating reaction: {str(e)}")
    #             self.logger.error(traceback.format_exc())

    #     except Exception as e:
    #         self.logger.error(f"Error handling reaction add: {str(e)}")
    #         self.logger.error(traceback.format_exc())

    # @commands.Cog.listener()
    # async def on_reaction_remove(self, reaction, user):
    #     """Handle reaction remove events"""
    #     try:
    #         self.logger.info(f"Reaction remove detected - Emoji: {reaction.emoji}, User: {user.name} ({user.id}), Message: {reaction.message.id}")
            
    #         # Ignore bot reactions
    #         if user.bot:
    #             self.logger.debug(f"Ignoring reaction removal from bot user: {user.id}")
    #             return

    #         # Get current message data from database
    #         try:
    #             self.logger.debug(f"Querying database for message {reaction.message.id}")
    #             results = self.db.execute_query("""
    #                 SELECT reaction_count, reactors
    #                 FROM messages
    #                 WHERE message_id = ?
    #             """, (reaction.message.id,))

    #             if not results:
    #                 self.logger.warning(f"Message {reaction.message.id} not found in database for reaction removal")
    #                 return

    #             self.logger.debug(f"Database query results: {results[0]}")
    #             current_count = results[0].get('reaction_count', 0) or 0
    #             current_reactors_json = results[0].get('reactors')
    #             current_reactors = json.loads(current_reactors_json) if current_reactors_json else []
                
    #             self.logger.debug(f"Current reaction state - Count: {current_count}, Reactors: {current_reactors}")

    #             # Remove reactor if present
    #             if user.id in current_reactors:
    #                 current_reactors.remove(user.id)
    #                 self.logger.debug(f"Removed reactor {user.id} from reactors list")

    #             # Update database
    #             self.logger.debug(f"Updating database - New count: {max(0, current_count - 1)}, New reactors: {current_reactors}")
    #             self.db.execute_query("""
    #                 UPDATE messages
    #                 SET reaction_count = ?, reactors = ?
    #                 WHERE message_id = ?
    #             """, (max(0, current_count - 1), json.dumps(current_reactors), reaction.message.id))
    #             self.logger.info(f"Successfully updated reaction removal in database for message {reaction.message.id}")

    #         except Exception as e:
    #             self.logger.error(f"Error updating reaction removal: {str(e)}")
    #             self.logger.error(traceback.format_exc())

    #     except Exception as e:
    #         self.logger.error(f"Error handling reaction remove: {str(e)}")
    #         self.logger.error(traceback.format_exc())

    async def _update_reaction(self, reaction, user, action: str):
        """Helper method to add or remove a reaction from the database."""
        if user.bot:
            return

        try:
            results = self.db.execute_query(
                "SELECT reaction_count, reactors FROM messages WHERE message_id = ?",
                (reaction.message.id,)
            )

            if not results:
                if self.dev_mode:
                    self.logger.warning(f"Message {reaction.message.id} not found in database for reaction update")
                return

            message_data = results[0]
            current_count = message_data.get('reaction_count', 0) or 0
            
            # Robustly load reactors, handling double-encoded JSON for old data
            reactors_raw = message_data.get('reactors')
            current_reactors = []
            if isinstance(reactors_raw, str):
                try:
                    loaded_reactors = json.loads(reactors_raw)
                    if isinstance(loaded_reactors, str):
                        # Handle double encoding
                        current_reactors = json.loads(loaded_reactors)
                    else:
                        current_reactors = loaded_reactors
                except json.JSONDecodeError:
                    self.logger.warning(f"Could not decode reactors JSON for message {reaction.message.id}: {reactors_raw}")
                    current_reactors = []
            elif isinstance(reactors_raw, list):
                current_reactors = reactors_raw

            if not isinstance(current_reactors, list):
                self.logger.warning(f"Reactors for message {reaction.message.id} is not a list, resetting.")
                current_reactors = []

            # Perform the requested action - only update if there's an actual change
            changed = False
            if action == 'add':
                if user.id not in current_reactors:
                    current_reactors.append(user.id)
                    changed = True
            elif action == 'remove':
                if user.id in current_reactors:
                    current_reactors.remove(user.id)
                    changed = True
            else:
                return # Invalid action

            # Only update database if there was an actual change
            if changed:
                new_count = len(current_reactors)
                self.db.execute_query(
                    "UPDATE messages SET reaction_count = ?, reactors = ? WHERE message_id = ?",
                    (new_count, json.dumps(current_reactors), reaction.message.id)
                )
                
                # Only log in dev mode
                if self.dev_mode:
                    self.logger.debug(f"[LoggerCog] Updated reaction {action} for message {reaction.message.id}")

        except Exception as e:
            self.logger.error(f"[LoggerCog] Error in _update_reaction (action: {action}): {e}", exc_info=True)

    # --- Public methods for ReactorCog to call --- 
    async def log_reaction_add(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction additions."""
        await self._update_reaction(reaction, user, 'add')

    async def log_reaction_remove(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction removals."""
        await self._update_reaction(reaction, user, 'remove')

    # Real-time message logging disabled - now using hourly batch processing
    # @commands.Cog.listener()
    # async def on_message(self, message):
    #     """Called when a message is sent in any channel the bot can see."""
    #     try:
    #         # Skip DMs
    #         if not message.guild:
    #             return

    #         # Ignore messages from the bot itself or the configured bot user
    #         if message.author == self.bot.user:
    #             return

    #         # Check if _prepare_message_data exists before calling
    #         if not hasattr(self, '_prepare_message_data'):
    #             self.logger.error(f"[LoggerCog] CRITICAL: _prepare_message_data method not found!")
    #             return
                
    #         message_data = await self._prepare_message_data(message)
    #         await self.db.store_messages([message_data]) 
            
    #         # Only log in dev mode or for errors
    #         if self.dev_mode:
    #             self.logger.debug(f"[LoggerCog] Logged message {message.id} from {message.author.name}")

    #     except AttributeError as ae:
    #          self.logger.error(f"[LoggerCog] AttributeError in on_message for message {message.id}: {ae}")
    #     except Exception as e:
    #         self.logger.error(f"[LoggerCog] Unexpected error logging message {message.id}: {e}")

    async def _prepare_message_data(self, message: discord.Message) -> dict:
        """Convert a discord message into a format suitable for database storage."""
        try:
            # Calculate total reaction count
            reaction_count = sum(reaction.count for reaction in message.reactions) if message.reactions else 0
            
            # Get list of unique reactors
            reactors = []
            if message.reactions:
                for reaction in message.reactions:
                    async for user in reaction.users():
                        if user.id not in reactors and user.id != self.bot_user_id:
                            reactors.append(user.id)
            
            # Handle thread_id with logging
            thread_id = None
            try:
                if hasattr(message, 'thread') and message.thread:
                    thread_id = message.thread.id
                    self.logger.debug(f"Found thread_id {thread_id} for message {message.id}")
                elif message.channel and isinstance(message.channel, discord.Thread):
                    thread_id = message.channel.id
                    self.logger.debug(f"Message {message.id} is in thread {thread_id}")
            except Exception as e:
                self.logger.debug(f"Error getting thread_id for message {message.id}: {e}")
            
            # Get guild display name (nickname) if available
            display_name = None
            global_name = message.author.global_name
            try:
                if hasattr(message, 'guild') and message.guild:
                    member = message.guild.get_member(message.author.id)
                    if member:
                        display_name = member.nick
            except Exception as e:
                self.logger.debug(f"Error getting display name for user {message.author.id}: {e}")
            
            # Get category ID if available
            category_id = None
            if hasattr(message.channel, 'category') and message.channel.category:
                category_id = message.channel.category.id
            
            return {
                'id': message.id,
                'message_id': message.id,
                'channel_id': message.channel.id,
                'channel_name': message.channel.name,
                'author_id': message.author.id,
                'author_name': message.author.name,
                'author_discriminator': message.author.discriminator,
                'author_avatar_url': str(message.author.avatar.url) if message.author.avatar else None,
                'content': message.content,
                'created_at': message.created_at.isoformat(),
                'attachments': [
                    {
                        'url': attachment.url,
                        'filename': attachment.filename
                    } for attachment in message.attachments
                ],
                'embeds': [embed.to_dict() for embed in message.embeds],
                'reaction_count': reaction_count,
                'reactors': reactors,
                'reference_id': message.reference.message_id if message.reference else None,
                'edited_at': message.edited_at.isoformat() if message.edited_at else None,
                'is_pinned': message.pinned,
                'thread_id': thread_id,
                'message_type': str(message.type),
                'flags': message.flags.value,
                'is_deleted': False,
                'display_name': display_name,
                'global_name': global_name,
                'category_id': category_id
            }
        except Exception as e:
            self.logger.error(f"Error preparing message data: {e}")
            raise

async def setup(bot: commands.Bot):
    """Sets up the LoggerCog."""
    # Ensure logger and dev_mode are available on the bot instance
    if not hasattr(bot, 'logger'):
        print("ERROR: Logger not found on bot object. Cannot load LoggerCog.")
        return
    if not hasattr(bot, 'dev_mode'):
         print("ERROR: dev_mode attribute not found on bot object. Cannot load LoggerCog.")
         return

    # Retrieve logger and dev_mode from the bot instance
    logger = bot.logger
    dev_mode = bot.dev_mode
    
    await bot.add_cog(LoggerCog(bot, logger, dev_mode=dev_mode))
    logger.info("LoggerCog added to bot.")
