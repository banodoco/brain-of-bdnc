"""
Unified Storage Handler - Manages writes to SQLite, Supabase, or both.
This provides a single interface for storing messages regardless of backend.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

from src.common.constants import STORAGE_SQLITE, STORAGE_SUPABASE, STORAGE_BOTH, get_storage_backend

logger = logging.getLogger('DiscordBot')

class StorageHandler:
    """
    Unified handler for storing data to SQLite, Supabase, or both.
    Works in conjunction with DatabaseHandler for SQLite operations.
    """
    
    def __init__(self, storage_backend: Optional[str] = None):
        """
        Initialize the storage handler.
        
        Args:
            storage_backend: One of 'sqlite', 'supabase', or 'both'. 
                           If None, reads from STORAGE_BACKEND env var.
        """
        self.storage_backend = storage_backend or get_storage_backend()
        self.supabase_client: Optional[Client] = None
        self.batch_size = 100  # Batch size for Supabase writes
        
        logger.debug(f"Storage backend configured: {self.storage_backend}")
        
        # Initialize Supabase if needed
        if self.storage_backend in [STORAGE_SUPABASE, STORAGE_BOTH]:
            self._init_supabase()
    
    def _init_supabase(self) -> None:
        """Initialize the Supabase client."""
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
        
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set. Cannot use Supabase backend.")
            raise ValueError("Supabase credentials required when using supabase or both storage backends")
        
        try:
            options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
            self.supabase_client = create_client(supabase_url, supabase_key, options=options)
            logger.debug("Supabase client initialized successfully for direct writes")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
            raise
    
    def should_write_to_sqlite(self) -> bool:
        """Check if we should write to SQLite."""
        return self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]
    
    def should_write_to_supabase(self) -> bool:
        """Check if we should write to Supabase."""
        return self.storage_backend in [STORAGE_SUPABASE, STORAGE_BOTH]
    
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

