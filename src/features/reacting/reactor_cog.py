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
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handles raw reaction add events by fetching necessary objects and delegating."""
        # VERY FIRST log to confirm event reception
        self.logger.debug(f"[ReactorCog] <<< RAW on_raw_reaction_add event received: Emoji={payload.emoji}, UserID={payload.user_id}, MessageID={payload.message_id} >>>")

        # Ignore reactions from bots or reactions without a user ID (shouldn't happen for add)
        if not payload.user_id or payload.member and payload.member.bot:
             # Check payload.member as user might not be cached
             if payload.member:
                 self.logger.debug(f"[ReactorCog] Ignoring raw reaction from bot: {payload.member.name}")
             else:
                 # Attempt to fetch user if not cached as member
                 user = self.bot.get_user(payload.user_id)
                 if user and user.bot:
                     self.logger.debug(f"[ReactorCog] Ignoring raw reaction from bot ID (fetched): {payload.user_id}")
                     return
                 elif not user:
                     self.logger.warning(f"[ReactorCog] Could not fetch user {payload.user_id} for bot check, proceeding cautiously.")
                     # Decide if you want to proceed or return here if user fetch fails
                 # Else: user fetched and not a bot, proceed

             if not payload.user_id: # If user_id itself is missing
                 self.logger.warning("[ReactorCog] Raw reaction received without user_id, ignoring.")
                 return
                 
             # If only member was checked and it was None, but user wasn't a bot
             # then we fall through here. Add a final check.
             user = self.bot.get_user(payload.user_id) # Re-fetch if needed or use cached
             if user and user.bot:
                 return # Explicitly ignore if finally confirmed as bot

        # Get the shared Reactor instance
        reactor_instance = getattr(self.bot, 'reactor_instance', None)
        if not reactor_instance:
            self.logger.error("[ReactorCog] Reactor instance not found on bot object in on_raw_reaction_add.")
            return

        # Fetch necessary objects
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                self.logger.warning(f"[ReactorCog] Could not find text channel {payload.channel_id}, ignoring raw reaction.")
                return

            message = await channel.fetch_message(payload.message_id)
            # Construct a partial reaction object (or fetch if needed, but payload.emoji is usually enough)
            # Note: For custom emojis, payload.emoji might be partial. str(payload.emoji) works.
            emoji = payload.emoji 
            # Fetch the user who reacted (payload.member might be None if user isn't cached)
            user = payload.member or self.bot.get_user(payload.user_id)
            if not user:
                 # If user still not found after get_user, maybe fetch required?
                 try:
                     user = await self.bot.fetch_user(payload.user_id)
                 except discord.NotFound:
                     self.logger.error(f"[ReactorCog] Could not find user {payload.user_id} for raw reaction, ignoring.")
                     return
                 except discord.HTTPException as e:
                     self.logger.error(f"[ReactorCog] HTTP error fetching user {payload.user_id}: {e}")
                     return # Decide if you want to proceed

            # Create a simple Reaction-like object if needed by check_reaction, 
            # or adapt check_reaction to handle payload directly
            # For now, let's simulate the key attributes needed by existing logic
            # This might need adjustment based on Reactor.check_reaction needs
            # Let's assume check_reaction needs reaction.emoji and reaction.message
            # We already fetched `message`. `payload.emoji` gives us the emoji.
            # We pass emoji and user directly now, adapting check_reaction later if needed

            self.logger.debug(f"[ReactorCog] Fetched objects for raw reaction: User={user.id}, Emoji={emoji}, Message={message.id}")

        except discord.NotFound:
            self.logger.warning(f"[ReactorCog] Could not find message {payload.message_id} in channel {payload.channel_id} for raw reaction, ignoring.")
            return
        except discord.Forbidden:
            self.logger.error(f"[ReactorCog] Permissions error fetching message {payload.message_id} or user {payload.user_id} for raw reaction.")
            return
        except Exception as e:
            self.logger.error(f"[ReactorCog] Error fetching objects for raw reaction: {e}")
            self.logger.error(traceback.format_exc())
            return

        # Now proceed with the check using fetched objects
        self.logger.debug(f"[ReactorCog] Found reactor_instance. Proceeding to check raw reaction.")
        try:
            # We need to adapt check_reaction or pass simulated reaction object
            # Option 1: Adapt check_reaction (preferred)
            # Option 2: Simulate reaction (easier for now)
            # Let's pass emoji and user separately for now, assuming check_reaction is flexible
            # or we adapt it next. We also need the message context. 
            # Let's pass the fetched message, emoji, and user to check_reaction.
            # *** This requires modifying reactor.check_reaction signature *** 
            # action_name = reactor_instance.check_reaction(message, emoji, user)

            # --- TEMPORARY: Simulate reaction object --- 
            # This is less robust than modifying check_reaction
            class TempReaction:
                def __init__(self, msg, emj):
                    self.message = msg
                    self.emoji = emj
            simulated_reaction = TempReaction(message, emoji)
            # --- End Temporary --- 

            self.logger.debug(f"[ReactorCog] Calling reactor_instance.check_reaction with simulated reaction for User: {user.id}, Emoji: {emoji}")
            action_name = reactor_instance.check_reaction(simulated_reaction, user) # Pass simulated object
            self.logger.debug(f"[ReactorCog] reactor_instance.check_reaction returned action: {action_name}")

            if action_name:
                self.logger.debug(f"[ReactorCog] Action '{action_name}' found. Calling reactor_instance.execute_action.")
                # Execute action needs the reaction object as well for context
                # Pass the simulated reaction object
                asyncio.create_task(reactor_instance.execute_action(action_name, simulated_reaction, user))
                self.logger.debug(f"[ReactorCog] Task created for execute_action '{action_name}'.")
            else:
                 self.logger.debug(f"[ReactorCog] No matching action found for raw reaction by User: {user.id}, Emoji: {emoji}")
            
        except Exception as e:
            self.logger.error(f"Error in ReactorCog on_raw_reaction_add after fetching objects: {e}")
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