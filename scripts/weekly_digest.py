#!/usr/bin/env python3
"""
Weekly Digest Utilities

Helper functions for an agent to explore Discord messages and create weekly digest documents.

Usage:
    python scripts/weekly_digest.py channels CATEGORY_ID     # List channels in category
    python scripts/weekly_digest.py messages CHANNEL_ID      # Get messages from past week
    python scripts/weekly_digest.py top CHANNEL_ID           # Get top messages by reactions
    python scripts/weekly_digest.py context MESSAGE_ID       # Get message + replies + surrounding
    python scripts/weekly_digest.py thread MESSAGE_ID        # Follow reply chain up and down
    python scripts/weekly_digest.py user USERNAME            # Get messages from a user
    python scripts/weekly_digest.py media CHANNEL_ID         # Get messages with attachments only
    python scripts/weekly_digest.py search QUERY             # Search messages by content
    python scripts/weekly_digest.py refresh MESSAGE_ID       # Refresh media URLs for a message
    python scripts/weekly_digest.py batch-refresh MSG_IDS    # Refresh multiple messages
    python scripts/weekly_digest.py category-top CATEGORY_ID # Top messages across all channels in category
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def get_client():
    """Get Supabase client."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


def get_channels_in_category(category_id: int) -> List[Dict]:
    """Get all channels in a category."""
    client = get_client()
    result = client.table('discord_channels').select('channel_id, channel_name').eq('category_id', category_id).execute()
    return result.data


