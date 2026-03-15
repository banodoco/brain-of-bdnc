"""Tool definitions and executors for admin chat.

Tools call into existing bot functionality to maintain consistency.
Following the Arnold pattern - includes a 'reply' tool that the LLM uses to respond.
"""
import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger('DiscordBot')

# Add project root to path for weekly_digest imports
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Server ID for Discord links
GUILD_ID = int(os.getenv('GUILD_ID', os.getenv('DEV_GUILD_ID', '0')))

# Track messages sent by the bot this session (for delete_message last_n)
# List of (channel_id, message_id) tuples, most recent last
_sent_messages: List[tuple] = []

# Cached Supabase client
_supabase_client = None


def _get_supabase():
    """Get or create a cached Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        _supabase_client = create_client(url, key)
    return _supabase_client

# Tables the agent is allowed to query
QUERYABLE_TABLES = {
    'competitions', 'competition_entries', 'discord_reactions',
    'discord_messages', 'discord_members', 'discord_channels',
    'events', 'invite_codes', 'grant_applications',
    'daily_summaries', 'channel_summary', 'shared_posts',
    'pending_intros', 'intro_votes', 'timed_mutes',
}


# ========== Tool Definitions (Anthropic format) ==========

TOOLS = [
    {
        "name": "reply",
        "description": "Send one or more messages back to the user. Use this to respond. Can send multiple messages if needed (e.g., for long content or separate topics).",
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of messages to send to the user (sent as separate Discord messages)"
                },
                "message": {
                    "type": "string",
                    "description": "Single message to send (alternative to messages array)"
                }
            },
            "required": []
        }
    },
    {
        "name": "end_turn",
        "description": "End the current turn without sending a message. Use when you've completed actions silently or when no response is needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional: brief reason for ending without reply (for logging)"
                }
            },
            "required": []
        }
    },
    {
        "name": "find_messages",
        "description": "Search and browse Discord messages. Combine any filters. Use for ALL message finding: top posts, user posts, content search, channel browsing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text search (case-insensitive)"
                },
                "username": {
                    "type": "string",
                    "description": "Filter by user (partial match)"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Filter to channel/thread"
                },
                "min_reactions": {
                    "type": "integer",
                    "description": "Min reactions (default 0)"
                },
                "has_media": {
                    "type": "boolean",
                    "description": "Only posts with attachments"
                },
                "days": {
                    "type": "integer",
                    "description": "Days back (default 7)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20, max 50)"
                },
                "sort": {
                    "type": "string",
                    "enum": ["reactions", "unique_reactors", "date"],
                    "description": "Sort order (default: reactions). unique_reactors = number of distinct users who reacted."
                },
                "refresh_media": {
                    "type": "boolean",
                    "description": "Get fresh media URLs for results (use for showing images/videos)"
                },
                "live": {
                    "type": "boolean",
                    "description": "Use live Discord API instead of DB (requires channel_id). Good for seeing current state including bot posts."
                }
            },
            "required": []
        }
    },
    {
        "name": "inspect_message",
        "description": "Deep look at one message: full content, reactions with emoji counts, surrounding context, replies, fresh media URLs. Use to drill into a specific post.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                },
                "context_size": {
                    "type": "integer",
                    "description": "Number of surrounding messages to include (default 3)"
                }
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "share_to_social",
        "description": "Share a Discord message to social media (Twitter, Instagram, TikTok, YouTube). Uses the existing sharing pipeline. Respects user opt-out preferences. The message MUST have attachments (images/videos). After sharing, use the reply tool to confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_link": {
                    "type": "string",
                    "description": "Discord message link (e.g., https://discord.com/channels/123/456/789)"
                },
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID (alternative to message_link)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_active_channels",
        "description": "List channels that have been active recently, sorted by message count. Use this to find where the activity is.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to check (default 7)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_daily_summaries",
        "description": "Get the bot-generated daily summaries for active channels. Great for getting a high-level overview of what happened without reading every message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days of summaries to fetch (default 7)"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional: filter to a specific channel"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_member_info",
        "description": "Get information about a Discord member including their sharing preferences and social handles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID"
                },
                "username": {
                    "type": "string",
                    "description": "Discord username to search for (alternative to user_id)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_bot_status",
        "description": "Get the bot's current status including uptime and connections.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "search_logs",
        "description": "Search the bot's system logs. See errors, recent tool calls, feature traces. Use to check what happened, diagnose issues, or review your own recent actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for in log messages (e.g. 'AdminChat', 'error', 'share')"
                },
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARNING", "INFO"],
                    "description": "Filter by log level (default: all levels)"
                },
                "hours": {
                    "type": "integer",
                    "description": "Hours back to search (default 6, max 48)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 30, max 100)"
                }
            },
            "required": []
        }
    },
    {
        "name": "send_message",
        "description": "Send a message to a Discord channel as the bot. Can optionally reply to a specific message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel or thread ID to send to"
                },
                "content": {
                    "type": "string",
                    "description": "Message content to send"
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional: message ID to reply to"
                }
            },
            "required": ["channel_id", "content"]
        }
    },
    {
        "name": "edit_message",
        "description": "Edit a bot message in a Discord channel. Can only edit messages sent by the bot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "message_id": {
                    "type": "string",
                    "description": "Message ID to edit"
                },
                "content": {
                    "type": "string",
                    "description": "New message content"
                }
            },
            "required": ["channel_id", "message_id", "content"]
        }
    },
    {
        "name": "delete_message",
        "description": "Delete bot message(s). Pass a specific channel_id + message_id, OR use last_n to delete the last N messages the bot sent this session (from any channel).",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID (required with message_id)"
                },
                "message_id": {
                    "type": "string",
                    "description": "Specific message ID to delete"
                },
                "last_n": {
                    "type": "integer",
                    "description": "Delete the last N messages the bot sent this session. Use this when asked to 'delete what you just sent' or similar."
                }
            },
            "required": []
        }
    },
    {
        "name": "upload_file",
        "description": "Upload a file to a Discord channel. Use for sharing videos, images, or other files. The file must be accessible on the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "file_path": {
                    "type": "string",
                    "description": "Local file path to upload"
                },
                "content": {
                    "type": "string",
                    "description": "Optional message to send with the file"
                }
            },
            "required": ["channel_id", "file_path"]
        }
    },
    {
        "name": "resolve_user",
        "description": "Resolve a username to a Discord user ID (for mentions). Also returns their display name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username to look up"
                }
            },
            "required": ["username"]
        }
    },
    {
        "name": "query_table",
        "description": "Query any database table directly. Use for data that isn't covered by other tools (e.g. competition_entries, discord_reactions, events, grant_applications). Returns up to `limit` rows matching the filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name (e.g. competition_entries, competitions, discord_reactions, events, invite_codes, grant_applications, discord_members, discord_messages, discord_channels)"
                },
                "select": {
                    "type": "string",
                    "description": "Comma-separated columns to return (default: *)"
                },
                "filters": {
                    "type": "object",
                    "description": "Equality filters as {column: value}. Use special prefixes for other operators: 'gt.', 'gte.', 'lt.', 'lte.', 'neq.', 'like.', 'ilike.' (e.g. {\"reaction_count\": \"gte.5\", \"author_name\": \"ilike.%john%\"})"
                },
                "order": {
                    "type": "string",
                    "description": "Column to order by (prefix with '-' for descending, e.g. '-created_at')"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default: 25, max: 100)"
                }
            },
            "required": ["table"]
        }
    },
]


# ========== Helper Functions ==========

def parse_message_link(link: str) -> Optional[Dict[str, int]]:
    """Parse a Discord message link into guild_id, channel_id, message_id."""
    pattern = r'https?://(?:discord\.com|discordapp\.com)/channels/(\d+)/(\d+)/(\d+)'
    match = re.match(pattern, link)
    if match:
        return {
            'guild_id': int(match.group(1)),
            'channel_id': int(match.group(2)),
            'message_id': int(match.group(3))
        }
    return None


def format_message_for_llm(msg: Dict, include_link: bool = True) -> Dict:
    """Format a message dict for LLM consumption."""
    result = {
        "message_id": str(msg.get('message_id')),
        "author": msg.get('author_name', 'Unknown'),
        "content": (msg.get('content', '') or '')[:300],
        "reactions": msg.get('reaction_count', 0),
        "unique_reactors": msg.get('unique_reactor_count'),
        "has_media": bool(msg.get('attachments') or msg.get('attachment_urls')),
        "date": msg.get('created_at', '')[:10] if msg.get('created_at') else None,
        "channel_id": str(msg.get('channel_id', '')),
    }
    if msg.get('channel_name'):
        result["channel"] = msg.get('channel_name')
    if msg.get('media_urls'):
        result["media_urls"] = msg['media_urls']
    if include_link:
        result["link"] = f"https://discord.com/channels/{GUILD_ID}/{msg.get('channel_id')}/{msg.get('message_id')}"
    return result


def _build_summary(formatted: List[Dict], header: str, media_urls_map: Dict[str, str] = None) -> str:
    """Build a pre-formatted summary string from formatted messages.

    Uses ---SPLIT--- markers so the cog sends each entry as a separate message
    for proper media embedding.
    """
    media_urls_map = media_urls_map or {}
    SPLIT_MARKER = "\n---SPLIT---\n"

    parts = [header]

    for i, msg in enumerate(formatted, 1):
        content_preview = msg.get('content', '')[:100]
        if len(msg.get('content', '')) > 100:
            content_preview += "..."

        media_url = media_urls_map.get(msg['message_id'])
        channel_tag = f" in #{msg['channel']}" if msg.get('channel') else ""

        ur = msg.get('unique_reactors')
        react_str = f"{ur} unique reactors" if ur is not None else f"{msg['reactions']} reactions"
        entry = f"**{i}. {msg['author']}** — {react_str}{channel_tag}"
        if content_preview:
            entry += f"\n> {content_preview}"
        entry += f"\n`{msg['message_id']}`"

        if media_url:
            entry += f"\n{media_url}"

        parts.append(entry)

    return SPLIT_MARKER.join(parts)


# ========== Tool Executors ==========

def execute_reply(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the reply tool - returns message(s) to send."""
    # Support both single message and array of messages
    messages = params.get('messages', [])
    single_message = params.get('message', '')

    # Handle case where messages is passed as a string instead of array
    if isinstance(messages, str):
        messages = [messages]

    if single_message and not messages:
        messages = [single_message]

    if not messages:
        return {"success": False, "error": "No message provided"}

    # Filter out empty messages
    messages = [m for m in messages if m and m.strip()]

    if not messages:
        return {"success": False, "error": "All messages were empty"}

    return {
        "success": True,
        "messages": messages  # Array of messages to send
    }


