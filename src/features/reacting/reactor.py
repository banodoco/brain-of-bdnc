# src/features/reacting/reactor.py

import discord
import os
import traceback
import json
import asyncio
import re # Added for text matching
from typing import Optional # Added for Optional type hint
from urllib.parse import quote # <<<--- Added Import
from discord.ui import View, Button, button # <<< Added for Views
from discord.ext import commands # <<< ADDED for bot_instance type hint and usage
from src.features.sharing.sharer import Sharer # Import the Sharer class
from src.common.db_handler import DatabaseHandler # <<< Added DB Handler import
from src.common.openmuse_interactor import OpenMuseInteractor # <<< Added OpenMuse Interactor import
from src.common.llm.claude_client import ClaudeClient # Added LLM Client import
from .subfeatures.permission_handler import handle_request_curation_permission, PermissionRequestView
import logging # Added to define logger for constants section if needed
from datetime import datetime, timedelta, timezone # For dispute resolution timing & timezone.utc

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

# Environment variable for watchlist configuration
# Example format:
# [
#   {"trigger_type": "reaction", "user_id": "123", "emoji": "🐦", "action": "send_tweet_about_message"},
#   {"trigger_type": "text", "text_pattern": "(?i)urgent", "action": "log_urgent_message", "channel_id": "*", "user_id": "*"},
#   {"trigger_type": "attachment", "attachment_type": "image/png", "action": "process_image_attachment", "user_id": "*"}
# ]
WATCHLIST_JSON = os.getenv('REACTION_WATCHLIST', '[]')

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
        self.watchlist = []
        self._load_watchlist()

    def _load_watchlist(self):
        """Loads and parses the reaction watchlist from environment variables."""
        # --- BEGIN ADDED LOG ---
        # Log the raw value obtained from the environment variable BEFORE parsing
        raw_watchlist_env = os.getenv('REACTION_WATCHLIST')
        self.logger.debug(f"[Reactor] Raw REACTION_WATCHLIST from env: {raw_watchlist_env}")
        # Use the same value for parsing attempt
        watchlist_to_parse = raw_watchlist_env if raw_watchlist_env is not None else '[]' # Use default if None
        # --- END ADDED LOG ---
        try:
            # --- MODIFIED LINE --- Use the variable logged above
            parsed_watchlist = json.loads(watchlist_to_parse)
            # --- END MODIFIED LINE ---
            if not isinstance(parsed_watchlist, list):
                raise ValueError("REACTION_WATCHLIST is not a valid JSON list.")
            
            self.watchlist = []
            valid_rules = 0
            for i, rule in enumerate(parsed_watchlist):
                if not isinstance(rule, dict) or 'action' not in rule:
                    self.logger.warning(f"[Reactor] Skipping invalid rule #{i+1} (missing 'action'): {rule}")
                    continue

                # Determine trigger type, default to 'reaction' for backward compatibility
                trigger_type = rule.get('trigger_type', 'reaction').lower()
                rule['trigger_type'] = trigger_type # Ensure type is stored in lowercase

                valid = False
                if trigger_type == 'reaction':
                    if 'user_id' in rule and 'emoji' in rule:
                        # Ensure user_id is a list or '*'
                        if isinstance(rule['user_id'], str) and rule['user_id'] != '*':
                            rule['user_id'] = [rule['user_id']]
                        elif not isinstance(rule['user_id'], list) and rule['user_id'] != '*':
                            self.logger.warning(f"[Reactor] Skipping invalid 'reaction' rule #{i+1} ('user_id' must be a string, a list of strings, or '*'): {rule}")
                            continue # Skip to next rule
                        valid = True
                    else:
                        self.logger.warning(f"[Reactor] Skipping invalid 'reaction' rule #{i+1} (missing 'user_id' or 'emoji'): {rule}")
                elif trigger_type == 'text':
                    # Accept either a full regex pattern *or* a simple substring to search for.
                    if 'text_pattern' in rule:
                        # Ensure channel_id and user_id default to '*' if not present
                        rule.setdefault('channel_id', '*')
                        rule.setdefault('user_id', '*')
                        try:
                            re.compile(rule['text_pattern'])  # Validate regex
                            valid = True
                        except re.error as e:
                            self.logger.warning(f"[Reactor] Skipping invalid 'text' rule #{i+1} (invalid regex '{rule['text_pattern']}'): {e}")
                    elif 'text_contains' in rule:
                        # Simple, case-insensitive substring match – no regex validation required
                        if not isinstance(rule['text_contains'], str):
                            self.logger.warning(f"[Reactor] Skipping invalid 'text' rule #{i+1} ('text_contains' is not a string): {rule}")
                        else:
                            rule.setdefault('channel_id', '*')
                            rule.setdefault('user_id', '*')
                            valid = True
                    else:
                        self.logger.warning(f"[Reactor] Skipping invalid 'text' rule #{i+1} (missing 'text_pattern' or 'text_contains'): {rule}")
                elif trigger_type == 'attachment':
                    if 'attachment_type' in rule:
                         # Ensure channel_id and user_id default to '*' if not present
                        rule.setdefault('channel_id', '*')
                        rule.setdefault('user_id', '*')
                        valid = True
                    else:
                        self.logger.warning(f"[Reactor] Skipping invalid 'attachment' rule #{i+1} (missing 'attachment_type'): {rule}")
                else:
                     self.logger.warning(f"[Reactor] Skipping rule #{i+1} with unknown trigger_type: '{trigger_type}'")

                if valid:
                    self.watchlist.append(rule)
                    valid_rules += 1
                
            self.logger.info(f"[Reactor] Loaded {valid_rules} valid rules ({len(self.watchlist)}) from REACTION_WATCHLIST.")
            if self.dev_mode:
                 for i, rule in enumerate(self.watchlist):
                     # Log based on type
                     log_str = f"[Reactor] Rule {i+1}: Type='{rule['trigger_type']}', Action='{rule['action']}'"
                     if rule['trigger_type'] == 'reaction':
                         log_str += f", User='{rule.get('user_id')}', Emoji='{rule.get('emoji')}'"
                     elif rule['trigger_type'] == 'text':
                          pattern_repr = rule.get('text_pattern', rule.get('text_contains'))
                          log_str += f", Pattern='{pattern_repr}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'"
                     elif rule['trigger_type'] == 'attachment':
                          log_str += f", Type='{rule.get('attachment_type')}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'"
                     self.logger.debug(log_str)

        except json.JSONDecodeError as e: # --- MODIFIED LINE --- Added exception details
            self.logger.error(f"[Reactor] Failed to parse REACTION_WATCHLIST JSON. Value was: {watchlist_to_parse}. Error: {e}")
            self.watchlist = []
        except ValueError as e:
            self.logger.error(f"[Reactor] Error loading REACTION_WATCHLIST: {e}")
            self.watchlist = []
        except Exception as e:
             self.logger.error(f"[Reactor] Unexpected error loading watchlist: {e}")
             self.logger.error(traceback.format_exc())
             self.watchlist = []

    def check_reaction(self, reaction, user):
        """Checks if a reaction matches any 'reaction' rule in the watchlist and returns the action name if matched."""
        reaction_rules = [rule for rule in self.watchlist if rule.get('trigger_type', 'reaction') == 'reaction']
        if not reaction_rules:
            self.logger.debug("[Reactor] check_reaction called, but no reaction rules are loaded.")
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
        message_rules = [rule for rule in self.watchlist if rule.get('trigger_type') in ['text', 'attachment']]
        if not message_rules:
            self.logger.debug("[Reactor] check_message called, but no text/attachment rules are loaded.")
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