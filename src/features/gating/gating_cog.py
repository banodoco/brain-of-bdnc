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

        # Config from env
        self.gate_channel_id = self._env_int('GATE_CHANNEL_ID')
        self.intro_channel_id = self._env_int('INTRO_CHANNEL_ID')
        self.speaker_role_id = self._env_int('SPEAKER_ROLE_ID')
        self.approver_role_id = self._env_int('APPROVER_ROLE_ID')
        self.super_approver_role_id = self._env_int('SUPER_APPROVER_ROLE_ID')
        self.welcome_channel_id = self._env_int('WELCOME_CHANNEL_ID')

        self.configured = all([
            self.gate_channel_id,
            self.intro_channel_id,
            self.speaker_role_id,
            self.approver_role_id,
            self.super_approver_role_id,
        ])
        if not self.configured:
            logger.warning("GatingCog: missing env vars, handlers will no-op")

        # In-memory set of pending intro message IDs for fast reaction filtering
        self._pending_message_ids: set[int] = set()

        # Temp welcome messages pending deletion: {message_id: (channel_id, sent_at)}
        self._temp_welcomes: dict[int, tuple[int, datetime]] = {}

    @staticmethod
    def _env_int(key: str) -> int | None:
        val = os.getenv(key)
        if val:
            try:
                return int(val)
            except ValueError:
                logger.warning(f"GatingCog: {key}={val!r} is not a valid int")
        return None

    async def cog_load(self):
        """Populate in-memory pending set from DB on startup."""
        if not self.configured or not self.db:
            return
        try:
            rows = self.db.get_all_pending_intros()
            self._pending_message_ids = {row['message_id'] for row in rows}
            logger.info(f"GatingCog: loaded {len(self._pending_message_ids)} pending intros from DB")
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
        if not self.configured:
            return
        # Don't welcome members who already have Speaker role (e.g. rejoining)
        speaker_role = member.guild.get_role(self.speaker_role_id)
        if speaker_role and speaker_role in member.roles:
            return
        channel = member.guild.get_channel(self.gate_channel_id)
        if not channel:
            return
        try:
            # Find the bot's welcome post to reply to
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
        if not self.configured or not self.db:
            return
        if message.author.bot:
            return
        if message.channel.id != self.intro_channel_id:
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
        speaker_role = message.guild.get_role(self.speaker_role_id)
        if not speaker_role:
            return
        if speaker_role in message.author.roles:
            return

        # Only first pending intro per member
        existing = self.db.get_pending_intro_by_member(message.author.id)
        if existing:
            return

        if self.db.create_pending_intro(message.author.id, message.id, message.channel.id):
            self._pending_message_ids.add(message.id)
            logger.info(f"GatingCog: tracked intro from {message.author} (msg {message.id})")

            # Nudge if no URL, image, or video attached
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
        if not self.configured or not self.db:
            return

        # Fast path: skip if not a pending intro message
        if payload.message_id not in self._pending_message_ids:
            return

        logger.info(f"GatingCog: reaction {payload.emoji} on pending intro {payload.message_id} by user {payload.user_id}")

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Check reactor has Approver or Super Approver role
        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return

        reactor_role_ids = {r.id for r in reactor.roles}
        is_approver = self.approver_role_id in reactor_role_ids
        is_super = self.super_approver_role_id in reactor_role_ids
        if not is_approver and not is_super:
            logger.info(f"GatingCog: reactor {reactor} lacks approver role, ignoring")
            return

        # Verify intro is still pending in DB
        intro = self.db.get_pending_intro_by_message(payload.message_id)
        if not intro:
            # Already approved or expired — clean up in-memory set
            self._pending_message_ids.discard(payload.message_id)
            return

        # Record the vote
        voter_role = 'super_approver' if is_super else 'approver'
        self.db.record_intro_vote(intro['id'], payload.message_id, payload.user_id, voter_role)

        # 1 vote from either role is enough
        await self._approve_member(guild, intro)

    async def _approve_member(self, guild: discord.Guild, intro: dict):
        """Grant Speaker role and update DB."""
        member = guild.get_member(intro['member_id'])
        if not member:
            return

        speaker_role = guild.get_role(self.speaker_role_id)
        if not speaker_role:
            return

        try:
            await member.add_roles(speaker_role, reason="Intro approved by community")
            self.db.approve_pending_intro(intro['message_id'])
            self._pending_message_ids.discard(intro['message_id'])
            logger.info(f"GatingCog: approved {member} (msg {intro['message_id']})")

            # Post a welcome message in the getting-started channel
            await self._send_speaker_welcome(guild, member)
        except Exception as e:
            logger.error(f"GatingCog: failed to approve {member}: {e}", exc_info=True)

    async def _send_speaker_welcome(self, guild: discord.Guild, member: discord.Member):
        """Post a welcome in the getting-started channel."""
        if not self.welcome_channel_id:
            return
        channel = guild.get_channel(self.welcome_channel_id)
        if not channel:
            return
        try:
            # Find the bot's reference message (first bot message in the channel)
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
        """Delete expired intro messages and mark them in DB."""
        if not self.configured or not self.db:
            return

        expired = self.db.get_expired_pending_intros(expiry_days=7)
        if not expired:
            return

        logger.info(f"GatingCog: expiring {len(expired)} intro(s)")
        for intro in expired:
            self.db.expire_pending_intro(intro['message_id'])
            self._pending_message_ids.discard(intro['message_id'])

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

        # Delete tracked messages that have expired
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

        # On first run, scan for orphaned welcome pings left by restarts
        if not self._startup_scan_done:
            self._startup_scan_done = True
            await self._scan_orphaned_welcomes()

    async def _scan_orphaned_welcomes(self):
        """Scan gate and welcome channels for bot welcome pings left behind by restarts."""
        now = datetime.now(timezone.utc)
        channel_ids = [cid for cid in (self.gate_channel_id, self.welcome_channel_id) if cid]
        deleted = 0
        for cid in channel_ids:
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
