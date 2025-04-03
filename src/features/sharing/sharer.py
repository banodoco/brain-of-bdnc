# Placeholder for Sharer class 

import discord
import logging
import os
import aiohttp
from pathlib import Path
from typing import List, Dict, Optional

from src.common.db_handler import DatabaseHandler
from src.common.claude_client import ClaudeClient
from .subfeatures.notify_user import send_sharing_request_dm
from .subfeatures.content_analyzer import generate_description_with_claude
from .subfeatures.social_poster import post_tweet

logger = logging.getLogger('DiscordBot')

class Sharer:
    def __init__(self, bot: discord.Client, db_handler: DatabaseHandler, logger_instance: logging.Logger, claude_client: ClaudeClient):
        self.bot = bot
        self.db_handler = db_handler
        self.logger = logger_instance
        self.claude_client = claude_client
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

    async def initiate_sharing_process(self, reaction: discord.Reaction, user: discord.User):
        """Starts the sharing process by sending a DM to the message author."""
        # Added detailed logging
        self.logger.debug(f"[Sharer] initiate_sharing_process called. Triggering User ID: {user.id}, Emoji: {reaction.emoji}, Message ID: {reaction.message.id}, Message Author ID: {reaction.message.author.id}")
        
        message = reaction.message
        author = message.author
        self.logger.info(f"Initiating sharing process for message {message.id} triggered by {user.id} reacting with {reaction.emoji}.")

        # Send the DM using the function from notify_user.py
        # Pass self (the Sharer instance) to break the import cycle
        await send_sharing_request_dm(self.bot, author, message, self.db_handler, self)
        
        # Note: The actual sharing (Claude, Twitter) happens *after* user interaction in the DM.
        # We need to modify `notify_user.py` to call `finalize_sharing` upon consent.

    async def finalize_sharing(self, user_id: int, message_id: int):
        """Fetches data, generates content, and posts to Twitter if consent is confirmed."""
        self.logger.info(f"Finalizing sharing process for user {user_id}, message {message_id}.")

        # 1. Fetch User Details (Confirm Consent Again)
        user_details = self.db_handler.get_member(user_id)
        if not user_details:
            self.logger.error(f"Cannot finalize sharing: User {user_id} not found in DB.")
            return
        
        if not user_details.get('sharing_consent', False):
            self.logger.warning(f"Cannot finalize sharing: User {user_id} consent is false in DB check for message {message_id}.")
            # This might happen in race conditions or if called incorrectly. Should ideally not occur if triggered by button.
            return

        # 2. Fetch Original Message
        # Need to reconstruct channel/message. This is tricky without the original context.
        # A better approach might be to store channel_id with message_id if needed later,
        # or pass the full message object initially if the finalize step is immediate.
        # For now, let's assume we can get the message object (requires bot to fetch it)
        # This part needs refinement based on how `finalize_sharing` is triggered.
        # TEMPORARY: Let's assume message object is passed or fetched.
        # We need the `message` object here.
        # For demonstration, let's simulate fetching it - **THIS NEEDS ACTUAL IMPLEMENTATION**
        message_object = await self._fetch_message_somehow(message_id) # Placeholder!
        if not message_object:
             self.logger.error(f"Cannot finalize sharing: Failed to fetch message {message_id}.")
             return
        
        # 3. Download Attachments
        downloaded_attachments = []
        if message_object.attachments:
            for attachment in message_object.attachments:
                downloaded = await self._download_attachment(attachment)
                if downloaded:
                    downloaded_attachments.append(downloaded)
        
        if not downloaded_attachments:
            self.logger.error(f"Cannot finalize sharing: No attachments found or failed to download for message {message_id}.")
            return # Can't post without media

        # 4. Generate Description with Claude
        self.logger.info(f"Generating description with Claude for message {message_id}.")
        generated_desc = await generate_description_with_claude(
            claude_client=self.claude_client,
            original_content=message_object.content,
            attachments=downloaded_attachments,
            user_name=user_details.get('global_name') or user_details.get('username')
        )

        if not generated_desc:
            self.logger.error(f"Failed to generate description with Claude for message {message_id}.")
            # Optionally: Post with a default message? For now, stop.
            self._cleanup_files([a['local_path'] for a in downloaded_attachments])
            return

        # 5. Post to Twitter
        self.logger.info(f"Posting to Twitter for message {message_id}.")
        tweet_url = await post_tweet(
            generated_description=generated_desc,
            user_details=user_details,
            attachments=downloaded_attachments,
            original_content=message_object.content
        )

        # 6. Log Outcome
        if tweet_url:
            self.logger.info(f"Successfully posted message {message_id} to Twitter: {tweet_url}")
            # Optional: Send confirmation to user or admin channel
        else:
            self.logger.error(f"Failed to post message {message_id} to Twitter.")
            # Optional: Notify admin channel

        # 7. Cleanup Downloaded Files
        self._cleanup_files([a['local_path'] for a in downloaded_attachments])

    def _cleanup_files(self, file_paths: List[str]):
        """Removes temporary files."""
        for file_path in file_paths:
            try:
                os.remove(file_path)
                self.logger.info(f"Removed temporary file: {file_path}")
            except OSError as e:
                self.logger.error(f"Error removing temporary file {file_path}: {e}")

    # Placeholder - Replace with actual message fetching logic
    async def _fetch_message_somehow(self, message_id: int) -> Optional[discord.Message]:
        """Placeholder: Needs a robust way to fetch a message by ID across channels."""
        self.logger.warning(f"_fetch_message_somehow is a placeholder. Needs implementation to find message {message_id}.")
        # This is complex because we don't know the channel.
        # Option 1: Store channel_id in DB alongside message_id when consent is granted.
        # Option 2: Iterate through guild channels (inefficient and potentially slow).
        # Option 3: Require channel_id to be passed to finalize_sharing.
        # For now, returning None.
        # Example using iteration (inefficient, use with caution):
        # for channel in self.bot.get_all_channels():
        #     if isinstance(channel, discord.TextChannel):
        #         try:
        #             msg = await channel.fetch_message(message_id)
        #             return msg
        #         except discord.NotFound:
        #             continue
        #         except discord.Forbidden:
        #             continue # Can't access channel
        #         except Exception as e:
        #              self.logger.error(f"Error fetching message {message_id} in channel {channel.id}: {e}")
        return None 