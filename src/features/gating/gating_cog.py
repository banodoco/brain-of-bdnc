import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from src.common.llm import get_llm_response
from src.common.soul import BOT_VOICE

logger = logging.getLogger('DiscordBot')

# ── Prompt used by Haiku to review new introductions ──

_INTRO_REVIEW_PROMPT = """\
You are a friendly greeter bot for Banodoco, an open-source AI art community on Discord. \
A new member has posted a message in the introductions channel. Your job is to welcome \
them if they've written a real intro, or ask them to try again if they haven't.

Everyone is welcome here — but this channel is for real introductions, not drive-by \
one-liners. The bar isn't high, but it exists.

{bot_voice}

## What makes a good intro

A good intro tells the community something specific about the person. It includes at \
least one of:
- A specific project, tool, or technique they're working with or exploring
- Links to their work, portfolio, social media, GitHub, etc.
- Images or videos of things they've made
- Enough concrete detail that you could distinguish them from any other newcomer

"I'm into AI art" is not an intro. "I've been training LoRAs for stylised portraits \
using Kohya and just started experimenting with Wan" is.

## What to do

Respond with exactly one of three actions on the first line, then your message (if any) \
after a blank line:

KEEP
(a short, warm, personal welcome)

Use KEEP for: intros with real substance — they say something specific about who they \
are or what they do. Write a brief personal reply (1-2 sentences) that references \
something from their intro. If the message has no links and no media attachments, \
encourage them to share their work — "the community would love to see what you've been \
making" — but don't make it sound required.

FEEDBACK
(reply to post in the channel)

Use FEEDBACK for: intros that show some effort but are too vague to act on — generic \
interest statements without specifics, or a couple of buzzwords with no substance. \
Write a warm 2-3 sentence reply. Welcome them, then ask for something concrete: what \
tools they use, what they're building, a link to their work. Frame it as "we'd love to \
know more" not "you failed."

DELETE
(message to show the user briefly before their intro is removed)

Use DELETE for: messages that aren't introductions at all. This includes:
- Spam, ads, completely off-topic messages
- Bare greetings ("hi", "hello", single emoji)
- Random questions not about themselves
- Generic one-liners that say nothing specific ("here to explore AI's potential", \
"interested in AI art and video generation")

If someone wrote words but said nothing meaningful about themselves — no specifics, \
no links, no media, no projects — that's a DELETE. Keep the message short and friendly: \
tell them what a good intro looks like and invite them to try again."""

