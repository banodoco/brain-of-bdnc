"""
Unified Storage Handler - Manages writes to Supabase.
This provides a single interface for storing messages.
"""

import asyncio
import json
import logging
import os
import re
import mimetypes
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path
import sys

import aiohttp

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

logger = logging.getLogger('DiscordBot')

class StorageHandler:
    """
    Handler for storing data to Supabase.
    """
    
    def __init__(self, storage_backend: Optional[str] = None):
        """
        Initialize the storage handler.
        
        Args:
            storage_backend: Ignored - always uses Supabase. Kept for backwards compatibility.
        """
        self.supabase_client: Optional[Client] = None
        self.batch_size = 100  # Batch size for Supabase writes
        
        logger.debug(f"Storage backend: supabase")
        
        # Initialize Supabase
        self._init_supabase()
    
    def _init_supabase(self) -> None:
        """Initialize the Supabase client."""
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
        
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set. Cannot use Supabase backend.")
            raise ValueError("Supabase credentials required")
        
        try:
            # Try with ClientOptions (newer API)
            try:
                options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
                self.supabase_client = create_client(supabase_url, supabase_key, options=options)
            except (AttributeError, TypeError):
                # Fall back to creating client without options if ClientOptions API has changed
                self.supabase_client = create_client(supabase_url, supabase_key)
            logger.debug("Supabase client initialized successfully for direct writes")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
            raise
    
    async def store_messages_to_supabase(self, messages: List[Dict]) -> int:
        """
        Store messages directly to Supabase.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Number of messages successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not messages:
            return 0
        
        try:
            # Transform messages to Supabase format
            supabase_messages = []
            for msg in messages:
                # Handle attachments and embeds - ensure they're properly formatted
                attachments = msg.get('attachments', [])
                if isinstance(attachments, str):
                    try:
                        attachments = json.loads(attachments)
                    except json.JSONDecodeError:
                        attachments = []
                
                embeds = msg.get('embeds', [])
                if isinstance(embeds, str):
                    try:
                        embeds = json.loads(embeds)
                    except json.JSONDecodeError:
                        embeds = []
                
                reactors = msg.get('reactors', [])
                if isinstance(reactors, str):
                    try:
                        reactors = json.loads(reactors)
                    except json.JSONDecodeError:
                        reactors = []
                
                supabase_msg = {
                    'message_id': msg.get('message_id') or msg.get('id'),
                    'channel_id': msg.get('channel_id'),
                    'author_id': msg.get('author_id'),
                    'content': msg.get('content'),
                    'created_at': msg.get('created_at'),
                    'attachments': attachments,
                    'embeds': embeds,
                    'reaction_count': msg.get('reaction_count', 0) or 0,
                    'reactors': reactors,
                    'reference_id': msg.get('reference_id'),
                    'edited_at': msg.get('edited_at'),
                    'is_pinned': bool(msg.get('is_pinned', False)),
                    'thread_id': msg.get('thread_id'),
                    'message_type': msg.get('message_type'),
                    'flags': msg.get('flags'),
                    'is_deleted': bool(msg.get('is_deleted', False)),
                    'indexed_at': msg.get('indexed_at') or datetime.utcnow().isoformat(),
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_messages.append(supabase_msg)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_messages), self.batch_size):
                batch = supabase_messages[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('discord_messages').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} messages to Supabase ({stored_count}/{len(supabase_messages)})")
                except Exception as e:
                    logger.error(f"Failed to store message batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} messages directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing messages to Supabase: {e}", exc_info=True)
            return 0
    
    async def store_members_to_supabase(self, members: List[Dict]) -> int:
        """
        Store member profiles directly to Supabase.
        
        Args:
            members: List of member dictionaries
            
        Returns:
            Number of members successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not members:
            return 0
        
        try:
            supabase_members = []
            for member in members:
                # Parse role_ids if it's a string
                role_ids = member.get('role_ids', [])
                if isinstance(role_ids, str):
                    try:
                        role_ids = json.loads(role_ids)
                    except json.JSONDecodeError:
                        role_ids = []
                
                supabase_member = {
                    'member_id': member.get('member_id'),
                    'username': member.get('username'),
                    'global_name': member.get('global_name'),
                    'server_nick': member.get('server_nick') or member.get('display_name'),
                    'avatar_url': member.get('avatar_url'),
                    'discriminator': member.get('discriminator'),
                    'bot': bool(member.get('bot', False)),
                    'system': bool(member.get('system', False)),
                    'accent_color': member.get('accent_color'),
                    'banner_url': member.get('banner_url'),
                    'discord_created_at': member.get('discord_created_at'),
                    'guild_join_date': member.get('guild_join_date'),
                    'role_ids': role_ids,
                    'twitter_handle': member.get('twitter_handle'),
                    'instagram_handle': member.get('instagram_handle'),
                    'youtube_handle': member.get('youtube_handle'),
                    'tiktok_handle': member.get('tiktok_handle'),
                    'website': member.get('website'),
                    'sharing_consent': bool(member.get('sharing_consent', False)),
                    'dm_preference': bool(member.get('dm_preference', True)),
                    'permission_to_curate': member.get('permission_to_curate'),
                    'created_at': member.get('created_at') or datetime.utcnow().isoformat(),
                    'updated_at': member.get('updated_at') or datetime.utcnow().isoformat(),
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_members.append(supabase_member)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_members), self.batch_size):
                batch = supabase_members[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('discord_members').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} members to Supabase")
                except Exception as e:
                    logger.error(f"Failed to store member batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} members directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing members to Supabase: {e}", exc_info=True)
            return 0
    
    async def store_channels_to_supabase(self, channels: List[Dict]) -> int:
        """
        Store channels directly to Supabase.
        
        Args:
            channels: List of channel dictionaries
            
        Returns:
            Number of channels successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not channels:
            return 0
        
        try:
            supabase_channels = []
            for channel in channels:
                supabase_channel = {
                    'channel_id': channel.get('channel_id'),
                    'channel_name': channel.get('channel_name'),
                    'category_id': channel.get('category_id'),
                    'description': channel.get('description'),
                    'suitable_posts': channel.get('suitable_posts'),
                    'unsuitable_posts': channel.get('unsuitable_posts'),
                    'rules': channel.get('rules'),
                    'setup_complete': bool(channel.get('setup_complete', False)),
                    'nsfw': bool(channel.get('nsfw', False)),
                    'enriched': bool(channel.get('enriched', False)),
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_channels.append(supabase_channel)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_channels), self.batch_size):
                batch = supabase_channels[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('discord_channels').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} channels to Supabase")
                except Exception as e:
                    logger.error(f"Failed to store channel batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} channels directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing channels to Supabase: {e}", exc_info=True)
            return 0
    
    async def get_summary_for_date(
        self,
        channel_id: int,
        date: Optional[datetime] = None,
        dev_mode: bool = False,
    ) -> Optional[str]:
        """
        Get the full summary for a channel on a given date.
        
        Args:
            channel_id: The channel ID
            date: Date to check (defaults to today)
            
        Returns:
            The full_summary text if exists, None otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            result = await asyncio.to_thread(
                lambda: self.supabase_client.table('daily_summaries')
                    .select('full_summary')
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute()
            )
            
            if result.data and len(result.data) > 0:
                return result.data[0].get('full_summary')
            return None
            
        except Exception as e:
            logger.error(f"Error getting summary for date: {e}", exc_info=True)
            return None

    async def summary_exists_for_date(
        self,
        channel_id: int,
        date: Optional[datetime] = None,
        dev_mode: bool = False,
    ) -> bool:
        """
        Check if a summary already exists for a channel on a given date.
        
        Args:
            channel_id: The channel ID
            date: Date to check (defaults to today)
            
        Returns:
            True if summary exists, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            result = await asyncio.to_thread(
                lambda: self.supabase_client.table('daily_summaries')
                    .select('channel_id')
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute()
            )
            
            exists = bool(result.data and len(result.data) > 0)
            logger.debug(f"Summary exists check for channel {channel_id}, date {summary_date}: {exists}")
            return exists
            
        except Exception as e:
            logger.error(f"Error checking if summary exists: {e}", exc_info=True)
            return False

    async def store_daily_summary_to_supabase(
        self, 
        channel_id: int, 
        full_summary: Optional[str], 
        short_summary: Optional[str], 
        date: Optional[datetime] = None,
        included_in_main_summary: bool = False,
        dev_mode: bool = False
    ) -> bool:
        """
        Store a daily summary to Supabase.
        
        Args:
            channel_id: The channel ID
            full_summary: Full summary text
            short_summary: Short summary text
            date: Date of the summary (defaults to today)
            included_in_main_summary: Whether items from this summary were included in the main summary
            dev_mode: Whether this summary was created in development mode
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            summary_data = {
                'date': summary_date,
                'channel_id': channel_id,
                'full_summary': full_summary,
                'short_summary': short_summary,
                'created_at': datetime.utcnow().isoformat(),
                'included_in_main_summary': included_in_main_summary,
                'dev_mode': dev_mode
            }
            
            await asyncio.to_thread(
                self.supabase_client.table('daily_summaries').upsert(
                    summary_data, 
                    on_conflict='date,channel_id'
                ).execute
            )
            
            logger.debug(f"Stored daily summary to Supabase for channel {channel_id}, date {summary_date} (dev_mode={dev_mode})")
            return True
            
        except Exception as e:
            logger.error(f"Error storing daily summary to Supabase: {e}", exc_info=True)
            return False

    async def mark_summaries_included_in_main(
        self,
        date: datetime,
        channel_message_ids: Dict[int, List[str]],
        dev_mode: bool = False,
    ) -> bool:
        """
        Mark channel summaries as having items included in the main summary.
        
        Args:
            date: The date of the summaries
            channel_message_ids: Dict mapping channel_id -> list of message_ids that were included
            
        Returns:
            True if all updates successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        summary_date = date.strftime('%Y-%m-%d')
        all_success = True
        
        for channel_id, _message_ids in channel_message_ids.items():
            try:
                await asyncio.to_thread(
                    self.supabase_client.table('daily_summaries')
                    .update({
                        'included_in_main_summary': True
                    })
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute
                )
                logger.debug(f"Marked summary for channel {channel_id} as included in main summary")
            except Exception as e:
                logger.error(f"Error marking summary for channel {channel_id} as included: {e}", exc_info=True)
                all_success = False
        
        return all_success

    async def update_channel_summary_full_summary(
        self,
        channel_id: int,
        date: datetime,
        full_summary: str,
        dev_mode: bool = False,
    ) -> bool:
        """
        Update the full_summary field for a channel's daily summary.
        Used to enrich channel summaries with inclusion flags and media URLs.
        
        Args:
            channel_id: The channel ID
            date: The date of the summary
            full_summary: The enriched full_summary JSON string
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        summary_date = date.strftime('%Y-%m-%d')
        
        try:
            await asyncio.to_thread(
                self.supabase_client.table('daily_summaries')
                .update({'full_summary': full_summary})
                .eq('date', summary_date)
                .eq('channel_id', channel_id)
                .eq('dev_mode', dev_mode)
                .execute
            )
            logger.debug(f"Updated full_summary for channel {channel_id} on {summary_date}")
            return True
        except Exception as e:
            logger.error(f"Error updating full_summary for channel {channel_id}: {e}", exc_info=True)
            return False
    
    async def update_summary_thread_to_supabase(self, channel_id: int, thread_id: Optional[int]) -> bool:
        """
        Update or delete a summary thread ID in Supabase.
        
        Args:
            channel_id: The channel ID
            thread_id: The thread ID (None to delete)
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            if thread_id:
                # Upsert the thread ID
                thread_data = {
                    'channel_id': channel_id,
                    'summary_thread_id': thread_id,
                    'updated_at': datetime.utcnow().isoformat()
                }
                
                await asyncio.to_thread(
                    self.supabase_client.table('channel_summary').upsert(thread_data).execute
                )
                logger.debug(f"Updated summary thread ID to {thread_id} for channel {channel_id} in Supabase")
            else:
                # Delete the entry
                await asyncio.to_thread(
                    self.supabase_client.table('channel_summary').delete().eq('channel_id', channel_id).execute
                )
                logger.debug(f"Deleted summary thread entry for channel {channel_id} in Supabase")
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating summary thread in Supabase: {e}", exc_info=True)
            return False

    # ========== Media Storage Methods ==========
    
    SUMMARY_MEDIA_BUCKET = "summary-media"
    MAX_UPLOAD_ATTEMPTS = 3
    BASE_RETRY_DELAY = 1.0

    async def upload_bytes_to_storage(
        self,
        file_bytes: bytes,
        storage_path: str,
        content_type: str,
        bucket_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Upload raw bytes to Supabase Storage with retry logic.
        
        Args:
            file_bytes: The raw bytes to upload
            storage_path: Path within the bucket (e.g., "2025-12-27/1234567890_0.mp4")
            content_type: MIME type of the file
            bucket_name: Target bucket (defaults to SUMMARY_MEDIA_BUCKET)
            
        Returns:
            Public URL of the uploaded file, or None on failure
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized for storage upload")
            return None
        
        bucket = bucket_name or self.SUMMARY_MEDIA_BUCKET
        
        for attempt in range(self.MAX_UPLOAD_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    self.supabase_client.storage.from_(bucket).upload,
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": "true"}
                )
                logger.debug(f"Uploaded {len(file_bytes)} bytes to {bucket}/{storage_path}")
                
                # Get public URL
                public_url = await asyncio.to_thread(
                    self.supabase_client.storage.from_(bucket).get_public_url,
                    storage_path
                )
                
                if public_url and isinstance(public_url, str):
                    return public_url.strip()
                    
                logger.warning(f"Got invalid URL after upload: {public_url}")
                return None
                
            except Exception as e:
                logger.warning(f"Upload attempt {attempt + 1}/{self.MAX_UPLOAD_ATTEMPTS} failed: {e}")
                if attempt + 1 < self.MAX_UPLOAD_ATTEMPTS:
                    await asyncio.sleep(self.BASE_RETRY_DELAY * (2 ** attempt))
                else:
                    logger.error(f"Upload to {bucket}/{storage_path} failed after {self.MAX_UPLOAD_ATTEMPTS} attempts")
        
        return None

    async def download_file(self, source_url: str) -> Optional[Dict[str, any]]:
        """
        Download a file from a URL.
        
        Args:
            source_url: URL to download from (e.g., Discord CDN URL)
            
        Returns:
            Dict with 'bytes', 'content_type', 'filename' or None on failure
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source_url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to download {source_url}: HTTP {response.status}")
                        return None
                    
                    file_bytes = await response.read()
                    
                    # Determine content type from response or URL
                    content_type = response.content_type
                    if not content_type or content_type == 'application/octet-stream':
                        guessed_type, _ = mimetypes.guess_type(source_url.split('?')[0])
                        content_type = guessed_type or 'application/octet-stream'
                    
                    # Extract filename from URL
                    url_path = source_url.split('?')[0]
                    filename = url_path.split('/')[-1] if '/' in url_path else 'file'
                    
                    logger.debug(f"Downloaded {len(file_bytes)} bytes ({content_type}) from {source_url[:80]}...")
                    
                    return {
                        'bytes': file_bytes,
                        'content_type': content_type,
                        'filename': filename
                    }
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout downloading {source_url}")
            return None
        except Exception as e:
            logger.error(f"Error downloading {source_url}: {e}", exc_info=True)
            return None

    async def download_and_upload_url(
        self,
        source_url: str,
        storage_path: str,
        bucket_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Download a file from a URL and upload to Supabase Storage.
        
        Args:
            source_url: URL to download from (e.g., Discord CDN URL)
            storage_path: Path within the bucket
            bucket_name: Target bucket (defaults to SUMMARY_MEDIA_BUCKET)
            
        Returns:
            Public URL of the uploaded file, or None on failure
        """
        file_data = await self.download_file(source_url)
        if not file_data:
            return None
        
        return await self.upload_bytes_to_storage(
            file_data['bytes'], storage_path, file_data['content_type'], bucket_name
        )
