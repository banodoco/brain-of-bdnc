# src/common/openmuse_interactor.py

import logging
import asyncio
from typing import Optional, Dict, Any
import discord
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from postgrest.exceptions import APIError # For specific Postgrest errors
import re  # For URL validation

# --- Added imports needed for upload logic ---
import cv2
import tempfile
import os
import httpx
from urllib.parse import quote # For profile URL generation
# --- End Added imports ---

# Helper function for logging truncation (can be outside class or a private method)
def _get_truncated_data_for_logging(data_obj: Any, avatar_key: str = 'discord_avatar_base64', max_len: int = 70) -> Any:
    if isinstance(data_obj, dict):
        copied_dict = data_obj.copy()
        if avatar_key in copied_dict and isinstance(copied_dict[avatar_key], str):
            full_avatar_string = copied_dict[avatar_key]
            prefix_to_keep = ""
            if "base64," in full_avatar_string:
                parts = full_avatar_string.split("base64,", 1)
                prefix_to_keep = parts[0] + "base64,"
                actual_b64_data = parts[1]
            else:
                actual_b64_data = full_avatar_string
            
            if len(actual_b64_data) > max_len:
                copied_dict[avatar_key] = prefix_to_keep + actual_b64_data[:max_len] + "... (truncated)"
        return copied_dict
    elif isinstance(data_obj, list):
        return [_get_truncated_data_for_logging(item, avatar_key, max_len) for item in data_obj]
    # Add handling for Supabase response objects if needed, by accessing their .data attribute
    elif hasattr(data_obj, 'data'): # Heuristic for Supabase response like objects
        # Don't modify original object, create a representation for logging
        class ResponseLogWrapper:
            def __init__(self, original_response, truncated_data):
                self.original_response = original_response
                self.truncated_data = truncated_data
            def __repr__(self):
                original_attrs = {attr: getattr(self.original_response, attr) for attr in dir(self.original_response) if not attr.startswith('_') and not callable(getattr(self.original_response, attr)) and attr != 'data'}
                return f"SupabaseResponse(data={self.truncated_data}, {original_attrs})"

        truncated_data_list = _get_truncated_data_for_logging(data_obj.data, avatar_key, max_len)
        return ResponseLogWrapper(data_obj, truncated_data_list)
    return data_obj

# Define the structure based on provided schema (for reference)
# ProfileTable = {
#     'id': 'uuid',
#     'username': 'text',
#     'avatar_url': 'text',
#     'created_at': 'timestamptz',
#     'display_name': 'text',
#     'description': 'text',
#     'links': 'ARRAY[_text]',
#     'real_name': 'text',
#     'background_image_url': 'text',
#     'discord_user_id': 'text',
#     'discord_username': 'text',
#     'discord_connected': 'boolean'
# }

PROFILES_TABLE = "profiles" # Define table name as a constant
MEDIA_TABLE = "media" # Define table name as a constant
VIDEO_BUCKET_NAME = "videos" # Define bucket name
THUMBNAIL_BUCKET_NAME = "thumbnails" # Define bucket name
WORKFLOWS_BUCKET_NAME = "workflows" # Added for workflow JSONs

# --- Added constants for upload logic ---
MAX_UPLOAD_ATTEMPTS = 3
BASE_RETRY_DELAY_SECONDS = 2
MAX_FILE_SIZE_BYTES = 512 * 1024 * 1024 # 512 MiB - Make this configurable?
# --- End Added constants ---

# Refined helper function for truncating avatar string within a dictionary for logging
def _truncate_avatar_in_dict_for_logging(data_dict: Optional[Dict], avatar_key: str = 'discord_avatar_base64', max_len: int = 70) -> Optional[Dict]:
    if not isinstance(data_dict, dict):
        return data_dict # Return as is if not a dict (e.g., None)
    copied_dict = data_dict.copy() # Work on a copy for logging
    if avatar_key in copied_dict and isinstance(copied_dict[avatar_key], str):
        full_avatar_string = copied_dict[avatar_key]
        prefix_to_keep = ""
        actual_b64_data = full_avatar_string
        try:
            prefix_end_idx = full_avatar_string.find("base64,")
            if prefix_end_idx != -1:
                prefix_to_keep = full_avatar_string[:prefix_end_idx + len("base64,")]
                actual_b64_data = full_avatar_string[len(prefix_to_keep):]
        except Exception:
            pass # If string operations fail, proceed with actual_b64_data as full_avatar_string
        
        if len(actual_b64_data) > max_len:
            copied_dict[avatar_key] = prefix_to_keep + actual_b64_data[:max_len] + "... (truncated)"
    return copied_dict

