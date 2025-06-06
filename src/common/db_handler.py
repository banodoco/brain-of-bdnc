import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path
from .constants import get_database_path
import time
import threading
import asyncio
import queue

logger = logging.getLogger('DiscordBot')

class DatabaseHandler:
    def __init__(self, db_path: Optional[str] = None, dev_mode: bool = False, pool_size: int = 5):
        """Initialize database path and ensure directory exists."""
        try:
            self.db_path = db_path if db_path else get_database_path(dev_mode)
            self.dev_mode = dev_mode
            
            db_dir = Path(self.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            
            self.connection_pool = queue.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self.connection_pool.put(conn)
            
            self.write_lock = threading.Lock()
            self._init_db()
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise

    def _get_connection_from_pool(self):
        try:
            conn = self.connection_pool.get(timeout=5)
            if self.dev_mode:
                logger.debug(f"Retrieved connection from pool, remaining connections: {self.connection_pool.qsize()}")
            return conn
        except queue.Empty:
            logger.warning('Connection pool exhausted. Creating a new connection.')
            return sqlite3.connect(self.db_path, check_same_thread=False)

    def _return_connection_to_pool(self, conn):
        self.connection_pool.put(conn)

    def close(self):
        """Close the database connection."""
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
                    channel_id BIGINT,
                    summary_thread_id BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id),
                    PRIMARY KEY (channel_id, created_at)
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
        """Execute a SQL query and return the results."""
        def query_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
            
        return self._execute_with_retry(query_operation)

    def _store_messages(self, messages: List[Dict]):
        if self.dev_mode:
            logger.debug(f"Starting to store {len(messages)} messages")
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

    def store_messages(self, messages: List[Dict]):
        asyncio.to_thread(self._store_messages, messages)

    def get_last_message_id(self, channel_id: int) -> Optional[int]:
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
                SELECT m.* FROM messages m JOIN messages_fts fts ON m.message_id = fts.rowid
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
                    ON CONFLICT(channel_id, created_at) DO UPDATE SET
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
                return (datetime.fromisoformat(result[0]), datetime.fromisoformat(result[1]))
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
        """Fetch a member from the database by their ID."""
        def get_member_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM members WHERE member_id = ?", (member_id,))
            result = cursor.fetchone()
            cursor.close()
            return dict(result) if result else None
        return self._execute_with_retry(get_member_operation)

    def message_exists(self, message_id: int) -> bool:
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
        return self._execute_with_retry(member_operation)

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
        return self._execute_with_retry(channel_operation)

    def get_messages_after(self, date: datetime) -> List[Dict]:
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM messages WHERE created_at > ?", (date.isoformat(),))
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)

    def get_messages_by_ids(self, message_ids: List[int]) -> List[Dict]:
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in message_ids)
            cursor.execute(f"SELECT * FROM messages WHERE message_id IN ({placeholders})", message_ids)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)

    def get_messages_in_range(self, start_date: datetime, end_date: datetime, channel_id: Optional[int] = None) -> List[Dict]:
        def get_range_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = "SELECT * FROM messages WHERE created_at BETWEEN ? AND ?"
            params = [start_date.isoformat(), end_date.isoformat()]
            if channel_id:
                query += " AND channel_id = ?"
                params.append(channel_id)
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_range_operation)

    def get_messages_by_authors_in_range(self, author_ids: List[int], start_date: datetime, end_date: datetime) -> List[Dict]:
        def get_messages_operation(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in author_ids)
            query = f"SELECT * FROM messages WHERE author_id IN ({placeholders}) AND created_at BETWEEN ? AND ?"
            params = author_ids + [start_date.isoformat(), end_date.isoformat()]
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        return self._execute_with_retry(get_messages_operation)
