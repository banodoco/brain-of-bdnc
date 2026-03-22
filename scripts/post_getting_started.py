"""
Update the getting-started channel from getting_started.md.

By default, edits existing messages in place. Only deletes and reposts
when the number of messages changes or attachments differ (--repost to force).

Also uploads any local attachment files to the content-assets Supabase
Storage bucket so the ContentCog can serve them during auto-sync.

Usage:
    python scripts/post_getting_started.py          # dry run
    python scripts/post_getting_started.py --send   # edit/post for real
    python scripts/post_getting_started.py --send --repost  # force delete and repost all
"""

import asyncio
import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import discord
from supabase import create_client
from src.common.server_config import ServerConfig
from src.features.content.content_cog import ContentCog, ContentSegment, CONTENT_ASSETS_BUCKET, CONTENT_REGISTRY

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "1148315179336081478"))

POSTS_DIR = Path(__file__).resolve().parent.parent / 'posts'
GETTING_STARTED_FILE = POSTS_DIR / 'getting_started.md'


def _get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def _get_server_config():
    client = _get_supabase()
    return ServerConfig(client) if client else None


def load_getting_started_messages():
    """Load content from server_content or local file, split by quote blocks."""
    content = None
    if GUILD_ID:
        sc = _get_server_config()
        if sc:
            content = sc.get_content(GUILD_ID, 'post_getting_started')
    if not content:
        content = GETTING_STARTED_FILE.read_text()
    _, split_pattern, _ = CONTENT_REGISTRY['post_getting_started']
    return ContentCog._split_content(content, split_pattern)


def upload_attachments_to_supabase(segments, sb):
    """Upload any local attachment files to the content-assets bucket."""
    if not sb:
        return
    for seg in segments:
        if not seg.attachment:
            continue
        local_path = POSTS_DIR / seg.attachment
        if not local_path.exists():
            print(f"  Warning: attachment {local_path} not found locally, skipping upload")
            continue
        storage_path = f"{GUILD_ID}/{seg.attachment}"
        file_bytes = local_path.read_bytes()
        # Guess content type
        ext = local_path.suffix.lower()
        content_types = {'.gif': 'image/gif', '.png': 'image/png', '.jpg': 'image/jpeg',
                         '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.mp4': 'video/mp4'}
        content_type = content_types.get(ext, 'application/octet-stream')
        try:
            sb.storage.from_(CONTENT_ASSETS_BUCKET).upload(
                path=storage_path, file=file_bytes,
                file_options={"content-type": content_type, "upsert": "true"}
            )
            print(f"  Uploaded {seg.attachment} to {CONTENT_ASSETS_BUCKET}/{storage_path}")
        except Exception as e:
            print(f"  Warning: failed to upload {seg.attachment}: {e}")


def _make_local_file(seg):
    """Create a discord.File from a local file in posts/."""
    if not seg.attachment:
        return discord.utils.MISSING
    local_path = POSTS_DIR / seg.attachment
    if not local_path.exists():
        print(f"  Warning: attachment {local_path} not found")
        return discord.utils.MISSING
    return discord.File(str(local_path), filename=seg.attachment)


async def repost_all(channel, segments, send):
    """Delete all existing messages and post fresh."""
    deleted = 0
    async for message in channel.history(limit=200):
        if send:
            await message.delete()
            await asyncio.sleep(0.5)
        deleted += 1
        content_preview = (message.content or '')[:50]
        print(f"  {'Deleted' if send else 'Would delete'}: {message.id} ({content_preview}...)")

    print(f"\n{'Deleted' if send else 'Would delete'} {deleted} messages.\n")

    for i, seg in enumerate(segments):
        att_info = f" + {seg.attachment}" if seg.attachment else ""
        if send:
            file = _make_local_file(seg)
            sent = await channel.send(content=seg.text, file=file)
            print(f"  Posted message {i+1}/{len(segments)} (id: {sent.id}){att_info}")
            await asyncio.sleep(0.5)
        else:
            text_preview = (seg.text or '')[:80]
            print(f"  Would post message {i+1}/{len(segments)}:{att_info}")
            print(f"    {text_preview}...")

    print(f"\n{'Posted' if send else 'Would post'} {len(segments)} messages.")


async def edit_in_place(channel, segments, send):
    """Edit existing messages in place where content differs."""
    existing = []
    async for message in channel.history(limit=200):
        existing.append(message)
    existing.reverse()

    if len(existing) != len(segments):
        print(f"Message count changed ({len(existing)} -> {len(segments)}), reposting all.")
        await repost_all(channel, segments, send)
        return

    # If any segment has attachments that changed, repost all
    has_att_change = any(
        ContentCog._attachment_changed(msg, seg)
        for msg, seg in zip(existing, segments)
    )
    if has_att_change:
        print("Attachment change detected, reposting all.")
        await repost_all(channel, segments, send)
        return

    edited = 0
    skipped = 0
    for i, (old_msg, seg) in enumerate(zip(existing, segments)):
        if old_msg.content == seg.text:
            skipped += 1
            print(f"  Message {i+1}/{len(segments)}: unchanged, skipping")
        else:
            edited += 1
            if send:
                await old_msg.edit(content=seg.text)
                await asyncio.sleep(0.5)
            print(f"  {'Edited' if send else 'Would edit'} message {i+1}/{len(segments)} (id: {old_msg.id})")

    print(f"\n{'Edited' if send else 'Would edit'} {edited}, skipped {skipped} unchanged.")


async def main(send: bool, repost: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            sc = _get_server_config()
            welcome_channel_id = (sc.get_server_field(GUILD_ID, 'welcome_channel_id', cast=int) if sc else None) or WELCOME_CHANNEL_ID
            channel = client.get_channel(welcome_channel_id)
            if channel is None:
                channel = await client.fetch_channel(welcome_channel_id)

            print(f"Found channel: #{channel.name}")

            segments = load_getting_started_messages()

            # Upload attachments to Supabase Storage
            if send:
                sb = _get_supabase()
                upload_attachments_to_supabase(segments, sb)

            if repost:
                await repost_all(channel, segments, send)
            else:
                await edit_in_place(channel, segments, send)

            print("\nDone!" if send else "\nDry run complete. Use --send to execute.")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update getting-started channel from getting_started.md")
    parser.add_argument("--send", action="store_true", help="Actually make changes (default is dry run)")
    parser.add_argument("--repost", action="store_true", help="Force delete and repost all messages")
    args = parser.parse_args()

    asyncio.run(main(send=args.send, repost=args.repost))
