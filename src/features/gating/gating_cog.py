import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

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

    async def cog_unload(self):
        self.cleanup_expired_intros.cancel()
        self.cleanup_temp_welcomes.cancel()

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

        # Delete short messages unless they're a reply to someone else
        MIN_INTRO_LENGTH = 50
        if len(message.content) < MIN_INTRO_LENGTH:
            is_reply_to_other = (
                message.reference
                and message.reference.message_id
                and message.reference.resolved
                and getattr(message.reference.resolved, 'author', None)
                and message.reference.resolved.author.id != message.author.id
            )
            if not is_reply_to_other:
                try:
                    await message.delete()
                    hint = await message.channel.send(
                        f"{message.author.mention} Proper introductions only! Try to mention:\n\n"
                        f"• Things you've made or contributed to the space\n"
                        f"• Why you're passionate about open-source AI art\n"
                        f"• What you're working on or want to contribute"
                    )
                    await asyncio.sleep(10)
                    await hint.delete()
                    logger.info(f"GatingCog: deleted short message from {message.author} in intros ({len(message.content)} chars)")
                except Exception as e:
                    logger.error(f"GatingCog: failed to delete short message: {e}")
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

        # Nudge if no URL, image, or video attached (only on first intro)
        if not existing:
            has_url = bool(re.search(r'https?://\S+', message.content))
            has_media = any(
                a.content_type and (a.content_type.startswith('image/') or a.content_type.startswith('video/'))
                for a in message.attachments
            )
            if not has_url and not has_media:
                try:
                    await message.reply(
                        "Thanks for introducing yourself! Consider editing your intro or replying "
                        "with a link to your work or an image/video of something you've made "
                        "\u2014 it helps approvers get to know you faster.",
                        mention_author=True,
                        delete_after=120,
                    )
                except Exception as e:
                    logger.error(f"GatingCog: failed to send media nudge: {e}")

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

            await self._send_speaker_welcome(guild, member, cfg)
        except Exception as e:
            logger.error(f"GatingCog: failed to approve {member}: {e}", exc_info=True)

    async def _send_speaker_welcome(self, guild: discord.Guild, member: discord.Member, cfg: dict):
        """Post a welcome in the getting-started channel."""
        welcome_channel_id = cfg.get('welcome_channel_id')
        if not welcome_channel_id:
            return
        channel = guild.get_channel(welcome_channel_id)
        if not channel:
            return
        try:
            reference = None
            async for hist_msg in channel.history(limit=50, oldest_first=True):
                if hist_msg.author.id == self.bot.user.id:
                    reference = hist_msg
                    break

            msg = await channel.send(
                f"Welcome {member.mention}! You now have Speaker access. "
                f"Check out the message above to get started \U0001f446",
                reference=reference,
            )
            self._temp_welcomes[msg.id] = (channel.id, msg.created_at)
        except Exception as e:
            logger.error(f"GatingCog: failed to send speaker welcome for {member.id}: {e}", exc_info=True)

    # ========== Task Loops ==========

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

    # ---- Temp welcome cleanup ----

    _TEMP_WELCOME_PATTERNS = (
        "welcome! If you'd like to speak",
        "You now have Speaker access",
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
