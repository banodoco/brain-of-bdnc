"""
ContentCog — auto-syncs server_content to Discord channels.

Watches the server_content table for changes and edits/posts messages
to the configured Discord channels. Replaces the manual post_rules,
post_welcome, and post_grants_guide scripts.

Each content_key maps to a channel field in server_config and a split
strategy for breaking content into multiple Discord messages.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

logger = logging.getLogger('DiscordBot')

# content_key -> (channel field, split pattern, forum thread name)
CONTENT_REGISTRY: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {
    'post_rules':   ('rules_channel_id',  r'\n\n(?=>)',     None),
    'post_welcome': ('gate_channel_id',   None,             None),
    'post_grants':  ('grants_channel_id', r'\n\n(?=###\s)', 'How Micro-Grants Work'),
}

# Forum companion threads — created once alongside the main thread
COMPANION_THREADS: Dict[str, List[Tuple[str, str]]] = {
    'post_grants': [
        ('Questions & Discussion',
         'Use this thread for questions about the micro-grants program.\n\n'
         'For grant applications, create a new post in the forum instead.'),
    ],
}


class ContentCog(commands.Cog):
    """Periodically syncs server_content to Discord channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)

    async def cog_load(self):
        self.sync_content.start()

    def cog_unload(self):
        self.sync_content.cancel()

    @property
    def server_config(self):
        return getattr(self.db, 'server_config', None)

    @property
    def supabase(self):
        sh = getattr(self.db, 'storage_handler', None)
        return getattr(sh, 'supabase_client', None) if sh else None

    # ------------------------------------------------------------------
    # Periodic sync
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def sync_content(self):
        """Check for updated content and sync to Discord."""
        try:
            await self._sync_all()
        except Exception as e:
            logger.error(f"[ContentCog] Error in sync_content: {e}", exc_info=True)

    @sync_content.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Core sync logic
    # ------------------------------------------------------------------

    async def _sync_all(self):
        sc = self.server_config
        sb = self.supabase
        if not sc or not sb:
            return

        for server in sc.get_enabled_servers(require_write=True):
            guild_id = server['guild_id']
            for content_key, (channel_field, _, _) in CONTENT_REGISTRY.items():
                try:
                    await self._sync_one(sb, sc, guild_id, content_key, channel_field)
                except Exception as e:
                    logger.error(f"[ContentCog] Error syncing {content_key} for guild {guild_id}: {e}", exc_info=True)

    async def _sync_one(self, sb, sc, guild_id: int, content_key: str, channel_field: str):
        """Sync a single content_key for a single guild if it's changed."""
        channel_id = sc.get_server_field(guild_id, channel_field, cast=int)
        if not channel_id:
            return

        content_row = self._get_content_row(sb, guild_id, content_key)
        if not content_row or not content_row.get('content'):
            return

        posted = self._get_posted_row(sb, guild_id, content_key)

        # Check if sync is needed
        content_updated = content_row.get('updated_at', '')
        last_synced = posted.get('last_synced_at', '') if posted else ''
        if posted and last_synced >= content_updated and posted.get('channel_id') == channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                logger.warning(f"[ContentCog] Channel {channel_id} not found for {content_key}")
                return

        _, split_pattern, thread_name = CONTENT_REGISTRY[content_key]
        new_messages = self._split_content(content_row['content'], split_pattern)
        if not new_messages:
            return

        if isinstance(channel, discord.ForumChannel) and thread_name:
            message_ids, thread_id = await self._sync_forum(
                channel, new_messages, thread_name, posted, content_key
            )
        else:
            message_ids = await self._sync_text_channel(channel, new_messages, posted)
            thread_id = None

        if message_ids:
            self._upsert_posted(sb, guild_id, content_key, channel_id, message_ids, thread_id)
            logger.info(f"[ContentCog] Synced {content_key} for guild {guild_id} ({len(message_ids)} messages)")

    # ------------------------------------------------------------------
    # Text channel sync (rules, welcome)
    # ------------------------------------------------------------------

    async def _sync_text_channel(self, channel: discord.TextChannel, new_messages: List[str],
                                  posted: Optional[dict]) -> List[int]:
        """Edit existing messages or post new ones in a text channel."""
        old_ids = posted.get('message_ids', []) if posted else []

        existing = []
        for msg_id in old_ids:
            try:
                existing.append(await channel.fetch_message(msg_id))
            except (discord.NotFound, discord.HTTPException):
                existing = []
                break

        if len(existing) == len(new_messages):
            for msg, new_content in zip(existing, new_messages):
                if msg.content != new_content:
                    await msg.edit(content=new_content)
                    await asyncio.sleep(0.5)
            return [m.id for m in existing]

        # Message count changed or no existing — delete old, post new
        for msg in existing:
            try:
                await msg.delete()
                await asyncio.sleep(0.5)
            except discord.HTTPException:
                pass

        sent_ids = []
        for content in new_messages:
            sent = await channel.send(content)
            sent_ids.append(sent.id)
            await asyncio.sleep(0.5)
        return sent_ids

    # ------------------------------------------------------------------
    # Forum channel sync (grants guide)
    # ------------------------------------------------------------------

    async def _sync_forum(self, forum: discord.ForumChannel, new_messages: List[str],
                           thread_name: str, posted: Optional[dict],
                           content_key: str) -> Tuple[List[int], Optional[int]]:
        """Edit existing forum thread or create new one."""
        thread = await self._find_forum_thread(forum, thread_name, posted)

        if thread:
            result_ids = await self._edit_forum_thread(thread, new_messages)
            await thread.edit(pinned=True, locked=True)
            return result_ids, thread.id

        # No existing thread — create fresh
        result = await forum.create_thread(name=thread_name, content=new_messages[0])
        thread = result.thread if hasattr(result, 'thread') else result
        result_ids = [result.message.id] if hasattr(result, 'message') else []

        for msg_content in new_messages[1:]:
            sent = await thread.send(msg_content)
            result_ids.append(sent.id)
            await asyncio.sleep(0.5)

        await thread.edit(pinned=True, locked=True)

        # Create companion threads (e.g. "Questions & Discussion")
        await self._ensure_companion_threads(forum, content_key)

        return result_ids, thread.id

    async def _find_forum_thread(self, forum: discord.ForumChannel, thread_name: str,
                                  posted: Optional[dict]) -> Optional[discord.Thread]:
        """Find an existing bot-owned forum thread by tracked ID or name search."""
        thread_id = posted.get('thread_id') if posted else None
        if thread_id:
            try:
                thread = await self.bot.fetch_channel(thread_id)
                if thread:
                    return thread
            except (discord.NotFound, discord.HTTPException):
                pass

        for t in forum.threads:
            if t.name == thread_name and t.owner_id == self.bot.user.id:
                return t
        async for t in forum.archived_threads(limit=50):
            if t.name == thread_name and t.owner_id == self.bot.user.id:
                return t
        return None

    async def _edit_forum_thread(self, thread: discord.Thread,
                                  new_messages: List[str]) -> List[int]:
        """Edit messages in an existing forum thread."""
        if thread.archived or thread.locked:
            await thread.edit(archived=False, locked=False)
            await asyncio.sleep(0.5)

        existing_msgs = []
        async for msg in thread.history(limit=100, oldest_first=True):
            if msg.author.id == self.bot.user.id:
                existing_msgs.append(msg)

        result_ids = []
        for i, new_content in enumerate(new_messages):
            if i < len(existing_msgs):
                if existing_msgs[i].content != new_content:
                    await existing_msgs[i].edit(content=new_content)
                    await asyncio.sleep(0.5)
                result_ids.append(existing_msgs[i].id)
            else:
                sent = await thread.send(new_content)
                result_ids.append(sent.id)
                await asyncio.sleep(0.5)

        for i in range(len(new_messages), len(existing_msgs)):
            try:
                await existing_msgs[i].delete()
                await asyncio.sleep(0.5)
            except discord.HTTPException:
                pass

        return result_ids

    async def _ensure_companion_threads(self, forum: discord.ForumChannel, content_key: str):
        """Create companion threads (like 'Questions & Discussion') if they don't exist."""
        companions = COMPANION_THREADS.get(content_key, [])
        for thread_name, thread_content in companions:
            exists = any(
                t.name == thread_name and t.owner_id == self.bot.user.id
                for t in forum.threads
            )
            if not exists:
                async for t in forum.archived_threads(limit=50):
                    if t.name == thread_name and t.owner_id == self.bot.user.id:
                        exists = True
                        if t.archived:
                            await t.edit(archived=False)
                        break

            if not exists:
                await forum.create_thread(name=thread_name, content=thread_content)
                logger.info(f"[ContentCog] Created companion thread '{thread_name}'")

    # ------------------------------------------------------------------
    # Content splitting
    # ------------------------------------------------------------------

    @staticmethod
    def _split_content(content: str, pattern: Optional[str]) -> List[str]:
        """Split content into Discord messages using a regex pattern."""
        if not pattern:
            return [content.strip()] if content.strip() else []
        parts = re.split(pattern, content)
        return [p.strip() for p in parts if p.strip()]

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_content_row(sb, guild_id: int, content_key: str) -> Optional[dict]:
        result = sb.table('server_content').select('*').eq(
            'guild_id', guild_id
        ).eq('content_key', content_key).limit(1).execute()
        return result.data[0] if result.data else None

    @staticmethod
    def _get_posted_row(sb, guild_id: int, content_key: str) -> Optional[dict]:
        try:
            result = sb.table('posted_content').select('*').eq(
                'guild_id', guild_id
            ).eq('content_key', content_key).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception:
            return None

    @staticmethod
    def _upsert_posted(sb, guild_id: int, content_key: str, channel_id: int,
                       message_ids: List[int], thread_id: Optional[int]):
        row = {
            'guild_id': guild_id,
            'content_key': content_key,
            'channel_id': channel_id,
            'message_ids': message_ids,
            'thread_id': thread_id,
            'last_synced_at': datetime.now(timezone.utc).isoformat(),
        }
        sb.table('posted_content').upsert(row).execute()
