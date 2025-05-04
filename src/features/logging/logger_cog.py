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
        if dev_mode:
            self.logger.debug(f"Database initialized with path: {self.db.db_path}")
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

    # --- Public methods for ReactorCog to call --- 
    async def log_reaction_add(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction additions."""
        # This method replicates the logic from the original on_reaction_add listener
        try:
            self.logger.debug(f"[LoggerCog] log_reaction_add called for Emoji: {reaction.emoji}, User: {user.id}, Message: {reaction.message.id}")
            # Ignore bot reactions (redundant if ReactorCog already checks, but safe)
            if user.bot:
                return

            # Get current message data from database
            # ... (rest of the logic from the original on_reaction_add) ...
            self.logger.debug(f"Querying database for message {reaction.message.id}")
            results = self.db.execute_query("""
                SELECT reaction_count, reactors
                FROM messages
                WHERE message_id = ?
            """, (reaction.message.id,))

            if not results:
                self.logger.warning(f"Message {reaction.message.id} not found in database for reaction update")
                return

            current_count = results[0].get('reaction_count', 0) or 0
            current_reactors_json = results[0].get('reactors')
            current_reactors = json.loads(current_reactors_json) if current_reactors_json else []
            
            if user.id not in current_reactors:
                current_reactors.append(user.id)
                self.logger.debug(f"Added new reactor {user.id} to reactors list")

            self.db.execute_query("""
                UPDATE messages
                SET reaction_count = ?, reactors = ?
                WHERE message_id = ?
            """, (current_count + 1, json.dumps(current_reactors), reaction.message.id))
            self.logger.info(f"[LoggerCog] Successfully updated reaction add in DB for message {reaction.message.id}")

        except Exception as e:
            self.logger.error(f"[LoggerCog] Error in log_reaction_add: {str(e)}")
            self.logger.error(traceback.format_exc())

    async def log_reaction_remove(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction removals."""
        # This method replicates the logic from the original on_reaction_remove listener
        try:
            self.logger.debug(f"[LoggerCog] log_reaction_remove called for Emoji: {reaction.emoji}, User: {user.id}, Message: {reaction.message.id}")
            # Ignore bot reactions
            if user.bot:
                return

            # Get current message data from database
            # ... (rest of the logic from the original on_reaction_remove) ...
            self.logger.debug(f"Querying database for message {reaction.message.id}")
            try:
                self.logger.debug(f"Querying database for message {reaction.message.id}")
                results = self.db.execute_query("""
                    SELECT reaction_count, reactors
                    FROM messages
                    WHERE message_id = ?
                """, (reaction.message.id,))

                if not results:
                    self.logger.warning(f"Message {reaction.message.id} not found in database for reaction update")
                    return

                self.logger.debug(f"Database query results: {results[0]}")
                current_count = results[0].get('reaction_count', 0) or 0
                current_reactors_json = results[0].get('reactors')
                current_reactors = json.loads(current_reactors_json) if current_reactors_json else []
                
                self.logger.debug(f"Current reaction state - Count: {current_count}, Reactors: {current_reactors}")

                # Add new reactor if not already in list
                if user.id not in current_reactors:
                    current_reactors.append(user.id)
                    self.logger.debug(f"Added new reactor {user.id} to reactors list")

                # Update database
                self.logger.debug(f"Updating database - New count: {current_count + 1}, New reactors: {current_reactors}")
                self.db.execute_query("""
                    UPDATE messages
                    SET reaction_count = ?, reactors = ?
                    WHERE message_id = ?
                """, (current_count + 1, json.dumps(current_reactors), reaction.message.id))
                self.logger.info(f"Successfully updated reaction in database for message {reaction.message.id}")

            except Exception as e:
                self.logger.error(f"Error updating reaction: {str(e)}")
                self.logger.error(traceback.format_exc())

        except Exception as e:
            self.logger.error(f"Error handling reaction add: {str(e)}")
            self.logger.error(traceback.format_exc())

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        """Handle reaction remove events"""
        try:
            self.logger.info(f"Reaction remove detected - Emoji: {reaction.emoji}, User: {user.name} ({user.id}), Message: {reaction.message.id}")
            
            # Ignore bot reactions
            if user.bot:
                self.logger.debug(f"Ignoring reaction removal from bot user: {user.id}")
                return

            # Get current message data from database
            try:
                self.logger.debug(f"Querying database for message {reaction.message.id}")
                results = self.db.execute_query("""
                    SELECT reaction_count, reactors
                    FROM messages
                    WHERE message_id = ?
                """, (reaction.message.id,))

                if not results:
                    self.logger.warning(f"Message {reaction.message.id} not found in database for reaction removal")
                    return

                self.logger.debug(f"Database query results: {results[0]}")
                current_count = results[0].get('reaction_count', 0) or 0
                current_reactors_json = results[0].get('reactors')
                current_reactors = json.loads(current_reactors_json) if current_reactors_json else []
                
                self.logger.debug(f"Current reaction state - Count: {current_count}, Reactors: {current_reactors}")

                # Remove reactor if present
                if user.id in current_reactors:
                    current_reactors.remove(user.id)
                    self.logger.debug(f"Removed reactor {user.id} from reactors list")

                # Update database
                self.logger.debug(f"Updating database - New count: {max(0, current_count - 1)}, New reactors: {current_reactors}")
                self.db.execute_query("""
                    UPDATE messages
                    SET reaction_count = ?, reactors = ?
                    WHERE message_id = ?
                """, (max(0, current_count - 1), json.dumps(current_reactors), reaction.message.id))
                self.logger.info(f"Successfully updated reaction removal in database for message {reaction.message.id}")

            except Exception as e:
                self.logger.error(f"Error updating reaction removal: {str(e)}")
                self.logger.error(traceback.format_exc())

        except Exception as e:
            self.logger.error(f"Error handling reaction remove: {str(e)}")
            self.logger.error(traceback.format_exc())

    @commands.Cog.listener()
    async def on_message(self, message):
        """Called when a message is sent in any channel the bot can see."""
        self.logger.debug(f"[LoggerCog] on_message triggered for message ID: {message.id} in channel {message.channel.id} by author {message.author.id}")
        try:
            # Ignore messages from the bot itself or the configured bot user
            # TODO: Add bot_user_id check similar to logger.py if needed
            if message.author == self.bot.user: # Use self.bot.user here
                self.logger.debug(f"[LoggerCog] Ignoring message from self (bot user: {self.bot.user.id})")
                return

            # TODO: Implement skip_channels check logic here
            self.logger.debug(f"[LoggerCog] Preparing message data for message {message.id}")
            # Check if _prepare_message_data exists before calling
            if not hasattr(self, '_prepare_message_data'):
                self.logger.error(f"[LoggerCog] CRITICAL: _prepare_message_data method not found on LoggerCog instance!")
                # Optionally, try to dynamically get it from somewhere if that's the intended design,
                # but it's likely a structural issue that needs fixing.
                # For now, just log the error and return to prevent crashing the listener.
                return
                
            message_data = await self._prepare_message_data(message) # This call will likely fail
            self.logger.debug(f"[LoggerCog] Message data prepared for message {message.id}. Keys: {list(message_data.keys())}")

            self.logger.debug(f"[LoggerCog] Storing message {message.id} using DB handler: {self.db}")
            self.db.store_messages([message_data]) 
            self.logger.info(f"[LoggerCog] Successfully logged message {message.id} from {message.author.name} in #{message.channel.name}")

        except AttributeError as ae:
             self.logger.error(f"[LoggerCog] AttributeError in on_message for message {message.id}: {ae}")
             self.logger.error(traceback.format_exc()) # Log the full traceback for AttributeError
        except Exception as e:
            self.logger.error(f"[LoggerCog] Unexpected error logging message {message.id}: {e}")
            self.logger.error(traceback.format_exc()) # Log the full traceback for other errors

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
                'reactors': json.dumps(reactors),
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
