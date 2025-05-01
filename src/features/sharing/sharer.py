# Placeholder for Sharer class 

import discord
import logging
import os
import aiohttp
import asyncio # Added
from pathlib import Path
from typing import List, Dict, Optional

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
        """Downloads a single attachment to the temporary directory."""
        save_path = self.temp_dir / f"{attachment.id}_{attachment.filename}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(save_path, 'wb') as f:
                            f.write(await resp.read())
                        self.logger.info(f"Successfully downloaded attachment: {save_path}")
                        return {
                            'url': attachment.url,
                            'filename': attachment.filename,
                            'content_type': attachment.content_type,
                            'size': attachment.size,
                            'id': attachment.id,
                            'local_path': str(save_path) # Store local path
                        }
                    else:
                        self.logger.error(f"Failed to download attachment {attachment.url}. Status: {resp.status}")
                        return None
        except Exception as e:
            self.logger.error(f"Error downloading attachment {attachment.url}: {e}", exc_info=True)
            return None

    # Renamed original function for clarity
    async def initiate_sharing_process_from_reaction(self, reaction: discord.Reaction, user: discord.User):
        """Starts the sharing process via reaction by sending a DM to the message author."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_reaction called. Triggering User ID: {user.id}, Emoji: {reaction.emoji}, Message ID: {reaction.message.id}, Message Author ID: {reaction.message.author.id}")
        message = reaction.message
        author = message.author
        self.logger.info(f"Initiating sharing process for message {message.id} triggered by {user.id} reacting with {reaction.emoji}.")
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self)

    # Added new function to initiate from summary/message object
    async def initiate_sharing_process_from_summary(self, message: discord.Message):
        """Starts the sharing process directly from a message object (e.g., top art summary)."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_summary called. Message ID: {message.id}, Author ID: {message.author.id}")
        author = message.author
        # Check if author is a bot before proceeding
        if author.bot:
            self.logger.warning(f"Attempted to initiate sharing for a bot message ({message.id}). Skipping.")
            return
            
        self.logger.info(f"Initiating sharing process for message {message.id} requested via summary.")
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
        
        # 4. Generate Title and Description
        
        # Determine Title (using generate_media_title from social_poster)
        generated_title = "Featured Creation" # Default
        if is_video or (is_image and not is_gif): # Generate for video or non-GIF image
            self.logger.info(f"Generating media title for message {message_id} ({'video' if is_video else 'image'}).")
            # Call generate_media_title WITHOUT passing claude_client
            generated_title = await generate_media_title(
                # claude_client=self.claude_client, # REMOVED
                attachment=primary_attachment, 
                original_comment=message_object.content,
                post_id=message_id
            )
        elif is_gif:
             generated_title = "Cool Gif" # Keep simple default for GIFs
             logger.info(f"Using default title '{generated_title}' for GIF message {message_id}.")
        else: # Fallback for unknown types
             logger.warning(f"Unknown attachment type for title generation, using default for message {message_id}.")
             generated_title = "Featured Creation" 

        # Generate Description using LLM Dispatcher
        generated_desc = "Check out this amazing creation!" # Default fallback description
        try:
            # Define system prompt for description
            desc_system_prompt = (
                f"Based on the title \"{generated_title}\" and the artist's original comment below, write a short, engaging social media description (1-2 sentences). "
                f"Mention the type of media (e.g., 'artwork', 'video', 'creation'). Avoid simply repeating the title. "
                f"Focus on generating excitement or interest."
            )
            # Define user content for description
            desc_user_content = f"Artist's Comment: \"{message_object.content if message_object.content else 'None'}\""
            desc_messages = [{"role": "user", "content": desc_user_content}]
            
            self.logger.info(f"Generating description via dispatcher for message {message_id}...")
            # Use the dispatcher
            claude_desc = await get_llm_response(
                client_name="claude",
                model="claude-3-5-haiku-20240307", # Use Haiku for faster/cheaper description
                system_prompt=desc_system_prompt,
                messages=desc_messages,
                max_tokens=150, # Allow slightly longer description
            )
            if claude_desc:
                generated_desc = claude_desc.strip()
                self.logger.info(f"LLM dispatcher generated description for message {message_id}: {generated_desc}")
            else:
                 # Should not happen if dispatcher raises error, but good fallback logging
                self.logger.warning(f"LLM description generation failed or returned empty for message {message_id}, using default.")
        except Exception as e:
            self.logger.error(f"Error during LLM description generation for message {message_id}: {e}", exc_info=True)
            # Fallback to default description is handled by initial assignment

        self.logger.info(f"Using Title: '{generated_title}', Description: '{generated_desc}' for message {message_id}")

        # 5. Post to Social Media Platforms
        # Post to Twitter first
        self.logger.info(f"Attempting to post message {message_id} to Twitter.")
        tweet_url = await post_tweet(
            generated_description=generated_desc, # Use generated description
            user_details=user_details,
            attachments=downloaded_attachments, # Pass list
            original_content=message_object.content
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
            ig_payload = _build_zapier_payload("instagram", user_details, primary_attachment, generated_title, generated_desc, message_object.content)
            self.logger.info(f"Attempting to post message {message_id} to Instagram via Zapier.")
            await asyncio.to_thread(post_to_instagram_via_zapier, ig_payload) # Run sync requests in thread
            await asyncio.sleep(2)

            # TikTok
            tiktok_payload = _build_zapier_payload("tiktok", user_details, primary_attachment, generated_title, generated_desc, message_object.content)
            self.logger.info(f"Attempting to post message {message_id} to TikTok via Zapier.")
            await asyncio.to_thread(post_to_tiktok_via_zapier, tiktok_payload)
            await asyncio.sleep(2)

            # YouTube
            youtube_payload = _build_zapier_payload("youtube", user_details, primary_attachment, generated_title, generated_desc, message_object.content)
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