def get_messages_in_range(channel_id: int, days: int = 7) -> List[Dict]:
    """Get messages from a channel within the last N days."""
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    result = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors, reference_id'
    ).eq('channel_id', channel_id).gte('created_at', cutoff).order('created_at', desc=True).execute()
    
    # Enrich with author names
    messages = result.data
    if messages:
        author_ids = list(set(m['author_id'] for m in messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
    
    return messages


def get_top_messages(channel_id: int, days: int = 7, min_reactions: int = 3, limit: int = 20) -> List[Dict]:
    """Get top messages by reaction count from a channel."""
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    result = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors, reference_id'
    ).eq('channel_id', channel_id).gte('created_at', cutoff).gte('reaction_count', min_reactions).order('reaction_count', desc=True).limit(limit).execute()
    
    # Enrich with author names
    messages = result.data
    if messages:
        author_ids = list(set(m['author_id'] for m in messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
            # Parse reactors count
            reactors = msg.get('reactors', [])
            if isinstance(reactors, str):
                try:
                    reactors = json.loads(reactors)
                except:
                    reactors = []
            msg['unique_reactor_count'] = len(reactors) if isinstance(reactors, list) else 0
    
    return messages


def get_top_messages_in_category(category_id: int, days: int = 7, min_reactions: int = 3, limit: int = 30) -> List[Dict]:
    """Get top messages across all channels in a category."""
    channels = get_channels_in_category(category_id)
    channel_ids = [ch['channel_id'] for ch in channels]
    channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels}
    
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    result = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors, reference_id'
    ).in_('channel_id', channel_ids).gte('created_at', cutoff).gte('reaction_count', min_reactions).order('reaction_count', desc=True).limit(limit).execute()
    
    # Enrich with author names and channel names
    messages = result.data
    if messages:
        author_ids = list(set(m['author_id'] for m in messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
            msg['channel_name'] = channel_names.get(msg['channel_id'], 'Unknown')
            # Parse reactors count
            reactors = msg.get('reactors', [])
            if isinstance(reactors, str):
                try:
                    reactors = json.loads(reactors)
                except:
                    reactors = []
            msg['unique_reactor_count'] = len(reactors) if isinstance(reactors, list) else 0
    
    return messages


def get_message_context(message_id: int, surrounding: int = 5) -> Dict[str, Any]:
    """
    Get a message with its full context:
    - The message itself
    - All replies to it
    - Surrounding messages (before and after)
    """
    client = get_client()
    
    # Get the target message
    result = client.table('discord_messages').select('*').eq('message_id', message_id).execute()
    if not result.data:
        return {"error": f"Message {message_id} not found"}
    
    target_msg = result.data[0]
    channel_id = target_msg['channel_id']
    created_at = target_msg['created_at']
    
    # Get replies (messages that reference this message)
    replies_result = client.table('discord_messages').select('*').eq('reference_id', message_id).order('created_at').execute()
    replies = replies_result.data
    
    # Get surrounding messages (before)
    before_result = client.table('discord_messages').select('*').eq('channel_id', channel_id).lt('created_at', created_at).order('created_at', desc=True).limit(surrounding).execute()
    before = list(reversed(before_result.data))
    
    # Get surrounding messages (after)
    after_result = client.table('discord_messages').select('*').eq('channel_id', channel_id).gt('created_at', created_at).order('created_at').limit(surrounding).execute()
    after = after_result.data
    
    # Enrich all messages with author names
    all_messages = [target_msg] + replies + before + after
    if all_messages:
        author_ids = list(set(m['author_id'] for m in all_messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in all_messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
    
    return {
        "target_message": target_msg,
        "replies": replies,
        "before": before,
        "after": after,
        "reply_count": len(replies)
    }


def get_message_by_id(message_id: int) -> Optional[Dict]:
    """Get a single message by ID with author name."""
    client = get_client()
    result = client.table('discord_messages').select('*').eq('message_id', message_id).execute()
    
    if not result.data:
        return None
    
    msg = result.data[0]
    
    # Get author name
    member_result = client.table('discord_members').select('username, global_name, server_nick').eq('member_id', msg['author_id']).execute()
    if member_result.data:
        m = member_result.data[0]
        msg['author_name'] = m.get('server_nick') or m.get('global_name') or m.get('username')
    else:
        msg['author_name'] = 'Unknown'
    
    return msg


def get_user_by_name(username: str) -> Optional[Dict]:
    """Find a user by username (partial match)."""
    client = get_client()
    # Try exact match first
    result = client.table('discord_members').select('*').eq('username', username).execute()
    if result.data:
        return result.data[0]
    
    # Try server_nick
    result = client.table('discord_members').select('*').eq('server_nick', username).execute()
    if result.data:
        return result.data[0]
    
    # Try global_name
    result = client.table('discord_members').select('*').eq('global_name', username).execute()
    if result.data:
        return result.data[0]
    
    # Try case-insensitive partial match on username
    result = client.table('discord_members').select('*').ilike('username', f'%{username}%').limit(10).execute()
    if result.data:
        return result.data[0]  # Return first match
    
    return None


def get_messages_by_user(user_id: int, channel_ids: List[int] = None, days: int = 7, limit: int = 50) -> List[Dict]:
    """Get messages from a specific user."""
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors, reference_id'
    ).eq('author_id', user_id).gte('created_at', cutoff).order('created_at', desc=True).limit(limit)
    
    if channel_ids:
        query = query.in_('channel_id', channel_ids)
    
    result = query.execute()
    messages = result.data
    
    # Get author name
    member_result = client.table('discord_members').select('username, global_name, server_nick').eq('member_id', user_id).execute()
    author_name = 'Unknown'
    if member_result.data:
        m = member_result.data[0]
        author_name = m.get('server_nick') or m.get('global_name') or m.get('username')
    
    # Get channel names
    if messages:
        channel_ids_found = list(set(m['channel_id'] for m in messages))
        channels_result = client.table('discord_channels').select('channel_id, channel_name').in_('channel_id', channel_ids_found).execute()
        channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels_result.data}
        
        for msg in messages:
            msg['author_name'] = author_name
            msg['channel_name'] = channel_names.get(msg['channel_id'], 'Unknown')
    
    return messages


def get_messages_with_media(channel_id: int = None, category_id: int = None, days: int = 7, min_reactions: int = 0, limit: int = 30) -> List[Dict]:
    """Get messages that have attachments."""
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    # Determine which channels to search
    channel_ids = []
    channel_names = {}
    
    if category_id:
        channels = get_channels_in_category(category_id)
        channel_ids = [ch['channel_id'] for ch in channels]
        channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels}
    elif channel_id:
        channel_ids = [channel_id]
        ch_result = client.table('discord_channels').select('channel_name').eq('channel_id', channel_id).execute()
        if ch_result.data:
            channel_names[channel_id] = ch_result.data[0]['channel_name']
    
    # Query messages with non-empty attachments
    query = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors'
    ).gte('created_at', cutoff).neq('attachments', []).order('reaction_count', desc=True).limit(limit)
    
    if channel_ids:
        query = query.in_('channel_id', channel_ids)
    
    if min_reactions > 0:
        query = query.gte('reaction_count', min_reactions)
    
    result = query.execute()
    messages = result.data
    
    # Enrich with author names
    if messages:
        author_ids = list(set(m['author_id'] for m in messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
            msg['channel_name'] = channel_names.get(msg['channel_id'], 'Unknown')
    
    return messages


def search_messages_by_content(query_text: str, channel_ids: List[int] = None, days: int = 7, limit: int = 30) -> List[Dict]:
    """Search messages by content (case-insensitive partial match)."""
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = client.table('discord_messages').select(
        'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors'
    ).gte('created_at', cutoff).ilike('content', f'%{query_text}%').order('created_at', desc=True).limit(limit)
    
    if channel_ids:
        query = query.in_('channel_id', channel_ids)
    
    result = query.execute()
    messages = result.data
    
    # Enrich with author and channel names
    if messages:
        author_ids = list(set(m['author_id'] for m in messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        channel_ids_found = list(set(m['channel_id'] for m in messages))
        channels_result = client.table('discord_channels').select('channel_id, channel_name').in_('channel_id', channel_ids_found).execute()
        channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels_result.data}
        
        for msg in messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
            msg['channel_name'] = channel_names.get(msg['channel_id'], 'Unknown')
    
    return messages


def get_thread_chain(message_id: int) -> Dict[str, Any]:
    """
    Follow a reply chain up and down:
    - Find the root message (follow reference_id up)
    - Find all messages in the thread (all replies to root and subsequent replies)
    """
    client = get_client()
    
    # Get the starting message
    result = client.table('discord_messages').select('*').eq('message_id', message_id).execute()
    if not result.data:
        return {"error": f"Message {message_id} not found"}
    
    current_msg = result.data[0]
    thread_messages = [current_msg]
    visited = {message_id}
    
    # Follow reference_id UP to find root
    root_msg = current_msg
    while root_msg.get('reference_id'):
        ref_id = root_msg['reference_id']
        if ref_id in visited:
            break
        visited.add(ref_id)
        
        parent_result = client.table('discord_messages').select('*').eq('message_id', ref_id).execute()
        if not parent_result.data:
            break
        root_msg = parent_result.data[0]
        thread_messages.insert(0, root_msg)
    
    # Now find all replies DOWN from root (BFS)
    queue = [root_msg['message_id']]
    while queue:
        current_id = queue.pop(0)
        replies_result = client.table('discord_messages').select('*').eq('reference_id', current_id).order('created_at').execute()
        
        for reply in replies_result.data:
            if reply['message_id'] not in visited:
                visited.add(reply['message_id'])
                thread_messages.append(reply)
                queue.append(reply['message_id'])
    
    # Sort by created_at
    thread_messages.sort(key=lambda x: x['created_at'])
    
    # Enrich with author names
    if thread_messages:
        author_ids = list(set(m['author_id'] for m in thread_messages))
        members_result = client.table('discord_members').select('member_id, username, global_name, server_nick').in_('member_id', author_ids).execute()
        member_map = {m['member_id']: m.get('server_nick') or m.get('global_name') or m.get('username') for m in members_result.data}
        
        for msg in thread_messages:
            msg['author_name'] = member_map.get(msg['author_id'], 'Unknown')
    
    return {
        "root_message": thread_messages[0] if thread_messages else None,
        "target_message_id": message_id,
        "thread": thread_messages,
        "thread_length": len(thread_messages)
    }


def batch_refresh_media(message_ids: List[int], dry_run: bool = False) -> Dict[str, Any]:
    """Refresh media URLs for multiple messages."""
    results = {
        "success": [],
        "failed": [],
        "no_attachments": []
    }
    
    for msg_id in message_ids:
        result = refresh_message_media(msg_id, dry_run=dry_run)
        
        if result.get("error"):
            results["failed"].append({"message_id": msg_id, "error": result["error"]})
        elif result.get("message") == "No attachments to refresh":
            results["no_attachments"].append(msg_id)
        elif result.get("success"):
            results["success"].append({
                "message_id": msg_id,
                "urls": result.get("new_urls", [])
            })
        else:
            results["failed"].append({"message_id": msg_id, "error": "Unknown error"})
    
    return results


def refresh_message_media(message_id: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    Refresh media URLs for a message by fetching from Discord API.
    Returns dict with old_urls, new_urls, and success status.
    """
    import discord
    from discord.ext import commands
    
    client = get_client()
    
    # Get message from DB
    result = client.table('discord_messages').select('channel_id, attachments').eq('message_id', message_id).execute()
    if not result.data:
        return {"error": f"Message {message_id} not found in database"}
    
    channel_id = result.data[0]['channel_id']
    old_attachments = result.data[0]['attachments']
    
    if isinstance(old_attachments, str):
        old_attachments = json.loads(old_attachments)
    
    if not old_attachments:
        return {"message": "No attachments to refresh", "success": True}
    
    bot_token = os.getenv('DISCORD_BOT_TOKEN')
    if not bot_token:
        return {"error": "DISCORD_BOT_TOKEN not set"}
    
    refresh_result = {"old_urls": [], "new_urls": [], "success": False}
    
    for att in old_attachments:
        refresh_result["old_urls"].append(att.get('url', ''))
    
    async def do_refresh():
        intents = discord.Intents.default()
        intents.message_content = True
        bot = commands.Bot(command_prefix='!', intents=intents)
        
        @bot.event
        async def on_ready():
            try:
                from src.common.discord_utils import refresh_media_url
                result = await refresh_media_url(bot, channel_id, message_id)
                
                if result and result.get('success'):
                    refresh_result["new_urls"] = [att.get('url', '') for att in result['attachments']]
                    refresh_result["fresh_attachments"] = result['attachments']
                    refresh_result["success"] = True
                    
                    if not dry_run:
                        # Update DB
                        client.table('discord_messages').update({'attachments': result['attachments']}).eq('message_id', message_id).execute()
                        refresh_result["db_updated"] = True
                    else:
                        refresh_result["db_updated"] = False
            except Exception as e:
                refresh_result["error"] = str(e)
            finally:
                await bot.close()
        
        try:
            await bot.start(bot_token)
        except Exception as e:
            if "Event loop is closed" not in str(e):
                refresh_result["error"] = str(e)
    
    asyncio.run(do_refresh())
    return refresh_result


def format_message_for_display(msg: Dict, include_attachments: bool = True) -> str:
    """Format a message for readable display."""
    lines = []
    lines.append(f"📝 **{msg.get('author_name', 'Unknown')}** ({msg['created_at'][:16].replace('T', ' ')})")
    lines.append(f"   Message ID: {msg['message_id']}")
    
    if msg.get('content'):
        content = msg['content'][:500] + '...' if len(msg.get('content', '')) > 500 else msg['content']
        lines.append(f"   Content: {content}")
    
    if msg.get('reaction_count'):
        lines.append(f"   🔥 Reactions: {msg['reaction_count']}")
    
    if include_attachments and msg.get('attachments'):
        attachments = msg['attachments']
        if isinstance(attachments, str):
            try:
                attachments = json.loads(attachments)
            except:
                attachments = []
        if attachments:
            lines.append(f"   📎 Attachments: {len(attachments)}")
            for att in attachments[:3]:  # Show first 3
                lines.append(f"      - {att.get('filename', 'file')}: {att.get('url', '')[:60]}...")
    
    return "\n".join(lines)


def generate_jump_url(channel_id: int, message_id: int, guild_id: int = None) -> str:
    """Generate Discord jump URL for a message."""
    if guild_id is None:
        guild_id = int(os.getenv('GUILD_ID', os.getenv('DEV_GUILD_ID', '0')))
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# ============ CLI Commands ============

def cmd_channels(args):
    """List channels in a category."""
    channels = get_channels_in_category(args.id)
    print(f"\n📂 Channels in category {args.id}:\n")
    for ch in channels:
        print(f"  - {ch['channel_name']} ({ch['channel_id']})")
    print(f"\nTotal: {len(channels)} channels")


def cmd_messages(args):
    """Get messages from a channel."""
    messages = get_messages_in_range(args.id, days=args.days)
    print(f"\n📨 Messages from channel {args.id} (last {args.days} days):\n")
    print(f"Total: {len(messages)} messages\n")
    
    for msg in messages[:args.limit]:
        print(format_message_for_display(msg))
        print()


def cmd_top(args):
    """Get top messages by reactions."""
    messages = get_top_messages(args.id, days=args.days, min_reactions=args.min_reactions, limit=args.limit)
    print(f"\n🔥 Top messages from channel {args.id} (last {args.days} days, min {args.min_reactions} reactions):\n")
    
    for i, msg in enumerate(messages, 1):
        print(f"--- #{i} ---")
        print(format_message_for_display(msg))
        print(f"   🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_category_top(args):
    """Get top messages across all channels in a category."""
    messages = get_top_messages_in_category(args.id, days=args.days, min_reactions=args.min_reactions, limit=args.limit)
    print(f"\n🔥 Top messages across category {args.id} (last {args.days} days, min {args.min_reactions} reactions):\n")
    
    for i, msg in enumerate(messages, 1):
        print(f"--- #{i} ({msg.get('channel_name', 'Unknown')}) ---")
        print(format_message_for_display(msg))
        print(f"   🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_context(args):
    """Get message context (replies, surrounding messages)."""
    context = get_message_context(args.id, surrounding=args.surrounding)
    
    if context.get("error"):
        print(f"❌ {context['error']}")
        return
    
    print(f"\n📖 Context for message {args.id}:\n")
    
    print("=== BEFORE ===")
    for msg in context['before']:
        print(format_message_for_display(msg, include_attachments=False))
        print()
    
    print("=== TARGET MESSAGE ===")
    print(format_message_for_display(context['target_message']))
    print(f"   🔗 {generate_jump_url(context['target_message']['channel_id'], context['target_message']['message_id'])}")
    print()
    
    print("=== AFTER ===")
    for msg in context['after']:
        print(format_message_for_display(msg, include_attachments=False))
        print()
    
    if context['replies']:
        print(f"=== REPLIES ({context['reply_count']}) ===")
        for msg in context['replies']:
            print(format_message_for_display(msg, include_attachments=False))
            print()


def cmd_refresh(args):
    """Refresh media URLs for a message."""
    print(f"\n🔄 Refreshing media for message {args.id}...")
    
    result = refresh_message_media(args.id, dry_run=args.dry_run)
    
    if result.get("error"):
        print(f"❌ Error: {result['error']}")
        return
    
    if result.get("message"):
        print(f"ℹ️  {result['message']}")
        return
    
    print(f"\n✅ Success!")
    print(f"   Old URLs: {len(result['old_urls'])}")
    for url in result['old_urls']:
        print(f"      {url[:80]}...")
    
    print(f"   New URLs: {len(result['new_urls'])}")
    for url in result['new_urls']:
        print(f"      {url[:80]}...")
    
    if result.get('db_updated'):
        print("   💾 Database updated")
    elif args.dry_run:
        print("   🔍 Dry run - database not updated")


def cmd_user(args):
    """Get messages from a specific user."""
    # Find user
    user = get_user_by_name(args.username)
    if not user:
        print(f"❌ User '{args.username}' not found")
        return
    
    user_id = user['member_id']
    display_name = user.get('server_nick') or user.get('global_name') or user.get('username')
    
    # Get channel IDs if category specified
    channel_ids = None
    if args.category:
        channels = get_channels_in_category(args.category)
        channel_ids = [ch['channel_id'] for ch in channels]
    
    messages = get_messages_by_user(user_id, channel_ids=channel_ids, days=args.days, limit=args.limit)
    
    print(f"\n👤 Messages from **{display_name}** (last {args.days} days):\n")
    print(f"Total: {len(messages)} messages\n")
    
    for msg in messages:
        print(f"[{msg.get('channel_name', 'Unknown')}]")
        print(format_message_for_display(msg))
        print(f"   🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_thread(args):
    """Follow a reply chain."""
    result = get_thread_chain(args.id)
    
    if result.get("error"):
        print(f"❌ {result['error']}")
        return
    
    print(f"\n🧵 Thread containing message {args.id}:\n")
    print(f"Thread length: {result['thread_length']} messages\n")
    
    for i, msg in enumerate(result['thread']):
        is_target = msg['message_id'] == args.id
        prefix = ">>> " if is_target else "    "
        
        # Show reply indicator
        if msg.get('reference_id'):
            print(f"{prefix}↳ (reply to {msg['reference_id']})")
        
        print(f"{prefix}{'=== TARGET ===' if is_target else ''}")
        print(format_message_for_display(msg).replace('\n', f'\n{prefix}'))
        print(f"{prefix}🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_media(args):
    """Get messages with attachments only."""
    messages = get_messages_with_media(
        channel_id=args.channel,
        category_id=args.category,
        days=args.days,
        min_reactions=args.min_reactions,
        limit=args.limit
    )
    
    scope = f"category {args.category}" if args.category else f"channel {args.channel}" if args.channel else "all channels"
    print(f"\n📎 Messages with media from {scope} (last {args.days} days):\n")
    print(f"Total: {len(messages)} messages\n")
    
    for i, msg in enumerate(messages, 1):
        print(f"--- #{i} ({msg.get('channel_name', 'Unknown')}) ---")
        print(format_message_for_display(msg))
        print(f"   🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_search(args):
    """Search messages by content."""
    # Get channel IDs if category specified
    channel_ids = None
    if args.category:
        channels = get_channels_in_category(args.category)
        channel_ids = [ch['channel_id'] for ch in channels]
    
    messages = search_messages_by_content(args.query, channel_ids=channel_ids, days=args.days, limit=args.limit)
    
    scope = f"category {args.category}" if args.category else "all channels"
    print(f"\n🔍 Search results for '{args.query}' in {scope} (last {args.days} days):\n")
    print(f"Total: {len(messages)} messages\n")
    
    for i, msg in enumerate(messages, 1):
        print(f"--- #{i} ({msg.get('channel_name', 'Unknown')}) ---")
        print(format_message_for_display(msg))
        print(f"   🔗 {generate_jump_url(msg['channel_id'], msg['message_id'])}")
        print()


def cmd_batch_refresh(args):
    """Refresh media URLs for multiple messages."""
    message_ids = [int(x.strip()) for x in args.ids.split(',')]
    
    print(f"\n🔄 Batch refreshing {len(message_ids)} messages...")
    
    results = batch_refresh_media(message_ids, dry_run=args.dry_run)
    
    print(f"\n📊 Results:")
    print(f"   ✅ Success: {len(results['success'])}")
    print(f"   ❌ Failed: {len(results['failed'])}")
    print(f"   ⏭️  No attachments: {len(results['no_attachments'])}")
    
    if results['success']:
        print(f"\n✅ Successfully refreshed:")
        for item in results['success']:
            print(f"   {item['message_id']}: {len(item['urls'])} URL(s)")
    
    if results['failed']:
        print(f"\n❌ Failed:")
        for item in results['failed']:
            print(f"   {item['message_id']}: {item['error']}")
    
    if args.dry_run:
        print("\n🔍 Dry run - database not updated")


def main():
    parser = argparse.ArgumentParser(
        description="Weekly digest utilities for exploring Discord messages",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # channels command
    p_channels = subparsers.add_parser('channels', help='List channels in a category')
    p_channels.add_argument('id', type=int, help='Category ID')
    
    # messages command
    p_messages = subparsers.add_parser('messages', help='Get messages from a channel')
    p_messages.add_argument('id', type=int, help='Channel ID')
    p_messages.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_messages.add_argument('--limit', type=int, default=50, help='Max messages to show')
    
    # top command
    p_top = subparsers.add_parser('top', help='Get top messages by reactions')
    p_top.add_argument('id', type=int, help='Channel ID')
    p_top.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_top.add_argument('--min-reactions', type=int, default=3, help='Minimum reaction count')
    p_top.add_argument('--limit', type=int, default=20, help='Max messages to show')
    
    # category-top command
    p_cat_top = subparsers.add_parser('category-top', help='Get top messages across category')
    p_cat_top.add_argument('id', type=int, help='Category ID')
    p_cat_top.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_cat_top.add_argument('--min-reactions', type=int, default=3, help='Minimum reaction count')
    p_cat_top.add_argument('--limit', type=int, default=30, help='Max messages to show')
    
    # context command
    p_context = subparsers.add_parser('context', help='Get message context')
    p_context.add_argument('id', type=int, help='Message ID')
    p_context.add_argument('--surrounding', type=int, default=5, help='Number of surrounding messages')
    
    # refresh command
    p_refresh = subparsers.add_parser('refresh', help='Refresh media URLs')
    p_refresh.add_argument('id', type=int, help='Message ID')
    p_refresh.add_argument('--dry-run', action='store_true', help='Preview without updating DB')
    
    # user command
    p_user = subparsers.add_parser('user', help='Get messages from a user')
    p_user.add_argument('username', help='Username to search for')
    p_user.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_user.add_argument('--limit', type=int, default=30, help='Max messages to show')
    p_user.add_argument('--category', type=int, help='Filter to channels in this category')
    
    # thread command
    p_thread = subparsers.add_parser('thread', help='Follow reply chain')
    p_thread.add_argument('id', type=int, help='Message ID to start from')
    
    # media command
    p_media = subparsers.add_parser('media', help='Get messages with attachments')
    p_media.add_argument('--channel', type=int, help='Channel ID')
    p_media.add_argument('--category', type=int, help='Category ID')
    p_media.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_media.add_argument('--min-reactions', type=int, default=0, help='Minimum reaction count')
    p_media.add_argument('--limit', type=int, default=30, help='Max messages to show')
    
    # search command
    p_search = subparsers.add_parser('search', help='Search messages by content')
    p_search.add_argument('query', help='Search query')
    p_search.add_argument('--days', type=int, default=7, help='Number of days to look back')
    p_search.add_argument('--limit', type=int, default=30, help='Max messages to show')
    p_search.add_argument('--category', type=int, help='Filter to channels in this category')
    
    # batch-refresh command
    p_batch = subparsers.add_parser('batch-refresh', help='Refresh multiple messages')
    p_batch.add_argument('ids', help='Comma-separated message IDs')
    p_batch.add_argument('--dry-run', action='store_true', help='Preview without updating DB')
    
    args = parser.parse_args()
    
    if args.command == 'channels':
        cmd_channels(args)
    elif args.command == 'messages':
        cmd_messages(args)
    elif args.command == 'top':
        cmd_top(args)
    elif args.command == 'category-top':
        cmd_category_top(args)
    elif args.command == 'context':
        cmd_context(args)
    elif args.command == 'refresh':
        cmd_refresh(args)
    elif args.command == 'user':
        cmd_user(args)
    elif args.command == 'thread':
        cmd_thread(args)
    elif args.command == 'media':
        cmd_media(args)
    elif args.command == 'search':
        cmd_search(args)
    elif args.command == 'batch-refresh':
        cmd_batch_refresh(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

