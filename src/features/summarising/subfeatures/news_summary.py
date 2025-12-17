import os
import json
import logging
from typing import List, Dict, Any
import asyncio

from dotenv import load_dotenv
import anthropic

# We removed direct Discord/bot usage here since SUMMARIZER handles posting logic now.
# This class now focuses on:
#  - queries to Claude (generate_news_summary, combine_channel_summaries, etc.)
#  - chunking/formatting the prompt & returned JSON.

class NewsSummarizer:
    def __init__(self, logger: logging.Logger, dev_mode=False):
        self.logger = logger
        self.logger.info("Initializing NewsSummarizer...")

        load_dotenv()
        self.dev_mode = dev_mode
        
        # Initialize Anthropic client
        self.anthropic_client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )

        if self.dev_mode:
            self.guild_id = int(os.getenv('DEV_GUILD_ID'))
            self.top_gen_channel_id = int(os.getenv('DEV_TOP_GEN_CHANNEL')) if os.getenv('DEV_TOP_GEN_CHANNEL') else None
        else:
            self.guild_id = int(os.getenv('GUILD_ID'))
            self.top_gen_channel_id = int(os.getenv('TOP_GEN_CHANNEL')) if os.getenv('TOP_GEN_CHANNEL') else None

        self.logger.info("NewsSummarizer initialized.")

    async def _call_anthropic(
        self, 
        system_prompt: str, 
        user_content: str, 
        max_tokens: int = 16000,
        max_retries: int = 3,
        use_web_search: bool = False
    ) -> str:
        """
        Call Anthropic API with Claude Sonnet 4.5.
        
        Args:
            use_web_search: If True, enables web search tool (beta). 
                           Disabled by default - was causing JSON parsing issues.
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Run the synchronous API call in a thread pool to not block the event loop
                loop = asyncio.get_event_loop()
                
                if use_web_search:
                    # Use beta API with web search tool
                    message = await loop.run_in_executor(
                        None,
                        lambda: self.anthropic_client.beta.messages.create(
                            model="claude-sonnet-4-5-20250929",
                            max_tokens=max_tokens,
                            temperature=1,
                            system=system_prompt,
                            messages=[
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": user_content}]
                                }
                            ],
                            tools=[{"name": "web_search", "type": "web_search_20250305"}],
                            betas=["web-search-2025-03-05"]
                        )
                    )
                else:
                    # Standard API without web search
                    message = await loop.run_in_executor(
                        None,
                        lambda: self.anthropic_client.messages.create(
                            model="claude-sonnet-4-5-20250929",
                            max_tokens=max_tokens,
                            temperature=1,
                            system=system_prompt,
                            messages=[
                                {
                                    "role": "user",
                                    "content": user_content
                                }
                            ]
                        )
                    )
                
                # Extract text from the response content blocks
                text_parts = []
                for block in message.content:
                    if hasattr(block, 'text'):
                        text_parts.append(block.text)
                
                return "\n".join(text_parts) if text_parts else ""
                
            except anthropic.RateLimitError as e:
                last_error = e
                wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                self.logger.warning(f"Rate limit hit (attempt {attempt + 1}/{max_retries}). Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
                continue
            except Exception as e:
                self.logger.error(f"Error calling Anthropic API: {e}", exc_info=True)
                raise
        
        # All retries exhausted
        self.logger.error(f"All {max_retries} retries exhausted for Anthropic API call")
        raise last_error

    # Updated system prompt - reorganized for cohesion
    _NEWS_GENERATION_SYSTEM_PROMPT = """You are creating a news summary for a Discord community called Banodoco, focused on AI art and generative tools.

=== OUTPUT FORMAT ===
Respond with ONLY a JSON array (no introduction, explanation, or markdown formatting).
If there are no significant news items, respond with exactly "[NO SIGNIFICANT NEWS]".

