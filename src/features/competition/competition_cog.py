# src/features/competition/competition_cog.py
"""
Competition voting system — fully driven by database state.

Setup: insert rows directly in Supabase:
  1. Insert into `discord_competitions` with slug, name, channel_id, voting_starts_at, etc.
  2. Insert into `competition_entries` tagging message IDs as entries
  3. The bot picks it up automatically when voting_starts_at arrives

The bot handles:
  - Scheduled voting start (posts entries, activates moderation)
  - Channel moderation during voting (auto-delete, point to threads)
  - Auto-close when voting period ends
  - State recovery on restart
"""
import discord
import logging
import random
import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from discord.ext import commands, tasks

logger = logging.getLogger('DiscordBot')

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.webm')


def is_video_attachment(att: discord.Attachment) -> bool:
    return any(att.filename.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)


def get_display_name(member) -> str:
    if hasattr(member, 'nick') and member.nick:
        return member.nick
    if hasattr(member, 'global_name') and member.global_name:
        return member.global_name
    return member.name


def best_attachment(message: discord.Message) -> Optional[discord.Attachment]:
    """Return the best attachment from a message — video > image > any."""
    for att in message.attachments:
        if is_video_attachment(att):
            return att
    for att in message.attachments:
        if att.content_type and att.content_type.startswith('image/'):
            return att
    return message.attachments[0] if message.attachments else None


class CompetitionCog(commands.Cog):
    """Reads competition config from DB, posts voting on schedule, moderates channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db_handler if hasattr(bot, 'db_handler') else None

        # Active voting state, keyed by the voting channel or questions thread.
        self._active_by_voting_channel_id: dict[int, dict] = {}
        self._active_by_questions_thread_id: dict[int, dict] = {}

    def _default_guild_id(self) -> Optional[int]:
        sc = getattr(self.db, 'server_config', None) if self.db else None
        return sc.get_default_guild_id(require_write=True) if sc else None

    def _resolve_guild_id(
        self,
        *,
        comp: Optional[dict] = None,
        channel_id: Optional[int] = None,
        message: Optional[discord.Message] = None,
    ) -> Optional[int]:
        """Resolve the guild that owns a competition from live context or channel metadata."""
        if message and getattr(message, 'guild', None):
            return message.guild.id

        candidate_channel_ids = []
        if channel_id:
            candidate_channel_ids.append(channel_id)
        if comp:
            for key in ('voting_channel_id', 'channel_id'):
                value = comp.get(key)
                if value and value not in candidate_channel_ids:
                    candidate_channel_ids.append(value)

        for candidate_id in candidate_channel_ids:
            channel = self.db.get_channel(candidate_id) if self.db else None
            guild_id = channel.get('guild_id') if channel else None
            if guild_id:
                return int(guild_id)

        return self._default_guild_id()

    def _register_active_competition(
        self,
        *,
        guild_id: int,
        slug: str,
        voting_channel_id: int,
        voting_end: datetime,
        questions_thread_id: Optional[int],
        next_entry_number: int,
    ) -> dict:
        state = {
            'guild_id': guild_id,
            'slug': slug,
            'voting_channel_id': voting_channel_id,
            'voting_end': voting_end,
            'questions_thread_id': questions_thread_id,
            'next_entry_number': next_entry_number,
        }
        self._active_by_voting_channel_id[voting_channel_id] = state
        if questions_thread_id:
            self._active_by_questions_thread_id[questions_thread_id] = state
        return state

    def _clear_active_competition(self, state: dict):
        self._active_by_voting_channel_id.pop(state['voting_channel_id'], None)
        if state.get('questions_thread_id'):
            self._active_by_questions_thread_id.pop(state['questions_thread_id'], None)

    def _get_active_state_for_channel(self, channel_id: int) -> Optional[dict]:
        return (
            self._active_by_questions_thread_id.get(channel_id)
            or self._active_by_voting_channel_id.get(channel_id)
        )

    async def _close_competition(self, state: dict):
        slug = state['slug']
        logger.info(f"Voting expired for {slug}")
        if self.db and slug:
            self.db.update_competition(
                slug,
                {'status': 'closed'},
                guild_id=state['guild_id'],
            )
        self._clear_active_competition(state)

    def cog_unload(self):
        if self._check_voting_expiry.is_running():
            self._check_voting_expiry.cancel()
        if self._check_scheduled_starts.is_running():
            self._check_scheduled_starts.cancel()

    # ------------------------------------------------------------------
    # Startup — restore active voting / start scheduler
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.db:
            logger.info("CompetitionCog ready (no DB)")
            return

        # Restore voting state if the bot restarted mid-vote
        active = await asyncio.to_thread(self.db.get_active_competitions)
        for comp in active:
            ends_at = self._parse_dt(comp.get('voting_ends_at'))
            if not ends_at:
                continue

            guild_id = self._resolve_guild_id(comp=comp)
            if datetime.now(timezone.utc) >= ends_at:
                self.db.update_competition(comp['slug'], {'status': 'closed'}, guild_id=guild_id)
                logger.info(f"Competition {comp['slug']} expired during downtime — closed")
                continue

            voting_channel_id = comp.get('voting_channel_id') or comp.get('channel_id')
            questions_thread_id = comp.get('questions_thread_id')

            # Restore next entry number from max existing entry
            entries = await asyncio.to_thread(self.db.get_competition_entries, comp['slug'], guild_id)
            max_num = max((e.get('entry_number', 0) or 0 for e in entries), default=0)
            next_entry_number = max_num + 1
            self._register_active_competition(
                guild_id=guild_id,
                slug=comp['slug'],
                voting_channel_id=voting_channel_id,
                voting_end=ends_at,
                questions_thread_id=questions_thread_id,
                next_entry_number=next_entry_number,
            )

            if not self._check_voting_expiry.is_running():
                self._check_voting_expiry.start()

            logger.info(
                f"CompetitionCog restored voting for {comp['slug']} "
                f"(ends {ends_at.isoformat()}, next_entry={next_entry_number})"
            )

        # Start the scheduler
        if not self._check_scheduled_starts.is_running():
            self._check_scheduled_starts.start()

        logger.info("CompetitionCog ready")

    # ------------------------------------------------------------------
    # on_message — moderate during voting
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        state = self._get_active_state_for_channel(message.channel.id)
        if not state:
            return

        if datetime.now(timezone.utc) >= state['voting_end']:
            await self._close_competition(state)
            return

        # Questions thread: if someone posts with "late entry", add their attachment as an entry
        if state.get('questions_thread_id') and message.channel.id == state['questions_thread_id']:
            if 'late entry' in message.content.lower():
                await self._handle_late_entry(message, state)
            return

        if message.channel.id != state['voting_channel_id']:
            return

        try:
            await self._delete_with_notice(message, state)
        except Exception as e:
            logger.error(f"CompetitionCog on_message error: {e}", exc_info=True)

    async def _handle_late_entry(self, message: discord.Message, state: dict):
        """Someone tagged the bot in the questions thread with an attachment — add as late entry."""
        att = best_attachment(message)
        if not att:
            await message.reply(
                "Attach your video/image to your message with the words \"late entry\" and I'll add it!"
            )
            return

        try:
            voting_channel = self.bot.get_channel(state['voting_channel_id'])
            if not voting_channel:
                voting_channel = await self.bot.fetch_channel(state['voting_channel_id'])

            author_name = get_display_name(message.author)
            entry_num = state['next_entry_number']
            state['next_entry_number'] += 1

            # Find a random "—" separator message to edit inline
            separators = []
            async for msg in voting_channel.history(limit=200):
                if msg.author.id == self.bot.user.id and msg.content.strip() == "—":
                    separators.append(msg)
            separator_msg = random.choice(separators) if separators else None

            if separator_msg:
                await separator_msg.edit(
                    content=f"—\n## By {author_name}\n{att.url}"
                )
                entry_msg = separator_msg
            else:
                # No separator found — post normally
                entry_msg = await voting_channel.send(f"—\n## By {author_name}\n{att.url}")

            # Record in DB
            if self.db:
                await asyncio.to_thread(self.db.upsert_competition_entry, {
                    'competition_slug': state['slug'],
                    'message_id': message.id,
                    'channel_id': message.channel.id,
                    'author_id': message.author.id,
                    'author_name': author_name,
                    'entry_number': entry_num,
                }, state['guild_id'])

            await message.reply(f"Added! {entry_msg.jump_url}")
            logger.info(f"Added late entry #{entry_num} from {author_name}")

        except Exception as e:
            logger.error(f"Error adding late entry: {e}", exc_info=True)
            await message.reply("Something went wrong adding your entry — please try again.")

    async def _delete_with_notice(self, message: discord.Message, state: dict):
        try:
            await message.delete()
        except discord.Forbidden:
            logger.warning(f"Can't delete message from {message.author} — missing permissions")
            return
        except discord.NotFound:
            return

        thread_url = ""
        if state.get('questions_thread_id'):
            thread_url = (
                f" https://discord.com/channels/{message.guild.id}"
                f"/{message.channel.id}/{state['questions_thread_id']}"
            )
        notice = await message.channel.send(
            f"{message.author.mention} Voting is in progress — "
            f"you can discuss and share questions in this thread:{thread_url}"
        )
        await asyncio.sleep(10)
        try:
            await notice.delete()
        except discord.NotFound:
            pass

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def _check_scheduled_starts(self):
        """Check for competitions scheduled to start voting now."""
        if not self.db:
            return

        try:
            scheduled = await asyncio.to_thread(self.db.get_scheduled_competitions)
            for comp in scheduled:
                starts_at = self._parse_dt(comp.get('voting_starts_at'))
                if not starts_at:
                    continue
                if datetime.now(timezone.utc) >= starts_at:
                    logger.info(f"Scheduled voting start for {comp['slug']}")
                    await self._trigger_voting(comp)
        except Exception as e:
            logger.error(f"Error checking scheduled starts: {e}", exc_info=True)

    @_check_scheduled_starts.before_loop
    async def _before_scheduled_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _check_voting_expiry(self):
        expired = [
            state
            for state in list(self._active_by_voting_channel_id.values())
            if datetime.now(timezone.utc) >= state['voting_end']
        ]
        for state in expired:
            await self._close_competition(state)

    @_check_voting_expiry.before_loop
    async def _before_expiry_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    async def _trigger_voting(self, comp: dict):
        """Start voting for a competition — fetch entries, post, activate moderation."""
        slug = comp['slug']
        guild_id = self._resolve_guild_id(comp=comp)
        try:
            entry_rows = await asyncio.to_thread(self.db.get_competition_entries, slug, guild_id)
            if not entry_rows:
                logger.warning(f"Scheduled voting for {slug} but no entries — skipping")
                return

            voting_ch_id = comp.get('voting_channel_id') or comp['channel_id']
            voting_channel = self.bot.get_channel(voting_ch_id)
            if not voting_channel:
                voting_channel = await self.bot.fetch_channel(voting_ch_id)

            # Fetch fresh message objects
            refreshed = []
            for entry in entry_rows:
                msg = await self._find_message(comp['channel_id'], entry['message_id'])
                if msg:
                    entry['_msg'] = msg
                    entry['author_name'] = get_display_name(msg.author)
                    refreshed.append(entry)
                else:
                    logger.warning(f"Entry {entry['message_id']} not found — skipping")
                await asyncio.sleep(0.3)

            if not refreshed:
                logger.warning(f"Voting for {slug} — no entries could be fetched")
                return

            voting_hours = comp.get('voting_hours', 24)
            voting_end = datetime.now(timezone.utc) + timedelta(hours=voting_hours)
            entry_count, questions_thread_id = await self._post_voting(
                voting_channel, refreshed, comp['name'],
                voting_hours, comp.get('min_join_weeks', 4),
                comp.get('voting_header'),
            )
            self._register_active_competition(
                guild_id=guild_id,
                slug=slug,
                voting_channel_id=voting_ch_id,
                voting_end=voting_end,
                questions_thread_id=questions_thread_id,
                next_entry_number=entry_count + 1,
            )

            # Persist entry numbers
            for e in refreshed:
                if e.get('entry_number'):
                    await asyncio.to_thread(self.db.upsert_competition_entry, e, guild_id=guild_id)

            await asyncio.to_thread(self.db.update_competition, slug, {
                'status': 'voting',
                'voting_started_at': datetime.now(timezone.utc).isoformat(),
                'voting_ends_at': voting_end.isoformat(),
                'questions_thread_id': questions_thread_id,
            }, guild_id=guild_id)

            if not self._check_voting_expiry.is_running():
                self._check_voting_expiry.start()

            logger.info(
                f"Voting started: {slug}, {entry_count} entries, "
                f"ends {voting_end.isoformat()}"
            )

        except Exception as e:
            logger.error(f"Error triggering voting for {slug}: {e}", exc_info=True)

    async def _post_voting(self, channel, entries, comp_name,
                           voting_hours, min_join_weeks, custom_header) -> tuple[int, int]:
        """Post voting header, all entries in random order, and a Q&A thread."""
        random.shuffle(entries)

        if custom_header:
            header = custom_header
        else:
            header = f"# VOTING TIME — {comp_name}\n\n"
            header += f"Voting is now open and lasts for **{voting_hours} hours**!\n\n"
            header += (
                "**How to vote:** React to the submissions below. "
                "You can vote for as many as you like. "
                "Feel free to open a thread on any entry to discuss it!\n\n"
            )
            header += (
                f"Votes from accounts that joined the server in the past "
                f"**{min_join_weeks} weeks** will not be counted. "
                f"We're keeping watch for suspicious voting patterns — "
                f"please play fair!\n\n"
            )
            header += "---"

        header_msg = await channel.send(header)
        await asyncio.sleep(1)

        for i, entry in enumerate(entries, 1):
            entry['entry_number'] = i
            msg_obj = entry.pop('_msg')
            att = best_attachment(msg_obj)

            if att:
                await channel.send(f"## By {entry['author_name']}\n{att.url}")
            else:
                await channel.send(f"## By {entry['author_name']}\n{msg_obj.jump_url}")

            logger.info(f"Posted entry {i}/{len(entries)}: {entry['author_name']}")
            await asyncio.sleep(0.5)

            # Separator between entries — doubles as an editable slot
            if i < len(entries):
                await channel.send("—")
                await asyncio.sleep(0.3)

        # Post discussions/questions/late-entries message
        questions_msg = await channel.send(
            "## Discussions, Questions & Late Entries\n\n"
            "Want to discuss the entries, ask a question, or submit a late entry? "
            "Post in the thread below. For a **late entry**, post a message "
            "containing the words \"late entry\" with your video attached and I'll add it!"
        )
        thread = await questions_msg.create_thread(name="Discussions, Questions & Late Entries")
        logger.info(f"Created Discussions, Questions & Late Entries thread ({thread.id})")

        # Jump to top link as the final message
        await channel.send(f"[Jump to top]({header_msg.jump_url})")

        return len(entries), thread.id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    async def cog_command_error(self, ctx, error):
        logger.error(f"Competition command error: {error}", exc_info=True)
        await ctx.send(f"Error: {error}")

    # ------------------------------------------------------------------
    # Admin commands (owner-only)
    # ------------------------------------------------------------------

    @commands.command(name='comp_test')
    async def comp_test(self, ctx, slug: str, channel_id: int):
        """Test-post voting to a specific channel without activating moderation."""
        if not await self.bot.is_owner(ctx.author):
            return
        guild_id = getattr(getattr(ctx, 'guild', None), 'id', None) or self._default_guild_id()
        comp = await asyncio.to_thread(self.db.get_competition, slug, guild_id)
        if not comp:
            await ctx.send(f"No competition found with slug `{slug}`")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = await self.bot.fetch_channel(channel_id)

        await ctx.send(f"Posting test voting for `{slug}` to <#{channel_id}>...")

        entry_rows = await asyncio.to_thread(self.db.get_competition_entries, slug, guild_id)
        if not entry_rows:
            await ctx.send("No entries found.")
            return

        refreshed = []
        for entry in entry_rows:
            msg = await self._find_message(comp['channel_id'], entry['message_id'])
            if msg:
                entry['_msg'] = msg
                entry['author_name'] = get_display_name(msg.author)
                refreshed.append(entry)
            await asyncio.sleep(0.3)

        if not refreshed:
            await ctx.send("Couldn't fetch any entry messages.")
            return

        voting_hours = comp.get('voting_hours', 24)
        entry_count, questions_thread_id = await self._post_voting(
            channel, refreshed, comp['name'],
            voting_hours, comp.get('min_join_weeks', 4),
            comp.get('voting_header'),
        )

        # Set active state so late entries via the questions thread work
        voting_end = datetime.now(timezone.utc) + timedelta(hours=voting_hours)
        self._register_active_competition(
            guild_id=guild_id,
            slug=slug,
            voting_channel_id=channel_id,
            voting_end=voting_end,
            questions_thread_id=questions_thread_id,
            next_entry_number=entry_count + 1,
        )

        await ctx.send(f"Done — posted {len(refreshed)} entries. Late entries enabled.")

    @commands.command(name='comp_wipe')
    async def comp_wipe(self, ctx, channel_id: int):
        """Delete all bot messages from a channel (for cleaning up test posts)."""
        if not await self.bot.is_owner(ctx.author):
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = await self.bot.fetch_channel(channel_id)

        await ctx.send(f"Wiping bot messages from <#{channel_id}>...")
        deleted = 0
        async for msg in channel.history(limit=500):
            if msg.author.id == self.bot.user.id:
                await msg.delete()
                deleted += 1
                await asyncio.sleep(0.5)
        await ctx.send(f"Deleted {deleted} messages.")

    async def _find_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.ForumChannel):
                thread = await self.bot.fetch_channel(message_id)
                if isinstance(thread, discord.Thread):
                    channel = thread
                else:
                    return None
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        except Exception as e:
            logger.error(f"Error fetching message {message_id}: {e}")
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(CompetitionCog(bot))
