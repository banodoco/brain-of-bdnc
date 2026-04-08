"""Tool definitions and executors for admin chat.

Tools call into existing bot functionality to maintain consistency.
Following the Arnold pattern - includes a 'reply' tool that the LLM uses to respond.
"""
import os
import re
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger('DiscordBot')

# Add project root to path for weekly_digest imports
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

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


_server_config = None


def _get_server_config():
    global _server_config
    if _server_config is None:
        from src.common.server_config import ServerConfig
        _server_config = ServerConfig(_get_supabase())
    return _server_config


def _resolve_guild_id(params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Prefer explicit tool input, then fall back to the configured default guild."""
    explicit = None
    if params:
        raw = params.get('guild_id')
        if raw not in (None, '', 0, '0'):
            try:
                explicit = int(raw)
            except (TypeError, ValueError):
                pass
    return _get_server_config().resolve_guild_id(explicit, require_write=True)

# Tables the agent is allowed to query
QUERYABLE_TABLES = {
    'competitions', 'competition_entries', 'discord_reactions',
    'discord_messages', 'members', 'discord_channels',
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
                    "description": "Filter to last N days. Omit to search all time."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20, max 100)"
                },
                "sort": {
                    "type": "string",
                    "enum": ["reactions", "unique_reactors", "date"],
                    "description": "Sort order (default: date, most recent first). reactions = most reacted. unique_reactors = most distinct reactors."
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
        "description": "Share a Discord message to social media (Twitter). Uses the existing sharing pipeline. Respects user opt-out preferences. The post can be text-only or include attachments if the source message has them. Use tweet_text to specify exact tweet copy — if omitted, a generic caption is auto-generated. Set reply_to_tweet to post as a thread reply. The response always includes tweet_url. If you re-run without reply_to_tweet on a previously shared message, the existing tweet_url is returned.",
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
                },
                "tweet_text": {
                    "type": "string",
                    "description": "Custom tweet text (max 280 chars). If provided, this exact text is used as the tweet instead of auto-generating."
                },
                "reply_to_tweet": {
                    "type": "string",
                    "description": "Optional Tweet ID or full tweet URL to reply to. When set, the post is added as a thread reply. If you re-run on a previously shared message without this field, the tool returns the existing tweet URL instead of posting again."
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
        "description": "Delete one or more messages by ID. Use find_messages(live=true) first to see messages and their IDs, then delete the ones that need removing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "message_id": {
                    "type": "string",
                    "description": "Single message ID to delete"
                },
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of message IDs to delete (for bulk deletion)"
                }
            },
            "required": ["channel_id"]
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
        "description": "Query any database table directly. Use for data that isn't covered by other tools (e.g. competition_entries, competitions, discord_reactions, events, grant_applications). Returns up to `limit` rows matching the filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name (e.g. competition_entries, competitions, discord_reactions, events, invite_codes, grant_applications, members, discord_messages, discord_channels)"
                },
                "select": {
                    "type": "string",
                    "description": "Comma-separated columns to return (default: *)"
                },
                "filters": {
                    "type": "object",
                    "description": "Equality filters as {column: value}. Use special prefixes for other operators: 'gt.', 'gte.', 'lt.', 'lte.', 'neq.', 'like.', 'ilike.' (e.g. {\"reaction_count\": \"gte.5\", \"author_id\": \"123456789\"})"
                },
                "order": {
                    "type": "string",
                    "description": "Column to order by (prefix with '-' for descending). Use a real column for the table you queried (e.g. '-reaction_count' for discord_messages)."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default: 25, max: 100)"
                }
            },
            "required": ["table"]
        }
    },
    {
        "name": "download_media",
        "description": "Download attachments from a Discord message to the local filesystem for processing. Files are saved to /tmp/media/{message_id}/. Use before run_media_command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Channel/thread ID containing the message"
                }
            },
            "required": ["message_id", "channel_id"]
        }
    },
    {
        "name": "run_media_command",
        "description": "Run a media processing command (ffmpeg, ffprobe, or python3 for PIL/Pillow). Working directory is /tmp/media/. 5 minute timeout. Use for combining images, transcoding video, generating thumbnails, image compositing with PIL, etc. For PIL: python3 -c \"from PIL import Image; ...\"",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run. Must start with ffmpeg, ffprobe, or python3."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_media_files",
        "description": "List files in the media working directory (/tmp/media/). Use to see downloaded files and processing results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Subdirectory to list (relative to /tmp/media/). Defaults to listing all."
                }
            },
            "required": []
        }
    },
]

MEMBER_TOOLS = {
    "reply",
    "end_turn",
    "find_messages",
    "inspect_message",
    "get_active_channels",
    "get_daily_summaries",
    "get_member_info",
    "get_bot_status",
    "resolve_user",
}

ADMIN_ONLY_TOOLS = {
    "share_to_social",
    "search_logs",
    "send_message",
    "edit_message",
    "delete_message",
    "upload_file",
    "query_table",
    "download_media",
    "run_media_command",
    "list_media_files",
}

ALL_TOOL_NAMES = {tool["name"] for tool in TOOLS}
assert MEMBER_TOOLS | ADMIN_ONLY_TOOLS == ALL_TOOL_NAMES, "Every admin chat tool must be classified"
assert MEMBER_TOOLS & ADMIN_ONLY_TOOLS == set(), "Tool role sets must be disjoint"


def get_tools_for_role(is_admin: bool) -> List[Dict[str, Any]]:
    """Return the tool schemas available for the current role."""
    allowed = ALL_TOOL_NAMES if is_admin else MEMBER_TOOLS
    return [tool for tool in TOOLS if tool["name"] in allowed]


# ========== Helper Functions ==========

_VISIBLE_CHANNEL_CACHE_TTL_SECONDS = 60
_visible_channel_cache: Dict[Tuple[int, int], Tuple[float, Set[int]]] = {}

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
        link_guild_id = msg.get('guild_id') or _get_server_config().resolve_guild_id(require_write=True)
        result["link"] = f"https://discord.com/channels/{link_guild_id}/{msg.get('channel_id')}/{msg.get('message_id')}"
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


async def _get_visible_channel_ids(bot: discord.Client, guild_id: int, user_id: int) -> Set[int]:
    """Return channel and active thread IDs the requester can view.

    Cached for 60s to avoid repeated Discord API lookups. Permission changes can
    take up to one cache window to propagate here.
    """
    cache_key = (guild_id, user_id)
    now = time.monotonic()
    cached = _visible_channel_cache.get(cache_key)
    if cached and now - cached[0] < _VISIBLE_CHANNEL_CACHE_TTL_SECONDS:
        return set(cached[1])

    if not bot:
        return set()

    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except Exception:
            return set()

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return set()

    visible_channel_ids: Set[int] = set()
    for channel in list(guild.channels) + list(guild.threads):
        try:
            if channel.permissions_for(member).view_channel:
                visible_channel_ids.add(channel.id)
        except Exception:
            continue

    _visible_channel_cache[cache_key] = (now, visible_channel_ids)
    return set(visible_channel_ids)


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


async def execute_find_messages(
    params: Dict[str, Any],
    bot: discord.Client = None,
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Unified message search. Delegates DB queries to discord_tools.find_messages,
    keeping the live Discord API path and LLM formatting here."""
    from scripts.discord_tools import (
        find_messages as dt_find, resolve_user as dt_resolve_user,
        _set_active_guild_id, _channel_map,
    )
    from src.common.discord_utils import refresh_media_url

    query = params.get('query', '')
    username = params.get('username', '')
    channel_id = params.get('channel_id', '')
    min_reactions = params.get('min_reactions', 0)
    has_media = params.get('has_media', False)
    days = params.get('days')  # None = all time
    limit = min(params.get('limit', 20), 100)
    sort = params.get('sort', 'date')
    do_refresh_media = params.get('refresh_media', False)
    live = params.get('live', False)

    # Resolve username to author_id upfront (used by both paths)
    author_id = None
    resolved_username = None
    if username:
        user_data = dt_resolve_user(username)
        if not user_data:
            return {"success": False, "error": f"User '{username}' not found"}
        author_id = user_data['member_id']
        resolved_username = user_data.get('username', username)

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)

        # ---- Live path: use Discord API directly ----
        if live:
            if not channel_id:
                return {"success": False, "error": "channel_id is required when live=true"}
            if not bot:
                return {"success": False, "error": "Bot not available for live queries"}

            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            if visible_channels is not None and channel.id not in visible_channels:
                return {"success": False, "error": "Permission denied"}

            messages = []
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
                if not need_all and len(messages) >= limit:
                    break

            if sort == 'unique_reactors':
                messages.sort(key=lambda m: m.get('unique_reactor_count', 0), reverse=True)
            elif sort == 'reactions':
                messages.sort(key=lambda m: m['reaction_count'], reverse=True)
            messages = messages[:limit]

        # ---- DB path: delegate to discord_tools ----
        else:
            _set_active_guild_id(resolved_guild_id)

            # Build allowed channel list (non-NSFW + visible)
            allowed_channel_ids = None
            if channel_id:
                requested_channel_id = int(channel_id)
                if visible_channels is not None and requested_channel_id not in visible_channels:
                    return {"success": False, "error": "Permission denied"}
                # channel_id is passed directly, no need for allowed list
            else:
                cmap = _channel_map(exclude_nsfw=True)
                safe_ids = set(cmap.keys())
                if visible_channels is not None:
                    safe_ids = safe_ids & visible_channels
                if not safe_ids:
                    return {
                        "success": True, "count": 0,
                        "summary": f"No messages found matching your filters.",
                        "messages": []
                    }
                allowed_channel_ids = list(safe_ids)

            messages = dt_find(
                query=query, days=days,
                channel_id=int(channel_id) if channel_id else None,
                author_id=author_id,
                min_reactions=min_reactions, has_media=has_media,
                limit=limit, sort=sort,
                exclude_nsfw=False,  # handled above via allowed_channel_ids
                allowed_channel_ids=allowed_channel_ids,
            )

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
            time_desc = f"in the last {days} days" if days else "across all time"
            return {
                "success": True,
                "count": 0,
                "summary": f"No messages found {desc} {time_desc}.",
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
        hit_cap = len(formatted) >= limit
        count_str = f"{len(formatted)}+" if hit_cap else str(len(formatted))
        header_parts = [f"**Found {count_str} messages"]
        if resolved_username:
            header_parts.append(f" from {resolved_username}")
        if query:
            header_parts.append(f" matching '{query}'")
        if live:
            header_parts.append(f" in <#{channel_id}>")
        if days:
            header_parts.append(f" (last {days} days)")
        else:
            header_parts.append(" (all time)")
        header_parts.append(f", sorted by {sort}")
        if hit_cap:
            header_parts.append(f" (showing top {limit}, use limit param for more)")
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


async def execute_inspect_message(
    params: Dict[str, Any],
    bot: discord.Client = None,
    visible_channels: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """Deep look at one message: content, reactions, context, replies, fresh media."""
    from scripts.discord_tools import context as dt_context, _set_active_guild_id
    from src.common.discord_utils import refresh_media_url

    message_id = params.get('message_id', '')
    context_size = params.get('context_size', 3)

    if not message_id:
        return {"success": False, "error": "message_id is required"}

    try:
        resolved_guild_id = _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        # Get message + context from DB via discord_tools
        ctx = dt_context(int(message_id), surrounding=context_size)

        if ctx.get('error'):
            return {"success": False, "error": ctx['error']}

        target = ctx.get('target', {})
        replies = ctx.get('replies', [])
        before = ctx.get('before', [])
        after = ctx.get('after', [])

        # Try to get live data from Discord API (fresh URLs + reaction detail)
        live_reactions = []
        media_urls = []
        channel_id = target.get('channel_id')
        if visible_channels is not None and channel_id not in visible_channels:
            return {"success": False, "error": "Permission denied"}

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
    raw_reply_to_tweet = params.get('reply_to_tweet')
    reply_to_tweet_id = None

    if raw_reply_to_tweet not in (None, ''):
        reply_value = str(raw_reply_to_tweet).strip()
        status_match = re.search(r'status/(\d+)', reply_value)
        if status_match:
            reply_to_tweet_id = status_match.group(1)
        elif reply_value.isdigit():
            reply_to_tweet_id = reply_value
        else:
            return {
                "success": False,
                "error": "reply_to_tweet must be a Tweet ID or a tweet URL containing status/<digits>"
            }

    # Parse link or use direct ID
    if message_link:
        parsed = parse_message_link(message_link)
        if not parsed:
            return {"success": False, "error": "Invalid message link format"}
        channel_id = parsed['channel_id']
        message_id = parsed['message_id']
    elif message_id:
        # Need to find the channel - search in DB
        from scripts.discord_tools import get_message as dt_get_message
        msg_data = dt_get_message(int(message_id))
        if not msg_data:
            return {"success": False, "error": f"Message {message_id} not found in database"}
        channel_id = msg_data['channel_id']
        message_id = int(message_id)
    else:
        return {"success": False, "error": "Provide either message_link or message_id"}

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            return {"success": False, "error": f"Could not find channel {channel_id}"}

        if isinstance(channel, discord.ForumChannel) or not hasattr(channel, 'fetch_message'):
            resolved_channel = None
            guild = getattr(channel, 'guild', None)
            if guild:
                resolved_channel = guild.get_thread(int(message_id))

            if resolved_channel is None:
                try:
                    fetched_channel = await bot.fetch_channel(int(message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched_channel = None

                if isinstance(fetched_channel, discord.Thread):
                    resolved_channel = fetched_channel

            if resolved_channel is None or not hasattr(resolved_channel, 'fetch_message'):
                return {"success": False, "error": f"Could not resolve thread for forum post {message_id}"}

            channel = resolved_channel

        message = await channel.fetch_message(message_id)
        if not message:
            return {"success": False, "error": f"Could not find message {message_id}"}

        tweet_text = params.get('tweet_text', '').strip() or None
        logger.info(
            f"[AdminChat] Triggering share for message {message_id} by user {message.author.id}" +
            (f" with custom tweet: '{tweet_text[:80]}...'" if tweet_text else "") +
            (f" in reply to tweet {reply_to_tweet_id}" if reply_to_tweet_id else "")
        )

        # Use existing sharing path
        result = await sharer.finalize_sharing(
            user_id=message.author.id,
            message_id=message.id,
            channel_id=channel.id,
            summary_channel=None,
            tweet_text=tweet_text,
            in_reply_to_tweet_id=reply_to_tweet_id,
        )

        if not result or not result.get("success"):
            return {"success": False, "error": (result or {}).get("error", "Sharing failed")}

        tweet_url = result.get("tweet_url")
        tweet_id = result.get("tweet_id")
        already_shared = bool(result.get("already_shared"))

        if already_shared:
            response_message = f"Already shared: {tweet_url}"
        else:
            response_message = f"Posted tweet: {tweet_url}"
            if reply_to_tweet_id:
                response_message += " (reply in thread)"

        return {
            "success": True,
            "message": response_message,
            "tweet_url": tweet_url,
            "tweet_id": tweet_id,
            "already_shared": already_shared,
        }

    except discord.NotFound:
        return {"success": False, "error": f"Message {message_id} not found"}
    except discord.Forbidden:
        return {"success": False, "error": "Bot doesn't have permission to access that channel/message"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in share_to_social: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_active_channels(
    params: Dict[str, Any],
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Get list of active channels."""
    from scripts.discord_tools import channels as dt_channels, _set_active_guild_id

    days = params.get('days', 7)

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        chs = dt_channels(days=days)

        # Apply visible_channels filter
        if visible_channels is not None:
            chs = [ch for ch in chs if ch['channel_id'] in visible_channels]

        # Format for LLM (top 20)
        formatted = [
            {
                "channel_id": str(ch['channel_id']),
                "name": ch['channel_name'],
                "messages": ch['messages']
            }
            for ch in chs[:20]
        ]

        return {
            "success": True,
            "count": len(formatted),
            "channels": formatted
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_active_channels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_daily_summaries(
    params: Dict[str, Any],
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Get daily summaries."""
    from collections import defaultdict
    from scripts.discord_tools import summaries as dt_summaries, _set_active_guild_id

    days = params.get('days', 7)
    channel_id = params.get('channel_id')

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        # Permission check for specific channel
        if channel_id and visible_channels is not None:
            if int(channel_id) not in visible_channels:
                return {"success": False, "error": "Permission denied"}

        rows = dt_summaries(days=days, channel_id=int(channel_id) if channel_id else None)

        # Apply visible_channels filter
        if visible_channels is not None:
            rows = [row for row in rows if int(row['channel_id']) in visible_channels]

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
                db_handler.storage_handler.supabase_client.table('members')
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
                "twitter_url": member.get('twitter_url'),
                "reddit_url": member.get('reddit_url'),
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
    """Delete one or more messages by ID."""
    channel_id = params.get('channel_id', '')
    message_id = params.get('message_id', '')
    message_ids = params.get('message_ids', [])

    if not channel_id:
        return {"success": False, "error": "channel_id is required"}

    # Combine single + list into one list
    ids_to_delete = list(message_ids)
    if message_id:
        ids_to_delete.append(message_id)
    if not ids_to_delete:
        return {"success": False, "error": "message_id or message_ids is required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
    except Exception as e:
        return {"success": False, "error": f"Could not find channel: {e}"}

    deleted = []
    errors = []
    for mid in ids_to_delete:
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.delete()
            deleted.append(mid)
        except discord.NotFound:
            errors.append(f"{mid}: not found")
        except discord.Forbidden:
            errors.append(f"{mid}: missing permissions")
        except Exception as e:
            errors.append(f"{mid}: {e}")

    result = {"success": True, "deleted": len(deleted), "deleted_ids": deleted}
    if errors:
        result["errors"] = errors
    return result


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
    from scripts.discord_tools import resolve_user as dt_resolve_user

    username = params.get('username', '')
    if not username:
        return {"success": False, "error": "username is required"}

    try:
        user_data = dt_resolve_user(username)
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
        resolved_guild_id = _resolve_guild_id(params)
        sb = _get_supabase()
        query = sb.table(table).select(select_cols)

        # Auto-scope guild_id for tables that have it
        GUILD_SCOPED_TABLES = {'discord_messages', 'discord_channels', 'daily_summaries',
                               'shared_posts', 'pending_intros', 'discord_reactions',
                               'discord_reaction_log', 'competitions'}
        if table in GUILD_SCOPED_TABLES and 'guild_id' not in filters:
            if resolved_guild_id:
                query = query.eq('guild_id', resolved_guild_id)

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


# ========== Media Tools ==========

MEDIA_DIR = '/tmp/media'
ALLOWED_MEDIA_BINARIES = {'ffmpeg', 'ffprobe', 'python3'}


async def execute_download_media(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Download attachments from a Discord message to /tmp/media/."""
    import aiohttp

    message_id = params.get('message_id', '')
    channel_id = params.get('channel_id', '')

    if not message_id or not channel_id:
        return {"success": False, "error": "message_id and channel_id are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch message: {e}"}

    if not message.attachments:
        return {"success": False, "error": "Message has no attachments"}

    out_dir = os.path.join(MEDIA_DIR, str(message_id))
    os.makedirs(out_dir, exist_ok=True)

    downloaded = []
    async with aiohttp.ClientSession() as session:
        for att in message.attachments:
            file_path = os.path.join(out_dir, att.filename)
            try:
                async with session.get(att.url) as resp:
                    if resp.status == 200:
                        with open(file_path, 'wb') as f:
                            f.write(await resp.read())
                        downloaded.append({
                            "filename": att.filename,
                            "path": file_path,
                            "size_bytes": os.path.getsize(file_path),
                            "content_type": att.content_type,
                        })
            except Exception as e:
                downloaded.append({"filename": att.filename, "error": str(e)})

    return {"success": True, "directory": out_dir, "files": downloaded}


async def execute_run_media_command(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a whitelisted media command (ffmpeg, ffprobe, python3)."""
    import asyncio as _asyncio
    import shlex

    command = params.get('command', '').strip()
    if not command:
        return {"success": False, "error": "command is required"}

    # Validate the binary is allowed
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return {"success": False, "error": f"Invalid command syntax: {e}"}

    binary = os.path.basename(parts[0])
    if binary not in ALLOWED_MEDIA_BINARIES:
        return {"success": False, "error": f"Binary '{binary}' not allowed. Use: {', '.join(sorted(ALLOWED_MEDIA_BINARIES))}"}

    os.makedirs(MEDIA_DIR, exist_ok=True)

    try:
        proc = await _asyncio.create_subprocess_shell(
            command,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            cwd=MEDIA_DIR,
        )
        stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=300)
        return {
            "success": proc.returncode == 0,
            "return_code": proc.returncode,
            "stdout": stdout.decode(errors='replace')[:4000],
            "stderr": stderr.decode(errors='replace')[:4000],
        }
    except _asyncio.TimeoutError:
        proc.kill()
        return {"success": False, "error": "Command timed out after 5 minutes"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_list_media_files(params: Dict[str, Any]) -> Dict[str, Any]:
    """List files in the media working directory."""
    sub = params.get('path', '')
    base = os.path.join(MEDIA_DIR, sub) if sub else MEDIA_DIR

    # Prevent directory traversal
    base = os.path.realpath(base)
    if not base.startswith(MEDIA_DIR):
        return {"success": False, "error": "Path must be within /tmp/media/"}

    if not os.path.exists(base):
        return {"success": True, "files": [], "note": "Directory does not exist yet. Download some media first."}

    files = []
    for root, dirs, filenames in os.walk(base):
        for fn in filenames:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, MEDIA_DIR)
            files.append({
                "path": fp,
                "relative": rel,
                "size_bytes": os.path.getsize(fp),
            })

    return {"success": True, "directory": base, "files": files}


# ========== Tool Executor Dispatcher ==========

async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    bot: discord.Client,
    db_handler,
    sharer,
    allowed_tools: Optional[Set[str]] = None,
    requester_id: Optional[int] = None,
    trusted_guild_id: Optional[int] = None,
    dm_channel_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a tool by name and return the result as a dict."""

    if allowed_tools is not None and tool_name not in allowed_tools:
        return {"success": False, "error": "Permission denied"}

    # Log params for search tools (skip reply/end_turn which are noisy)
    if tool_name not in ("reply", "end_turn"):
        logger.info(f"[AdminChat] Executing tool: {tool_name} {tool_input}")
    else:
        logger.info(f"[AdminChat] Executing tool: {tool_name}")

    trusted_tool_input = dict(tool_input)
    resolved_guild_id = trusted_guild_id if requester_id is not None else None
    visible_channels: Optional[Set[int]] = None

    if requester_id is not None and trusted_guild_id is not None:
        trusted_tool_input['guild_id'] = trusted_guild_id
        if tool_name in {"find_messages", "inspect_message", "get_active_channels", "get_daily_summaries"}:
            visible_channels = await _get_visible_channel_ids(bot, trusted_guild_id, requester_id)
            # Allow the requester to read their own DM with the bot via live=true.
            if dm_channel_id is not None:
                if visible_channels is None:
                    visible_channels = {dm_channel_id}
                else:
                    visible_channels = set(visible_channels) | {dm_channel_id}

    if tool_name == "reply":
        return execute_reply(trusted_tool_input)
    elif tool_name == "end_turn":
        return execute_end_turn(trusted_tool_input)
    elif tool_name == "find_messages":
        return await execute_find_messages(
            trusted_tool_input,
            bot,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "inspect_message":
        return await execute_inspect_message(
            trusted_tool_input,
            bot,
            visible_channels=visible_channels,
        )
    elif tool_name == "share_to_social":
        return await execute_share_to_social(bot, sharer, trusted_tool_input)
    elif tool_name == "get_active_channels":
        return await execute_get_active_channels(
            trusted_tool_input,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "get_daily_summaries":
        return await execute_get_daily_summaries(
            trusted_tool_input,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "get_member_info":
        return await execute_get_member_info(db_handler, trusted_tool_input)
    elif tool_name == "get_bot_status":
        return await execute_get_bot_status(bot)
    elif tool_name == "search_logs":
        return await execute_search_logs(trusted_tool_input)
    elif tool_name == "send_message":
        return await execute_send_message(bot, trusted_tool_input)
    elif tool_name == "edit_message":
        return await execute_edit_message(bot, trusted_tool_input)
    elif tool_name == "delete_message":
        return await execute_delete_message(bot, trusted_tool_input)
    elif tool_name == "upload_file":
        return await execute_upload_file(bot, trusted_tool_input)
    elif tool_name == "resolve_user":
        return await execute_resolve_user(trusted_tool_input)
    elif tool_name == "query_table":
        return await execute_query_table(trusted_tool_input)
    elif tool_name == "download_media":
        return await execute_download_media(bot, trusted_tool_input)
    elif tool_name == "run_media_command":
        return await execute_run_media_command(trusted_tool_input)
    elif tool_name == "list_media_files":
        return await execute_list_media_files(trusted_tool_input)
    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
