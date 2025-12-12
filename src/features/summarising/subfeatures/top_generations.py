import os
import re
import json
import asyncio
import traceback
import discord
from typing import Optional, List, Dict
from datetime import datetime, timedelta

from src.common import discord_utils

class TopGenerations:
    def __init__(self, summarizer_instance):
        """
        summarizer_instance is an instance of ChannelSummarizer.
        We store it to access its bot, db_handler, logger, rate_limiter etc.
        """
        self.summarizer = summarizer_instance

    async def post_top_x_generations(
        self,
        summary_channel: discord.TextChannel,
        limit: int = 5,
        channel_id: Optional[int] = None,
        ignore_message_ids: Optional[List[int]] = None,
        also_post_to_channel_id: Optional[int] = None
    ):
        """
        (4) Send the top X gens post. 
        We'll just pick top `limit` video-type messages with >= 5 unique reactors in the last 24 hours,
        and post them in a thread.
        """
        try:
            self.summarizer.logger.info("Starting post_top_x_generations")
            yesterday = datetime.utcnow() - timedelta(hours=24)

            art_channel_id = int(os.getenv('DEV_ART_CHANNEL_ID' if self.summarizer.dev_mode else 'ART_CHANNEL_ID', 0))
            
            channel_condition = ""
            query_params = []
            
            # If dev mode, we only consider test channels – otherwise use your real channels
            if self.summarizer.dev_mode:
                # We might have "test" channels defined via env
                test_channels_str = os.getenv("TEST_DATA_CHANNEL", "")
                if not test_channels_str:
                    self.summarizer.logger.error("TEST_DATA_CHANNEL not set")
                    return
                
                test_channel_ids = [int(cid.strip()) for cid in test_channels_str.split(',') if cid.strip()]
                if not test_channel_ids:
                    self.summarizer.logger.error("No valid channel IDs found in TEST_DATA_CHANNEL")
                    return
                
                # FIXED: Apply date filtering in dev mode too!
                query_params.append(yesterday.isoformat())
                date_condition = "m.created_at > ?"
                channels_str = ','.join(str(c) for c in test_channel_ids)
                channel_condition = f" AND m.channel_id IN ({channels_str})"
            else:
                # Production: we do filter on date
                query_params.append(yesterday.isoformat())
                date_condition = "m.created_at > ?"
                
                if channel_id:
                    channel_condition = "AND m.channel_id = ?"
                    query_params.append(channel_id)
                else:
                    if self.summarizer.channels_to_monitor:
                        channels_str = ','.join(str(c) for c in self.summarizer.channels_to_monitor)
                        # Include sub-channels in the same categories
                        channel_condition = (
                            f" AND (m.channel_id IN ({channels_str}) "
                            f"     OR EXISTS (SELECT 1 FROM channels c2 WHERE c2.channel_id = m.channel_id AND c2.category_id IN ({channels_str})))"
                        )
            
            # Exclude the art channel from these top generations
            if art_channel_id != 0:
                channel_condition += f" AND m.channel_id != {art_channel_id}"

            ignore_condition = ""
            if ignore_message_ids and len(ignore_message_ids) > 0:
                ignore_ids_str = ','.join(str(mid) for mid in ignore_message_ids)
                ignore_condition = f" AND m.message_id NOT IN ({ignore_ids_str})"
            
            # Build a query that looks for attachments with .mp4/.mov/.webm, and 3+ unique reactors
            query = f"""
                WITH video_messages AS (
                    SELECT 
                        m.message_id,
                        m.channel_id,
                        m.content,
                        m.attachments,
                        m.reactors,
                        c.channel_name,
                        COALESCE(mem.server_nick, mem.global_name, mem.username) as author_name,
                        CASE 
                            WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                            ELSE json_array_length(m.reactors)
                        END as unique_reactor_count
                    FROM messages m
                    JOIN channels c ON m.channel_id = c.channel_id
                    JOIN members mem ON m.author_id = mem.member_id
                    WHERE {date_condition}
                    {channel_condition}
                    {ignore_condition}
                    AND json_valid(m.attachments)
                    AND m.attachments != '[]'
                    AND LOWER(c.channel_name) NOT LIKE '%nsfw%'
                    AND EXISTS (
                        SELECT 1
                        FROM json_each(m.attachments)
                        WHERE LOWER(json_extract(value, '$.filename')) LIKE '%.mp4'
                           OR LOWER(json_extract(value, '$.filename')) LIKE '%.mov'
                           OR LOWER(json_extract(value, '$.filename')) LIKE '%.webm'
                    )
                )
                SELECT *
                FROM video_messages
                WHERE unique_reactor_count >= 5
                ORDER BY unique_reactor_count DESC
                LIMIT {limit}
            """

            top_generations = await asyncio.to_thread(
                self.summarizer.db_handler.execute_query,
                query,
                tuple(query_params)
            )
            
            if not top_generations:
                self.summarizer.logger.info(f"No qualifying videos found - skipping top {limit} gens post.")
                return None
            
            first_gen = top_generations[0]
            # Handle both parsed list and JSON string
            attachments = first_gen['attachments']
            if isinstance(attachments, str):
                attachments = json.loads(attachments)
            
            # Find a video attachment in the first (top) generation
            video_attachment = next(
                (a for a in attachments if any(a.get('filename', '').lower().endswith(ext) 
                                               for ext in ('.mp4', '.mov', '.webm'))),
                None
            )
            if not video_attachment:
                return None
                
            desc = [
                f"## {'Top Generation' if len(top_generations) == 1 else f'Top {len(top_generations)} Generations'}"
                + (f" in #{first_gen['channel_name']}" if channel_id else "")
                + "\n",
                f"1. By **{first_gen['author_name']}**" + (f" in #{first_gen['channel_name']}" if not channel_id else "")
            ]
            
            # If there's text content, trim and un-mention
            if first_gen['content'] and first_gen['content'].strip():
                desc.append(self._replace_user_mentions(first_gen['content'][:150]))
            
            desc.append(f"🔥 {first_gen['unique_reactor_count']} unique reactions")
            desc.append(video_attachment['url'])
            # Generate jump URL dynamically
            jump_url = f"https://discord.com/channels/{self.summarizer.guild_id}/{first_gen['channel_id']}/{first_gen['message_id']}"
            desc.append(f"🔗 Original post: {jump_url}")
            msg_text = "\n".join(desc)
            
            header_message = await discord_utils.safe_send_message(
                self.summarizer.bot, 
                summary_channel, 
                self.summarizer.rate_limiter, 
                self.summarizer.logger, 
                content=msg_text
            )
            
            # If multiple top gens, create a thread to list them
            if len(top_generations) > 1 and header_message:
                thread = await self.summarizer.create_summary_thread(
                    header_message,
                    f"Top Generations - {self.summarizer._get_today_str()}",
                    is_top_generations=True
                )
                
                if not thread:
                    self.summarizer.logger.error("Failed to create thread for top generations")
                    return None
                
                # Post the rest (2..N)
                for i, row in enumerate(top_generations[1:], start=2):
                    gen = dict(row)
                    # Handle both parsed list and JSON string
                    attachments = gen['attachments']
                    if isinstance(attachments, str):
                        attachments = json.loads(attachments)
                    video_attachment = next(
                        (a for a in attachments if any(a.get('filename', '').lower().endswith(ext)
                                                       for ext in ('.mp4', '.mov', '.webm'))),
                        None
                    )
                    if not video_attachment:
                        continue
                    
                    desc = [
                        f"**{i}.** By **{gen['author_name']}**" + (f" in #{gen['channel_name']}" if not channel_id else "")
                    ]
                    
                    if gen['content'] and gen['content'].strip():
                        desc.append(self._replace_user_mentions(gen['content'][:150]))
                    
                    desc.append(f"🔥 {gen['unique_reactor_count']} unique reactions")
                    desc.append(video_attachment['url'])
                    # Generate jump URL dynamically
                    jump_url = f"https://discord.com/channels/{self.summarizer.guild_id}/{gen['channel_id']}/{gen['message_id']}"
                    desc.append(f"🔗 Original post: {jump_url}")
                    msg_text = "\n".join(desc)
                    
                    await discord_utils.safe_send_message(
                        self.summarizer.bot, 
                        thread, 
                        self.summarizer.rate_limiter, 
                        self.summarizer.logger, 
                        content=msg_text
                    )
                    await asyncio.sleep(1)
            
            # Also post to additional channel if specified (as individual messages, not thread)
            if also_post_to_channel_id:
                try:
                    additional_channel = await self.summarizer.bot.fetch_channel(also_post_to_channel_id)
                    if additional_channel:
                        self.summarizer.logger.info(f"Also posting top generations to channel {also_post_to_channel_id} as individual messages in random order")
                        
                        # Randomize the order for the additional channel
                        import random
                        randomized_generations = list(top_generations)
                        random.shuffle(randomized_generations)
                        
                        # Post ALL generations as individual messages (no thread)
                        for i, row in enumerate(randomized_generations, start=1):
                            gen = dict(row)
                            # Handle both parsed list and JSON string
                            attachments = gen['attachments']
                            if isinstance(attachments, str):
                                attachments = json.loads(attachments)
                            video_attachment = next(
                                (a for a in attachments if any(a.get('filename', '').lower().endswith(ext)
                                                               for ext in ('.mp4', '.mov', '.webm'))),
                                None
                            )
                            if not video_attachment:
                                continue
                            
                            # Format message for individual posting
                            desc = [
                                f"By **{gen['author_name']}**" + (f" in #{gen['channel_name']}" if not channel_id else "")
                            ]
                            
                            if gen['content'] and gen['content'].strip():
                                desc.append(self._replace_user_mentions(gen['content'][:150]))
                            
                            desc.append(f"🔥 {gen['unique_reactor_count']} unique reactions")
                            desc.append(video_attachment['url'])
                            # Generate jump URL dynamically
                            jump_url = f"https://discord.com/channels/{self.summarizer.guild_id}/{gen['channel_id']}/{gen['message_id']}"
                            desc.append(f"🔗 Original post: {jump_url}")
                            msg_text_individual = "\n".join(desc)
                            
                            await discord_utils.safe_send_message(
                                self.summarizer.bot, 
                                additional_channel, 
                                self.summarizer.rate_limiter, 
                                self.summarizer.logger, 
                                content=msg_text_individual
                            )
                            await asyncio.sleep(1)
                                    
                        self.summarizer.logger.info(f"Successfully posted {len(top_generations)} individual top generations to additional channel {also_post_to_channel_id}")
                    else:
                        self.summarizer.logger.error(f"Could not fetch additional channel {also_post_to_channel_id}")
                        
                except Exception as e:
                    self.summarizer.logger.error(f"Error posting to additional channel {also_post_to_channel_id}: {e}")
                    self.summarizer.logger.debug(traceback.format_exc())

            self.summarizer.logger.info("Posted top X gens successfully.")
            return top_generations[0] if top_generations else None

        except Exception as e:
            self.summarizer.logger.error(f"Error in post_top_x_generations: {e}")
            self.summarizer.logger.debug(traceback.format_exc())
            return None

    async def post_top_gens_for_channel(self, thread: discord.Thread, channel_id: int):
        """
        (5)(iv) Post the top gens from that channel that haven't yet been included,
        i.e., with over 3 reactions, in the last 24 hours.
        """
        try:
            self.summarizer.logger.info(f"Posting top gens for channel {channel_id} in thread {thread.name}")
            
            yesterday = datetime.utcnow() - timedelta(hours=24)
            
            query = """
                SELECT 
                    m.message_id,
                    m.channel_id,
                    m.content,
                    m.attachments,
                    COALESCE(mem.server_nick, mem.global_name, mem.username) as author_name,
                    CASE 
                        WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                        ELSE json_array_length(m.reactors)
                    END as unique_reactor_count
                FROM messages m
                JOIN members mem ON m.author_id = mem.member_id
                JOIN channels c ON m.channel_id = c.channel_id
                WHERE m.channel_id = ?
                AND m.created_at > ?
                AND json_valid(m.attachments)
                AND m.attachments != '[]'
                AND LOWER(c.channel_name) NOT LIKE '%nsfw%'
                AND EXISTS (
                    SELECT 1
                    FROM json_each(m.attachments)
                    WHERE LOWER(json_extract(value, '$.filename')) LIKE '%.mp4'
                       OR LOWER(json_extract(value, '$.filename')) LIKE '%.mov'
                       OR LOWER(json_extract(value, '$.filename')) LIKE '%.webm'
                )
                AND (
                    CASE 
                        WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                        ELSE json_array_length(m.reactors)
                    END
                ) >= 3
                ORDER BY unique_reactor_count DESC
                LIMIT 5
            """
            
            results = await asyncio.to_thread(
                self.summarizer.db_handler.execute_query,
                query,
                (channel_id, yesterday.isoformat())
            )
            
            if not results:
                self.summarizer.logger.info(f"No top generations found for channel {channel_id}")
                return

            await discord_utils.safe_send_message(
                self.summarizer.bot, 
                thread, 
                self.summarizer.rate_limiter, 
                self.summarizer.logger, 
                content="\n## Top Generations\n"
            )
            
            for i, row in enumerate(results, start=1):
                try:
                    # Handle both parsed list and JSON string
                    attachments = row['attachments']
                    if isinstance(attachments, str):
                        attachments = json.loads(attachments)
                    video_attachment = next(
                        (a for a in attachments if any(a.get('filename', '').lower().endswith(ext)
                                                       for ext in ('.mp4', '.mov', '.webm'))),
                        None
                    )
                    if not video_attachment:
                        continue
                    
                    desc = [
                        f"**{i}.** By **{row['author_name']}**",
                        f"🔥 {row['unique_reactor_count']} unique reactions"
                    ]
                    
                    if row['content'] and row['content'].strip():
                        desc.append(self._replace_user_mentions(row['content'][:150]))
                    
                    desc.append(video_attachment['url'])
                    # Generate jump URL dynamically
                    jump_url = f"https://discord.com/channels/{self.summarizer.guild_id}/{row['channel_id']}/{row['message_id']}"
                    desc.append(f"🔗 Original post: {jump_url}")
                    msg_text = "\n".join(desc)
                    
                    await discord_utils.safe_send_message(
                        self.summarizer.bot, 
                        thread, 
                        self.summarizer.rate_limiter, 
                        self.summarizer.logger, 
                        content=msg_text
                    )
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    self.summarizer.logger.error(f"Error processing generation {i}: {e}")
                    self.summarizer.logger.debug(traceback.format_exc())
                    continue

            self.summarizer.logger.info(f"Successfully posted top generations for channel {channel_id}")

        except Exception as e:
            self.summarizer.logger.error(f"Error in post_top_gens_for_channel: {e}")
            self.summarizer.logger.debug(traceback.format_exc())

    def _replace_user_mentions(self, text: str) -> str:
        """
        Replace <@123...> with @username lookups from DB for more readable messages.
        """
        user_ids = re.findall(r'<@!?(\d+)>', text)
        if not user_ids:
            return text

        placeholders = ','.join('?' for _ in user_ids)
        query = f"SELECT member_id, COALESCE(server_nick, global_name, username) as display_name FROM members WHERE member_id IN ({placeholders})"
        
        results = self.summarizer.db_handler.execute_query(query, tuple(user_ids))
        
        id_to_name = {str(row['member_id']): f"@{row['display_name']}" for row in results}

        def replace(match):
            user_id = match.group(1)
            return id_to_name.get(user_id, match.group(0))

        return re.sub(r'<@!?(\d+)>', replace, text)