JSON Structure:
[
 {
   "title": "BFL ship new Controlnets for FluxText",
   "mainText": "A new ComfyUI analytics node has been developed to track and analyze data pipeline components, including inputs, outputs, and embeddings. This enhancement aims to provide more controllable prompting capabilities:",
   "mainMediaMessageId": "4532454353425342", # Single main media message ID, or null if none
   "message_id": "4532454353425342", # Primary message for the topic
   "channel_id": "1138865343314530324",
   "subTopics": [
     {
       "text": "Here's another example of **Kijai** using it in combination with **Redux** - **Kijai** noted that it worked better than the previous version:",
       "subTopicMediaMessageIds": ["4532454353425343"], # List of message IDs with media
       "message_id": "4532454353425343",
       "channel_id": "1138865343314530324"
     }
   ]
 },
 {
   "title": "Banodocians Experiment with Animatediff Stylization",
   "mainText": "Several Banodocians have been exploring new stylization techniques with Animatediff, sharing impressive results:",
   "mainMediaMessageId": null,
   "message_id": "510987654321098765",
   "channel_id": "1221869948469776516",
   "subTopics": [
     {
       "text": "**UserA** shared a workflow combining ControlNet and custom prompts:",
       "subTopicMediaMessageIds": ["511111111111111111"],
       "message_id": "511111111111111111",
       "channel_id": "1221869948469776516"
     },
     {
       "text": "**UserB** demonstrated a different approach focusing on temporal consistency, showing before and after examples:",
       "subTopicMediaMessageIds": ["987696564536212", "34254532453454543"],
       "message_id": "522222222222222222",
       "channel_id": "1221869948469776516"
     },
     {
        "text": "**UserC** found success using a specific LoRA model:",
        "subTopicMediaMessageIds": [],
        "message_id": "533333333333333333",
        "channel_id": "1221869948469776516"
     }
   ]
 }
]

=== WHAT TO COVER ===
Prioritize these types of content (in rough order of importance):
1. Original creations by community members (custom nodes, workflows, tools, LoRAs, scripts) - Banodocian contributions are especially newsworthy
2. Notable achievements, demonstrations, or impressive work shared by members
3. Content with high engagement (many reactions/comments) - this signals community interest
4. New features, tools, or announcements people are excited about
5. Shared workflows (often JSON files) with examples of them in action
6. AI art, AI art-related tools, and open source projects
7. Negative news is okay but frame constructively

=== HOW TO WRITE ===
Evidence & Attribution:
- Do NOT jump to conclusions unsupported by evidence in the messages
- Only report what is explicitly stated or clearly demonstrated
- If unclear or ambiguous, skip it or note the uncertainty
- If messages contain external links (GitHub, blogs, announcements), report what is stated in the messages
- Distinguish between facts, opinions, and speculation
- Always credit creators with bold usernames: "**username**"
- For subjective opinions, attribute them: "**Draken** felt..."

Tone:
- Don't be hyperbolic or overly enthusiastic
- Refer to community members collectively as "Banodocians"

=== TECHNICAL REQUIREMENTS ===
Required fields for each news item:
- title, mainText, message_id, channel_id, subTopics (array)
- mainMediaMessageId: single string ID if ONE primary media, otherwise null

Required fields for each subtopic:
- text, message_id, channel_id
- subTopicMediaMessageIds: list of message IDs (can be empty [])

Media & Links:
- Every topic and subtopic MUST have message_id and channel_id for linking back
- AGGRESSIVELY search for related media - find message IDs where images/videos were posted
- Prioritize messages with reactions or direct replies when selecting media references
- Don't bias toward just the first messages about a topic
- Include interesting follow-up discussions or examples as subtopics
- End text with a colon if it directly precedes referenced media

Grouping Media for Display:
- Images in the SAME subTopicMediaMessageIds array get SENT TOGETHER as one Discord message
- GROUP TOGETHER when:
  * Many variants of the same thing without much distinctness between them
  * User posted them all at once (same message or rapid succession)
  * They're essentially "more of the same" - iterations, variations, batch outputs
- KEEP SEPARATE (different arrays/subtopics) when:
  * Each image has individual merit and deserves its own attention
  * User posted them one-by-one with different explanations for each
  * Small number of images that each show something distinct
- Basically: if they'd feel redundant shown separately, batch them. If each one tells its own story, keep them separate.

Formatting:
- Must be valid JSON in exactly the format shown above
- Don't repeat items or leave empty fields (except optional media fields)
- No markdown formatting, introduction text, or explanation - ONLY the JSON array or "[NO SIGNIFICANT NEWS]" """

    # Added system prompt from user provided code
    _SHORT_SUMMARY_SYSTEM_PROMPT = """Create exactly 3 bullet points summarizing key developments. STRICT format requirements:
