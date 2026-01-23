"""Tool definitions and executors for admin chat.

Tools call into existing bot functionality to maintain consistency.
Following the Arnold pattern - includes a 'reply' tool that the LLM uses to respond.
"""
import os
import re
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
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
        "name": "get_top_messages",
        "description": "Get the most popular messages by reaction count. Can search server-wide or in a specific channel. Great for finding content to share. Returns message IDs you can use with share_to_social.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Filter to a specific channel ID (optional - if omitted, searches server-wide)"
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to search (default 7)"
                },
                "min_reactions": {
                    "type": "integer",
                    "description": "Minimum reaction count to include (default 3)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20, max 50)"
                },
                "has_media": {
                    "type": "boolean",
                    "description": "Only return messages with attachments/media (default false)"
                }
            },
            "required": []
        }
    },
    {
        "name": "search_content",
        "description": "Search messages by text content. Useful for finding specific topics, LoRAs, tools, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for (case-insensitive)"
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to search (default 7)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20)"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_message_context",
        "description": "Get a message with its full context: the message itself, all replies to it, and surrounding messages. Use this to understand community response to a post.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                },
                "surrounding": {
                    "type": "integer",
                    "description": "Number of surrounding messages to include (default 5)"
                }
            },
            "required": ["message_id"]
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
        "name": "refresh_media",
        "description": "Get fresh media URLs for a message. Discord CDN URLs expire, so use this to get current downloadable/viewable URLs for attachments. Returns URLs you can include in replies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                }
            },
            "required": ["message_id"]
        }
    }
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
        "has_media": bool(msg.get('attachments') or msg.get('attachment_urls')),
        "date": msg.get('created_at', '')[:10] if msg.get('created_at') else None,
    }
    if msg.get('channel_name'):
        result["channel"] = msg.get('channel_name')
    if include_link:
        result["link"] = f"https://discord.com/channels/{GUILD_ID}/{msg.get('channel_id')}/{msg.get('message_id')}"
    return result


# ========== Tool Executors ==========

