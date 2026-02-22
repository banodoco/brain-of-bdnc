"""
Update the rules channel from rules.md.

By default, edits existing messages in place. Only deletes and reposts
when the number of messages changes (--repost to force).

Usage:
    python scripts/post_rules.py          # dry run
    python scripts/post_rules.py --send   # edit/post for real
    python scripts/post_rules.py --send --repost  # force delete and repost all
"""

import asyncio
import argparse
import os
import re

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
RULES_CHANNEL_ID = 1138515622582562947

RULES_FILE = os.path.join(os.path.dirname(__file__), "..", "rules.md")


def load_rules_messages():
    """Load rules from rules.md. The header (## line) becomes the first message.
    Each quote block section becomes a separate message."""
    with open(RULES_FILE) as f:
        content = f.read()

    messages = []
    parts = re.split(r'\n\n(?=>)', content)
    for part in parts:
        part = part.strip()
        if part:
            messages.append(part)
    return messages


async def repost_all(channel, new_messages, send):
    """Delete all existing messages and post fresh."""
    deleted = 0
    async for message in channel.history(limit=200):
        if send:
            await message.delete()
            await asyncio.sleep(0.5)
        deleted += 1
        print(f"  {'Deleted' if send else 'Would delete'}: {message.id} ({message.content[:50]}...)")

    print(f"\n{'Deleted' if send else 'Would delete'} {deleted} messages.\n")

    for i, msg in enumerate(new_messages):
        if send:
            sent = await channel.send(msg)
            print(f"  Posted message {i+1}/{len(new_messages)} (id: {sent.id})")
            await asyncio.sleep(0.5)
        else:
            print(f"  Would post message {i+1}/{len(new_messages)}:")
            print(f"    {msg[:80]}...")

    print(f"\n{'Posted' if send else 'Would post'} {len(new_messages)} messages.")


async def edit_in_place(channel, new_messages, send):
    """Edit existing messages in place where content differs."""
    # Fetch existing messages in chronological order
    existing = []
    async for message in channel.history(limit=200):
        existing.append(message)
    existing.reverse()

    if len(existing) != len(new_messages):
        print(f"Message count changed ({len(existing)} -> {len(new_messages)}), reposting all.")
        await repost_all(channel, new_messages, send)
        return

    edited = 0
    skipped = 0
    for i, (old_msg, new_content) in enumerate(zip(existing, new_messages)):
        if old_msg.content == new_content:
            skipped += 1
            print(f"  Message {i+1}/{len(new_messages)}: unchanged, skipping")
        else:
            edited += 1
            if send:
                await old_msg.edit(content=new_content)
                await asyncio.sleep(0.5)
            print(f"  {'Edited' if send else 'Would edit'} message {i+1}/{len(new_messages)} (id: {old_msg.id})")

    print(f"\n{'Edited' if send else 'Would edit'} {edited}, skipped {skipped} unchanged.")


async def main(send: bool, repost: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(RULES_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(RULES_CHANNEL_ID)

            print(f"Found channel: #{channel.name}")

            new_messages = load_rules_messages()

            if repost:
                await repost_all(channel, new_messages, send)
            else:
                await edit_in_place(channel, new_messages, send)

            print("\nDone!" if send else "\nDry run complete. Use --send to execute.")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update rules channel from rules.md")
    parser.add_argument("--send", action="store_true", help="Actually make changes (default is dry run)")
    parser.add_argument("--repost", action="store_true", help="Force delete and repost all messages")
    args = parser.parse_args()

    asyncio.run(main(send=args.send, repost=args.repost))
