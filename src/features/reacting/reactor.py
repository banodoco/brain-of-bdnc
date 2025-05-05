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
from src.features.sharing.sharer import Sharer # Import the Sharer class
from src.common.db_handler import DatabaseHandler # <<< Added DB Handler import
from src.common.openmuse_interactor import OpenMuseInteractor # <<< Added OpenMuse Interactor import

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

# --- BEGIN VIEW DEFINITION --- Correctly modify the existing view
class PermissionRequestView(discord.ui.View):
    # Update __init__ to accept necessary parameters
    def __init__(self, *, timeout=86400, author: discord.User, curator: discord.User, message: discord.Message, message_link: str, db_handler, logger, openmuse_interactor):
        super().__init__(timeout=timeout)
        self.author = author
        self.curator = curator
        self.message = message # Store the message
        self.message_link = message_link
        self.db_handler = db_handler
        self.logger = logger
        self.openmuse_interactor = openmuse_interactor # Store the interactor
        self.response_message: Optional[discord.Message] = None # To edit the DM later

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Ensure only the message author can interact
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return False
        return True

    async def disable_all_items(self):
        """Disables all buttons and updates the message if possible."""
        for item in self.children:
            if isinstance(item, Button):
                item.disabled = True
        if self.response_message:
            try:
                await self.response_message.edit(view=self)
                self.logger.debug(f"[Reactor][PermissionView] Disabled buttons for DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to disable view items for message {self.response_message.id}: {e}")
        else:
             self.logger.warning(f"[Reactor][PermissionView] response_message not set, cannot disable buttons via edit.")

    # Modify the existing button callback for ALLOW
    @discord.ui.button(label="Allow", style=discord.ButtonStyle.green, custom_id="permission_allow") # Changed label/ID
    async def allow_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[Reactor][PermissionView] Author {self.author.id} clicked 'Allow' for message {self.message.id}.")

        # Defer response before potentially long operations
        await interaction.response.defer(ephemeral=True, thinking=True) # Defer ephemerally while thinking

        # --- 1. Update Database ---
        success = False
        try:
            self.logger.info(f"[Reactor][PermissionView] Updating permission to TRUE for author {self.author.id}.")
            success = await asyncio.to_thread(
                self.db_handler.update_member_permission_status,
                self.author.id,
                True
            )
            if not success:
                self.logger.error(f"[Reactor][PermissionView] Failed to update permission to TRUE (returned False).")
            else:
                 self.logger.info(f"[Reactor][PermissionView] Successfully updated permission to TRUE.")
        except Exception as db_err:
            self.logger.error(f"[Reactor][PermissionView] Exception updating permission to TRUE: {db_err}", exc_info=True)
            await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please try again later or contact support.", ephemeral=True)
            self.stop()
            return

        if not success:
             await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
             self.stop()
             return

        # --- 2. Attempt to delete original DM --- (Do this early)
        if self.response_message:
            try:
                await self.response_message.delete()
                self.logger.info(f"[Reactor][PermissionView] Deleted original permission request DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to delete original permission DM {self.response_message.id}: {e}")
        else:
            self.logger.warning("[Reactor][PermissionView] response_message not set, cannot delete original DM.")

        # --- 3. Perform Upload --- (Buttons are implicitly disabled as message is deleted)
        # We no longer need to call self.disable_all_items() explicitly

        profile_record = None # Store profile data from upload result
        if not self.message or not self.message.attachments:
             self.logger.warning(f"[Reactor][PermissionView] Original message/attachments missing for {getattr(self.message, 'id', 'Unknown')}. Skipping upload.")
             upload_success_count = 0
             upload_fail_count = 0
             # Try to get profile data anyway, in case it was fetched before attachments were checked
             if self.openmuse_interactor:
                 try:
                     _, profile_record = await self.openmuse_interactor.find_or_create_profile(self.author)
                 except Exception as profile_err:
                      self.logger.error(f"[Reactor][PermissionView] Error fetching profile data when attachments missing: {profile_err}")
             # No attachments, proceed to final feedback without upload details

        elif not self.openmuse_interactor:
             self.logger.error(f"[Reactor][PermissionView] OpenMuseInteractor not available. Cannot upload.")
             await interaction.followup.send("Permission granted, but an internal error occurred preventing the upload. Please contact support.", ephemeral=True)
             self.stop()
             return # Stop if interactor is missing
        else:
            # Perform uploads if attachments exist and interactor is available
            upload_success_count = 0
            upload_fail_count = 0
            for attachment in self.message.attachments:
                self.logger.info(f"[Reactor][PermissionView] Uploading attachment '{attachment.filename}' for message {self.message.id}.")
                try:
                    # Capture both media and profile records
                    media_record, current_profile_record = await self.openmuse_interactor.upload_discord_attachment(
                        attachment=attachment,
                        author=self.author,
                        message=self.message
                    )
                    if media_record:
                        self.logger.info(f"[Reactor][PermissionView] Upload success: '{attachment.filename}'. Media ID: {media_record.get('id')}")
                        upload_success_count += 1
                        # Store the profile record from the latest successful interaction
                        if current_profile_record:
                            profile_record = current_profile_record
                    else:
                        self.logger.error(f"[Reactor][PermissionView] Upload failure: '{attachment.filename}' (media_record is None).")
                        upload_fail_count += 1
                        # Still store profile record even on media failure if available
                        if current_profile_record and not profile_record:
                            profile_record = current_profile_record
                except Exception as upload_ex:
                    self.logger.error(f"[Reactor][PermissionView] Exception during upload of '{attachment.filename}': {upload_ex}", exc_info=True)
                    upload_fail_count += 1

        # --- 4. Send New Confirmation DM to Author --- 
        profile_url = None
        if profile_record:
             username = profile_record.get('username')
             if username:
                  try:
                      # Construct the profile URL safely
                      profile_url = f"https://openmuse.ai/profile/{quote(username)}"
                      self.logger.info(f"[Reactor][PermissionView] Constructed profile URL: {profile_url}")
                  except Exception as quote_err:
                      self.logger.error(f"[Reactor][PermissionView] Error URL-encoding username '{username}': {quote_err}")
             else:
                  self.logger.warning(f"[Reactor][PermissionView] Username not found in profile_record for user {self.author.id}.")
        else:
             self.logger.warning(f"[Reactor][PermissionView] profile_record not available after upload attempt for user {self.author.id}. Cannot generate profile link.")
        
        if profile_url:
             final_content = f"Thanks! You can find your profile [here]({profile_url}) and just log in with Discord to update or edit it."
        else:
             # Fallback message if profile URL couldn't be generated
             final_content = "Thanks! Your permission has been recorded."
             if upload_fail_count > 0:
                  final_content += " Some uploads may have failed, please check with curators."

        try:
             # Send the new message directly to the author's DM channel
             await self.author.send(content=final_content)
             self.logger.info(f"[Reactor][PermissionView] Sent final confirmation DM to author {self.author.id}.")
        except (discord.HTTPException, discord.Forbidden) as send_err:
             self.logger.error(f"[Reactor][PermissionView] Failed to send final confirmation DM to author {self.author.id}: {send_err}")

        # --- 5. Feedback to Curator --- (Keep this part)
        curator_feedback = f"{self.author.mention} granted permission for {self.message_link}. Upload result: {upload_success_count} succeeded, {upload_fail_count} failed."
        try:
            await self.curator.send(curator_feedback)
            self.logger.info(f"[Reactor][PermissionView] Sent upload status feedback to curator {self.curator.id}.")
        except discord.Forbidden:
            self.logger.warning(f"[Reactor][PermissionView] Could not send upload status DM feedback to curator {self.curator.id}.")
        except Exception as e:
             self.logger.error(f"[Reactor][PermissionView] Error sending feedback to curator {self.curator.id}: {e}")

        self.stop() # Stop the view after completion

    # Modify the existing button callback for DENY
    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="permission_deny") # Changed label/ID
    async def deny_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[Reactor][PermissionView] Author {self.author.id} clicked 'Deny' for message {self.message.id}.")

        # Defer response ephemerally
        await interaction.response.defer(ephemeral=True)

        # --- 1. Update Database ---
        success = False
        try:
            self.logger.info(f"[Reactor][PermissionView] Updating permission to FALSE for author {self.author.id}.")
            success = await asyncio.to_thread(
                self.db_handler.update_member_permission_status,
                self.author.id,
                False
            )
            if not success:
                 self.logger.error(f"[Reactor][PermissionView] Failed to update permission to FALSE (returned False).")
            else:
                 self.logger.info(f"[Reactor][PermissionView] Successfully updated permission to FALSE.")
        except Exception as db_err:
            self.logger.error(f"[Reactor][PermissionView] Exception updating permission to FALSE: {db_err}", exc_info=True)
            await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
            self.stop()
            return

        if not success:
             await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
             self.stop()
             return

        # --- 2. Attempt to delete original DM ---
        if self.response_message:
            try:
                await self.response_message.delete()
                self.logger.info(f"[Reactor][PermissionView] Deleted original permission request DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to delete original permission DM {self.response_message.id}: {e}")
        else:
             self.logger.warning("[Reactor][PermissionView] response_message not set, cannot delete original DM.")

        # --- 3. Send New Confirmation DM to Author ---
        final_content = "No problem, thank you for your response!"
        try:
            # Send the new message directly to the author's DM channel
            await self.author.send(content=final_content)
            self.logger.info(f"[Reactor][PermissionView] Sent denial confirmation DM to author {self.author.id}.")
        except (discord.HTTPException, discord.Forbidden) as send_err:
            self.logger.error(f"[Reactor][PermissionView] Failed to send denial confirmation DM to author {self.author.id}: {send_err}")

        # --- 4. Feedback to Curator --- (Keep this part)
        curator_feedback = f"{self.author.mention} denied permission for {self.message_link}."
        try:
            await self.curator.send(curator_feedback)
            self.logger.info(f"[Reactor][PermissionView] Sent denial feedback to curator {self.curator.id}.")
        except discord.Forbidden:
            self.logger.warning(f"[Reactor][PermissionView] Could not send denial DM feedback to curator {self.curator.id}.")
        except Exception as e:
            self.logger.error(f"[Reactor][PermissionView] Error sending denial feedback to curator {self.curator.id}: {e}")


        self.stop() # Stop the view

    async def on_timeout(self):
        self.logger.info(f"[Reactor][PermissionView] Permission request view timed out for author {self.author.id}, message {getattr(self.message, 'id', 'Unknown')}.")
        # Edit the original DM to show it expired
        if self.response_message:
            try:
                # Disable buttons on timeout as well
                for item in self.children:
                    if isinstance(item, Button):
                        item.disabled = True
                await self.response_message.edit(content=f"This permission request for {self.message_link} has expired.", view=self)
            except discord.HTTPException as e:
                 self.logger.warning(f"[Reactor][PermissionView] Failed to edit message on timeout: {e}")
        # No need to update DB, permission remains NULL
        # No need to inform curator unless desired

