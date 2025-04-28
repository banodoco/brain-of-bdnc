# src/features/reacting/reactor_cog.py

import discord
from discord.ext import commands
import os
import traceback
import json
import asyncio
import logging
# Import necessary libraries for your actions (e.g., tweepy for Twitter)
# import tweepy # Example

# Core Reactor logic class is no longer imported or instantiated here
# from .reactor import Reactor 

# Watchlist environment variable is primarily used by the Reactor class now
# Example format: '[{"user_id": "12345", "emoji": "🐦", "action": "send_tweet_about_message"}, {"user_id": "*", "emoji": "📌", "action": "pin_reaction_message"}]'
# WATCHLIST_JSON = os.getenv('REACTION_WATCHLIST', '[]') 

class ReactorCog(commands.Cog):
    # __init__ no longer creates the Reactor instance
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        # self.reactor = Reactor(logger=logger, dev_mode=dev_mode) # REMOVED - Reactor instance is created in main.py
        if dev_mode:
            self.logger.info(f"Initializing ReactorCog in development mode (Reactor instance expected on bot object)")

    async def cog_load(self):
        # You might want to check here if the bot has the reactor instance
        if not hasattr(self.bot, 'reactor_instance') or self.bot.reactor_instance is None:
             self.logger.error("Reactor instance not found on bot object during ReactorCog load!")
             # Optionally raise an error or prevent cog loading?
        else:
            if self.dev_mode:
                self.logger.debug("ReactorCog loaded. Found reactor_instance on bot object.")
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.dev_mode:
            self.logger.info("ReactorCog is ready")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handles message events to check for text/attachment triggers."""
        # Ignore messages from bots or messages without content/attachments in non-DM channels
        if message.author.bot or not message.guild:
            return
        # Also ignore messages without content AND without attachments
        if not message.content and not message.attachments:
             return

        # Get the shared Reactor instance
        reactor_instance = getattr(self.bot, 'reactor_instance', None)
        if not reactor_instance:
            # Log error once, maybe disable further checks?
            if not getattr(self, '_reactor_instance_error_logged', False):
                 self.logger.error("[ReactorCog] Reactor instance not found on bot object in on_message.")
                 self._reactor_instance_error_logged = True # Prevent log flooding
            return

        self.logger.debug(f"[ReactorCog] Processing message {message.id} in channel {message.channel.id} for potential text/attachment triggers.")
        
        try:
            action_name = reactor_instance.check_message(message)
            self.logger.debug(f"[ReactorCog] reactor_instance.check_message returned action: {action_name} for message {message.id}")

            if action_name:
                self.logger.debug(f"[ReactorCog] Action '{action_name}' found for message {message.id}. Calling reactor_instance.execute_message_action.")
                # Execute the message-specific action
                asyncio.create_task(reactor_instance.execute_message_action(action_name, message))
                self.logger.debug(f"[ReactorCog] Task created for execute_message_action '{action_name}' for message {message.id}.")
            # else: No action needed based on message content/attachments

        except Exception as e:
            self.logger.error(f"Error in ReactorCog on_message processing message {message.id}: {e}")
            self.logger.error(traceback.format_exc())

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handles raw reaction add events by fetching necessary objects and delegating."""
        try:
            # --- STEP 1: Restore initial checks (NOW WRAPPED IN TRY/EXCEPT) ---
            
            # VERY FIRST log to confirm event reception and show raw payload data
            self.logger.info(f"[ReactorCog] <<< RAW on_raw_reaction_add event received: User={payload.user_id}, Emoji={payload.emoji}, Msg={payload.message_id} >>>") # Use INFO for visibility, simplified payload
            
            # --- Get LoggerCog instance --- 
            logger_cog = self.bot.get_cog("LoggerCog")
            if not logger_cog:
                self.logger.warning("[ReactorCog] LoggerCog instance not found! Cannot log reaction add/remove to DB.")
            else:
                 self.logger.info("[ReactorCog] Found LoggerCog instance.") # Use INFO
            
            # Ignore reactions from bots or reactions without a user ID (shouldn't happen for add)
            if not payload.user_id:
                self.logger.warning("[ReactorCog] Raw reaction received without user_id, ignoring.")
                return
            
            # Attempt to fetch user early for bot check
            self.logger.info(f"[ReactorCog] Attempting to fetch user {payload.user_id}") # Use INFO
            user = self.bot.get_user(payload.user_id) # Check cache first
            if not user:
                self.logger.info(f"[ReactorCog] User {payload.user_id} not in cache, fetching via API...") # Use INFO
                try:
                    user = await self.bot.fetch_user(payload.user_id)
                    self.logger.info(f"[ReactorCog] Fetched user {payload.user_id} via API.") # Use INFO
                except (discord.NotFound, discord.HTTPException):
                    self.logger.warning(f"[ReactorCog] Could not fetch user {payload.user_id} for bot check, ignoring event.")
                    return # Ignore if user can't be fetched
            else:
                 self.logger.info(f"[ReactorCog] Found user {payload.user_id} in cache.") # Use INFO
            
            if user.bot:
                self.logger.info(f"[ReactorCog] Ignoring raw reaction from bot: {user.name} ({user.id})") # Use INFO
                return
            
            self.logger.info("[ReactorCog] Passed initial checks (User is not bot). Proceeding...") # Use INFO
        
            # --- END STEP 1 ---

            # --- STEP 2: Restore Reactor instance check and object fetching ---
            # Get the shared Reactor instance
            reactor_instance = getattr(self.bot, 'reactor_instance', None)
            if not reactor_instance:
                self.logger.error("[ReactorCog] Reactor instance not found on bot object in on_raw_reaction_add.")
                return
            else:
                self.logger.info("[ReactorCog] Found Reactor instance.") # Use INFO
            
            # Fetch necessary objects
            message = None
            channel = None # Initialize channel
            emoji = None # Initialize emoji
            simulated_reaction = None # Initialize simulated_reaction
            try:
                self.logger.info(f"[ReactorCog] Fetching channel {payload.channel_id}") # Use INFO
                channel = self.bot.get_channel(payload.channel_id)
                if not channel or not isinstance(channel, discord.TextChannel):
                    self.logger.warning(f"[ReactorCog] Could not find text channel {payload.channel_id}, ignoring raw reaction.")
                    return
                
                self.logger.info(f"[ReactorCog] Found channel: {channel.name} ({channel.id}). Fetching message {payload.message_id}") # Use INFO
                message = await channel.fetch_message(payload.message_id)
                emoji = payload.emoji 
                # User was already fetched above for the bot check
                
                self.logger.info(f"[ReactorCog] Fetched message {message.id}. Emoji: {emoji}, User: {user.name} ({user.id})") # Use INFO

                # --- Simulate Reaction object --- (Needed for LoggerCog and Reactor)
                # (Keep this commented for now, restore later if needed)
                # class TempReaction:
                #     def __init__(self, msg, emj):
                #         self.message = msg
                #         self.emoji = emj
                # simulated_reaction = TempReaction(message, emoji)
                # self.logger.info(f"[ReactorCog] Simulated reaction object created.") # Use INFO
                
                # --- Call LoggerCog to log the reaction add --- 
                # (Keep this commented for now, restore later)
                # if logger_cog:
                #     self.logger.info(f"[ReactorCog] --> Creating task to call LoggerCog.log_reaction_add") # Use INFO
                #     asyncio.create_task(logger_cog.log_reaction_add(simulated_reaction, user)) # Need simulated_reaction
                #     self.logger.info(f"[ReactorCog] <-- Task created for LoggerCog.log_reaction_add") # Use INFO
                
                self.logger.info("[ReactorCog] Successfully fetched channel and message.") # Use INFO

            except discord.NotFound:
                self.logger.warning(f"[ReactorCog] Could not find message {payload.message_id} in channel {payload.channel_id} for raw reaction, ignoring.")
                return
            except discord.Forbidden:
                self.logger.error(f"[ReactorCog] Permissions error fetching message {payload.message_id} or channel {payload.channel_id} for raw reaction.")
                return
            except Exception as e:
                self.logger.error(f"[ReactorCog] Error fetching objects for raw reaction: {e}")
                self.logger.error(traceback.format_exc())
                return
            
            # --- END STEP 2 ---
            self.logger.info("[ReactorCog] Passed object fetching. Proceeding...") # Use INFO

            # --- STEP 3: Restore final logic (Simulate reaction, call logger, call reactor) ---
            # Proceed with Reactor check only if message was fetched successfully
            if message and user:
                # --- Simulate Reaction object --- (Needed for LoggerCog and Reactor)
                # Create a simple class that mimics the necessary attributes
                class TempReaction:
                    def __init__(self, msg, emj):
                        self.message = msg
                        self.emoji = emj
                simulated_reaction = TempReaction(message, emoji)
                self.logger.info(f"[ReactorCog] Simulated reaction object created for message {message.id}.") # Use INFO

                # --- Call LoggerCog to log the reaction add --- 
                if logger_cog:
                    # Use asyncio.create_task to prevent blocking the main event flow
                    self.logger.info(f"[ReactorCog] --> Creating task to call LoggerCog.log_reaction_add") # Use INFO
                    # Pass the simulated reaction object
                    asyncio.create_task(logger_cog.log_reaction_add(simulated_reaction, user)) 
                    self.logger.info(f"[ReactorCog] <-- Task created for LoggerCog.log_reaction_add") # Use INFO
                
                # --- Call Reactor --- 
                self.logger.info(f"[ReactorCog] Message and user resolved. Proceeding to check reaction action with Reactor instance.") # Use INFO
                try:
                    self.logger.info(f"[ReactorCog] --> Calling reactor_instance.check_reaction for User: {user.id}, Emoji: {emoji} on Message: {message.id}") # Use INFO
                    action_name = reactor_instance.check_reaction(simulated_reaction, user) # Pass simulated object
                    self.logger.info(f"[ReactorCog] <-- reactor_instance.check_reaction returned action: '{action_name}'") # Use INFO

                    if action_name:
                        self.logger.info(f"[ReactorCog] --> Creating task for reactor_instance.execute_reaction_action with action '{action_name}'.") # Use INFO
                        # Execute action needs the reaction object as well for context
                        asyncio.create_task(reactor_instance.execute_reaction_action(action_name, simulated_reaction, user))
                        self.logger.info(f"[ReactorCog] <-- Task created for execute_reaction_action '{action_name}'.") # Use INFO
                    else:
                         self.logger.info(f"[ReactorCog] No matching reactor action found for raw reaction by User: {user.id}, Emoji: {emoji}") # Use INFO
                    
                except Exception as e:
                    self.logger.error(f"[ReactorCog] Error in ReactorCog check_reaction/execute_action block: {e}")
                    self.logger.error(traceback.format_exc())
            else:
                 self.logger.warning(f"[ReactorCog] Skipping reactor action check because message or user could not be fully resolved.")
            # --- END STEP 3 ---

        except Exception as e:
             # Catch ANY exception that occurs in the block above
             self.logger.error(f"[ReactorCog] UNCAUGHT EXCEPTION in on_raw_reaction_add initial block: {e}")
             self.logger.error(traceback.format_exc())
             # Optional: re-raise if you want the error to propagate further
             # raise e

    # Add listener for reaction remove if logging removals is needed
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handles raw reaction remove events for logging purposes."""
        self.logger.debug(f"[ReactorCog] <<< RAW on_raw_reaction_remove event received: Payload={payload.__dict__} >>>")

        # Get LoggerCog instance
        logger_cog = self.bot.get_cog("LoggerCog")
        if not logger_cog:
            self.logger.warning("[ReactorCog] LoggerCog instance not found! Cannot log reaction remove to DB.")
            return

        # Ignore reactions from bots or reactions without a user ID
        if not payload.user_id:
            self.logger.warning("[ReactorCog] Raw reaction remove received without user_id, ignoring.")
            return
        
        # Attempt to fetch user early for bot check
        user = self.bot.get_user(payload.user_id) # Check cache first
        if not user:
            try:
                user = await self.bot.fetch_user(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                self.logger.warning(f"[ReactorCog] Could not fetch user {payload.user_id} for bot check (remove event), ignoring.")
                return # Ignore if user can't be fetched
        
        if user.bot:
            self.logger.debug(f"[ReactorCog] Ignoring raw reaction remove from bot: {user.name} ({user.id})")
            return

        # Fetch necessary objects
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                self.logger.warning(f"[ReactorCog] Could not find text channel {payload.channel_id} for reaction remove.")
                return
            message = await channel.fetch_message(payload.message_id)
            emoji = payload.emoji

            # Simulate Reaction object
            class TempReaction:
                def __init__(self, msg, emj):
                    self.message = msg
                    self.emoji = emj
            simulated_reaction = TempReaction(message, emoji)

            # Call LoggerCog to log the reaction removal
            asyncio.create_task(logger_cog.log_reaction_remove(simulated_reaction, user))
            self.logger.debug(f"[ReactorCog] Task created to call LoggerCog.log_reaction_remove")

        except discord.NotFound:
            self.logger.warning(f"[ReactorCog] Could not find message {payload.message_id} for reaction remove logging.")
        except discord.Forbidden:
            self.logger.error(f"[ReactorCog] Permissions error fetching objects for reaction remove logging.")
        except Exception as e:
            self.logger.error(f"[ReactorCog] Error fetching objects for reaction remove logging: {e}")
            self.logger.error(traceback.format_exc())

    # --- Action Methods are handled by the Reactor class via the shared instance --- 

# Standard setup function remains the same for loading the cog
async def setup(bot):
    # Pass dependencies required by the Cog itself (not the Reactor)
    # Assuming logger and dev_mode are accessible on the bot or passed differently
    logger = getattr(bot, 'logger', logging.getLogger('DiscordBot')) # Example fallback
    dev_mode = getattr(bot, 'dev_mode', False) # Example fallback
    await bot.add_cog(ReactorCog(bot, logger, dev_mode))
    logger.info("ReactorCog added to bot.")

# Optional setup function if needed later
# async def setup(bot):
#    # If you need async setup for the cog
#    cog = ReactorCog(bot, bot.logger, bot.dev_mode)
#    await bot.add_cog(cog) 