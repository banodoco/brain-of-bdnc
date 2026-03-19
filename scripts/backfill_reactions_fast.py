"""
Fast backfill of granular reaction data using Discord REST API directly.

Skips discord.py's fetch_message entirely — just hits the reactions endpoint
for each emoji on each message. Much faster than the standard backfill.

Usage:
    python scripts/backfill_reactions_fast.py                    # backfill missing only
    python scripts/backfill_reactions_fast.py --all              # refresh all
    python scripts/backfill_reactions_fast.py --dry-run          # preview
    python scripts/backfill_reactions_fast.py --batch-size 200   # custom batch
"""
import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

logger = logging.getLogger('FastReactionBackfill')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)


class FastReactionBackfiller:
    def __init__(self, token: str, supabase_url: str, supabase_key: str,
                 refresh_all: bool = False, dry_run: bool = False,
                 batch_size: int = 100):
        self.token = token
        self.refresh_all = refresh_all
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/bndc, 1.0)",
        }

        from supabase import create_client
        self.sb = create_client(supabase_url, supabase_key)

        # Stats
        self.updated = 0
        self.skipped = 0
        self.errors = 0
        self.api_calls = 0

    # ------------------------------------------------------------------
    # Discord REST helpers
    # ------------------------------------------------------------------
    def _api_get(self, url: str, max_retries: int = 5) -> Tuple[Optional[list], Optional[str]]:
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req) as resp:
                    self.api_calls += 1
                    return json.loads(resp.read()), None
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                try:
                    data = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    data = {}
                if e.code == 429:
                    retry_after = data.get('retry_after', 1) + 0.1
                    time.sleep(retry_after)
                    continue
                elif e.code == 404:
                    return None, "not_found"
                elif e.code == 403:
                    return None, "forbidden"
                else:
                    return None, f"http_{e.code}: {body[:100]}"
            except Exception as e:
                return None, str(e)
        return None, "max_retries"

    def _fetch_message_reactions(self, channel_id: str, message_id: str) -> Tuple[List[Dict], int, List[int]]:
        """Fetch all reactions for a message via REST API.

        Returns: (reaction_rows, reaction_count, unique_reactor_ids)
        """
        # First get the message to see its reactions
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        msg_data, err = self._api_get(url)
        if err or not msg_data:
            return [], 0, []

        reactions = msg_data.get('reactions', [])
        if not reactions:
            return [], 0, []

        reaction_rows = []
        unique_ids = set()
        reaction_count = 0

        for r in reactions:
            reaction_count += r['count']
            emoji = r['emoji']
            if emoji.get('id'):
                emoji_str = f"{emoji['name']}:{emoji['id']}"
                emoji_api = f"{emoji['name']}:{emoji['id']}"
            else:
                emoji_str = emoji['name']
                emoji_api = emoji['name']

            encoded = urllib.parse.quote(emoji_api)
            react_url = (f"https://discord.com/api/v10/channels/{channel_id}"
                         f"/messages/{message_id}/reactions/{encoded}?limit=100")
            users, err = self._api_get(react_url)
            if err or not isinstance(users, list):
                continue

            for u in users:
                uid = int(u['id'])
                unique_ids.add(uid)
                reaction_rows.append({
                    'message_id': int(message_id),
                    'user_id': uid,
                    'emoji': emoji_str,
                })

            time.sleep(0.05)  # Light rate limit between emoji fetches

        return reaction_rows, reaction_count, list(unique_ids)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _fetch_messages_to_backfill(self) -> List[Dict]:
        """Fetch all messages needing reaction backfill."""
        all_results = []
        offset = 0

        while True:
            query = self.sb.table('discord_messages') \
                .select('message_id, channel_id, reaction_count')

            if not self.refresh_all:
                # Only messages with reactions but no granular data
                query = query.gt('reaction_count', 0).eq('reactors', '[]')
            else:
                query = query.gt('reaction_count', 0)

            query = query.order('created_at', desc=True) \
                .range(offset, offset + 1000 - 1)

            result = query.execute()
            batch = result.data or []
            if not batch:
                break
            all_results.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        return all_results

    def _flush_to_db(self, all_reaction_rows: List[Dict], all_updates: List[Dict]):
        """Flush accumulated reaction data to DB."""
        if not all_reaction_rows and not all_updates:
            return

        # Update discord_messages.reactors and reaction_count
        for update in all_updates:
            self.sb.table('discord_messages') \
                .update({
                    'reaction_count': update['reaction_count'],
                    'reactors': update['reactors'],
                }) \
                .eq('message_id', update['message_id']) \
                .execute()

        # Upsert into discord_reactions
        for i in range(0, len(all_reaction_rows), 500):
            batch = [dict(r, removed_at=None) for r in all_reaction_rows[i:i + 500]]
            self.sb.table('discord_reactions').upsert(batch).execute()

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------
    def run(self):
        messages = self._fetch_messages_to_backfill()
        total = len(messages)
        mode = "refresh-all" if self.refresh_all else "missing-only"
        logger.info(f"Found {total} messages to process (mode={mode})")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would process {total} messages. Exiting.")
            return

        if total == 0:
            return

        pending_rows = []
        pending_updates = []
        start_time = time.time()

        for i, row in enumerate(messages, 1):
            message_id = str(row['message_id'])
            channel_id = str(row['channel_id'])

            try:
                reaction_rows, reaction_count, unique_ids = \
                    self._fetch_message_reactions(channel_id, message_id)

                if reaction_rows:
                    pending_rows.extend(reaction_rows)
                    pending_updates.append({
                        'message_id': int(message_id),
                        'reaction_count': reaction_count,
                        'reactors': unique_ids,
                    })
                    self.updated += 1
                else:
                    self.skipped += 1

                # Flush every batch_size messages
                if len(pending_updates) >= self.batch_size:
                    self._flush_to_db(pending_rows, pending_updates)
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    eta = (total - i) / rate if rate > 0 else 0
                    logger.info(
                        f"[{i}/{total}] Flushed {len(pending_rows)} rows for "
                        f"{len(pending_updates)} msgs | "
                        f"{rate:.1f} msg/s | ETA: {eta/60:.1f}min | "
                        f"API calls: {self.api_calls}"
                    )
                    pending_rows = []
                    pending_updates = []

                time.sleep(0.05)  # Light sleep between messages

            except Exception as e:
                logger.error(f"Error on message {message_id}: {e}")
                self.errors += 1

        # Final flush
        if pending_updates:
            self._flush_to_db(pending_rows, pending_updates)
            logger.info(f"Final flush: {len(pending_rows)} rows for {len(pending_updates)} msgs")

        elapsed = time.time() - start_time
        logger.info(
            f"Complete: {self.updated} updated, {self.skipped} skipped, "
            f"{self.errors} errors | {self.api_calls} API calls | "
            f"{elapsed:.0f}s ({elapsed/60:.1f}min)"
        )


def main():
    parser = argparse.ArgumentParser(description='Fast reaction backfill via REST API')
    parser.add_argument('--all', action='store_true',
                        help='Refresh all messages, not just missing')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview without making changes')
    parser.add_argument('--batch-size', type=int, default=100,
                        help='DB flush batch size (default: 100)')
    args = parser.parse_args()

    load_dotenv()

    backfiller = FastReactionBackfiller(
        token=os.getenv('DISCORD_BOT_TOKEN'),
        supabase_url=os.getenv('SUPABASE_URL'),
        supabase_key=os.getenv('SUPABASE_SERVICE_KEY'),
        refresh_all=args.all,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )
    backfiller.run()


if __name__ == "__main__":
    main()
