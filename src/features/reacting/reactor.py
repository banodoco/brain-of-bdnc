# src/features/reacting/reactor.py

import discord
import os
import traceback
import json
import asyncio
import re # Added for text matching
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

# --- BEGIN VIEW DEFINITION ---
class PermissionRequestView(View):
    def __init__(self, author: discord.Member, curator: discord.User, message_link: str, db_handler: DatabaseHandler, logger):
        super().__init__(timeout=86400.0) # 24 hour timeout for the view
        self.author = author
        self.curator = curator
        self.message_link = message_link
        self.db_handler = db_handler
        self.logger = logger
        self.response_message = None # To store the message sent with the view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow the author of the original message to interact
        if interaction.user.id == self.author.id:
            return True
        else:
            await interaction.response.send_message("Sorry, only the author of the work can respond.", ephemeral=True)
            return False

    async def disable_view(self, interaction: discord.Interaction, final_message: str):
        """Disables all buttons and updates the message."""
        for item in self.children:
            if isinstance(item, Button):
                item.disabled = True
        # Use the original interaction or the stored message to edit
        edit_target = interaction.message if interaction.message else self.response_message
        if edit_target:
            await edit_target.edit(content=final_message, view=self)
        else:
             self.logger.warning(f"Could not find message to edit after permission response for author {self.author.id}")

    @button(label="Give Permission", style=discord.ButtonStyle.success, custom_id="give_curate_permission")
    async def give_permission_callback(self, interaction: discord.Interaction, button: Button):
        self.logger.info(f"[Reactor][Permission] User {self.author.id} GRANTED permission to curator {self.curator.id} for message {self.message_link}")
        # Update database
        success = await asyncio.to_thread(self.db_handler.update_member_permission_status, self.author.id, True)

        response_text = f"Thank you! Permission granted to @{self.curator.display_name} to curate your work."
        if not success:
            self.logger.error(f"[Reactor][Permission] Failed to update database for user {self.author.id} granting permission.")
            response_text += "\n(There was an issue saving your preference, please contact an admin)."

        await interaction.response.defer() # Acknowledge interaction immediately
        await self.disable_view(interaction, response_text)
        self.stop() # Stop the view from listening

    @button(label="Deny Permission", style=discord.ButtonStyle.danger, custom_id="deny_curate_permission")
    async def deny_permission_callback(self, interaction: discord.Interaction, button: Button):
        self.logger.info(f"[Reactor][Permission] User {self.author.id} DENIED permission to curator {self.curator.id} for message {self.message_link}")
        # Update database
        success = await asyncio.to_thread(self.db_handler.update_member_permission_status, self.author.id, False)

        response_text = f"Okay, permission denied. @{self.curator.display_name} will not curate this work."
        if not success:
            self.logger.error(f"[Reactor][Permission] Failed to update database for user {self.author.id} denying permission.")
            response_text += "\n(There was an issue saving your preference, please contact an admin)."

        await interaction.response.defer() # Acknowledge interaction immediately
        await self.disable_view(interaction, response_text)
        self.stop() # Stop the view from listening

    async def on_timeout(self):
        self.logger.info(f"[Reactor][Permission] Permission request timed out for author {self.author.id}, curator {self.curator.id}, message {self.message_link}")
        timeout_message = "This permission request has expired."
        # Attempt to edit the original message if we stored it
        if self.response_message:
            try:
                for item in self.children:
                    if isinstance(item, Button):
                        item.disabled = True
                await self.response_message.edit(content=timeout_message, view=self)
            except discord.NotFound:
                self.logger.warning(f"[Reactor][Permission] Could not find DM message {self.response_message.id} to edit on timeout.")
            except discord.Forbidden:
                 self.logger.warning(f"[Reactor][Permission] Missing permissions to edit DM message {self.response_message.id} on timeout.")
            except Exception as e:
                 self.logger.error(f"[Reactor][Permission] Error editing DM message {self.response_message.id} on timeout: {e}")
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
        """[Reaction Action] Sends a DM to the message author requesting permission to curate their work."""
        message = reaction.message
        author = message.author
        message_link = message.jump_url
        self.logger.info(f"[Reactor][Permission] Action 'request_curation_permission' triggered by curator {curator.id} ({curator.display_name}) on message {message.id} by author {author.id} ({author.display_name}).")

        if author.bot:
            self.logger.info(f"[Reactor][Permission] Author {author.id} is a bot. Skipping permission request.")
            return

        if author.id == curator.id:
            self.logger.info(f"[Reactor][Permission] Curator {curator.id} reacted to their own message {message.id}. Skipping permission request.")
            # Optionally send feedback to the curator
            # try:
            #     await curator.send(f"You can't request curation permission for your own message: {message_link}")
            # except discord.Forbidden:
            #     self.logger.warning(f"[Reactor][Permission] Could not send feedback DM to curator {curator.id}.")
            return

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

            # Check if permission is already decided (True or False, not None)
            if permission_status is not None: # Checks for both True and False
                status_str = "granted" if permission_status else "denied"
                self.logger.info(f"[Reactor][Permission] Author {author.id} has already {status_str} permission. No action needed.")
                # Optionally DM the curator that permission is already set
                # try:
                #      await curator.send(f"Permission for {author.display_name}'s message ({message_link}) has already been {status_str}.")
                # except discord.Forbidden:
                #      self.logger.warning(f"[Reactor][Permission] Could not send feedback DM to curator {curator.id}.")
                return

            # Proceed to send DM if permission is NULL
            self.logger.info(f"[Reactor][Permission] Permission status for author {author.id} is NULL. Sending permission request DM.")

            dm_content = (
                f"Hi @{author.display_name}! @{curator.display_name} would like to curate your work to OpenMuse: {message_link}\n\n"
                f"It will be hosted under your profile name there - for you to edit and update if/when you claim an account by signing up with your Discord account at https://openmuse.ai/.\n\n"
                f"Do you give permission? (This request expires in 24 hours)"
            )

            view = PermissionRequestView(author=author, curator=curator, message_link=message_link, db_handler=self.db_handler, logger=self.logger)

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