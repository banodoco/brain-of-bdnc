"""
Post the grants guide as a pinned forum post in #micro-grants.
Always deletes and reposts to ensure clean formatting.

Usage:
    python scripts/post_grants_guide.py          # dry run
    python scripts/post_grants_guide.py --send   # post for real
"""

import asyncio
import argparse
import os
import re

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GRANTS_CHANNEL_ID = int(os.getenv("GRANTS_CHANNEL_ID", "1479173703441846404"))

GRANTS_FILE = os.path.join(os.path.dirname(__file__), "..", "grants.md")
GUIDE_THREAD_NAME = "How Micro-Grants Work"
QUESTIONS_THREAD_NAME = "Questions & Discussion"


def load_grants_messages():
    """Load grants guide from grants.md, split into messages by ### headers."""
    with open(GRANTS_FILE) as f:
        content = f.read()

    messages = []
    parts = re.split(r'\n\n(?=###\s)', content)
    for part in parts:
        part = part.strip()
        if part:
            messages.append(part)
    return messages


async def find_bot_threads(forum, bot_id):
    """Find existing guide and questions threads owned by the bot."""
    threads = {}
    for thread in forum.threads:
        if thread.owner_id == bot_id:
            if thread.name == GUIDE_THREAD_NAME:
                threads['guide'] = thread
            elif thread.name == QUESTIONS_THREAD_NAME:
                threads['questions'] = thread

    async for thread in forum.archived_threads(limit=50):
        if thread.owner_id == bot_id:
            if thread.name == GUIDE_THREAD_NAME and 'guide' not in threads:
                threads['guide'] = thread
            elif thread.name == QUESTIONS_THREAD_NAME and 'questions' not in threads:
                threads['questions'] = thread

    return threads


async def delete_thread(thread, send):
    """Delete a thread, unarchiving first if needed."""
    if send:
        if thread.archived:
            await thread.edit(archived=False)
            await asyncio.sleep(0.5)
        # Unpin before deleting
        if hasattr(thread.flags, 'pinned') and thread.flags.pinned:
            await thread.edit(pinned=False)
            await asyncio.sleep(0.5)
        await thread.delete()
        await asyncio.sleep(1)
    print(f"  {'Deleted' if send else 'Would delete'} thread: {thread.name} ({thread.id})")


async def main(send: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(GRANTS_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(GRANTS_CHANNEL_ID)

            if not isinstance(channel, discord.ForumChannel):
                print(f"Error: #{channel.name} is not a forum channel")
                return

            print(f"Found forum channel: #{channel.name}")

            new_messages = load_grants_messages()
            if not new_messages:
                print("No content found in grants.md")
                return

            starter_content = new_messages[0]
            followup_messages = new_messages[1:]

            # Find existing threads
            existing = await find_bot_threads(channel, client.user.id)

            # Only delete the guide thread — keep questions thread
            if 'guide' in existing:
                await delete_thread(existing['guide'], send)

            # Create guide thread
            print(f"\n{'Creating' if send else 'Would create'} forum post: \"{GUIDE_THREAD_NAME}\"")

            if send:
                result = await channel.create_thread(
                    name=GUIDE_THREAD_NAME,
                    content=starter_content,
                )
                guide_thread = result.thread if hasattr(result, 'thread') else result
                print(f"  Created thread: {guide_thread.id}")

                for i, msg in enumerate(followup_messages):
                    await guide_thread.send(msg)
                    await asyncio.sleep(0.5)
                    print(f"  Posted message {i+2}/{len(new_messages)}")

                await guide_thread.edit(pinned=True, locked=True)
                print("  Pinned and locked.")
            else:
                print(f"  Starter: {starter_content[:80]}...")
                for i, msg in enumerate(followup_messages):
                    print(f"  Message {i+2}: {msg[:80]}...")

            # Create questions thread only if it doesn't exist
            if 'questions' not in existing:
                print(f"\n{'Creating' if send else 'Would create'} forum post: \"{QUESTIONS_THREAD_NAME}\"")

                questions_content = (
                    "Use this thread for questions about the micro-grants program.\n\n"
                    "For grant applications, create a new post in the forum instead."
                )

                if send:
                    result = await channel.create_thread(
                        name=QUESTIONS_THREAD_NAME,
                        content=questions_content,
                    )
                    q_thread = result.thread if hasattr(result, 'thread') else result
                    print(f"  Created thread: {q_thread.id}")
                else:
                    print(f"  Content: {questions_content[:80]}...")
            else:
                print(f"\nQuestions thread already exists ({existing['questions'].id}), keeping it.")

            print(f"\n{'Done!' if send else 'Dry run complete. Use --send to execute.'}")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post grants guide to forum channel")
    parser.add_argument("--send", action="store_true", help="Actually make changes (default is dry run)")
    args = parser.parse_args()

    asyncio.run(main(send=args.send))
