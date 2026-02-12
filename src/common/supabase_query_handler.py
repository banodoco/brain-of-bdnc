"""
Supabase Query Handler - Handles read queries from Supabase PostgreSQL.
Executes queries via Supabase client.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

logger = logging.getLogger('DiscordBot')

class SupabaseQueryHandler:
    """Handles read queries from Supabase PostgreSQL database."""
    
    def __init__(self, supabase_client: Optional[Client] = None):
        """
        Initialize the Supabase query handler.
        
        Args:
            supabase_client: Optional pre-initialized Supabase client
        """
        if supabase_client:
            self.supabase = supabase_client
        else:
            self._init_supabase()
    
    def _init_supabase(self) -> None:
        """Initialize the Supabase client."""
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
        
        if not supabase_url or not supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set for Supabase queries")
        
        try:
            # Try with ClientOptions (newer API)
            try:
                options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
                self.supabase = create_client(supabase_url, supabase_key, options=options)
            except (AttributeError, TypeError):
                # Fall back to creating client without options if ClientOptions API has changed
                self.supabase = create_client(supabase_url, supabase_key)
            logger.debug("SupabaseQueryHandler initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client for queries: {e}", exc_info=True)
            raise
    
    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """
        Parse a timestamp string from Supabase, handling variable decimal places.
        
        Python's fromisoformat() requires 0, 3, or 6 decimal places for fractional seconds,
        but Supabase can return timestamps with 2 decimal places (e.g., '2025-09-09T19:41:22.91+00:00').
        This method normalizes the timestamp to 6 decimal places before parsing.
        
        Args:
            timestamp_str: ISO format timestamp string (may end with 'Z' or timezone offset)
            
        Returns:
            Parsed datetime object
        """
        # Replace 'Z' with '+00:00' for UTC
        timestamp_str = timestamp_str.replace('Z', '+00:00')
        
        # Find the decimal point in the fractional seconds
        if '.' in timestamp_str:
            # Split on the timezone indicator (+ or -)
            if '+' in timestamp_str:
                datetime_part, tz_part = timestamp_str.rsplit('+', 1)
                tz_separator = '+'
            elif timestamp_str.count('-') > 2:  # More than date separators
                datetime_part, tz_part = timestamp_str.rsplit('-', 1)
                tz_separator = '-'
            else:
                datetime_part = timestamp_str
                tz_part = None
                tz_separator = None
            
            # Split the datetime part on the decimal point
            if '.' in datetime_part:
                base_part, fractional = datetime_part.split('.', 1)
                # Pad or truncate to 6 digits
                fractional = fractional.ljust(6, '0')[:6]
                datetime_part = f"{base_part}.{fractional}"
            
            # Reconstruct the full timestamp
            if tz_part:
                timestamp_str = f"{datetime_part}{tz_separator}{tz_part}"
            else:
                timestamp_str = datetime_part
        
        return datetime.fromisoformat(timestamp_str)
    
    async def get_last_message_id(self, channel_id: int) -> Optional[int]:
        """Get the most recent message ID for a channel."""
        try:
            result = await asyncio.to_thread(
                self.supabase.table('discord_messages')
                .select('message_id')
                .eq('channel_id', channel_id)
                .order('message_id', desc=True)
                .limit(1)
                .execute
            )
            return result.data[0]['message_id'] if result.data else None
        except Exception as e:
            logger.error(f"Error getting last message ID from Supabase: {e}", exc_info=True)
            raise
    
    async def get_member(self, member_id: int) -> Optional[Dict]:
        """Fetch a member from Supabase by their ID."""
        try:
            result = await asyncio.to_thread(
                self.supabase.table('discord_members')
                .select('*')
                .eq('member_id', member_id)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting member from Supabase: {e}", exc_info=True)
            raise
    
    async def get_channel(self, channel_id: int) -> Optional[Dict]:
        """Fetch a channel from Supabase by its ID."""
        try:
            result = await asyncio.to_thread(
                self.supabase.table('discord_channels')
                .select('*')
                .eq('channel_id', channel_id)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting channel from Supabase: {e}", exc_info=True)
            raise
    
    async def message_exists(self, message_id: int) -> bool:
        """Check if a message exists in Supabase."""
        try:
            result = await asyncio.to_thread(
                self.supabase.table('discord_messages')
                .select('message_id')
                .eq('message_id', message_id)
                .limit(1)
                .execute
            )
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error checking message existence in Supabase: {e}", exc_info=True)
            raise
    
    async def get_summary_thread_id(self, channel_id: int) -> Optional[int]:
        """Get the summary thread ID for a channel."""
        try:
            result = await asyncio.to_thread(
                self.supabase.table('channel_summary')
                .select('summary_thread_id')
                .eq('channel_id', channel_id)
                .order('created_at', desc=True)
                .limit(1)
                .execute
            )
            return result.data[0]['summary_thread_id'] if result.data else None
        except Exception as e:
            logger.error(f"Error getting summary thread ID from Supabase: {e}", exc_info=True)
            raise
    
    async def get_all_message_ids(self, channel_id: int) -> List[int]:
        """Get all message IDs for a channel."""
        try:
            all_ids = []
            offset = 0
            batch_size = 1000
            
            while True:
                result = await asyncio.to_thread(
                    self.supabase.table('discord_messages')
                    .select('message_id')
                    .eq('channel_id', channel_id)
                    .range(offset, offset + batch_size - 1)
                    .execute
                )
                
                if not result.data:
                    break
                
                all_ids.extend([row['message_id'] for row in result.data])
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            return all_ids
        except Exception as e:
            logger.error(f"Error getting all message IDs from Supabase: {e}", exc_info=True)
            raise
    
    async def get_message_date_range(self, channel_id: int) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Get the date range of messages in a channel."""
        try:
            # Get min date
            min_result = await asyncio.to_thread(
                self.supabase.table('discord_messages')
                .select('created_at')
                .eq('channel_id', channel_id)
                .order('created_at', desc=False)
                .limit(1)
                .execute
            )
            
            # Get max date
            max_result = await asyncio.to_thread(
                self.supabase.table('discord_messages')
                .select('created_at')
                .eq('channel_id', channel_id)
                .order('created_at', desc=True)
                .limit(1)
                .execute
            )
            
            min_date = None
            max_date = None
            
            if min_result.data:
                min_date = self._parse_timestamp(min_result.data[0]['created_at'])
            if max_result.data:
                max_date = self._parse_timestamp(max_result.data[0]['created_at'])
            
            return (min_date, max_date)
        except Exception as e:
            logger.error(f"Error getting message date range from Supabase: {e}", exc_info=True)
            raise
    
    async def get_messages_after(self, date: datetime) -> List[Dict]:
        """Get messages after a specific date with member info."""
        try:
            all_messages = []
            offset = 0
            batch_size = 1000
            
            while True:
                result = await asyncio.to_thread(
                    self.supabase.table('discord_messages')
                    .select('*, discord_members(username, global_name, server_nick)')
                    .gt('created_at', date.isoformat())
                    .range(offset, offset + batch_size - 1)
                    .execute
                )
                
                if not result.data:
                    break
                
                # Flatten the member data
                for msg in result.data:
                    if msg.get('discord_members'):
                        member = msg['discord_members']
                        msg['author_name'] = member.get('server_nick') or member.get('global_name') or member.get('username')
                        del msg['discord_members']
                    all_messages.append(msg)
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            return all_messages
        except Exception as e:
            logger.error(f"Error getting messages after date from Supabase: {e}", exc_info=True)
            raise
    
    async def get_messages_in_range(self, start_date: datetime, end_date: datetime, channel_id: Optional[int] = None) -> List[Dict]:
        """Get messages within a date range with member info."""
        try:
            all_messages = []
            offset = 0
            batch_size = 1000
            
            # Fetch messages
            while True:
                query = (self.supabase.table('discord_messages')
                        .select('*')
                        .gte('created_at', start_date.isoformat())
                        .lte('created_at', end_date.isoformat()))
                
                if channel_id:
                    query = query.eq('channel_id', channel_id)
                
                result = await asyncio.to_thread(
                    query.range(offset, offset + batch_size - 1).execute
                )
                
                if not result.data:
                    break
                
                all_messages.extend(result.data)
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            # Fetch member info for all unique authors
            if all_messages:
                unique_author_ids = list(set(msg['author_id'] for msg in all_messages))
                members_map = {}
                
                # Fetch members in batches
                for i in range(0, len(unique_author_ids), 100):
                    batch_ids = unique_author_ids[i:i + 100]
                    members_result = await asyncio.to_thread(
                        self.supabase.table('discord_members')
                        .select('member_id, username, global_name, server_nick')
                        .in_('member_id', batch_ids)
                        .execute
                    )
                    
                    for member in members_result.data:
                        members_map[member['member_id']] = member
                
                # Add author_name to messages
                for msg in all_messages:
                    member = members_map.get(msg['author_id'])
                    if member:
                        msg['author_name'] = member.get('server_nick') or member.get('global_name') or member.get('username')
                    else:
                        msg['author_name'] = None
            
            return all_messages
        except Exception as e:
            logger.error(f"Error getting messages in range from Supabase: {e}", exc_info=True)
            raise
    
    async def get_messages_by_authors_in_range(self, author_ids: List[int], start_date: datetime, end_date: datetime) -> List[Dict]:
        """Get messages by specific authors within a date range."""
        try:
            all_messages = []
            offset = 0
            batch_size = 1000
            
            # Fetch messages
            while True:
                result = await asyncio.to_thread(
                    self.supabase.table('discord_messages')
                    .select('*')
                    .in_('author_id', author_ids)
                    .gte('created_at', start_date.isoformat())
                    .lte('created_at', end_date.isoformat())
                    .range(offset, offset + batch_size - 1)
                    .execute
                )
                
                if not result.data:
                    break
                
                all_messages.extend(result.data)
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            # Fetch member info for all authors
            if all_messages and author_ids:
                members_map = {}
                
                # Fetch members in batches
                for i in range(0, len(author_ids), 100):
                    batch_ids = author_ids[i:i + 100]
                    members_result = await asyncio.to_thread(
                        self.supabase.table('discord_members')
                        .select('member_id, username, global_name, server_nick')
                        .in_('member_id', batch_ids)
                        .execute
                    )
                    
                    for member in members_result.data:
                        members_map[member['member_id']] = member
                
                # Add author_name to messages
                for msg in all_messages:
                    member = members_map.get(msg['author_id'])
                    if member:
                        msg['author_name'] = member.get('server_nick') or member.get('global_name') or member.get('username')
                    else:
                        msg['author_name'] = None
            
            return all_messages
        except Exception as e:
            logger.error(f"Error getting messages by authors from Supabase: {e}", exc_info=True)
            raise
    
    async def get_messages_by_ids(self, message_ids: List[int]) -> List[Dict]:
        """Get messages by their IDs with member info."""
        try:
            # Supabase has a limit on IN clause size, so batch if needed
            all_messages = []
            batch_size = 100
            
            # Fetch messages
            for i in range(0, len(message_ids), batch_size):
                batch_ids = message_ids[i:i + batch_size]
                
                result = await asyncio.to_thread(
                    self.supabase.table('discord_messages')
                    .select('*')
                    .in_('message_id', batch_ids)
                    .execute
                )
                
                all_messages.extend(result.data)
            
            # Fetch member info
            if all_messages:
                unique_author_ids = list(set(msg['author_id'] for msg in all_messages))
                members_map = {}
                
                for i in range(0, len(unique_author_ids), 100):
                    batch_ids = unique_author_ids[i:i + 100]
                    members_result = await asyncio.to_thread(
                        self.supabase.table('discord_members')
                        .select('member_id, username, global_name, server_nick')
                        .in_('member_id', batch_ids)
                        .execute
                    )
                    
                    for member in members_result.data:
                        members_map[member['member_id']] = member
                
                # Add author_name to messages
                for msg in all_messages:
                    member = members_map.get(msg['author_id'])
                    if member:
                        msg['author_name'] = member.get('server_nick') or member.get('global_name') or member.get('username')
                    else:
                        msg['author_name'] = None
            
            return all_messages
        except Exception as e:
            logger.error(f"Error getting messages by IDs from Supabase: {e}", exc_info=True)
            raise
    
    async def get_message_dates(self, channel_id: int) -> List[str]:
        """Get distinct dates that have messages in a channel."""
        try:
            # Use RPC for date aggregation (more efficient)
            # For now, fetch all and aggregate in Python
            offset = 0
            batch_size = 1000
            dates_set = set()
            
            while True:
                result = await asyncio.to_thread(
                    self.supabase.table('discord_messages')
                    .select('created_at')
                    .eq('channel_id', channel_id)
                    .range(offset, offset + batch_size - 1)
                    .execute
                )
                
                if not result.data:
                    break
                
                for msg in result.data:
                    # Extract date portion
                    date_str = msg['created_at'][:10]  # YYYY-MM-DD
                    dates_set.add(date_str)
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            return sorted(list(dates_set))
        except Exception as e:
            logger.error(f"Error getting message dates from Supabase: {e}", exc_info=True)
            raise
    
    async def search_messages(self, query: str, channel_id: Optional[int] = None) -> List[Dict]:
        """Search messages using PostgreSQL full-text search."""
        try:
            # Use textSearch for full-text search on content
            # Note: This requires a tsvector column or uses to_tsvector on the fly
            all_messages = []
            offset = 0
            batch_size = 1000
            
            while True:
                query_builder = (self.supabase.table('discord_messages')
                               .select('*, discord_members(username, global_name, server_nick)')
                               .text_search('content', query))
                
                if channel_id:
                    query_builder = query_builder.eq('channel_id', channel_id)
                
                result = await asyncio.to_thread(
                    query_builder.range(offset, offset + batch_size - 1).execute
                )
                
                if not result.data:
                    break
                
                # Flatten the member data
                for msg in result.data:
                    if msg.get('discord_members'):
                        member = msg['discord_members']
                        msg['author_name'] = member.get('server_nick') or member.get('global_name') or member.get('username')
                        del msg['discord_members']
                    all_messages.append(msg)
                
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            return all_messages
        except Exception as e:
            logger.error(f"Error searching messages in Supabase: {e}", exc_info=True)
            # Full-text search might not be configured, return empty
            logger.warning("Full-text search may not be configured in Supabase. Returning empty results.")
            return []
    
    async def execute_raw_sql(self, sql: str, params: Tuple = None) -> List[Dict]:
        """
        Execute raw SQL query on Supabase.
        
        For complex queries, this method converts them to REST API calls.
        Supports common query patterns used in daily updates.
        
        Args:
            sql: SQL query
            params: Query parameters
            
        Returns:
            List of result dictionaries
        """
        try:
            logger.debug(f"Executing query on Supabase via REST API")
            logger.debug(f"SQL: {sql[:200]}...")
            
            # Parse the SQL to determine query type and convert to REST API call
            # Normalize whitespace for easier matching (collapse newlines/spaces to single space)
            import re
            sql_lower = re.sub(r'\s+', ' ', sql.lower().strip())
            
            logger.debug(f"🔍 Normalized SQL (first 150 chars): {sql_lower[:150]}...")
            logger.debug(f"🔍 Routing checks: CTE={sql_lower.startswith('with ')}, FROM messages={'from messages' in sql_lower}, JOIN messages={'join messages' in sql_lower}, FROM channels={'from channels' in sql_lower}, JOIN channels={'join channels' in sql_lower}")
            
            # Handle CTEs (WITH clause) - convert to regular query
            if sql_lower.startswith('with '):
                logger.info(f"🔀 Routing to _handle_cte_query (detected: WITH clause)")
                return await self._handle_cte_query(sql, params)
            
            # Handle channels query (supports both old and new table names)
            if ('from discord_channels' in sql_lower or 'from channels' in sql_lower or 'join channels' in sql_lower or 'join discord_channels' in sql_lower):
                # Check it's not a join FROM messages/members
                if 'from messages' not in sql_lower and 'from members' not in sql_lower and 'from discord_messages' not in sql_lower:
                    logger.info(f"🔀 Routing to _query_production_channels (detected: FROM/JOIN channels without FROM messages/members)")
                    return await self._query_production_channels(sql, params)
                else:
                    logger.debug(f"🔍 Skipping _query_production_channels (has FROM messages/members)")
            
            # Handle message queries (supports both 'discord_messages' and 'messages')
            if 'discord_messages' in sql_lower or ('from messages' in sql_lower or 'join messages' in sql_lower):
                logger.debug(f"🔀 Routing to _query_messages")
                return await self._query_messages(sql, params)
            
            # Handle member queries (supports both 'discord_members' and 'members')
            if 'discord_members' in sql_lower or ('from members' in sql_lower or 'join members' in sql_lower):
                logger.info(f"🔀 Routing to _query_members (detected: FROM/JOIN members)")
                return await self._query_members(sql, params)
            
            # Generic fallback - try to parse and execute via REST
            else:
                logger.info(f"🔀 Routing to _execute_generic_query (no specific handler matched)")
                logger.info(f"🔍 SQL contains: FROM messages={('from messages' in sql_lower)}, JOIN messages={('join messages' in sql_lower)}, JOIN channels={('join channels' in sql_lower)}")
                return await self._execute_generic_query(sql, params)
            
        except Exception as e:
            logger.error(f"Error executing query on Supabase: {e}", exc_info=True)
            logger.error(f"Query was: {sql[:500]}...")
            raise
    
    async def _query_production_channels(self, sql: str, params: Tuple = None) -> List[Dict]:
        """Query channels using REST API, with support for complex JOINs."""
        try:
            sql_lower = sql.lower()
            
            # Check if this is the complex production channels query with JOINs
            if 'left join' in sql_lower and 'group by' in sql_lower and 'msg_count' in sql_lower:
                return await self._query_production_channels_with_messages(sql, params)
            
            # Simple channel query
            query = self.supabase.table('discord_channels').select('*')
            
            # Filter for production channels if specified
            if 'is_production' in sql_lower:
                query = query.eq('is_production', True)
            
            # Handle LIMIT
            user_limit = None
            if 'limit' in sql_lower:
                import re
                limit_match = re.search(r'limit\s+(\d+)', sql_lower)
                if limit_match:
                    user_limit = int(limit_match.group(1))
            
            # Fetch with pagination
            channels = []
            offset = 0
            batch_size = 1000
            
            while True:
                paginated_query = query.range(offset, offset + batch_size - 1)
                response = paginated_query.execute()
                batch = response.data if response.data else []
                
                if not batch:
                    break
                
                channels.extend(batch)
                
                # If user specified a LIMIT and we've reached it, stop
                if user_limit and len(channels) >= user_limit:
                    channels = channels[:user_limit]
                    break
                
                # If we got less than batch_size, we've reached the end
                if len(batch) < batch_size:
                    break
                
                offset += batch_size
            
            return channels
            
        except Exception as e:
            logger.error(f"Error querying channels: {e}")
            return []
    
    async def _query_production_channels_with_messages(self, sql: str, params: Tuple = None) -> List[Dict]:
        """Handle complex production channels query with JOINs, GROUP BY, and message counts."""
        try:
            import re
            from datetime import datetime, timedelta
            from collections import defaultdict
            
            # Extract channel IDs from SQL
            channel_ids_match = re.search(r'IN \(([0-9,\s]+)\)', sql)
            if not channel_ids_match:
                logger.warning("Could not extract channel IDs from production channels query")
                return []
            
            channel_ids = [int(cid.strip()) for cid in channel_ids_match.group(1).split(',')]
            logger.debug(f"Querying for channel IDs: {channel_ids}")
            
            # Fetch all channels
            channels_response = self.supabase.table('discord_channels').select('*').in_('channel_id', [str(cid) for cid in channel_ids]).execute()
            all_channels = channels_response.data if channels_response.data else []
            
            # Also fetch channels by category_id
            category_response = self.supabase.table('discord_channels').select('*').in_('category_id', [str(cid) for cid in channel_ids]).execute()
            if category_response.data:
                all_channels.extend(category_response.data)
            
            # Fetch messages from last 24 hours
            time_24h_ago = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            messages_response = self.supabase.table('discord_messages').select('channel_id,message_id').gte('created_at', time_24h_ago).execute()
            messages = messages_response.data if messages_response.data else []
            
            # Count messages per channel
            message_counts = defaultdict(int)
            for msg in messages:
                message_counts[msg['channel_id']] += 1
            
            # Build result with JOINs
            results = []
            category_names = {ch['channel_id']: ch['channel_name'] for ch in all_channels}
            
            for channel in all_channels:
                channel_id = channel['channel_id']
                msg_count = message_counts.get(channel_id, 0)
                
                # Apply HAVING filter (>= 25 messages)
                if msg_count < 25:
                    continue
                
                # Get category/source name
                category_id = channel.get('category_id')
                source = category_names.get(category_id, 'Unknown') if category_id else 'Unknown'
                
                results.append({
                    'channel_id': channel_id,
                    'channel_name': channel['channel_name'],
                    'source': source,
                    'msg_count': msg_count
                })
            
            # Sort by msg_count DESC
            results.sort(key=lambda x: x['msg_count'], reverse=True)
            
            logger.debug(f"Production channels query returned {len(results)} channels")
            return results
            
        except Exception as e:
            logger.error(f"Error in production channels with messages query: {e}", exc_info=True)
            return []
    
    async def _query_messages(self, sql: str, params: Tuple = None) -> List[Dict]:
        """Query messages using REST API, handling complex filters."""
        logger.debug(f"🎯 ENTERED _query_messages")
        logger.debug(f"🎯 SQL preview: {sql[:200]}...")
        logger.debug(f"🎯 Params: {params}")
        try:
            import re
            sql_lower = sql.lower()
            
            # Extract parameters - separate channel_id, message_id and timestamp
            time_filter = None
            channel_id_from_params = None
            message_id_from_params = None
            channel_ids_from_params = []  # Initialize before params parsing
            
            if params:
                for p in params:
                    # Check if it's a timestamp
                    if isinstance(p, str) and ('T' in p or (p.count('-') >= 2)):
                        time_filter = p
                    # Check if it's a channel/message ID (long integer)
                    elif isinstance(p, (int, str)) and len(str(p)) >= 15:
                        try:
                            # Determine if it's message_id or channel_id based on SQL
                            if 'message_id' in sql_lower and 'where message_id' in sql_lower:
                                message_id_from_params = int(p)
                            else:
                                channel_id_from_params = int(p)
                                # Add to the list immediately so it gets used in the query
                                channel_ids_from_params = [channel_id_from_params]
                                logger.debug(f"🔍 Extracted channel_id from params: {channel_id_from_params}")
                        except (ValueError, TypeError, IndexError):
                            pass

            # Also try to extract channel_id(s) from SQL WHERE clause (only if not already found in params)
            if not channel_ids_from_params and not message_id_from_params:
                # First try to match IN clause with multiple IDs: channel_id IN (123, 456, 789)
                in_match = re.search(r'channel_id\s+in\s*\(([^)]+)\)', sql_lower)
                if in_match:
                    try:
                        # Extract all channel IDs from the IN clause
                        ids_str = in_match.group(1)
                        channel_ids_from_params = [int(cid.strip()) for cid in ids_str.split(',') if cid.strip().isdigit()]
                        logger.debug(f"🔍 Extracted channel_ids from SQL IN clause: {channel_ids_from_params}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to parse channel_ids from IN clause: {e}")
                
                # If no IN clause, try single equality: channel_id = 123
                if not channel_ids_from_params:
                    eq_match = re.search(r'channel_id\s*=\s*(\d+)', sql_lower)
                    if eq_match:
                        try:
                            channel_id_from_params = int(eq_match.group(1))
                            channel_ids_from_params = [channel_id_from_params]
                            logger.info(f"🔍 Extracted single channel_id from SQL: {channel_id_from_params}")
                        except (ValueError, TypeError):
                            pass

                # Match patterns like: WHERE message_id = 123
                message_match = re.search(r'message_id\s*=\s*(\d+)', sql_lower)
                if message_match:
                    try:
                        message_id_from_params = int(message_match.group(1))
                        logger.info(f"🔍 Extracted message_id from SQL: {message_id_from_params}")
                    except (ValueError, TypeError):
                        pass
                
                if not channel_ids_from_params and not message_id_from_params:
                    logger.warning(f"⚠️ No channel_id or message_id found in SQL WHERE clause!")
            
            # Start with base query
            query = self.supabase.table('discord_messages').select('*')
            
            # Handle message_id filter (takes priority)
            if message_id_from_params:
                query = query.eq('message_id', str(message_id_from_params))
                logger.debug(f"✅ Applying message filter: message_id = {message_id_from_params}")
            # Handle channel_id filter (single or multiple)
            elif channel_ids_from_params:
                # Check if SQL includes category_id logic (for sub-channels)
                # Pattern: OR EXISTS (SELECT 1 FROM channels c2 WHERE c2.channel_id = m.channel_id AND c2.category_id IN (...))
                if 'category_id' in sql_lower and 'exists' in sql_lower:
                    logger.info("🔍 Detected category_id logic in SQL - expanding channel list to include sub-channels...")
                    try:
                        # Fetch all channel_ids where either channel_id OR category_id is in the monitor list
                        channels_response = self.supabase.table('discord_channels').select('channel_id, category_id').execute()
                        all_channels = channels_response.data if channels_response.data else []
                        
                        # Build expanded list: direct channels + channels whose category is in the list
                        expanded_channel_ids = set(channel_ids_from_params)
                        for ch in all_channels:
                            ch_id = ch.get('channel_id')
                            cat_id = ch.get('category_id')
                            if cat_id and int(cat_id) in channel_ids_from_params:
                                expanded_channel_ids.add(int(ch_id))
                        
                        channel_ids_from_params = list(expanded_channel_ids)
                        logger.info(f"✅ Expanded channel list to {len(channel_ids_from_params)} channels (including sub-channels)")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to expand channel list with category logic: {e}")
                
                if len(channel_ids_from_params) == 1:
                    query = query.eq('channel_id', str(channel_ids_from_params[0]))
                    logger.debug(f"✅ Applying channel filter: channel_id = {channel_ids_from_params[0]}")
                else:
                    # Use .in_() for multiple channel IDs
                    query = query.in_('channel_id', [str(cid) for cid in channel_ids_from_params])
                    logger.debug(f"✅ Applying channel filter: channel_id IN {channel_ids_from_params}")
            else:
                logger.warning(f"⚠️ No message/channel filter applied - will fetch ALL messages!")
            
            # Try to extract time filter from SQL
            time_filter_from_sql = None
            time_match = re.search(r"created_at\s*>=\s*['\"]([^'\"]+)['\"]", sql_lower)
            if time_match:
                time_filter_from_sql = time_match.group(1)
                logger.info(f"🔍 Extracted time filter from SQL: {time_filter_from_sql}")
            
            # Handle time range filter (skip if fetching by specific message_id)
            if time_filter_from_sql or time_filter or 'created_at >' in sql_lower or 'created_at >=' in sql_lower:
                if not time_filter:
                    time_filter = time_filter_from_sql
                if not time_filter:
                    # Try to extract from SQL or use default
                    from datetime import datetime, timedelta
                    time_filter = (datetime.utcnow() - timedelta(hours=24)).isoformat()
                
                query = query.gte('created_at', time_filter)
                logger.debug(f"✅ Applying time filter: created_at >= {time_filter}")
            elif not message_id_from_params:
                # Only warn if we're not fetching a specific message by ID
                logger.warning(f"⚠️ No time filter applied - will fetch ALL messages!")
            
            # Fetch ALL matching messages with pagination (we'll filter in Python)
            logger.debug(f"🚀 Executing Supabase query with pagination...")
            messages = []
            seen_message_ids = set()  # Track seen IDs to prevent duplicates from pagination overlap
            offset = 0
            batch_size = 1000
            
            while True:
                # Apply range for pagination
                paginated_query = query.range(offset, offset + batch_size - 1)
                response = paginated_query.execute()
                batch = response.data if response.data else []
                
                if not batch:
                    break
                
                # Deduplicate: Supabase pagination can return overlapping results with complex IN queries
                new_messages = 0
                for msg in batch:
                    msg_id = msg.get('message_id')
                    if msg_id and msg_id not in seen_message_ids:
                        seen_message_ids.add(msg_id)
                        messages.append(msg)
                        new_messages += 1
                
                logger.debug(f"📦 Fetched batch: {len(batch)} messages, {new_messages} new (total unique: {len(messages)})")
                
                # If we got less than batch_size, we've reached the end
                if len(batch) < batch_size:
                    break
                
                offset += batch_size
            
            logger.info(f"✅ Fetched {len(messages)} messages from Supabase before post-processing")
            
            # If query includes channel join, enrich with channel names
            if 'join channels' in sql_lower or 'c.channel_name' in sql_lower:
                logger.debug(f"🔧 Enriching with channel names...")
                messages = await self._enrich_with_channel_names(messages)
                logger.debug(f"✅ After channel enrichment: {len(messages)} messages")
            
            # Post-process messages for complex filters
            logger.debug(f"🔧 Post-processing messages...")
            messages = await self._post_process_messages(messages, sql, sql_lower, params)
            logger.debug(f"✅ After post-processing: {len(messages)} messages")
            
            # If query includes author_name/member, enrich with author names
            if 'author_name' in sql_lower or 'member' in sql_lower or 'join members' in sql_lower:
                logger.debug(f"🔧 Enriching with author names...")
                messages = await self._enrich_with_author_names(messages)
                logger.debug(f"✅ After author enrichment: {len(messages)} messages")
            
            # Handle ordering AFTER enrichment
            if 'order by' in sql_lower:
                if 'unique_reactor_count desc' in sql_lower or 'reaction_count desc' in sql_lower:
                    messages = sorted(messages, key=lambda x: x.get('unique_reactor_count', x.get('reaction_count', 0)), reverse=True)
                elif 'created_at desc' in sql_lower:
                    messages = sorted(messages, key=lambda x: x.get('created_at', ''), reverse=True)
            
            # Handle GROUP BY channel_id with COUNT (for dev mode channel selection)
            if 'group by channel_id' in sql_lower and 'count(*)' in sql_lower:
                logger.info(f"🔍 Detected GROUP BY channel_id with COUNT - processing...")
                from collections import defaultdict
                channel_counts = defaultdict(int)
                for msg in messages:
                    channel_counts[msg.get('channel_id')] += 1
                
                logger.info(f"📊 Channel message counts: {dict(channel_counts)}")
                
                # Apply HAVING filter if present
                min_count = 25  # default for HAVING COUNT(*) >= 25
                if 'having count(*)' in sql_lower:
                    having_match = re.search(r'having\s+count\(\*\)\s*>=\s*(\d+)', sql_lower)
                    if having_match:
                        min_count = int(having_match.group(1))
                
                logger.info(f"📋 Applying HAVING filter: COUNT(*) >= {min_count}")
                
                # Return channels that meet the threshold
                results = []
                for channel_id, count in channel_counts.items():
                    if count >= min_count:
                        logger.info(f"✅ Channel {channel_id} meets threshold: {count} >= {min_count}")
                        results.append({'channel_id': channel_id})
                    else:
                        logger.info(f"❌ Channel {channel_id} below threshold: {count} < {min_count}")
                
                logger.info(f"✅ GROUP BY returned {len(results)} channels with >= {min_count} messages")
                return results
            
            # Handle LIMIT
            if 'limit' in sql_lower:
                limit_match = re.search(r'limit\s+(\d+)', sql_lower)
                if limit_match:
                    limit = int(limit_match.group(1))
                    messages = messages[:limit]
            
            logger.debug(f"Returning {len(messages)} messages after filtering")
            return messages
            
        except Exception as e:
            logger.error(f"❌ EXCEPTION in _query_messages: {e}", exc_info=True)
            logger.error(f"❌ Returning empty list due to exception")
            return []
    
    async def _post_process_messages(self, messages: List[Dict], sql: str, sql_lower: str, params: Tuple = None) -> List[Dict]:
        """Post-process messages for complex filters that REST API can't handle."""
        import re
        
        # Handle NSFW channel filtering - fetch channel names if needed
        channel_names = {}
        if 'nsfw' in sql_lower or "not like '%nsfw%'" in sql_lower:
            # Get unique channel IDs
            channel_ids = list(set(msg.get('channel_id') for msg in messages if msg.get('channel_id')))
            if channel_ids:
                logger.debug(f"Fetching channel names for NSFW filtering ({len(channel_ids)} channels)")
                channel_query = self.supabase.table('discord_channels').select('channel_id,channel_name').in_('channel_id', [str(cid) for cid in channel_ids])
                channel_response = channel_query.execute()
                if channel_response.data:
                    channel_names = {ch['channel_id']: ch.get('channel_name', '') for ch in channel_response.data}
        
        filtered = []
        
        for msg in messages:
            # Always calculate reactor count first
            reactors = msg.get('reactors')
            try:
                import json
                if isinstance(reactors, str):
                    reactors_list = json.loads(reactors) if reactors and reactors != '[]' else []
                elif isinstance(reactors, list):
                    reactors_list = reactors
                else:
                    reactors_list = []
                
                unique_reactor_count = len(reactors_list)
            except (json.JSONDecodeError, TypeError, KeyError):
                unique_reactor_count = 0
            
            # Add this to the message dict for sorting/filtering
            msg['unique_reactor_count'] = unique_reactor_count
            
            # Handle attachment-related filters
            has_attachments_requirement = ('attachments' in sql_lower or 
                                          'json_valid(m.attachments)' in sql_lower or
                                          "attachments != '[]'" in sql_lower)
            
            if has_attachments_requirement:
                attachments = msg.get('attachments')
                
                # Skip if no attachments
                if not attachments or attachments == '[]' or attachments == []:
                    continue
                
                # Parse JSON attachments
                try:
                    if isinstance(attachments, str):
                        attachments_list = json.loads(attachments)
                    elif isinstance(attachments, list):
                        attachments_list = attachments
                    else:
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue

                if not attachments_list:
                    continue
                
                # Check for video files (.mp4, .mov, .webm)
                if '.mp4' in sql_lower or '.mov' in sql_lower or '.webm' in sql_lower or 'video' in sql_lower:
                    has_video = any(
                        att.get('filename', '').lower().endswith(('.mp4', '.mov', '.webm'))
                        for att in attachments_list
                    )
                    if not has_video:
                        continue
            
            # Handle reactor count threshold - parse dynamically from SQL
            # Look for patterns like "unique_reactor_count >= X" or "reaction_count >= X"
            # NOTE: Do NOT match ") >= X" as that could be COUNT(*) >= X
            reactor_threshold = None
            reactor_match = re.search(r'(?:unique_reactor_count|reaction_count)\s*>=\s*(\d+)', sql_lower)
            if reactor_match:
                reactor_threshold = int(reactor_match.group(1))
                logger.debug(f"Detected reactor count threshold in SQL: >= {reactor_threshold}")
                if unique_reactor_count < reactor_threshold:
                    logger.debug(f"Filtering out message with {unique_reactor_count} reactors (threshold: {reactor_threshold})")
                    continue
            
            # Handle NSFW filter - skip messages from NSFW channels
            if channel_names and ('not like' in sql_lower and 'nsfw' in sql_lower):
                channel_id = msg.get('channel_id')
                channel_name = channel_names.get(channel_id, '')
                if channel_name and 'nsfw' in channel_name.lower():
                    logger.debug(f"Filtering out message from NSFW channel: {channel_name}")
                    continue
            
            filtered.append(msg)
        
        logger.debug(f"Post-processing: {len(messages)} → {len(filtered)} messages")
        return filtered
    
    async def _query_members(self, sql: str, params: Tuple = None) -> List[Dict]:
        """Query members using REST API with pagination."""
        try:
            query = self.supabase.table('discord_members').select('*')
            
            # Handle member_id IN clause
            if 'member_id in' in sql.lower() and params:
                member_ids = [str(mid) for mid in params]
                query = query.in_('member_id', member_ids)
            # Handle single member_id filter  
            elif 'member_id' in sql.lower() and params:
                member_id = params[0] if params else None
                if member_id:
                    query = query.eq('member_id', str(member_id))
            
            # Fetch with pagination
            results = []
            offset = 0
            batch_size = 1000
            
            while True:
                paginated_query = query.range(offset, offset + batch_size - 1)
                response = paginated_query.execute()
                batch = response.data if response.data else []
                
                if not batch:
                    break
                
                results.extend(batch)
                
                # If we got less than batch_size, we've reached the end
                if len(batch) < batch_size:
                    break
                
                offset += batch_size
            
            # Handle COALESCE(server_nick, global_name, username) as display_name
            if 'display_name' in sql.lower():
                for row in results:
                    row['display_name'] = row.get('server_nick') or row.get('global_name') or row.get('username') or 'Unknown'
            
            return results
            
        except Exception as e:
            logger.error(f"Error querying members: {e}")
            return []
    
    async def _handle_cte_query(self, sql: str, params: Tuple = None) -> List[Dict]:
        """
        Handle CTE (WITH clause) queries by converting them to regular queries.
        """
        try:
            logger.debug("Handling CTE query")
            
            # Extract the SELECT part after the CTE
            # Pattern: WITH cte_name AS (...) SELECT ... FROM cte_name
            
            # For now, treat it as a regular message query since most CTEs are for messages
            # Extract the main query logic from the CTE
            sql_lower = sql.lower()
            
            # Find the inner SELECT in the CTE
            with_end = sql_lower.find('select', sql_lower.find('as ('))
            if with_end == -1:
                logger.warning("Could not parse CTE structure")
                return []
            
            # Get the query content from the CTE
            # This is a simplified approach - we execute the inner query
            return await self._query_messages(sql, params)
            
        except Exception as e:
            logger.error(f"Error handling CTE query: {e}")
            return []
    
    async def _execute_generic_query(self, sql: str, params: Tuple = None) -> List[Dict]:
        """
        Fallback for complex queries - attempts basic parsing.
        For unsupported queries, returns empty list.
        """
        # Silently return empty - these warnings are too noisy
        return []
    
    async def _enrich_with_channel_names(self, messages: List[Dict]) -> List[Dict]:
        """Add channel_name to messages by looking up channels."""
        if not messages:
            return messages
        
        # Get unique channel IDs
        channel_ids = list(set(msg.get('channel_id') for msg in messages if msg.get('channel_id')))
        
        if not channel_ids:
            return messages
        
        # Fetch channel names
        channel_query = self.supabase.table('discord_channels').select('channel_id,channel_name').in_('channel_id', [str(cid) for cid in channel_ids])
        channel_response = channel_query.execute()
        channels = channel_response.data if channel_response.data else []
        
        # Create lookup dict
        channel_lookup = {ch['channel_id']: ch.get('channel_name', 'Unknown') for ch in channels}
        
        # Enrich messages
        for msg in messages:
            channel_id = msg.get('channel_id')
            if channel_id:
                msg['channel_name'] = channel_lookup.get(channel_id, 'Unknown')
        
        return messages
    
    async def _enrich_with_author_names(self, messages: List[Dict]) -> List[Dict]:
        """Add author_name to messages by looking up members."""
        if not messages:
            return messages
        
        # Get unique author IDs
        author_ids = list(set(msg.get('author_id') for msg in messages if msg.get('author_id')))
        
        if not author_ids:
            return messages
        
        # Fetch member names (Supabase uses member_id, not user_id)
        member_query = self.supabase.table('discord_members').select('member_id,username,global_name,server_nick').in_('member_id', author_ids)
        member_response = member_query.execute()
        members = member_response.data if member_response.data else []
        
        # Create lookup dict with proper name priority
        member_lookup = {}
        for m in members:
            # Use server_nick > global_name > username (same as SQL COALESCE)
            name = m.get('server_nick') or m.get('global_name') or m.get('username') or 'Unknown'
            member_lookup[m['member_id']] = name
        
        # Enrich messages
        for msg in messages:
            author_id = msg.get('author_id')
            if author_id:
                msg['author_name'] = member_lookup.get(author_id, 'Unknown')
        
        return messages

