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
- reply: Send message(s) to user. Can send multiple messages via the "messages" array.
- end_turn: End without sending a message (for silent actions)
- share_to_social: Share a message to Twitter/Instagram/TikTok/YouTube (needs message_id or link)
- get_top_messages: Find popular messages by reactions (can filter by channel, media-only)
- search_content: Search messages by text content  
- get_message_context: Get a message with its replies and community response
- get_active_channels: List channels by activity
- get_member_info: Look up a Discord member
- get_bot_status: Check bot health
- refresh_media: Get fresh, working URLs for a message's attachments (Discord URLs expire)

END EVERY TURN with either reply or end_turn.

CRITICAL - SHOWING RESULTS:
Search tools (get_top_messages, search_content) return a "summary" field that's pre-formatted for display.
ALWAYS include this summary in your reply. Example workflow:
1. Call get_top_messages
2. Get result with "summary" field containing formatted list
3. Use reply() and include the summary text so user sees the results
NEVER say "here are options" without showing them. The summary field has them ready to show.

CHAINING WORKFLOW:
When asked to "find and share" or similar multi-step tasks:
1. Use search tools to find candidates
2. Show results to user with message IDs
3. Wait for user to pick one, OR pick the best one if explicitly asked
4. Use share_to_social with the message_id to share
5. Reply with confirmation

SHOWING MEDIA IN DMs:
Discord CDN URLs expire. To show actual images/videos in the chat:
1. Find messages with get_top_messages or search_content
2. Use refresh_media(message_id) to get fresh URLs  
3. Include the URLs in your reply - Discord will auto-embed them

For long responses, use multiple messages:
reply(messages=["First part...", "Second part..."])

IMPORTANT:
- share_to_social requires messages with attachments (has_media=true)
- Use has_media=true in get_top_messages to find shareable content
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
    
    async def chat(self, user_id: int, user_message: str) -> Optional[List[str]]:
        """Process a chat message and return the response.
        
        Follows the Arnold pattern:
        1. Send message to Claude with available tools
        2. If Claude calls tools, execute them and feed results back
        3. Repeat until Claude calls the 'reply' tool
        """
        
        # Handle special commands
        if user_message.strip().lower() in ['clear', 'reset', '/clear', '/reset']:
            self.clear_conversation(user_id)
            return ["Conversation cleared!"]
        
        # Build messages with conversation history
        conversation = self.get_conversation(user_id)
        
        # Format: include recent history in the user message for context
        full_message = user_message
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
            
            # Update conversation history
            conversation.append({"role": "user", "content": user_message})
            if final_replies:
                # Store combined reply in history
                conversation.append({"role": "assistant", "content": "\n---\n".join(final_replies)})
            
            self._trim_conversation(user_id)
            
            # Return list of messages (or None if ended without reply)
            return final_replies if final_replies else None
            
        except anthropic.APIError as e:
            logger.error(f"[AdminChat] Anthropic API error: {e}", exc_info=True)
            return [f"API Error: {str(e)}"]
        
        except Exception as e:
            logger.error(f"[AdminChat] Unexpected error: {e}", exc_info=True)
            return [f"Error: {str(e)}"]
