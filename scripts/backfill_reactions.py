"""
Backfill reaction data from Discord API into Supabase.

Usage:
    python scripts/backfill_reactions.py --days 7
    python scripts/backfill_reactions.py --days 7 --refresh-all
    python scripts/backfill_reactions.py --days 7 --dry-run
    python scripts/backfill_reactions.py --days 7 --post-top-gens
"""
import argparse
import asyncio
import json
import random
import discord
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from dotenv import load_dotenv
import sys

# Add parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.base_bot import BaseDiscordBot
from src.common.db_handler import DatabaseHandler

logger = logging.getLogger('ReactionBackfill')


class ReactionBackfiller(BaseDiscordBot):
    def __init__(self, days: int = 7, refresh_all: bool = False,
                 dry_run: bool = False, post_top_gens: bool = False,
                 dev_mode: bool = False):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            heartbeat_timeout=120.0,
            guild_ready_timeout=30.0,
            gateway_queue_size=512,
            logger=logger
        )

        self.dev_mode = dev_mode
        self.db = DatabaseHandler(dev_mode=dev_mode)
        self.days = days
        self.refresh_all = refresh_all
        self.dry_run = dry_run
        self.post_top_gens = post_top_gens

        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        if not logger.handlers:
            logger.addHandler(handler)

    async def setup_hook(self):
        logger.info("Reaction backfiller initialized and ready")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def _fetch_messages_to_backfill(self) -> List[Dict]:
        """Fetch messages from Supabase that need reaction backfill, with pagination."""
        sb = self.db.storage_handler.supabase_client
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat()

        all_results = []
        offset = 0
        batch_size = 1000

        while True:
            query = sb.table('discord_messages') \
                .select('message_id, channel_id') \
                .gte('created_at', cutoff) \
                .neq('attachments', '[]')

            if not self.refresh_all:
                query = query.or_('reactors.is.null,reactors.eq.[]')

            query = query.order('created_at', desc=True) \
                .range(offset, offset + batch_size - 1)

            result = query.execute()
            batch = result.data if result.data else []

            if not batch:
                break

            all_results.extend(batch)

            if len(batch) < batch_size:
                break

            offset += batch_size

        return all_results

    # ------------------------------------------------------------------
    # Backfill reactions
    # ------------------------------------------------------------------
    async def backfill_reactions(self):
        """Backfill reaction data for messages in the date range."""
        try:
            results = self._fetch_messages_to_backfill()

            if not results:
                logger.info("No messages found needing reaction backfill")
                return

            mode = "refresh-all" if self.refresh_all else "empty-reactors-only"
            logger.info(f"Found {len(results)} messages to process "
                        f"(last {self.days} days, mode={mode})")

            if self.dry_run:
                logger.info("[DRY RUN] Would process %d messages. Exiting.", len(results))
                return

            updated = 0
            skipped = 0
            errors = 0

            for i, row in enumerate(results, 1):
                try:
                    message_id = row['message_id']
                    channel_id = row['channel_id']

                    channel = self.get_channel(channel_id)
                    if not channel:
                        logger.warning(f"Could not find channel {channel_id}")
                        skipped += 1
                        continue

                    message = await channel.fetch_message(message_id)
                    if not message:
                        logger.warning(f"Could not find message {message_id}")
                        skipped += 1
                        continue

                    # Collect unique reactors (excluding the bot itself)
                    reactors = []
                    reaction_count = 0

                    if message.reactions:
                        for reaction in message.reactions:
                            reaction_count += reaction.count
                            async for user in reaction.users():
                                if user.id not in reactors and user.id != self.user.id:
                                    reactors.append(user.id)

                    self.db.update_reactions(message_id, reaction_count, reactors)
                    updated += 1

                    if updated % 50 == 0 or len(reactors) >= 5:
                        logger.info(f"[{i}/{len(results)}] msg={message_id} "
                                    f"reactors={len(reactors)} reaction_count={reaction_count}")

                    await asyncio.sleep(0.5)  # Rate limit

                except discord.NotFound:
                    logger.debug(f"Message {row['message_id']} not found (deleted?)")
                    skipped += 1
                except discord.Forbidden:
                    logger.warning(f"No access to message {row['message_id']}")
                    skipped += 1
                except Exception as e:
                    logger.error(f"Error processing message {row['message_id']}: {e}")
                    errors += 1
                    continue

            logger.info(f"Reaction backfill complete: "
                        f"{updated} updated, {skipped} skipped, {errors} errors")

        except Exception as e:
            logger.error(f"Error during reaction backfill: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Post missing top generations
    # ------------------------------------------------------------------
    async def post_missing_top_gens(self):
        """Find and post top generations that were missed due to stale reactor data."""
        top_gens_channel_id = int(os.getenv('TOP_GENS_ID', 0))
        if not top_gens_channel_id:
            logger.error("TOP_GENS_ID not set, cannot post top generations")
            return

        guild_id = int(os.getenv('GUILD_ID', 0))
        art_channel_id = int(os.getenv('ART_CHANNEL_ID', 0))
        channels_str = os.getenv('CHANNELS_TO_MONITOR', '')
        monitor_ids = [int(c.strip()) for c in channels_str.split(',') if c.strip()]

        top_gens_channel = self.get_channel(top_gens_channel_id)
        if not top_gens_channel:
            try:
                top_gens_channel = await self.fetch_channel(top_gens_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:  # Discord API errors
                logger.error(f"Could not fetch top gens channel {top_gens_channel_id}: {e}")
                return

        sb = self.db.storage_handler.supabase_client

        # Expand monitor_ids to include sub-channels by category
        expanded_channel_ids = set(monitor_ids)
        try:
            ch_resp = sb.table('discord_channels') \
                .select('channel_id, category_id').execute()
            for ch in (ch_resp.data or []):
                cat_id = ch.get('category_id')
                if cat_id and int(cat_id) in monitor_ids:
                    expanded_channel_ids.add(int(ch['channel_id']))
        except Exception as e:
            logger.warning(f"Could not expand channel list: {e}")

        if art_channel_id:
            expanded_channel_ids.discard(art_channel_id)

        # Check each day in the backfill window
        now = datetime.now(timezone.utc)
        total_posted = 0

        for days_ago in range(self.days, -1, -1):
            # The top_generations query uses a 24h window ending at ~7:00 UTC
            window_end = (now - timedelta(days=days_ago)).replace(
                hour=7, minute=0, second=0, microsecond=0)
            # Skip if window_end is in the future (hasn't been scheduled yet)
            if window_end > now:
                continue
            window_start = window_end - timedelta(hours=24)
            date_label = window_end.strftime('%Y-%m-%d')

            # Check if top gens were already posted for this date
            # by looking at system_logs (check full calendar day)
            try:
                day_start = window_end.replace(hour=0, minute=0, second=0)
                day_end = day_start + timedelta(days=1)
                log_check = sb.table('system_logs') \
                    .select('message') \
                    .ilike('message', '%Posted top%gens%') \
                    .gte('created_at', day_start.isoformat()) \
                    .lt('created_at', day_end.isoformat()) \
                    .limit(1) \
                    .execute()
                if log_check.data:
                    logger.info(f"[{date_label}] Top gens already posted, skipping")
                    continue
            except Exception:
                pass  # If we can't check, proceed anyway

            # Query qualifying video messages for this window
            qualifying = self._find_qualifying_videos(
                sb, window_start, window_end, expanded_channel_ids)

            if not qualifying:
                logger.info(f"[{date_label}] No qualifying videos (5+ reactors)")
                continue

            logger.info(f"[{date_label}] Found {len(qualifying)} qualifying videos")

            if self.dry_run:
                for gen in qualifying:
                    logger.info(f"  [DRY RUN] msg={gen['message_id']} "
                                f"reactors={gen['unique_reactor_count']} "
                                f"by {gen.get('author_name', '?')} "
                                f"in #{gen.get('channel_name', '?')}")
                continue

            # Post them in random order, matching the production format
            randomized = list(qualifying)
            random.shuffle(randomized)

            for gen in randomized:
                attachments = gen['attachments']
                if isinstance(attachments, str):
                    attachments = json.loads(attachments)

                video_attachment = next(
                    (a for a in attachments
                     if any(a.get('filename', '').lower().endswith(ext)
                            for ext in ('.mp4', '.mov', '.webm'))),
                    None
                )
                if not video_attachment:
                    continue

                desc = [
                    f"By **{gen.get('author_name', 'Unknown')}**"
                    f" in #{gen.get('channel_name', 'unknown')}",
                    f"🔥 {gen['unique_reactor_count']} unique reactions"
                ]

                content = gen.get('content', '')
                if content and content.strip():
                    desc.append(f"> \"{content[:150]}\"")

                desc.append(video_attachment['url'])
                jump_url = (f"https://discord.com/channels/"
                            f"{guild_id}/{gen['channel_id']}/{gen['message_id']}")
                desc.append(f"🔗 Original post: {jump_url}")

                msg_text = "\n".join(desc)

                try:
                    await top_gens_channel.send(msg_text)
                    total_posted += 1
                    await asyncio.sleep(1)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:  # Discord API errors
                    logger.error(f"Error posting to top gens channel: {e}")

            logger.info(f"[{date_label}] Posted {len(randomized)} top generations")

        logger.info(f"Top gens backfill complete: {total_posted} total posted")

    def _find_qualifying_videos(self, sb, window_start, window_end,
                                channel_ids) -> List[Dict]:
        """Find video messages with 5+ unique reactors in a time window."""
        all_messages = []
        offset = 0
        batch_size = 1000

        while True:
            query = sb.table('discord_messages') \
                .select('message_id, channel_id, author_id, content, '
                        'attachments, reactors, reaction_count') \
                .gte('created_at', window_start.isoformat()) \
                .lt('created_at', window_end.isoformat()) \
                .neq('attachments', '[]') \
                .in_('channel_id', [str(c) for c in channel_ids]) \
                .range(offset, offset + batch_size - 1)

            result = query.execute()
            batch = result.data or []
            if not batch:
                break
            all_messages.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size

        # Filter in Python: video files, 5+ unique reactors, non-NSFW
        # Fetch channel names for filtering and display
        ch_ids = list(set(m['channel_id'] for m in all_messages))
        channel_names = {}
        if ch_ids:
            for i in range(0, len(ch_ids), 100):
                batch_ids = ch_ids[i:i + 100]
                resp = sb.table('discord_channels') \
                    .select('channel_id, channel_name') \
                    .in_('channel_id', [str(c) for c in batch_ids]) \
                    .execute()
                for ch in (resp.data or []):
                    channel_names[ch['channel_id']] = ch['channel_name']

        # Fetch author names
        author_ids = list(set(m['author_id'] for m in all_messages))
        author_names = {}
        if author_ids:
            for i in range(0, len(author_ids), 100):
                batch_ids = author_ids[i:i + 100]
                resp = sb.table('discord_members') \
                    .select('member_id, username, global_name, server_nick') \
                    .in_('member_id', batch_ids) \
                    .execute()
                for m in (resp.data or []):
                    author_names[m['member_id']] = (
                        m.get('server_nick') or m.get('global_name')
                        or m.get('username') or 'Unknown')

        qualifying = []
        for msg in all_messages:
            # Skip NSFW channels
            ch_name = channel_names.get(msg['channel_id'], '')
            if 'nsfw' in ch_name.lower():
                continue

            # Check for video attachment
            attachments = msg['attachments']
            if isinstance(attachments, str):
                try:
                    attachments = json.loads(attachments)
                except (json.JSONDecodeError, TypeError):
                    continue

            has_video = any(
                a.get('filename', '').lower().endswith(('.mp4', '.mov', '.webm'))
                for a in attachments
            )
            if not has_video:
                continue

            # Count unique reactors
            reactors = msg.get('reactors')
            if isinstance(reactors, str):
                try:
                    reactors = json.loads(reactors)
                except (json.JSONDecodeError, TypeError):
                    reactors = []
            elif not isinstance(reactors, list):
                reactors = []

            unique_reactor_count = len(reactors)
            if unique_reactor_count < 5:
                continue

            msg['unique_reactor_count'] = unique_reactor_count
            msg['channel_name'] = ch_name
            msg['author_name'] = author_names.get(msg['author_id'], 'Unknown')
            msg['attachments'] = attachments
            qualifying.append(msg)

        # Sort by reactor count descending, cap at 20
        qualifying.sort(key=lambda x: x['unique_reactor_count'], reverse=True)
        return qualifying[:20]

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def on_ready(self):
        """Called when the client is ready."""
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guilds")

        try:
            await self.backfill_reactions()

            if self.post_top_gens:
                logger.info("Now checking for missing top generation posts...")
                await self.post_missing_top_gens()
        finally:
            await self.close()


def main():
    parser = argparse.ArgumentParser(description='Backfill reaction data from Discord')
    parser.add_argument('--days', type=int, default=7,
                        help='Number of days to look back (default: 7)')
    parser.add_argument('--refresh-all', action='store_true',
                        help='Refresh ALL messages with attachments, not just empty reactors')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--post-top-gens', action='store_true',
                        help='After backfill, post missing top generations')
    args = parser.parse_args()

    load_dotenv()
    dev_mode = os.getenv('DEV_MODE', '').lower() == 'true'

    backfiller = ReactionBackfiller(
        days=args.days,
        refresh_all=args.refresh_all,
        dry_run=args.dry_run,
        post_top_gens=args.post_top_gens,
        dev_mode=dev_mode,
    )
    backfiller.run(os.getenv('DISCORD_BOT_TOKEN'))


if __name__ == "__main__":
    main()
