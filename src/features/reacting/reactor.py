# src/features/reacting/reactor.py

import discord
import os
import traceback
import json
import re # Added for text matching
from discord.ext import commands # <<< ADDED for bot_instance type hint and usage
from src.features.sharing.sharer import Sharer # Import the Sharer class
from src.common.db_handler import DatabaseHandler # <<< Added DB Handler import
from src.common.openmuse_interactor import OpenMuseInteractor # <<< Added OpenMuse Interactor import
from src.common.llm.claude_client import ClaudeClient # Added LLM Client import
from .subfeatures.permission_handler import handle_request_curation_permission

# Assuming get_llm_response is structured to be importable like this:
# This might need adjustment based on your project structure for src.common.llm
from src.common.llm import get_llm_response

# --- BEGIN NEW IMPORT FOR DISPUTE RESOLVER ---
from .subfeatures.dispute_resolver import handle_initiate_dispute_resolution
# --- END NEW IMPORT FOR DISPUTE RESOLVER ---
# --- BEGIN NEW IMPORT FOR OPENMUSE UPLOADER ---
from .subfeatures.openmuse_uploader import handle_upload_to_openmuse
# --- END NEW IMPORT FOR OPENMUSE UPLOADER ---
# --- BEGIN NEW IMPORT FOR TWEET SHARER BRIDGE ---
from .subfeatures.tweet_sharer_bridge import handle_send_tweet_about_message
# --- END NEW IMPORT FOR TWEET SHARER BRIDGE ---
# --- BEGIN UPDATED IMPORT FOR WORKFLOW UPLOADER ---
from .subfeatures.workflow_uploader import process_workflow_upload_request # UPDATED
# --- END UPDATED IMPORT FOR WORKFLOW UPLOADER ---

# Environment variable for watchlist configuration
# Example format:
# [
#   {"trigger_type": "reaction", "user_id": "123", "emoji": "🐦", "action": "send_tweet_about_message"},
#   {"trigger_type": "text", "text_pattern": "(?i)urgent", "action": "log_urgent_message", "channel_id": "*", "user_id": "*"},
#   {"trigger_type": "attachment", "attachment_type": "image/png", "action": "process_image_attachment", "user_id": "*"}
# ]
MAX_UPLOAD_ATTEMPTS = 3
BASE_RETRY_DELAY_SECONDS = 2


