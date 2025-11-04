#!/usr/bin/env python3
"""
Sync script to automatically sync messages and member profiles from SQLite to Supabase.
This script creates the necessary tables in Supabase and syncs data from the local production database.
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

# Add the project root to the path so we can import our modules
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
# Simple logging setup
def setup_logging(dev_mode=False):
    logging.basicConfig(
        level=logging.DEBUG if dev_mode else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger('SupabaseSync')

# Configuration
BATCH_SIZE = 100  # Number of records to sync at once
SYNC_INTERVAL = 300  # Sync every 5 minutes (for continuous mode)

class SupabaseSync:
    """Handles syncing data from SQLite to Supabase."""
    
    def __init__(self, sqlite_db_path: str, supabase_url: str, supabase_key: str, logger: logging.Logger):
        self.sqlite_db_path = sqlite_db_path
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.logger = logger
        self.supabase: Optional[Client] = None
        self._init_supabase()
    
    def _init_supabase(self) -> None:
        """Initialize the Supabase client."""
        if not self.supabase_url or not self.supabase_key:
            self.logger.error("Supabase URL or Service Key is missing. Cannot initialize client.")
            return
        
        try:
            self.logger.info("Initializing Supabase client for sync...")
            options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
            self.supabase = create_client(self.supabase_url, self.supabase_key, options=options)
            self.logger.info("Supabase client initialized successfully.")
        except Exception as e:
            self.logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
    
    async def create_tables_if_not_exist(self) -> bool:
        """Create the necessary tables in Supabase if they don't exist."""
        if not self.supabase:
            self.logger.error("Supabase client not initialized.")
            return False
        
        try:
            self.logger.info("Checking if Supabase tables exist...")
            
            # Check if tables exist by trying to query them
            tables_to_check = ['discord_messages', 'discord_members', 'discord_channels']
            
            for table_name in tables_to_check:
                try:
                    # Try to query the table with a limit of 0 to check if it exists
                    await asyncio.to_thread(
                        self.supabase.table(table_name).select('*').limit(0).execute
                    )
                    self.logger.info(f"Table {table_name} already exists.")
                except Exception as e:
                    self.logger.warning(f"Table {table_name} may not exist or is not accessible: {e}")
            
            self.logger.info("Tables check completed. Please ensure the tables are created using the provided SQL schema.")
            self.logger.info("You can run the create_supabase_schema.sql file in your Supabase SQL editor.")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to check tables in Supabase: {e}", exc_info=True)
            return False
    
    def get_sqlite_connection(self) -> sqlite3.Connection:
        """Get a connection to the SQLite database."""
        conn = sqlite3.connect(self.sqlite_db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row  # This allows accessing columns by name
        return conn
    
    async def sync_messages(self, limit: Optional[int] = None) -> int:
        """Sync messages from SQLite to Supabase."""
        if not self.supabase:
            self.logger.error("Supabase client not initialized.")
            return 0
        
        self.logger.info("Starting messages sync...")
        
        try:
            # Get the latest synced message timestamp from Supabase
            latest_sync = await asyncio.to_thread(
                self.supabase.table('discord_messages')
                .select('created_at')
                .order('created_at', desc=True)
                .limit(1)
                .execute
            )
            
            last_sync_time = None
            if latest_sync.data:
                last_sync_time = latest_sync.data[0]['created_at']
                self.logger.info(f"Last synced message timestamp: {last_sync_time}")
            
            # Get messages from SQLite that are newer than the last sync
            conn = self.get_sqlite_connection()
            cursor = conn.cursor()
            
            if last_sync_time:
                query = """
                SELECT * FROM messages 
                WHERE created_at > ? 
                ORDER BY created_at ASC
                """
                params = (last_sync_time,)
            else:
                query = """
                SELECT * FROM messages 
                ORDER BY created_at ASC
                """
                params = ()
            
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, params)
            messages = cursor.fetchall()
            conn.close()
            
            if not messages:
                self.logger.info("No new messages to sync.")
                return 0
            
            self.logger.info(f"Found {len(messages)} messages to sync.")
            
            # Convert SQLite rows to Supabase format
            supabase_messages = []
            for msg in messages:
                # Parse JSON fields
                attachments = json.loads(msg['attachments']) if msg['attachments'] else []
                embeds = json.loads(msg['embeds']) if msg['embeds'] else []
                reactors = json.loads(msg['reactors']) if msg['reactors'] else []
                
                supabase_msg = {
                    'message_id': msg['message_id'],
                    'channel_id': msg['channel_id'],
                    'author_id': msg['author_id'],
                    'content': msg['content'],
                    'created_at': msg['created_at'],
                    'attachments': attachments,
                    'embeds': embeds,
                    'reaction_count': msg['reaction_count'] or 0,
                    'reactors': reactors,
                    'reference_id': msg['reference_id'],
                    'edited_at': msg['edited_at'],
                    'is_pinned': bool(msg['is_pinned']),
                    'thread_id': msg['thread_id'],
                    'message_type': msg['message_type'],
                    'flags': msg['flags'],
                    'is_deleted': bool(msg['is_deleted']),
                    'indexed_at': msg['indexed_at'],
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_messages.append(supabase_msg)
            
            # Sync in batches
            synced_count = 0
            for i in range(0, len(supabase_messages), BATCH_SIZE):
                batch = supabase_messages[i:i + BATCH_SIZE]
                
                try:
                    await asyncio.to_thread(
                        self.supabase.table('discord_messages').upsert(batch).execute
                    )
                    synced_count += len(batch)
                    self.logger.info(f"Synced batch of {len(batch)} messages ({synced_count}/{len(supabase_messages)})")
                    
                except Exception as e:
                    self.logger.error(f"Failed to sync message batch: {e}", exc_info=True)
                    continue
            
            self.logger.info(f"Successfully synced {synced_count} messages to Supabase.")
            return synced_count
            
        except Exception as e:
            self.logger.error(f"Error during messages sync: {e}", exc_info=True)
            return 0
    
    async def sync_members(self, limit: Optional[int] = None) -> int:
        """Sync member profiles from SQLite to Supabase."""
        if not self.supabase:
            self.logger.error("Supabase client not initialized.")
            return 0
        
        self.logger.info("Starting members sync...")
        
        try:
            # Get the latest synced member timestamp from Supabase
            latest_sync = await asyncio.to_thread(
                self.supabase.table('discord_members')
                .select('updated_at')
                .order('updated_at', desc=True)
                .limit(1)
                .execute
            )
            
            last_sync_time = None
            if latest_sync.data:
                last_sync_time = latest_sync.data[0]['updated_at']
                self.logger.info(f"Last synced member timestamp: {last_sync_time}")
            
            # Get members from SQLite that are newer than the last sync
            conn = self.get_sqlite_connection()
            cursor = conn.cursor()
            
            if last_sync_time:
                query = """
                SELECT * FROM members 
                WHERE updated_at > ? 
                ORDER BY updated_at ASC
                """
                params = (last_sync_time,)
            else:
                query = """
                SELECT * FROM members 
                ORDER BY updated_at ASC
                """
                params = ()
            
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, params)
            members = cursor.fetchall()
            conn.close()
            
            if not members:
                self.logger.info("No new members to sync.")
                return 0
            
            self.logger.info(f"Found {len(members)} members to sync.")
            
            # Convert SQLite rows to Supabase format
            supabase_members = []
            for member in members:
                # Parse JSON fields
                role_ids = json.loads(member['role_ids']) if member['role_ids'] else []
                
                supabase_member = {
                    'member_id': member['member_id'],
                    'username': member['username'],
                    'global_name': member['global_name'],
                    'server_nick': member['server_nick'],
                    'avatar_url': member['avatar_url'],
                    'discriminator': member['discriminator'],
                    'bot': bool(member['bot']),
                    'system': bool(member['system']),
                    'accent_color': member['accent_color'],
                    'banner_url': member['banner_url'],
                    'discord_created_at': member['discord_created_at'],
                    'guild_join_date': member['guild_join_date'],
                    'role_ids': role_ids,
                    'twitter_handle': member['twitter_handle'],
                    'instagram_handle': member['instagram_handle'],
                    'youtube_handle': member['youtube_handle'],
                    'tiktok_handle': member['tiktok_handle'],
                    'website': member['website'],
                    'sharing_consent': bool(member['sharing_consent']),
                    'dm_preference': bool(member['dm_preference']),
                    'permission_to_curate': member['permission_to_curate'] if member['permission_to_curate'] is not None else None,
                    'created_at': member['created_at'],
                    'updated_at': member['updated_at'],
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_members.append(supabase_member)
            
            # Sync in batches
            synced_count = 0
            for i in range(0, len(supabase_members), BATCH_SIZE):
                batch = supabase_members[i:i + BATCH_SIZE]
                
                try:
                    await asyncio.to_thread(
                        self.supabase.table('discord_members').upsert(batch).execute
                    )
                    synced_count += len(batch)
                    self.logger.info(f"Synced batch of {len(batch)} members ({synced_count}/{len(supabase_members)})")
                    
                except Exception as e:
                    self.logger.error(f"Failed to sync member batch: {e}", exc_info=True)
                    continue
            
            self.logger.info(f"Successfully synced {synced_count} members to Supabase.")
            return synced_count
            
        except Exception as e:
            self.logger.error(f"Error during members sync: {e}", exc_info=True)
            return 0
    
    async def sync_channels(self, limit: Optional[int] = None) -> int:
        """Sync channels from SQLite to Supabase."""
        if not self.supabase:
            self.logger.error("Supabase client not initialized.")
            return 0
        
        self.logger.info("Starting channels sync...")
        
        try:
            # Get all channels from SQLite (channels don't have updated_at, so sync all)
            conn = self.get_sqlite_connection()
            cursor = conn.cursor()
            
            query = "SELECT * FROM channels ORDER BY channel_id ASC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query)
            channels = cursor.fetchall()
            conn.close()
            
            if not channels:
                self.logger.info("No channels to sync.")
                return 0
            
            self.logger.info(f"Found {len(channels)} channels to sync.")
            
            # Convert SQLite rows to Supabase format
            supabase_channels = []
            for channel in channels:
                supabase_channel = {
                    'channel_id': channel['channel_id'],
                    'channel_name': channel['channel_name'],
                    'category_id': channel['category_id'],
                    'description': channel['description'],
                    'suitable_posts': channel['suitable_posts'],
                    'unsuitable_posts': channel['unsuitable_posts'],
                    'rules': channel['rules'],
                    'setup_complete': bool(channel['setup_complete']),
                    'nsfw': bool(channel['nsfw']),
                    'enriched': bool(channel['enriched']),
                    'synced_at': datetime.utcnow().isoformat()
                }
                supabase_channels.append(supabase_channel)
            
            # Sync in batches
            synced_count = 0
            for i in range(0, len(supabase_channels), BATCH_SIZE):
                batch = supabase_channels[i:i + BATCH_SIZE]
                
                try:
                    await asyncio.to_thread(
                        self.supabase.table('discord_channels').upsert(batch).execute
                    )
                    synced_count += len(batch)
                    self.logger.info(f"Synced batch of {len(batch)} channels ({synced_count}/{len(supabase_channels)})")
                    
                except Exception as e:
                    self.logger.error(f"Failed to sync channel batch: {e}", exc_info=True)
                    continue
            
            self.logger.info(f"Successfully synced {synced_count} channels to Supabase.")
            return synced_count
            
        except Exception as e:
            self.logger.error(f"Error during channels sync: {e}", exc_info=True)
            return 0
    
    async def full_sync(self, limit: Optional[int] = None) -> Dict[str, int]:
        """Perform a full sync of all data types."""
        self.logger.info("Starting full sync to Supabase...")
        
        # First, create tables if they don't exist
        if not await self.create_tables_if_not_exist():
            self.logger.error("Failed to create tables. Aborting sync.")
            return {'messages': 0, 'members': 0, 'channels': 0}
        
        # Sync all data types
        results = {}
        results['channels'] = await self.sync_channels(limit)
        results['members'] = await self.sync_members(limit)
        results['messages'] = await self.sync_messages(limit)
        
        total_synced = sum(results.values())
        self.logger.info(f"Full sync completed. Total records synced: {total_synced}")
        self.logger.info(f"Breakdown: {results}")
        
        return results
    
    async def continuous_sync(self, interval: int = SYNC_INTERVAL):
        """Run continuous sync at specified intervals."""
        self.logger.info(f"Starting continuous sync with {interval} second intervals...")
        
        while True:
            try:
                await self.full_sync()
                self.logger.info(f"Sleeping for {interval} seconds...")
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                self.logger.info("Continuous sync interrupted by user.")
                break
            except Exception as e:
                self.logger.error(f"Error in continuous sync: {e}", exc_info=True)
                await asyncio.sleep(interval)


async def main():
    """Main function to run the sync script."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Sync Discord data from SQLite to Supabase')
    parser.add_argument('--dev', action='store_true', help='Use development database')
    parser.add_argument('--continuous', action='store_true', help='Run continuous sync')
    parser.add_argument('--limit', type=int, help='Limit number of records to sync (for testing)')
    parser.add_argument('--messages-only', action='store_true', help='Sync only messages')
    parser.add_argument('--members-only', action='store_true', help='Sync only members')
    parser.add_argument('--channels-only', action='store_true', help='Sync only channels')
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(dev_mode=args.dev)
    logger.info("Starting Supabase sync script...")
    
    # Get database path
    if args.dev:
        db_path = "data/dev.db"
    else:
        db_path = "data/production.db"
    
    if not os.path.exists(db_path):
        logger.error(f"Database file not found: {db_path}")
        return
    
    # Get Supabase credentials
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables must be set.")
        return
    
    # Initialize sync client
    sync_client = SupabaseSync(db_path, supabase_url, supabase_key, logger)
    
    try:
        if args.continuous:
            await sync_client.continuous_sync()
        elif args.messages_only:
            await sync_client.sync_messages(args.limit)
        elif args.members_only:
            await sync_client.sync_members(args.limit)
        elif args.channels_only:
            await sync_client.sync_channels(args.limit)
        else:
            await sync_client.full_sync(args.limit)
            
    except KeyboardInterrupt:
        logger.info("Sync interrupted by user.")
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
