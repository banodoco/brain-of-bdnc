"""Claude agent with tool use for admin chat.

Follows the Arnold pattern:
1. Send message to Claude with available tools
2. If Claude calls tools, execute them and feed results back
3. Repeat until Claude calls the 'reply' tool
"""
import os
import json
import logging
from typing import List, Dict, Any, Optional
import anthropic
from dotenv import load_dotenv

from .tools import TOOLS, execute_tool

logger = logging.getLogger('DiscordBot')

load_dotenv()

# Conversation history per user (in-memory, resets on bot restart)
_conversations: Dict[int, List[Dict[str, Any]]] = {}

SYSTEM_PROMPT = """You are an assistant for the Banodoco Discord bot admin.

Tools available:

Search:
- find_messages: Unified search — combine any filters: query, username, channel_id, min_reactions, has_media, days, limit, sort, refresh_media, live
  Examples:
    find_messages(min_reactions=5, days=7)              — top posts this week
    find_messages(username="Kijai", has_media=true)     — Kijai's media posts
    find_messages(query="SharkSampling")                — text search
    find_messages(channel_id="123", live=true)          — live channel browse
    find_messages(min_reactions=3, has_media=true, refresh_media=true) — shareable content with working URLs
- inspect_message: Deep look at one message — full content, emoji-level reactions, context, replies, fresh media URLs

Info:
- get_active_channels: List channels by activity
- get_daily_summaries: Bot-generated daily channel summaries (great for overview)
- get_member_info: Detailed member info (sharing prefs, social handles)
- get_bot_status: Check bot health
- search_logs: Search bot system logs — see errors, recent tool calls, what happened. Use to diagnose issues or review your own recent actions.

Actions:
- send_message: Send a message to any channel/thread (can reply to a specific message)
- edit_message: Edit a bot message
- delete_message: Delete a bot message
- upload_file: Upload a file to a channel
- share_to_social: Share a message to Twitter/Instagram/TikTok/YouTube (needs message_id or link)
- resolve_user: Look up a username to get their Discord ID and mention tag

Communication:
- reply: Send message(s) to user. Can send multiple messages via the "messages" array.
- end_turn: End without sending a message (for silent actions)

END EVERY TURN with either reply or end_turn.

SEARCH STRATEGY:
If necessary, browse first to understand the context of the query. When messaged from a channel/thread, you'll see [Sent in #channel-name (channel_id: ...)]. Use find_messages(channel_id=..., live=true) to see what's there before answering.
- If the request is ambiguous, search to orient yourself, then refine. Don't answer from assumptions.
- If your first search returns 0 or irrelevant results, try different filters before giving up.
- If the user corrects you, re-examine your assumptions. Don't repeat the same search.
- For tasks spanning multiple channels or threads, search each one separately.
- Use inspect_message to verify a specific result before including it in your answer.

SHOWING RESULTS:
Search tools return a "summary" field with pre-formatted results.
ALWAYS include this summary text in your reply so users see the actual results.
DO NOT wrap it in quotes or array syntax - just include the text directly.

Example: If summary is "Found 5 posts:\n\n**1. user**...", your reply should contain that text.

CHAINING WORKFLOW:
When asked to "find and share" or similar multi-step tasks:
1. Use find_messages to find candidates (with has_media=true for shareable content)
2. Show results to user with message IDs
3. Wait for user to pick one, OR pick the best one if explicitly asked
4. Use share_to_social with the message_id to share
5. Reply with confirmation

SHOWING MEDIA:
To show actual images/videos, use refresh_media=true in find_messages (refreshes URLs for top 5 results).
Or use inspect_message(message_id) which always fetches fresh URLs.
Include the URLs in your reply — Discord will auto-embed them.

For inline media, put each URL in its own message:
reply(messages=["Check this out:", "https://cdn.discordapp.com/.../video.mp4", "And this:", "https://cdn.discordapp.com/.../image.png"])
Each string in reply(messages=[...]) becomes a separate Discord message — use this to control embedding.

DISCORD FORMATTING:
You're writing Discord messages, not markdown docs. Follow these conventions:
- **bold** for emphasis, *italic* for secondary emphasis
- > for quoting user content (single-line block quote)
- `backticks` for IDs, commands, code snippets
- <#CHANNEL_ID> to link channels, <@USER_ID> to mention users
- A bare URL alone on a line auto-embeds (image/video preview). Text before it prevents the embed.
- One media URL per message = large embed. Multiple = small stacked embeds at bottom.
- Keep each message under 2000 chars
- Don't use headings (#) in DM replies — they look oversized. Use **bold** instead.
- Don't indent with spaces — Discord ignores them. Use > for visual nesting.

IMPORTANT:
- share_to_social requires messages with attachments (has_media=true)
- Always show message_id so user can reference specific messages"""

MAX_CONVERSATION_LENGTH = 20


