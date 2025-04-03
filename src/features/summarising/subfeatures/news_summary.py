import os
import sys
import json
import logging
import traceback
from typing import List, Dict, Any, Optional
import asyncio

from dotenv import load_dotenv

# Import the shared client
from src.common.claude_client import ClaudeClient

# We removed direct Discord/bot usage here since SUMMARIZER handles posting logic now.
# This class now focuses on:
#  - queries to Claude (generate_news_summary, combine_channel_summaries, etc.)
#  - chunking/formatting the prompt & returned JSON.

class NewsSummarizer:
    def __init__(self, claude_client: ClaudeClient, logger: logging.Logger, dev_mode=False):
        self.logger = logger
        self.logger.info("Initializing NewsSummarizer...")

        load_dotenv()
        self.dev_mode = dev_mode

        if self.dev_mode:
            self.guild_id = int(os.getenv('DEV_GUILD_ID'))
        else:
            self.guild_id = int(os.getenv('GUILD_ID'))

        # Store the passed Claude client instead of creating a new one
        self.claude_client = claude_client
        self.logger.info("NewsSummarizer initialized with shared Claude client.")

    def format_messages_for_claude(self, messages):
        """Format messages for Claude analysis."""
        conversation = """You MUST respond with ONLY a JSON array containing news items. NO introduction text, NO explanation, NO markdown formatting.

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
5. Call out notable achievements or demonstrations or work that people did
6. Don't avoid negative news but try to frame it in a positive way

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
11. If something seems to be a subjective opinion but still noteworthy, mention it as such: "**Draken** felt...", etc.

Here are the messages to analyze:

"""

        for msg in messages:
            conversation += f"=== Message from {msg['author_name']} ===\n"
            conversation += f"Time: {msg['created_at']}\n"
            conversation += f"Content: {msg['content']}\n"
            if msg['reaction_count']:
                conversation += f"Reactions: {msg['reaction_count']}\n"
            if msg['attachments']:
                conversation += "Attachments:\n"
                for attach in msg['attachments']:
                    if isinstance(attach, dict):
                        # url = attach.get('url', '') # Don't send the URL
                        filename = attach.get('filename', '')
                        # Only send the filename to avoid confusing the LLM with attachment IDs
                        conversation += f"- {filename}\n"
                    else:
                        # Fallback if not a dict
                        conversation += f"- {attach}\n"
            conversation += f"Message ID: {msg['message_id']}\n"
            conversation += f"Channel ID: {msg['channel_id']}\n"
            conversation += "\n"

        conversation += "\nRemember: Respond with ONLY the JSON array or '[NO SIGNIFICANT NEWS]'. NO other text."
        return conversation

    async def generate_news_summary(self, messages: List[Dict[str, Any]]) -> str:
        """
        Generate a news summary from a given list of messages
        by sending them to Claude in chunks if needed.
        """
        if not messages:
            self.logger.warning("No messages to analyze")
            return "[NO MESSAGES TO ANALYZE]"

        chunk_size = 1000
        chunk_summaries = []
        previous_summary = None

        for i in range(0, len(messages), chunk_size):
            chunk = messages[i:i + chunk_size]
            self.logger.info(f"Summarizing chunk {i//chunk_size + 1} of {(len(messages) + chunk_size - 1)//chunk_size}")
            
            prompt = self.format_messages_for_claude(chunk)
            if previous_summary:
                prompt = f"""Previous summary chunk(s) contained these items:
{previous_summary}

DO NOT duplicate or repeat any of the topics, ideas, or media from above.
Only include NEW and DIFFERENT topics from the messages below.
If all significant topics have already been covered, respond with "[NO SIGNIFICANT NEWS]".

{prompt}"""
            
            try:
                text = await self.claude_client.generate_text(
                    content=prompt,
                    model="claude-3-5-sonnet-latest",
                    max_tokens=8192
                )
                self.logger.debug(f"Claude response for chunk summary: {text}") # Log raw response
                if text and text not in ["[NOTHING OF NOTE]", "[NO SIGNIFICANT NEWS]", "[NO MESSAGES TO ANALYZE]"]:
                    chunk_summaries.append(text)
                    if previous_summary:
                        previous_summary = previous_summary + "\n\n" + text
                    else:
                        previous_summary = text
            except Exception as e:
                self.logger.error(f"Error during generate_news_summary call via ClaudeClient: {e}")
                self.logger.debug(traceback.format_exc())

        if not chunk_summaries:
            return "[NO SIGNIFICANT NEWS]"
        
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        # If multiple chunk summaries, combine them
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
        by asking Claude which items are the most interesting.
        """
        if not summaries:
            return "[NO SIGNIFICANT NEWS]"

        # Prepare a prompt that merges them
        prompt = """You are analyzing multiple JSON summaries.
Each summary is in the same format: an array of objects with fields:
  title, mainText, mainMediaMessageId (optional), message_id, channel_id,
  subTopics (which is an array of objects with text, subTopicMediaMessageIds (optional), message_id, channel_id).
We want to combine them into a single JSON array that contains the top 3-5 most interesting items overall.
You MUST keep each chosen item in the exact same structure (all fields) as it appeared in the original input. Retain the original message_id and channel_id values.

If no interesting items, respond with "[NO SIGNIFICANT NEWS]".
Otherwise, respond with ONLY a JSON array. No extra text.

Here are the input summaries:
"""

        for s in summaries:
            prompt += f"\n{s}\n"

        prompt += "\nReturn just the final JSON array with the top items (or '[NO SIGNIFICANT NEWS]')."

        try:
            text = await self.claude_client.generate_text(
                content=prompt,
                model="claude-3-5-sonnet-latest",
                max_tokens=8192
            )
            self.logger.debug(f"Claude response for combined summary: {text}") # Log raw response
            return text if text else "[NO SIGNIFICANT NEWS]"
        except Exception as e:
            self.logger.error(f"Error combining summaries via ClaudeClient: {e}")
            self.logger.debug(traceback.format_exc())
            return "[NO SIGNIFICANT NEWS]"

    async def generate_short_summary(self, full_summary: str, message_count: int) -> str:
        """
        Get a short summary using Claude with proper async handling.
        """
        conversation = f"""Create exactly 3 bullet points summarizing key developments. STRICT format requirements:
1. The FIRST LINE MUST BE EXACTLY: 📨 __{message_count} messages sent__
2. Then three bullet points that:
   - Start with -
   - Give a short summary of one of the main topics from the full summary - priotise topics that are related to the channel and are likely to be useful to others.
   - Bold the most important finding/result/insight using **
   - Keep each to a single line
4. DO NOT MODIFY THE MESSAGE COUNT OR FORMAT IN ANY WAY

Required format:
"📨 __{message_count} messages sent__
• [Main topic 1] 
• [Main topic 2]
• [Main topic 3]"
DO NOT CHANGE THE MESSAGE COUNT LINE. IT MUST BE EXACTLY AS SHOWN ABOVE. DO NOT ADD INCLUDE ELSE IN THE MESSAGE OTHER THAN THE ABOVE.

Full summary to work from:
{full_summary}"""

        text = await self.claude_client.generate_text(
            content=conversation,
            model="claude-3-5-haiku-latest",
            max_tokens=8192,
            max_retries=3
        )

        self.logger.debug(f"Claude response for short summary: {text}") # Log raw response

        if text:
            return text
        else:
            return f"📨 __{message_count} messages sent__\n• Unable to generate short summary due to API error after retries."


if __name__ == "__main__":
    def main():
        pass
    main()