1. The FIRST LINE MUST BE EXACTLY: 📨 __{message_count} messages sent__
2. Then three bullet points that:
   - Start with • (bullet character)
   - Give a short summary of one of the main topics from the full summary - prioritise topics that are related to the channel and are likely to be useful to others.
   - Bold the most important finding/result/insight using **
   - Keep each to a single line
3. DO NOT MODIFY THE MESSAGE COUNT OR FORMAT IN ANY WAY

Required format:
"📨 __{message_count} messages sent__
• [Main topic 1]
• [Main topic 2]
• [Main topic 3]"
DO NOT CHANGE THE MESSAGE COUNT LINE. IT MUST BE EXACTLY AS SHOWN ABOVE. DO NOT INCLUDE ANYTHING ELSE IN THE MESSAGE OTHER THAN THE ABOVE."""

    def format_messages_for_user_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Formats the list of message dictionaries into a string for the user prompt."""
        message_string = "Here are the messages to analyze:\n\n"
        for msg in messages:
            message_string += f"=== Message from {msg['author_name']} ===\n"
            message_string += f"Time: {msg['created_at']}\n"
            message_string += f"Content: {msg['content']}\n"
            if msg.get('reaction_count'): # Use .get for safety
                message_string += f"Reactions: {msg['reaction_count']}\n"
            if msg.get('attachments'):
                message_string += "Attachments:\n"
                try:
                    attachments_list = json.loads(msg['attachments']) if isinstance(msg['attachments'], str) else msg['attachments']
                    if isinstance(attachments_list, list):
                        for attach in attachments_list:
                            if isinstance(attach, dict):
                                filename = attach.get('filename', '')
                                message_string += f"- {filename}\n"
                            else:
                                message_string += f"- {attach}\n"
                    else:
                        message_string += f"- (Could not parse attachments: {msg['attachments']})\n"
                except Exception:
                     message_string += f"- (Could not parse attachments: {msg['attachments']})\n"
                     
            message_string += f"Message ID: {msg['message_id']}\n"
            message_string += f"Channel ID: {msg['channel_id']}\n"
            message_string += "\n"
        
        message_string += "\nRemember: Respond with ONLY the JSON array or '[NO SIGNIFICANT NEWS]'. NO other text."
        return message_string

    async def generate_news_summary(self, messages: List[Dict[str, Any]]) -> str:
        """
        Generate a news summary from a given list of messages
        by sending them to the LLM dispatcher in chunks if needed.
        """
        self.logger.info(f"Starting generate_news_summary with {len(messages) if messages else 0} messages")
        
        if not messages:
            self.logger.warning("No messages to analyze")
            return "[NO MESSAGES TO ANALYZE]"

        chunk_size = 1000 # Consider adjusting based on typical token counts
        chunk_summaries = []
        previous_summary_json = None

        self.logger.info(f"Processing {len(messages)} messages in chunks of {chunk_size}")

        for i in range(0, len(messages), chunk_size):
            chunk = messages[i:i + chunk_size]
            chunk_num = i//chunk_size + 1
            total_chunks = (len(messages) + chunk_size - 1)//chunk_size
            self.logger.info(f"Summarizing chunk {chunk_num} of {total_chunks} ({len(chunk)} messages)")
            
            # Format the current chunk data for the user prompt
            self.logger.info(f"Formatting messages for user prompt...")
            user_prompt_content = self.format_messages_for_user_prompt(chunk)
            self.logger.info(f"User prompt formatted. Length: {len(user_prompt_content)} chars")
            
            # Prepend context from previous chunks if available
            if previous_summary_json:
                user_prompt_content = f"""Previous summary chunk(s) contained these items (JSON format):
{previous_summary_json}

DO NOT duplicate or repeat any of the topics, ideas, or media from the JSON above.
Only include NEW and DIFFERENT topics from the messages below.
If all significant topics have already been covered, respond with "[NO SIGNIFICANT NEWS]".

{user_prompt_content}"""
            
            try:
                # Add delay between chunks to avoid rate limits (30k tokens/min limit)
                if chunk_num > 1:
                    self.logger.info(f"Waiting 30s before processing chunk {chunk_num} to respect rate limits...")
                    await asyncio.sleep(30)
                
                # Call Anthropic API with Claude Sonnet 4.5
                self.logger.info(f"Calling LLM for chunk {chunk_num}/{total_chunks}...")
                text = await self._call_anthropic(
                    system_prompt=self._NEWS_GENERATION_SYSTEM_PROMPT,
                    user_content=user_prompt_content,
                    max_tokens=16000
                )
                
                self.logger.info(f"LLM response received for chunk {chunk_num}. Length: {len(text) if text else 0} chars")
                self.logger.debug(f"LLM response for chunk summary: {text}") 
                
                # Basic validation of response before adding
                if text and isinstance(text, str) and text.strip() not in ["[NOTHING OF NOTE]", "[NO SIGNIFICANT NEWS]", "[NO MESSAGES TO ANALYZE]"]:
                    # Attempt to parse to ensure it's likely valid JSON before appending
                    try:
                        # Strip markdown code blocks if present
                        clean_text = text.strip()
                        if clean_text.startswith('```json'):
                            clean_text = clean_text[7:]  # Remove ```json
                        if clean_text.startswith('```'):
                            clean_text = clean_text[3:]  # Remove ```
                        if clean_text.endswith('```'):
                            clean_text = clean_text[:-3]  # Remove trailing ```
                        clean_text = clean_text.strip()
                        
                        # Check if it starts with '[' - basic JSON array check
                        if clean_text.startswith('['):
                             json.loads(clean_text) # Try parsing
                             chunk_summaries.append(clean_text) # Add the valid JSON string
                             previous_summary_json = clean_text # Update context for next chunk
                             self.logger.info(f"Successfully processed chunk {i//chunk_size + 1}")
                        else:
                             self.logger.warning(f"LLM response for chunk {i//chunk_size + 1} was not a JSON array: {text[:100]}...")
                    except json.JSONDecodeError:
                         self.logger.warning(f"LLM response for chunk {i//chunk_size + 1} was not valid JSON: {text[:100]}...")
                else:
                     self.logger.info(f"Chunk {i//chunk_size + 1} resulted in no significant news or invalid response.")

            except Exception as e:
                # Catch errors from the dispatcher
                self.logger.error(f"Error during generate_news_summary LLM call for chunk {i//chunk_size + 1}: {e}", exc_info=True)
                # Decide if we should continue to next chunk or stop?
                # For now, just log and continue, but might result in incomplete summary.

        if not chunk_summaries:
            self.logger.info("No valid chunk summaries generated.")
            return "[NO SIGNIFICANT NEWS]"
        
        # If only one chunk summary (already validated as JSON string)
        if len(chunk_summaries) == 1:
            self.logger.info("Returning single chunk summary.")
            return chunk_summaries[0]

        # If multiple chunk summaries, combine them
        self.logger.info(f"Combining {len(chunk_summaries)} chunk summaries.")
        return await self.combine_channel_summaries(chunk_summaries)

    def format_news_for_discord(self, news_items_json: str) -> List[Dict[str, str]]:
        """
        Convert the JSON string from Claude into a list of dictionaries
        each containing a 'content' field that can be posted to Discord.
        """
        if news_items_json in ["[NO SIGNIFICANT NEWS]", "[NO MESSAGES TO ANALYZE]"]:
            return [{"content": news_items_json}]

        # Attempt to parse JSON
        try:
            idx = news_items_json.find('[')
            if idx == -1:
                return [{"content": news_items_json}]

            parsed_str = news_items_json[idx:]
            items = json.loads(parsed_str)
        except json.JSONDecodeError:
            return [{"content": news_items_json}]

        messages_to_send = []
        for item in items:
            main_part = []
            main_part.append(f"## {item.get('title','No Title')}\n")
            # mainText + messageLink
            message_id = int(item['message_id'])
            channel_id = int(item['channel_id'])
            jump_url = f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"
            main_part.append(f"{item.get('mainText', '')} {jump_url}")

            messages_to_send.append({"content": "\n".join(main_part)})

            # mainMediaMessageId
            if item.get("mainMediaMessageId") and item["mainMediaMessageId"] not in [None, "null", "unknown", ""]:
                # Return dict with type, message_id, and channel_id
                messages_to_send.append({
                    "type": "media_reference",
                    "message_id": str(item["mainMediaMessageId"]), # Ensure it's a string
                    "channel_id": str(item["channel_id"]) # Associated channel_id
                })

            # subTopics
            subs = item.get("subTopics", [])
            if subs:
                for sub in subs:
                    text = sub.get("text", "")
                    sub_msg = []
                    # Generate jump URL for subtopic
                    if sub.get('message_id') and sub.get('channel_id'):
                        message_id = int(sub['message_id'])
                        channel_id = int(sub['channel_id'])
                        jump_url = f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"
                        sub_msg.append(f"• {text} {jump_url}")
                    else:
                        sub_msg.append(f"• {text}")

                    messages_to_send.append({"content": "\n".join(sub_msg)})

                    # subTopicMediaMessageIds (plural, list) - keep grouped for batch posting
                    media_ids = sub.get("subTopicMediaMessageIds")
                    if media_ids and isinstance(media_ids, list):
                        # Filter out invalid IDs
                        valid_ids = [
                            str(msg_id).strip() for msg_id in media_ids 
                            if msg_id and str(msg_id).strip() not in [None, "null", "unknown", ""]
                        ]
                        if valid_ids:
                            # Keep grouped media together for batch posting
                            messages_to_send.append({
                                "type": "media_reference_group",
                                "message_ids": valid_ids,
                                "channel_id": str(sub["channel_id"])
                            })

        return messages_to_send

    async def combine_channel_summaries(self, summaries: List[str]) -> str:
        """
        Combine multiple summary JSON strings into a single filtered summary
        by asking the LLM dispatcher which items are the most interesting.
        Assumes input `summaries` are strings of valid JSON arrays.
        """
        if not summaries:
            return "[NO SIGNIFICANT NEWS]"

        # Define System Prompt
        system_prompt = """You are analyzing multiple JSON summaries.
Each summary is in the same format: an array of objects with fields:
  title, mainText, mainMediaMessageId (optional), message_id, channel_id,
  subTopics (which is an array of objects with text, subTopicMediaMessageIds (optional), message_id, channel_id).
We want to combine them into a single JSON array that contains the top 3-5 most interesting items overall.
You MUST keep each chosen item in the exact same structure (all fields) as it appeared in the original input. Retain the original message_id and channel_id values.

IMPORTANT: Do NOT jump to conclusions that aren't supported by evidence. Only include items that are clearly substantiated in the original summaries.

If no interesting items, respond with "[NO SIGNIFICANT NEWS]".
Otherwise, respond with ONLY a JSON array. No extra text.

Return just the final JSON array with the top items (or '[NO SIGNIFICANT NEWS]')."""
        
        # Prepare user prompt content
        user_prompt_content = "Here are the input summaries (each is a JSON array string):\n\n"
        for i, s in enumerate(summaries):
            user_prompt_content += f"--- Summary {i+1} ---\n{s}\n\n"

        try:
            # Call Anthropic API with Claude Sonnet 4.5
            text = await self._call_anthropic(
                system_prompt=system_prompt,
                user_content=user_prompt_content,
                max_tokens=16000
            )
            self.logger.debug(f"LLM response for combined summary: {text}") 
            
            # Basic validation
            if text and isinstance(text, str) and text.strip() == "[NO SIGNIFICANT NEWS]":
                return "[NO SIGNIFICANT NEWS]"
            elif text and isinstance(text, str):
                # Strip markdown code blocks if present
                clean_text = text.strip()
                if clean_text.startswith('```json'):
                    clean_text = clean_text[7:]  # Remove ```json
                if clean_text.startswith('```'):
                    clean_text = clean_text[3:]  # Remove ```
                if clean_text.endswith('```'):
                    clean_text = clean_text[:-3]  # Remove trailing ```
                clean_text = clean_text.strip()
                
                if clean_text.startswith('['):
                    # Try parsing to be sure
                    try:
                        json.loads(clean_text)
                        return clean_text # Return valid JSON string
                    except json.JSONDecodeError:
                         self.logger.warning(f"Combined summary response looked like JSON but failed parsing: {text[:100]}...")
                         return "[ERROR PARSING COMBINED SUMMARY]" # Indicate error
                else:
                    self.logger.warning(f"Unexpected response format for combined summary: {text[:100]}...")
                    return "[ERROR COMBINING SUMMARIES]" # Indicate error
            else:
                self.logger.warning(f"Unexpected response format for combined summary: {text[:100] if text else 'None'}...")
                return "[ERROR COMBINING SUMMARIES]" # Indicate error
                
        except Exception as e:
            self.logger.error(f"Error combining summaries via LLM dispatcher: {e}", exc_info=True)
            return "[ERROR COMBINING SUMMARIES]" # Indicate error

    async def find_and_format_top_media(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Finds messages with media and > 5 reactions, and formats them for posting.
        Each media item is formatted into a separate dictionary for individual posting.
        Ignores channels with 'nsfw' in the name.
        """
        if not self.top_gen_channel_id:
            self.logger.warning("TOP_GEN_CHANNEL or DEV_TOP_GEN_CHANNEL not set in .env, skipping top media posts.")
            return []

        top_media_to_post = []
        for msg in messages:
            # Note: This requires 'channel_name' to be present in the message dictionary.
            channel_name = msg.get('channel_name', '').lower()
            if 'nsfw' in channel_name:
                continue

            # Check for attachments and reaction count
            if msg.get('attachments') and msg.get('reaction_count', 0) > 5:
                try:
                    attachments = json.loads(msg['attachments']) if isinstance(msg['attachments'], str) else msg['attachments']
                    if not isinstance(attachments, list) or not attachments:
                        continue
                    
                    comment_text = msg.get('content', '').strip()

                    for attachment in attachments:
                        if not isinstance(attachment, dict) or 'url' not in attachment:
                            continue

                        # Each attachment gets its own post
                        post_data = {
                            "type": "top_media_post",
                            "channel_id": self.top_gen_channel_id,
                            "content": f"Creator: **{msg['author_name']}**\n"
                                       f"Comments: {comment_text}\n"
                                       f"Reactions: {msg['reaction_count']}\n"
                                       f"File:",
                            "file_url": attachment['url'],
                            "filename": attachment.get('filename', 'untitled')
                        }
                        top_media_to_post.append(post_data)

                except json.JSONDecodeError:
                    self.logger.warning(f"Could not parse attachments for message {msg['message_id']}: {msg['attachments']}")
                except Exception as e:
                    self.logger.error(f"Error processing message {msg['message_id']} for top media: {e}", exc_info=True)

        return top_media_to_post

    async def generate_short_summary(self, full_summary: str, message_count: int) -> str:
        """
        Get a short summary using the LLM dispatcher with proper async handling.
        """
        # Use the new system prompt defined above, formatting the message count in
        system_prompt_formatted = self._SHORT_SUMMARY_SYSTEM_PROMPT.format(message_count=message_count)

        try:
            # Call Anthropic API with Claude Sonnet 4.5
            text = await self._call_anthropic(
                system_prompt=system_prompt_formatted,
                user_content=f"Full summary to work from:\n{full_summary}",
                max_tokens=1024  # Lower max tokens for short summary
            )
            self.logger.debug(f"LLM response for short summary: {text}")
            
            # Basic validation of the response format
            if text and isinstance(text, str):
                lines = text.strip().split('\n')
                # Check for bullet character (•) at the start of content lines
                content_lines = [l for l in lines[1:] if l.strip()]  # Skip empty lines
                if len(lines) >= 1 and lines[0].strip() == f"📨 __{message_count} messages sent__" and all(l.strip().startswith('•') for l in content_lines):
                     # Format is correct
                     return text.strip()
                else:
                    self.logger.warning(f"Short summary response did not match expected format: {text[:100]}...")
                    # Return the response anyway - it's probably close enough
                    return text.strip()
            else:
                 # Handle cases where text is None or not a string (though dispatcher should prevent None)
                 self.logger.error("LLM dispatcher returned invalid type or empty response for short summary.")
                 return f"📨 __{message_count} messages sent__\n• Failed to generate summary (Invalid LLM Response)."

        except Exception as e:
            self.logger.error(f"Error generating short summary via LLM dispatcher: {e}", exc_info=True)
            # Return a formatted error message
            return f"📨 __{message_count} messages sent__\n• Error generating summary due to API issue."


if __name__ == "__main__":
    def main():
        pass
    main()
