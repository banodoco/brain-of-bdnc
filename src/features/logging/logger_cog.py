# src/features/logging/logger_cog.py

import asyncio
import traceback
import json
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