class Reactor:
    def __init__(self, logger, sharer_instance: Sharer, db_handler: DatabaseHandler, openmuse_interactor: OpenMuseInteractor, bot_instance: commands.Bot, llm_client: ClaudeClient, dev_mode=False):
        self.logger = logger
        self.dev_mode = dev_mode
        self.sharer = sharer_instance # Store the Sharer instance
        self.db_handler = db_handler # Store DB Handler instance
        self.openmuse_interactor = openmuse_interactor # <<< Store OpenMuse Interactor instance
        self.bot = bot_instance # <<< ADDED to store bot_instance
        self.llm_client = llm_client # Store LLM client instance
        self.watchlist_by_guild = {}
        self._watchlist_refresh_marker = None
        self._load_watchlist()

    def _load_watchlist(self):
        """Load and parse per-guild reactor rules from server_config."""
        sc = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        watchlist_by_guild = {}
        valid_rules = 0
        try:
            servers = sc.get_enabled_servers(require_write=True) if sc else []
            if not servers:
                # Fall back to env var for backward compatibility
                self._watchlist_refresh_marker = None
                env_watchlist = os.getenv('REACTION_WATCHLIST')
                if env_watchlist:
                    try:
                        parsed = json.loads(env_watchlist)
                        if isinstance(parsed, list):
                            env_prefix = 'DEV_' if self.dev_mode else ''
                            env_guild_id = int(os.getenv(f'{env_prefix}GUILD_ID', '0')) or 0
                            if env_guild_id:
                                guild_rules = []
                                for i, rule in enumerate(parsed):
                                    normalized = self._normalize_rule(rule, env_guild_id, i + 1)
                                    if normalized:
                                        guild_rules.append(normalized)
                                if guild_rules:
                                    watchlist_by_guild[env_guild_id] = guild_rules
                                    self.logger.info(f"[Reactor] Loaded {len(guild_rules)} rules from env REACTION_WATCHLIST for guild {env_guild_id}")
                    except (json.JSONDecodeError, ValueError) as e:
                        self.logger.error(f"[Reactor] Failed to parse env REACTION_WATCHLIST: {e}")
                self.watchlist_by_guild = watchlist_by_guild
                return

            self._watchlist_refresh_marker = getattr(sc, '_last_refresh_monotonic', None)
            for server in servers:
                guild_id = server['guild_id']
                parsed_watchlist = server.get('reaction_watchlist') or []
                if not isinstance(parsed_watchlist, list):
                    self.logger.warning(f"[Reactor] reaction_watchlist for guild {guild_id} is not a list; skipping")
                    continue

                guild_rules = []
                for i, rule in enumerate(parsed_watchlist):
                    normalized = self._normalize_rule(rule, guild_id, i + 1)
                    if normalized:
                        guild_rules.append(normalized)
                        valid_rules += 1

                if guild_rules:
                    watchlist_by_guild[guild_id] = guild_rules

            self.watchlist_by_guild = watchlist_by_guild
            total_guilds = len(self.watchlist_by_guild)
            self.logger.info(f"[Reactor] Loaded {valid_rules} valid rules across {total_guilds} guild(s) from server_config.reaction_watchlist.")
        except Exception as e:
            self.logger.error(f"[Reactor] Unexpected error loading watchlist: {e}")
            self.logger.error(traceback.format_exc())
            self.watchlist_by_guild = {}

    def _normalize_rule(self, rule, guild_id: int, rule_index: int):
        if not isinstance(rule, dict) or 'action' not in rule:
            self.logger.warning(f"[Reactor] Skipping invalid rule #{rule_index} for guild {guild_id} (missing 'action'): {rule}")
            return None

        rule = dict(rule)
        trigger_type = str(rule.get('trigger_type', 'reaction')).lower()
        rule['trigger_type'] = trigger_type

        if trigger_type == 'reaction':
            if 'user_id' not in rule or 'emoji' not in rule:
                self.logger.warning(f"[Reactor] Skipping invalid reaction rule #{rule_index} for guild {guild_id}: {rule}")
                return None
            if isinstance(rule['user_id'], str) and rule['user_id'] != '*':
                rule['user_id'] = [rule['user_id']]
            elif not isinstance(rule['user_id'], list) and rule['user_id'] != '*':
                self.logger.warning(f"[Reactor] Skipping invalid reaction rule #{rule_index} for guild {guild_id}: {rule}")
                return None
            if isinstance(rule['user_id'], list):
                rule['user_id'] = [str(user_id) for user_id in rule['user_id']]
            rule['emoji'] = str(rule['emoji'])
            return rule

        if trigger_type == 'text':
            rule.setdefault('channel_id', '*')
            rule.setdefault('user_id', '*')
            if rule['channel_id'] != '*':
                rule['channel_id'] = str(rule['channel_id'])
            if rule['user_id'] != '*':
                rule['user_id'] = str(rule['user_id'])
            if 'text_pattern' in rule:
                try:
                    re.compile(rule['text_pattern'])
                    return rule
                except re.error as e:
                    self.logger.warning(f"[Reactor] Skipping invalid text regex for guild {guild_id}: {e}")
                    return None
            if isinstance(rule.get('text_contains'), str):
                return rule
            self.logger.warning(f"[Reactor] Skipping invalid text rule #{rule_index} for guild {guild_id}: {rule}")
            return None

        if trigger_type == 'attachment':
            if 'attachment_type' not in rule:
                self.logger.warning(f"[Reactor] Skipping invalid attachment rule #{rule_index} for guild {guild_id}: {rule}")
                return None
            rule.setdefault('channel_id', '*')
            rule.setdefault('user_id', '*')
            if rule['channel_id'] != '*':
                rule['channel_id'] = str(rule['channel_id'])
            if rule['user_id'] != '*':
                rule['user_id'] = str(rule['user_id'])
            return rule

        self.logger.warning(f"[Reactor] Skipping rule #{rule_index} for guild {guild_id} with unknown trigger_type '{trigger_type}'")
        return None

    def _get_watchlist_for_guild(self, guild_id: int | None):
        sc = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        if sc:
            sc._maybe_refresh()
            refresh_marker = getattr(sc, '_last_refresh_monotonic', None)
            if refresh_marker != self._watchlist_refresh_marker:
                self._load_watchlist()
        if guild_id is None:
            return []
        return self.watchlist_by_guild.get(guild_id, [])

    def check_reaction(self, reaction, user):
        """Checks if a reaction matches any 'reaction' rule in the watchlist and returns the action name if matched."""
        guild_id = getattr(getattr(reaction, 'message', None), 'guild', None)
        guild_id = getattr(guild_id, 'id', None)
        guild_watchlist = self._get_watchlist_for_guild(guild_id)
        reaction_rules = [rule for rule in guild_watchlist if rule.get('trigger_type', 'reaction') == 'reaction']
        if not reaction_rules:
            self.logger.debug(f"[Reactor] check_reaction called for guild {guild_id}, but no reaction rules are loaded.")
            return None
        if user.bot:
            # This check is redundant if ReactorCog already filters bots, but safe to keep.
            self.logger.debug("[Reactor] check_reaction called for a bot user, ignoring.")
            return None

        emoji_str = str(reaction.emoji)
        user_id_str = str(user.id)
        self.logger.debug(f"[Reactor] check_reaction: Checking User ID '{user_id_str}' with Emoji '{emoji_str}' against {len(reaction_rules)} reaction rules.")

        for i, rule in enumerate(reaction_rules):
            # Rule structure already validated in _load_watchlist
            user_match = False
            if rule['user_id'] == '*':
                user_match = True
            elif isinstance(rule['user_id'], list):
                user_match = user_id_str in rule['user_id']
            
            emoji_match = (rule['emoji'] == '*' or rule['emoji'] == emoji_str)
            self.logger.debug(f"[Reactor] Rule {i+1} Check: Target User(s)='{rule['user_id']}', Target Emoji='{rule['emoji']}'. Incoming User='{user_id_str}', Incoming Emoji='{emoji_str}'. User Match: {user_match}, Emoji Match: {emoji_match}")

            if user_match and emoji_match:
                action_name = rule['action']
                self.logger.info(f"[Reactor] Reaction rule matched: User='{rule['user_id']}', Emoji='{rule['emoji']}'. Triggering action: '{action_name}' for user {user_id_str} on message {reaction.message.id}")
                return action_name
        
        self.logger.debug(f"[Reactor] check_reaction: No reaction rule matched for User ID '{user_id_str}' with Emoji '{emoji_str}'.")
        return None # No match

    def check_message(self, message: discord.Message):
        """Checks if a message matches any 'text' or 'attachment' rule in the watchlist."""
        guild_id = getattr(getattr(message, 'guild', None), 'id', None)
        guild_watchlist = self._get_watchlist_for_guild(guild_id)
        message_rules = [rule for rule in guild_watchlist if rule.get('trigger_type') in ['text', 'attachment']]
        if not message_rules:
            self.logger.debug(f"[Reactor] check_message called for guild {guild_id}, but no text/attachment rules are loaded.")
            return None
        if message.author.bot:
            self.logger.debug("[Reactor] check_message called for a bot message, ignoring.")
            return None

        user_id_str = str(message.author.id)
        channel_id_str = str(message.channel.id)
        self.logger.debug(f"[Reactor] check_message: Checking Message {message.id} from User '{user_id_str}' in Channel '{channel_id_str}' against {len(message_rules)} text/attachment rules.")

        for i, rule in enumerate(message_rules):
            # Check common filters first (user, channel)
            channel_match = (rule.get('channel_id', '*') == '*' or rule.get('channel_id', '*') == channel_id_str)
            user_match = (rule.get('user_id', '*') == '*' or rule.get('user_id', '*') == user_id_str)

            if not channel_match or not user_match:
                continue # Skip rule if channel or user doesn't match

            trigger_type = rule['trigger_type']
            action_name = rule['action']

            # Check specific trigger conditions
            if trigger_type == 'text':
                if 'text_pattern' in rule:
                    pattern = rule['text_pattern']
                    try:
                        if re.search(pattern, message.content):
                            self.logger.info(f"[Reactor] Text rule matched (regex): Pattern='{pattern}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'. Triggering action: '{action_name}' for message {message.id}")
                            return action_name  # Return first match
                    except re.error as e:
                        self.logger.error(f"[Reactor] Regex error during check_message for pattern '{pattern}': {e}")
                        continue  # Skip this rule if regex is somehow invalid despite pre-check
                elif 'text_contains' in rule:
                    substr = rule['text_contains'].lower()
                    if substr in message.content.lower():
                        self.logger.info(f"[Reactor] Text rule matched (substring): Substr='{substr}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'. Triggering action: '{action_name}' for message {message.id}")
                        return action_name  # Return first match

            elif trigger_type == 'attachment':
                target_type = rule['attachment_type'].lower() # Ensure comparison is case-insensitive
                if message.attachments:
                     for attachment in message.attachments:
                         content_type = getattr(attachment, 'content_type', '').lower()
                         # Direct match or wildcard check (e.g., "image/*")
                         type_match = (target_type == '*' or 
                                       content_type == target_type or
                                       (target_type.endswith('/*') and content_type.startswith(target_type[:-1])))
                         if type_match:
                             self.logger.info(f"[Reactor] Attachment rule matched: Type='{rule['attachment_type']}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'. Triggering action: '{action_name}' for message {message.id} (Attachment: {attachment.filename})")
                             return action_name # Return first match
        
        self.logger.debug(f"[Reactor] check_message: No text/attachment rule matched for Message {message.id}.")
        return None # No match

    async def execute_reaction_action(self, action_name, reaction, user):
        """Finds and executes the specified action method for reaction triggers."""
        method_name = f"_react_action_{action_name}" # Construct method name based on convention
        self.logger.debug(f"[Reactor] execute_reaction_action: Looking for method '{method_name}' for action '{action_name}'")
        action_method = getattr(self, method_name, None)

        if action_method and callable(action_method):
            try:
                # Expects methods like: _react_action_some_action(self, reaction, user)
                self.logger.info(f"[Reactor] Executing reaction action method '{action_method.__name__}' for user {user.id}.")
                await action_method(reaction, user) # Pass reaction and user
                self.logger.debug(f"[Reactor] Finished executing reaction action method '{action_method.__name__}'.")
            except Exception as e:
                self.logger.error(f"[Reactor] Error executing reaction action '{action_method.__name__}': {e}")
                self.logger.error(traceback.format_exc())
        else:
            # More specific error log using the constructed name
            self.logger.error(f"[Reactor] Reaction action method '{method_name}' (for action '{action_name}') not found or not callable.")

    async def execute_message_action(self, action_name, message):
        """Finds and executes the specified action method for message triggers."""
        method_name = f"_msg_action_{action_name}" # Construct method name based on convention
        self.logger.debug(f"[Reactor] execute_message_action: Looking for method '{method_name}' for action '{action_name}'")
        action_method = getattr(self, method_name, None)

        if action_method and callable(action_method):
            try:
                # Expects methods like: _msg_action_some_action(self, message)
                self.logger.info(f"[Reactor] Executing message action method '{action_method.__name__}' for message {message.id}.")
                await action_method(message) # Pass message object
                self.logger.debug(f"[Reactor] Finished executing message action method '{action_method.__name__}'.")
            except Exception as e:
                self.logger.error(f"[Reactor] Error executing message action '{action_method.__name__}': {e}")
                self.logger.error(traceback.format_exc())
        else:
             # More specific error log using the constructed name
            self.logger.error(f"[Reactor] Message action method '{method_name}' (for action '{action_name}') not found or not callable.")

    # --- Reaction-Triggered Actions ---
    # Action name in JSON: "request_curation_permission"
    async def _react_action_request_curation_permission(self, reaction: discord.Reaction, curator: discord.User):
        """[Reaction Action] Sends a DM requesting permission or uploads if permission granted."""
        # Call the refactored handler
        await handle_request_curation_permission(
            bot=self.bot, # Added bot instance
            reaction=reaction,
            curator=curator,
            db_handler=self.db_handler,
            openmuse_interactor=self.openmuse_interactor,
            logger=self.logger
        )

    # Action name in JSON: "send_tweet_about_message"
    async def _react_action_send_tweet_about_message(self, reaction, user):
        """[Reaction Action] Initiates the sharing process by calling the handler."""
        await handle_send_tweet_about_message(
            reaction=reaction,
            user=user,
            sharer_instance=self.sharer, # Pass the Reactor's sharer instance
            logger=self.logger,
            db_handler=self.db_handler, # <<< ADDED db_handler
            bot_instance=self.bot, # <<< ADDED bot_instance
            llm_client=self.llm_client # Pass the LLM client instance
        )

    # Added new reaction action method for general logging of reactions
    async def _react_action_log_general_reaction(self, reaction, user):
        """[Reaction Action] Logs a general reaction event for monitoring purposes."""
        message = reaction.message
        self.logger.info(f"[Reactor] _react_action_log_general_reaction: Received reaction {reaction.emoji} by user {user.id} on message {message.id}.")
        
    # Action name in JSON: "upload_attachment_to_supabase"
    async def _react_action_upload_to_openmuse(self, reaction, user):
        """[Reaction Action] Calls the handler for OpenMuse uploads."""
        await handle_upload_to_openmuse(
            reaction=reaction,
            user=user,
            openmuse_interactor=self.openmuse_interactor,
            logger=self.logger
        )

    # Action name in JSON: "prompt_openmuse_share" or a new name like "initiate_workflow_upload"
    async def _react_action_initiate_workflow_upload(self, reaction: discord.Reaction, user: discord.User):
        """[Reaction Action] Initiates the full workflow upload process via the subfeature handler."""
        # user here is the curator who added the reaction
        self.logger.info(f"[Reactor] Action 'initiate_workflow_upload' triggered by curator {user.id} for message {reaction.message.id}")
        await process_workflow_upload_request(
            bot=self.bot,
            reaction=reaction, 
            curator_user=user, # This 'user' is the one who reacted (the curator)
            logger=self.logger, 
            rate_limiter=self.bot.rate_limiter, # Assuming bot has a RateLimiter instance
            db_handler=self.db_handler,
            claude_client=self.llm_client, # Pass the reactor's Claude client instance
            openmuse_interactor=self.openmuse_interactor # Pass the reactor's OpenMuse interactor instance
        )

    # --- Message-Triggered Actions ---
    # Action name in JSON: "log_special_keyword" (Example)
    async def _msg_action_log_special_keyword(self, message: discord.Message):
        """[Message Action] Placeholder: Logs messages identified with a special keyword."""
        self.logger.info(f"ACTION [log_special_keyword]: Special keyword detected in message {message.id} from {message.author.name} ({message.author.id}) in channel {message.channel.name}: '{message.content[:50]}...'")

    async def _msg_action_initiate_dispute_resolution(self, message: discord.Message):
        """[Message Action] Initiates dispute resolution by calling the handler."""
        await handle_initiate_dispute_resolution(
            message=message,
            db_handler=self.db_handler,
            get_llm_response_func=get_llm_response, # Pass the imported get_llm_response
            logger=self.logger,
            dev_mode=self.dev_mode
        )

    # --- Add other action methods following the convention ---
    # e.g., async def _react_action_some_other_reaction(...)
    # e.g., async def _msg_action_process_file(...)

    # --- Add other action methods as defined in your watchlist (reaction or message based) ---