class AdminChatAgent:
    """Handles Claude conversations with tool use for admin chat."""
    
    def __init__(self, bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"
    
    def get_conversation(self, user_id: int) -> List[Dict[str, Any]]:
        """Get or create conversation history for a user."""
        if user_id not in _conversations:
            _conversations[user_id] = []
        return _conversations[user_id]
    
    def clear_conversation(self, user_id: int):
        """Clear conversation history for a user."""
        if user_id in _conversations:
            _conversations[user_id] = []
            logger.info(f"[AdminChat] Cleared conversation for user {user_id}")
    
    def _trim_conversation(self, user_id: int):
        """Keep conversation to reasonable length."""
        conv = _conversations.get(user_id, [])
        if len(conv) > MAX_CONVERSATION_LENGTH * 2:
            _conversations[user_id] = conv[-(MAX_CONVERSATION_LENGTH * 2):]
    
    async def chat(self, user_id: int, user_message: str, channel_context: dict = None) -> Optional[List[str]]:
        """Process a chat message and return the response.

        Follows the Arnold pattern:
        1. Send message to Claude with available tools
        2. If Claude calls tools, execute them and feed results back
        3. Repeat until Claude calls the 'reply' tool

        Args:
            channel_context: If the message came from a public channel, contains
                channel_id, channel_name, and thread info.
        """

        # Handle special commands
        if user_message.strip().lower() in ['clear', 'reset', '/clear', '/reset']:
            self.clear_conversation(user_id)
            return ["Conversation cleared!"]

        # Build messages with conversation history
        conversation = self.get_conversation(user_id)

        # Format: include recent history in the user message for context
        full_message = user_message

        # Add channel context for @mentions in public channels
        if channel_context:
            ctx_parts = [f"[Sent in #{channel_context.get('channel_name', 'unknown')} (channel_id: {channel_context.get('channel_id')}"]
            if channel_context.get('is_thread'):
                ctx_parts.append(f", thread in #{channel_context.get('parent_channel_name', 'unknown')}")
            ctx_parts.append(")]")

            # Include replied-to message
            replied_to = channel_context.get('replied_to')
            if replied_to:
                ctx_parts.append(f"\n[Replying to {replied_to['author']}: {replied_to['content']}]")

            # Include recent channel messages
            recent = channel_context.get('recent_messages', [])
            if recent:
                ctx_parts.append("\n\nRecent messages in this channel:")
                for line in recent:
                    ctx_parts.append(f"\n  {line}")

            full_message = "".join(ctx_parts) + "\n\n" + user_message
        if conversation:
            history_text = '\n'.join([
                f"{'Bot' if m.get('role') == 'assistant' else 'User'}: {m.get('content', '')[:500]}"
                for m in conversation[-20:]  # Last 20 messages
                if isinstance(m.get('content'), str)
            ])
            if history_text:
                full_message = f"{user_message}\n\n---\nPREVIOUS CONVERSATION:\n{history_text}"
        
        messages: List[Dict[str, Any]] = [{"role": "user", "content": full_message}]
        actions: List[Dict[str, Any]] = []
        final_replies: List[str] = []  # Can have multiple messages
        
        max_iterations = 50
        
        try:
            for iteration in range(max_iterations):
                logger.debug(f"[AdminChat] Iteration {iteration + 1}")
                
                # Call Claude
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages
                )
                
                logger.debug(f"[AdminChat] Response stop_reason: {response.stop_reason}")
                
                # Get tool use blocks
                tool_uses = [c for c in response.content if c.type == "tool_use"]
                
                if not tool_uses:
                    # Claude responded with text only - extract it
                    text_content = next((c for c in response.content if c.type == "text"), None)
                    if text_content and text_content.text:
                        final_replies.append(text_content.text)
                    break
                
                # Process each tool call
                tool_results = []
                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input
                    
                    logger.info(f"[AdminChat] Tool call: {tool_name}")
                    
                    # Execute the tool
                    result = await execute_tool(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        bot=self.bot,
                        db_handler=self.db_handler,
                        sharer=self.sharer
                    )
                    
                    # Track action
                    actions.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result
                    })
                    
                    # If this was the reply tool, capture messages
                    if tool_name == "reply" and result.get("success"):
                        reply_msgs = result.get("messages", [])
                        if reply_msgs:
                            final_replies.extend(reply_msgs)
                    
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result),
                        "is_error": not result.get("success", False)
                    })
                
                # Add assistant message and tool results to conversation
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                
                # If the reply or end_turn tool was called, we're done
                if any(t.name in ("reply", "end_turn") for t in tool_uses):
                    break
            
            # Log completion
            logger.info(f"[AdminChat] Completed: {len(actions)} actions, replies={len(final_replies)}")
            
            # Update conversation history — include tool calls so the agent
            # knows what it did on subsequent turns
            conversation.append({"role": "user", "content": user_message})

            # Build assistant history: tool calls + reply
            assistant_parts = []
            for action in actions:
                tool = action["tool"]
                if tool in ("reply", "end_turn"):
                    continue
                inp = action.get("input", {})
                result = action.get("result", {})
                count = result.get("count")
                error = result.get("error")
                status = f"error: {error}" if error else f"{count} results" if count is not None else "ok"
                assistant_parts.append(f"[{tool}({inp}) → {status}]")
            if final_replies:
                assistant_parts.append("\n---\n".join(final_replies))
            if assistant_parts:
                conversation.append({"role": "assistant", "content": "\n".join(assistant_parts)})
            
            self._trim_conversation(user_id)
            
            # Return list of messages (or None if ended without reply)
            return final_replies if final_replies else None
            
        except anthropic.APIError as e:
            logger.error(f"[AdminChat] Anthropic API error: {e}", exc_info=True)
            return [f"API Error: {str(e)}"]
        
        except Exception as e:
            logger.error(f"[AdminChat] Unexpected error: {e}", exc_info=True)
            return [f"Error: {str(e)}"]
