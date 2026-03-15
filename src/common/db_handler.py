import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
import asyncio

logger = logging.getLogger('DiscordBot')


def _emoji_to_str(emoji) -> str:
    """Convert a discord emoji to a string representation.

    Unicode emoji → char string, custom emoji → 'name:id'.
    """
    if hasattr(emoji, 'id') and emoji.id:
        return f"{emoji.name}:{emoji.id}"
    return str(emoji)


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

    def update_member_stored_avatar(self, member_id: int, stored_avatar_url: str) -> bool:
        """Save a permanent avatar URL for a member."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('discord_members').update({
                'stored_avatar_url': stored_avatar_url,
            }).eq('member_id', member_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating stored avatar for member {member_id}: {e}", exc_info=True)
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

    def add_reaction(self, message_id: int, user_id: int, emoji_str: str) -> bool:
        """Upsert a single row into discord_reactions."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for add_reaction")
            return False

        try:
            self.storage_handler.supabase_client.table('discord_reactions').upsert({
                'message_id': message_id,
                'user_id': user_id,
                'emoji': emoji_str,
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error adding reaction for message {message_id}: {e}")
            return False

    def remove_reaction(self, message_id: int, user_id: int, emoji_str: str) -> bool:
        """Delete a single row from discord_reactions."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for remove_reaction")
            return False

        try:
            self.storage_handler.supabase_client.table('discord_reactions') \
                .delete() \
                .eq('message_id', message_id) \
                .eq('user_id', user_id) \
                .eq('emoji', emoji_str) \
                .execute()
            return True
        except Exception as e:
            logger.error(f"Error removing reaction for message {message_id}: {e}")
            return False

    def upsert_reactions_batch(self, message_id: int, rows: list) -> bool:
        """Full-replace granular reactions for a message.

        Deletes all existing rows for the message, then inserts the new rows
        in batches of 100.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for upsert_reactions_batch")
            return False

        sb = self.storage_handler.supabase_client
        try:
            # Delete existing rows for this message
            sb.table('discord_reactions') \
                .delete() \
                .eq('message_id', message_id) \
                .execute()

            # Insert new rows in batches
            for i in range(0, len(rows), 100):
                batch = rows[i:i + 100]
                sb.table('discord_reactions').insert(batch).execute()

            return True
        except Exception as e:
            logger.error(f"Error upserting reaction batch for message {message_id}: {e}")
            return False

    def bulk_upsert_reactions(self, message_ids: list, rows: list) -> bool:
        """Bulk-replace reactions for multiple messages at once.

        Deletes all existing rows for the given message_ids, then inserts all
        new rows in batches. Much more efficient than per-message upsert for
        backfill scenarios.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for bulk_upsert_reactions")
            return False

        sb = self.storage_handler.supabase_client
        try:
            # Delete existing rows for all message_ids (in_ has a practical limit)
            for i in range(0, len(message_ids), 100):
                batch_ids = message_ids[i:i + 100]
                sb.table('discord_reactions') \
                    .delete() \
                    .in_('message_id', batch_ids) \
                    .execute()

            # Insert all new rows in batches
            for i in range(0, len(rows), 500):
                batch = rows[i:i + 500]
                sb.table('discord_reactions').insert(batch).execute()

            return True
        except Exception as e:
            logger.error(f"Error in bulk_upsert_reactions: {e}")
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

    def get_muted_member_ids(self) -> list[int]:
        """Return member IDs where is_speaker is explicitly False."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_members')
                .select('member_id')
                .eq('is_speaker', False)
                .execute()
            )
            return [row['member_id'] for row in (result.data or [])]
        except Exception as e:
            logger.error(f"Error fetching muted member IDs: {e}", exc_info=True)
            return []

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

    # ========== Channel Speaker Modes ==========

    def get_all_channel_speaker_modes(self) -> Dict[int, str]:
        """Bulk fetch speaker_mode for all channels.

        Returns:
            Dict mapping channel_id (int) -> speaker_mode string.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_all_channel_speaker_modes")
            return {}

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_channels')
                .select('channel_id,speaker_mode')
                .execute()
            )
            return {
                row['channel_id']: row.get('speaker_mode', 'normal')
                for row in (result.data or [])
            }
        except Exception as e:
            logger.error(f"Error fetching channel speaker modes: {e}", exc_info=True)
            return {}

    def set_channel_speaker_mode(self, channel_id: int, mode: str) -> bool:
        """Update the speaker_mode for a single channel.

        Args:
            channel_id: Discord channel ID.
            mode: One of 'normal', 'readonly', 'exempt'.

        Returns:
            True if update succeeded.
        """
        if mode not in ('normal', 'readonly', 'exempt'):
            logger.error(f"Invalid speaker_mode '{mode}' for channel {channel_id}")
            return False

        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for set_channel_speaker_mode")
            return False

        try:
            (
                self.storage_handler.supabase_client.table('discord_channels')
                .update({'speaker_mode': mode})
                .eq('channel_id', channel_id)
                .execute()
            )
            logger.info(f"Set speaker_mode='{mode}' for channel {channel_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting speaker_mode for channel {channel_id}: {e}", exc_info=True)
            return False

    def ensure_channel_exists(self, channel_id: int, channel_name: str,
                              category_id: Optional[int] = None, nsfw: bool = False) -> bool:
        """Upsert a channel row without overwriting speaker_mode.

        If the channel already exists, only channel_name/category_id/nsfw are refreshed.
        If it doesn't exist, it's created with speaker_mode defaulting to 'normal' (DB default).

        Returns:
            True if upsert succeeded.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for ensure_channel_exists")
            return False

        try:
            data = {
                'channel_id': channel_id,
                'channel_name': channel_name,
                'category_id': category_id,
                'nsfw': nsfw,
            }
            (
                self.storage_handler.supabase_client.table('discord_channels')
                .upsert(data)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error in ensure_channel_exists for channel {channel_id}: {e}", exc_info=True)
            return False

    # ========== Onboarding Defaults ==========

    def get_onboarding_default_ids(self) -> List[int]:
        """Return channel IDs where onboarding_default is True."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_channels')
                .select('channel_id')
                .eq('onboarding_default', True)
                .execute()
            )
            return [row['channel_id'] for row in (result.data or [])]
        except Exception as e:
            logger.error(f"Error fetching onboarding default IDs: {e}", exc_info=True)
            return []

    # ========== Pending Intros (Gated Entry) ==========

    def create_pending_intro(self, member_id: int, message_id: int, channel_id: int) -> bool:
        """Insert a new pending intro record."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('pending_intros').insert({
                'member_id': member_id,
                'message_id': message_id,
                'channel_id': channel_id,
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating pending intro for member {member_id}: {e}", exc_info=True)
            return False

    def get_pending_intro_by_member(self, member_id: int) -> Optional[Dict]:
        """Return the latest pending intro for a member, or None."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('member_id', member_id)
                .eq('status', 'pending')
                .order('created_at', desc=True)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching pending intro for member {member_id}: {e}", exc_info=True)
            return None

    def get_pending_intro_by_message(self, message_id: int) -> Optional[Dict]:
        """Lookup a pending intro by message ID."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('message_id', message_id)
                .eq('status', 'pending')
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching pending intro for message {message_id}: {e}", exc_info=True)
            return None

    def approve_pending_intro(self, message_id: int) -> bool:
        """Mark a pending intro as approved."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('status', 'pending').execute()
            return True
        except Exception as e:
            logger.error(f"Error approving pending intro for message {message_id}: {e}", exc_info=True)
            return False

    def expire_pending_intro(self, message_id: int) -> bool:
        """Mark a pending intro as expired."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('pending_intros').update({
                'status': 'expired',
                'expired_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('status', 'pending').execute()
            return True
        except Exception as e:
            logger.error(f"Error expiring pending intro for message {message_id}: {e}", exc_info=True)
            return False

    def get_expired_pending_intros(self, expiry_days: int = 7) -> List[Dict]:
        """Return pending intros older than expiry_days."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=expiry_days)).isoformat()
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('status', 'pending')
                .lt('created_at', cutoff)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching expired pending intros: {e}", exc_info=True)
            return []

    def get_all_pending_intros(self) -> List[Dict]:
        """Return all pending intros (for bot restart recovery)."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('status', 'pending')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching all pending intros: {e}", exc_info=True)
            return []

    def record_intro_vote(self, intro_id: int, message_id: int, voter_id: int, voter_role: str) -> bool:
        """Record a vote on an intro. Returns False if already voted."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('intro_votes').insert({
                'intro_id': intro_id,
                'message_id': message_id,
                'voter_id': voter_id,
                'voter_role': voter_role,
            }).execute()
            return True
        except Exception as e:
            # Unique constraint violation means already voted
            if 'duplicate' in str(e).lower() or '23505' in str(e):
                return False
            logger.error(f"Error recording intro vote: {e}", exc_info=True)
            return False

    # ========== Grant Applications ==========

    def create_grant_application(self, thread_id: int, applicant_id: int, thread_content: str,
                                 attachment_urls: list | None = None) -> bool:
        """Insert a new grant application record."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {
                'thread_id': thread_id,
                'applicant_id': applicant_id,
                'thread_content': thread_content,
                'status': 'reviewing',
            }
            if attachment_urls:
                data['attachment_urls'] = attachment_urls
            self.storage_handler.supabase_client.table('grant_applications').insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating grant application for thread {thread_id}: {e}", exc_info=True)
            return False

    def get_grant_by_thread(self, thread_id: int) -> Optional[Dict]:
        """Return the grant application for a thread, or None."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .eq('thread_id', thread_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching grant for thread {thread_id}: {e}", exc_info=True)
            return None

    def update_grant_status(self, thread_id: int, status: str, **kwargs) -> bool:
        """Update a grant application's status and any additional fields."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {'status': status}
            # Handle timestamp fields that use 'now()'
            for key, value in kwargs.items():
                if value == 'now()':
                    data[key] = datetime.now(timezone.utc).isoformat()
                else:
                    data[key] = value
            (
                self.storage_handler.supabase_client.table('grant_applications')
                .update(data)
                .eq('thread_id', thread_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error updating grant status for thread {thread_id}: {e}", exc_info=True)
            return False

    def record_grant_payment(self, thread_id: int, tx_signature: str, sol_amount: float, sol_price_usd: float) -> bool:
        """Record a successful grant payment."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('grant_applications')
                .update({
                    'status': 'paid',
                    'payment_status': 'confirmed',
                    'tx_signature': tx_signature,
                    'sol_amount': sol_amount,
                    'sol_price_usd': sol_price_usd,
                    'paid_at': datetime.now(timezone.utc).isoformat(),
                })
                .eq('thread_id', thread_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error recording grant payment for thread {thread_id}: {e}", exc_info=True)
            return False

    def get_inflight_payments(self) -> List[Dict]:
        """Return grants where payment needs recovery: in-flight or pending retry."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .in_('payment_status', ['sending', 'sent', 'retry'])
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching inflight payments: {e}", exc_info=True)
            return []

    def get_member_engagement(self, member_id: int) -> Dict:
        """Get engagement stats for a member: total message count and last 20 messages >50 chars."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return {'total_messages': 0, 'recent_messages': []}
        try:
            sb = self.storage_handler.supabase_client
            # Total message count
            count_resp = sb.table('discord_messages').select('message_id', count='exact').eq('author_id', member_id).execute()
            total = count_resp.count or 0
            # Last 20 substantive messages (>50 chars)
            msgs_resp = (
                sb.table('discord_messages')
                .select('content,channel_id,created_at')
                .eq('author_id', member_id)
                .gt('content', '')  # non-empty
                .order('created_at', desc=True)
                .limit(100)
                .execute()
            )
            # Filter to >50 chars client-side (Supabase REST can't filter by length)
            substantive = [
                {'content': m['content'][:200], 'channel_id': m['channel_id'], 'created_at': m['created_at'][:10]}
                for m in (msgs_resp.data or [])
                if m.get('content') and len(m['content']) > 50
            ][:20]
            return {'total_messages': total, 'recent_messages': substantive}
        except Exception as e:
            logger.error(f"Error fetching engagement for member {member_id}: {e}", exc_info=True)
            return {'total_messages': 0, 'recent_messages': []}

    def get_active_grants_for_applicant(self, applicant_id: int) -> List[Dict]:
        """Return active (non-terminal) grant applications for a user."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .eq('applicant_id', applicant_id)
                .in_('status', ['reviewing', 'awaiting_wallet'])
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching active grants for applicant {applicant_id}: {e}", exc_info=True)
            return []

    def get_grant_history_for_applicant(self, applicant_id: int) -> List[Dict]:
        """Return all past grant applications for a user (any status)."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('thread_id,status,gpu_type,recommended_hours,total_cost_usd,created_at,paid_at')
                .eq('applicant_id', applicant_id)
                .order('created_at', desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching grant history for applicant {applicant_id}: {e}", exc_info=True)
            return []

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

    # ------------------------------------------------------------------
    # Competitions
    # ------------------------------------------------------------------

    def upsert_competition(self, data: Dict) -> bool:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('competitions').upsert(
                data, on_conflict='slug'
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting competition: {e}", exc_info=True)
            return False

    def get_competition(self, slug: str) -> Optional[Dict]:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*').eq('slug', slug).execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching competition {slug}: {e}", exc_info=True)
            return None

    def get_active_competitions(self) -> List[Dict]:
        """Return competitions with status 'voting'."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*').eq('status', 'voting').execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching active competitions: {e}", exc_info=True)
            return []

    def get_scheduled_competitions(self) -> List[Dict]:
        """Return competitions in 'setup' status that have a voting_starts_at set."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*')
                .eq('status', 'setup')
                .not_.is_('voting_starts_at', 'null')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching scheduled competitions: {e}", exc_info=True)
            return []

    def update_competition(self, slug: str, data: Dict) -> bool:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competitions')
                .update(data).eq('slug', slug).execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error updating competition {slug}: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Competition entries
    # ------------------------------------------------------------------

    def upsert_competition_entry(self, entry: Dict) -> bool:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('competition_entries').upsert(
                entry, on_conflict='competition_slug,message_id'
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting competition entry: {e}", exc_info=True)
            return False

    def get_competition_entries(self, slug: str) -> List[Dict]:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('competition_entries')
                .select('*')
                .eq('competition_slug', slug)
                .order('created_at')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching competition entries for {slug}: {e}", exc_info=True)
            return []

    def delete_competition_entry(self, slug: str, message_id: int) -> bool:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competition_entries')
                .delete().eq('competition_slug', slug).eq('message_id', message_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error deleting competition entry {message_id}: {e}", exc_info=True)
            return False

    def clear_competition_entries(self, slug: str) -> bool:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competition_entries')
                .delete().eq('competition_slug', slug).execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error clearing competition entries for {slug}: {e}", exc_info=True)
            return False
