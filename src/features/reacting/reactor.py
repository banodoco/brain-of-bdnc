# src/features/reacting/reactor.py

import discord
import os
import traceback
import json
import asyncio
import re # Added for text matching
from src.features.sharing.sharer import Sharer # Import the Sharer class
from supabase import create_client, Client # Added for Supabase
import cv2 # Add opencv import (requires installation)
import numpy as np # Add numpy import (requires installation)
import tempfile # Add tempfile import
from io import BytesIO # Keep BytesIO if needed elsewhere, though temp file used here
from urllib.parse import quote # <<< Added for URL encoding usernames
import httpx # Import httpx to potentially catch specific errors like WriteError

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
    def __init__(self, logger, sharer_instance: Sharer, supabase_url: str | None, supabase_key: str | None, dev_mode=False):
        self.logger = logger
        self.dev_mode = dev_mode
        self.sharer = sharer_instance # Store the Sharer instance
        # Store credentials passed from main.py
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.watchlist = []
        self.supabase: Client | None = self._init_supabase() # Initialize Supabase client
        self._load_watchlist()
        # TODO: Initialize other API clients or shared resources here if needed
        # e.g., self.twitter_api = self.setup_twitter_api()

    def _init_supabase(self) -> Client | None:
        """Initializes the Supabase client if credentials are provided."""
        # Use instance variables instead of module-level constants
        if self.supabase_url and self.supabase_key:
            try:
                self.logger.info("[Reactor] Initializing Supabase client.")
                # Use instance variables
                return create_client(self.supabase_url, self.supabase_key)
            except Exception as e:
                self.logger.error(f"[Reactor] Failed to initialize Supabase client: {e}")
                # Log the specific key being used (masked) during the failed attempt inside Reactor
                masked_key_reactor = f"{self.supabase_key[:5]}...{self.supabase_key[-5:]}" if self.supabase_key and len(self.supabase_key) > 10 else self.supabase_key
                self.logger.error(f"[Reactor] Attempted connection with URL: {self.supabase_url} and Key: {masked_key_reactor}")
                return None
        else:
            self.logger.warning("[Reactor] Supabase URL or Service Key not provided to Reactor. Supabase-dependent actions will be skipped.")
            return None

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
    async def _react_action_upload_attachment_to_supabase(self, reaction, user):
        """[Reaction Action] Finds the first attachment, ensures user profile exists, uploads to Supabase Storage, adds media record, and handles video thumbnails."""
        message = reaction.message
        discord_user_id_str = str(user.id)
        self.logger.info(f"[Reactor] Action 'upload_attachment_to_supabase' triggered for message {message.id} by user {discord_user_id_str} with emoji {reaction.emoji}.")
        self.logger.debug(f"[Reactor] Entering _react_action_upload_attachment_to_supabase. Checking author: reacting user {user.id} vs message author {message.author.id}")
        
        if user.id != message.author.id:
            self.logger.debug(f"[Reactor] User {user.id} reacted with {reaction.emoji}, but is not the author ({message.author.id}) of message {message.id}. Skipping action.")
            return

        if not self.supabase:
            self.logger.error("[Reactor] Supabase client not initialized. Cannot execute 'upload_attachment_to_supabase'.")
            return

        if not message.attachments:
            self.logger.warning(f"[Reactor] No attachments found on message {message.id} for 'upload_attachment_to_supabase' action (triggered by author {user.id}).")
            return
        
        # --- Process the first attachment ---
        attachment = message.attachments[0]
        filename = attachment.filename
        content_type = attachment.content_type or 'application/octet-stream'

        # --- Add File Size Check ---
        MAX_FILE_SIZE_BYTES = 512 * 1024 * 1024 # 512 MiB
        if attachment.size > MAX_FILE_SIZE_BYTES:
            self.logger.warning(f"[Reactor] Attachment '{filename}' ({attachment.size} bytes) exceeds maximum size ({MAX_FILE_SIZE_BYTES} bytes). Skipping upload.")
            dm_message = "Sorry, we're too poor to host files this large right now :("
            try:
                await user.send(dm_message)
                self.logger.info(f"[Reactor] Sent file size limit DM to user {user.id}.")
            except discord.Forbidden:
                self.logger.warning(f"[Reactor] Failed to send file size limit DM to user {user.id}. They may have DMs disabled.")
            except Exception as e:
                 self.logger.error(f"[Reactor] Error sending file size limit DM to user {user.id}: {e}")
            # Add a reaction to the original message to indicate the issue
            try:
                 await message.add_reaction("💾") # Disk emoji for size issue
            except Exception as react_ex:
                 self.logger.error(f"[Reactor] Failed to add size limit reaction to message {message.id}: {react_ex}")
            return # Stop processing this action
        # --- End File Size Check ---

        # Define Supabase targets
        video_bucket_name = "videos" # Bucket for the main video/file
        thumbnail_bucket_name = "thumbnails" # Bucket for extracted thumbnails
        profiles_table = "profiles" 
        media_table = "media"       

        profile_id_uuid = None
        placeholder_image_url = None # Initialize placeholder URL
        public_url = None # Initialize public_url for the main file
        calculated_aspect_ratio = None # << Initialize aspect ratio

        try:
            # --- 1. Select Profile based on discord_user_id ---
            self.logger.info(f"[Reactor] Checking for existing profile with discord_user_id: {discord_user_id_str}")
            try:
                # Select id, discord_connected, username, display_name
                select_response = await asyncio.to_thread(
                    self.supabase.table(profiles_table)
                    .select('id, discord_connected, username, display_name') # <<< Added username, display_name
                    .eq('discord_user_id', discord_user_id_str)
                    .limit(1)
                    .execute
                )
                # --- ADDED LOGGING AND CHECK FOR NONE RESPONSE ---
                self.logger.debug(f"[Reactor] Raw select_response from Supabase query: {select_response}")
                if select_response is None:
                    self.logger.error(f"[Reactor] Supabase select query for discord_user_id '{discord_user_id_str}' returned None unexpectedly. Aborting action.") 
                    await message.add_reaction("⚠️") 
                    return 
                # --- END LOGGING AND CHECK ---

                existing_profile_data = select_response.data
            except Exception as sel_ex:
                 # --- ADDED TRACEBACK ---
                 self.logger.error(f"[Reactor] Error selecting profile for discord_user_id '{discord_user_id_str}': {sel_ex}")
                 self.logger.error(traceback.format_exc()) # Log the full traceback
                 await message.add_reaction("⚠️")
                 return

            # --- 1a. If Profile Exists, Conditionally Update It ---
            if existing_profile_data:
                profile_id_uuid = existing_profile_data['id']
                initial_discord_connected = existing_profile_data.get('discord_connected') 
                supabase_username = existing_profile_data.get('username') # <<< Get username from Supabase
                existing_display_name = existing_profile_data.get('display_name') # <<< Get display_name from Supabase
                self.logger.info(f"[Reactor] Found existing profile. UUID: {profile_id_uuid}, User: {supabase_username}, Display: {existing_display_name}, Connected: {initial_discord_connected}")
                
                # Prepare data for update conditionally
                profile_update_data = {
                    # Always update avatar
                    'avatar_url': str(user.display_avatar.url) if user.display_avatar else None,
                }
                # ONLY update display_name if it's currently NULL or empty in Supabase
                if not existing_display_name:
                    profile_update_data['display_name'] = user.display_name 
                    self.logger.info(f"[Reactor] Existing display_name is NULL/empty, will update.")
                else:
                    self.logger.info(f"[Reactor] Existing display_name ('{existing_display_name}') found, will not update.")
                
                # Only perform update if there's actually data to update (at least avatar_url)
                if profile_update_data:
                    self.logger.info(f"[Reactor] --> Updating existing profile {profile_id_uuid} with data: {profile_update_data}")
                    try:
                         await asyncio.to_thread(
                            self.supabase.table(profiles_table)
                            .update(profile_update_data)
                            .eq('id', profile_id_uuid)
                            .execute
                        )
                         self.logger.info(f"[Reactor] <-- Successfully updated profile {profile_id_uuid}.")
                    except Exception as upd_ex:
                         self.logger.error(f"[Reactor] Error updating profile {profile_id_uuid}: {upd_ex}")
                else:
                    self.logger.info(f"[Reactor] No data requires updating for profile {profile_id_uuid}.")

            # --- 1b. If Profile Does Not Exist, handle it ---
            else:
                self.logger.info(f"[Reactor] No existing profile found for discord_user_id {discord_user_id_str}. Informing user to sign up.")
                # --- START: Send DM to user --- 
                dm_message = "You need to sign up for a profile on OpenMuse first to upload using this reaction: https://openmuse.ai/" # Removed @ symbol
                try:
                    await user.send(dm_message)
                    self.logger.info(f"[Reactor] Sent profile required DM to user {user.id}.")
                except discord.Forbidden:
                    self.logger.warning(f"[Reactor] Failed to send profile required DM to user {user.id}. DMs disabled?")
                except Exception as dm_ex:
                    self.logger.error(f"[Reactor] Error sending profile required DM to user {user.id}: {dm_ex}")
                # --- END: Send DM to user ---
                
                # --- Add reaction to original message ---
                try:
                    await message.add_reaction("👤") # User profile emoji
                    self.logger.info(f"[Reactor] Added profile required reaction to message {message.id}")
                except Exception as react_ex:
                    self.logger.error(f"[Reactor] Failed to add profile required reaction to message {message.id}: {react_ex}")
                    
                return # Stop the function here, do not proceed to upload

            # --- Sanity Check: Ensure profile_id_uuid is set --- 
            # This check should only be relevant if a profile WAS found and updated
            if not profile_id_uuid:
                self.logger.error(f"[Reactor] profile_id_uuid is not set after profile handling for discord_user_id '{discord_user_id_str}'. Aborting.")
                await message.add_reaction("🆘") # Different emoji for unexpected state
                return

            # --- 2. Download the file content from Discord ---
            self.logger.info(f"[Reactor] --> Attempting to read attachment '{filename}' from message {message.id}.") 
            file_bytes = await attachment.read()
            self.logger.info(f"[Reactor] <-- Read {len(file_bytes)} bytes for attachment '{filename}'.") 

            # --- 2.5 Extract and Upload Thumbnail IF VIDEO --- 
            thumbnail_upload_success = False # Flag for successful thumbnail upload
            if content_type.startswith('video/'):
                self.logger.info(f"[Reactor] Attachment '{filename}' is a video ({content_type}). Attempting thumbnail extraction and ratio calculation using OpenCV.")
                temp_video_file = None
                cap = None # <<< Reverted back to cap
                frame = None 
                try:
                    # Write video bytes to a temporary file
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as temp_video_file:
                        temp_video_file.write(file_bytes)
                        temp_video_path = temp_video_file.name
                    self.logger.info(f"[Reactor] Video bytes written to temporary file: {temp_video_path}")
                    
                    # Use OpenCV VideoCapture
                    cap = cv2.VideoCapture(temp_video_path)
                    if not cap.isOpened():
                        self.logger.error(f"[Reactor] OpenCV could not open temporary video file: {temp_video_path}")
                    else:
                        ret, frame = cap.read() # Read the first frame
                        if ret:
                            self.logger.info(f"[Reactor] Successfully read first frame from video using OpenCV.")
                            # --- Calculate Aspect Ratio using OpenCV frame.shape ---
                            try:
                                h, w = frame.shape[:2]
                                if h > 0: 
                                    calculated_aspect_ratio = round(w / h, 2) 
                                    self.logger.info(f"[Reactor] OpenCV Calculated aspect ratio: {calculated_aspect_ratio} (w={w}, h={h})")
                                else:
                                    self.logger.warning("[Reactor] Frame height is 0, cannot calculate aspect ratio.")
                            except Exception as ar_ex:
                                 self.logger.error(f"[Reactor] Error calculating aspect ratio from frame shape: {ar_ex}")
                            # ---------------------------------------------------------
                            
                            # Encode frame as JPEG using OpenCV
                            is_success, buffer = cv2.imencode(".jpg", frame)
                            if is_success:
                                thumbnail_bytes = buffer.tobytes()
                                self.logger.info(f"[Reactor] Encoded frame to {len(thumbnail_bytes)} bytes (JPEG).")
                                
                                # Define thumbnail path
                                thumbnail_filename = f"{os.path.splitext(filename)[0]}_thumb.jpg"
                                thumbnail_storage_path = f"user_media/{profile_id_uuid}/{message.id}_{thumbnail_filename}"
                                self.logger.info(f"[Reactor] --> Attempting to upload thumbnail to bucket '{thumbnail_bucket_name}' at path '{thumbnail_storage_path}'.")
                                
                                # --- BEGIN THUMBNAIL UPLOAD RETRY LOGIC ---
                                for attempt in range(MAX_UPLOAD_ATTEMPTS):
                                    try:
                                        await asyncio.to_thread(
                                            self.supabase.storage.from_(thumbnail_bucket_name).upload,
                                            path=thumbnail_storage_path,
                                            file=thumbnail_bytes,
                                            file_options={"content-type": "image/jpeg", "upsert": "true"}
                                        )
                                        self.logger.info(f"[Reactor] <-- Successfully uploaded thumbnail '{thumbnail_filename}' (Attempt {attempt + 1}).")
                                        thumbnail_upload_success = True
                                        break # Exit loop on success
                                    except (Exception, httpx.WriteError) as upload_ex: # Catch specific write errors too
                                        self.logger.warning(f"[Reactor] Thumbnail upload attempt {attempt + 1}/{MAX_UPLOAD_ATTEMPTS} failed: {upload_ex}")
                                        if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                                            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
                                            self.logger.info(f"[Reactor] Retrying thumbnail upload in {delay} seconds...")
                                            await asyncio.sleep(delay)
                                        else:
                                            self.logger.error(f"[Reactor] Thumbnail upload failed after {MAX_UPLOAD_ATTEMPTS} attempts.")
                                            # Add reaction but don't stop the whole process
                                            try:
                                                await message.add_reaction("🖼️") # Indicate thumbnail issue
                                            except Exception as react_ex:
                                                 self.logger.error(f"[Reactor] Failed to add thumbnail error reaction: {react_ex}")
                                            # thumbnail_upload_success remains False
                                # --- END THUMBNAIL UPLOAD RETRY LOGIC ---

                                # Only get URL if thumbnail upload succeeded
                                if thumbnail_upload_success:
                                    try:
                                        self.logger.info(f"[Reactor] --> Attempting to get public URL for thumbnail '{thumbnail_storage_path}'.")
                                        thumb_url_resp = await asyncio.to_thread(
                                                self.supabase.storage.from_(thumbnail_bucket_name).get_public_url, thumbnail_storage_path
                                        )
                                        placeholder_image_url = thumb_url_resp 
                                        self.logger.info(f"[Reactor] <-- Got thumbnail public URL: {placeholder_image_url}")
                                    except Exception as url_ex:
                                        self.logger.error(f"[Reactor] Failed to get public URL for successfully uploaded thumbnail '{thumbnail_storage_path}': {url_ex}")
                                        # placeholder_image_url remains None

                            else:
                                self.logger.error("[Reactor] Failed to encode video frame (obtained via OpenCV) to JPEG using OpenCV.")
                        else:
                            self.logger.error("[Reactor] Failed to read first frame from video using OpenCV.")
                except Exception as thumb_ex:
                     self.logger.error(f"[Reactor] Error during thumbnail/ratio processing (OpenCV): {thumb_ex}")
                     self.logger.error(traceback.format_exc())
                finally:
                    # Release the OpenCV capture
                    if cap and cap.isOpened():
                        cap.release()
                        self.logger.info("[Reactor] Released video capture.")
                    # Remove the temporary file
                    if temp_video_file and os.path.exists(temp_video_path):
                         try:
                             os.remove(temp_video_path)
                             self.logger.info(f"[Reactor] Removed temporary video file: {temp_video_path}")
                         except OSError as e:
                             self.logger.error(f"[Reactor] Error removing temporary file {temp_video_path}: {e}")
            else:
                self.logger.info(f"[Reactor] Attachment '{filename}' is not a video ({content_type}). Skipping thumbnail extraction and aspect ratio calculation.")

            # --- 3. Upload Original File to Supabase Storage ---
            storage_path = f"user_media/{profile_id_uuid}/{message.id}_{filename}" 
            self.logger.info(f"[Reactor] --> Attempting to upload original file '{filename}' to Supabase Storage bucket '{video_bucket_name}' at path '{storage_path}'.") 
            
            # --- BEGIN ORIGINAL FILE UPLOAD RETRY LOGIC ---
            main_upload_success = False
            for attempt in range(MAX_UPLOAD_ATTEMPTS):
                try:
                    await asyncio.to_thread(
                        self.supabase.storage.from_(video_bucket_name).upload,
                        path=storage_path,
                        file=file_bytes,
                        file_options={"content-type": content_type, "upsert": "true"} 
                    )
                    self.logger.info(f"[Reactor] <-- Successfully uploaded original file '{filename}' to Supabase Storage path '{storage_path}' (Attempt {attempt + 1}).")
                    main_upload_success = True
                    break # Exit loop on success
                except (Exception, httpx.WriteError) as upload_ex:
                     self.logger.warning(f"[Reactor] Original file upload attempt {attempt + 1}/{MAX_UPLOAD_ATTEMPTS} failed: {upload_ex}")
                     if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                         delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
                         self.logger.info(f"[Reactor] Retrying original file upload in {delay} seconds...")
                         await asyncio.sleep(delay)
                     else:
                         self.logger.error(f"[Reactor] Original file upload failed after {MAX_UPLOAD_ATTEMPTS} attempts. Aborting action for this message.")
                         # --- SEND DM ON FAILURE ---
                         dm_message = f"Sorry, I couldn't upload your file '{filename}' to OpenMuse after {MAX_UPLOAD_ATTEMPTS} attempts. \nPlease try again later. If the problem persists, let an admin know."
                         try:
                             await user.send(dm_message)
                             self.logger.info(f"[Reactor] Sent upload failure DM to user {user.id}.")
                         except discord.Forbidden:
                             self.logger.warning(f"[Reactor] Failed to send upload failure DM to user {user.id}. They may have DMs disabled.")
                         except Exception as dm_ex:
                             self.logger.error(f"[Reactor] Error sending upload failure DM to user {user.id}: {dm_ex}")
                         # --- END SEND DM ---
                         # Add reaction and STOP the function execution
                         try:
                             await message.add_reaction("🔁") # Indicate retry failure
                         except Exception as react_ex:
                              self.logger.error(f"[Reactor] Failed to add retry failure reaction: {react_ex}")
                         return # <<<< EXIT THE FUNCTION HERE
            # --- END ORIGINAL FILE UPLOAD RETRY LOGIC ---

            # --- 4. Get Public URL for the Original File (Only if upload succeeded) ---
            if main_upload_success:
                try:
                    self.logger.info(f"[Reactor] --> Attempting to get public URL for original file '{storage_path}'.")
                    public_url_response = await asyncio.to_thread(
                         self.supabase.storage.from_(video_bucket_name).get_public_url, storage_path
                    )
                    public_url = public_url_response
                    self.logger.info(f"[Reactor] <-- Got public URL for original file '{storage_path}': {public_url}") 
                except Exception as url_ex:
                    self.logger.error(f"[Reactor] Failed to get public URL for successfully uploaded original file '{storage_path}': {url_ex}")
                    # public_url remains None. Consider if this should also cause failure/reaction.
                    # For now, log and proceed to insert media record without URL.
                    try:
                        await message.add_reaction("🔗") # Link error reaction
                    except Exception: pass # Ignore reaction error here

            else:
                # This case should not be reachable due to the 'return' in the retry loop failure
                self.logger.error("[Reactor] Reached code path after main upload failure - this should not happen.")
                return

            # --- 5. Insert record into Supabase Media Table ---
            # Determine classification based on channel name
            classification = None
            if message.channel and hasattr(message.channel, 'name'): # Check if channel and name exist
                if message.channel.name.lower().startswith('art'):
                    classification = 'art'
                    self.logger.info(f"[Reactor] Setting classification to 'art' based on channel name '{message.channel.name}'.")
                else:
                    classification = 'gen'
                    self.logger.info(f"[Reactor] Setting classification to 'gen' as channel name '{message.channel.name}' does not start with 'art'.")
            else:
                 classification = 'gen' # Default if channel name unavailable
                 self.logger.warning("[Reactor] Could not determine channel name, defaulting classification to 'gen'.")

            media_data = {
                'user_id': profile_id_uuid, 
                'title': None if content_type.startswith('video/') else filename, # Set title to None for videos
                'url': public_url, 
                'placeholder_image': placeholder_image_url, 
                'type': content_type if not content_type.startswith('video/') else 'video', # Use original type or 'video'
                'classification': classification, 
                'admin_status': 'Listed', 
                'user_status': 'View', 
                'metadata': { 
                    "discord_message_id": str(message.id),
                    "discord_channel_id": str(message.channel.id),
                    "discord_guild_id": str(message.guild.id) if message.guild else None,
                    "discord_attachment_url": attachment.url, 
                    "reacted_by_discord_user_id": discord_user_id_str,
                    "trigger_emoji": str(reaction.emoji),
                    "aspectRatio": calculated_aspect_ratio # <<< CHANGED KEY NAME HERE
                }
            }
            self.logger.info(f"[Reactor] --> Attempting to insert record into Supabase table '{media_table}'. Data: {media_data}") 
            await asyncio.to_thread(
                 self.supabase.table(media_table).insert(media_data).execute
            )
            self.logger.info(f"[Reactor] <-- Successfully inserted record into table '{media_table}'.") # Simplified log message

            # Optionally react to the original message to indicate success
            self.logger.info(f"[Reactor] --> Adding ✔️ reaction to message {message.id}.")
            await message.add_reaction("✔️") # <<< Changed emoji here 
            self.logger.info(f"[Reactor] <-- Added ✔️ reaction.")

            # --- 6. Check discord_connected and send Welcome DM if needed --- 
            if initial_discord_connected == False: 
                self.logger.info(f"[Reactor] Profile {profile_id_uuid} discord_connected is False. Attempting to send Welcome DM.")
                try:
                    # Use the username fetched/set from/in Supabase for the URL
                    username_for_url = supabase_username 
                    if not username_for_url:
                        # Fallback to current Discord username if Supabase username wasn't retrieved (shouldn't happen)
                        self.logger.warning(f"[Reactor] Supabase username not available for profile {profile_id_uuid}, falling back to current Discord username for DM URL.")
                        username_for_url = user.name
                    
                    formatted_username = quote(username_for_url, safe='') 
                    profile_url = f"https://openmuse.ai/profile/{formatted_username}"
                    dm_message = (
                        f"Your first upload to OpenMuse via Discord has been successful!\n\n"
                        f"You can see your profile here: {profile_url}"
                    )
                    
                    self.logger.info(f"[Reactor] --> Sending Welcome DM to user {user.id} (URL uses username: '{username_for_url}').")
                    await user.send(dm_message)
                    self.logger.info(f"[Reactor] <-- Successfully sent Welcome DM to user {user.id}.")

                    # Update discord_connected to True
                    self.logger.info(f"[Reactor] --> Attempting to update discord_connected to True for profile {profile_id_uuid}.")
                    update_response = await asyncio.to_thread(
                        self.supabase.table(profiles_table)
                        .update({'discord_connected': True})
                        .eq('id', profile_id_uuid)
                        .execute
                    )
                    # You might want to check update_response for errors if needed
                    self.logger.info(f"[Reactor] <-- Updated discord_connected status for profile {profile_id_uuid}.")

                except discord.Forbidden:
                     self.logger.warning(f"[Reactor] Failed to send Welcome DM to user {user.id}. They may have DMs disabled.")
                except Exception as dm_update_ex:
                     self.logger.error(f"[Reactor] Error sending Welcome DM or updating discord_connected for profile {profile_id_uuid}: {dm_update_ex}")
                     self.logger.error(traceback.format_exc())
            else:
                 # Log why DM wasn't sent (already connected or status unknown)
                 if initial_discord_connected is None:
                     self.logger.info(f"[Reactor] Skipping Welcome DM for profile {profile_id_uuid} as initial status could not be determined.")
                 else: # Must be True if not False or None
                     self.logger.info(f"[Reactor] Skipping Welcome DM for profile {profile_id_uuid} as discord_connected is already True.")

            self.logger.info(f"[Reactor] <-- Finished action '_react_action_upload_attachment_to_supabase' successfully for message {message.id}.")

        except discord.HTTPException as e:
             self.logger.error(f"[Reactor] Discord HTTP error during operation for message {message.id}: {e}")
             # Ensure reaction is added even if error happens before the retry logic completes
             try: await message.add_reaction("❌") 
             except Exception: pass
        except Exception as e: 
            # This block will now catch errors *not* handled by the retry loops (e.g., profile handling, media insert)
            # or if the retry logic itself has an unexpected error.
            self.logger.error(f"[Reactor] Unhandled error during Supabase operation for message {message.id} (Profile: {profile_id_uuid}): {e}")
            self.logger.error(traceback.format_exc())
            # Ensure reaction is added
            try: await message.add_reaction("❌") 
            except Exception: pass

    # --- Message-Triggered Actions ---
    # Action name in JSON: "log_special_keyword" (Example)
    async def _msg_action_log_special_keyword(self, message: discord.Message):
        """[Message Action] Placeholder: Logs messages identified with a special keyword."""
        self.logger.info(f"ACTION [log_special_keyword]: Special keyword detected in message {message.id} from {message.author.name} ({message.author.id}) in channel {message.channel.name}: '{message.content[:50]}...'")

    # --- Add other action methods following the convention ---
    # e.g., async def _react_action_some_other_reaction(...)
    # e.g., async def _msg_action_process_file(...)

    # --- Add other action methods as defined in your watchlist (reaction or message based) --- 