def execute_end_turn(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the end_turn tool - ends without sending a message."""
    reason = params.get('reason', 'No reason provided')
    logger.info(f"[AdminChat] End turn: {reason}")
    return {
        "success": True,
        "end_turn": True,
        "reason": reason
    }


async def execute_find_messages(params: Dict[str, Any], bot: discord.Client = None) -> Dict[str, Any]:
    """Unified message search. Builds one query with all filters, or uses live Discord API."""
    from scripts.weekly_digest import get_client, get_user_by_name, _enrich_messages
    from src.common.discord_utils import refresh_media_url

    query = params.get('query', '')
    username = params.get('username', '')
    channel_id = params.get('channel_id', '')
    min_reactions = params.get('min_reactions', 0)
    has_media = params.get('has_media', False)
    days = params.get('days', 7)
    limit = min(params.get('limit', 20), 50)
    sort = params.get('sort', 'reactions')
    do_refresh_media = params.get('refresh_media', False)
    live = params.get('live', False)

    # Resolve username to author_id upfront (used by both paths)
    author_id = None
    resolved_username = None
    if username:
        user_data = get_user_by_name(username)
        if not user_data:
            return {"success": False, "error": f"User '{username}' not found"}
        author_id = user_data['member_id']
        resolved_username = user_data.get('username', username)

    try:
        # ---- Live path: use Discord API directly ----
        if live:
            if not channel_id:
                return {"success": False, "error": "channel_id is required when live=true"}
            if not bot:
                return {"success": False, "error": "Bot not available for live queries"}

            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))

            messages = []
            # When sorting by reactions/reactors, collect more candidates since best ones may be older
            need_all = sort in ('reactions', 'unique_reactors')
            fetch_limit = limit * 3 if need_all else limit * 2
            async for msg in channel.history(limit=min(fetch_limit, 500)):
                if author_id and msg.author.id != author_id:
                    continue
                if query and query.lower() not in (msg.content or '').lower():
                    continue
                total_reactions = sum(r.count for r in msg.reactions) if msg.reactions else 0
                if min_reactions and total_reactions < min_reactions:
                    continue
                if has_media and not msg.attachments:
                    continue

                # Build a dict that matches the DB shape so format_message_for_llm works
                messages.append({
                    "message_id": msg.id,
                    "channel_id": int(channel_id),
                    "author_name": msg.author.display_name,
                    "content": (msg.content or '')[:400],
                    "reaction_count": total_reactions,
                    "attachments": [a.url for a in msg.attachments] if msg.attachments else [],
                    "media_urls": [a.url for a in msg.attachments] if msg.attachments else None,
                    "created_at": msg.created_at.isoformat(),
                })
                # For date sort, stop early once we have enough
                if not need_all and len(messages) >= limit:
                    break

            # Sort and trim
            if sort == 'unique_reactors':
                messages.sort(key=lambda m: m.get('unique_reactor_count', 0), reverse=True)
            elif sort == 'reactions':
                messages.sort(key=lambda m: m['reaction_count'], reverse=True)
            messages = messages[:limit]

        # ---- DB path: build single Supabase query ----
        else:
            client = get_client()
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            # Get channel names for enrichment + NSFW filtering
            channels_result = client.table('discord_channels').select('channel_id, channel_name, nsfw').execute()
            safe_channels = {ch['channel_id']: ch['channel_name'] for ch in channels_result.data if not ch.get('nsfw')}

            # Build query — chain all applicable filters
            MSG_SELECT = 'message_id, channel_id, author_id, content, created_at, attachments, reaction_count, reactors, reference_id'
            q = client.table('discord_messages').select(MSG_SELECT).gte('created_at', cutoff)

            # Only search in safe (non-NSFW) channels
            if channel_id:
                q = q.eq('channel_id', int(channel_id))
            else:
                q = q.in_('channel_id', list(safe_channels.keys()))

            if author_id:
                q = q.eq('author_id', author_id)
            if query:
                q = q.ilike('content', f'%{query}%')
            if min_reactions:
                q = q.gte('reaction_count', min_reactions)
            if has_media:
                q = q.neq('attachments', [])

            # Sort and limit at the DB level when possible
            if sort == 'unique_reactors':
                # Can't sort by array length in Supabase REST — over-fetch and sort client-side
                q = q.order('reaction_count', desc=True).limit(limit * 3)
            elif sort == 'date':
                q = q.order('created_at', desc=True).limit(limit)
            else:
                q = q.order('reaction_count', desc=True).limit(limit)

            messages = q.execute().data
            _enrich_messages(messages, channel_names=safe_channels, parse_reactors=True)

            # Client-side sort for unique_reactors
            if sort == 'unique_reactors':
                messages.sort(key=lambda m: m.get('unique_reactor_count', 0), reverse=True)
                messages = messages[:limit]

        # ---- Common output for both paths ----
        if not messages:
            desc_parts = []
            if query:
                desc_parts.append(f"matching '{query}'")
            if resolved_username:
                desc_parts.append(f"from {resolved_username}")
            if min_reactions:
                desc_parts.append(f"with {min_reactions}+ reactions")
            desc = " ".join(desc_parts) or "matching your filters"
            return {
                "success": True,
                "count": 0,
                "summary": f"No messages found {desc} in the last {days} days.",
                "messages": []
            }

        # Refresh media URLs for top results if requested
        media_urls_map = {}
        if do_refresh_media and bot:
            for msg in messages[:min(limit, 20)]:
                try:
                    ch_id = msg.get('channel_id')
                    m_id = msg.get('message_id')
                    result = await refresh_media_url(bot, ch_id, m_id, logger)
                    if result and result.get('success'):
                        urls = [att['url'] for att in result.get('attachments', []) if att.get('url')]
                        if urls:
                            media_urls_map[str(m_id)] = urls[0]
                            msg['media_urls'] = urls
                except Exception as e:
                    logger.debug(f"[AdminChat] Could not refresh media for {msg.get('message_id')}: {e}")

        formatted = [format_message_for_llm(msg) for msg in messages]

        # Build header
        header_parts = ["**Found ", str(len(formatted)), " messages"]
        if query:
            header_parts.append(f" matching '{query}'")
        if resolved_username:
            header_parts.append(f" from {resolved_username}")
        if live:
            header_parts.append(f" in <#{channel_id}>")
        header_parts.append(":**")

        summary = _build_summary(formatted, "".join(header_parts), media_urls_map)

        return {
            "success": True,
            "count": len(formatted),
            "summary": summary,
            "messages": formatted
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in find_messages: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_inspect_message(params: Dict[str, Any], bot: discord.Client = None) -> Dict[str, Any]:
    """Deep look at one message: content, reactions, context, replies, fresh media."""
    from scripts.weekly_digest import get_message_context, get_message_by_id
    from src.common.discord_utils import refresh_media_url

    message_id = params.get('message_id', '')
    context_size = params.get('context_size', 3)

    if not message_id:
        return {"success": False, "error": "message_id is required"}

    try:
        # Get message + context from DB
        context = get_message_context(int(message_id), surrounding=context_size)

        if context.get('error'):
            return {"success": False, "error": context['error']}

        target = context.get('target_message', {})
        replies = context.get('replies', [])
        before = context.get('before', [])
        after = context.get('after', [])

        # Try to get live data from Discord API (fresh URLs + reaction detail)
        live_reactions = []
        media_urls = []
        channel_id = target.get('channel_id')

        if bot and channel_id:
            try:
                channel = bot.get_channel(channel_id)
                if not channel:
                    channel = await bot.fetch_channel(channel_id)

                # Handle ForumChannel
                if isinstance(channel, discord.ForumChannel):
                    try:
                        thread = await bot.fetch_channel(int(message_id))
                        if isinstance(thread, discord.Thread):
                            channel = thread
                    except Exception:
                        pass

                if hasattr(channel, 'fetch_message'):
                    live_msg = await channel.fetch_message(int(message_id))

                    # Fresh reaction detail
                    for r in live_msg.reactions:
                        live_reactions.append({
                            "emoji": str(r.emoji),
                            "count": r.count
                        })

                    # Fresh attachment URLs
                    for att in live_msg.attachments:
                        media_urls.append({
                            "filename": att.filename,
                            "url": att.url,
                            "content_type": att.content_type,
                        })
            except Exception as e:
                logger.debug(f"[AdminChat] Could not fetch live message {message_id}: {e}")

        # Format target
        formatted_target = format_message_for_llm(target, include_link=True)
        # Override with full content (not truncated)
        formatted_target["content"] = (target.get('content', '') or '')

        # Format replies
        formatted_replies = [
            {
                "author": r.get('author_name', 'Unknown'),
                "content": (r.get('content', '') or '')[:200],
                "message_id": str(r.get('message_id', '')),
                "reactions": r.get('reaction_count', 0),
            }
            for r in replies[:10]
        ]

        # Format surrounding context
        formatted_before = [
            {"author": m.get('author_name', 'Unknown'), "content": (m.get('content', '') or '')[:150]}
            for m in before
        ]
        formatted_after = [
            {"author": m.get('author_name', 'Unknown'), "content": (m.get('content', '') or '')[:150]}
            for m in after
        ]

        # Build summary using ---SPLIT--- so the cog sends media URLs as separate messages
        SPLIT = "\n---SPLIT---\n"
        total_reactions = sum(r['count'] for r in live_reactions) if live_reactions else target.get('reaction_count', 0)

        # First part: message info
        info_lines = [f"**Message by {formatted_target['author']}** — {total_reactions} reactions"]
        if formatted_target['content']:
            info_lines.append(f"> {formatted_target['content'][:500]}")
        else:
            info_lines.append("*(no text content)*")
        info_lines.append(formatted_target.get('link', ''))
        if live_reactions:
            reaction_str = "  ".join(f"{r['emoji']} {r['count']}" for r in live_reactions)
            info_lines.append(reaction_str)
        if formatted_replies:
            info_lines.append(f"\n**Replies** ({len(formatted_replies)})")
            for r in formatted_replies[:5]:
                reply_preview = r['content'][:100] + ("..." if len(r['content']) > 100 else "")
                info_lines.append(f"> **{r['author']}:** {reply_preview}")

        parts = ["\n".join(info_lines)]

        # Each media URL as its own split part so it embeds properly
        for m in media_urls:
            parts.append(m['url'])

        return {
            "success": True,
            "message": formatted_target,
            "reactions": live_reactions,
            "media": media_urls,
            "replies": formatted_replies,
            "reply_count": len(replies),
            "context_before": formatted_before,
            "context_after": formatted_after,
            "summary": SPLIT.join(parts),
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in inspect_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_share_to_social(
    bot: discord.Client,
    sharer,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute the share_to_social tool using existing sharer.finalize_sharing()."""

    message_link = params.get('message_link', '')
    message_id = params.get('message_id', '')

    # Parse link or use direct ID
    if message_link:
        parsed = parse_message_link(message_link)
        if not parsed:
            return {"success": False, "error": "Invalid message link format"}
        channel_id = parsed['channel_id']
        message_id = parsed['message_id']
    elif message_id:
        # Need to find the channel - search in DB
        from scripts.weekly_digest import get_message_by_id
        msg_data = get_message_by_id(int(message_id))
        if not msg_data:
            return {"success": False, "error": f"Message {message_id} not found in database"}
        channel_id = msg_data['channel_id']
        message_id = int(message_id)
    else:
        return {"success": False, "error": "Provide either message_link or message_id"}

    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            return {"success": False, "error": f"Could not find channel {channel_id}"}

        message = await channel.fetch_message(message_id)
        if not message:
            return {"success": False, "error": f"Could not find message {message_id}"}

        if not message.attachments:
            return {"success": False, "error": f"Message has no attachments to share. Content: '{message.content[:100]}...'"}

        logger.info(f"[AdminChat] Triggering share for message {message_id} by user {message.author.id}")

        # Use existing sharing path
        await sharer.finalize_sharing(
            user_id=message.author.id,
            message_id=message.id,
            channel_id=channel.id,
            summary_channel=None
        )

        return {
            "success": True,
            "message": f"Initiated sharing for message {message_id} by {message.author.display_name}. Will post to Twitter/Instagram/TikTok/YouTube."
        }

    except discord.NotFound:
        return {"success": False, "error": f"Message {message_id} not found"}
    except discord.Forbidden:
        return {"success": False, "error": "Bot doesn't have permission to access that channel/message"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in share_to_social: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_active_channels(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get list of active channels."""
    from scripts.weekly_digest import get_active_channels

    days = params.get('days', 7)

    try:
        channels = get_active_channels(days=days)

        # Format for LLM (top 20)
        formatted = [
            {
                "channel_id": str(ch['channel_id']),
                "name": ch['channel_name'],
                "messages": ch['message_count']
            }
            for ch in channels[:20]
        ]

        return {
            "success": True,
            "count": len(formatted),
            "channels": formatted
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_active_channels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_daily_summaries(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get daily summaries."""
    from collections import defaultdict
    from supabase import create_client as sc

    days = params.get('days', 7)
    channel_id = params.get('channel_id')

    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        client = sc(url, key)
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')

        q = client.table('daily_summaries').select('date, channel_id, short_summary') \
            .gte('date', cutoff).order('date', desc=True)
        if channel_id:
            q = q.eq('channel_id', str(channel_id))
        rows = q.execute().data

        by_date = defaultdict(list)
        for r in rows:
            by_date[r['date']].append(r)

        summary_lines = []
        for date in sorted(by_date.keys(), reverse=True):
            items = by_date[date]
            summary_lines.append(f"\n**{date}** ({len(items)} channels)")
            for item in items:
                s = (item.get('short_summary') or '')[:300]
                summary_lines.append(f"  [{item['channel_id']}] {s}")

        return {
            "success": True,
            "days": days,
            "summary": "\n".join(summary_lines),
            "total_summaries": len(rows)
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_daily_summaries: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_member_info(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Get member information from the database."""

    user_id = params.get('user_id')
    username = params.get('username')

    if not user_id and not username:
        return {"success": False, "error": "Provide either user_id or username"}

    try:
        if user_id:
            member = db_handler.get_member(int(user_id))
        else:
            result = db_handler._run_async_in_thread(
                db_handler.storage_handler.supabase_client.table('discord_members')
                .select('*')
                .ilike('username', f'%{username}%')
                .limit(5)
                .execute
            )
            if result.data:
                if len(result.data) > 1:
                    usernames = [m.get('username', 'unknown') for m in result.data]
                    return {"success": False, "error": f"Multiple matches: {', '.join(usernames)}. Use user_id for exact match."}
                member = result.data[0]
            else:
                member = None

        if not member:
            return {"success": False, "error": f"No member found"}

        return {
            "success": True,
            "member": {
                "id": member.get('member_id'),
                "username": member.get('username'),
                "display_name": member.get('global_name') or member.get('server_nick'),
                "include_in_updates": member.get('include_in_updates'),
                "allow_content_sharing": member.get('allow_content_sharing'),
                "first_shared_at": member.get('first_shared_at'),
                "twitter_handle": member.get('twitter_handle'),
                "reddit_handle": member.get('reddit_handle'),
            }
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_member_info: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_bot_status(bot: discord.Client) -> Dict[str, Any]:
    """Get bot status information."""
    import time

    try:
        uptime_seconds = None
        if hasattr(bot, 'start_time'):
            uptime_seconds = int(time.time() - bot.start_time)

        return {
            "success": True,
            "status": {
                "online": bot.is_ready(),
                "latency_ms": round(bot.latency * 1000, 2),
                "uptime_seconds": uptime_seconds,
                "dev_mode": getattr(bot, 'dev_mode', False),
                "guilds": len(bot.guilds),
            }
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_bot_status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_search_logs(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search bot system logs from Supabase."""
    from supabase import create_client as sc

    query = params.get('query', '')
    level = params.get('level', '')
    hours = min(params.get('hours', 6), 48)
    limit = min(params.get('limit', 30), 100)

    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        client = sc(url, key)

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        q = client.table('system_logs').select(
            'timestamp, level, logger_name, message'
        ).gte('timestamp', cutoff).order('timestamp', desc=True)

        if level:
            q = q.eq('level', level)
        if query:
            q = q.ilike('message', f'%{query}%')
        q = q.limit(limit)

        rows = q.execute().data

        if not rows:
            return {
                "success": True,
                "count": 0,
                "summary": f"No logs found{' matching ' + repr(query) if query else ''} in the last {hours}h."
            }

        # Format oldest-first for readability
        rows.reverse()
        lines = []
        for r in rows:
            ts = r['timestamp'][:19].replace('T', ' ')
            lvl = r['level'][:4]
            msg = r['message'][:200]
            lines.append(f"`{ts}` **{lvl}** {msg}")

        return {
            "success": True,
            "count": len(rows),
            "summary": "\n".join(lines)
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in search_logs: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def _refresh_cdn_urls(bot: discord.Client, content: str) -> str:
    """Replace expired Discord CDN URLs in content with fresh ones.

    Finds Discord CDN attachment URLs, extracts channel_id/message_id,
    fetches fresh URLs from the Discord API, and substitutes them.
    """
    from src.common.discord_utils import refresh_media_url

    cdn_pattern = re.compile(
        r'https://cdn\.discordapp\.com/attachments/(\d+)/(\d+)/([^\s\)]+)'
    )
    matches = list(cdn_pattern.finditer(content))
    if not matches:
        return content

    # Deduplicate by (channel_id, attachment_id) to avoid redundant API calls
    seen = {}
    for match in matches:
        ch_id, att_id = int(match.group(1)), int(match.group(2))
        if (ch_id, att_id) not in seen:
            seen[(ch_id, att_id)] = match

    # For each unique attachment, find the source message and refresh
    # The channel_id in a CDN URL is the channel where the file was uploaded
    # We need to find the message that contains this attachment
    for (ch_id, att_id), match in seen.items():
        try:
            result = await refresh_media_url(bot, ch_id, att_id, logger)
            if result and result.get('attachments'):
                old_filename = match.group(3).split('?')[0]  # Strip query params
                for att in result['attachments']:
                    if att.get('filename') == old_filename or old_filename in att.get('url', ''):
                        content = content.replace(match.group(0), att['url'])
                        break
        except Exception as e:
            logger.debug(f"[AdminChat] Could not refresh CDN URL: {e}")

    return content


async def execute_send_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Send a message to a channel, optionally as a reply. Auto-refreshes Discord CDN URLs."""
    channel_id = params.get('channel_id', '')
    content = params.get('content', '')
    reply_to = params.get('reply_to')

    if not channel_id or not content:
        return {"success": False, "error": "channel_id and content are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))

        # Auto-refresh any Discord CDN URLs before sending
        content = await _refresh_cdn_urls(bot, content)

        kwargs = {}
        if reply_to:
            try:
                ref_msg = await channel.fetch_message(int(reply_to))
                kwargs['reference'] = ref_msg
            except Exception:
                pass  # Send without reply if message not found

        msg = await channel.send(content, **kwargs)

        # Track for later deletion
        _sent_messages.append((int(channel_id), msg.id))

        return {
            "success": True,
            "message_id": str(msg.id),
            "jump_url": msg.jump_url
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in send_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_edit_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Edit a bot message."""
    channel_id = params.get('channel_id', '')
    message_id = params.get('message_id', '')
    content = params.get('content', '')

    if not all([channel_id, message_id, content]):
        return {"success": False, "error": "channel_id, message_id, and content are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        if msg.author.id != bot.user.id:
            return {"success": False, "error": "Can only edit bot's own messages"}
        await msg.edit(content=content)
        return {"success": True, "message_id": str(msg.id)}
    except discord.NotFound:
        return {"success": False, "error": "Message not found"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in edit_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_delete_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete bot message(s). Supports specific ID or last_n from session."""
    channel_id = params.get('channel_id', '')
    message_id = params.get('message_id', '')
    last_n = params.get('last_n')

    # Mode 1: delete last N sent messages
    if last_n:
        if not _sent_messages:
            return {"success": False, "error": "No messages sent this session to delete"}

        to_delete = _sent_messages[-last_n:]
        deleted = 0
        errors = []
        for ch_id, msg_id in reversed(to_delete):
            try:
                channel = bot.get_channel(ch_id)
                if not channel:
                    channel = await bot.fetch_channel(ch_id)
                msg = await channel.fetch_message(msg_id)
                if msg.author.id == bot.user.id:
                    await msg.delete()
                    deleted += 1
                    _sent_messages.remove((ch_id, msg_id))
            except discord.NotFound:
                _sent_messages.remove((ch_id, msg_id))
                deleted += 1  # Already gone
            except Exception as e:
                errors.append(f"{msg_id}: {e}")

        result = {"success": True, "deleted": deleted}
        if errors:
            result["errors"] = errors
        return result

    # Mode 2: delete specific message
    if not channel_id or not message_id:
        return {"success": False, "error": "Provide channel_id + message_id, or last_n"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        if msg.author.id != bot.user.id:
            return {"success": False, "error": "Can only delete bot's own messages"}
        await msg.delete()
        # Remove from tracking if present
        pair = (int(channel_id), int(message_id))
        if pair in _sent_messages:
            _sent_messages.remove(pair)
        return {"success": True}
    except discord.NotFound:
        return {"success": False, "error": "Message not found"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in delete_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_upload_file(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Upload a file to a channel."""
    channel_id = params.get('channel_id', '')
    file_path = params.get('file_path', '')
    content = params.get('content', '')

    if not channel_id or not file_path:
        return {"success": False, "error": "channel_id and file_path are required"}

    if not os.path.exists(file_path):
        return {"success": False, "error": f"File not found: {file_path}"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        file = discord.File(file_path)
        msg = await channel.send(content=content or None, file=file)
        urls = [a.url for a in msg.attachments]
        return {
            "success": True,
            "message_id": str(msg.id),
            "attachment_urls": urls
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in upload_file: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_resolve_user(params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a username to a Discord user ID."""
    from scripts.weekly_digest import get_user_by_name

    username = params.get('username', '')
    if not username:
        return {"success": False, "error": "username is required"}

    try:
        user_data = get_user_by_name(username)
        if not user_data:
            return {"success": False, "error": f"User '{username}' not found"}
        return {
            "success": True,
            "user_id": str(user_data['member_id']),
            "username": user_data.get('username'),
            "display_name": user_data.get('global_name') or user_data.get('server_nick'),
            "mention": f"<@{user_data['member_id']}>"
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in resolve_user: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_query_table(params: Dict[str, Any]) -> Dict[str, Any]:
    """Query any allowed database table with filters."""
    table = params.get('table', '')
    select_cols = params.get('select', '*')
    filters = params.get('filters', {})
    order = params.get('order', '')
    limit = min(params.get('limit', 25), 100)

    if not table:
        return {"success": False, "error": "table is required"}
    if table not in QUERYABLE_TABLES:
        return {"success": False, "error": f"Table '{table}' not allowed. Available: {', '.join(sorted(QUERYABLE_TABLES))}"}

    try:
        sb = _get_supabase()
        query = sb.table(table).select(select_cols)

        # Apply filters with operator support
        for col, val in filters.items():
            val_str = str(val)
            if val_str.startswith('gt.'):
                query = query.gt(col, val_str[3:])
            elif val_str.startswith('gte.'):
                query = query.gte(col, val_str[4:])
            elif val_str.startswith('lt.'):
                query = query.lt(col, val_str[3:])
            elif val_str.startswith('lte.'):
                query = query.lte(col, val_str[4:])
            elif val_str.startswith('neq.'):
                query = query.neq(col, val_str[4:])
            elif val_str.startswith('like.'):
                query = query.like(col, val_str[5:])
            elif val_str.startswith('ilike.'):
                query = query.ilike(col, val_str[6:])
            elif val_str.startswith('in.'):
                # Comma-separated list: "in.a,b,c"
                values = val_str[3:].split(',')
                query = query.in_(col, values)
            elif val_str == 'is.null':
                query = query.is_(col, 'null')
            elif val_str == 'not.null':
                query = query.not_.is_(col, 'null')
            else:
                query = query.eq(col, val)

        # Apply ordering
        if order:
            desc = order.startswith('-')
            col_name = order.lstrip('-')
            query = query.order(col_name, desc=desc)

        query = query.limit(limit)
        result = query.execute()

        return {
            "success": True,
            "count": len(result.data),
            "data": result.data,
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in query_table: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ========== Tool Executor Dispatcher ==========

async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    bot: discord.Client,
    db_handler,
    sharer
) -> Dict[str, Any]:
    """Execute a tool by name and return the result as a dict."""

    # Log params for search tools (skip reply/end_turn which are noisy)
    if tool_name not in ("reply", "end_turn"):
        logger.info(f"[AdminChat] Executing tool: {tool_name} {tool_input}")
    else:
        logger.info(f"[AdminChat] Executing tool: {tool_name}")

    if tool_name == "reply":
        return execute_reply(tool_input)
    elif tool_name == "end_turn":
        return execute_end_turn(tool_input)
    elif tool_name == "find_messages":
        return await execute_find_messages(tool_input, bot)
    elif tool_name == "inspect_message":
        return await execute_inspect_message(tool_input, bot)
    elif tool_name == "share_to_social":
        return await execute_share_to_social(bot, sharer, tool_input)
    elif tool_name == "get_active_channels":
        return await execute_get_active_channels(tool_input)
    elif tool_name == "get_daily_summaries":
        return await execute_get_daily_summaries(tool_input)
    elif tool_name == "get_member_info":
        return await execute_get_member_info(db_handler, tool_input)
    elif tool_name == "get_bot_status":
        return await execute_get_bot_status(bot)
    elif tool_name == "search_logs":
        return await execute_search_logs(tool_input)
    elif tool_name == "send_message":
        return await execute_send_message(bot, tool_input)
    elif tool_name == "edit_message":
        return await execute_edit_message(bot, tool_input)
    elif tool_name == "delete_message":
        return await execute_delete_message(bot, tool_input)
    elif tool_name == "upload_file":
        return await execute_upload_file(bot, tool_input)
    elif tool_name == "resolve_user":
        return await execute_resolve_user(tool_input)
    elif tool_name == "query_table":
        return await execute_query_table(tool_input)
    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
