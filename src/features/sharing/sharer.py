# Placeholder for Sharer class 

import discord
import logging
import os
import aiohttp
import asyncio # Added
from pathlib import Path
from typing import List, Dict, Optional, Tuple # Added Tuple
import mimetypes # For inferring content type from URL

from src.common.db_handler import DatabaseHandler
# Remove old client import
# from src.common.claude_client import ClaudeClient 
# Import the dispatcher
from src.common.llm import get_llm_response
from .subfeatures.notify_user import send_sharing_request_dm
# Removed content_analyzer import, assuming title generation covers description needs
# from .subfeatures.content_analyzer import generate_description_with_claude
# Import specific functions from social_poster
from .subfeatures.social_poster import (
    post_tweet,
    post_to_instagram_via_zapier,
    post_to_tiktok_via_zapier,
    post_to_youtube_via_zapier,
    _build_zapier_payload, # Also import the payload builder
    generate_media_title, # Now correctly references the moved function
)

logger = logging.getLogger('DiscordBot')

class Sharer:
    # Remove claude_client from init
    def __init__(self, bot: discord.Client, db_handler: DatabaseHandler, logger_instance: logging.Logger):
        self.bot = bot
        self.db_handler = db_handler
        self.logger = logger_instance
        # Remove client storage
        # self.claude_client = claude_client 
        self.temp_dir = Path("./temp_media_sharing")
        self.temp_dir.mkdir(exist_ok=True)

    async def _download_attachment(self, attachment: discord.Attachment) -> Optional[Dict]:
        """Downloads a single discord.Attachment to the temporary directory."""
        # Keep original filename but prefix with ID for uniqueness
        # Sanitize filename slightly to avoid issues, though discord.Attachment.filename should be reasonable
        safe_filename = "".join(c if c.isalnum() or c in ('.', '_', '-') else '_' for c in attachment.filename)
        save_path = self.temp_dir / f"{attachment.id}_{safe_filename}"
        try:
            await attachment.save(save_path) # discord.Attachment has a save method
            self.logger.info(f"Successfully downloaded attachment using discord.Attachment.save: {save_path}")
            return {
                'url': attachment.url,
                'filename': attachment.filename, # Original filename for display/metadata
                'content_type': attachment.content_type,
                'size': attachment.size,
                'id': attachment.id,
                'local_path': str(save_path) # Store local path
            }
        except Exception as e:
            self.logger.error(f"Error downloading attachment {attachment.url} using discord.Attachment.save: {e}", exc_info=True)
            # Fallback to aiohttp if .save() fails (e.g. if it's not available or has issues)
            self.logger.info(f"Falling back to aiohttp download for {attachment.url}")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            with open(save_path, 'wb') as f:
                                f.write(await resp.read())
                            self.logger.info(f"Successfully downloaded attachment via aiohttp fallback: {save_path}")
                            return {
                                'url': attachment.url,
                                'filename': attachment.filename,
                                'content_type': attachment.content_type,
                                'size': attachment.size,
                                'id': attachment.id,
                                'local_path': str(save_path)
                            }
                        else:
                            self.logger.error(f"AIOHTTP fallback failed to download {attachment.url}. Status: {resp.status}")
                            return None
            except Exception as e2:
                self.logger.error(f"Error during aiohttp fallback download for {attachment.url}: {e2}", exc_info=True)
                return None

    async def _download_media_from_url(self, url: str, message_id: str, item_index: int) -> Optional[Dict]:
        """Downloads media from a direct URL to the temporary directory."""
        try:
            filename_from_url = Path(url.split('?')[0]).name # Basic filename extraction from URL
            # Sanitize filename
            safe_filename_from_url = "".join(c if c.isalnum() or c in ('.', '_', '-') else '_' for c in filename_from_url)
            if not safe_filename_from_url: # if URL has no filename part (e.g. ends with /)
                safe_filename_from_url = f"media_{item_index}" 
            
            save_path = self.temp_dir / f"tweet_media_{message_id}_{item_index}_{safe_filename_from_url}"
            save_path = save_path.with_suffix(Path(filename_from_url).suffix) # Ensure correct suffix

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        with open(save_path, 'wb') as f:
                            f.write(await resp.read())
                        
                        content_type = resp.headers.get('Content-Type')
                        if not content_type:
                            content_type, _ = mimetypes.guess_type(url)
                        if not content_type: # Fallback if still not found
                            content_type = 'application/octet-stream'

                        self.logger.info(f"Successfully downloaded media from URL: {url} to {save_path}")
                        return {
                            'url': url,
                            'filename': filename_from_url, # Filename derived from URL for metadata
                            'content_type': content_type,
                            'local_path': str(save_path)
                        }
                    else:
                        self.logger.error(f"Failed to download media from URL {url}. Status: {resp.status}")
                        return None
        except Exception as e:
            self.logger.error(f"Error downloading media from URL {url}: {e}", exc_info=True)
            return None

    async def send_tweet(
        self,
        content: str, 
        image_urls: Optional[List[str]], 
        message_id: str, # Original Discord message ID for context/logging
        user_id: int,      # Original Discord user ID for fetching details
        original_message_content: Optional[str] = None # Original Discord message text
    ) -> Tuple[bool, Optional[str]]:
        """Prepares data and posts a tweet using the social_poster.post_tweet function."""
        self.logger.info(f"Sharer.send_tweet called for message_id {message_id} by user_id {user_id} with content: '{content[:50]}...'")

        user_details = self.db_handler.get_member(user_id)
        if not user_details:
            self.logger.error(f"Sharer.send_tweet: User {user_id} not found in DB. Cannot post tweet.")
            return False, None

        downloaded_media_for_tweet = []
        temp_files_to_clean = []

        if image_urls:
            self.logger.info(f"Sharer.send_tweet: Processing {len(image_urls)} image_urls for message_id {message_id}.")
            for i, url in enumerate(image_urls):
                downloaded_item = await self._download_media_from_url(url, message_id, i)
                if downloaded_item and downloaded_item.get('local_path'):
                    downloaded_media_for_tweet.append(downloaded_item)
                    temp_files_to_clean.append(downloaded_item['local_path'])
                else:
                    self.logger.warning(f"Sharer.send_tweet: Failed to download media from URL: {url} for message_id {message_id}")
        
        if not downloaded_media_for_tweet and image_urls:
            self.logger.error(f"Sharer.send_tweet: All media downloads failed for message_id {message_id}. Cannot post tweet.")
            self._cleanup_files(temp_files_to_clean) # Clean up any partial downloads
            return False, None
        
        if not downloaded_media_for_tweet: # No image_urls provided, or all failed
             # If your post_tweet requires media, you must return False here.
             # If post_tweet can handle text-only, you might proceed.
             # Based on social_poster.py, post_tweet currently requires attachments.
            self.logger.warning(f"Sharer.send_tweet: No media provided or prepared for message_id {message_id}. Cannot post tweet as post_tweet requires media.")
            self._cleanup_files(temp_files_to_clean)
            return False, None

        self.logger.info(f"Sharer.send_tweet: Calling social_poster.post_tweet for message_id {message_id} with {len(downloaded_media_for_tweet)} media items.")
        tweet_url = await post_tweet(
            generated_description=content,  # This is the reactor's comment
            user_details=user_details,
            attachments=downloaded_media_for_tweet, # List of dicts with 'local_path'
            original_content=original_message_content # Original Discord message text
        )

        self._cleanup_files(temp_files_to_clean)

        if tweet_url:
            self.logger.info(f"Sharer.send_tweet: Successfully posted tweet for message_id {message_id}. URL: {tweet_url}")
            return True, tweet_url
        else:
            self.logger.error(f"Sharer.send_tweet: Failed to post tweet for message_id {message_id} (post_tweet returned None).")
            return False, None

    # Renamed original function for clarity
    async def initiate_sharing_process_from_reaction(self, reaction: discord.Reaction, user: discord.User):
        """Starts the sharing process via reaction by sending a DM to the message author, or finalizing if consent already given."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_reaction called. Triggering User ID: {user.id}, Emoji: {reaction.emoji}, Message ID: {reaction.message.id}, Message Author ID: {reaction.message.author.id}")
        message = reaction.message
        author = message.author

        # Fetch user details to check for existing consent
        user_details = self.db_handler.get_member(author.id)

        # Check if consent is already 1 (True).
        # The modal in notify_user.py uses `user_details.get('sharing_consent') == 1` for pre-fill.
        if user_details and user_details.get('sharing_consent') == 1:
            self.logger.info(f"User {author.id} (message author) has already granted sharing consent for message {message.id}. Proceeding directly to finalize_sharing.")
            if message.channel:
                asyncio.create_task(self.finalize_sharing(author.id, message.id, message.channel.id))
            else:
                # This case should be rare in normal Discord operation with reactions
                self.logger.warning(f"Cannot finalize sharing automatically for message {message.id} as message.channel is not available.")
            return # Skip sending DM

        # If no existing consent or user_details not found (new user flow handled by send_sharing_request_dm), proceed to send DM
        self.logger.info(f"Initiating sharing process (sending DM) for message {message.id} triggered by {user.id} reacting with {reaction.emoji}.")
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self)

    # Added new function to initiate from summary/message object
    async def initiate_sharing_process_from_summary(self, message: discord.Message):
        """Starts the sharing process directly from a message object, or finalizing if consent already given."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_summary called. Message ID: {message.id}, Author ID: {message.author.id}")
        author = message.author
        # Check if author is a bot before proceeding
        if author.bot:
            self.logger.warning(f"Attempted to initiate sharing for a bot message ({message.id}). Skipping.")
            return
            
        # Fetch user details to check for existing consent
        user_details = self.db_handler.get_member(author.id)

        # Check if consent is already 1 (True)
        if user_details and user_details.get('sharing_consent') == 1:
            self.logger.info(f"User {author.id} has already granted sharing consent for message {message.id} (triggered by summary). Proceeding directly to finalize_sharing.")
            if message.channel:
                asyncio.create_task(self.finalize_sharing(author.id, message.id, message.channel.id))
            else:
                # This case might be more relevant if message object comes from an unusual source
                self.logger.warning(f"Cannot finalize sharing automatically for message {message.id} (triggered by summary) as message.channel is not available.")
            return # Skip sending DM

        self.logger.info(f"Initiating sharing process (sending DM) for message {message.id} requested via summary.")
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self)

    # Updated signature to include channel_id
    async def finalize_sharing(self, user_id: int, message_id: int, channel_id: int):
        """Fetches data, generates content, and posts to social media if consent is confirmed."""
        self.logger.info(f"Finalizing sharing process for user {user_id}, message {message_id} in channel {channel_id}.")

        # 1. Fetch User Details (Confirm Consent Again)
        user_details = self.db_handler.get_member(user_id)
        if not user_details:
            self.logger.error(f"Cannot finalize sharing: User {user_id} not found in DB.")
            return

        if not user_details.get('sharing_consent', False):
            self.logger.warning(f"Cannot finalize sharing: User {user_id} consent is false in DB check for message {message_id}.")
            return

        # 2. Fetch Original Message using channel_id and message_id
        message_object = await self._fetch_message(channel_id, message_id)
        if not message_object:
             self.logger.error(f"Cannot finalize sharing: Failed to fetch message {message_id} from channel {channel_id}.")
             return

        # 3. Download Attachments
        downloaded_attachments = []
        if message_object.attachments:
            for attachment in message_object.attachments:
                downloaded = await self._download_attachment(attachment)
                if downloaded:
                    # Add jump URL to attachment dict for Zapier payload builder
                    downloaded['post_jump_url'] = message_object.jump_url
                    downloaded_attachments.append(downloaded)

        if not downloaded_attachments:
            self.logger.error(f"Cannot finalize sharing: No attachments found or failed to download for message {message_id}.")
            return # Can't post without media
            
        # Assume first attachment is primary for now
        primary_attachment = downloaded_attachments[0]
        media_local_path = primary_attachment.get('local_path')
        is_video = primary_attachment.get('content_type', '').startswith('video') or Path(media_local_path).suffix.lower() in ['.mp4', '.mov', '.webm', '.avi', '.mkv']
        is_gif = Path(media_local_path).suffix.lower() == '.gif'
        is_image = primary_attachment.get('content_type', '').startswith('image')
        
        # 4. Generate Title and Description (Title for other platforms, Description will be custom for Twitter)
        
        # Determine Title (using generate_media_title from social_poster)
        generated_title = "Featured Creation" # Default
        if is_video or (is_image and not is_gif): # Generate for video or non-GIF image
            self.logger.info(f"Generating media title for message {message_id} ({'video' if is_video else 'image'}).")
            generated_title = await generate_media_title(
                attachment=primary_attachment, 
                original_comment=message_object.content,
                post_id=message_id
            )
        elif is_gif:
             generated_title = "Cool Gif" # Keep simple default for GIFs
             self.logger.info(f"Using default title '{generated_title}' for GIF message {message_id}.")
        else: # Fallback for unknown types
             self.logger.warning(f"Unknown attachment type for title generation, using default for message {message_id}.")
             generated_title = "Featured Creation" 

        # LLM-Generated Description (for platforms other than Twitter, or if new format is not used there)
        llm_generated_desc = "Check out this amazing creation!" # Default fallback
        try:
            desc_system_prompt = (
                f"Based on the title \"{generated_title}\" and the artist's original comment below, write a short, engaging social media description (1-2 sentences). "
                f"Mention the type of media (e.g., 'artwork', 'video', 'creation'). Avoid simply repeating the title. "
                f"Focus on generating excitement or interest."
            )
            desc_user_content = f"Artist's Comment: \"{message_object.content if message_object.content else 'None'}\""
            desc_messages = [{"role": "user", "content": desc_user_content}]
            
            self.logger.info(f"Generating LLM description (for non-Twitter use) via dispatcher for message {message_id}...")
            claude_desc_response = await get_llm_response(
                client_name="claude",
                model="claude-3-5-sonnet-latest", 
                system_prompt=desc_system_prompt,
                messages=desc_messages,
                max_tokens=150,
            )
            if claude_desc_response:
                llm_generated_desc = claude_desc_response.strip()
                self.logger.info(f"LLM dispatcher generated description for non-Twitter use for message {message_id}: {llm_generated_desc}")
            else:
                self.logger.warning(f"LLM description generation (non-Twitter) failed or returned empty for message {message_id}, using default.")
        except Exception as e:
            self.logger.error(f"Error during LLM description generation (non-Twitter) for message {message_id}: {e}", exc_info=True)

        # --- Construct Twitter-specific content --- 
        # Get Artist Credit Text (replicated from social_poster._build_tweet_caption logic)
        raw_twitter_handle = user_details.get('twitter_handle')
        user_global_name = user_details.get('global_name')
        user_discord_name = user_details.get('username') # Assuming 'username' is the Discord username
        artist_credit_text = None

        if raw_twitter_handle:
            handle_val = raw_twitter_handle.strip()
            extracted_username = None
            is_url_like_structure = '://' in handle_val or \
                                   'x.com/' in handle_val.lower() or \
                                   'twitter.com/' in handle_val.lower()
            if handle_val.startswith('@') and is_url_like_structure:
                handle_val = handle_val[1:]
            if '://' in handle_val:
                path_after_scheme = handle_val.split('://', 1)[-1]
                domain_and_path_lower = path_after_scheme.lower()
                if domain_and_path_lower.startswith('twitter.com/'):
                    extracted_username = path_after_scheme[len('twitter.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('www.twitter.com/'):
                    extracted_username = path_after_scheme[len('www.twitter.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('x.com/'):
                    extracted_username = path_after_scheme[len('x.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('www.x.com/'):
                    extracted_username = path_after_scheme[len('www.x.com/'):].split('/')[0]
            elif 'x.com/' in handle_val.lower():
                match_pattern = 'x.com/'
                start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
                extracted_username = handle_val[start_idx:].split('/')[0]
            elif 'twitter.com/' in handle_val.lower():
                match_pattern = 'twitter.com/'
                start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
                extracted_username = handle_val[start_idx:].split('/')[0]
            else:
                extracted_username = handle_val
            if extracted_username:
                cleaned_username = extracted_username.split('?')[0].split('#')[0]
                if cleaned_username.startswith('@'):
                    cleaned_username = cleaned_username[1:]
                if cleaned_username:
                    artist_credit_text = f"@{cleaned_username}"

        if not artist_credit_text:
            if user_global_name:
                artist_credit_text = user_global_name
            elif user_discord_name:
                artist_credit_text = user_discord_name
            else:
                artist_credit_text = "the artist" # Fallback

        artist_original_comment = message_object.content.strip() if message_object.content else None
        
        twitter_specific_caption_parts = []
        twitter_specific_caption_parts.append(f"Top art post of the day by {artist_credit_text}")

        if artist_original_comment:
            # Enclose the comment in single quotes as requested
            escaped_comment = artist_original_comment.replace("'", "\\'").replace("\"", '\\"') # Basic escape for f-string
            twitter_specific_caption_parts.append(f"\nComment by artist: '{escaped_comment}'")

        final_twitter_content = "\n".join(twitter_specific_caption_parts)
        self.logger.info(f"Using specific Twitter format for message {message_id}. Content: '{final_twitter_content[:100]}...'")
        # --- End Twitter-specific content construction ---

        self.logger.info(f"Using Title (for non-Twitter): '{generated_title}', LLM Desc (for non-Twitter): '{llm_generated_desc}' for message {message_id}")

        # 5. Post to Social Media Platforms
        # Post to Twitter first
        self.logger.info(f"Attempting to post message {message_id} to Twitter.")
        tweet_url = await post_tweet(
            generated_description=final_twitter_content, # Use our new Twitter-specific formatted string
            user_details=user_details,
            attachments=downloaded_attachments, # Pass list
            original_content=message_object.content # Still pass original content for _build_tweet_caption if it uses it for other things
        )
        if tweet_url:
            self.logger.info(f"Successfully posted message {message_id} to Twitter: {tweet_url}")
        else:
            self.logger.error(f"Failed to post message {message_id} to Twitter.")
            
        # Add short delay between platform posts
        await asyncio.sleep(2)

        # Post to Zapier webhooks (if not a GIF, as per example logic)
        if not is_gif:
            # Instagram
            ig_payload = _build_zapier_payload("instagram", user_details, primary_attachment, generated_title, llm_generated_desc, message_object.content)
            self.logger.info(f"Attempting to post message {message_id} to Instagram via Zapier.")
            await asyncio.to_thread(post_to_instagram_via_zapier, ig_payload) # Run sync requests in thread
            await asyncio.sleep(2)

            # TikTok
            tiktok_payload = _build_zapier_payload("tiktok", user_details, primary_attachment, generated_title, llm_generated_desc, message_object.content)
            self.logger.info(f"Attempting to post message {message_id} to TikTok via Zapier.")
            await asyncio.to_thread(post_to_tiktok_via_zapier, tiktok_payload)
            await asyncio.sleep(2)

            # YouTube
            youtube_payload = _build_zapier_payload("youtube", user_details, primary_attachment, generated_title, llm_generated_desc, message_object.content)
            self.logger.info(f"Attempting to post message {message_id} to YouTube via Zapier.")
            await asyncio.to_thread(post_to_youtube_via_zapier, youtube_payload)

        else:
            self.logger.info(f"Skipping Zapier posts for GIF message {message_id}.")

        # 6. Cleanup Downloaded Files
        self._cleanup_files([a['local_path'] for a in downloaded_attachments])

    def _cleanup_files(self, file_paths: List[str]):
        """Removes temporary files."""
        for file_path in file_paths:
            try:
                os.remove(file_path)
                self.logger.info(f"Removed temporary file: {file_path}")
            except OSError as e:
                self.logger.error(f"Error removing temporary file {file_path}: {e}")

    # Replaced placeholder with actual implementation
    async def _fetch_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        """Fetches a message using channel_id and message_id."""
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                # Fallback: try fetching channel if not in cache
                channel = await self.bot.fetch_channel(channel_id)
                
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                 message = await channel.fetch_message(message_id)
                 self.logger.info(f"Successfully fetched message {message_id} from channel {channel_id}")
                 return message
            else:
                self.logger.error(f"Channel {channel_id} is not a TextChannel or Thread.")
                return None
        except discord.NotFound:
            self.logger.error(f"Could not find channel {channel_id} or message {message_id}.")
            return None
        except discord.Forbidden:
            self.logger.error(f"Bot lacks permissions to fetch message {message_id} from channel {channel_id}.")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error fetching message {message_id} from channel {channel_id}: {e}", exc_info=True)
            return None 