#!/usr/bin/env python3
"""
Fetch fresh Discord CDN URLs for specific messages.

Connects to Discord briefly, fetches fresh attachment URLs, prints them,
and optionally updates the database.

Usage:
    # Just print fresh URLs
    python scripts/fetch_fresh_urls.py MESSAGE_ID [MESSAGE_ID ...]

    # Update the database too
    python scripts/fetch_fresh_urls.py --update MESSAGE_ID [MESSAGE_ID ...]

    # With channel hint (faster, avoids DB lookup)
    python scripts/fetch_fresh_urls.py --channel 123456 MESSAGE_ID
"""

import argparse
import asyncio
import json
import logging
import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, '.env'))

import discord
from discord.ext import commands

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def fetch_message_attachments(bot, channel_id, message_id):
    """Fetch fresh attachment URLs for a single message."""
    # Try as regular channel first
    channel = None
    try:
        channel = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden):
        pass

    if isinstance(channel, discord.ForumChannel):
        # For forum channels, the message_id might be the thread starter
        try:
            thread = await bot.fetch_channel(message_id)
            if isinstance(thread, discord.Thread):
                channel = thread
        except (discord.NotFound, discord.Forbidden):
            return None

    if not channel or isinstance(channel, discord.ForumChannel):
        return None

    try:
        message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden):
        return None

    attachments = []
    for att in message.attachments:
        attachments.append({
            'id': att.id,
            'filename': att.filename,
            'url': att.url,
            'proxy_url': att.proxy_url,
            'size': att.size,
            'content_type': att.content_type,
        })

    return {
        'message_id': message_id,
        'channel_id': channel_id,
        'content': message.content[:100] if message.content else '',
        'attachments': attachments,
    }


async def run(message_ids, channel_hint=None, update_db=False):
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        print("Error: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='!', intents=intents)

    db_handler = None
    if update_db:
        from src.common.db_handler import DatabaseHandler
        db_handler = DatabaseHandler()

    # Look up channel_ids from DB if not provided
    from supabase import create_client
    sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])

    message_channel_map = {}
    for mid in message_ids:
        if channel_hint:
            message_channel_map[mid] = int(channel_hint)
        else:
            # Look up in DB
            res = sb.table('discord_messages').select('channel_id,thread_id').eq('message_id', mid).execute()
            if res.data:
                row = res.data[0]
                # Try thread_id first (for messages in threads stored with parent channel_id)
                message_channel_map[mid] = row['thread_id'] or row['channel_id']
            else:
                print(f"Warning: message {mid} not found in database", file=sys.stderr)

    results = []

    @bot.event
    async def on_ready():
        try:
            for mid, ch_id in message_channel_map.items():
                result = await fetch_message_attachments(bot, ch_id, int(mid))

                if not result:
                    # Retry with channel_id if we tried thread_id
                    res = sb.table('discord_messages').select('channel_id,thread_id').eq('message_id', mid).execute()
                    if res.data:
                        row = res.data[0]
                        alt_id = row['channel_id'] if ch_id == row.get('thread_id') else row.get('thread_id')
                        if alt_id and alt_id != ch_id:
                            result = await fetch_message_attachments(bot, alt_id, int(mid))

                if result:
                    results.append(result)
                    print(f"\n=== Message {mid} ===")
                    print(f"Content: {result['content']}")
                    for att in result['attachments']:
                        print(f"  {att['filename']}: {att['url']}")

                    if update_db and db_handler:
                        msg_data = {
                            'message_id': int(mid),
                            'channel_id': result['channel_id'],
                            'attachments': result['attachments'],
                        }
                        db_handler.update_message(msg_data)
                        print(f"  -> Updated in database")
                else:
                    print(f"\nFailed to fetch message {mid}", file=sys.stderr)

                await asyncio.sleep(0.3)

            # Output JSON summary
            print(f"\n=== JSON Summary ===")
            print(json.dumps(results, indent=2, default=str))
        finally:
            await bot.close()

    await bot.start(token)
    return results


def main():
    parser = argparse.ArgumentParser(description="Fetch fresh Discord CDN URLs for specific messages")
    parser.add_argument('message_ids', nargs='+', help='Message IDs to refresh')
    parser.add_argument('--channel', type=str, help='Channel ID hint (skips DB lookup)')
    parser.add_argument('--update', action='store_true', help='Update database with fresh URLs')
    args = parser.parse_args()

    asyncio.run(run(args.message_ids, args.channel, args.update))


if __name__ == '__main__':
    main()
