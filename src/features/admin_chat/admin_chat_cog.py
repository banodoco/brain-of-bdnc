"""Discord cog for privileged admin chat and approved member bot access."""
import asyncio
import os
import logging
import time
from collections import deque
import discord
from discord.ext import commands

from .agent import AdminChatAgent

logger = logging.getLogger('DiscordBot')


class AdminChatCog(commands.Cog):
    """Cog that handles admin chat plus approved member requests."""

    _ACCESS_CACHE_TTL_SECONDS = 60
    _RATE_LIMIT_WINDOW_SECONDS = 300
    _RATE_LIMIT_MAX_MESSAGES = 10

    def __init__(self, bot: commands.Bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self.agent: AdminChatAgent = None

        # Track whether the agent is busy processing a request per user
        self._busy: dict[int, bool] = {}
        # Queue follow-up messages that arrive while agent is busy
        self._pending_messages: dict[int, discord.Message] = {}
        self._message_access_cache: dict[int, tuple[float, bool]] = {}
        self._guild_context_cache: dict[int, tuple[float, int | None]] = {}
        self._rate_limits: dict[int, deque[float]] = {}

        # Get admin user ID
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if admin_id_str:
            try:
                self.admin_user_id = int(admin_id_str)
                logger.info(f"[AdminChat] Configured for admin user ID: {self.admin_user_id}")
            except ValueError:
                logger.error(f"[AdminChat] Invalid ADMIN_USER_ID: {admin_id_str}")
                self.admin_user_id = None
        else:
            logger.warning("[AdminChat] ADMIN_USER_ID not set - admin chat disabled")
            self.admin_user_id = None

    def _get_supabase(self):
        """Get the shared Supabase client if available."""
        storage_handler = getattr(self.db_handler, 'storage_handler', None)
        return getattr(storage_handler, 'supabase_client', None)
    
    def _ensure_agent(self):
        """Lazily initialize the agent (to avoid issues during bot startup)."""
        if self.agent is None:
            try:
                self.agent = AdminChatAgent(
                    bot=self.bot,
                    db_handler=self.db_handler,
                    sharer=self.sharer
                )
                logger.info("[AdminChat] Agent initialized")
            except Exception as e:
                logger.error(f"[AdminChat] Failed to initialize agent: {e}", exc_info=True)
                raise
    
    def _is_directed_at_bot(self, message: discord.Message) -> bool:
        """Check if a message is directed at the bot (mention, reply, or DM)."""
        if message.author.bot:
            return False
        if not message.content.strip():
            return False

        # DMs always count
        if isinstance(message.channel, discord.DMChannel):
            return True

        # In public channels, respond if the bot is directly @mentioned (not @everyone/@here)
        if self.bot.user and self.bot.user.mentioned_in(message) and not message.mention_everyone:
            return True

        # Also respond if replying to one of the bot's messages
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and ref.author.id == self.bot.user.id:
                return True

        return False

    def _is_admin(self, user_id: int) -> bool:
        """Check if a user is the admin."""
        return self.admin_user_id is not None and user_id == self.admin_user_id

    async def _can_user_message_bot(self, user_id: int) -> bool:
        """Check members.can_message_bot with a short in-memory cache."""
        now = time.monotonic()
        cached = self._message_access_cache.get(user_id)
        if cached and now - cached[0] < self._ACCESS_CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_supabase()
        if client is None:
            return False

        result = await asyncio.to_thread(
            client.table('members')
            .select('can_message_bot')
            .eq('member_id', user_id)
            .limit(1)
            .execute
        )
        allowed = bool(result.data and result.data[0].get('can_message_bot'))
        self._message_access_cache[user_id] = (now, allowed)
        return allowed

    async def _resolve_context_guild_id(self, user_id: int, guild_hint: int | None = None) -> int | None:
        """Resolve a trusted guild context for the requester."""
        if guild_hint is not None:
            server_config = getattr(self.db_handler, 'server_config', None)
            if self.bot.get_guild(guild_hint) is None:
                return None
            if server_config and not server_config.is_guild_enabled(guild_hint):
                return None
            return guild_hint

        now = time.monotonic()
        cached = self._guild_context_cache.get(user_id)
        if cached and now - cached[0] < self._ACCESS_CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_supabase()
        if client is None:
            return None

        result = await asyncio.to_thread(
            client.table('guild_members')
            .select('guild_id')
            .eq('member_id', user_id)
            .execute
        )
        server_config = getattr(self.db_handler, 'server_config', None)
        guild_ids = sorted({
            int(row['guild_id'])
            for row in (result.data or [])
            if row.get('guild_id') is not None
            and self.bot.get_guild(int(row['guild_id'])) is not None
            and (server_config is None or server_config.is_guild_enabled(int(row['guild_id'])))
        })
        if not guild_ids:
            resolved = None
        else:
            default_guild_id = server_config.get_default_guild_id(require_write=False) if server_config else None
            resolved = default_guild_id if default_guild_id in guild_ids else guild_ids[0]
            if len(guild_ids) > 1:
                logger.info(f"[AdminChat] Resolved DM guild for {user_id} to {resolved} from {guild_ids}")

        self._guild_context_cache[user_id] = (now, resolved)
        return resolved

    def _is_rate_limited(self, user_id: int) -> bool:
        """Apply a simple sliding-window limit for non-admin users."""
        now = time.monotonic()
        bucket = self._rate_limits.setdefault(user_id, deque())
        while bucket and now - bucket[0] > self._RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= self._RATE_LIMIT_MAX_MESSAGES:
            return True
        bucket.append(now)
        return False

    def _strip_mention(self, content: str) -> str:
        """Remove the bot @mention from message content."""
        if self.bot.user:
            content = content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '')
        return content.strip()

    _ABORT_PHRASES = {'stop', 'abort', 'cancel', 'halt', 'nevermind', 'never mind', 'quit', 'enough'}

    def _is_abort(self, content: str) -> bool:
        """Check if the message is an abort request."""
        normalised = content.strip().lower().rstrip('!.')
        return normalised in self._ABORT_PHRASES

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle admin chat and approved member bot requests."""

        if not self._is_directed_at_bot(message):
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_admin = self._is_admin(message.author.id)
        content = message.content if is_dm else self._strip_mention(message.content)

        if not content:
            return

        resolved_guild_id = message.guild.id if message.guild else None
        if not is_admin:
            if not await self._can_user_message_bot(message.author.id):
                return
            resolved_guild_id = await self._resolve_context_guild_id(message.author.id, resolved_guild_id)
            if resolved_guild_id is None:
                logger.info(f"[AdminChat] No guild context for approved member {message.author.id}")
                return
            if self._is_rate_limited(message.author.id):
                await message.reply("Slow down a bit. Try again in a few minutes.")
                return

        user_id = message.author.id
        source = "DM" if is_dm else f"#{getattr(message.channel, 'name', 'unknown')}"
        role = "admin" if is_admin else "member"
        logger.info(f"[AdminChat] Received from {role} in {source}: {content[:50]}...")

        # If agent is busy, check if this is an abort or queue it
        if self._busy.get(user_id):
            if self._is_abort(content):
                if self.agent:
                    self.agent.request_abort(user_id)
                    logger.info(f"[AdminChat] Abort requested by user {user_id}")
                await message.add_reaction("\u23f9\ufe0f")  # stop button emoji
                return
            else:
                # Queue the message to process after current run finishes
                self._pending_messages[user_id] = message
                return

        try:
            # Initialize agent if needed
            self._ensure_agent()

            # Build channel context for non-DM messages
            channel_context = None
            if is_dm:
                guild = self.bot.get_guild(resolved_guild_id) if resolved_guild_id else None
                channel_context = {
                    "source": "dm",
                    "guild_id": str(resolved_guild_id) if resolved_guild_id else None,
                    "guild_name": guild.name if guild else None,
                }
            else:
                ch = message.channel
                channel_context = {
                    "guild_id": str(resolved_guild_id),
                    "channel_id": str(ch.id),
                    "channel_name": getattr(ch, 'name', 'unknown'),
                }
                # If it's a thread, include parent info
                if isinstance(ch, discord.Thread) and ch.parent:
                    channel_context["is_thread"] = True
                    channel_context["parent_channel_id"] = str(ch.parent_id)
                    channel_context["parent_channel_name"] = ch.parent.name

                # If replying to a message, include it
                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message):
                        channel_context["replied_to"] = {
                            "message_id": str(ref.id),
                            "author": ref.author.display_name,
                            "content": (ref.content or '')[:500],
                        }

                # Grab recent messages for surrounding context
                try:
                    recent = []
                    async for msg in ch.history(limit=10):
                        if msg.id == message.id:
                            continue
                        recent.append(f"[{msg.id}] {msg.author.display_name}: {(msg.content or '')[:150]}")
                    recent.reverse()
                    channel_context["recent_messages"] = recent
                except Exception:
                    pass

            # Mark busy and run agent
            self._busy[user_id] = True
            try:
                responses = await self.agent.chat(
                    user_id=user_id,
                    user_message=content,
                    channel_context=channel_context,
                    channel=message.channel,
                    is_admin=is_admin,
                    requester_id=None if is_admin else user_id,
                )
            finally:
                self._busy[user_id] = False

            # responses is a list of messages, or None if ended without reply
            if responses is None:
                logger.info("[AdminChat] Turn ended without reply (silent action)")
                return

            # Send each response message
            total_chars = 0
            messages_sent = 0

            # In public channels, reply to the original message for the first response
            reply_ref = message if not is_dm else None

            for response in responses:
                # Skip empty responses
                if not response or not response.strip():
                    continue

                # Split on ---SPLIT--- marker for proper media embedding
                # Each part becomes a separate Discord message
                parts = response.split('\n---SPLIT---\n')

                for part in parts:
                    part = part.strip()
                    if not part:
                        continue

                    total_chars += len(part)

                    # Handle long messages by splitting
                    if len(part) <= 2000:
                        await message.channel.send(part, reference=reply_ref)
                        messages_sent += 1
                    else:
                        # Split into chunks
                        chunks = [part[i:i+1990] for i in range(0, len(part), 1990)]
                        for chunk in chunks:
                            if chunk.strip():
                                await message.channel.send(chunk, reference=reply_ref)
                                messages_sent += 1

                    # Only reply-thread the first message
                    reply_ref = None

            logger.info(f"[AdminChat] Sent {messages_sent} message(s) ({total_chars} chars total)")

        except Exception as e:
            logger.error(f"[AdminChat] Error processing message: {e}", exc_info=True)
            await message.channel.send(f"Sorry, I encountered an error: {str(e)}")

        # Process any message that arrived while we were busy
        pending = self._pending_messages.pop(user_id, None)
        if pending:
            logger.info(f"[AdminChat] Processing queued message from {user_id}")
            await self.on_message(pending)
    
    @commands.command(name='adminchat_clear')
    @commands.is_owner()
    async def clear_history(self, ctx: commands.Context):
        """Clear the admin chat conversation history."""
        if self.agent:
            self.agent.clear_conversation(ctx.author.id)
            await ctx.send("Conversation history cleared.")
        else:
            await ctx.send("Agent not initialized.")


async def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    # These will be passed from main.py
    db_handler = getattr(bot, 'db_handler', None)
    sharer = getattr(bot, 'sharer', None)
    
    if db_handler is None or sharer is None:
        logger.error("[AdminChat] Cannot setup cog - db_handler or sharer not found on bot")
        return
    
    await bot.add_cog(AdminChatCog(bot, db_handler, sharer))
    logger.info("[AdminChat] Cog loaded")
