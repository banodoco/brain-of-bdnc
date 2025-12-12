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
# Import the new subfeature
from .subfeatures import message_linker # Adjusted import path

# Helper class to simulate discord.Reaction for raw events
class SimpleReaction:
    """Lightweight reaction object for use with raw reaction events."""
    def __init__(self, message, emoji):
        self.message = message
        self.emoji = emoji 

class ReactorCog(commands.Cog):
    # __init__ no longer creates the Reactor instance
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        # self.reactor = Reactor(logger=logger, dev_mode=dev_mode) # REMOVED - Reactor instance is created in main.py
        if dev_mode:
            self.logger.info(f"Initializing ReactorCog in development mode (Reactor instance expected on bot object)")
        self.message_linker_channel_ids = [] # Initialize attribute

    def _load_message_linker_config(self):
        """Loads message_linker channel configurations from the environment watchlist."""
        watchlist_json = os.getenv('REACTION_WATCHLIST', '[]')
        try:
            watchlist = json.loads(watchlist_json)
            self.message_linker_channel_ids = [] # Reset before loading
            for item in watchlist:
                if item.get("feature") == "message_linker" and "channel_id" in item:
                    try:
                        channel_id_str = item["channel_id"]
                        if channel_id_str == "*": # Wildcard for all channels
                            self.logger.info("[ReactorCog] MessageLinker configured for all channels ('*').")
                            # Represent "all channels" with a special value, e.g., None or an empty list
                            # For now, if "*" is found, we'll make it process all channels by not restricting.
                            # However, the current MessageLinker logic requires specific IDs or it does nothing.
                            # For true wildcard, MessageLinker.process_message_links would need adjustment.
                            # Let's assume for now "*" means no specific channel filtering by the cog, 
                            # and MessageLinker would need to be initialized with an empty list or special flag.
                            # For this implementation, we will treat '*' as an invalid specific ID and log a warning.
                            # Or, better, let MessageLinker handle it if we pass None.
                            # Let's stick to specific IDs and ignore '*' for now, or treat it as "enabled everywhere".
                            # For now, if '*' is set, let's enable it everywhere by setting allowed_channel_ids to None in MessageLinker's init.
                            # This means message_linker.py needs to be robust if allowed_channel_ids is None.
                            # Let's adjust: if "*", we pass an empty list, and MessageLinker must be updated to treat empty list as "all channels".
                            # Revising: Current MessageLinker treats empty list as "no channels".
                            # So, for "*", we should perhaps pass a special sentinel or not filter in the cog.
                            # For now, let's assume channel_id will always be a specific ID for message_linker feature.
                            # We will simply log if '*' is used for 'message_linker' and not add it.
                            self.logger.warning("[ReactorCog] MessageLinker channel_id='*' is ambiguous. Please specify channel IDs or update MessageLinker to handle wildcard.")
                            # To enable for all channels, the MessageLinker itself should not have channel restrictions.
                            # The MessageLinker class currently allows an empty list, meaning no channels.
                            # If we want '*' to mean all, we would pass None to MessageLinker's allowed_channel_ids and it would skip the check.
                            # Let's keep current MessageLinker behavior: an empty list = disabled. Non-empty list = only those channels.
                            # So, '*' in watchlist for message_linker won't enable it unless MessageLinker changes.
                            # For current request, we only care about specific ID 1376260046945648720.
                            continue # Skip '*' for now for message_linker feature
                        
                        channel_id = int(channel_id_str)
                        if channel_id not in self.message_linker_channel_ids:
                            self.message_linker_channel_ids.append(channel_id)
                    except ValueError:
                        self.logger.error(f"[ReactorCog] Invalid channel_id '{item['channel_id']}' for message_linker in REACTION_WATCHLIST. Must be an integer.")
            
            if self.message_linker_channel_ids:
                self.logger.info(f"[ReactorCog] Loaded MessageLinker config. Allowed channel IDs: {self.message_linker_channel_ids}")
            else:
                self.logger.info("[ReactorCog] No specific channel IDs found for MessageLinker in REACTION_WATCHLIST. It will be disabled or follow its default behavior.")

        except json.JSONDecodeError:
            self.logger.error(f"Error decoding REACTION_WATCHLIST JSON: {watchlist_json}")
            self.message_linker_channel_ids = [] # Ensure it's empty on error

    async def cog_load(self):
        # Load the configuration for MessageLinker channels
        self._load_message_linker_config()

        # You might want to check here if the bot has the reactor instance
        if not hasattr(self.bot, 'reactor_instance') or self.bot.reactor_instance is None:
             self.logger.error("Reactor instance not found on bot object during ReactorCog load!")
             # Optionally raise an error or prevent cog loading?
        else:
            if self.dev_mode:
                self.logger.debug("ReactorCog loaded. Found reactor_instance on bot object.")
        
        # Setup for MessageLinker
        # We need to ensure the setup function from message_linker.py is called.
        # It's an async function, so we need to await it or create a task.
        # Since cog_load can be async, we can await it here.
        try:
            # Pass the loaded channel IDs to the setup function
            await message_linker.setup(self.bot, self.logger, allowed_channel_ids=self.message_linker_channel_ids)
            if self.dev_mode:
                self.logger.debug(f"MessageLinker setup initiated from ReactorCog with channels: {self.message_linker_channel_ids}")
        except Exception as e:
            self.logger.error(f"Error during MessageLinker setup in ReactorCog: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.dev_mode:
            self.logger.info("ReactorCog is ready")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handles message events to check for text/attachment triggers AND message links."""
        # Ignore messages from bots or messages without content/attachments in non-DM channels
        if message.author.bot or not message.guild:
            return
        
        # --- Message Linker Processing ---
        # Check for message links first, independently of other reactor logic
        # Get the MessageLinker instance
        linker_instance = getattr(self.bot, 'message_linker_instance', None)
        if linker_instance:
            try:
                # Create a task to avoid blocking other on_message processing
                asyncio.create_task(linker_instance.process_message_links(message))
                if self.dev_mode:
                    self.logger.debug(f"[ReactorCog] Task created for MessageLinker.process_message_links for message {message.id}")
            except Exception as e:
                self.logger.error(f"[ReactorCog] Error creating task for MessageLinker for message {message.id}: {e}", exc_info=True)
        elif not getattr(self, '_message_linker_instance_error_logged', False): # Log error once
            self.logger.error("[ReactorCog] MessageLinker instance not found on bot object in on_message.")
            self._message_linker_instance_error_logged = True
        # --- End Message Linker Processing ---

        # Also ignore messages without content AND without attachments for the main reactor logic
        # This check is now after the message linker, as message linker only needs message.content for links
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
            
            # Log event reception at DEBUG level to reduce noise
            self.logger.debug(f"[ReactorCog] Reaction event: User={payload.user_id}, Emoji={payload.emoji}, Msg={payload.message_id}")
            
            # --- Get LoggerCog instance --- 
            logger_cog = self.bot.get_cog("LoggerCog")
            if not logger_cog:
                self.logger.warning("[ReactorCog] LoggerCog instance not found! Cannot log reaction add/remove to DB.")
            
            # Ignore reactions from bots or reactions without a user ID (shouldn't happen for add)
            if not payload.user_id:
                self.logger.warning("[ReactorCog] Raw reaction received without user_id, ignoring.")
                return
            
            # Attempt to fetch user early for bot check
            user = self.bot.get_user(payload.user_id) # Check cache first
            if not user:
                self.logger.debug(f"[ReactorCog] User {payload.user_id} not in cache, fetching via API...")
                try:
                    user = await self.bot.fetch_user(payload.user_id)
                except (discord.NotFound, discord.HTTPException):
                    self.logger.warning(f"[ReactorCog] Could not fetch user {payload.user_id} for bot check, ignoring event.")
                    return # Ignore if user can't be fetched
            
            if user.bot:
                self.logger.debug(f"[ReactorCog] Ignoring raw reaction from bot: {user.name}")
                return
        
            # --- END STEP 1 ---

            # --- STEP 2: Restore Reactor instance check and object fetching ---
            # Get the shared Reactor instance
            reactor_instance = getattr(self.bot, 'reactor_instance', None)
            if not reactor_instance:
                self.logger.error("[ReactorCog] Reactor instance not found on bot object in on_raw_reaction_add.")
                return
            
            # Fetch necessary objects
            message = None
            channel = None # Initialize channel
            emoji = None # Initialize emoji
            simulated_reaction = None # Initialize simulated_reaction
            try:
                channel = self.bot.get_channel(payload.channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(payload.channel_id)
                    except discord.NotFound:
                        self.logger.warning(f"[ReactorCog] Could not find channel {payload.channel_id} via API (NotFound). Ignoring raw reaction.")
                        return
                    except discord.Forbidden:
                        self.logger.error(f"[ReactorCog] Permissions error fetching channel {payload.channel_id} via API (Forbidden). Ignoring raw reaction.")
                        return
                    except discord.HTTPException as e:
                        self.logger.error(f"[ReactorCog] HTTP error fetching channel {payload.channel_id} via API: {e}. Ignoring raw reaction.")
                        return

                # Type check after attempting to fetch
                expected_text_channel_types = (
                    discord.ChannelType.text,
                    discord.ChannelType.news,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                    discord.ChannelType.group, # For completeness, though less common for reaction triggers
                    discord.ChannelType.forum # Forum channels themselves might not be where reactions happen, but threads within them.
                                              # Threads are covered by public_thread/private_thread.
                                              # This check is primarily for the channel object the message is in.
                )
                if not channel or not hasattr(channel, 'type') or channel.type not in expected_text_channel_types:
                    self.logger.warning(f"[ReactorCog] Channel {payload.channel_id} (type: {getattr(channel, 'type', 'UnknownType')}) is not a recognized text-based guild channel, ignoring raw reaction.")
                    return
                
                message = await channel.fetch_message(payload.message_id)
                emoji = payload.emoji

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

            # --- STEP 3: Restore final logic (Simulate reaction, call logger, call reactor) ---
            # Proceed with Reactor check only if message was fetched successfully
            if message and user:
                # Simulate Reaction object (Needed for LoggerCog and Reactor)
                simulated_reaction = SimpleReaction(message, emoji)

                # --- Call LoggerCog to log the reaction add --- 
                if logger_cog:
                    # Use asyncio.create_task to prevent blocking the main event flow
                    asyncio.create_task(logger_cog.log_reaction_add(simulated_reaction, user))
                
                # --- Call Reactor --- 
                try:
                    action_name = reactor_instance.check_reaction(simulated_reaction, user) # Pass simulated object

                    if action_name:
                        self.logger.info(f"[ReactorCog] Executing reactor action '{action_name}' for User: {user.id}, Emoji: {emoji}")
                        # Execute action needs the reaction object as well for context
                        asyncio.create_task(reactor_instance.execute_reaction_action(action_name, simulated_reaction, user))
                    
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
            if not channel:
                try:
                    self.logger.info(f"[ReactorCog][Remove] Channel {payload.channel_id} not in cache, attempting to fetch via API...")
                    channel = await self.bot.fetch_channel(payload.channel_id)
                    self.logger.info(f"[ReactorCog][Remove] Successfully fetched channel {payload.channel_id} ({channel.name}) via API.")
                except discord.NotFound:
                    self.logger.warning(f"[ReactorCog][Remove] Could not find channel {payload.channel_id} via API (NotFound) for reaction remove.")
                    return
                except discord.Forbidden:
                    self.logger.error(f"[ReactorCog][Remove] Permissions error fetching channel {payload.channel_id} via API (Forbidden) for reaction remove.")
                    return
                except discord.HTTPException as e:
                    self.logger.error(f"[ReactorCog][Remove] HTTP error fetching channel {payload.channel_id} via API for reaction remove: {e}.")
                    return

            # DIAGNOSTIC LOG
            if hasattr(discord, "threads") and hasattr(discord.threads, "Thread") and hasattr(discord, "TextChannel"):
                self.logger.info(f"[ReactorCog][Remove] DIAGNOSTIC: issubclass(discord.threads.Thread, discord.TextChannel) = {issubclass(discord.threads.Thread, discord.TextChannel)}")
            else:
                self.logger.warning("[ReactorCog][Remove] DIAGNOSTIC: discord.threads.Thread or discord.TextChannel not found for issubclass check.")

            expected_text_channel_types_remove = (
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
                discord.ChannelType.group
            )
            if not channel or not hasattr(channel, 'type') or channel.type not in expected_text_channel_types_remove:
                self.logger.warning(f"[ReactorCog][Remove] Channel {payload.channel_id} (type: {getattr(channel, 'type', 'UnknownType')} / object type: {type(channel)}) is not a recognized text-based guild channel for reaction remove.")
                return
            message = await channel.fetch_message(payload.message_id)
            emoji = payload.emoji

            # Simulate Reaction object
            simulated_reaction = SimpleReaction(message, emoji)

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