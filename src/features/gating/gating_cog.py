import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone

import discord
from discord.ext import commands, tasks
from src.common.llm import get_llm_response

logger = logging.getLogger('DiscordBot')


class GatingCog(commands.Cog):
    """Gated entry system: new members post intros, approvers react, bot grants Speaker role."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)

        sc = getattr(self.db, 'server_config', None) if self.db else None
        configured_servers = sc.get_enabled_servers(require_write=True) if sc else []
        self.any_configured = any(
            all([
                server.get('gate_channel_id'),
                server.get('intro_channel_id'),
                server.get('speaker_role_id'),
                server.get('approver_role_id'),
                server.get('super_approver_role_id'),
            ])
            for server in configured_servers
        )
        if not self.any_configured:
            logger.warning("GatingCog: no fully configured writable guilds in server_config")

        # message_id -> member_id for all intro messages from pending members
        self._pending_messages: dict[int, int] = {}

        # Temp welcome messages pending deletion: {message_id: (channel_id, sent_at)}
        self._temp_welcomes: dict[int, tuple[int, datetime]] = {}

        # Previous daily new speakers message per guild: {guild_id: (channel_id, message_id)}
        self._last_daily_speakers_msg: dict[int, tuple[int, int]] = {}

    def _get_guild_config(self, guild_id: int) -> dict:
        """Resolve gating config for a guild from server_config."""
        sc = getattr(self.db, 'server_config', None) if self.db else None
        server = sc.get_server(guild_id) if sc else None

        # Build per-field with explicit DB key -> env key mapping
        cfg = {}
        field_names = [
            'gate_channel_id',
            'intro_channel_id',
            'speaker_role_id',
            'approver_role_id',
            'super_approver_role_id',
            'welcome_channel_id',
        ]
        for db_key in field_names:
            val = server.get(db_key) if server else None
            cfg[db_key] = int(val) if val is not None else None
        return cfg

    def _guild_has_gating(self, guild_id: int) -> bool:
        """Check if this guild has gating configured (gate + intro + speaker + approver)."""
        cfg = self._get_guild_config(guild_id)
        return all([
            cfg.get('gate_channel_id'),
            cfg.get('intro_channel_id'),
            cfg.get('speaker_role_id'),
            cfg.get('approver_role_id'),
            cfg.get('super_approver_role_id'),
        ])

    async def cog_load(self):
        """Populate in-memory pending set from DB on startup."""
        if not self.db:
            return
        try:
            rows = self.db.get_all_pending_intros()
            self._pending_messages = {row['message_id']: row['member_id'] for row in rows}
            logger.info(f"GatingCog: loaded {len(self._pending_messages)} pending intros from DB")
        except Exception as e:
            logger.error(f"GatingCog: failed to load pending intros: {e}", exc_info=True)
        self.cleanup_expired_intros.start()
        self.cleanup_temp_welcomes.start()
        self.scan_intro_channels.start()
        self.daily_new_speakers.start()

    async def cog_unload(self):
        self.cleanup_expired_intros.cancel()
        self.cleanup_temp_welcomes.cancel()
        self.scan_intro_channels.cancel()
        self.daily_new_speakers.cancel()

    # ========== Listeners ==========

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Ping the new member in the gate channel, then delete after 5s."""
        if not self.db:
            return
        cfg = self._get_guild_config(member.guild.id)
        if not cfg.get('gate_channel_id') or not cfg.get('speaker_role_id'):
            return
        # Don't welcome members who already have Speaker role (e.g. rejoining)
        speaker_role = member.guild.get_role(cfg['speaker_role_id'])
        if speaker_role and speaker_role in member.roles:
            return
        channel = member.guild.get_channel(cfg['gate_channel_id'])
        if not channel:
            return
        try:
            reference = None
            async for hist_msg in channel.history(limit=50, oldest_first=True):
                if hist_msg.author.id == self.bot.user.id:
                    reference = hist_msg
                    break

            msg = await channel.send(
                f"Hi {member.mention}, welcome! If you'd like to speak, see the message above 👆",
                reference=reference,
            )
            self._temp_welcomes[msg.id] = (channel.id, msg.created_at)
        except Exception as e:
            logger.error(f"GatingCog: failed to send gate welcome for {member.id}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Track intro messages from non-Speakers in the intro channel."""
        if not self.db or message.author.bot or not message.guild:
            return
        guild_id = message.guild.id
        if not self._guild_has_gating(guild_id):
            return
        cfg = self._get_guild_config(guild_id)

        if message.channel.id != cfg['intro_channel_id']:
            return

        # Ignore replies to other people (conversations, not intros)
        is_reply_to_other = (
            message.reference
            and message.reference.message_id
            and message.reference.resolved
            and getattr(message.reference.resolved, 'author', None)
            and message.reference.resolved.author.id != message.author.id
        )
        if is_reply_to_other:
            return

        # Only track non-Speakers
        speaker_role = message.guild.get_role(cfg['speaker_role_id'])
        if not speaker_role:
            return
        if speaker_role in message.author.roles:
            return

        # Track this message for reaction-based approval
        existing = self.db.get_pending_intro_by_member(message.author.id, guild_id=guild_id)
        if existing:
            # Update DB record to point to the latest message
            self.db.update_pending_intro_message(existing['id'], message.id, message.channel.id)
            logger.info(f"GatingCog: updated pending intro for {message.author} -> msg {message.id}")
        else:
            if not self.db.create_pending_intro(message.author.id, message.id, message.channel.id, guild_id=guild_id):
                return
            logger.info(f"GatingCog: tracked intro from {message.author} (msg {message.id})")
        # Track all intro messages from this member so any can trigger approval
        self._pending_messages[message.id] = message.author.id

        # Ask Haiku to review the intro (only on first message from this member)
        if not existing:
            asyncio.create_task(self._review_intro(message))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Check if reaction on a pending intro meets the approval threshold."""
        if not self.db:
            return

        # Fast path: skip if not a pending intro message
        member_id = self._pending_messages.get(payload.message_id)
        if member_id is None:
            return

        logger.info(f"GatingCog: reaction {payload.emoji} on pending intro {payload.message_id} by user {payload.user_id}")

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        cfg = self._get_guild_config(payload.guild_id)
        if not cfg.get('approver_role_id'):
            return

        # Check reactor has Approver or Super Approver role
        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return

        reactor_role_ids = {r.id for r in reactor.roles}
        is_approver = cfg['approver_role_id'] in reactor_role_ids
        is_super = cfg.get('super_approver_role_id') in reactor_role_ids if cfg.get('super_approver_role_id') else False
        if not is_approver and not is_super:
            logger.info(f"GatingCog: reactor {reactor} lacks approver role, ignoring")
            return

        # Look up pending intro by member (works regardless of which message was reacted to)
        intro = self.db.get_pending_intro_by_member(member_id, guild_id=payload.guild_id)
        if not intro:
            # Clean up stale in-memory entries for this member
            self._remove_member_messages(member_id)
            return

        # Record the vote
        voter_role = 'super_approver' if is_super else 'approver'
        self.db.record_intro_vote(intro['id'], payload.message_id, payload.user_id, voter_role, guild_id=payload.guild_id)

        # 1 vote from either role is enough
        await self._approve_member(guild, intro, cfg, reacted_message_id=payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Clean up pending intro tracking when a tracked message is deleted."""
        member_id = self._pending_messages.pop(payload.message_id, None)
        if member_id is None:
            return

        # If no other tracked messages remain for this member, remove the DB record too
        has_other = any(mid for mid, mid_member in self._pending_messages.items() if mid_member == member_id)
        if not has_other:
            intro = self.db.get_pending_intro_by_member(member_id, guild_id=getattr(payload, 'guild_id', None))
            if intro:
                self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
                logger.info(f"GatingCog: expired pending intro for member {member_id} (all messages deleted)")
        else:
            logger.info(f"GatingCog: removed deleted message {payload.message_id} from tracking for member {member_id}")

    def _remove_member_messages(self, member_id: int):
        """Remove all in-memory tracked messages for a member."""
        to_remove = [mid for mid, mid_member in self._pending_messages.items() if mid_member == member_id]
        for mid in to_remove:
            del self._pending_messages[mid]

    _INTRO_REVIEW_PROMPT = """\
You are a greeter bot for Banodoco, an open-source AI art community on Discord. \
A new member has posted a message in the introductions channel. Your job is to \
decide what to do with it and optionally respond.

## Community standards

This community is for people actively contributing to open-source AI art:
- Making art and pushing creative boundaries with AI tools
- Contributing to open-source projects (code, models, workflows, nodes, etc.)
- Sharing what they're learning publicly — notes, tutorials, breakdowns
- Helping others learn and grow

## What makes a good intro

A good intro shows the person is relevant to the community. It should touch on \
at least one or two of:
- What they've made, built, or contributed (art, code, workflows, models, etc.)
- What they're working on or want to work on
- Why they care about open-source AI art
- Links to their work, GitHub, portfolio, social media, etc.
- Images or videos of things they've made

It does NOT need to be long or formal. A few sentences showing genuine involvement is fine.

## What to do

Respond with exactly one of three actions on the first line, then your message (if any) after a blank line:

DELETE
(message to show the user briefly before their intro is removed)

Use DELETE for: spam, completely off-topic messages, very low effort messages that aren't \
introductions at all (e.g. "hi", "hello", single emoji, random questions). \
Keep your message short — tell them what an intro should include.

FEEDBACK
(reply to post in the channel)

Use FEEDBACK for: intros that show some effort but are vague or could be stronger. \
Write a short (2-3 sentence), warm reply suggesting what they could add. \
Don't be preachy — just the one or two most impactful improvements.

KEEP
(a short, warm, personal welcome thanking them for their intro)

Use KEEP for: intros that are good enough — they show the person is relevant \
and interested, even if not perfect. Err on the side of KEEP over FEEDBACK. \
Don't be overly demanding — a genuine, brief intro from someone who clearly \
does AI art is fine. Write a brief personal reply (1-2 sentences) that thanks them, \
references something specific from their intro, and lets them know the community \
will review it soon. Be genuine, not generic."""

    async def _review_intro(self, message: discord.Message):
        """Use Haiku to review an intro and decide: delete, give feedback, or keep."""
        try:
            has_url = bool(re.search(r'https?://\S+', message.content))
            has_media = bool(message.attachments)

            response = await get_llm_response(
                client_name="claude",
                model="claude-haiku-4-5-20251001",
                system_prompt=self._INTRO_REVIEW_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Introduction from {message.author.display_name}:\n\n"
                        f"{message.content}\n\n"
                        f"Has links: {has_url}\n"
                        f"Has media attachments: {has_media}"
                    ),
                }],
                max_tokens=300,
            )

            response = response.strip()
            action = response.split('\n')[0].strip().upper()
            body = '\n'.join(response.split('\n')[1:]).strip()

            # Check member wasn't already approved while Haiku was thinking
            if message.id not in self._pending_messages:
                logger.info(f"GatingCog: skipping Haiku action for {message.author} (already resolved)")
                return

            if action == 'KEEP':
                if body:
                    await message.reply(body, mention_author=True, delete_after=300)
                logger.info(f"GatingCog: Haiku welcomed {message.author}")
                return

            if action == 'DELETE':
                logger.info(f"GatingCog: Haiku deleted intro from {message.author}")
                # Remove from tracking since we're deleting it
                self._pending_messages.pop(message.id, None)
                member_id = message.author.id
                guild_id = message.guild.id
                has_other = any(mid for mid, m in self._pending_messages.items() if m == member_id)
                if not has_other:
                    intro = self.db.get_pending_intro_by_member(member_id, guild_id=guild_id)
                    if intro:
                        self.db.expire_pending_intro(intro['message_id'], guild_id=guild_id)
                try:
                    await message.delete()
                    if body:
                        hint = await message.channel.send(f"{message.author.mention} {body}")
                        await asyncio.sleep(15)
                        await hint.delete()
                except Exception as e:
                    logger.error(f"GatingCog: failed to delete intro from {message.author}: {e}")
                return

            if action == 'FEEDBACK':
                if body:
                    await message.reply(body, mention_author=True, delete_after=300)
                logger.info(f"GatingCog: Haiku sent feedback to {message.author}")
                return

            # Unexpected response — log and do nothing
            logger.warning(f"GatingCog: unexpected Haiku response for {message.author}: {response[:100]}")
        except Exception as e:
            logger.error(f"GatingCog: failed to review intro from {message.author}: {e}", exc_info=True)

    async def _approve_member(self, guild: discord.Guild, intro: dict, cfg: dict,
                              reacted_message_id: int | None = None):
        """Grant Speaker role and update DB."""
        member = guild.get_member(intro['member_id'])
        if not member:
            return

        speaker_role = guild.get_role(cfg['speaker_role_id'])
        if not speaker_role:
            return

        try:
            await member.add_roles(speaker_role, reason="Intro approved by community")
            self.db.approve_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
            self._remove_member_messages(intro['member_id'])
            logger.info(f"GatingCog: approved {member} (msg {intro['message_id']})")

            # Add green checkmark to the message that was reacted to
            if reacted_message_id:
                try:
                    channel = guild.get_channel(intro['channel_id'])
                    if channel:
                        msg = await channel.fetch_message(reacted_message_id)
                        await msg.add_reaction('\u2705')
                except Exception as e:
                    logger.error(f"GatingCog: failed to add checkmark to {reacted_message_id}: {e}")

            # DM the new speaker
            try:
                await member.send(
                    f"Hey {member.display_name}! You've been approved to speak in **{guild.name}**. "
                    f"Welcome aboard \U0001f389"
                )
            except discord.Forbidden:
                logger.info(f"GatingCog: couldn't DM {member} (DMs disabled)")
            except Exception as e:
                logger.error(f"GatingCog: failed to DM {member}: {e}")
        except Exception as e:
            logger.error(f"GatingCog: failed to approve {member}: {e}", exc_info=True)

    # ========== Task Loops ==========

    @tasks.loop(count=1)
    async def scan_intro_channels(self):
        """Scan intro channels to backfill all messages from pending members."""
        if not self.db or not self._pending_messages:
            return
        pending_member_ids = set(self._pending_messages.values())
        for guild in self.bot.guilds:
            cfg = self._get_guild_config(guild.id)
            intro_channel_id = cfg.get('intro_channel_id')
            speaker_role_id = cfg.get('speaker_role_id')
            if not intro_channel_id or not speaker_role_id:
                continue
            channel = guild.get_channel(intro_channel_id)
            if not channel:
                continue
            speaker_role = guild.get_role(speaker_role_id)
            found = 0
            try:
                async for msg in channel.history(limit=200):
                    if msg.author.bot or msg.author.id not in pending_member_ids:
                        continue
                    if speaker_role and speaker_role in msg.author.roles:
                        continue
                    if msg.id not in self._pending_messages:
                        self._pending_messages[msg.id] = msg.author.id
                        found += 1
            except Exception as e:
                logger.error(f"GatingCog: failed to scan intro channel {intro_channel_id}: {e}")
            if found:
                logger.info(f"GatingCog: found {found} additional messages from pending members in {guild.name}")
        logger.info(f"GatingCog: intro channel scan complete, tracking {len(self._pending_messages)} total messages")

        # Find previous daily speakers message so we can delete it when the next one posts
        for guild in self.bot.guilds:
            cfg = self._get_guild_config(guild.id)
            welcome_channel_id = cfg.get('welcome_channel_id')
            if not welcome_channel_id:
                continue
            channel = guild.get_channel(welcome_channel_id)
            if not channel:
                continue
            try:
                async for msg in channel.history(limit=50):
                    if msg.author.id == self.bot.user.id and self._NEW_SPEAKERS_PATTERN in msg.content:
                        self._last_daily_speakers_msg[guild.id] = (channel.id, msg.id)
                        break
            except Exception as e:
                logger.error(f"GatingCog: failed to scan for previous daily speakers msg: {e}")

    @scan_intro_channels.before_loop
    async def before_scan_intro_channels(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def cleanup_expired_intros(self):
        """Expire stale pending intros in DB."""
        if not self.db:
            return

        expired = self.db.get_expired_pending_intros(expiry_days=7)
        if not expired:
            return

        logger.info(f"GatingCog: expiring {len(expired)} intro(s)")
        for intro in expired:
            self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
            self._remove_member_messages(intro['member_id'])

    @cleanup_expired_intros.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ---- Daily new speakers announcement ----

    _NEW_SPEAKERS_PATTERN = "New speakers today"

    @tasks.loop(time=time(hour=19, minute=30, tzinfo=timezone.utc))
    async def daily_new_speakers(self):
        """Post a daily announcement tagging all newly approved speakers."""
        if not self.db:
            return

        for guild in self.bot.guilds:
            cfg = self._get_guild_config(guild.id)
            welcome_channel_id = cfg.get('welcome_channel_id')
            if not welcome_channel_id:
                continue
            channel = guild.get_channel(welcome_channel_id)
            if not channel:
                continue

            # Delete previous day's announcement
            prev = self._last_daily_speakers_msg.get(guild.id)
            if prev:
                try:
                    prev_channel = guild.get_channel(prev[0])
                    if prev_channel:
                        prev_msg = await prev_channel.fetch_message(prev[1])
                        await prev_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
                self._last_daily_speakers_msg.pop(guild.id, None)

            approved = self.db.get_recently_approved_intros(hours=24, guild_id=guild.id)
            if not approved:
                continue

            member_ids = {intro['member_id'] for intro in approved}
            mentions = []
            for mid in member_ids:
                member = guild.get_member(mid)
                if member:
                    mentions.append(member.mention)
            if not mentions:
                continue

            try:
                msg = await channel.send(
                    f"**New speakers today** — welcome {', '.join(mentions)}! "
                    f"\U0001f389"
                )
                self._last_daily_speakers_msg[guild.id] = (channel.id, msg.id)
                logger.info(f"GatingCog: posted daily new speakers ({len(mentions)}) in {guild.name}")
            except Exception as e:
                logger.error(f"GatingCog: failed to post daily new speakers in {guild.name}: {e}")

    @daily_new_speakers.before_loop
    async def before_daily_new_speakers(self):
        await self.bot.wait_until_ready()

    # ---- Temp welcome cleanup ----

    _TEMP_WELCOME_PATTERNS = (
        "welcome! If you'd like to speak",
    )
    TEMP_WELCOME_TTL = timedelta(minutes=5)

    @tasks.loop(minutes=1)
    async def cleanup_temp_welcomes(self):
        """Delete temporary welcome pings older than 5 minutes."""
        now = datetime.now(timezone.utc)

        expired_ids = [
            mid for mid, (_, sent_at) in self._temp_welcomes.items()
            if now - sent_at >= self.TEMP_WELCOME_TTL
        ]
        for mid in expired_ids:
            channel_id, _ = self._temp_welcomes.pop(mid)
            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"GatingCog: failed to delete temp welcome {mid}: {e}")

        if not self._startup_scan_done:
            self._startup_scan_done = True
            await self._scan_orphaned_welcomes()

    async def _scan_orphaned_welcomes(self):
        """Scan gate and welcome channels for bot welcome pings left behind by restarts."""
        now = datetime.now(timezone.utc)
        # Scan all guilds the bot is in that have gating configured
        channel_ids_to_scan: list[int] = []
        for guild in self.bot.guilds:
            cfg = self._get_guild_config(guild.id)
            for key in ('gate_channel_id', 'welcome_channel_id'):
                cid = cfg.get(key)
                if cid:
                    channel_ids_to_scan.append(cid)

        deleted = 0
        for cid in channel_ids_to_scan:
            channel = self.bot.get_channel(cid)
            if not channel:
                continue
            try:
                async for msg in channel.history(limit=50):
                    if msg.author.id != self.bot.user.id:
                        continue
                    if not any(p in msg.content for p in self._TEMP_WELCOME_PATTERNS):
                        continue
                    if now - msg.created_at >= self.TEMP_WELCOME_TTL:
                        try:
                            await msg.delete()
                            deleted += 1
                        except discord.NotFound:
                            pass
                        except Exception as e:
                            logger.error(f"GatingCog: failed to delete orphaned welcome {msg.id}: {e}")
            except Exception as e:
                logger.error(f"GatingCog: failed to scan channel {cid} for orphaned welcomes: {e}")
        if deleted:
            logger.info(f"GatingCog: cleaned up {deleted} orphaned welcome message(s)")

    @cleanup_temp_welcomes.before_loop
    async def before_cleanup_temp_welcomes(self):
        await self.bot.wait_until_ready()
        self._startup_scan_done = False