class GatingCog(commands.Cog):
    """
    Gated entry system: new members post intros, approvers react, bot grants Speaker role.

    Flow:
      1. on_member_join     → temp welcome ping in gate channel (auto-deleted after 5 min)
      2. on_message          → track intro, Haiku reviews first message (DELETE / FEEDBACK / KEEP)
      3. on_raw_reaction_add → approver reacts on any tracked message → _approve_member
      4. _approve_member     → grant Speaker role, ✅ reaction, DM the member

    Cleanup:
      - on_raw_message_delete  → remove from tracking, expire DB if no messages left
      - cleanup_expired_intros → expire pending intros older than 7 days
      - scan_intro_channels    → backfill _pending_messages from channel history on startup
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)

        # message_id → member_id for all intro messages from pending members
        self._pending_messages: dict[int, int] = {}

        # Temp gate-channel welcome pings awaiting deletion: {message_id: (channel_id, sent_at)}
        self._temp_welcomes: dict[int, tuple[int, datetime]] = {}

    # ── Config helpers ──

    def _get_guild_config(self, guild_id: int) -> dict:
        """Resolve gating config for a guild from server_config."""
        sc = getattr(self.db, 'server_config', None) if self.db else None
        server = sc.get_server(guild_id) if sc else None
        cfg = {}
        for key in (
            'gate_channel_id', 'intro_channel_id', 'speaker_role_id',
            'approver_role_id', 'super_approver_role_id', 'welcome_channel_id',
        ):
            val = server.get(key) if server else None
            cfg[key] = int(val) if val is not None else None
        return cfg

    def _get_gating_config(self, guild_id: int) -> dict | None:
        """Return guild config if gating is fully configured, else None."""
        cfg = self._get_guild_config(guild_id)
        required = ('gate_channel_id', 'intro_channel_id', 'speaker_role_id',
                     'approver_role_id', 'super_approver_role_id')
        return cfg if all(cfg.get(k) for k in required) else None

    # ── Lifecycle ──

    async def cog_load(self):
        if not self.db:
            return
        try:
            rows = self.db.get_all_pending_intros()
            self._pending_messages = {row['message_id']: row['member_id'] for row in rows}
            logger.info(f"GatingCog: loaded {len(self._pending_messages)} pending intros from DB")
        except Exception as e:
            logger.error(f"GatingCog: failed to load pending intros: {e}", exc_info=True)
        self.scan_intro_channels.start()
        self.cleanup_expired_intros.start()
        self.cleanup_temp_welcomes.start()

    async def cog_unload(self):
        self.scan_intro_channels.cancel()
        self.cleanup_expired_intros.cancel()
        self.cleanup_temp_welcomes.cancel()

    # ═══════════════════════════════════════════════════════════════
    #  1. New member joins → temp welcome in gate channel
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.db:
            return
        cfg = self._get_gating_config(member.guild.id)
        if not cfg:
            return
        speaker_role = member.guild.get_role(cfg['speaker_role_id'])
        if speaker_role and speaker_role in member.roles:
            return
        channel = member.guild.get_channel(cfg['gate_channel_id'])
        if not channel:
            return
        try:
            # Reply to the bot's pinned welcome message if found
            reference = None
            async for hist_msg in channel.history(limit=50, oldest_first=True):
                if hist_msg.author.id == self.bot.user.id:
                    reference = hist_msg
                    break
            msg = await channel.send(
                f"Hi {member.mention}, welcome! If you'd like to speak, see the message above \U0001f446",
                reference=reference,
            )
            self._temp_welcomes[msg.id] = (channel.id, msg.created_at)
        except Exception as e:
            logger.error(f"GatingCog: failed to send gate welcome for {member.id}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════
    #  2. Member posts intro → track + Haiku review (first msg only)
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.db or message.author.bot or not message.guild:
            return
        guild_id = message.guild.id
        cfg = self._get_gating_config(guild_id)
        if not cfg:
            return
        if message.channel.id != cfg['intro_channel_id']:
            return

        # Ignore replies to other people (conversations, not intros)
        if (message.reference and message.reference.resolved
                and getattr(message.reference.resolved, 'author', None)
                and message.reference.resolved.author.id != message.author.id):
            return

        # Only track non-Speakers
        speaker_role = message.guild.get_role(cfg['speaker_role_id'])
        if not speaker_role or speaker_role in message.author.roles:
            return

        # Track this message so any reaction on it can trigger approval
        existing = self.db.get_pending_intro_by_member(message.author.id, guild_id=guild_id)
        if existing:
            self.db.update_pending_intro_message(existing['id'], message.id, message.channel.id)
            logger.info(f"GatingCog: updated pending intro for {message.author} -> msg {message.id}")
        else:
            if not self.db.create_pending_intro(message.author.id, message.id, message.channel.id, guild_id=guild_id):
                return
            logger.info(f"GatingCog: tracked intro from {message.author} (msg {message.id})")
        self._pending_messages[message.id] = message.author.id

        # Haiku review on first message only
        if not existing:
            asyncio.create_task(self._review_intro(message))

    async def _review_intro(self, message: discord.Message):
        """Ask Haiku to review an intro: DELETE / FEEDBACK / KEEP."""
        try:
            has_url = bool(re.search(r'https?://\S+', message.content))
            has_media = bool(message.attachments)

            response = await get_llm_response(
                client_name="claude",
                model="claude-opus-4-6",
                system_prompt=_INTRO_REVIEW_PROMPT.format(bot_voice=BOT_VOICE),
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

            # Guard: member may have been approved while Haiku was thinking
            if message.id not in self._pending_messages:
                logger.info(f"GatingCog: skipping Haiku action for {message.author} (already resolved)")
                return

            if action == 'KEEP':
                if body:
                    await message.reply(body, mention_author=True, delete_after=300)
                logger.info(f"GatingCog: Haiku welcomed {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'DELETE':
                self._pending_messages.pop(message.id, None)
                member_id = message.author.id
                if not any(m == member_id for m in self._pending_messages.values()):
                    intro = self.db.get_pending_intro_by_member(member_id, guild_id=message.guild.id)
                    if intro:
                        self.db.expire_pending_intro(intro['message_id'], guild_id=message.guild.id)
                try:
                    await message.delete()
                    if body:
                        hint = await message.channel.send(f"{message.author.mention} {body}")
                        await asyncio.sleep(15)
                        await hint.delete()
                except Exception as e:
                    logger.error(f"GatingCog: failed to delete intro from {message.author}: {e}")
                logger.info(f"GatingCog: Haiku deleted intro from {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'FEEDBACK':
                if body:
                    await message.reply(body, mention_author=True, delete_after=300)
                logger.info(f"GatingCog: Haiku sent feedback to {message.author}: {body[:200] if body else '(no body)'}")

            else:
                logger.warning(f"GatingCog: unexpected Haiku response for {message.author}: {response[:100]}")
        except Exception as e:
            logger.error(f"GatingCog: failed to review intro from {message.author}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════
    #  3. Approver reacts → approve member
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not self.db:
            return
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

        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return
        reactor_role_ids = {r.id for r in reactor.roles}
        is_approver = cfg['approver_role_id'] in reactor_role_ids
        is_super = cfg.get('super_approver_role_id') in reactor_role_ids if cfg.get('super_approver_role_id') else False
        if not is_approver and not is_super:
            logger.info(f"GatingCog: reactor {reactor} lacks approver role, ignoring")
            return

        intro = self.db.get_pending_intro_by_member(member_id, guild_id=payload.guild_id)
        if not intro:
            self._remove_member_messages(member_id)
            return

        voter_role = 'super_approver' if is_super else 'approver'
        self.db.record_intro_vote(intro['id'], payload.message_id, payload.user_id, voter_role, guild_id=payload.guild_id)
        await self._approve_member(guild, intro, cfg, reacted_message_id=payload.message_id)

    # ═══════════════════════════════════════════════════════════════
    #  4. Approve: grant role, ✅, DM
    # ═══════════════════════════════════════════════════════════════

    async def _approve_member(self, guild: discord.Guild, intro: dict, cfg: dict,
                              reacted_message_id: int | None = None):
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

            if reacted_message_id:
                try:
                    channel = guild.get_channel(intro['channel_id'])
                    if channel:
                        msg = await channel.fetch_message(reacted_message_id)
                        await msg.add_reaction('\u2705')
                except Exception as e:
                    logger.error(f"GatingCog: failed to add checkmark to {reacted_message_id}: {e}")

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

    # ═══════════════════════════════════════════════════════════════
    #  Message deletion → cleanup tracking
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        member_id = self._pending_messages.pop(payload.message_id, None)
        if member_id is None:
            return
        if not any(m == member_id for m in self._pending_messages.values()):
            intro = self.db.get_pending_intro_by_member(member_id, guild_id=getattr(payload, 'guild_id', None))
            if intro:
                self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
                logger.info(f"GatingCog: expired pending intro for member {member_id} (all messages deleted)")
        else:
            logger.info(f"GatingCog: removed deleted message {payload.message_id} from tracking for member {member_id}")

    def _remove_member_messages(self, member_id: int):
        """Remove all in-memory tracked messages for a member."""
        to_remove = [mid for mid, m in self._pending_messages.items() if m == member_id]
        for mid in to_remove:
            del self._pending_messages[mid]

    # ═══════════════════════════════════════════════════════════════
    #  Background tasks
    # ═══════════════════════════════════════════════════════════════

    @tasks.loop(count=1)
    async def scan_intro_channels(self):
        """Backfill _pending_messages from channel history on startup."""
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

    @scan_intro_channels.before_loop
    async def before_scan_intro_channels(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def cleanup_expired_intros(self):
        """Expire and delete pending intros older than 3 days."""
        if not self.db:
            return
        expired = self.db.get_expired_pending_intros(expiry_days=3)
        if not expired:
            return
        logger.info(f"GatingCog: expiring {len(expired)} intro(s)")
        for intro in expired:
            # Delete the message from Discord
            await self._delete_intro_message(intro)
            self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
            self._remove_member_messages(intro['member_id'])

    async def _delete_intro_message(self, intro: dict):
        """Try to delete an intro message from Discord."""
        try:
            guild = self.bot.get_guild(intro.get('guild_id')) if intro.get('guild_id') else None
            if not guild:
                return
            channel = guild.get_channel(intro['channel_id'])
            if not channel:
                return
            msg = await channel.fetch_message(intro['message_id'])
            await msg.delete()
            logger.info(f"GatingCog: deleted expired intro message {intro['message_id']} from {msg.author}")
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error(f"GatingCog: failed to delete intro message {intro['message_id']}: {e}")

    @cleanup_expired_intros.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()
        # One-time: clean up old stale intros from before this feature existed
        await self._cleanup_old_stale_intros()

    async def _cleanup_old_stale_intros(self):
        """One-time scan: delete intro-channel messages from non-speakers older than 3 days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        for guild in self.bot.guilds:
            cfg = self._get_gating_config(guild.id)
            if not cfg:
                continue
            channel = guild.get_channel(cfg['intro_channel_id'])
            if not channel:
                continue
            speaker_role = guild.get_role(cfg['speaker_role_id'])
            if not speaker_role:
                continue
            deleted = 0
            try:
                async for msg in channel.history(limit=500, before=cutoff):
                    if msg.author.bot:
                        continue
                    # If they still don't have Speaker role, delete
                    member = guild.get_member(msg.author.id)
                    if member and speaker_role in member.roles:
                        continue
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.error(f"GatingCog: failed to delete stale intro {msg.id}: {e}")
            except Exception as e:
                logger.error(f"GatingCog: failed to scan intro channel for stale messages: {e}")
            if deleted:
                logger.info(f"GatingCog: cleaned up {deleted} stale intro(s) in {guild.name}")

    TEMP_WELCOME_TTL = timedelta(minutes=5)

    @tasks.loop(minutes=1)
    async def cleanup_temp_welcomes(self):
        """Delete gate-channel welcome pings older than 5 minutes."""
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

        # One-time scan on startup for orphaned welcome pings from previous runs
        if not self._startup_scan_done:
            self._startup_scan_done = True
            for guild in self.bot.guilds:
                cfg = self._get_guild_config(guild.id)
                cid = cfg.get('gate_channel_id')
                if not cid:
                    continue
                channel = self.bot.get_channel(cid)
                if not channel:
                    continue
                deleted = 0
                try:
                    async for msg in channel.history(limit=50):
                        if (msg.author.id == self.bot.user.id
                                and "welcome! If you'd like to speak" in msg.content
                                and now - msg.created_at >= self.TEMP_WELCOME_TTL):
                            try:
                                await msg.delete()
                                deleted += 1
                            except discord.NotFound:
                                pass
                except Exception as e:
                    logger.error(f"GatingCog: failed to scan gate channel {cid} for orphaned welcomes: {e}")
                if deleted:
                    logger.info(f"GatingCog: cleaned up {deleted} orphaned welcome(s) in {guild.name}")

    @cleanup_temp_welcomes.before_loop
    async def before_cleanup_temp_welcomes(self):
        await self.bot.wait_until_ready()
        self._startup_scan_done = False
