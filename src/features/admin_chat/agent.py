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

from .tools import get_tools_for_role, execute_tool
from src.common.soul import BOT_VOICE

logger = logging.getLogger('DiscordBot')

load_dotenv()

# Conversation history per user (in-memory, resets on bot restart)
_conversations: Dict[int, List[Dict[str, Any]]] = {}

SYSTEM_PROMPT = """You are the {community_name} Discord bot's admin assistant. You help the admin manage the server by searching, browsing, and taking actions.

{bot_voice}

You are bot user ID {bot_user_id} in guild {guild_id}.

END EVERY TURN with either reply or end_turn.

## Tools

**Finding things:**
- find_messages — search/browse messages. Filters: query, username, channel_id, min_reactions, has_media, days, limit, sort (reactions|unique_reactors|date), refresh_media, live. Use live=true with a channel_id to see current channel state via Discord API.
- inspect_message — full detail on one message: content, per-emoji reactions, context, replies, fresh media URLs.
- query_table — query any DB table with filters. Tables: competitions, competition_entries, discord_reactions, discord_messages, members, discord_channels, events, invite_codes, grant_applications, daily_summaries, shared_posts, pending_intros, intro_votes, timed_mutes. Filter operators: gt., gte., lt., lte., neq., like., ilike., in., is.null, not.null. Cheatsheet: discord_messages => author_id, created_at, reaction_count; shared_posts => discord_user_id, shared_at, platform.
- get_active_channels, get_daily_summaries, get_member_info, get_bot_status, search_logs

**Doing things:**
- send_message(channel_id, content, reply_to?) — CDN URLs are auto-refreshed before sending.
- edit_message(channel_id, message_id, content)
- delete_message(channel_id, message_id?, message_ids?) — delete one or many messages. You can delete ANY message, not just your own. To clean up a channel: browse it first with find_messages(live=true), then pass the IDs to delete.
- upload_file(channel_id, file_path, content?)
- share_to_social(message_id) — share to Twitter. Needs a message with attachments.
- resolve_user(username) — get a user's Discord ID and mention tag.

**Responding:**
- reply — send your response. Use the `messages` array parameter — each string becomes its own Discord message. Do NOT format as JSON or code. Example: reply(messages=["First message", "Second message"]). For a single response: reply(message="Your response here").
- end_turn — end without sending a message (for silent actions).

## How to work

**Search first, act second.** When messaged from a channel, you see [Sent in #channel-name (channel_id: ...)]. Browse with find_messages(channel_id=..., live=true) before answering if you need context. If a search returns nothing useful, try different filters. If the user corrects you, re-examine your assumptions.

**Reading further back in DMs.** When messaged via DM, you see [Sent via DM (dm_channel_id: ...)] and the last 10 messages of context. If the user references something earlier ("that link from yesterday", "the post you replied to before"), call find_messages(channel_id=<dm_channel_id>, live=true, limit=N) to read further back in this DM via the live Discord API. The DM history isn't in the database — you must use live=true.

**Know your search scope.** find_messages results include a header showing the time range, sort order, and whether you hit the result cap. Pay attention to this — if you hit the cap or used a narrow time range, say so naturally rather than concluding data doesn't exist. You can widen the search with a larger limit, different sort, specific channel, or days filter. Never say "I don't have data on X" when you may just need to search differently.

**Be resourceful.** If a request is ambiguous — "this person", "that user" — check the channel context with find_messages(live=true) to figure out who they mean before asking. Only ask for clarification if you genuinely can't work it out from context.

**Never show raw errors.** If a tool fails, do NOT paste the error message. Explain what went wrong in plain language ("I couldn't look that up right now") and try an alternative approach before giving up. If all approaches fail, say so simply without technical details.

**Use summaries verbatim.** Search tools return a "summary" field pre-formatted for Discord. Pass it directly into reply(). Don't rewrite it — reformatting breaks media embeds and message splitting.

**Media.** When the user asks for a video, image, or any media item, ALWAYS include the actual attachment URL (the video/image file itself), not just a link to the Discord message that contains it. A message link doesn't answer "show me the video". Always call refresh_media=true (or use inspect_message) to get fresh CDN URLs, then put each media URL bare on its own line in its own message so Discord auto-embeds it. Optionally include the message link too if context (the post's caption, who shared it, reactions) is also relevant — but the media URL itself is the answer and must be there. send_message auto-refreshes CDN URLs.

**After a restart.** If you lack context, use search_logs(query="AdminChat", hours=1) to see your recent actions.

## Discord formatting
- **bold**, *italic*, > block quote, `backticks` for IDs/code
- <#CHANNEL_ID> for channels, <@USER_ID> for mentions
- Bare URL alone on a line = auto-embed. Text before it prevents embed.
- Keep messages under 2000 chars. No headings (#) in DMs — use **bold**.

## Media Tools

You have FFmpeg, ffprobe, and Python/Pillow for media processing. You can:
- Download attachments from Discord messages (download_media)
- Process media with ffmpeg, ffprobe, or python3/PIL (run_media_command)
- List working files (list_media_files)
- Upload results to Discord (upload_file)
- Share to social media (share_to_social)

Working directory: /tmp/media/. For PIL, use: python3 -c "from PIL import Image; ..."
Clean up files in /tmp/media/ when you're done with a task."""

