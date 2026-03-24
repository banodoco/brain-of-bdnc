#!/usr/bin/env python3
"""
View a Discord thread's messages with context, media, and structure.

Renders a thread from the database in a readable format showing:
- All messages with author names, timestamps
- Attachments and their types (image/video)
- Reply chains (reference_id)
- Reactions

Usage:
    # View a thread by its thread/channel ID
    python scripts/view_thread.py THREAD_ID

    # Show only messages with media
    python scripts/view_thread.py THREAD_ID --media-only

    # Limit number of messages
    python scripts/view_thread.py THREAD_ID --limit 20

    # Show attachment URLs
    python scripts/view_thread.py THREAD_ID --show-urls
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, '.env'))


def get_client():
    from supabase import create_client
    return create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])


def fetch_thread_messages(sb, thread_id: str, limit: int = 200, guild_id: Optional[int] = None) -> List[Dict]:
    """Fetch all messages from a thread, trying both channel_id and thread_id."""
    # Try as channel_id first (forum threads)
    q = sb.table('discord_messages').select('*').eq('channel_id', thread_id)
    if guild_id:
        q = q.eq('guild_id', guild_id)
    res = q.order('created_at').limit(limit).execute()

    if not res.data:
        # Try as thread_id (regular threads)
        q = sb.table('discord_messages').select('*').eq('thread_id', thread_id)
        if guild_id:
            q = q.eq('guild_id', guild_id)
        res = q.order('created_at').limit(limit).execute()

    return res.data or []


def fetch_members(sb, member_ids: List[str], guild_id: Optional[int] = None) -> Dict[str, str]:
    """Fetch member display names."""
    if not member_ids:
        return {}
    names = {}
    # Batch fetch
    for batch_start in range(0, len(member_ids), 50):
        batch = member_ids[batch_start:batch_start + 50]
        if guild_id:
            res = (
                sb.table('member_guild_profile')
                .select('member_id,display_name')
                .eq('guild_id', guild_id)
                .in_('member_id', batch)
                .execute()
            )
            for m in (res.data or []):
                names[str(m['member_id'])] = m.get('display_name') or str(m['member_id'])

        missing = [member_id for member_id in batch if member_id not in names]
        if missing:
            res = (
                sb.table('members')
                .select('member_id,username,global_name,server_nick')
                .in_('member_id', missing)
                .execute()
            )
            for m in (res.data or []):
                names[str(m['member_id'])] = m.get('server_nick') or m.get('global_name') or m.get('username') or str(m['member_id'])
    return names


def classify_attachment(att: Dict) -> str:
    """Classify an attachment as video, image, or other."""
    filename = att.get('filename', '').lower()
    content_type = att.get('content_type', '') or ''

    if any(filename.endswith(ext) for ext in ['.mp4', '.mov', '.webm', '.avi']):
        return 'video'
    if any(filename.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
        return 'image'
    if 'video' in content_type:
        return 'video'
    if 'image' in content_type:
        return 'image'
    return 'file'


def render_thread(messages: List[Dict], names: Dict[str, str], show_urls: bool = False, media_only: bool = False):
    """Render thread messages to stdout."""
    if not messages:
        print("No messages found.")
        return

    # Build a map of message_id -> message for reply resolution
    msg_map = {str(m['message_id']): m for m in messages}

    print(f"{'='*70}")
    print(f"Thread: {messages[0].get('channel_id') or messages[0].get('thread_id')}")
    print(f"Messages: {len(messages)}")

    # Count media
    video_count = 0
    image_count = 0
    for m in messages:
        atts = m.get('attachments', [])
        if isinstance(atts, str):
            try:
                atts = json.loads(atts)
            except:
                atts = []
        for a in (atts or []):
            t = classify_attachment(a)
            if t == 'video':
                video_count += 1
            elif t == 'image':
                image_count += 1

    print(f"Media: {video_count} videos, {image_count} images")
    print(f"{'='*70}\n")

    for m in messages:
        author_id = str(m.get('author_id', ''))
        author_name = names.get(author_id, author_id)
        created = m.get('created_at', '')[:16].replace('T', ' ')
        content = m.get('content', '') or ''
        ref_id = m.get('reference_id')
        reactions = m.get('reaction_count', 0)
        msg_id = m.get('message_id')

        atts = m.get('attachments', [])
        if isinstance(atts, str):
            try:
                atts = json.loads(atts)
            except:
                atts = []
        atts = atts or []

        classified = [(a, classify_attachment(a)) for a in atts]

        if media_only and not any(t in ('video', 'image') for _, t in classified):
            continue

        # Header
        reaction_str = f" [{reactions} reactions]" if reactions else ""
        print(f"[{created}] {author_name}{reaction_str}")

        # Reply indicator
        if ref_id:
            ref_msg = msg_map.get(str(ref_id))
            if ref_msg:
                ref_author = names.get(str(ref_msg.get('author_id', '')), '?')
                ref_content = (ref_msg.get('content', '') or '')[:60]
                print(f"  ↳ replying to {ref_author}: {ref_content}...")
            else:
                print(f"  ↳ replying to message {ref_id}")

        # Content
        if content:
            for line in content.split('\n'):
                print(f"  {line}")

        # Attachments
        for att, att_type in classified:
            emoji = {'video': '🎬', 'image': '🖼', 'file': '📎'}.get(att_type, '📎')
            filename = att.get('filename', '?')
            print(f"  {emoji} {filename}")
            if show_urls:
                print(f"     URL: {att.get('url', 'N/A')}")

        # Message ID for reference
        print(f"  (id: {msg_id})")
        print()


def main():
    parser = argparse.ArgumentParser(description="View a Discord thread from the database")
    parser.add_argument('thread_id', help='Thread or channel ID to view')
    parser.add_argument('--media-only', action='store_true', help='Show only messages with media')
    parser.add_argument('--show-urls', action='store_true', help='Show attachment URLs')
    parser.add_argument('--limit', type=int, default=200, help='Max messages to fetch')
    parser.add_argument('--videos-only', action='store_true', help='Show only messages with video attachments')
    parser.add_argument('--guild-id', type=int, help='Scope queries to a specific guild')
    args = parser.parse_args()

    sb = get_client()
    messages = fetch_thread_messages(sb, args.thread_id, args.limit, guild_id=args.guild_id)

    if not messages:
        print(f"No messages found for thread {args.thread_id}")
        sys.exit(1)

    guild_id = args.guild_id or messages[0].get('guild_id')

    # Collect unique author IDs
    author_ids = list(set(str(m.get('author_id', '')) for m in messages if m.get('author_id')))
    names = fetch_members(sb, author_ids, guild_id=guild_id)

    if args.videos_only:
        # Filter to only messages with video attachments
        filtered = []
        for m in messages:
            atts = m.get('attachments', [])
            if isinstance(atts, str):
                try:
                    atts = json.loads(atts)
                except:
                    atts = []
            if any(classify_attachment(a) == 'video' for a in (atts or [])):
                filtered.append(m)
        messages = filtered

    render_thread(messages, names, show_urls=args.show_urls, media_only=args.media_only)


if __name__ == '__main__':
    main()