def execute_reply(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the reply tool - returns message(s) to send."""
    # Support both single message and array of messages
    messages = params.get('messages', [])
    single_message = params.get('message', '')
    
    if single_message and not messages:
        messages = [single_message]
    
    if not messages:
        return {"success": False, "error": "No message provided"}
    
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


async def execute_get_top_messages(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get top messages by reaction count."""
    from scripts.weekly_digest import get_top_messages_server_wide, get_top_messages, get_messages_with_media
    
    channel_id = params.get('channel_id')
    days = params.get('days', 7)
    min_reactions = params.get('min_reactions', 3)
    limit = min(params.get('limit', 20), 50)
    has_media = params.get('has_media', False)
    
    try:
        if has_media:
            # Use media-specific function
            if channel_id:
                messages = get_messages_with_media(channel_id=int(channel_id), days=days, min_reactions=min_reactions, limit=limit)
            else:
                messages = get_messages_with_media(days=days, min_reactions=min_reactions, limit=limit)
        elif channel_id:
            messages = get_top_messages(int(channel_id), days=days, min_reactions=min_reactions, limit=limit)
        else:
            messages = get_top_messages_server_wide(days=days, min_reactions=min_reactions, limit=limit)
        
        if not messages:
            return {
                "success": True,
                "count": 0,
                "summary": f"No messages found with {min_reactions}+ reactions in the last {days} days.",
                "messages": []
            }
        
        # Format for LLM
        formatted = [format_message_for_llm(msg) for msg in messages[:limit]]
        
        # Create pre-formatted summary for easy inclusion in reply
        summary_lines = [f"Found {len(formatted)} messages:\n"]
        for i, msg in enumerate(formatted, 1):
            media_tag = " 📷" if msg.get('has_media') else ""
            content_preview = msg.get('content', '')[:80]
            if len(msg.get('content', '')) > 80:
                content_preview += "..."
            summary_lines.append(
                f"**{i}. {msg['author']}** ({msg['reactions']} reactions{media_tag})\n"
                f"   {content_preview}\n"
                f"   ID: `{msg['message_id']}`"
            )
        
        return {
            "success": True,
            "count": len(formatted),
            "summary": "\n".join(summary_lines),
            "messages": formatted
        }
        
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_top_messages: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_search_content(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search messages by content."""
    from scripts.weekly_digest import search_messages_by_content
    
    query = params.get('query', '')
    days = params.get('days', 7)
    limit = min(params.get('limit', 20), 50)
    
    if not query:
        return {"success": False, "error": "query is required"}
    
    try:
        messages = search_messages_by_content(query, days=days, limit=limit)
        
        if not messages:
            return {
                "success": True,
                "query": query,
                "count": 0,
                "summary": f"No messages found matching '{query}' in the last {days} days.",
                "messages": []
            }
        
        # Format for LLM
        formatted = [format_message_for_llm(msg) for msg in messages[:limit]]
        
        # Create pre-formatted summary
        summary_lines = [f"Found {len(formatted)} messages matching '{query}':\n"]
        for i, msg in enumerate(formatted, 1):
            media_tag = " 📷" if msg.get('has_media') else ""
            content_preview = msg.get('content', '')[:80]
            if len(msg.get('content', '')) > 80:
                content_preview += "..."
            summary_lines.append(
                f"**{i}. {msg['author']}** ({msg['reactions']} reactions{media_tag})\n"
                f"   {content_preview}\n"
                f"   ID: `{msg['message_id']}`"
            )
        
        return {
            "success": True,
            "query": query,
            "count": len(formatted),
            "summary": "\n".join(summary_lines),
            "messages": formatted
        }
        
    except Exception as e:
        logger.error(f"[AdminChat] Error in search_content: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_message_context(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get message with context (replies, surrounding)."""
    from scripts.weekly_digest import get_message_context
    
    message_id = params.get('message_id', '')
    surrounding = params.get('surrounding', 5)
    
    if not message_id:
        return {"success": False, "error": "message_id is required"}
    
    try:
        context = get_message_context(int(message_id), surrounding=surrounding)
        
        if context.get('error'):
            return {"success": False, "error": context['error']}
        
        # Format the target message
        target = context.get('target_message', {})
        formatted_target = format_message_for_llm(target, include_link=True) if target else None
        
        # Format replies
        replies = context.get('replies', [])
        formatted_replies = [
            {"author": r.get('author_name', 'Unknown'), "content": (r.get('content', '') or '')[:200]}
            for r in replies[:10]  # Limit replies
        ]
        
        return {
            "success": True,
            "target_message": formatted_target,
            "reply_count": len(replies),
            "replies": formatted_replies,
            "has_community_response": len(replies) > 0
        }
        
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_message_context: {e}", exc_info=True)
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


async def execute_refresh_media(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh media URLs for a message using Discord API."""
    from src.common.discord_utils import refresh_media_url
    from scripts.weekly_digest import get_message_by_id
    
    message_id = params.get('message_id', '')
    
    if not message_id:
        return {"success": False, "error": "message_id is required"}
    
    try:
        # First get message from DB to find channel
        msg_data = get_message_by_id(int(message_id))
        if not msg_data:
            return {"success": False, "error": f"Message {message_id} not found in database"}
        
        channel_id = msg_data.get('channel_id')
        if not channel_id:
            return {"success": False, "error": "Could not determine channel ID"}
        
        # Use discord_utils to refresh
        result = await refresh_media_url(bot, channel_id, int(message_id), logger)
        
        if not result or not result.get('success'):
            return {"success": False, "error": "Could not refresh media URLs (message may be deleted)"}
        
        attachments = result.get('attachments', [])
        
        # Format for LLM - just the URLs
        media_urls = []
        for att in attachments:
            url = att.get('url', '')
            filename = att.get('filename', 'unknown')
            content_type = att.get('content_type', '')
            media_urls.append({
                "filename": filename,
                "url": url,
                "type": content_type
            })
        
        return {
            "success": True,
            "message_id": message_id,
            "author": msg_data.get('author_name', 'Unknown'),
            "content": (msg_data.get('content', '') or '')[:200],
            "media_count": len(media_urls),
            "media": media_urls,
            "link": f"https://discord.com/channels/{GUILD_ID}/{channel_id}/{message_id}"
        }
        
    except Exception as e:
        logger.error(f"[AdminChat] Error in refresh_media: {e}", exc_info=True)
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
    
    logger.info(f"[AdminChat] Executing tool: {tool_name}")
    
    if tool_name == "reply":
        return execute_reply(tool_input)
    elif tool_name == "end_turn":
        return execute_end_turn(tool_input)
    elif tool_name == "share_to_social":
        return await execute_share_to_social(bot, sharer, tool_input)
    elif tool_name == "get_top_messages":
        return await execute_get_top_messages(tool_input)
    elif tool_name == "search_content":
        return await execute_search_content(tool_input)
    elif tool_name == "get_message_context":
        return await execute_get_message_context(tool_input)
    elif tool_name == "get_active_channels":
        return await execute_get_active_channels(tool_input)
    elif tool_name == "get_member_info":
        return await execute_get_member_info(db_handler, tool_input)
    elif tool_name == "get_bot_status":
        return await execute_get_bot_status(bot)
    elif tool_name == "refresh_media":
        return await execute_refresh_media(bot, tool_input)
    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