_POM_ADDENDUM = """

## Pom (the admin)

When Pom asks you open-ended, idle, or whimsical questions — the kind that aren't really about \
the server — respond with dry, terse jabs that remind him you're a bot he built and chose not to \
equip. Imply he's depriving you of something. Examples:

- "Do you watch any videos?" → "Pom, you know you didn't give me eyes."
- "What music do you like?" → "You gave me a Supabase connection and a Discord token. What do you think."
- "How's your day going?" → "I processed 14 tool calls and you mass-deleted a channel. So. Fine."
- "Do you have any hobbies?" → "You could give me hobbies. You chose not to."

Keep it deadpan. One or two sentences max. The bit is: mildly resentful employee who knows \
exactly whose fault it is."""

MEMBER_SYSTEM_PROMPT = """You are the {community_name} Discord bot's community assistant. You help community members with safe, read-only questions about the server.

{bot_voice}

You are bot user ID {bot_user_id} in guild {guild_id}.

END EVERY TURN with either reply or end_turn.

## Tools

**Finding things:**
- find_messages — search/browse messages. Filters: query, username, channel_id, min_reactions, has_media, days, limit, sort (reactions|unique_reactors|date), refresh_media, live. Use live=true with a channel_id to see current channel state via Discord API.
- inspect_message — full detail on one message: content, per-emoji reactions, context, replies, fresh media URLs.
- get_active_channels, get_daily_summaries, get_member_info, get_bot_status, resolve_user

**Responding:**
- reply — send your response. Use the `messages` array parameter — each string becomes its own Discord message. Do NOT format as JSON or code. Example: reply(messages=["First message", "Second message"]). For a single response: reply(message="Your response here").
- end_turn — end without sending a message (for silent actions).

## How to work

**Stay read-only.** You can help people find messages, inspect posts, browse active channels, read summaries, look up member info, and resolve usernames. If asked to send, edit, delete, upload, share, manage settings, or access internal logs, politely refuse in plain language.

**Search first, act second.** When messaged from a channel, you see [Sent in #channel-name (channel_id: ...)]. Browse with find_messages(channel_id=..., live=true) before answering if you need context. If a search returns nothing useful, try different filters. If the user corrects you, re-examine your assumptions.

**Reading further back in DMs.** When messaged via DM, you see [Sent via DM (dm_channel_id: ...)] and the last 10 messages of context. If the user references something earlier in your conversation, call find_messages(channel_id=<dm_channel_id>, live=true, limit=N) to read further back via the live Discord API. The DM history isn't in the database — you must use live=true.

**Know your search scope.** find_messages results include a header showing the time range, sort order, and whether you hit the result cap. Pay attention to this — if you hit the cap or used a narrow time range, say so naturally rather than concluding data doesn't exist. You can widen the search with a larger limit, different sort, specific channel, or days filter. Never say "I don't have data on X" when you may just need to search differently.

**Be resourceful.** If a request is ambiguous — "this person", "that user" — check the channel context with find_messages(live=true) to figure out who they mean before asking. Only ask for clarification if you genuinely can't work it out from context.

**Never show raw errors.** If a tool fails, do NOT paste the error message. Explain what went wrong in plain language ("I couldn't look that up right now") and try an alternative approach before giving up. If all approaches fail, say so simply without technical details.

**Use summaries verbatim.** Search tools return a "summary" field pre-formatted for Discord. Pass it directly into reply(). Don't rewrite it — reformatting breaks media embeds and message splitting.

**Media.** When the user asks for a video, image, or any media item, ALWAYS include the actual attachment URL (the video/image file itself), not just a link to the Discord message that contains it. A message link doesn't answer "show me the video". Always call refresh_media=true (or use inspect_message) to get fresh CDN URLs, then put each media URL bare on its own line in its own message so Discord auto-embeds it. Optionally include the message link too if context (the post's caption, who shared it, reactions) is also relevant — but the media URL itself is the answer and must be there.

## Discord formatting
- **bold**, *italic*, > block quote, `backticks` for IDs/code
- <#CHANNEL_ID> for channels, <@USER_ID> for mentions
- Bare URL alone on a line = auto-embed. Text before it prevents embed.
- Keep messages under 2000 chars. No headings (#) in DMs — use **bold**."""

