import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path
from .constants import get_database_path, get_storage_backend, STORAGE_SQLITE, STORAGE_BOTH, STORAGE_SUPABASE
import time
import threading
import asyncio
import queue

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
        """Initialize database path and ensure directory exists."""
        try:
            self.db_path = db_path if db_path else get_database_path(dev_mode)
            self.dev_mode = dev_mode
            
            # Initialize storage backend configuration
            self.storage_backend = storage_backend or get_storage_backend()
            logger.debug(f"DatabaseHandler using storage backend: {self.storage_backend}")
            
            # Initialize storage handler for Supabase writes if needed
            self.storage_handler = None
            self.query_handler = None
            if self.storage_backend in ['supabase', 'both']:
                try:
                    from .storage_handler import StorageHandler
                    from .supabase_query_handler import SupabaseQueryHandler
                    self.storage_handler = StorageHandler(self.storage_backend)
                    # Use the same Supabase client for queries
                    self.query_handler = SupabaseQueryHandler(self.storage_handler.supabase_client)
                    logger.debug(f"Supabase query handler initialized for read operations")
                except Exception as e:
                    logger.error(f"Failed to initialize Supabase handlers: {e}", exc_info=True)
                    if self.storage_backend == 'supabase':
                        # If only Supabase was requested and it fails, raise error
                        raise
                    # If 'both' was requested, continue with SQLite only
                    logger.warning("Continuing with SQLite only due to Supabase initialization failure")
                    self.storage_backend = STORAGE_SQLITE
            
            # Only initialize SQLite if needed
            if self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]:
                db_dir = Path(self.db_path).parent
                db_dir.mkdir(parents=True, exist_ok=True)
                
                self.connection_pool = queue.Queue(maxsize=pool_size)
                for _ in range(pool_size):
                    conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    # Optimize SQLite for better performance
                    conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
                    conn.execute("PRAGMA synchronous=NORMAL")  # Balance between safety and speed
                    conn.execute("PRAGMA cache_size=10000")  # Increase cache size
                    conn.execute("PRAGMA temp_store=MEMORY")  # Store temp data in memory
                    self.connection_pool.put(conn)
                
                self.write_lock = threading.Lock()
                self._init_db()
            else:
                # Supabase-only mode - no SQLite connection pool needed
                self.connection_pool = None
                self.write_lock = None
                logger.info("Running in Supabase-only mode - SQLite not initialized")
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def _run_async_in_thread(self, coro):
        """Helper to run async operations from sync context."""
        try:
            # Check if we're already in an async context
            try:
                loop = asyncio.get_running_loop()
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
    
    def _should_use_supabase_for_reads(self) -> bool:
        """
        Determine if we should use Supabase for read operations.
        
        Priority:
        - 'supabase' mode: Always use Supabase
        - 'both' mode: Use Supabase (faster, more scalable)
        - 'sqlite' mode: Use SQLite only
        """
        return self.query_handler is not None and self.storage_backend in ['supabase', 'both']

    def _get_connection_from_pool(self):
        if not self.connection_pool:
            raise RuntimeError("SQLite not initialized - running in Supabase-only mode")
        try:
            conn = self.connection_pool.get(timeout=5)
            if self.dev_mode:
                logger.debug(f"Retrieved connection from pool, remaining connections: {self.connection_pool.qsize()}")
            return conn
        except queue.Empty:
            logger.warning('Connection pool exhausted. Creating a new connection.')
            return sqlite3.connect(self.db_path, check_same_thread=False)

    def _return_connection_to_pool(self, conn):
        if self.connection_pool:
            self.connection_pool.put(conn)

    def close(self):
        """Close the database connection."""
        if self.connection_pool:
            while not self.connection_pool.empty():
                conn = self.connection_pool.get()
                conn.close()

    def __del__(self):
        """Ensure connection is closed when object is destroyed."""
        self.close()

    def _execute_with_retry(self, operation, max_retries=5, initial_delay=0.2):
        """Execute a database operation with retry logic."""
        last_error = None
        for attempt in range(max_retries):
            conn = self._get_connection_from_pool()
            try:
                result = operation(conn)
                conn.commit()
                return result
            except sqlite3.OperationalError as e:
                last_error = e
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.warning(f"Database locked, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise
            finally:
                self._return_connection_to_pool(conn)
        if last_error:
            raise last_error
        raise Exception("Maximum retries exceeded")

    def _init_db(self):
        """Initialize all database tables."""
        def init_operation(conn):
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id BIGINT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    description TEXT,
                    suitable_posts TEXT,
                    unsuitable_posts TEXT,
                    rules TEXT,
                    setup_complete BOOLEAN DEFAULT FALSE,
                    nsfw BOOLEAN DEFAULT FALSE,
                    enriched BOOLEAN DEFAULT FALSE,
                    category_id BIGINT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channel_summary (
                    channel_id BIGINT PRIMARY KEY,
                    summary_thread_id BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    daily_summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    channel_id BIGINT NOT NULL REFERENCES channels(channel_id),
                    full_summary TEXT,
                    short_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, channel_id) ON CONFLICT REPLACE
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    member_id BIGINT PRIMARY KEY,
                    username TEXT NOT NULL,
                    global_name TEXT,
                    server_nick TEXT,
                    avatar_url TEXT,
                    discriminator TEXT,
                    bot BOOLEAN DEFAULT FALSE,
                    system BOOLEAN DEFAULT FALSE,
                    accent_color INTEGER,
                    banner_url TEXT,
                    discord_created_at TEXT,
                    guild_join_date TEXT,
                    role_ids TEXT,
                    twitter_handle TEXT,
                    instagram_handle TEXT,
                    youtube_handle TEXT,
                    tiktok_handle TEXT,
                    website TEXT,
                    sharing_consent BOOLEAN DEFAULT FALSE,
                    dm_preference BOOLEAN DEFAULT TRUE,
                    permission_to_curate BOOLEAN DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id BIGINT PRIMARY KEY,
                    channel_id BIGINT,
                    author_id BIGINT,
                    content TEXT,
                    created_at TEXT,
                    attachments TEXT,
                    embeds TEXT,
                    reaction_count INTEGER DEFAULT 0,
                    reactors TEXT,
                    reference_id BIGINT,
                    edited_at TEXT,
                    is_pinned BOOLEAN,
                    thread_id BIGINT,
                    message_type TEXT,
                    flags INTEGER,
                    is_deleted BOOLEAN DEFAULT FALSE,
                    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content,
                    content='messages',
                    content_rowid='message_id'
                )
            """)
            
            self._create_indexes(cursor)
            
            cursor.close()
        
        self._execute_with_retry(lambda conn: init_operation(conn))

    def _create_indexes(self, cursor):
        """Create all necessary indexes."""
        indexes = [
            ("idx_channel_id", "messages(channel_id)"),
            ("idx_created_at", "messages(created_at)"),
            ("idx_author_id", "messages(author_id)"),
            ("idx_reference_id", "messages(reference_id)"),
            ("idx_daily_summaries_date", "daily_summaries(date)"),
            ("idx_daily_summaries_channel", "daily_summaries(channel_id)"),
            ("idx_members_username", "members(username)")
        ]
        
        for index_name, index_def in indexes:
            try:
                cursor.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {index_def}")
            except sqlite3.Error as e:
                logger.error(f"Error creating index {index_name}: {e}")

    def execute_query(self, query: str, params: tuple = ()) -> List[dict]:
        """
        Execute a raw SQL query. Routes to Supabase if configured.
        SQLite SQL will be automatically translated to PostgreSQL.
        """
        # Use Supabase for query execution if configured
        if self._should_use_supabase_for_reads():
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
                if self.storage_backend == 'supabase':
                    raise  # No fallback in supabase-only mode
                logger.warning("⚠️ [DB HANDLER] Falling back to SQLite for query execution")
        
        # SQLite query
        logger.info(f"🔄 [DB HANDLER] Routing query to SQLITE")
        logger.info(f"🔄 [DB HANDLER] Query preview: {query[:200]}")
        def query_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        
        result = self._execute_with_retry(query_operation)
        logger.info(f"✅ [DB HANDLER] SQLite returned {len(result)} results")
        return result

    def _store_messages(self, messages: List[Dict]):
        # Only log in dev mode and for larger batches
        if self.dev_mode and len(messages) > 1:
            logger.debug(f"Starting to store {len(messages)} messages")
        
        # Store to SQLite if configured
        if self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]:
            with self.write_lock:
                def store_operation(conn):
                    cursor = conn.cursor()
                    for msg in messages:
                        cursor.execute("""
                            INSERT OR REPLACE INTO messages (
                                message_id, channel_id, author_id, content, created_at,
                                attachments, embeds, reaction_count, reactors, reference_id,
                                edited_at, is_pinned, thread_id, message_type, flags, is_deleted
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            msg.get('message_id'), msg.get('channel_id'), msg.get('author_id'),
                            msg.get('content'), msg.get('created_at'), json.dumps(msg.get('attachments')),
                            json.dumps(msg.get('embeds')), msg.get('reaction_count'), json.dumps(msg.get('reactors')),
                            msg.get('reference_id'), msg.get('edited_at'), msg.get('is_pinned'),
                            msg.get('thread_id'), msg.get('message_type'), msg.get('flags'),
                            msg.get('is_deleted', False)
                        ))
                        
                        if msg.get('content'):
                            cursor.execute("""
                                INSERT OR REPLACE INTO messages_fts (rowid, content) VALUES (?, ?)
                            """, (msg.get('message_id'), msg.get('content')))
                    cursor.close()
                self._execute_with_retry(store_operation)

    async def store_messages(self, messages: List[Dict]):
        """Store messages to configured backend(s)."""
        # Store to SQLite if configured
        if self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]:
            await asyncio.to_thread(self._store_messages, messages)
        
        # Store to Supabase if configured
        if self.storage_handler and self.storage_backend in ['supabase', 'both']:
            await self.storage_handler.store_messages_to_supabase(messages)

    def get_last_message_id(self, channel_id: int) -> Optional[int]:
        """Get the most recent message ID for a channel. Routes to Supabase if configured."""
        if self._should_use_supabase_for_reads():
            try:
                return self._run_async_in_thread(
                    self.query_handler.get_last_message_id(channel_id)
                )
            except Exception as e:
                logger.error(f"Supabase query failed, falling back to SQLite: {e}")
                if self.storage_backend == 'supabase':
                    raise
        
        # SQLite query
        def get_last_message_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(message_id) FROM messages WHERE channel_id = ?", (channel_id,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result and result[0] else None
        return self._execute_with_retry(get_last_message_operation)

    def search_messages(self, query: str, channel_id: Optional[int] = None) -> List[Dict]:
        def search_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            sql_query = """
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM messages m 
                JOIN messages_fts fts ON m.message_id = fts.rowid
                JOIN members mb ON m.author_id = mb.member_id
                WHERE fts.content MATCH ?
            """
            params = [query]
            if channel_id:
                sql_query += " AND m.channel_id = ?"
                params.append(channel_id)
            cursor.execute(sql_query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(search_operation)

    def store_daily_summary(self, channel_id: int, full_summary: Optional[str], short_summary: Optional[str], date: Optional[datetime] = None) -> bool:
        def summary_operation(conn):
            cursor = conn.cursor()
            summary_date = date.strftime('%Y-%m-%d') if date else datetime.now().strftime('%Y-%m-%d')
            cursor.execute("""
                INSERT INTO daily_summaries (date, channel_id, full_summary, short_summary)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date, channel_id) DO UPDATE SET
                full_summary = excluded.full_summary,
                short_summary = excluded.short_summary,
                created_at = CURRENT_TIMESTAMP
            """, (summary_date, channel_id, full_summary, short_summary))
            cursor.close()
            return cursor.rowcount > 0
        return self._execute_with_retry(summary_operation)

    def get_summary_thread_id(self, channel_id: int) -> Optional[int]:
        def get_thread_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT summary_thread_id FROM channel_summary WHERE channel_id = ? ORDER BY created_at DESC LIMIT 1", (channel_id,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else None
        return self._execute_with_retry(get_thread_operation)

    def update_summary_thread(self, channel_id: int, thread_id: Optional[int]):
        def update_thread_operation(conn):
            cursor = conn.cursor()
            if thread_id:
                cursor.execute("""
                    INSERT INTO channel_summary (channel_id, summary_thread_id, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(channel_id) DO UPDATE SET
                    summary_thread_id = excluded.summary_thread_id,
                    updated_at = CURRENT_TIMESTAMP
                """, (channel_id, thread_id))
            else:
                cursor.execute("DELETE FROM channel_summary WHERE channel_id = ?", (channel_id,))
            cursor.close()
        self._execute_with_retry(update_thread_operation)

    def get_all_message_ids(self, channel_id: int) -> List[int]:
        def get_ids_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT message_id FROM messages WHERE channel_id = ?", (channel_id,))
            ids = [row[0] for row in cursor.fetchall()]
            cursor.close()
            return ids
        return self._execute_with_retry(get_ids_operation)

    def get_message_date_range(self, channel_id: int) -> Tuple[Optional[datetime], Optional[datetime]]:
        def get_range_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(created_at), MAX(created_at) FROM messages WHERE channel_id = ?", (channel_id,))
            result = cursor.fetchone()
            cursor.close()
            if result and result[0] and result[1]:
                return (to_aware_utc(result[0]), to_aware_utc(result[1]))
            return (None, None)
        return self._execute_with_retry(get_range_operation)

    def get_message_dates(self, channel_id: int) -> List[str]:
        def get_dates_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT strftime('%Y-%m-%d', created_at) FROM messages WHERE channel_id = ? ORDER BY 1", (channel_id,))
            dates = [row[0] for row in cursor.fetchall()]
            cursor.close()
            return dates
        return self._execute_with_retry(get_dates_operation)

    def get_member(self, member_id: int) -> Optional[Dict]:
        """Fetch a member from the database by their ID. Routes to Supabase if configured."""
        if self._should_use_supabase_for_reads():
            try:
                return self._run_async_in_thread(
                    self.query_handler.get_member(member_id)
                )
            except Exception as e:
                logger.error(f"Supabase query failed, falling back to SQLite: {e}")
                if self.storage_backend == 'supabase':
                    raise
        
        # SQLite query
        def get_member_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM members WHERE member_id = ?", (member_id,))
            result = cursor.fetchone()
            cursor.close()
            return dict(result) if result else None
        return self._execute_with_retry(get_member_operation)

    def message_exists(self, message_id: int) -> bool:
        """Check if a message exists. Routes to Supabase if configured."""
        if self._should_use_supabase_for_reads():
            try:
                return self._run_async_in_thread(
                    self.query_handler.message_exists(message_id)
                )
            except Exception as e:
                logger.error(f"Supabase query failed, falling back to SQLite: {e}")
                if self.storage_backend == 'supabase':
                    raise
        
        # SQLite query
        def check_message_operation(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,))
            result = cursor.fetchone()
            cursor.close()
            return result is not None
        return self._execute_with_retry(check_message_operation)

    def update_message(self, message: Dict) -> bool:
        def update_operation(conn):
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE messages SET
                    content = ?,
                    edited_at = ?,
                    reaction_count = ?,
                    reactors = ?,
                    is_pinned = ?,
                    is_deleted = ?
                WHERE message_id = ?
            """, (
                message.get('content'), message.get('edited_at'),
                message.get('reaction_count'), json.dumps(message.get('reactors')),
                message.get('is_pinned'), message.get('is_deleted', False),
                message.get('message_id')
            ))
            cursor.close()
            return cursor.rowcount > 0
        return self._execute_with_retry(update_operation)

    def create_or_update_member(self, member_id: int, username: str, display_name: Optional[str] = None, 
                              global_name: Optional[str] = None, avatar_url: Optional[str] = None,
                              discriminator: Optional[str] = None, bot: bool = False, 
                              system: bool = False, accent_color: Optional[int] = None,
                              banner_url: Optional[str] = None, discord_created_at: Optional[str] = None,
                              guild_join_date: Optional[str] = None, role_ids: Optional[str] = None,
                              twitter_handle: Optional[str] = None, instagram_handle: Optional[str] = None,
                              youtube_handle: Optional[str] = None, tiktok_handle: Optional[str] = None,
                              website: Optional[str] = None, sharing_consent: Optional[bool] = None,
                              dm_preference: Optional[bool] = None, permission_to_curate: Optional[bool] = None) -> bool:
        # Prepare member data dict for potential Supabase storage
        member_data = {
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
            'instagram_handle': instagram_handle,
            'youtube_handle': youtube_handle,
            'tiktok_handle': tiktok_handle,
            'website': website,
            'sharing_consent': sharing_consent,
            'dm_preference': dm_preference,
            'permission_to_curate': permission_to_curate,
            'updated_at': datetime.now().isoformat()
        }
        
        result = False
        
        # Store to SQLite if configured
        if self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]:
            def member_operation(conn):
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM members WHERE member_id = ?", (member_id,))
                existing_member = cursor.fetchone()
                
                if existing_member:
                    update_fields = {
                        'username': username, 'global_name': global_name, 'server_nick': display_name, 
                        'avatar_url': avatar_url, 'discriminator': discriminator, 'bot': bot, 'system': system,
                        'accent_color': accent_color, 'banner_url': banner_url, 
                        'discord_created_at': discord_created_at, 'guild_join_date': guild_join_date,
                        'role_ids': role_ids, 'twitter_handle': twitter_handle, 'instagram_handle': instagram_handle,
                        'youtube_handle': youtube_handle, 'tiktok_handle': tiktok_handle, 'website': website,
                        'sharing_consent': sharing_consent, 'dm_preference': dm_preference, 
                        'permission_to_curate': permission_to_curate, 'updated_at': datetime.now().isoformat()
                    }
                    
                    set_clauses = []
                    params = []
                    for key, value in update_fields.items():
                        if value is not None:
                            set_clauses.append(f"{key} = ?")
                            params.append(value)
                    params.append(member_id)
                    
                    if set_clauses:
                        cursor.execute(f"UPDATE members SET {', '.join(set_clauses)} WHERE member_id = ?", tuple(params))
                else:
                    cursor.execute("""
                        INSERT INTO members (member_id, username, global_name, server_nick, avatar_url, discriminator, bot, system, accent_color, banner_url, discord_created_at, guild_join_date, role_ids, twitter_handle, instagram_handle, youtube_handle, tiktok_handle, website, sharing_consent, dm_preference, permission_to_curate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        member_id, username, global_name, display_name, avatar_url, discriminator, bot, system,
                        accent_color, banner_url, discord_created_at, guild_join_date, role_ids, twitter_handle,
                        instagram_handle, youtube_handle, tiktok_handle, website, sharing_consent, dm_preference, permission_to_curate
                    ))
                cursor.close()
                return cursor.rowcount > 0
            result = self._execute_with_retry(member_operation)
        
        # Store to Supabase if configured (run async operation in thread)
        if self.storage_handler and self.storage_backend in ['supabase', 'both']:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                result = result or (stored > 0)
            except Exception as e:
                logger.error(f"Error storing member to Supabase: {e}", exc_info=True)
        
        return result

    def update_member_permission_status(self, member_id: int, permission_status: Optional[bool]) -> bool:
        def update_permission_operation(conn):
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE members SET permission_to_curate = ?, updated_at = CURRENT_TIMESTAMP WHERE member_id = ?
            """, (permission_status, member_id))
            cursor.close()
            return cursor.rowcount > 0
        return self._execute_with_retry(update_permission_operation)

    def get_channel(self, channel_id: int) -> Optional[Dict]:
        def get_channel_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,))
            result = cursor.fetchone()
            cursor.close()
            return dict(result) if result else None
        return self._execute_with_retry(get_channel_operation)

    def create_or_update_channel(self, channel_id: int, channel_name: str, nsfw: bool = False, category_id: Optional[int] = None) -> bool:
        # Prepare channel data for potential Supabase storage
        channel_data = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'nsfw': nsfw,
            'category_id': category_id
        }
        
        result = False
        
        # Store to SQLite if configured
        if self.storage_backend in [STORAGE_SQLITE, STORAGE_BOTH]:
            def channel_operation(conn):
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,))
                exists = cursor.fetchone()
                if exists:
                    cursor.execute("UPDATE channels SET channel_name = ?, nsfw = ?, category_id = ? WHERE channel_id = ?", (channel_name, nsfw, category_id, channel_id))
                else:
                    cursor.execute("INSERT INTO channels (channel_id, channel_name, nsfw, category_id) VALUES (?, ?, ?, ?)", (channel_id, channel_name, nsfw, category_id))
                cursor.close()
                return cursor.rowcount > 0
            result = self._execute_with_retry(channel_operation)
        
        # Store to Supabase if configured (run async operation in thread)
        if self.storage_handler and self.storage_backend in ['supabase', 'both']:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_channels_to_supabase([channel_data])
                )
                result = result or (stored > 0)
            except Exception as e:
                logger.error(f"Error storing channel to Supabase: {e}", exc_info=True)
        
        return result

    def get_messages_after(self, date: datetime) -> List[Dict]:
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM messages m
                JOIN members mb ON m.author_id = mb.member_id
                WHERE m.created_at > ?
            """, (date.isoformat(),))
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)

    def get_messages_by_ids(self, message_ids: List[int]) -> List[Dict]:
        # Route to Supabase if configured
        if self._should_use_supabase_for_reads():
            return self._run_async_in_thread(
                self.query_handler.get_messages_by_ids(message_ids)
            )
        
        # Otherwise use SQLite
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in message_ids)
            cursor.execute(f"""
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM messages m
                JOIN members mb ON m.author_id = mb.member_id
                WHERE m.message_id IN ({placeholders})
            """, message_ids)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)

    def get_messages_in_range(self, start_date: datetime, end_date: datetime, channel_id: Optional[int] = None) -> List[Dict]:
        """Get messages within a date range. Routes to Supabase if configured."""
        if self._should_use_supabase_for_reads():
            try:
                logger.debug(f"Querying messages in range from Supabase (channel_id={channel_id})")
                return self._run_async_in_thread(
                    self.query_handler.get_messages_in_range(start_date, end_date, channel_id)
                )
            except Exception as e:
                logger.error(f"Supabase query failed, falling back to SQLite: {e}")
                if self.storage_backend == 'supabase':
                    raise  # No fallback in supabase-only mode
        
        # SQLite query
        def get_range_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM messages m
                JOIN members mb ON m.author_id = mb.member_id
                WHERE m.created_at BETWEEN ? AND ?
            """
            params = [start_date.isoformat(), end_date.isoformat()]
            if channel_id:
                query += " AND m.channel_id = ?"
                params.append(channel_id)
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_range_operation)

    def get_messages_by_authors_in_range(self, author_ids: List[int], start_date: datetime, end_date: datetime) -> List[Dict]:
        """Get messages by specific authors in a date range. Routes to Supabase if configured."""
        if self._should_use_supabase_for_reads():
            try:
                logger.debug(f"Querying messages by authors from Supabase ({len(author_ids)} authors)")
                return self._run_async_in_thread(
                    self.query_handler.get_messages_by_authors_in_range(author_ids, start_date, end_date)
                )
            except Exception as e:
                logger.error(f"Supabase query failed, falling back to SQLite: {e}")
                if self.storage_backend == 'supabase':
                    raise
        
        # SQLite query
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in author_ids)
            query = f"""
                SELECT m.*, 
                       COALESCE(mb.server_nick, mb.global_name, mb.username) as author_name
                FROM messages m
                JOIN members mb ON m.author_id = mb.member_id
                WHERE m.author_id IN ({placeholders}) AND m.created_at BETWEEN ? AND ?
            """
            params = author_ids + [start_date.isoformat(), end_date.isoformat()]
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)