# --- END VIEW DEFINITION ---

class Reactor:
    def __init__(self, logger, sharer_instance: Sharer, db_handler: DatabaseHandler, openmuse_interactor: OpenMuseInteractor, dev_mode=False):
        self.logger = logger
        self.dev_mode = dev_mode
        self.sharer = sharer_instance # Store the Sharer instance
        self.db_handler = db_handler # Store DB Handler instance
        self.openmuse_interactor = openmuse_interactor # <<< Store OpenMuse Interactor instance
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
                        valid = True
                    else:
                        self.logger.warning(f"[Reactor] Skipping invalid 'reaction' rule #{i+1} (missing 'user_id' or 'emoji'): {rule}")
                elif trigger_type == 'text':
                    if 'text_pattern' in rule:
                        # Ensure channel_id and user_id default to '*' if not present
                        rule.setdefault('channel_id', '*')
                        rule.setdefault('user_id', '*')
                        try:
                            re.compile(rule['text_pattern']) # Validate regex
                            valid = True
                        except re.error as e:
                           self.logger.warning(f"[Reactor] Skipping invalid 'text' rule #{i+1} (invalid regex '{rule['text_pattern']}'): {e}")
                    else:
                        self.logger.warning(f"[Reactor] Skipping invalid 'text' rule #{i+1} (missing 'text_pattern'): {rule}")
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
                          log_str += f", Pattern='{rule.get('text_pattern')}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'"
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
            user_match = (rule['user_id'] == '*' or rule['user_id'] == user_id_str)
            emoji_match = (rule['emoji'] == '*' or rule['emoji'] == emoji_str)
            self.logger.debug(f"[Reactor] Rule {i+1} Check: Target User='{rule['user_id']}', Target Emoji='{rule['emoji']}'. Incoming User='{user_id_str}', Incoming Emoji='{emoji_str}'. User Match: {user_match}, Emoji Match: {emoji_match}")

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
                pattern = rule['text_pattern']
                try:
                    if re.search(pattern, message.content):
                        self.logger.info(f"[Reactor] Text rule matched: Pattern='{pattern}', Channel='{rule.get('channel_id', '*')}', User='{rule.get('user_id', '*')}'. Triggering action: '{action_name}' for message {message.id}")
                        return action_name # Return first match
                except re.error as e:
                    self.logger.error(f"[Reactor] Regex error during check_message for pattern '{pattern}': {e}")
                    continue # Skip this rule if regex is somehow invalid despite pre-check

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
        message = reaction.message
        author = message.author
        message_link = message.jump_url
        # Add check for openmuse_interactor existence
        if not hasattr(self, 'openmuse_interactor') or self.openmuse_interactor is None:
             self.logger.error("[Reactor][Permission] OpenMuseInteractor not available. Cannot proceed.")
             # Optionally react to the original message with an error indicator
             # try: await message.add_reaction("⚙️") except Exception: pass
             return

        self.logger.info(f"[Reactor][Permission] Action 'request_curation_permission' triggered by curator {curator.id} ({curator.display_name}) on message {message.id} by author {author.id} ({author.display_name}).")

        if author.bot:
            self.logger.info(f"[Reactor][Permission] Author {author.id} is a bot. Skipping permission request.")
            return
        '''
        if author.id == curator.id:
            self.logger.info(f"[Reactor][Permission] Curator {curator.id} reacted to their own message {message.id}. Skipping permission request.")
            # Optionally send feedback to the curator
            # try:
            #     await curator.send(f"You can't request curation permission for your own message: {message_link}")
            # except discord.Forbidden:
            #     self.logger.warning(f"[Reactor][Permission] Could not send feedback DM to curator {curator.id}.")
            return
        '''
        if not self.db_handler:
            self.logger.error("[Reactor][Permission] Database handler not available. Cannot request curation permission.")
            # Optionally react to the original message with an error indicator
            # try:
            #     await message.add_reaction("⚙️") # Cog/Settings emoji
            # except Exception:
            #     pass
            return

        # Check author's current permission status
        try:
            author_member_data = await asyncio.to_thread(self.db_handler.get_member, author.id)

            if not author_member_data:
                 self.logger.info(f"[Reactor][Permission] Author {author.id} not found in DB. Creating member entry.")
                 # Attempt to create the member with basic info
                 # Ensure we have necessary info like username. display_name might be None.
                 success = await asyncio.to_thread(
                     self.db_handler.create_or_update_member,
                     member_id=author.id,
                     username=author.name,
                     display_name=getattr(author, 'display_name', None), # Use display_name if available
                     global_name=getattr(author, 'global_name', None),
                     avatar_url=str(author.display_avatar.url) if author.display_avatar else None,
                     discriminator=getattr(author, 'discriminator', None),
                     bot=author.bot,
                     system=author.system
                 )
                 if not success:
                    self.logger.error(f"[Reactor][Permission] Failed to create database entry for author {author.id}. Aborting permission request.")
                    # Optionally react to the original message
                    # try: await message.add_reaction("💾") except Exception: pass
                    return
                 # After creation, permission_to_curate will be NULL, so we can proceed
                 self.logger.info(f"[Reactor][Permission] Member entry created for author {author.id}. Proceeding with DM.")
                 permission_status = None # Explicitly set to None after creation
            else:
                permission_status = author_member_data.get('permission_to_curate')
                self.logger.info(f"[Reactor][Permission] Author {author.id} found in DB. Current permission_to_curate status: {permission_status}")

            # Check if permission is already decided (True or False/1 or 0, not None)
            if permission_status is not None:
                # Check truthiness (handles both Python True and integer 1)
                if permission_status:
                    self.logger.info(f"[Reactor][Permission] Author {author.id} has already granted permission (status: {permission_status}). Proceeding with upload for message {message.id}.")
                    # --- UPLOAD LOGIC for existing permission ---
                    if not message.attachments:
                        self.logger.warning(f"[Reactor][Permission] Message {message.id} has no attachments to upload, despite existing permission.")
                        # Optionally notify curator
                        # try: await curator.send(f"{author.display_name} granted permission previously, but the message {message_link} has no attachments.")
                        # except discord.Forbidden: pass
                        return

                    upload_success_count = 0
                    upload_fail_count = 0
                    for attachment in message.attachments:
                        self.logger.info(f"[Reactor][Permission] Uploading attachment '{attachment.filename}' for message {message.id} due to existing permission.")
                        try:
                            # Assuming self.openmuse_interactor is available
                            media_record, profile_record = await self.openmuse_interactor.upload_discord_attachment(
                                attachment=attachment,
                                author=author,
                                message=message
                            )
                            if media_record:
                                self.logger.info(f"[Reactor][Permission] Successfully uploaded attachment '{attachment.filename}' for message {message.id}.")
                                upload_success_count += 1
                            else:
                                self.logger.error(f"[Reactor][Permission] Failed to upload attachment '{attachment.filename}' for message {message.id} (media_record is None).")
                                upload_fail_count += 1
                        except Exception as upload_ex:
                            self.logger.error(f"[Reactor][Permission] Exception during upload of attachment '{attachment.filename}': {upload_ex}", exc_info=True)
                            upload_fail_count += 1

                    # Feedback to curator about upload result
                    feedback_msg = f"Attempted upload for {author.display_name}'s message ({message_link}) based on existing permission: {upload_success_count} succeeded, {upload_fail_count} failed."
                    try:
                         await curator.send(feedback_msg)
                         self.logger.info(f"[Reactor][Permission] Sent upload status feedback to curator {curator.id}.")
                         # Optionally react to the original message
                         # if upload_fail_count == 0 and upload_success_count > 0:
                         #     await message.add_reaction("✅") # Success
                         # elif upload_fail_count > 0 and upload_success_count > 0:
                         #     await message.add_reaction("⚠️") # Partial success
                         # elif upload_fail_count > 0 and upload_success_count == 0:
                         #      await message.add_reaction("❌") # Failure
                    except discord.Forbidden:
                         self.logger.warning(f"[Reactor][Permission] Could not send upload status DM feedback to curator {curator.id}.")
                    except Exception as react_ex:
                         self.logger.warning(f"[Reactor][Permission] Could not add reaction to message {message.id} after upload attempt: {react_ex}")

                    return # Stop here, upload attempted based on existing permission
                    # --- END UPLOAD LOGIC ---
                else: # permission_status is False, 0, or anything else non-None and not truthy
                    status_str = "denied"
                    # Log the actual status value for clarity
                    self.logger.info(f"[Reactor][Permission] Author {author.id} has already {status_str} permission (status: {permission_status}). No action needed.")
                # Optionally DM the curator that permission is already set
                # try:
                #      await curator.send(f"Permission for {author.display_name}'s message ({message_link}) has already been {status_str}.")
                # except discord.Forbidden:
                #      self.logger.warning(f"[Reactor][Permission] Could not send feedback DM to curator {curator.id}.")
                return

            # Proceed to send DM if permission is NULL
            self.logger.info(f"[Reactor][Permission] Permission status for author {author.id} is NULL. Sending permission request DM.")

            dm_content = (
                f"Hi {author.mention}! {curator.mention} would like to curate your work to [OpenMuse](https://openmuse.ai/. ): {message_link}\n\n"
                f"It will be hosted under your profile name there - for you to edit and update if/when you claim an account by signing up with your Discord account.\n\n"
                f"Do you give permission? (This request expires in 24 hours)"
            )

            # Pass the interactor and original message to the view
            view = PermissionRequestView(
                author=author,
                curator=curator,
                message=message, # Pass the full message object
                message_link=message_link,
                db_handler=self.db_handler,
                logger=self.logger,
                openmuse_interactor=self.openmuse_interactor # Pass the interactor instance
            )

            try:
                sent_message = await author.send(content=dm_content, view=view)
                view.response_message = sent_message # Store the sent message in the view for editing later (e.g., on timeout)
                self.logger.info(f"[Reactor][Permission] Successfully sent permission request DM to author {author.id} for message {message.id}. DM ID: {sent_message.id}")
                # Optionally react to the original message to show DM was sent
                # try: await message.add_reaction("✉️") except Exception: pass

            except discord.Forbidden:
                self.logger.warning(f"[Reactor][Permission] Could not send permission request DM to author {author.id}. They may have DMs disabled.")
                # Optionally react to the original message to show DM failed
                # try: await message.add_reaction("🚫") except Exception: pass
                 # Maybe DM the curator?
                # try:
                #     await curator.send(f"Could not send curation permission request to {author.display_name} for message {message_link}. Their DMs might be closed.")
                # except discord.Forbidden:
                #     pass # Ignore if curator DMs are closed too
            except Exception as e:
                self.logger.error(f"[Reactor][Permission] Error sending permission request DM to author {author.id}: {e}")
                self.logger.error(traceback.format_exc())
                # Optionally react to the original message
                # try: await message.add_reaction("⚠️") except Exception: pass

        except Exception as e:
             self.logger.error(f"[Reactor][Permission] Unexpected error during 'request_curation_permission' for message {message.id}: {e}")
             self.logger.error(traceback.format_exc())
             # Add a generic error reaction to the original message
             # try: await message.add_reaction("🆘") except Exception: pass

    # Action name in JSON: "send_tweet_about_message"
    async def _react_action_send_tweet_about_message(self, reaction, user):
        """[Reaction Action] Initiates the sharing process via the Sharer class."""
        message = reaction.message
        self.logger.info(f"[Reactor] Action 'send_tweet_about_message' triggered for message: {message.id} by user {user.id}. Initiating sharing process.")

        if self.sharer:
             # Call the Sharer instance to handle the process
             await self.sharer.initiate_sharing_process_from_reaction(reaction, user)
        else:
             self.logger.error("[Reactor] Sharer instance not available. Cannot initiate sharing process.")
             
    # Added new reaction action method for general logging of reactions
    async def _react_action_log_general_reaction(self, reaction, user):
        """[Reaction Action] Logs a general reaction event for monitoring purposes."""
        message = reaction.message
        self.logger.info(f"[Reactor] _react_action_log_general_reaction: Received reaction {reaction.emoji} by user {user.id} on message {message.id}.")
        
    # Action name in JSON: "upload_attachment_to_supabase"
    async def _react_action_upload_to_openmuse(self, reaction, user):
        """[Reaction Action] Finds the first attachment, ensures user profile exists, uploads to Supabase Storage, adds media record, and handles video thumbnails."""
        message = reaction.message
        discord_user_id_str = str(user.id)
        self.logger.info(f"[Reactor] Action 'upload_attachment_to_supabase' triggered for message {message.id} by user {discord_user_id_str} with emoji {reaction.emoji}.")
        self.logger.debug(f"[Reactor] Entering _react_action_upload_attachment_to_supabase. Checking author: reacting user {user.id} vs message author {message.author.id}")
        
        # --- Ensure interactor is available ---
        if not self.openmuse_interactor:
             self.logger.error("[Reactor] OpenMuse Interactor not available. Cannot execute 'upload_attachment_to_supabase'.")
             try: await message.add_reaction("⚙️")
             except Exception: pass
             return

        # --- Keep initial checks ---
        if user.id != message.author.id:
            self.logger.debug(f"[Reactor] User {user.id} reacted with {reaction.emoji}, but is not the author ({message.author.id}) of message {message.id}. Skipping action.")
            # Add feedback reaction?
            # try: await message.add_reaction("🤔") except Exception: pass
            return

        if not message.attachments:
            self.logger.warning(f"[Reactor] No attachments found on message {message.id} for 'upload_attachment_to_supabase' action (triggered by author {user.id}).")
            try: await message.add_reaction("📎") # Paperclip emoji for no attachment
            except Exception: pass
            return
        
        # --- Process the first attachment using the Interactor ---
        attachment = message.attachments[0]
        self.logger.info(f"[Reactor] Calling OpenMuseInteractor.upload_discord_attachment for '{attachment.filename}'")

        # --- Call the Interactor --- 
        media_record, profile_data = await self.openmuse_interactor.upload_discord_attachment(
            attachment=attachment,
            author=user, # Pass the reacting user (who is confirmed to be the author)
            message=message
        )

        # --- Handle the result --- 
        if media_record:
            # Success!
            self.logger.info(f"[Reactor] Upload successful via Interactor for message {message.id}. Media ID: {media_record.get('id')}")
            try: await message.add_reaction("✔️")
            except Exception as e:
                 self.logger.error(f"[Reactor] Failed to add success reaction: {e}")

        else:
            # Upload or DB insert failed, profile step might have succeeded
            self.logger.warning(f"[Reactor] OpenMuseInteractor failed to create media record for message {message.id}. Profile Data: {bool(profile_data)}")

            # Determine appropriate reaction based on possibility (could enhance Interactor return value)
            failure_emoji = "❌" # Default failure emoji
            
            # Check for size error first
            if attachment.size > self.openmuse_interactor.MAX_FILE_SIZE_BYTES:
                 failure_emoji = "💾"
                 # Send DM for size error - nested inside the size check
                 dm_message = "Sorry, we're too poor to host files this large right now :("
                 try:
                     await user.send(dm_message)
                     self.logger.info(f"[Reactor] Sent file size limit DM to user {user.id}.")
                 except discord.Forbidden:
                     self.logger.warning(f"[Reactor] Failed to send file size limit DM to user {user.id}. They may have DMs disabled.")
                 except Exception as e:
                     self.logger.error(f"[Reactor] Error sending file size limit DM to user {user.id}: {e}")
            
            # Check for profile failure ONLY if size wasn't the issue
            elif not profile_data:
                 # If profile data is also None, profile step likely failed
                 failure_emoji = "👤"
                 # Consider adding a DM here if profile step failed?

            # Add the determined failure reaction (runs regardless of which condition above was met)
            try:
                await message.add_reaction(failure_emoji)
            except Exception as e:
                 self.logger.error(f"[Reactor] Failed to add failure reaction '{failure_emoji}': {e}")

        self.logger.info(f"[Reactor] Finished action '_react_action_upload_attachment_to_supabase' for message {message.id}.")

    # --- Message-Triggered Actions ---
    # Action name in JSON: "log_special_keyword" (Example)
    async def _msg_action_log_special_keyword(self, message: discord.Message):
        """[Message Action] Placeholder: Logs messages identified with a special keyword."""
        self.logger.info(f"ACTION [log_special_keyword]: Special keyword detected in message {message.id} from {message.author.name} ({message.author.id}) in channel {message.channel.name}: '{message.content[:50]}...'")

    # --- Add other action methods following the convention ---
    # e.g., async def _react_action_some_other_reaction(...)
    # e.g., async def _msg_action_process_file(...)

    # --- Add other action methods as defined in your watchlist (reaction or message based) --- 