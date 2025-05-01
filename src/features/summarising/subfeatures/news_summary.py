import os
import sys
import json
import logging
import traceback
from typing import List, Dict, Any, Optional
import asyncio

from dotenv import load_dotenv

# Add new dispatcher import
from src.common.llm import get_llm_response 

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

        if self.dev_mode:
            self.guild_id = int(os.getenv('DEV_GUILD_ID'))
        else:
            self.guild_id = int(os.getenv('GUILD_ID'))

        self.logger.info("NewsSummarizer initialized.")

    # Updated system prompt from user provided code
    _NEWS_GENERATION_SYSTEM_PROMPT = """You MUST respond with ONLY a JSON array containing news items. NO introduction text, NO explanation, NO markdown formatting.

If there are no significant news items, respond with exactly "[NO SIGNIFICANT NEWS]".
Otherwise, respond with ONLY a JSON array in this exact format:

[
 {
   "title": "BFL ship new Controlnets for FluxText",
   "mainText": "A new ComfyUI analytics node has been developed to track and analyze data pipeline components, including inputs, outputs, and embeddings. This enhancement aims to provide more controllable prompting capabilities:",
   "mainMediaMessageId": "4532454353425342", # ID of the message containing the single main media
   "message_id": "4532454353425342", # ID of the primary message for the topic
   "channel_id": "1138865343314530324",
   "subTopics": [
     {
       "text": "Here's another example of **Kijai** using it in combination with **Redux** - **Kijai** noted that it worked better than the previous version:",
       "subTopicMediaMessageIds": ["4532454353425343"], # List of message IDs containing media for this subtopic
       "message_id": "4532454353425343", # ID of the primary message for the subtopic
       "channel_id": "1138865343314530324"
     }
   ]
 },
 {
   "title": "Banodocians Experiment with Animatediff Stylization",
   "mainText": "Several Banodocians have been exploring new stylization techniques with Animatediff, sharing impressive results:",
   "mainMediaMessageId": null, # No single main media, examples are in subtopics
   "message_id": "510987654321098765", # ID of a message introducing the topic
   "channel_id": "1221869948469776516",
   "subTopics": [
     {
       "text": "**UserA** shared a workflow combining ControlNet and custom prompts:",
       "subTopicMediaMessageIds": ["511111111111111111"], # ID of UserA's message with the video
       "message_id": "511111111111111111",
       "channel_id": "1221869948469776516"
     },
     {
       "text": "**UserB** demonstrated a different approach focusing on temporal consistency, showing before and after examples:",
       "subTopicMediaMessageIds": ["987696564536212", "34254532453454543"], # Multiple message IDs for before/after media
       "message_id": "522222222222222222", # ID of UserB's main explanation message
       "channel_id": "1221869948469776516"
     },
     {
        "text": "**UserC** found success using a specific LoRA model:",
        "subTopicMediaMessageIds": [], # No direct media in this subtopic message, but part of the overall discussion
        "message_id": "533333333333333333",
        "channel_id": "1221869948469776516"
     }
   ]
 }
]

Focus on these types of content:
1. New features or tools that were announced or people are excited about
2. Demos or images that got a lot of attention (especially messages with many reactions) - especially if there are multple examples of people using it that people remarked upon or reacted to.
3. Focus on the things that people seem most excited about or commented/reacted to on a lot
4. Focus on AI art and AI art-related tools and open source tools and projects
5. Workflows (often json files) that people shared - include examples of them in action if possible
6. Call out notable achievements or demonstrations or work that people did
7. Don't avoid negative news but try to frame it in a positive way

IMPORTANT REQUIREMENTS FOR MEDIA AND LINKS:
1. Each topic MUST have `message_id` and `channel_id` for linking back to the original message introducing the topic.
2. Include `mainMediaMessageId` (as a single string ID) ONLY if there is ONE primary piece of media for the main topic. Otherwise, set it to null.
3. For subtopics, use `subTopicMediaMessageIds` (plural) which MUST be a LIST of strings (message IDs). Include the IDs of ALL relevant messages containing media for that specific subtopic.
4. AGGRESSIVELY search for related media - if a subtopic discusses specific images/videos, identify the `message_id`s of the messages where that media was posted and put them in the `subTopicMediaMessageIds` list. Prioritize messages with reactions or direct replies.
5. For each subtopic, you MUST include `message_id` and `channel_id` for the subtopic's primary message (the one containing the text or starting the sub-discussion).
6. Prioritize messages with reactions or responses when selecting which `message_id`s to reference for media.
7. Be careful not to bias towards just the first messages about a topic.
8. If a topic has interesting follow-up discussions or examples, include those as subtopics.
9. Always end descriptive text (`mainText`, `text`) with a colon if it directly precedes media referenced by a `mainMediaMessageId` or `subTopicMediaMessageIds`.

Requirements for the response:
1. Must be valid JSON in exactly the above format.
2. Each news item must have: `title`, `mainText`, `message_id`, `channel_id`, and `subTopics`. `mainMediaMessageId` is optional (can be null).
3. `subTopics` is an array of objects. Each subtopic object MUST have `text`, `message_id`, `channel_id`. `subTopicMediaMessageIds` (a list of strings) is optional (can be an empty list []).
4. Always end descriptive text (`mainText`, `text`) with a colon if it directly precedes media referenced by `mainMediaMessageId` or `subTopicMediaMessageIds`.
5. All usernames must be in bold with ** (e.g., "**username**") - ALWAYS try to give credit to the creator or state if opinions come from a specific person.
6. If there are no significant news items, respond with exactly "[NO SIGNIFICANT NEWS]".
7. Include NOTHING other than the JSON response or "[NO SIGNIFICANT NEWS]".
8. Don't repeat the same item or leave any empty fields (except optional `mainMediaMessageId` and `subTopicMediaMessageIds`).
9. When you're referring to groups of community members, refer to them as Banodocians.
10. Don't be hyperbolic or overly enthusiastic.
11. If something seems to be a subjective opinion but still noteworthy, mention it as such: "**Draken** felt...", etc."""

    # Added system prompt from user provided code
    _SHORT_SUMMARY_SYSTEM_PROMPT = """Create exactly 3 bullet points summarizing key developments. STRICT format requirements:
1. The FIRST LINE MUST BE EXACTLY: 📨 __{message_count} messages sent__
2. Then three bullet points that:
   - Start with - (hyphen, not bullet)
   - Give a short summary of one of the main topics from the full summary - priotise topics that are related to the channel and are likely to be useful to others.
   - Bold the most important finding/result/insight using **
   - Keep each to a single line
4. DO NOT MODIFY THE MESSAGE COUNT OR FORMAT IN ANY WAY

Required format:
"📨 __{message_count} messages sent__
• [Main topic 1] 
• [Main topic 2]
• [Main topic 3]"
DO NOT CHANGE THE MESSAGE COUNT LINE. IT MUST BE EXACTLY AS SHOWN ABOVE. DO NOT ADD INCLUDE ELSE IN THE MESSAGE OTHER THAN THE ABOVE."""

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
        if not messages:
            self.logger.warning("No messages to analyze")
            return "[NO MESSAGES TO ANALYZE]"

        chunk_size = 1000 # Consider adjusting based on typical token counts
        chunk_summaries = []
        previous_summary_json = None

        for i in range(0, len(messages), chunk_size):
            chunk = messages[i:i + chunk_size]
            self.logger.info(f"Summarizing chunk {i//chunk_size + 1} of {(len(messages) + chunk_size - 1)//chunk_size}")
            
            # Format the current chunk data for the user prompt
            user_prompt_content = self.format_messages_for_user_prompt(chunk)
            
            # Prepend context from previous chunks if available
            if previous_summary_json:
                user_prompt_content = f"""Previous summary chunk(s) contained these items (JSON format):
{previous_summary_json}

DO NOT duplicate or repeat any of the topics, ideas, or media from the JSON above.
Only include NEW and DIFFERENT topics from the messages below.
If all significant topics have already been covered, respond with "[NO SIGNIFICANT NEWS]".

{user_prompt_content}"""
            
            # Prepare messages for the dispatcher
            llm_messages = [{"role": "user", "content": user_prompt_content}]
            
            try:
                # Call the dispatcher - Updated model name
                text = await get_llm_response(
                    client_name="claude",
                    model="claude-3-5-sonnet-latest", 
                    system_prompt=self._NEWS_GENERATION_SYSTEM_PROMPT,
                    messages=llm_messages,
                    max_tokens=8192 
                )
                
                self.logger.debug(f"LLM response for chunk summary: {text}") 
                
                # Basic validation of response before adding
                if text and isinstance(text, str) and text.strip() not in ["[NOTHING OF NOTE]", "[NO SIGNIFICANT NEWS]", "[NO MESSAGES TO ANALYZE]"]:
                    # Attempt to parse to ensure it's likely valid JSON before appending
                    try:
                        # Check if it starts with '[' - basic JSON array check
                        if text.strip().startswith('['):
                             json.loads(text.strip()) # Try parsing
                             chunk_summaries.append(text.strip()) # Add the valid JSON string
                             previous_summary_json = text.strip() # Update context for next chunk
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

                    # subTopicMediaMessageIds (plural, list)
                    media_ids = sub.get("subTopicMediaMessageIds")
                    if media_ids and isinstance(media_ids, list):
                        for msg_id in media_ids:
                            if msg_id and str(msg_id).strip() not in [None, "null", "unknown", ""]:
                                # Return dict with type, message_id, and channel_id for EACH ID
                                messages_to_send.append({
                                    "type": "media_reference",
                                    "message_id": str(msg_id).strip(), # Ensure it's a string and stripped
                                    "channel_id": str(sub["channel_id"]) # Associated channel_id from the subtopic
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

If no interesting items, respond with "[NO SIGNIFICANT NEWS]".
Otherwise, respond with ONLY a JSON array. No extra text.

Return just the final JSON array with the top items (or '[NO SIGNIFICANT NEWS]')."""
        
        # Prepare user prompt content
        user_prompt_content = "Here are the input summaries (each is a JSON array string):\n\n"
        for i, s in enumerate(summaries):
            user_prompt_content += f"--- Summary {i+1} ---\n{s}\n\n"
        
        # Prepare messages for dispatcher
        messages = [{"role": "user", "content": user_prompt_content}]

        try:
            # Call the dispatcher - Updated model name
            text = await get_llm_response(
                client_name="claude",
                model="claude-3-5-sonnet-latest", 
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=8192 
            )
            self.logger.debug(f"LLM response for combined summary: {text}") 
            
            # Basic validation
            if text and isinstance(text, str) and text.strip() == "[NO SIGNIFICANT NEWS]":
                return "[NO SIGNIFICANT NEWS]"
            elif text and isinstance(text, str) and text.strip().startswith('['):
                # Try parsing to be sure
                try:
                    json.loads(text.strip())
                    return text.strip() # Return valid JSON string
                except json.JSONDecodeError:
                     self.logger.warning(f"Combined summary response looked like JSON but failed parsing: {text[:100]}...")
                     return "[ERROR PARSING COMBINED SUMMARY]" # Indicate error
            else:
                self.logger.warning(f"Unexpected response format for combined summary: {text[:100]}...")
                return "[ERROR COMBINING SUMMARIES]" # Indicate error
                
        except Exception as e:
            self.logger.error(f"Error combining summaries via LLM dispatcher: {e}", exc_info=True)
            return "[ERROR COMBINING SUMMARIES]" # Indicate error

    async def generate_short_summary(self, full_summary: str, message_count: int) -> str:
        """
        Get a short summary using the LLM dispatcher with proper async handling.
        """
        # Use the new system prompt defined above, formatting the message count in
        system_prompt_formatted = self._SHORT_SUMMARY_SYSTEM_PROMPT.format(message_count=message_count)
        # Prepare user message content (the full summary to work from)
        messages = [{"role": "user", "content": f"Full summary to work from:\n{full_summary}"}]

        try:
            # Call the LLM dispatcher - Use a cheaper/faster model - Updated model name
            text = await get_llm_response(
                client_name="claude",
                model="claude-3-5-haiku-latest", 
                system_prompt=system_prompt_formatted,
                messages=messages,
                max_tokens=512 # Keep lower max tokens for short summary
            )
            self.logger.debug(f"LLM response for short summary: {text}")
            
            # Basic validation of the response format
            if text and isinstance(text, str):
                lines = text.strip().split('\n')
                # Adjusted check for hyphen instead of bullet
                if len(lines) >= 1 and lines[0].strip() == f"📨 __{message_count} messages sent__" and all(l.strip().startswith('-') for l in lines[1:]):
                     # Further checks could be added (e.g., number of bullet points)
                     return text.strip()
                else:
                    self.logger.warning(f"Short summary response did not match expected format (Message Count Line or Hyphen Mismatch): {text[:100]}...")
                    # Fallback or attempt to fix? For now, return error indication. Might need manual fix later.
                    # return f"📨 __{message_count} messages sent__\n• LLM response format error." # Option 1: Provide default error bullets
                    return text.strip() # Option 2: Return the potentially incorrect text and hope it's close
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