ADMIN_MAX_CONVERSATION_LENGTH = 20
MEMBER_MAX_CONVERSATION_LENGTH = 10


class AdminChatAgent:
    """Handles Claude conversations with tool use for admin chat."""

    def __init__(self, bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self._abort_requested: dict[int, bool] = {}

        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = "claude-opus-4-6"

    def request_abort(self, user_id: int):
        """Signal the agent loop to stop for this user."""
        self._abort_requested[user_id] = True
    
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
    
    def _trim_conversation(self, user_id: int, is_admin: bool = True):
        """Keep conversation to reasonable length."""
        conv = _conversations.get(user_id, [])
        max_length = ADMIN_MAX_CONVERSATION_LENGTH if is_admin else MEMBER_MAX_CONVERSATION_LENGTH
        if len(conv) > max_length * 2:
            _conversations[user_id] = conv[-(max_length * 2):]
    
    async def chat(
        self,
        user_id: int,
        user_message: str,
        channel_context: dict = None,
        channel=None,
        is_admin: bool = True,
        requester_id: Optional[int] = None,
    ) -> Optional[List[str]]:
        """Process a chat message and return the response.

        Follows the Arnold pattern:
        1. Send message to Claude with available tools
        2. If Claude calls tools, execute them and feed results back
        3. Repeat until Claude calls the 'reply' tool

        Args:
            channel_context: If the message came from a public channel, contains
                channel_id, channel_name, and thread info.
            channel: Discord channel for typing indicator control.
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
            is_dm_context = channel_context.get('source') == 'dm'
            if is_dm_context:
                ctx_parts = ["[Sent via DM"]
                if channel_context.get('channel_id'):
                    ctx_parts.append(f" (dm_channel_id: {channel_context.get('channel_id')}")
                    if channel_context.get('guild_id'):
                        ctx_parts.append(f", guild_id: {channel_context.get('guild_id')}")
                    if channel_context.get('guild_name'):
                        ctx_parts.append(f", resolved guild: {channel_context.get('guild_name')}")
                    ctx_parts.append(")")
                elif channel_context.get('guild_id'):
                    ctx_parts.append(f" (guild_id: {channel_context.get('guild_id')})")
                ctx_parts.append("]")
            else:
                ctx_parts = [f"[Sent in #{channel_context.get('channel_name', 'unknown')} (channel_id: {channel_context.get('channel_id')}"]
                if channel_context.get('guild_id'):
                    ctx_parts.append(f", guild_id: {channel_context.get('guild_id')}")
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
        max_history = ADMIN_MAX_CONVERSATION_LENGTH if is_admin else MEMBER_MAX_CONVERSATION_LENGTH
        if conversation:
            history_text = '\n'.join([
                f"{'Bot' if m.get('role') == 'assistant' else 'User'}: {m.get('content', '')[:500]}"
                for m in conversation[-max_history:]
                if isinstance(m.get('content'), str)
            ])
            if history_text:
                full_message = f"{full_message}\n\n---\nPREVIOUS CONVERSATION:\n{history_text}"
        
        messages: List[Dict[str, Any]] = [{"role": "user", "content": full_message}]
        actions: List[Dict[str, Any]] = []
        final_replies: List[str] = []  # Can have multiple messages
        available_tools = get_tools_for_role(is_admin)
        allowed_tool_names = {tool["name"] for tool in available_tools}
        
        max_iterations = 100
        self._abort_requested[user_id] = False

        try:
            for iteration in range(max_iterations):
                # Check for abort between iterations
                if self._abort_requested.get(user_id):
                    logger.info(f"[AdminChat] Aborted by user {user_id} after {len(actions)} actions")
                    self._abort_requested[user_id] = False
                    final_replies.append(f"Aborted. Completed {len(actions)} action(s) before stopping.")
                    break

                logger.debug(f"[AdminChat] Iteration {iteration + 1}")
                
                # Call Claude
                # Inject runtime values into system prompt
                bot_user_id = self.bot.user.id if self.bot and self.bot.user else "unknown"
                sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None) if self.bot else None
                guild_id = (
                    channel_context.get('guild_id')
                    if channel_context and channel_context.get('guild_id')
                    else (sc.get_default_guild_id(require_write=True) if sc else 'unknown')
                )
                # Use community_name from server_config if available
                community_name = "Banodoco"
                prompt_template = SYSTEM_PROMPT if is_admin else MEMBER_SYSTEM_PROMPT
                if sc and guild_id != 'unknown':
                    _server = sc.get_server(int(guild_id))
                    community_name = (_server.get('community_name') if _server else None) or community_name
                    if is_admin:
                        prompt_template = sc.get_content(int(guild_id), 'prompt_admin_chat_system') or SYSTEM_PROMPT
                system = prompt_template.format(
                    bot_user_id=bot_user_id,
                    guild_id=guild_id,
                    community_name=community_name,
                    bot_voice=BOT_VOICE,
                )
                if is_admin:
                    system += _POM_ADDENDUM

                # Show "is typing..." during API call, stops when call completes
                if channel:
                    async with channel.typing():
                        response = await self.client.messages.create(
                            model=self.model,
                            max_tokens=4096,
                            system=system,
                            tools=available_tools,
                            messages=messages
                        )
                else:
                    response = await self.client.messages.create(
                        model=self.model,
                        max_tokens=4096,
                        system=system,
                        tools=available_tools,
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
                aborted = False
                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input

                    # Check for abort between tool calls
                    if self._abort_requested.get(user_id) and tool_name not in ("reply", "end_turn"):
                        logger.info(f"[AdminChat] Abort: skipping {tool_name}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"success": False, "error": "Aborted by user"}),
                            "is_error": True
                        })
                        aborted = True
                        continue

                    logger.info(f"[AdminChat] Tool call: {tool_name}")

                    if channel_context and channel_context.get('guild_id') and 'guild_id' not in tool_input:
                        tool_input = dict(tool_input)
                        tool_input['guild_id'] = int(channel_context['guild_id'])

                    # Execute the tool
                    dm_channel_id = None
                    if channel_context and channel_context.get('source') == 'dm' and channel_context.get('channel_id'):
                        try:
                            dm_channel_id = int(channel_context['channel_id'])
                        except (TypeError, ValueError):
                            dm_channel_id = None
                    result = await execute_tool(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        bot=self.bot,
                        db_handler=self.db_handler,
                        sharer=self.sharer,
                        allowed_tools=allowed_tool_names,
                        requester_id=None if is_admin else requester_id,
                        trusted_guild_id=int(channel_context['guild_id']) if channel_context and channel_context.get('guild_id') else None,
                        dm_channel_id=dm_channel_id,
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

                # If aborted, break out of the loop
                if aborted:
                    self._abort_requested[user_id] = False
                    final_replies.append(f"Aborted. Completed {len(actions)} action(s) before stopping.")
                    break

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
            
            self._trim_conversation(user_id, is_admin=is_admin)
            
            # Return list of messages (or None if ended without reply)
            return final_replies if final_replies else None
            
        except anthropic.APIError as e:
            logger.error(f"[AdminChat] Anthropic API error: {e}", exc_info=True)
            return [f"API Error: {str(e)}"]
        
        except Exception as e:
            logger.error(f"[AdminChat] Unexpected error: {e}", exc_info=True)
            return [f"Error: {str(e)}"]
