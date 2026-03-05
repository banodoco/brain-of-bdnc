"""
Post or update the grants guide as a pinned forum post in #micro-grants.

Usage:
    python scripts/post_grants_guide.py          # dry run
    python scripts/post_grants_guide.py --send   # post/edit for real
    python scripts/post_grants_guide.py --send --repost  # force delete and repost
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


def load_grants_messages():
    """Load grants guide from grants.md, split into messages by quote block."""
    with open(GRANTS_FILE) as f:
        content = f.read()

    messages = []
    parts = re.split(r'\n\n(?=>)', content)
    for part in parts:
        part = part.strip()
        if part:
            messages.append(part)
    return messages


async def find_guide_thread(forum):
    """Find an existing guide thread by name."""
    # Check active threads
    for thread in forum.threads:
        if thread.name == GUIDE_THREAD_NAME and thread.owner_id == forum._state.user.id:
            return thread

    # Check archived threads
    async for thread in forum.archived_threads(limit=50):
        if thread.name == GUIDE_THREAD_NAME and thread.owner_id == forum._state.user.id:
            return thread

    return None


async def main(send: bool, repost: bool):
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

            # First message is the thread starter, rest are follow-up messages
            starter_content = new_messages[0]
            followup_messages = new_messages[1:]

            existing_thread = await find_guide_thread(channel)

            if existing_thread and not repost:
                # Edit existing thread messages in place
                print(f"Found existing guide thread: {existing_thread.id}")

                # Unarchive if needed
                if existing_thread.archived:
                    if send:
                        await existing_thread.edit(archived=False)
                    print(f"  {'Unarchived' if send else 'Would unarchive'} thread")

                # Get existing messages
                existing_msgs = []
                async for msg in existing_thread.history(limit=200):
                    if msg.author.id == client.user.id:
                        existing_msgs.append(msg)
                existing_msgs.reverse()

                all_new = [starter_content] + followup_messages

                if len(existing_msgs) != len(all_new):
                    print(f"Message count changed ({len(existing_msgs)} -> {len(all_new)}), reposting.")
                    repost_flag = True
                else:
                    repost_flag = False
                    edited = 0
                    for i, (old_msg, new_content) in enumerate(zip(existing_msgs, all_new)):
                        if old_msg.content == new_content:
                            print(f"  Message {i+1}: unchanged")
                        else:
                            if send:
                                await old_msg.edit(content=new_content)
                                await asyncio.sleep(0.5)
                            edited += 1
                            print(f"  {'Edited' if send else 'Would edit'} message {i+1}")
                    print(f"\n{'Edited' if send else 'Would edit'} {edited} messages.")

                if not repost_flag:
                    # Pin the thread
                    if send and not existing_thread.pinned:
                        await existing_thread.edit(pinned=True)
                        print("Pinned thread.")
                    print("\nDone!" if send else "\nDry run complete. Use --send to execute.")
                    return

            # Create new thread (or repost)
            if existing_thread and repost:
                print(f"Deleting existing guide thread: {existing_thread.id}")
                if send:
                    if existing_thread.archived:
                        await existing_thread.edit(archived=False)
                    await existing_thread.delete()
                    await asyncio.sleep(1)

            print(f"\n{'Creating' if send else 'Would create'} forum post: \"{GUIDE_THREAD_NAME}\"")
            print(f"  Starter: {starter_content[:80]}...")

            if send:
                thread_with_message = await channel.create_thread(
                    name=GUIDE_THREAD_NAME,
                    content=starter_content,
                )
                thread = thread_with_message.thread if hasattr(thread_with_message, 'thread') else thread_with_message
                print(f"  Created thread: {thread.id}")

                for i, msg in enumerate(followup_messages):
                    await thread.send(msg)
                    await asyncio.sleep(0.5)
                    print(f"  Posted follow-up {i+1}/{len(followup_messages)}")

                # Pin the thread
                await thread.edit(pinned=True)
                print("  Pinned thread.")
            else:
                for i, msg in enumerate(followup_messages):
                    print(f"  Would post follow-up {i+1}: {msg[:80]}...")

            print(f"\n{'Done!' if send else 'Dry run complete. Use --send to execute.'}")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post grants guide to forum channel")
    parser.add_argument("--send", action="store_true", help="Actually make changes (default is dry run)")
    parser.add_argument("--repost", action="store_true", help="Force delete and repost")
    args = parser.parse_args()

    asyncio.run(main(send=args.send, repost=args.repost))
