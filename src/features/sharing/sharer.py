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
    generate_media_title,
)
from src.common import discord_utils # Ensure this is imported

logger = logging.getLogger('DiscordBot')

ANNOUNCEMENT_CHANNEL_ID = 1246615722164224141 # User provided ID

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
        self._processing_lock = asyncio.Lock()
        self._currently_processing = set()
        self._posted_to_summary = set()  # Track messages already posted to summary channels
        self._successfully_shared = set()  # Track messages already successfully shared to prevent duplicates

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
            # Ensure the suffix is preserved or correctly added
            original_suffix = Path(filename_from_url).suffix
            if original_suffix:
                save_path = save_path.with_suffix(original_suffix)
            # else: # if no suffix in URL, might try to guess or leave as is based on Content-Type later

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
        author_display_name: str, # NEW parameter for display name
        original_message_content: Optional[str] = None, # Original Discord message text
        original_message_jump_url: Optional[str] = None
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
            await self._announce_tweet_url(
                tweet_url=tweet_url, 
                author_display_name=author_display_name, 
                original_message_jump_url=original_message_jump_url, 
                context_message_id=message_id
            )
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

        # Check for explicit opt-out first (allow_content_sharing = False)
        if user_details and user_details.get('allow_content_sharing') is False:
            self.logger.info(f"User {author.id} (message author) has opted out of content sharing (allow_content_sharing=False). Skipping.")
            return

        # Check if consent is already True (allow_content_sharing = True)
        if user_details and user_details.get('allow_content_sharing') is True:
            self.logger.info(f"User {author.id} (message author) has already granted sharing consent for message {message.id}. Proceeding directly to finalize_sharing.")
            if message.channel:
                # For reaction-based sharing, summary_channel is not typically involved unless specifically designed so.
                # If it were, it would need to be sourced from somewhere else (e.g. reaction context or config)
                asyncio.create_task(self.finalize_sharing(author.id, message.id, message.channel.id, summary_channel=None))
            else:
                # This case should be rare in normal Discord operation with reactions
                self.logger.warning(f"Cannot finalize sharing automatically for message {message.id} as message.channel is not available.")
            return # Skip sending DM

        # If no existing consent or user_details not found (new user flow handled by send_sharing_request_dm), proceed to send DM
        self.logger.info(f"Initiating sharing process (sending DM) for message {message.id} triggered by {user.id} reacting with {reaction.emoji}.")
        # For reaction-based sharing, summary_channel is not passed to the DM
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self, summary_channel=None)

    # Added new function to initiate from summary/message object
    async def initiate_sharing_process_from_summary(self, message: discord.Message, summary_channel: Optional[discord.TextChannel] = None):
        """Starts the sharing process directly from a message object, or finalizing if consent already given."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_summary called. Message ID: {message.id}, Author ID: {message.author.id}, Summary Channel: {summary_channel.id if summary_channel else 'None'}")
        author = message.author
        # Check if author is a bot before proceeding
        if author.bot:
            self.logger.warning(f"Attempted to initiate sharing for a bot message ({message.id}). Skipping.")
            return
            
        # Fetch user details to check for existing consent
        user_details = self.db_handler.get_member(author.id)

        # Check for explicit opt-out first (allow_content_sharing = False)
        if user_details and user_details.get('allow_content_sharing') is False:
            self.logger.info(f"User {author.id} has opted out of content sharing (allow_content_sharing=False). Skipping summary share for message {message.id}.")
            return

        # Check if consent is already True (allow_content_sharing = True)
        if user_details and user_details.get('allow_content_sharing') is True:
            self.logger.info(f"User {author.id} has already granted sharing consent for message {message.id} (triggered by summary). Proceeding directly to finalize_sharing.")
            if message.channel:
                asyncio.create_task(self.finalize_sharing(author.id, message.id, message.channel.id, summary_channel=summary_channel))
            else:
                # This case might be more relevant if message object comes from an unusual source
                self.logger.warning(f"Cannot finalize sharing automatically for message {message.id} (triggered by summary) as message.channel is not available.")
            return # Skip sending DM

        self.logger.info(f"Initiating sharing process (sending DM) for message {message.id} requested via summary.")
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self, summary_channel=summary_channel)

    async def finalize_sharing(self, user_id: int, message_id: int, channel_id: int, summary_channel: Optional[discord.TextChannel] = None):
        """
        Finalizes the sharing process after receiving consent. 
        This function now acts as the central point for all sharing activities for a given message.
        It is responsible for fetching content, generating descriptions, and posting to all configured platforms.
        """
        # Acquire lock and hold it for the entire operation to prevent concurrent processing
        async with self._processing_lock:
            if message_id in self._currently_processing:
                self.logger.warning(f"Sharing for message {message_id} is already in progress. Aborting.")
                return
            if message_id in self._successfully_shared:
                self.logger.info(f"Message {message_id} has already been successfully shared. Skipping duplicate sharing attempt.")
                return
            self._currently_processing.add(message_id)

            try:
                # Step 2: Fetch the original message
                message = await self._fetch_message(channel_id, message_id)
                if not message:
                    self.logger.error(f"Failed to fetch message {message_id} in finalize_sharing. Aborting.")
                    return

                # Step 3: Download attachments
                downloaded_attachments = []
                for attachment in message.attachments:
                    downloaded_item = await self._download_attachment(attachment)
                    if downloaded_item:
                        downloaded_attachments.append(downloaded_item)

                if not downloaded_attachments:
                    self.logger.warning(f"No attachments could be downloaded for message {message_id}. Sharing might fail for platforms requiring media.")
                
                # Step 4: Get author details
                user_details = self.db_handler.get_member(user_id)
                if not user_details:
                    self.logger.error(f"Failed to get user details for user {user_id}. Aborting.")
                    self._cleanup_files([att['local_path'] for att in downloaded_attachments])
                    return

                # Step 5: Generate title and descriptions
                is_video = any('video' in (att.get('content_type') or '') for att in downloaded_attachments)
                media_type = 'video' if is_video else 'image'
                
                first_attachment = downloaded_attachments[0] if downloaded_attachments else None
                generated_title = None

                if first_attachment:
                    self.logger.info(f"Generating media title for message {message_id} ({first_attachment.get('content_type')}).")
                    generated_title = await generate_media_title(
                        attachment=first_attachment,
                        original_comment=message.content,
                        post_id=message.id
                    )
                
                self.logger.info(f"Generating LLM description (for non-Twitter use) via dispatcher for message {message_id}...")
                # Build a concise prompt for the LLM. The function `get_llm_response` expects the standard
                # arguments (client_name, model, system_prompt, messages,…). We were previously calling it
                # with a non-existent `prompt` kwarg which caused a TypeError and aborted the sharing flow.
                llm_description = await get_llm_response(
                    client_name="claude",
                    model="claude-sonnet-4-5-20250929",
                    system_prompt=(
                        "You are an expert social-media copywriter. Respond with exactly one engaging yet "
                        "concise sentence (no hashtags) that describes the attached media so it can be used "
                        "as a caption on various platforms."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Author: {message.author.display_name}\n"
                            f"Media type: {media_type}\n"
                            f"Original comment: {message.content}"
                        )
                    }],
                    max_tokens=64,
                    temperature=0.7,
                )

                twitter_content = ""
                if summary_channel and "top-art-sharing" in summary_channel.name.lower():
                    self.logger.info(f"Using specific Twitter format for message {message_id}. Content: '{summary_channel.topic[:50]}...'")
                    twitter_content = summary_channel.topic
                else:
                     self.logger.info(f"Using Title (for non-Twitter): '{generated_title}', LLM Desc (for non-Twitter): '{llm_description[:50]}...' for message {message_id}")
                     # Check if this is from top art sharing (summary_channel exists) vs regular reaction-based sharing
                     if summary_channel:
                         # This is top art sharing from daily summary
                         twitter_content = f"Top art sharing post of the day by {message.author.display_name}:"
                     else:
                         # This is regular reaction-based sharing
                         twitter_content = f"Check out this post by {message.author.display_name}! {message.jump_url}"

                if downloaded_attachments:
                    self.logger.info(f"Attempting to post message {message_id} to Twitter.")
                    tweet_url = await post_tweet(
                        generated_description=twitter_content,
                        user_details=user_details,
                        attachments=downloaded_attachments,
                        original_content=message.content
                    )
                    if tweet_url:
                        self.logger.info(f"Successfully posted message {message_id} to Twitter: {tweet_url}")
                        await self._announce_tweet_url(tweet_url, message.author.display_name, message.jump_url, str(message_id))
                        # Mark as successfully shared to prevent duplicate Twitter posts
                        self._successfully_shared.add(message_id)
                        self.logger.info(f"Marked message {message_id} as successfully shared to prevent duplicates.")
                    else:
                        self.logger.error(f"Failed to post message {message_id} to Twitter.")

                if downloaded_attachments:
                    self.logger.info(f"Attempting to post message {message_id} to Instagram via Zapier.")
                    ig_caption = f"{generated_title}\n\n{llm_description}\n\nCredits to user: {message.author.display_name}"
                    await post_to_instagram_via_zapier(
                        user_details=user_details,
                        attachments=downloaded_attachments,
                        caption=ig_caption,
                        jump_url=message.jump_url
                    )

                if downloaded_attachments and is_video:
                    self.logger.info(f"Attempting to post message {message_id} to TikTok via Zapier.")
                    tiktok_caption = f"{generated_title} - by {message.author.display_name}. {llm_description}"
                    await post_to_tiktok_via_zapier(
                        user_details=user_details,
                        attachments=downloaded_attachments,
                        caption=tiktok_caption,
                        jump_url=message.jump_url
                    )

                if downloaded_attachments and is_video:
                    self.logger.info(f"Attempting to post message {message_id} to YouTube via Zapier.")
                    youtube_title = generated_title or f"Cool video by {message.author.display_name}"
                    youtube_description = f"{llm_description}\n\nOriginally posted by {message.author.display_name} on Discord.\nOriginal post: {message.jump_url}"
                    await post_to_youtube_via_zapier(
                        user_details=user_details,
                        attachments=downloaded_attachments,
                        title=youtube_title,
                        description=youtube_description,
                        jump_url=message.jump_url
                    )

                # Removed summary channel posting to prevent messages appearing after "jump to beginning"
                # if summary_channel:
                #     # Create a unique key for this message-channel combination
                #     summary_key = f"{message_id}_{summary_channel.id}"
                #     if summary_key not in self._posted_to_summary:
                #         self.logger.info(f"Attempting to post summary to original summary channel {summary_channel.id} for message {message_id}")
                #         summary_content = f"Successfully shared post by <@{user_id}>: {message.jump_url}"
                #         await summary_channel.send(summary_content)
                #         self._posted_to_summary.add(summary_key)
                #         self.logger.info(f"Successfully posted summary for message {message_id} to summary channel {summary_channel.id}")
                #     else:
                #         self.logger.info(f"Summary for message {message_id} already posted to channel {summary_channel.id}, skipping duplicate post")

            except Exception as e:
                self.logger.error(f"An unexpected error occurred during finalize_sharing for message {message_id}: {e}", exc_info=True)
            finally:
                if message_id in self._currently_processing:
                    self._currently_processing.remove(message_id)
                self.logger.info(f"Finished processing sharing for message {message_id}.")
                if 'downloaded_attachments' in locals():
                    self._cleanup_files([att['local_path'] for att in downloaded_attachments if 'local_path' in att])

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
                self.logger.info(f"[Sharer._fetch_message] Channel {channel_id} not in cache, fetching.")
                channel = await self.bot.fetch_channel(channel_id)
                
            if isinstance(channel, (discord.TextChannel, discord.Thread)): # Added discord.Thread
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

    async def _announce_tweet_url(self, tweet_url: str, author_display_name: str, original_message_jump_url: Optional[str] = None, context_message_id: Optional[str] = None):
        if not ANNOUNCEMENT_CHANNEL_ID:
            self.logger.info("[Sharer] Tweet announcement channel ID not configured. Skipping announcement.")
            return

        try:
            channel = self.bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
            if not channel:
                self.logger.info(f"[Sharer] Announcement channel {ANNOUNCEMENT_CHANNEL_ID} not in cache, fetching.")
                channel = await self.bot.fetch_channel(ANNOUNCEMENT_CHANNEL_ID)
            
            if not isinstance(channel, discord.TextChannel):
                self.logger.error(f"[Sharer] Announcement channel {ANNOUNCEMENT_CHANNEL_ID} is not a text channel or could not be fetched.")
                return

            message_content = f"Tweet: {tweet_url}"
            if original_message_jump_url:
                message_content += f"\n\nBased on this post by {author_display_name}: {original_message_jump_url}"
            elif context_message_id: # Fallback for context if jump_url somehow not available
                message_content += f"\n\nBased on this post by {author_display_name} (Original Discord Message ID for context: {context_message_id})"
            else: # Absolute fallback
                message_content += f"\n\n(Shared content by {author_display_name})"
            
            if hasattr(self.bot, 'rate_limiter') and self.bot.rate_limiter is not None:
                await discord_utils.safe_send_message(
                    self.bot,
                    channel,
                    self.bot.rate_limiter,
                    self.logger,
                    content=message_content
                )
                self.logger.info(f"[Sharer] Announced tweet {tweet_url} to channel {ANNOUNCEMENT_CHANNEL_ID} via safe_send_message.")
            else:
                # Fallback to direct send if rate_limiter is not available
                self.logger.warning(f"[Sharer] Bot instance does not have a rate_limiter or it's None. Sending announcement for {tweet_url} directly to {ANNOUNCEMENT_CHANNEL_ID}.")
                await channel.send(content=message_content)
                self.logger.info(f"[Sharer] Announced tweet {tweet_url} to channel {ANNOUNCEMENT_CHANNEL_ID} (direct send).")

        except discord.NotFound:
            self.logger.error(f"[Sharer] Could not find announcement channel {ANNOUNCEMENT_CHANNEL_ID} via get_channel or fetch_channel.")
        except discord.Forbidden:
            self.logger.error(f"[Sharer] Bot lacks permissions to send message to announcement channel {ANNOUNCEMENT_CHANNEL_ID} or to fetch it.")
        except Exception as e:
            self.logger.error(f"[Sharer] Error announcing tweet to channel {ANNOUNCEMENT_CHANNEL_ID}: {e}", exc_info=True) 