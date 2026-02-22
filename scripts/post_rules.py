"""
Delete all existing messages in the rules channel and post the updated rules.

Usage:
    python scripts/post_rules.py          # dry run (shows what would happen)
    python scripts/post_rules.py --send   # actually delete and post
"""

import asyncio
import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
RULES_CHANNEL_ID = 1138515622582562947

RULES_FILE = os.path.join(os.path.dirname(__file__), "..", "rules.md")


def load_rules_messages():
    """Load rules from rules.md. Text before the first code block becomes
    the first message (e.g. a markdown header). Each code block becomes
    its own message."""
    import re
    with open(RULES_FILE) as f:
        content = f.read()

    messages = []
    # Extract any text before the first code block as its own message
    first_block = content.find('```')
    if first_block > 0:
        preamble = content[:first_block].strip()
        if preamble:
            messages.append(preamble)
        content = content[first_block:]

    # Split remaining content between closing ``` and opening ```
    blocks = re.split(r'```\s*\n\s*\n\s*```', content)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if not block.startswith('```'):
            block = '```' + block
        if not block.endswith('```'):
            block = block + '```'
        messages.append(block)
    return messages


async def main(send: bool):
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

            # Delete existing messages
            deleted = 0
            async for message in channel.history(limit=200):
                if send:
                    await message.delete()
                    await asyncio.sleep(0.5)  # rate limit safety
                deleted += 1
                print(f"  {'Deleted' if send else 'Would delete'}: {message.id} ({message.content[:50]}...)")

            print(f"\n{'Deleted' if send else 'Would delete'} {deleted} messages.\n")

            # Post new rules
            RULES_MESSAGES = load_rules_messages()
            for i, msg in enumerate(RULES_MESSAGES):
                if send:
                    sent = await channel.send(msg)
                    print(f"  Posted message {i+1}/{len(RULES_MESSAGES)} (id: {sent.id})")
                    await asyncio.sleep(0.5)
                else:
                    print(f"  Would post message {i+1}/{len(RULES_MESSAGES)}:")
                    print(f"    {msg[:80]}...")

            print(f"\n{'Posted' if send else 'Would post'} {len(RULES_MESSAGES)} messages.")
            print("\nDone!" if send else "\nDry run complete. Use --send to execute.")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post updated rules to the rules channel")
    parser.add_argument("--send", action="store_true", help="Actually delete and post (default is dry run)")
    args = parser.parse_args()

    asyncio.run(main(send=args.send))
