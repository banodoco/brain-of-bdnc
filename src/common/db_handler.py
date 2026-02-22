import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import asyncio

logger = logging.getLogger('DiscordBot')

def to_aware_utc(dt_str: str) -> Optional[datetime]:
    """Convert an ISO format string to a timezone-aware datetime object in UTC."""
    if not dt_str:
        return None
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

class DatabaseHandler:
    def __init__(self, db_path: Optional[str] = None, dev_mode: bool = False, pool_size: int = 5, storage_backend: Optional[str] = None):
        """Initialize database handler with Supabase backend."""
        try:
            self.dev_mode = dev_mode
            
            # Initialize Supabase handlers
            self.storage_handler = None
            self.query_handler = None
            try:
                from .storage_handler import StorageHandler
                from .supabase_query_handler import SupabaseQueryHandler
                self.storage_handler = StorageHandler()
                # Use the same Supabase client for queries
                self.query_handler = SupabaseQueryHandler(self.storage_handler.supabase_client)
                logger.debug(f"Supabase handlers initialized for read/write operations")
            except Exception as e:
                logger.error(f"Failed to initialize Supabase handlers: {e}", exc_info=True)
                raise
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def _run_async_in_thread(self, coro):
        """Helper to run async operations from sync context."""
        try:
            # Check if we're already in an async context
            try:
                asyncio.get_running_loop()
                # We're in an async context - need to run in a separate thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, coro)
                    return future.result(timeout=30)
            except RuntimeError:
                # Not in an async context, safe to use asyncio.run
                return asyncio.run(coro)
        except Exception as e:
            logger.error(f"Error running async operation: {e}", exc_info=True)
            raise

    def close(self):
        """Close the database connection (no-op for Supabase)."""
        pass

    def __del__(self):
        """Ensure connection is closed when object is destroyed."""
        self.close()

    def execute_query(self, query: str, params: tuple = ()) -> List[dict]:
        """
        Execute a raw SQL query via Supabase.
        """
        try:
            logger.info(f"🔄 [DB HANDLER] Routing query to SUPABASE")
            logger.info(f"🔄 [DB HANDLER] Query preview: {query[:200]}")
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(query, params if params else None)
            )
            logger.info(f"✅ [DB HANDLER] Supabase returned {len(result)} results")
            return result
        except Exception as e:
            logger.error(f"❌ [DB HANDLER] Supabase query failed: {e}")
            raise

    async def store_messages(self, messages: List[Dict]):
        """Store messages to Supabase."""
        if self.storage_handler:
            await self.storage_handler.store_messages_to_supabase(messages)

    def get_last_message_id(self, channel_id: int) -> Optional[int]:
        """Get the most recent message ID for a channel."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_last_message_id(channel_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def search_messages(self, query: str, channel_id: Optional[int] = None) -> List[Dict]:
        """Search messages by content using Supabase ILIKE."""
        try:
            sql = """
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM discord_messages m 
                JOIN discord_members mb ON m.author_id = mb.member_id
                WHERE m.content ILIKE ?
            """
            params = [f'%{query}%']
            if channel_id:
                sql += " AND m.channel_id = ?"
                params.append(channel_id)
            
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(sql, tuple(params))
            )
            return result
        except Exception as e:
            logger.error(f"Supabase query failed for search_messages: {e}")
            return []

    def get_summary_for_date(self, channel_id: int, date: Optional[datetime] = None) -> Optional[str]:
        """Get the full summary for a channel on a given date."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.get_summary_for_date(channel_id, date, self.dev_mode)
            )
        return None

    def summary_exists_for_date(self, channel_id: int, date: Optional[datetime] = None) -> bool:
        """Check if a summary already exists for a channel on a given date."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.summary_exists_for_date(channel_id, date, self.dev_mode)
            )
        return False

    def store_daily_summary(
        self, 
        channel_id: int, 
        full_summary: Optional[str], 
        short_summary: Optional[str], 
        date: Optional[datetime] = None,
        included_in_main_summary: bool = False,
        dev_mode: bool = False
    ) -> bool:
        """Store a daily summary to Supabase."""
        if self.storage_handler:
            logger.info(f"Storing summary to Supabase for channel {channel_id} (dev_mode={dev_mode})")
            supabase_result = self._run_async_in_thread(
                self.storage_handler.store_daily_summary_to_supabase(
                    channel_id, full_summary, short_summary, date,
                    included_in_main_summary, dev_mode
                )
            )
            if not supabase_result:
                logger.error(f"Failed to store summary to Supabase for channel {channel_id}")
                return False
            return True
        else:
            logger.warning("Storage handler not initialized, cannot store to Supabase")
            return False

    def mark_summaries_included_in_main(self, date: datetime, channel_message_ids: Dict[int, List[str]]) -> bool:
        """Mark channel summaries as having items included in the main summary."""
        if self.storage_handler:
            logger.info(f"Marking {len(channel_message_ids)} channel summaries as included in main summary")
            return self._run_async_in_thread(
                self.storage_handler.mark_summaries_included_in_main(date, channel_message_ids, self.dev_mode)
            )
        else:
            logger.warning("Storage handler not initialized, cannot mark summaries")
            return False

    async def download_and_upload_media(self, source_url: str, storage_path: str) -> Optional[str]:
        """Download media from URL and upload to Supabase Storage."""
        if self.storage_handler:
            return await self.storage_handler.download_and_upload_url(source_url, storage_path)
        else:
            logger.warning("Storage handler not initialized, cannot upload media")
            return None

    async def download_file(self, source_url: str) -> Optional[Dict[str, any]]:
        """Download a file and return bytes + metadata."""
        if self.storage_handler:
            return await self.storage_handler.download_file(source_url)
        else:
            logger.warning("Storage handler not initialized, cannot download file")
            return None

    async def upload_bytes(self, file_bytes: bytes, storage_path: str, content_type: str) -> Optional[str]:
        """Upload raw bytes to Supabase Storage."""
        if self.storage_handler:
            return await self.storage_handler.upload_bytes_to_storage(file_bytes, storage_path, content_type)
        else:
            logger.warning("Storage handler not initialized, cannot upload bytes")
            return None

    def update_channel_summary_full_summary(self, channel_id: int, date: datetime, full_summary: str) -> bool:
        """Update the full_summary for a channel's daily summary (used to add inclusion flags and media URLs)."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.update_channel_summary_full_summary(channel_id, date, full_summary, self.dev_mode)
            )
        else:
            logger.warning("Storage handler not initialized, cannot update full_summary")
            return False

    def get_summary_thread_id(self, channel_id: int) -> Optional[int]:
        """Get the summary thread ID for a channel."""
        if self.query_handler:
            try:
                logger.debug(f"Fetching summary thread ID from Supabase for channel {channel_id}")
                return self._run_async_in_thread(
                    self.query_handler.get_summary_thread_id(channel_id)
                )
            except Exception as e:
                logger.error(f"Failed to get summary thread ID from Supabase: {e}")
                raise
        return None

    def update_summary_thread(self, channel_id: int, thread_id: Optional[int]):
        """Update the summary thread ID for a channel."""
        if self.storage_handler:
            logger.debug(f"Updating summary thread ID in Supabase for channel {channel_id}: {thread_id}")
            self._run_async_in_thread(
                self.storage_handler.update_summary_thread_to_supabase(channel_id, thread_id)
            )

    def get_all_message_ids(self, channel_id: int) -> List[int]:
        """Get all message IDs for a channel."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT message_id FROM discord_messages WHERE channel_id = ?",
                    (channel_id,)
                )
            )
            return [row.get('message_id') for row in result if row.get('message_id')]
        except Exception as e:
            logger.error(f"Supabase query failed for get_all_message_ids: {e}")
            return []

    def get_message_date_range(self, channel_id: int) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Get the date range of messages in a channel."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_message_date_range(channel_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed for get_message_date_range: {e}")
            raise

    def get_message_dates(self, channel_id: int) -> List[str]:
        """Get distinct message dates for a channel."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT DISTINCT DATE(created_at) as date FROM discord_messages WHERE channel_id = ? ORDER BY date",
                    (channel_id,)
                )
            )
            return [row.get('date') for row in result if row.get('date')]
        except Exception as e:
            logger.error(f"Supabase query failed for get_message_dates: {e}")
            return []

    def get_member(self, member_id: int) -> Optional[Dict]:
        """Fetch a member from the database by their ID."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_member(member_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def message_exists(self, message_id: int) -> bool:
        """Check if a message exists."""
        try:
            return self._run_async_in_thread(
                self.query_handler.message_exists(message_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def update_message(self, message: Dict) -> bool:
        """Update a message in Supabase."""
        try:
            stored = self._run_async_in_thread(
                self.storage_handler.store_messages_to_supabase([message])
            )
            return stored > 0
        except Exception as e:
            logger.error(f"Error updating message in Supabase: {e}", exc_info=True)
            return False

    def create_or_update_member(self, member_id: int, username: str, display_name: Optional[str] = None, 
                              global_name: Optional[str] = None, avatar_url: Optional[str] = None,
                              discriminator: Optional[str] = None, bot: bool = False, 
                              system: bool = False, accent_color: Optional[int] = None,
                              banner_url: Optional[str] = None, discord_created_at: Optional[str] = None,
                              guild_join_date: Optional[str] = None, role_ids: Optional[str] = None,
                              twitter_handle: Optional[str] = None, reddit_handle: Optional[str] = None,
                              include_in_updates: Optional[bool] = None,
                              allow_content_sharing: Optional[bool] = None) -> bool:
        """Create or update a member in Supabase.
        
        Permission fields (include_in_updates, allow_content_sharing) default to TRUE in the database.
        Only pass explicit values when the user has made a choice.
        """
        member_data: Dict[str, Any] = {
            'member_id': member_id,
            'username': username,
            'global_name': global_name,
            'server_nick': display_name,
            'avatar_url': avatar_url,
            'discriminator': discriminator,
            'bot': bot,
            'system': system,
            'accent_color': accent_color,
            'banner_url': banner_url,
            'discord_created_at': discord_created_at,
            'guild_join_date': guild_join_date,
            'role_ids': role_ids,
            'twitter_handle': twitter_handle,
            'reddit_handle': reddit_handle,
            'updated_at': datetime.now().isoformat()
        }

        # IMPORTANT: Do not send NULL for these fields during routine member syncs.
        # If we upsert NULL, we override the DB defaults (TRUE) and can also wipe
        # previously-set preferences. Only include these keys when the user has
        # explicitly made a choice (True/False).
        if include_in_updates is not None:
            member_data['include_in_updates'] = include_in_updates
        if allow_content_sharing is not None:
            member_data['allow_content_sharing'] = allow_content_sharing
        
        if self.storage_handler:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error storing member to Supabase: {e}", exc_info=True)
                return False
        return False

    def update_member_sharing_permission(self, member_id: int, allow_content_sharing: bool) -> bool:
        """Update member's content sharing permission in Supabase.
        
        Args:
            member_id: Discord member ID
            allow_content_sharing: Whether the user allows their content to be shared
            
        Returns:
            True if update succeeded, False otherwise
        """
        if self.storage_handler:
            try:
                member_data = {
                    'member_id': member_id,
                    'allow_content_sharing': allow_content_sharing,
                    'updated_at': datetime.now().isoformat()
                }
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error updating member sharing permission in Supabase: {e}", exc_info=True)
                return False
        return False
    
    def update_member_updates_permission(self, member_id: int, include_in_updates: bool) -> bool:
        """Update member's include in updates permission in Supabase.
        
        Args:
            member_id: Discord member ID
            include_in_updates: Whether the user allows being mentioned in summaries/digests
            
        Returns:
            True if update succeeded, False otherwise
        """
        if self.storage_handler:
            try:
                member_data = {
                    'member_id': member_id,
                    'include_in_updates': include_in_updates,
                    'updated_at': datetime.now().isoformat()
                }
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error updating member updates permission in Supabase: {e}", exc_info=True)
                return False
        return False

    # ========== Reaction Updates ==========

    def update_reactions(self, message_id: int, reaction_count: int, reactors: list) -> bool:
        """Update reaction data for a message via Supabase REST API.

        Bypasses execute_raw_sql() which cannot route UPDATE statements.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for update_reactions")
            return False

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_messages')
                .update({'reaction_count': reaction_count, 'reactors': reactors})
                .eq('message_id', message_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating reactions for message {message_id}: {e}")
            return False

    # ========== Message Content / Edit History ==========

    def update_message_content(self, message_id: int, new_content: Optional[str], new_edited_at: Optional[str]) -> Optional[bool]:
        """Update a message's content and append the previous version to edit_history.

        Reads the current row first to snapshot old content, then writes atomically
        via the REST API.  Skips the update if content has not actually changed.

        Args:
            message_id:    Discord message ID.
            new_content:   The edited content string from Discord.
            new_edited_at: ISO-format timestamp of the edit, or None.

        Returns:
            True  – row was updated successfully.
            False – message exists in DB but content is unchanged (no-op).
            None  – message not found in DB at all.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for update_message_content")
            return None

        client = self.storage_handler.supabase_client
        try:
            # 1. Read current state
            result = (
                client.table('discord_messages')
                .select('content,edited_at,edit_history')
                .eq('message_id', message_id)
                .execute()
            )
            if not result.data:
                logger.warning(f"update_message_content: message {message_id} not found in DB")
                return None

            row = result.data[0]
            old_content = row.get('content')
            old_edited_at = row.get('edited_at')

            # 2. Skip if nothing changed (e.g. embed-resolution triggers)
            if old_content == new_content:
                return False

            # 3. Build the new history array
            existing_history = row.get('edit_history') or []
            if not isinstance(existing_history, list):
                existing_history = []

            history_entry = {
                'content': old_content,
                'edited_at': old_edited_at,
                'recorded_at': datetime.now(timezone.utc).isoformat()
            }
            updated_history = existing_history + [history_entry]

            # 4. Write new content + updated history
            (
                client.table('discord_messages')
                .update({
                    'content': new_content,
                    'edited_at': new_edited_at,
                    'edit_history': updated_history,
                    'synced_at': datetime.now(timezone.utc).isoformat()
                })
                .eq('message_id', message_id)
                .execute()
            )
            logger.debug(f"update_message_content: updated message {message_id} (history depth now {len(updated_history)})")
            return True

        except Exception as e:
            logger.error(f"Error in update_message_content for message {message_id}: {e}", exc_info=True)
            return False

    # ========== Shared Posts Tracking ==========
    
    def record_shared_post(
        self, 
        discord_message_id: int, 
        discord_user_id: int, 
        platform: str, 
        platform_post_id: str,
        platform_post_url: Optional[str] = None,
        delete_eligible_hours: int = 6
    ) -> bool:
        """Record a shared post to enable deletion later.
        
        Args:
            discord_message_id: Original Discord message ID
            discord_user_id: Discord user ID of content author
            platform: Platform name (e.g., 'twitter')
            platform_post_id: ID of the post on the platform (e.g., tweet ID)
            platform_post_url: Full URL to the post
            delete_eligible_hours: Hours during which delete is allowed (default 6)
            
        Returns:
            True if recorded successfully
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for record_shared_post")
            return False
        
        try:
            from datetime import timedelta
            delete_eligible_until = (datetime.now() + timedelta(hours=delete_eligible_hours)).isoformat()
            
            data = {
                'discord_message_id': discord_message_id,
                'discord_user_id': discord_user_id,
                'platform': platform,
                'platform_post_id': platform_post_id,
                'platform_post_url': platform_post_url,
                'shared_at': datetime.now().isoformat(),
                'delete_eligible_until': delete_eligible_until
            }
            
            self.storage_handler.supabase_client.table('shared_posts').upsert(data).execute()
            logger.info(f"Recorded shared post: {platform} post {platform_post_id} for message {discord_message_id}")
            return True
        except Exception as e:
            logger.error(f"Error recording shared post: {e}", exc_info=True)
            return False
    
    def get_shared_post(self, discord_message_id: int, platform: str) -> Optional[Dict]:
        """Get a shared post record by Discord message ID and platform.
        
        Returns:
            Dict with post details or None if not found
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        
        try:
            result = (
                self.storage_handler.supabase_client.table('shared_posts')
                .select('*')
                .eq('discord_message_id', discord_message_id)
                .eq('platform', platform)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting shared post: {e}", exc_info=True)
            return None
    
    def mark_shared_post_deleted(self, discord_message_id: int, platform: str) -> bool:
        """Mark a shared post as deleted.
        
        Returns:
            True if updated successfully
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        
        try:
            (
                self.storage_handler.supabase_client.table('shared_posts')
                .update({'deleted_at': datetime.now().isoformat()})
                .eq('discord_message_id', discord_message_id)
                .eq('platform', platform)
                .execute()
            )
            logger.info(f"Marked {platform} post for message {discord_message_id} as deleted")
            return True
        except Exception as e:
            logger.error(f"Error marking shared post as deleted: {e}", exc_info=True)
            return False
    
    def mark_member_first_shared(self, member_id: int) -> bool:
        """Set first_shared_at timestamp for a member (only if not already set).
        
        Returns:
            True if this was their first share (timestamp was set), False otherwise
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        
        try:
            # Check if already shared
            result = (
                self.storage_handler.supabase_client.table('discord_members')
                .select('first_shared_at')
                .eq('member_id', member_id)
                .execute()
            )
            
            if result.data and result.data[0].get('first_shared_at'):
                # Already has first_shared_at set
                return False
            
            # Set first_shared_at
            (
                self.storage_handler.supabase_client.table('discord_members')
                .update({'first_shared_at': datetime.now().isoformat()})
                .eq('member_id', member_id)
                .execute()
            )
            logger.info(f"Marked first share for member {member_id}")
            return True
        except Exception as e:
            logger.error(f"Error marking member first shared: {e}", exc_info=True)
            return False

    def get_channel(self, channel_id: int) -> Optional[Dict]:
        """Get channel info by ID."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT * FROM discord_channels WHERE channel_id = ? LIMIT 1",
                    (channel_id,)
                )
            )
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Supabase query failed for get_channel: {e}")
            return None

    def create_or_update_channel(self, channel_id: int, channel_name: str, nsfw: bool = False, category_id: Optional[int] = None) -> bool:
        """Create or update a channel in Supabase."""
        channel_data = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'nsfw': nsfw,
            'category_id': category_id
        }
        
        if self.storage_handler:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_channels_to_supabase([channel_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error storing channel to Supabase: {e}", exc_info=True)
                return False
        return False

    def get_messages_after(self, date: datetime) -> List[Dict]:
        """Get messages after a certain date."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    """
                    SELECT m.*, 
                           COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                    FROM discord_messages m
                    JOIN discord_members mb ON m.author_id = mb.member_id
                    WHERE m.created_at > ?
                    """,
                    (date.isoformat(),)
                )
            )
            return result
        except Exception as e:
            logger.error(f"Supabase query failed for get_messages_after: {e}")
            return []

    def get_messages_by_ids(self, message_ids: List[int]) -> List[Dict]:
        """Get messages by their IDs."""
        return self._run_async_in_thread(
            self.query_handler.get_messages_by_ids(message_ids)
        )

    # ========== Timed Mutes ==========

    def set_is_speaker(self, member_id: int, is_speaker: bool) -> bool:
        """Update the is_speaker flag for a member."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for set_is_speaker")
            return False

        try:
            (
                self.storage_handler.supabase_client.table('discord_members')
                .update({'is_speaker': is_speaker})
                .eq('member_id', member_id)
                .execute()
            )
            logger.info(f"Set is_speaker={is_speaker} for member {member_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting is_speaker for member {member_id}: {e}", exc_info=True)
            return False

    def get_is_speaker(self, member_id: int) -> bool:
        """Check if a member should have the Speaker role. Returns True by default."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return True

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_members')
                .select('is_speaker')
                .eq('member_id', member_id)
                .execute()
            )
            if result.data:
                # Default to True if NULL
                return result.data[0].get('is_speaker') is not False
            return True
        except Exception as e:
            logger.error(f"Error getting is_speaker for member {member_id}: {e}", exc_info=True)
            return True

    def create_timed_mute(self, member_id: int, guild_id: int, mute_end_at: str, reason: Optional[str] = None, muted_by_id: Optional[int] = None) -> bool:
        """Upsert a timed mute record."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for create_timed_mute")
            return False

        try:
            data = {
                'member_id': member_id,
                'guild_id': guild_id,
                'mute_end_at': mute_end_at,
                'reason': reason,
                'muted_by_id': muted_by_id,
            }
            self.storage_handler.supabase_client.table('timed_mutes').upsert(data).execute()
            logger.info(f"Created timed mute for member {member_id} in guild {guild_id}, expires {mute_end_at}")
            return True
        except Exception as e:
            logger.error(f"Error creating timed mute for member {member_id}: {e}", exc_info=True)
            return False

    def get_expired_mutes(self) -> List[Dict]:
        """Get all timed mutes that have expired."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_expired_mutes")
            return []

        try:
            now = datetime.now(timezone.utc).isoformat()
            result = (
                self.storage_handler.supabase_client.table('timed_mutes')
                .select('*')
                .lte('mute_end_at', now)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching expired mutes: {e}", exc_info=True)
            return []

    def delete_timed_mute(self, member_id: int, guild_id: int) -> bool:
        """Delete a timed mute record."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for delete_timed_mute")
            return False

        try:
            (
                self.storage_handler.supabase_client.table('timed_mutes')
                .delete()
                .eq('member_id', member_id)
                .eq('guild_id', guild_id)
                .execute()
            )
            logger.info(f"Deleted timed mute for member {member_id} in guild {guild_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting timed mute for member {member_id}: {e}", exc_info=True)
            return False

    def get_messages_in_range(self, start_date: datetime, end_date: datetime, channel_id: Optional[int] = None) -> List[Dict]:
        """Get messages within a date range."""
        try:
            logger.debug(f"Querying messages in range from Supabase (channel_id={channel_id})")
            return self._run_async_in_thread(
                self.query_handler.get_messages_in_range(start_date, end_date, channel_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def get_messages_by_authors_in_range(self, author_ids: List[int], start_date: datetime, end_date: datetime) -> List[Dict]:
        """Get messages by specific authors in a date range."""
        try:
            logger.debug(f"Querying messages by authors from Supabase ({len(author_ids)} authors)")
            return self._run_async_in_thread(
                self.query_handler.get_messages_by_authors_in_range(author_ids, start_date, end_date)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise
