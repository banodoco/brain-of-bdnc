# src/features/competition/competition_cog.py
"""
Competition voting system — fully driven by database state.

Setup: insert rows directly in Supabase:
  1. Insert into `competitions` with slug, name, channel_id, voting_starts_at, etc.
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

        # Active voting state — restored from DB on ready
        self._active_slug: Optional[str] = None
        self._active_channel_id: Optional[int] = None
        self._voting_end: Optional[datetime] = None
        self._questions_thread_id: Optional[int] = None
        self._next_entry_number: int = 0

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

            if datetime.now(timezone.utc) >= ends_at:
                self.db.update_competition(comp['slug'], {'status': 'closed'})
                logger.info(f"Competition {comp['slug']} expired during downtime — closed")
                continue

            self._active_slug = comp['slug']
            self._active_channel_id = comp.get('voting_channel_id') or comp.get('channel_id')
            self._voting_end = ends_at
            self._questions_thread_id = comp.get('questions_thread_id')

            # Restore next entry number from max existing entry
            entries = await asyncio.to_thread(self.db.get_competition_entries, comp['slug'])
            max_num = max((e.get('entry_number', 0) or 0 for e in entries), default=0)
            self._next_entry_number = max_num + 1

            if not self._check_voting_expiry.is_running():
                self._check_voting_expiry.start()

            logger.info(
                f"CompetitionCog restored voting for {comp['slug']} "
                f"(ends {ends_at.isoformat()}, next_entry={self._next_entry_number})"
            )
            break  # One active competition at a time

        # Start the scheduler
        if not self._check_scheduled_starts.is_running():
            self._check_scheduled_starts.start()

        logger.info("CompetitionCog ready")

    # ------------------------------------------------------------------
    # on_message — moderate during voting
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self._active_channel_id:
            return
        if message.author.bot:
            return

        if self._voting_end and datetime.now(timezone.utc) >= self._voting_end:
            logger.info("Voting period expired — disabling moderation")
            self._clear_voting_state()
            return

        # Questions thread: if someone posts with #lateentry, add their attachment as an entry
        if self._questions_thread_id and message.channel.id == self._questions_thread_id:
            if '#lateentry' in message.content.lower():
                await self._handle_late_entry(message)
            return

        if message.channel.id != self._active_channel_id:
            return

        try:
            await self._delete_with_notice(message)
        except Exception as e:
            logger.error(f"CompetitionCog on_message error: {e}", exc_info=True)

    async def _handle_late_entry(self, message: discord.Message):
        """Someone tagged the bot in the questions thread with an attachment — add as late entry."""
        if not self._active_slug or not self._active_channel_id:
            return
        att = best_attachment(message)
        if not att:
            await message.reply(
                "Attach your video/image to your message with #lateentry and I'll add it!"
            )
            return

        try:
            voting_channel = self.bot.get_channel(self._active_channel_id)
            if not voting_channel:
                voting_channel = await self.bot.fetch_channel(self._active_channel_id)

            author_name = get_display_name(message.author)
            entry_num = self._next_entry_number
            self._next_entry_number += 1

            # Post separator then entry in the voting channel
            await voting_channel.send("—")
            await voting_channel.send(
                f"## By {author_name}\n{att.url}",
            )

            # Record in DB
            if self.db and self._active_slug:
                await asyncio.to_thread(self.db.upsert_competition_entry, {
                    'competition_slug': self._active_slug,
                    'message_id': message.id,
                    'channel_id': message.channel.id,
                    'author_id': message.author.id,
                    'author_name': author_name,
                    'entry_number': entry_num,
                })

            await message.reply(f"Added as entry #{entry_num}!")
            logger.info(f"Added late entry #{entry_num} from {author_name}")

        except Exception as e:
            logger.error(f"Error adding late entry: {e}", exc_info=True)
            await message.reply("Something went wrong adding your entry — please try again.")

    async def _delete_with_notice(self, message: discord.Message):
        try:
            await message.delete()
        except discord.Forbidden:
            logger.warning(f"Can't delete message from {message.author} — missing permissions")
            return
        except discord.NotFound:
            return

        notice = await message.channel.send(
            f"{message.author.mention} Voting is in progress — "
            f"feel free to discuss entries in their threads!"
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
        if not self.db or self._active_channel_id:
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
                    break  # One at a time
        except Exception as e:
            logger.error(f"Error checking scheduled starts: {e}", exc_info=True)

    @_check_scheduled_starts.before_loop
    async def _before_scheduled_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _check_voting_expiry(self):
        if not self._active_channel_id or not self._voting_end:
            self._check_voting_expiry.cancel()
            return
        if datetime.now(timezone.utc) >= self._voting_end:
            slug = self._active_slug
            logger.info(f"Voting expired for {slug}")
            if self.db and slug:
                self.db.update_competition(slug, {'status': 'closed'})
            self._clear_voting_state()
            self._check_voting_expiry.cancel()

    @_check_voting_expiry.before_loop
    async def _before_expiry_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    async def _trigger_voting(self, comp: dict):
        """Start voting for a competition — fetch entries, post, activate moderation."""
        slug = comp['slug']
        try:
            entry_rows = await asyncio.to_thread(self.db.get_competition_entries, slug)
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
            entry_count = await self._post_voting(
                voting_channel, refreshed, comp['name'],
                voting_hours, comp.get('min_join_weeks', 4),
                comp.get('voting_header'),
            )

            # Persist entry numbers
            for e in refreshed:
                if e.get('entry_number'):
                    await asyncio.to_thread(self.db.upsert_competition_entry, e)

            voting_end = datetime.now(timezone.utc) + timedelta(hours=voting_hours)
            await asyncio.to_thread(self.db.update_competition, slug, {
                'status': 'voting',
                'voting_started_at': datetime.now(timezone.utc).isoformat(),
                'voting_ends_at': voting_end.isoformat(),
                'questions_thread_id': self._questions_thread_id,
            })

            self._active_slug = slug
            self._active_channel_id = voting_ch_id
            self._voting_end = voting_end
            self._next_entry_number = entry_count + 1

            if not self._check_voting_expiry.is_running():
                self._check_voting_expiry.start()

            logger.info(
                f"Voting started: {slug}, {entry_count} entries, "
                f"ends {voting_end.isoformat()}"
            )

        except Exception as e:
            logger.error(f"Error triggering voting for {slug}: {e}", exc_info=True)

    async def _post_voting(self, channel, entries, comp_name,
                           voting_hours, min_join_weeks, custom_header) -> int:
        """Post voting header, all entries in random order, and a Q&A thread. Returns count."""
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

        await channel.send(header)
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
            "Post in the thread below. For a late entry, post a message "
            "with #lateentry and I'll add it!"
        )
        thread = await questions_msg.create_thread(name="Discussions, Questions & Late Entries")
        self._questions_thread_id = thread.id
        logger.info(f"Created Discussions, Questions & Late Entries thread ({thread.id})")

        return len(entries)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_voting_state(self):
        self._active_slug = None
        self._active_channel_id = None
        self._voting_end = None
        self._questions_thread_id = None
        self._next_entry_number = 0

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
        comp = await asyncio.to_thread(self.db.get_competition, slug)
        if not comp:
            await ctx.send(f"No competition found with slug `{slug}`")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = await self.bot.fetch_channel(channel_id)

        await ctx.send(f"Posting test voting for `{slug}` to <#{channel_id}>...")

        entry_rows = await asyncio.to_thread(self.db.get_competition_entries, slug)
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
        entry_count = await self._post_voting(
            channel, refreshed, comp['name'],
            voting_hours, comp.get('min_join_weeks', 4),
            comp.get('voting_header'),
        )

        # Set active state so late entries via the questions thread work
        self._active_slug = slug
        self._active_channel_id = channel_id
        self._next_entry_number = entry_count + 1

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
