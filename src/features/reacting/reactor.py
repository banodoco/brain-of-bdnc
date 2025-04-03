# src/features/reacting/reactor.py

import discord
import os
import traceback
import json
import asyncio
from src.features.sharing.sharer import Sharer # Import the Sharer class

# Environment variable for watchlist configuration
WATCHLIST_JSON = os.getenv('REACTION_WATCHLIST', '[]')

class Reactor:
    def __init__(self, logger, sharer_instance: Sharer, dev_mode=False):
        self.logger = logger
        self.dev_mode = dev_mode
        self.sharer = sharer_instance # Store the Sharer instance
        self.watchlist = []
        self._load_watchlist()
        # TODO: Initialize other API clients or shared resources here if needed
        # e.g., self.twitter_api = self.setup_twitter_api()

    def _load_watchlist(self):
        """Loads and parses the reaction watchlist from environment variables."""
        try:
            parsed_watchlist = json.loads(WATCHLIST_JSON)
            if not isinstance(parsed_watchlist, list):
                raise ValueError("REACTION_WATCHLIST is not a valid JSON list.")
            
            self.watchlist = []
            for rule in parsed_watchlist:
                if not isinstance(rule, dict) or 'user_id' not in rule or 'emoji' not in rule or 'action' not in rule:
                    self.logger.warning(f"Skipping invalid rule in REACTION_WATCHLIST: {rule}")
                    continue
                self.watchlist.append(rule)
            
            self.logger.info(f"[Reactor] Loaded {len(self.watchlist)} rules from REACTION_WATCHLIST.")
            if self.dev_mode:
                 for i, rule in enumerate(self.watchlist):
                     self.logger.debug(f"[Reactor] Rule {i+1}: User='{rule['user_id']}', Emoji='{rule['emoji']}', Action='{rule['action']}'")

        except json.JSONDecodeError:
            self.logger.error(f"[Reactor] Failed to parse REACTION_WATCHLIST JSON: {WATCHLIST_JSON}")
            self.watchlist = []
        except ValueError as e:
            self.logger.error(f"[Reactor] Error loading REACTION_WATCHLIST: {e}")
            self.watchlist = []
        except Exception as e:
             self.logger.error(f"[Reactor] Unexpected error loading watchlist: {e}")
             self.logger.error(traceback.format_exc())
             self.watchlist = []

    def check_reaction(self, reaction, user):
        """Checks if a reaction matches any rule in the watchlist and returns the action name if matched."""
        if not self.watchlist:
            self.logger.debug("[Reactor] check_reaction called, but watchlist is empty.")
            return None
        if user.bot:
            # This check is redundant if ReactorCog already filters bots, but safe to keep.
            self.logger.debug("[Reactor] check_reaction called for a bot user, ignoring.")
            return None

        emoji_str = str(reaction.emoji)
        user_id_str = str(user.id)
        self.logger.debug(f"[Reactor] check_reaction: Checking User ID '{user_id_str}' with Emoji '{emoji_str}' against {len(self.watchlist)} rules.")

        for i, rule in enumerate(self.watchlist):
            user_match = (rule['user_id'] == '*' or rule['user_id'] == user_id_str)
            emoji_match = (rule['emoji'] == '*' or rule['emoji'] == emoji_str)
            self.logger.debug(f"[Reactor] Rule {i+1}: Target User='{rule['user_id']}' (Match: {user_match}), Target Emoji='{rule['emoji']}' (Match: {emoji_match})")

            if user_match and emoji_match:
                action_name = rule['action']
                self.logger.info(f"[Reactor] Watchlist rule matched: User='{rule['user_id']}', Emoji='{rule['emoji']}'. Triggering action: '{action_name}' for user {user_id_str} on message {reaction.message.id}")
                return action_name
        
        self.logger.debug(f"[Reactor] check_reaction: No rule matched for User ID '{user_id_str}' with Emoji '{emoji_str}'.")
        return None # No match

    async def execute_action(self, action_name, reaction, user):
        """Finds and executes the specified action method."""
        self.logger.debug(f"[Reactor] execute_action called for action: '{action_name}', User: {user.id}, Emoji: {reaction.emoji}")
        action_method = getattr(self, action_name, None)
                    
        if action_method and callable(action_method):
            try:
                # Run action concurrently
                # Pass reaction and user, the method can decide what it needs
                self.logger.info(f"[Reactor] Executing action method '{action_method.__name__}' for user {user.id}.")
                await action_method(reaction, user)
                self.logger.debug(f"[Reactor] Finished executing action method '{action_method.__name__}'.")
            except Exception as e:
                self.logger.error(f"[Reactor] Error executing action '{action_method.__name__}': {e}")
                self.logger.error(traceback.format_exc())
        else:
            self.logger.error(f"[Reactor] Action method '{action_name}' not found or not callable in Reactor class.")

    # --- Action Methods (Implement these based on your needs) ---

    async def send_tweet_about_message(self, reaction, user):
        """Initiates the sharing process via the Sharer class."""
        message = reaction.message
        self.logger.info(f"[Reactor] Action 'send_tweet_about_message' triggered for message: {message.id} by user {user.id}. Initiating sharing process.")
        
        if self.sharer:
             # Call the Sharer instance to handle the process
             await self.sharer.initiate_sharing_process(reaction, user)
        else:
             self.logger.error("[Reactor] Sharer instance not available. Cannot initiate sharing process.")
             # Optionally send a fallback message or log critical error

    async def log_general_reaction(self, reaction, user):
        """Placeholder action: Logs that a specific reaction occurred."""
        self.logger.info(f"ACTION [log_general_reaction]: User {user.name} ({user.id}) reacted with {reaction.emoji} to message {reaction.message.id}")

    async def pin_reaction_message(self, reaction, user):
        """Placeholder action: Pins the message that was reacted to."""
        message = reaction.message
        self.logger.info(f"ACTION [pin_reaction_message]: Attempting to pin message {message.id} triggered by {user.name} ({user.id}) with {reaction.emoji}")
        try:
            if not message.pinned:
                 await message.pin()
                 self.logger.info(f"Successfully pinned message {message.id}")
            else:
                 self.logger.info(f"Message {message.id} is already pinned.")
        except discord.Forbidden:
            self.logger.error(f"Failed to pin message {message.id}: Missing Permissions")
        except discord.HTTPException as e:
            self.logger.error(f"Failed to pin message {message.id}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error pinning message {message.id}: {e}")
            self.logger.error(traceback.format_exc())

    async def launch_rocket(self, reaction, user):
        """Placeholder action: Example of another custom action."""
        self.logger.info(f"ACTION [launch_rocket]: 🚀 Launch sequence initiated by {user.name} ({user.id}) reacting with {reaction.emoji} on message {reaction.message.id}! (Placeholder)")

    # --- Add other action methods as defined in your watchlist --- 