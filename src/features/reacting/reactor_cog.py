# src/features/reacting/reactor_cog.py

import discord
from discord.ext import commands
import os
import traceback
import asyncio
import logging
import time
from typing import Optional
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
        if dev_mode:
            self.logger.info(f"Initializing ReactorCog in development mode (Reactor instance expected on bot object)")
        self.message_linker_channel_ids = [] # Initialize attribute
        self._rate_limit_until = 0  # monotonic timestamp; skip reaction handling while active
        self._bot_initiated_removals = {}

    # Codepoints for emojis commonly used as political symbols (beyond flags)
    POLITICAL_CODEPOINTS = {
        0x1F349,   # 🍉 Watermelon (Palestinian solidarity)
        0x1F53B,   # 🔻 Red triangle pointing down
    }

    # Codepoints for religious symbols and places of worship
    RELIGIOUS_CODEPOINTS = {
        0x271D,    # ✝ Latin cross
        0x271E,    # ✞ Shadowed Latin cross
        0x2720,    # ✠ Maltese cross
        0x2625,    # ☥ Ankh
        0x2626,    # ☦ Orthodox cross
        0x2627,    # ☧ Chi Rho
        0x2629,    # ☩ Cross of Jerusalem
        0x262A,    # ☪ Star and crescent
        0x262C,    # ☬ Adi Shakti
        0x2721,    # ✡ Star of David
        0x1F52F,   # 🔯 Six-pointed star with dot
        0x2638,    # ☸ Wheel of Dharma
        0x262F,    # ☯ Yin Yang
        0x1F549,   # 🕉 Om
        0x1F54E,   # 🕎 Menorah
        0x1F6D0,   # 🛐 Place of worship
        0x26EA,    # ⛪ Church
        0x1F54C,   # 🕌 Mosque
        0x1F6D5,   # 🛕 Hindu temple
        0x1F54D,   # 🕍 Synagogue
        0x1F54B,   # 🕋 Kaaba
        0x26E9,    # ⛩ Shinto shrine
        0x1F4FF,   # 📿 Prayer beads
        0x1FAAC,   # 🪬 Hamsa
        0x1FAAF,   # 🪯 Khanda
        0x1F9FF,   # 🧿 Nazar amulet
    }

    @staticmethod
    def _classify_restricted_emoji(emoji_str: str) -> Optional[str]:
        """Classify a restricted emoji as flag, political, or religious."""
        codepoints = [ord(c) for c in emoji_str]

        # Country flags: pairs of Regional Indicator Symbol Letters (U+1F1E6 to U+1F1FF)
        regional_indicators = [cp for cp in codepoints if 0x1F1E6 <= cp <= 0x1F1FF]
        if len(regional_indicators) == 2:
            return 'flag'

        # Subdivision flags: 🏴 (U+1F3F4) followed by tag characters (U+E0061-U+E007A)
        if codepoints and codepoints[0] == 0x1F3F4 and any(0xE0061 <= cp <= 0xE007A for cp in codepoints):
            return 'flag'

        if any(cp in ReactorCog.POLITICAL_CODEPOINTS for cp in codepoints):
            return 'political'

        if any(cp in ReactorCog.RELIGIOUS_CODEPOINTS for cp in codepoints):
            return 'religious'

        return None

    def _prune_bot_removals(self, ttl_seconds=60):
        """Drop expired bot-initiated removal markers."""
        now = time.monotonic()
        expired_keys = [
            key
            for key, (_, _, created_at) in self._bot_initiated_removals.items()
            if now - created_at > ttl_seconds
        ]
        for key in expired_keys:
            self._bot_initiated_removals.pop(key, None)

    def register_bot_removal(self, message_id, user_id, emoji_str, classification, reason=None):
        """Record a short-lived marker for a bot-initiated reaction removal."""
        self._prune_bot_removals()
        self._bot_initiated_removals[(message_id, user_id, emoji_str)] = (
            classification,
            reason,
            time.monotonic(),
        )

    def _is_feature_enabled(self, guild_id, channel_id, feature):
        """Check if a feature is enabled via server_config, defaulting to True."""
        sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None)
        if sc is None:
            return True
        return sc.is_feature_enabled(guild_id, channel_id, feature)

    def _load_message_linker_config(self):
        """Load message_linker channel configuration from server_config."""
        try:
            self.message_linker_channel_ids = [] # Reset before loading
            sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None)
            if sc:
                for server in sc.get_enabled_servers():
                    guild_id = server['guild_id']
                    channels = server.get('message_linker_channels') or []
                    if not isinstance(channels, list):
                        self.logger.warning(f"[ReactorCog] message_linker_channels for guild {guild_id} is not a list; skipping")
                        continue
                    for raw_channel_id in channels:
                        try:
                            channel_id = int(raw_channel_id)
                        except (TypeError, ValueError):
                            self.logger.error(f"[ReactorCog] Invalid message_linker channel ID '{raw_channel_id}' for guild {guild_id}")
                            continue
                        if channel_id not in self.message_linker_channel_ids:
                            self.message_linker_channel_ids.append(channel_id)

            # Env fallback if server_config yielded nothing
            if not self.message_linker_channel_ids:
                import json as _json
                env_val = os.getenv('REACTION_WATCHLIST', '[]')
                try:
                    parsed = _json.loads(env_val)
                    if isinstance(parsed, list):
                        for rule in parsed:
                            if isinstance(rule, dict) and rule.get('trigger_type') == 'message_link':
                                for ch in (rule.get('channels') or []):
                                    try:
                                        cid = int(ch)
                                        if cid not in self.message_linker_channel_ids:
                                            self.message_linker_channel_ids.append(cid)
                                    except (TypeError, ValueError):
                                        pass
                except (_json.JSONDecodeError, ValueError):
                    pass

            if self.message_linker_channel_ids:
                self.logger.info(f"[ReactorCog] Loaded MessageLinker config. Allowed channel IDs: {self.message_linker_channel_ids}")
            else:
                self.logger.info("[ReactorCog] No message_linker channels configured.")
        except Exception as e:
            self.logger.error(f"[ReactorCog] Error loading message_linker config: {e}", exc_info=True)
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

        if not self._is_feature_enabled(message.guild.id, message.channel.id, 'reactions'):
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
            # --- Rate limit cooldown: skip processing if we recently hit a 429 ---
            if time.monotonic() < self._rate_limit_until:
                self.logger.debug("[ReactorCog] Skipping reaction add — rate limit cooldown active")
                return

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

            # Feature guard: reactions_enabled
            if not self._is_feature_enabled(payload.guild_id, payload.channel_id, 'reactions'):
                return

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

            except discord.HTTPException as e:
                if e.status == 429:
                    self._rate_limit_until = time.monotonic() + 60
                    self.logger.warning(f"[ReactorCog] 429 on fetch_message — entering 60s cooldown")
                else:
                    self.logger.error(f"[ReactorCog] HTTP {e.status} fetching message {payload.message_id}: {e}")
                return
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
                # --- Restricted emoji enforcement: remove and post reminder in rules channel ---
                emoji_str = str(emoji)
                category = self._classify_restricted_emoji(emoji_str)
                if category is not None:
                    self.logger.info(f"[ReactorCog] Restricted emoji '{emoji_str}' detected from user {user.id} on message {message.id} — removing and posting reminder.")
                    try:
                        self.register_bot_removal(message.id, user.id, emoji_str, 'bot_auto_restricted', category)
                        await message.remove_reaction(emoji, user)
                        db_handler = getattr(self.bot, 'db_handler', None)
                        if db_handler:
                            db_handler.record_moderation_decision(
                                message_id=message.id,
                                channel_id=getattr(message.channel, 'id', None),
                                guild_id=getattr(message.guild, 'id', payload.guild_id),
                                reactor_user_id=user.id,
                                reactor_name=getattr(user, 'display_name', user.name),
                                emoji=emoji_str,
                                message_author_id=getattr(message.author, 'id', None),
                                message_author_name=getattr(message.author, 'display_name', getattr(message.author, 'name', None)),
                                message_content_snippet=(message.content or '')[:200],
                                classification='bot_auto_restricted',
                                reason=category,
                                is_suspicious=False,
                            )
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.logger.warning(f"[ReactorCog] Could not remove restricted reaction: {e}")
                    # Post a temporary reminder in the rules channel (auto-deletes after 5 minutes)
                    try:
                        sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None)
                        rules_channel_id = sc.get_server_field(payload.guild_id, 'rules_channel_id', cast=int) if sc else None
                        if rules_channel_id:
                            rules_channel = self.bot.get_channel(rules_channel_id) or await self.bot.fetch_channel(rules_channel_id)
                            reminder_msg = await rules_channel.send(
                                f"<@{user.id}> — friendly reminder: political or religious symbols (flags, political emojis, religious iconography, etc.) "
                                f"are not allowed as reactions here. This is a non-political, non-religious environment. "
                                f"Please keep reactions fun and neutral! 🙂"
                            )
                            # Delete the reminder after 5 minutes so it doesn't clutter the channel
                            async def _delete_after_delay(msg, delay):
                                await asyncio.sleep(delay)
                                try:
                                    await msg.delete()
                                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                    pass
                            asyncio.create_task(_delete_after_delay(reminder_msg, 300))
                        else:
                            self.logger.warning(f"[ReactorCog] No rules_channel_id configured for guild {payload.guild_id} — could not post restricted emoji reminder.")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.logger.warning(f"[ReactorCog] Could not post restricted emoji reminder in rules channel: {e}")
                    return  # Stop further processing for this reaction

                # Simulate Reaction object (Needed for LoggerCog and Reactor)
                simulated_reaction = SimpleReaction(message, emoji)

                # --- Call LoggerCog to log the reaction add --- 
                if logger_cog:
                    # Use asyncio.create_task to prevent blocking the main event flow
                    asyncio.create_task(logger_cog.log_reaction_add(simulated_reaction, user))
                
                # --- Call Reactor (only if sharing_enabled for this channel) ---
                try:
                    # Skip reactor actions if sharing is disabled for this channel
                    # (reactions are still logged by LoggerCog above)
                    if not self._is_feature_enabled(payload.guild_id, payload.channel_id, 'sharing'):
                        self.logger.debug(f"[ReactorCog] sharing_enabled=False for channel {payload.channel_id}, skipping reactor actions")
                    else:
                        action_name = reactor_instance.check_reaction(simulated_reaction, user)

                        if action_name:
                            self.logger.info(f"[ReactorCog] Executing reactor action '{action_name}' for User: {user.id}, Emoji: {emoji}")
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
        # --- Rate limit cooldown: skip processing if we recently hit a 429 ---
        if time.monotonic() < self._rate_limit_until:
            self.logger.debug("[ReactorCog] Skipping reaction remove — rate limit cooldown active")
            return

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

        # Feature guard: reactions_enabled
        if not self._is_feature_enabled(payload.guild_id, payload.channel_id, 'reactions'):
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
            emoji_str = str(emoji)

            # Simulate Reaction object
            simulated_reaction = SimpleReaction(message, emoji)

            # Call LoggerCog to log the reaction removal
            asyncio.create_task(logger_cog.log_reaction_remove(simulated_reaction, user))
            self.logger.debug(f"[ReactorCog] Task created to call LoggerCog.log_reaction_remove")

            db_handler = getattr(self.bot, 'db_handler', None)
            content_snippet = (message.content or '')[:200]
            marker_key = (message.id, user.id, emoji_str)
            marker = self._bot_initiated_removals.pop(marker_key, None)
            if marker is not None:
                classification, reason, _ = marker
                if classification == 'bot_auto_restricted':
                    return
                if db_handler:
                    db_handler.record_moderation_decision(
                        message_id=message.id,
                        channel_id=getattr(message.channel, 'id', None),
                        guild_id=getattr(message.guild, 'id', payload.guild_id),
                        reactor_user_id=user.id,
                        reactor_name=getattr(user, 'display_name', user.name),
                        emoji=emoji_str,
                        message_author_id=getattr(message.author, 'id', None),
                        message_author_name=getattr(message.author, 'display_name', getattr(message.author, 'name', None)),
                        message_content_snippet=content_snippet,
                        classification=classification,
                        reason=reason,
                        is_suspicious=False,
                    )
                return

            is_suspicious = emoji_str in {'🤮', '👎', '😭'}
            if db_handler:
                db_handler.record_moderation_decision(
                    message_id=message.id,
                    channel_id=getattr(message.channel, 'id', None),
                    guild_id=getattr(message.guild, 'id', payload.guild_id),
                    reactor_user_id=user.id,
                    reactor_name=getattr(user, 'display_name', user.name),
                    emoji=emoji_str,
                    message_author_id=getattr(message.author, 'id', None),
                    message_author_name=getattr(message.author, 'display_name', getattr(message.author, 'name', None)),
                    message_content_snippet=content_snippet,
                    classification='user_self_removal',
                    reason=None,
                    is_suspicious=is_suspicious,
                )

            if is_suspicious:
                async def _notify_admin():
                    try:
                        admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
                        if admin_id:
                            admin_user = await self.bot.fetch_user(admin_id); dm = await admin_user.create_dm()
                            await dm.send(f"Suspicious reaction removal: {user.mention} removed {emoji_str} from {message.jump_url}\nSnippet: {content_snippet}")
                    except Exception as e:
                        self.logger.warning(f"[ReactorCog] Failed to DM admin about suspicious reaction removal: {e}")
                asyncio.create_task(_notify_admin())

        except discord.HTTPException as e:
            if e.status == 429:
                self._rate_limit_until = time.monotonic() + 60
                self.logger.warning(f"[ReactorCog] 429 on fetch_message (remove) — entering 60s cooldown")
            else:
                self.logger.error(f"[ReactorCog] HTTP {e.status} fetching message {payload.message_id} for reaction remove: {e}")
        except discord.NotFound:
            self.logger.warning(f"[ReactorCog] Could not find message {payload.message_id} for reaction remove logging.")
        except discord.Forbidden:
            self.logger.error(f"[ReactorCog] Permissions error fetching objects for reaction remove logging.")
        except Exception as e:
            self.logger.error(f"[ReactorCog] Error fetching objects for reaction remove logging: {e}")
            self.logger.error(traceback.format_exc())

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        """Record moderation decisions for bulk reaction clears."""
        db_handler = getattr(self.bot, 'db_handler', None)
        if not db_handler:
            return

        snapshot = db_handler.get_message_snapshot(payload.message_id)
        active_reactors = db_handler.get_active_reactors(payload.message_id)
        if not active_reactors:
            return

        channel_id = snapshot.get('channel_id') if snapshot else getattr(payload, 'channel_id', None)
        author_id = snapshot.get('author_id') if snapshot else None
        author_name = snapshot.get('author_name') if snapshot else None
        content_snippet = ((snapshot.get('content') or '')[:200] if snapshot else None)
        for row in active_reactors:
            db_handler.record_moderation_decision(
                message_id=payload.message_id,
                channel_id=channel_id,
                guild_id=row.get('guild_id') or (snapshot.get('guild_id') if snapshot else getattr(payload, 'guild_id', None)),
                reactor_user_id=row.get('user_id'),
                reactor_name=None,
                emoji=row.get('emoji'),
                message_author_id=author_id,
                message_author_name=author_name,
                message_content_snippet=content_snippet,
                classification='moderator_cleared_all',
                reason=None,
                is_suspicious=False,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        """Record moderation decisions for single-emoji clears."""
        db_handler = getattr(self.bot, 'db_handler', None)
        if not db_handler:
            return

        emoji_str = str(payload.emoji)
        snapshot = db_handler.get_message_snapshot(payload.message_id)
        active_reactors = db_handler.get_active_reactors(payload.message_id, emoji=emoji_str)
        if not active_reactors:
            return

        channel_id = snapshot.get('channel_id') if snapshot else getattr(payload, 'channel_id', None)
        author_id = snapshot.get('author_id') if snapshot else None
        author_name = snapshot.get('author_name') if snapshot else None
        content_snippet = ((snapshot.get('content') or '')[:200] if snapshot else None)
        for row in active_reactors:
            db_handler.record_moderation_decision(
                message_id=payload.message_id,
                channel_id=channel_id,
                guild_id=row.get('guild_id') or (snapshot.get('guild_id') if snapshot else getattr(payload, 'guild_id', None)),
                reactor_user_id=row.get('user_id'),
                reactor_name=None,
                emoji=row.get('emoji'),
                message_author_id=author_id,
                message_author_name=author_name,
                message_content_snippet=content_snippet,
                classification='moderator_cleared_emoji',
                reason=None,
                is_suspicious=False,
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Record moderation decisions when message deletion cascades remove reactions."""
        db_handler = getattr(self.bot, 'db_handler', None)
        if not db_handler:
            return

        snapshot = db_handler.get_message_snapshot(payload.message_id)
        active_reactors = db_handler.get_active_reactors(payload.message_id)
        if not active_reactors:
            return

        channel_id = snapshot.get('channel_id') if snapshot else payload.channel_id
        author_id = snapshot.get('author_id') if snapshot else None
        author_name = snapshot.get('author_name') if snapshot else None
        content_snippet = ((snapshot.get('content') or '')[:200] if snapshot else None)
        for row in active_reactors:
            db_handler.record_moderation_decision(
                message_id=payload.message_id,
                channel_id=channel_id,
                guild_id=row.get('guild_id') or (snapshot.get('guild_id') if snapshot else payload.guild_id),
                reactor_user_id=row.get('user_id'),
                reactor_name=None,
                emoji=row.get('emoji'),
                message_author_id=author_id,
                message_author_name=author_name,
                message_content_snippet=content_snippet,
                classification='message_deleted_cascade',
                reason=None,
                is_suspicious=False,
            )

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