class OpenMuseInteractor:
    """Handles interactions with the OpenMuse Supabase backend."""

    def __init__(self, supabase_url: str, supabase_key: str, logger: logging.Logger):
        """
        Initializes the OpenMuseInteractor.

        Args:
            supabase_url: The URL for the Supabase project.
            supabase_key: The Supabase service role key.
            logger: The logger instance for logging messages.
        """
        self.logger = logger
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.supabase: Optional[Client] = self._init_supabase()

    def _init_supabase(self) -> Optional[Client]:
        """Initializes the Supabase client."""
        if not self.supabase_url or not self.supabase_key:
            self.logger.error("[OpenMuseInteractor] Supabase URL or Service Key is missing. Cannot initialize client.")
            return None
        try:
            self.logger.info("[OpenMuseInteractor] Initializing Supabase client.")
            # Try with ClientOptions (newer API)
            try:
                options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=30)
                client: Client = create_client(self.supabase_url, self.supabase_key, options=options)
            except (AttributeError, TypeError):
                # Fall back to creating client without options if ClientOptions API has changed
                client: Client = create_client(self.supabase_url, self.supabase_key)
            self.logger.info("[OpenMuseInteractor] Supabase client initialized successfully.")
            return client
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Failed to initialize Supabase client: {e}", exc_info=True)
            return None

    async def find_or_create_profile(self, user: discord.User | discord.Member) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Finds a profile by Discord user ID in Supabase. If not found, creates a new profile.
        Optionally updates existing profile details like username, display name, avatar.

        Args:
            user: The discord.User or discord.Member object.

        Returns:
            A tuple containing (profile_data_dict, profile_id_uuid) if successful, otherwise (None, None).
        """
        if not self.supabase:
            self.logger.error("[OpenMuseInteractor] Supabase client not initialized. Cannot find or create profile.")
            return None, None

        discord_user_id_str = str(user.id)
        self.logger.info(f"[OpenMuseInteractor] Finding or creating profile for Discord User ID: {discord_user_id_str} ({user.name})")

        try:
            # --- 1. Try to find existing profile ---
            self.logger.debug(f"[OpenMuseInteractor] Querying Supabase for profile with discord_user_id: {discord_user_id_str}")
            select_response = await asyncio.to_thread(
                self.supabase.table(PROFILES_TABLE)
                .select('*') # Select all columns for potential update checks
                .eq('discord_user_id', discord_user_id_str)
                .limit(1)
                .execute
            )

            self.logger.debug(f"[OpenMuseInteractor] Supabase select response: {_truncate_avatar_in_dict_for_logging(select_response)}")

            if select_response.data:
                # --- 2a. Profile Found - Check for updates ---
                existing_profile = select_response.data[0]
                profile_id_uuid = existing_profile['id']
                self.logger.debug(f"[OpenMuseInteractor] Existing profile data (pre-update check): {_truncate_avatar_in_dict_for_logging(existing_profile)}")
                self.logger.info(f"[OpenMuseInteractor] Found existing profile. UUID: {profile_id_uuid}, Supabase Username: {existing_profile.get('username')}")

                update_data = {}
                # Always update avatar? Or only if changed? Let's always update for simplicity.
                current_avatar_url = str(user.display_avatar.url) if user.display_avatar else None
                if current_avatar_url != existing_profile.get('avatar_url'):
                     update_data['avatar_url'] = current_avatar_url
                     self.logger.debug(f"[OpenMuseInteractor] Profile {profile_id_uuid}: Avatar URL changed/needs update.")

                # Update Discord username if it changed
                if user.name != existing_profile.get('discord_username'):
                     update_data['discord_username'] = user.name
                     self.logger.debug(f"[OpenMuseInteractor] Profile {profile_id_uuid}: Discord username changed.")

                # Update display name only if it's currently NULL/empty in Supabase
                # Or if the Discord display name differs from the Supabase one? Let's update if different.
                current_display_name = getattr(user, 'display_name', user.name) # Use display_name, fallback to username
                if current_display_name != existing_profile.get('display_name'):
                     update_data['display_name'] = current_display_name
                     self.logger.debug(f"[OpenMuseInteractor] Profile {profile_id_uuid}: Display name changed/needs update.")

                # Update Supabase username *only if it's currently NULL*? This is tricky.
                # Let's *not* update the main 'username' automatically from Discord. It should be user-set.
                # if not existing_profile.get('username'):
                #     update_data['username'] = user.name # Or display_name? Risky due to uniqueness.

                if update_data:
                    self.logger.info(f"[OpenMuseInteractor] Updating profile {profile_id_uuid} with data: {update_data}")
                    try:
                        await asyncio.to_thread(
                            self.supabase.table(PROFILES_TABLE)
                            .update(update_data)
                            .eq('id', profile_id_uuid)
                            .execute
                        )
                        self.logger.info(f"[OpenMuseInteractor] Successfully updated profile {profile_id_uuid}.")
                        # Merge updates into existing_profile for return value consistency
                        existing_profile.update(update_data)
                    except APIError as update_err:
                         self.logger.error(f"[OpenMuseInteractor] Supabase API error updating profile {profile_id_uuid}: {update_err}")
                         # Decide whether to return the old data or None on update failure
                         return _truncate_avatar_in_dict_for_logging(existing_profile), profile_id_uuid # Return potentially stale data but with ID
                    except Exception as update_ex:
                         self.logger.error(f"[OpenMuseInteractor] Unexpected error updating profile {profile_id_uuid}: {update_ex}", exc_info=True)
                         return _truncate_avatar_in_dict_for_logging(existing_profile), profile_id_uuid # Return potentially stale data but with ID
                else:
                    self.logger.info(f"[OpenMuseInteractor] Profile {profile_id_uuid} data is up-to-date. No update needed.")

                return _truncate_avatar_in_dict_for_logging(existing_profile), profile_id_uuid # Return existing profile and its ID

            else:
                # --- 2b. Profile Not Found - Create New One ---
                self.logger.info(f"[OpenMuseInteractor] No existing profile found for Discord User ID: {discord_user_id_str}. Creating new profile.")

                # Prepare data for the new profile
                # Use Discord username as the initial Supabase username.
                # WARN: This might conflict if usernames are not unique or change. Consider alternatives.
                initial_username = user.name
                # Check if username already exists (optional, requires another query)
                # ... (add username check if needed) ...

                new_profile_data = {
                    'discord_user_id': discord_user_id_str,
                    'username': initial_username, # Using Discord username initially
                    'discord_username': user.name,
                    'display_name': getattr(user, 'display_name', user.name), # Server display name or global name
                    'avatar_url': str(user.display_avatar.url) if user.display_avatar else None,
                    'discord_connected': False, # Start as not connected, let another process handle welcome/activation
                    # Add defaults for other nullable fields if desired (e.g., empty arrays/strings)
                    'links': [],
                    'description': None,
                    'real_name': None,
                    'background_image_url': None
                }

                self.logger.debug(f"[OpenMuseInteractor] Attempting to insert new profile data: {_truncate_avatar_in_dict_for_logging(new_profile_data)}")

                try:
                    insert_response = await asyncio.to_thread(
                        self.supabase.table(PROFILES_TABLE)
                        .insert(new_profile_data)
                        .execute
                    )
                    self.logger.debug(f"[OpenMuseInteractor] Supabase insert response: {_truncate_avatar_in_dict_for_logging(insert_response)}")

                    if insert_response.data:
                        created_profile = insert_response.data[0]
                        profile_id_uuid = created_profile.get('id')
                        self.logger.debug(f"[OpenMuseInteractor] Created profile data: {_truncate_avatar_in_dict_for_logging(created_profile)}")
                        self.logger.info(f"[OpenMuseInteractor] Successfully created new profile. UUID: {profile_id_uuid}, Username: {created_profile.get('username')}")
                        return _truncate_avatar_in_dict_for_logging(created_profile), profile_id_uuid # Return newly created profile and its ID
                    else:
                        # This case might indicate an error even without an exception (e.g., RLS preventing insert/return)
                        self.logger.error("[OpenMuseInteractor] Supabase insert executed but returned no data. Profile creation might have failed silently.")
                        # Log the response details if possible
                        if hasattr(insert_response, 'status_code'):
                             self.logger.error(f"[OpenMuseInteractor] Insert response status: {insert_response.status_code}")
                        if hasattr(insert_response, 'error'):
                             self.logger.error(f"[OpenMuseInteractor] Insert response error: {insert_response.error}")
                        return None, None

                except APIError as insert_err:
                     # Specific handling for unique constraint violation on username (if applicable)
                     if 'duplicate key value violates unique constraint' in str(insert_err) and 'profiles_username_key' in str(insert_err):
                         self.logger.error(f"[OpenMuseInteractor] Username '{initial_username}' already exists. Cannot create profile automatically. User might need manual intervention or alternative username strategy.", exc_info=False) # Avoid full trace for expected errors
                         # Consider attempting to find the profile by username *now*?
                         # Or just fail here. Let's fail for now.
                         return None, None
                     else:
                          self.logger.error(f"[OpenMuseInteractor] Supabase API error creating profile for {discord_user_id_str}: {insert_err}")
                          return None, None
                except Exception as insert_ex:
                    self.logger.error(f"[OpenMuseInteractor] Unexpected error creating profile for {discord_user_id_str}: {insert_ex}", exc_info=True)
                    return None, None

        except APIError as select_err:
             self.logger.error(f"[OpenMuseInteractor] Supabase API error finding profile for {discord_user_id_str}: {select_err}")
             return None, None
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Unexpected error during find_or_create_profile for {discord_user_id_str}: {e}", exc_info=True)
            return None, None

    def _is_valid_url(self, url: str) -> bool:
        """Return True if the URL is a valid HTTP(S) URL."""
        if not url or not isinstance(url, str):
            return False
        # Simple regex for http(s) URLs
        return re.match(r"^https?://[\w\-\.]+(:\d+)?(/[\w\-\.~:/?#\[\]@!$&'()*+,;=%]*)?$", url) is not None

    async def _upload_bytes_to_storage(
        self,
        file_bytes: bytes,
        bucket_name: str,
        storage_path: str,
        content_type: str,
        upsert: bool = True
    ) -> Optional[str]:
        """
        Uploads raw bytes to a specified Supabase storage bucket with retry logic.

        Args:
            file_bytes: The raw bytes of the file to upload.
            bucket_name: The name of the target Supabase storage bucket.
            storage_path: The desired path (including filename) within the bucket.
            content_type: The MIME type of the file.
            upsert: Whether to overwrite the file if it already exists.

        Returns:
            The public URL of the uploaded file if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error(f"[OpenMuseInteractor_UploadBytes] Supabase client not initialized. Cannot upload to {bucket_name}/{storage_path}.")
            return None

        self.logger.info(f"[OpenMuseInteractor_UploadBytes] --> Attempting to upload {len(file_bytes)} bytes to bucket '{bucket_name}' at path '{storage_path}' (Content-Type: {content_type}).")
        
        upload_successful = False
        for attempt in range(MAX_UPLOAD_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    self.supabase.storage.from_(bucket_name).upload,
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": str(upsert).lower()} # upsert needs to be string "true" or "false"
                )
                self.logger.info(f"[OpenMuseInteractor_UploadBytes] <-- Successfully uploaded to '{bucket_name}/{storage_path}' (Attempt {attempt + 1}).")
                upload_successful = True
                break
            except (Exception, httpx.WriteError) as upload_ex: # Catch generic Exception and specific httpx.WriteError
                self.logger.warning(f"[OpenMuseInteractor_UploadBytes] Upload attempt {attempt + 1}/{MAX_UPLOAD_ATTEMPTS} for '{bucket_name}/{storage_path}' failed: {upload_ex}")
                if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                    delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
                    self.logger.info(f"[OpenMuseInteractor_UploadBytes] Retrying upload in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(f"[OpenMuseInteractor_UploadBytes] Upload for '{bucket_name}/{storage_path}' failed after {MAX_UPLOAD_ATTEMPTS} attempts.")
        
        if not upload_successful:
            return None

        # Get Public URL
        try:
            self.logger.info(f"[OpenMuseInteractor_UploadBytes] --> Getting public URL for '{bucket_name}/{storage_path}'.")
            public_url_response = await asyncio.to_thread(
                self.supabase.storage.from_(bucket_name).get_public_url, storage_path
            )
            public_url = public_url_response
            self.logger.info(f"[OpenMuseInteractor_UploadBytes] <-- Got public URL: {public_url}")

            if isinstance(public_url, str):
                trimmed_url = public_url.strip()
                if trimmed_url != public_url:
                    self.logger.info(f"[OpenMuseInteractor_UploadBytes] Trimmed whitespace from URL: '{public_url}' -> '{trimmed_url}'")
                public_url = trimmed_url
            
            if not self._is_valid_url(public_url):
                self.logger.warning(f"[OpenMuseInteractor_UploadBytes] Invalid or empty URL after trimming: '{public_url}' for '{bucket_name}/{storage_path}'. Setting to None.")
                return None
            return public_url
        except Exception as url_ex:
            self.logger.error(f"[OpenMuseInteractor_UploadBytes] Failed to get public URL for '{bucket_name}/{storage_path}': {url_ex}")
            return None

    async def upload_file_to_storage(
        self,
        file_source: discord.Attachment | bytes,
        bucket_name: str,
        storage_path: str,
        content_type: Optional[str] = None,
        upsert: bool = True
    ) -> Optional[str]:
        """
        Generalized method to upload a file (from discord.Attachment or raw bytes)
        to a specified Supabase storage bucket.

        Args:
            file_source: A discord.Attachment object or raw bytes of the file.
            bucket_name: The target Supabase storage bucket (e.g., "workflows", "videos").
            storage_path: The desired path (including filename) within the bucket.
            content_type: The MIME type of the file. If None and file_source is
                          discord.Attachment, it's inferred from the attachment.
                          Defaults to 'application/octet-stream' if not determinable.
            upsert: Whether to overwrite the file if it already exists.

        Returns:
            The public URL of the uploaded file if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error(f"[OpenMuseInteractor_UploadFile] Supabase client not initialized. Cannot upload.")
            return None

        file_bytes: Optional[bytes] = None
        final_content_type: str

        if isinstance(file_source, discord.Attachment):
            self.logger.info(f"[OpenMuseInteractor_UploadFile] Reading bytes from discord.Attachment '{file_source.filename}'.")
            try:
                file_bytes = await file_source.read()
                final_content_type = content_type or file_source.content_type or 'application/octet-stream'
                self.logger.info(f"[OpenMuseInteractor_UploadFile] Read {len(file_bytes)} bytes. Determined content type: {final_content_type}.")
            except discord.HTTPException as e:
                self.logger.error(f"[OpenMuseInteractor_UploadFile] Discord HTTP error reading attachment {file_source.filename}: {e}")
                return None
            except Exception as e:
                self.logger.error(f"[OpenMuseInteractor_UploadFile] Error reading attachment {file_source.filename}: {e}", exc_info=True)
                return None
        elif isinstance(file_source, bytes):
            file_bytes = file_source
            final_content_type = content_type or 'application/octet-stream'
            self.logger.info(f"[OpenMuseInteractor_UploadFile] Using provided {len(file_bytes)} bytes. Content type: {final_content_type}.")
        else:
            self.logger.error(f"[OpenMuseInteractor_UploadFile] Invalid file_source type: {type(file_source)}. Must be discord.Attachment or bytes.")
            return None

        if not file_bytes: # Should be caught above, but as a safeguard
            self.logger.error(f"[OpenMuseInteractor_UploadFile] File bytes are empty. Cannot upload.")
            return None

        return await self._upload_bytes_to_storage(
            file_bytes=file_bytes,
            bucket_name=bucket_name,
            storage_path=storage_path,
            content_type=final_content_type,
            upsert=upsert
        )

    async def upload_discord_attachment(
        self,
        attachment: discord.Attachment,
        author: discord.User | discord.Member,
        message: discord.Message,
        admin_status: str = 'Listed'
        # Optional: Pass reaction if needed for metadata, though probably not
        # reaction: discord.Reaction | None = None
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Handles uploading a discord attachment to Supabase storage,
        generating thumbnails for videos, and creating a media record.

        Args:
            attachment: The discord.Attachment object to upload.
            author: The discord.User or discord.Member who authored the message.
            message: The original discord.Message containing the attachment.
            admin_status: The admin_status for the media record

        Returns:
            A tuple containing (media_record, profile_record) upon successful
            upload and database insertion, otherwise (None, None).
            Returns potentially stale profile_record even on media insert failure if profile was found/created.
        """
        self.logger.info(f"[OpenMuseInteractor] Initiating upload for attachment '{attachment.filename}' from message {message.id} by author {author.id}.")

        if not self.supabase:
            self.logger.error("[OpenMuseInteractor] Supabase client not initialized. Cannot upload attachment.")
            return None, None

        # --- 1. Find or Create Author Profile --- Find MUST happen first
        profile_data, profile_id_uuid = await self.find_or_create_profile(author)

        if not profile_data or not profile_id_uuid:
            self.logger.error(f"[OpenMuseInteractor] Failed to find or create profile for author {author.id}. Aborting upload for message {message.id}.")
            # Profile find/create logs errors internally
            return None, None # Cannot proceed without a profile ID

        # Store initial discord_connected status for later check
        initial_discord_connected = profile_data.get('discord_connected')
        supabase_username = profile_data.get('username') # Needed for potential welcome DM URL

        # --- 2. Check File Size --- Done after profile check
        if attachment.size > MAX_FILE_SIZE_BYTES:
            self.logger.warning(f"[OpenMuseInteractor] Attachment '{attachment.filename}' ({attachment.size} bytes) from message {message.id} exceeds max size ({MAX_FILE_SIZE_BYTES} bytes). Aborting upload.")
            # Caller (Reactor) should handle informing the user
            return None, _truncate_avatar_in_dict_for_logging(profile_data) # Return profile data even if upload fails

        # --- 3. Download File Content --- Download only if size is okay
        try:
            self.logger.info(f"[OpenMuseInteractor] --> Reading attachment '{attachment.filename}' bytes from message {message.id}.")
            file_bytes = await attachment.read()
            self.logger.info(f"[OpenMuseInteractor] <-- Read {len(file_bytes)} bytes for attachment '{attachment.filename}'.")
        except discord.HTTPException as e:
            self.logger.error(f"[OpenMuseInteractor] Discord HTTP error reading attachment {attachment.filename} from message {message.id}: {e}")
            return None, _truncate_avatar_in_dict_for_logging(profile_data)
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Error reading attachment {attachment.filename} from message {message.id}: {e}", exc_info=True)
            return None, _truncate_avatar_in_dict_for_logging(profile_data)

        # --- 4. Process Video Thumbnail & Aspect Ratio (if applicable) ---
        content_type = attachment.content_type or 'application/octet-stream'
        placeholder_image_url = None
        calculated_aspect_ratio = None
        _thumbnail_upload_success = False # Required only if it's a video

        if content_type.startswith('video/'):
            self.logger.info(f"[OpenMuseInteractor] Attachment '{attachment.filename}' is video ({content_type}). Processing thumbnail/ratio.")
            temp_video_file = None
            cap = None
            frame = None
            try:
                # Write video bytes to a temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(attachment.filename)[1]) as temp_video_file:
                    temp_video_file.write(file_bytes)
                    temp_video_path = temp_video_file.name
                self.logger.info(f"[OpenMuseInteractor] Video bytes written to temporary file: {temp_video_path}")

                cap = cv2.VideoCapture(temp_video_path)
                if not cap.isOpened():
                    self.logger.error(f"[OpenMuseInteractor] OpenCV could not open temporary video file: {temp_video_path}")
                else:
                    ret, frame = cap.read()
                    if ret:
                        self.logger.info(f"[OpenMuseInteractor] Successfully read first frame from video using OpenCV.")
                        # Calculate Aspect Ratio
                        try:
                            h, w = frame.shape[:2]
                            if h > 0:
                                calculated_aspect_ratio = round(w / h, 2)
                                self.logger.info(f"[OpenMuseInteractor] OpenCV Calculated aspect ratio: {calculated_aspect_ratio} (w={w}, h={h})")
                            else:
                                self.logger.warning("[OpenMuseInteractor] Frame height is 0, cannot calculate aspect ratio.")
                        except Exception as ar_ex:
                            self.logger.error(f"[OpenMuseInteractor] Error calculating aspect ratio from frame shape: {ar_ex}")

                        # Encode frame as JPEG
                        is_success, buffer = cv2.imencode(".jpg", frame)
                        if is_success:
                            thumbnail_bytes = buffer.tobytes()
                            self.logger.info(f"[OpenMuseInteractor] Encoded frame to {len(thumbnail_bytes)} bytes (JPEG).")

                            thumbnail_filename = f"{os.path.splitext(attachment.filename)[0]}_thumb.jpg"
                            thumbnail_storage_path = f"user_media/{profile_id_uuid}/{message.id}_{thumbnail_filename}"
                            
                            placeholder_image_url = await self._upload_bytes_to_storage(
                                file_bytes=thumbnail_bytes,
                                bucket_name=THUMBNAIL_BUCKET_NAME,
                                storage_path=thumbnail_storage_path,
                                content_type="image/jpeg",
                                upsert=True # Typically true for thumbnails derived from same source
                            )

                            if placeholder_image_url:
                                self.logger.info(f"[OpenMuseInteractor] Thumbnail uploaded successfully. URL: {placeholder_image_url}")
                                _thumbnail_upload_success = True # Mark as success if URL is obtained
                            else:
                                self.logger.error(f"[OpenMuseInteractor] Thumbnail upload failed for '{thumbnail_storage_path}' (no URL returned or error in _upload_bytes_to_storage).")
                                # thumbnail_upload_success remains False
                        else:
                            self.logger.error("[OpenMuseInteractor] Failed to encode video frame to JPEG using OpenCV.")
                    else:
                        self.logger.error("[OpenMuseInteractor] Failed to read first frame from video using OpenCV.")
            except Exception as thumb_ex:
                 self.logger.error(f"[OpenMuseInteractor] Error during thumbnail/ratio processing (OpenCV): {thumb_ex}", exc_info=True)
            finally:
                if cap and cap.isOpened():
                    cap.release()
                if temp_video_file and os.path.exists(temp_video_path):
                    try:
                        os.remove(temp_video_path)
                    except OSError as e:
                        self.logger.error(f"[OpenMuseInteractor] Error removing temporary file {temp_video_path}: {e}")
        else:
            self.logger.info(f"[OpenMuseInteractor] Attachment '{attachment.filename}' is not video. Skipping thumbnail generation.")

        # --- 5. Upload Original File --- Always attempt this
        storage_path = f"user_media/{profile_id_uuid}/{message.id}_{attachment.filename}"
        self.logger.info(f"[OpenMuseInteractor] --> Attempting to upload original file '{attachment.filename}' to bucket '{VIDEO_BUCKET_NAME}' at path '{storage_path}'.")
        
        public_url = await self._upload_bytes_to_storage(
            file_bytes=file_bytes, # These are the bytes of the original attachment read earlier
            bucket_name=VIDEO_BUCKET_NAME, # Or more generic like 'media_files_bucket'
            storage_path=storage_path,
            content_type=content_type, # Original content type of the attachment
            upsert=True # Standard upsert policy
        )

        if not public_url:
            self.logger.error(f"[OpenMuseInteractor] Original file upload failed for '{storage_path}'. Aborting media record creation.")
            return None, _truncate_avatar_in_dict_for_logging(profile_data)
        
        self.logger.info(f"[OpenMuseInteractor] Original file uploaded successfully. URL: {public_url}")
        main_upload_success = True # If public_url is not None, it was successful.

        # --- 6. Get Original File Public URL (if upload succeeded) ---
        # This step is now integrated into _upload_bytes_to_storage and its return value (public_url)
        # Validation of the URL also happens within _upload_bytes_to_storage.
        # So, no separate "Get Original File Public URL" section needed here.
        
        # main_upload_success is true if public_url is not None
        if not main_upload_success: # Should be redundant if logic above is correct
             self.logger.error("[OpenMuseInteractor] Reached code after main upload failure - should not happen.")
             return None, _truncate_avatar_in_dict_for_logging(profile_data)

        # Trim and validate thumbnail URL if present (already handled by _upload_bytes_to_storage for placeholder_image_url)
        # The placeholder_image_url is already validated or None if it failed.

        # --- 7. Insert Media Record --- Requires successful profile step
        classification = 'art' if message.channel and hasattr(message.channel, 'name') and message.channel.name.lower().startswith('art') else 'gen'
        media_type = 'video' if content_type.startswith('video/') else content_type # Simplified type
        media_title = None if media_type == 'video' else attachment.filename

        media_data = {
            'user_id': profile_id_uuid,
            'title': media_title,
            'url': public_url,
            'placeholder_image': placeholder_image_url,
            'type': media_type,
            'classification': classification,
            'admin_status': admin_status,
            'user_status': 'Listed', # Changed from 'View'
            'description': message.content,
            'metadata': {
                "discord_message_id": str(message.id),
                "discord_channel_id": str(message.channel.id),
                "discord_guild_id": str(message.guild.id) if message.guild else None,
                "discord_attachment_url": attachment.url,
                # "reacted_by_discord_user_id": str(reaction.user.id) if reaction else None, # Removed - belongs in Reactor
                # "trigger_emoji": str(reaction.emoji) if reaction else None, # Removed - belongs in Reactor
                "aspectRatio": calculated_aspect_ratio,
                "original_filename": attachment.filename,
                "original_content_type": content_type
            }
        }

        try:
            self.logger.info(f"[OpenMuseInteractor] --> Attempting to insert record into Supabase table '{MEDIA_TABLE}'.")
            insert_response = await asyncio.to_thread(
                 self.supabase.table(MEDIA_TABLE).insert(media_data).execute
            )
            self.logger.debug(f"[OpenMuseInteractor] Media insert response: {_truncate_avatar_in_dict_for_logging(insert_response)}")

            if insert_response.data:
                inserted_media_record = insert_response.data[0]
                self.logger.info(f"[OpenMuseInteractor] <-- Successfully inserted media record into table '{MEDIA_TABLE}'. Media ID: {inserted_media_record.get('id')}")

                # --- 8. Handle Welcome DM logic / discord_connected update ---
                if initial_discord_connected == False:
                    self.logger.info(f"[OpenMuseInteractor] Profile {profile_id_uuid} discord_connected is False. Attempting to update status and send Welcome DM.")
                    try:
                        # Use the username fetched/set earlier
                        username_for_url = supabase_username
                        if not username_for_url:
                            self.logger.warning(f"[OpenMuseInteractor] Supabase username not available for profile {profile_id_uuid} in welcome DM, falling back.")
                            username_for_url = author.name

                        formatted_username = quote(username_for_url, safe='')
                        profile_url = f"https://openmuse.ai/profile/{formatted_username}"
                        dm_message_content = (
                            f"Your first upload to OpenMuse via Discord has been successful!\n\n"
                            f"You can see your profile here: {profile_url}"
                        )

                        # Send DM - Belongs in Reactor, but done here for now for ease of transition
                        # TODO: Refactor DM sending back to Reactor based on a flag/data returned from here
                        self.logger.info(f"[OpenMuseInteractor] --> Sending Welcome DM to user {author.id}.")
                        await author.send(dm_message_content)
                        self.logger.info(f"[OpenMuseInteractor] <-- Successfully sent Welcome DM to user {author.id}.")

                        # Update discord_connected to True
                        self.logger.info(f"[OpenMuseInteractor] --> Attempting to update discord_connected to True for profile {profile_id_uuid}.")
                        _update_response = await asyncio.to_thread(
                            self.supabase.table(PROFILES_TABLE)
                            .update({'discord_connected': True})
                            .eq('id', profile_id_uuid)
                            .execute
                        )
                        self.logger.info(f"[OpenMuseInteractor] <-- discord_connected status updated for profile {profile_id_uuid}.")
                        # Update profile_data dict to reflect change for return value
                        profile_data['discord_connected'] = True

                    except discord.Forbidden:
                         self.logger.warning(f"[OpenMuseInteractor] Failed to send Welcome DM to user {author.id}. DMs disabled?")
                    except Exception as dm_update_ex:
                         self.logger.error(f"[OpenMuseInteractor] Error sending Welcome DM or updating discord_connected for profile {profile_id_uuid}: {dm_update_ex}", exc_info=True)
                else:
                    self.logger.info(f"[OpenMuseInteractor] Skipping Welcome DM for profile {profile_id_uuid} (Initial Connected Status: {initial_discord_connected}).")

                # Return successful media record and the (potentially updated) profile data
                return inserted_media_record, _truncate_avatar_in_dict_for_logging(profile_data)
            else:
                self.logger.error(f"[OpenMuseInteractor] Supabase media insert for message {message.id} executed but returned no data. Insert failed? RLS?",
                                 extra={"response": _truncate_avatar_in_dict_for_logging(insert_response)}), _truncate_avatar_in_dict_for_logging(profile_data) # Log full response if possible
                return None, _truncate_avatar_in_dict_for_logging(profile_data) # Return profile, but indicate media insert failure

        except APIError as insert_err:
             self.logger.error(f"[OpenMuseInteractor] Supabase API error inserting media record for message {message.id}: {insert_err}")
             return None, _truncate_avatar_in_dict_for_logging(profile_data)
        except Exception as insert_ex:
            self.logger.error(f"[OpenMuseInteractor] Unexpected error inserting media record for message {message.id}: {insert_ex}", exc_info=True)
            return None, _truncate_avatar_in_dict_for_logging(profile_data)

    async def create_media_record(
        self,
        user_id_uuid: str,
        media_url: str,
        filename: str, 
        content_type: str,
        file_size: int, 
        description: Optional[str], 
        message: discord.Message, 
        author_discord_user: discord.User, 
        profile_data: Dict[str, Any], 
        admin_status: str = "Listed",
        user_status: str = "Listed",
        title: Optional[str] = None,
        placeholder_image_url: Optional[str] = None,
        calculated_aspect_ratio: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Creates a media record in the Supabase 'media' table.
        This is a more direct way to insert a media record when the file is already uploaded
        or when detailed processing like thumbnailing is handled externally or skipped.
        Args:
            user_id_uuid: The UUID of the user's profile.
            media_url: The public URL of the uploaded media.
            filename: The filename of the media (e.g., original_filename or new .mp4 filename).
            content_type: The MIME type of the media.
            file_size: The size of the media file in bytes.
            description: Optional description for the media (e.g., from the Discord message content).
            message: The discord.Message object for additional metadata (channel, guild ID).
            author_discord_user: The discord.User who created the media, for welcome DM.
            profile_data: The profile data dictionary for the author, used for welcome DM logic.
            admin_status: Admin status for the media record.
            user_status: User status for the media record.
            title: Optional title for the media.
            placeholder_image_url: Optional URL for a placeholder/thumbnail image.
            calculated_aspect_ratio: Optional aspect ratio of the media.
        Returns:
            A dictionary representing the inserted media record if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error("[OpenMuseInteractor_CreateMedia] Supabase client not initialized. Cannot create media record.")
            return None

        self.logger.info(f"[OpenMuseInteractor_CreateMedia] Creating media record for URL: {media_url}, Filename: {filename}")

        classification = 'art' if message.channel and hasattr(message.channel, 'name') and message.channel.name.lower().startswith('art') else 'gen'
        media_type = 'video' if content_type.startswith('video/') else content_type 
        
        if title is None and media_type != 'video': 
            title = filename

        media_data_payload = {
            'user_id': user_id_uuid,
            'title': title,
            'url': media_url,
            'placeholder_image': placeholder_image_url,
            'type': media_type,
            'classification': classification,
            'admin_status': admin_status,
            'user_status': user_status,
            'description': description,
            'metadata': {
                "discord_message_id": str(message.id),
                "discord_channel_id": str(message.channel.id),
                "discord_guild_id": str(message.guild.id) if message.guild else None,
                "aspectRatio": calculated_aspect_ratio,
                "original_filename": filename, 
                "original_content_type": content_type,
                "file_size": file_size 
            }
        }
        # Remove None keys from metadata before insert if Supabase prefers that
        media_data_payload['metadata'] = {k: v for k, v in media_data_payload['metadata'].items() if v is not None}


        try:
            self.logger.info(f"[OpenMuseInteractor_CreateMedia] --> Attempting to insert record into Supabase table '{MEDIA_TABLE}' with payload: {_truncate_avatar_in_dict_for_logging(media_data_payload)}")
            insert_response = await asyncio.to_thread(
                 self.supabase.table(MEDIA_TABLE).insert(media_data_payload).execute
            )
            self.logger.debug(f"[OpenMuseInteractor_CreateMedia] Media insert response: {_truncate_avatar_in_dict_for_logging(insert_response)}")

            if insert_response.data:
                inserted_media_record = insert_response.data[0]
                self.logger.info(f"[OpenMuseInteractor_CreateMedia] <-- Successfully inserted media record. Media ID: {inserted_media_record.get('id')}")

                # --- Handle Welcome DM logic (similar to upload_discord_attachment) ---
                initial_discord_connected = profile_data.get('discord_connected')
                supabase_username = profile_data.get('username') # From the passed profile_data

                if initial_discord_connected == False:
                    self.logger.info(f"[OpenMuseInteractor_CreateMedia] Profile {user_id_uuid} discord_connected is False. Attempting to update status and send Welcome DM.")
                    try:
                        username_for_url = supabase_username or author_discord_user.name # Fallback to discord username
                        formatted_username = quote(username_for_url, safe='')
                        profile_url = f"https://openmuse.ai/profile/{formatted_username}"
                        dm_message_content = (
                            f"Your first upload to OpenMuse via Discord has been successful!\\n\\n"
                            f"You can see your profile here: {profile_url}"
                        )
                        
                        self.logger.info(f"[OpenMuseInteractor_CreateMedia] --> Sending Welcome DM to user {author_discord_user.id}.")
                        await author_discord_user.send(dm_message_content)
                        self.logger.info(f"[OpenMuseInteractor_CreateMedia] <-- Successfully sent Welcome DM to user {author_discord_user.id}.")
                        
                        self.logger.info(f"[OpenMuseInteractor_CreateMedia] --> Attempting to update discord_connected to True for profile {user_id_uuid}.")
                        await asyncio.to_thread(
                            self.supabase.table(PROFILES_TABLE)
                            .update({'discord_connected': True})
                            .eq('id', user_id_uuid)
                            .execute
                        )
                        self.logger.info(f"[OpenMuseInteractor_CreateMedia] <-- discord_connected status updated for profile {user_id_uuid}.")
                        # Note: The profile_data dict passed in is not mutated here. If the caller needs the updated
                        # profile_data (with discord_connected=True), it should be aware or refetch.
                    except discord.Forbidden:
                         self.logger.warning(f"[OpenMuseInteractor_CreateMedia] Failed to send Welcome DM to user {author_discord_user.id}. DMs disabled?")
                    except Exception as dm_update_ex:
                         self.logger.error(f"[OpenMuseInteractor_CreateMedia] Error sending Welcome DM or updating discord_connected for profile {user_id_uuid}: {dm_update_ex}", exc_info=True)
                else:
                    self.logger.info(f"[OpenMuseInteractor_CreateMedia] Skipping Welcome DM for profile {user_id_uuid} (Initial Connected Status: {initial_discord_connected}).")
                
                return inserted_media_record
            else:
                self.logger.error(f"[OpenMuseInteractor_CreateMedia] Supabase media insert executed but returned no data. Insert failed? RLS?",
                                 extra={"response": _truncate_avatar_in_dict_for_logging(insert_response)})
                return None
        except APIError as insert_err:
             self.logger.error(f"[OpenMuseInteractor_CreateMedia] Supabase API error inserting media record: {insert_err}")
             return None
        except Exception as insert_ex:
            self.logger.error(f"[OpenMuseInteractor_CreateMedia] Unexpected error inserting media record: {insert_ex}", exc_info=True)
            return None

    # --- Add other Supabase interaction methods here as needed ---
    # Example: async def get_media_by_id(self, media_id: str): ...
    # Example: async def update_media_status(self, media_id: str, status: str): ... 