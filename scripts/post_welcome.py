"""
Post the welcome message to the gate channel from welcome.md.

By default edits existing message in place. Reposts if no message exists
or --repost is passed.

Usage:
    python scripts/post_welcome.py          # dry run
    python scripts/post_welcome.py --send   # post/edit for real
    python scripts/post_welcome.py --send --repost  # force delete and repost
"""

import asyncio
import argparse
import os

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GATE_CHANNEL_ID = int(os.getenv("GATE_CHANNEL_ID", "0"))

WELCOME_FILE = os.path.join(os.path.dirname(__file__), "..", "welcome.md")


def load_welcome_message():
    with open(WELCOME_FILE) as f:
        return f.read().strip()


async def main(send: bool, repost: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(GATE_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(GATE_CHANNEL_ID)

            print(f"Found channel: #{channel.name}")

            new_content = load_welcome_message()

            # Fetch existing bot messages
            existing = []
            async for message in channel.history(limit=50):
                if message.author == client.user:
                    existing.append(message)
            existing.reverse()

            if repost or not existing:
                # Delete old messages and post fresh
                for msg in existing:
                    if send:
                        await msg.delete()
                        await asyncio.sleep(0.5)
                    print(f"  {'Deleted' if send else 'Would delete'}: {msg.id}")

                if send:
                    sent = await channel.send(new_content)
                    print(f"  Posted welcome message (id: {sent.id})")
                else:
                    print(f"  Would post welcome message:")
                    print(f"    {new_content[:80]}...")
            else:
                # Edit first bot message in place
                old_msg = existing[0]
                if old_msg.content == new_content:
                    print("  Welcome message unchanged, skipping.")
                else:
                    if send:
                        await old_msg.edit(content=new_content)
                        print(f"  Edited welcome message (id: {old_msg.id})")
                    else:
                        print(f"  Would edit message {old_msg.id}")

            print("\nDone!" if send else "\nDry run complete. Use --send to execute.")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post welcome message to gate channel")
    parser.add_argument("--send", action="store_true", help="Actually make changes (default is dry run)")
    parser.add_argument("--repost", action="store_true", help="Force delete and repost")
    args = parser.parse_args()

    asyncio.run(main(send=args.send, repost=args.repost))
