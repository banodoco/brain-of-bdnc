import os
import re
import json
import sqlite3
import asyncio
import traceback
import discord
from typing import Optional, TYPE_CHECKING
from datetime import datetime, timedelta

# Import Sharer for type hinting
if TYPE_CHECKING:
    from src.features.sharing.sharer import Sharer

class TopArtSharing:
    # Update init to accept and store Sharer
    def __init__(self, bot, sharer_instance: 'Sharer'):
        self.bot = bot
        self.sharer = sharer_instance # Store Sharer instance

    async def post_top_art_share(self, summary_channel: discord.TextChannel):
        """
        Finds the top art post (image or video/gif) in the last 24h 
        from the art channel and initiates the sharing process via the Sharer.
        """
        try:
            self.bot.logger.info("Starting post_top_art_share")

            # Determine correct art channel ID based on mode
            art_channel_id = int(os.getenv('DEV_ART_CHANNEL_ID' if self.bot.dev_mode else 'ART_CHANNEL_ID', 0))
            if not art_channel_id:
                self.bot.logger.error("Art channel ID (ART_CHANNEL_ID or DEV_ART_CHANNEL_ID) not configured.")
                return

            self.bot.logger.info(f"Using Art channel ID: {art_channel_id}")

            yesterday = datetime.utcnow() - timedelta(hours=24)
            # Updated query to get author_id directly
            query = """
                SELECT 
                    m.message_id,
                    m.channel_id,
                    m.author_id, -- Added author_id
                    m.content,
                    m.attachments,
                    m.reactors,
                    COALESCE(mem.server_nick, mem.global_name, mem.username) as author_name,
                    CASE 
                        WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                        ELSE json_array_length(m.reactors)
                    END as unique_reactor_count
                FROM messages m
                JOIN members mem ON m.author_id = mem.member_id
                WHERE m.channel_id = ?
                AND m.created_at > ?
                AND json_valid(m.attachments)
                AND m.attachments != '[]'
                ORDER BY unique_reactor_count DESC
                LIMIT 1
            """
            
            top_art_data = None
            try:
                # Use await for async DB operations if your db handler supports it
                # Assuming sync execution for now based on original code
                loop = asyncio.get_event_loop()
                def db_query(): 
                    conn = self.bot.db._get_connection() # Get connection from handler
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(query, (art_channel_id, yesterday.isoformat()))
                    result = cursor.fetchone()
                    cursor.close()
                    conn.row_factory = None # Reset row factory
                    return dict(result) if result else None
                    
                top_art_data = await loop.run_in_executor(None, db_query)

            except Exception as e:
                self.bot.logger.error(f"Database error fetching top art post: {e}", exc_info=True)
                return # Stop if DB query fails
                
            if not top_art_data:
                self.bot.logger.info("No suitable art posts found in database for the last 24 hours.")
                # Optionally send a message to summary channel indicating no top art?                
                # await self.bot.safe_send_message(summary_channel, "_No top art post found in the last 24 hours._")
                return
            
            # Extract necessary IDs
            message_id = top_art_data.get('message_id')
            channel_id = top_art_data.get('channel_id')
            author_id = top_art_data.get('author_id') # Get author_id

            if not all([message_id, channel_id, author_id]):
                 self.bot.logger.error(f"Missing critical data (msg_id, chan_id, author_id) in fetched top art post: {top_art_data}")
                 return
                 
            # Fetch the actual message object
            try:
                art_channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                if not isinstance(art_channel, (discord.TextChannel, discord.Thread)):
                    self.bot.logger.error(f"Fetched channel {channel_id} is not a text channel or thread.")
                    return
                    
                message_object = await art_channel.fetch_message(message_id)
                self.bot.logger.info(f"Successfully fetched top art message object: {message_id}")
                
            except discord.NotFound:
                self.bot.logger.error(f"Could not find art channel {channel_id} or message {message_id} to initiate sharing.")
                return
            except discord.Forbidden:
                self.bot.logger.error(f"Bot lacks permissions to fetch top art message {message_id} from channel {channel_id}.")
                return
            except Exception as e:
                self.bot.logger.error(f"Error fetching top art message object {message_id}: {e}", exc_info=True)
                return
                
            # Post the top art directly to the summary channel
            author_display_name = message_object.author.display_name # Get display name
            reaction_count = top_art_data.get('unique_reactor_count', 0) # Get reaction count from DB data
            attachment_url = message_object.attachments[0].url if message_object.attachments else "No attachment found"
            jump_url = message_object.jump_url
            
            # Construct message in the desired format
            content_parts = [
                f"## Top Art Post By **{author_display_name}**\n" # Combined header and author
            ]
            
            if message_object.content:
                 # Format original content as a quote block
                 quoted_content = '\n'.join([f'> {line}' for line in message_object.content.split('\n')])
                 content_parts.append(quoted_content)
                 
            content_parts.append(attachment_url)
            content_parts.append(f"🔗 Original post: {jump_url}")
            
            content_to_post = "\n".join(content_parts)
                 
            await self.bot.safe_send_message(summary_channel, content_to_post)
            self.bot.logger.info(f"Posted top art post {message_id} directly to summary channel.")
            
            # Now, initiate the DM process with the author
            self.bot.logger.info(f"Initiating sharing DM process for top art post {message_id} by author {author_id}")
            await self.sharer.initiate_sharing_process_from_summary(message_object)
            self.bot.logger.info(f"Sharing DM process initiated for {message_id}.")

        except Exception as e:
            self.bot.logger.error(f"Error in post_top_art_share: {e}", exc_info=True)

    # Removed _replace_user_mentions as it's no longer needed here

