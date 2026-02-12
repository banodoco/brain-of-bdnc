import os
import asyncio
import discord
from typing import TYPE_CHECKING
from datetime import datetime, timedelta


# Import Sharer for type hinting
if TYPE_CHECKING:
    from src.features.sharing.sharer import Sharer
    from src.features.summarising.summariser import ChannelSummarizer # For type hinting self.summarizer

class TopArtSharing:
    # Update init to accept and store Sharer
    def __init__(self, summarizer_instance: 'ChannelSummarizer', sharer_instance: 'Sharer'):
        self.summarizer = summarizer_instance # Renamed for clarity
        self.sharer = sharer_instance # Store Sharer instance

    async def post_top_art_share(self, summary_channel: discord.TextChannel):
        """
        Finds the top art post (image or video/gif) in the last 24h 
        from the art channel and initiates the sharing process via the Sharer.
        """
        try:
            self.summarizer.logger.info("Starting post_top_art_share")

            # Determine correct art channel ID based on mode
            art_channel_id = int(os.getenv('DEV_ART_CHANNEL_ID' if self.summarizer.dev_mode else 'ART_CHANNEL_ID', 0))
            if not art_channel_id:
                self.summarizer.logger.error("Art channel ID (ART_CHANNEL_ID or DEV_ART_CHANNEL_ID) not configured.")
                return

            self.summarizer.logger.info(f"Using Art channel ID: {art_channel_id}")

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
                # Use the new execute_query method which handles connection pooling and retries
                results = await asyncio.to_thread(
                    self.summarizer.db_handler.execute_query,
                    query,
                    (art_channel_id, yesterday.isoformat())
                )
                top_art_data = results[0] if results else None

            except Exception as e:
                self.summarizer.logger.error(f"Database error fetching top art post: {e}", exc_info=True)
                return # Stop if DB query fails
                
            if not top_art_data:
                self.summarizer.logger.info("No suitable art posts found in database for the last 24 hours.")
                # Optionally send a message to summary channel indicating no top art?                
                # await self.bot.safe_send_message(summary_channel, "_No top art post found in the last 24 hours._")
                return
            
            # Extract necessary IDs
            message_id = top_art_data.get('message_id')
            channel_id = top_art_data.get('channel_id')
            author_id = top_art_data.get('author_id') # Get author_id

            if not all([message_id, channel_id, author_id]):
                 self.summarizer.logger.error(f"Missing critical data (msg_id, chan_id, author_id) in fetched top art post: {top_art_data}")
                 return
                 
            # Fetch the actual message object
            try:
                # Access discord methods via self.summarizer.bot (the actual discord.py Bot instance)
                art_channel_actual = self.summarizer.bot.get_channel(channel_id) or await self.summarizer.bot.fetch_channel(channel_id)
                if not isinstance(art_channel_actual, (discord.TextChannel, discord.Thread)):
                    self.summarizer.logger.error(f"Fetched channel {channel_id} is not a text channel or thread.")
                    return
                    
                message_object = await art_channel_actual.fetch_message(message_id)
                self.summarizer.logger.info(f"Successfully fetched top art message object: {message_id}")
                
            except discord.NotFound:
                self.summarizer.logger.error(f"Could not find art channel {channel_id} or message {message_id} to initiate sharing.")
                return
            except discord.Forbidden:
                self.summarizer.logger.error(f"Bot lacks permissions to fetch top art message {message_id} from channel {channel_id}.")
                return
            except Exception as e_fetch:
                self.summarizer.logger.error(f"Error fetching top art message object {message_id}: {e_fetch}", exc_info=True)
                return
                
            # Initiate the DM process with the author, passing the summary_channel
            self.summarizer.logger.info(f"Initiating sharing DM process for top art post {message_id} by author {author_id}")
            # Pass summary_channel to the sharer process
            await self.sharer.initiate_sharing_process_from_summary(message_object, summary_channel)
            self.summarizer.logger.info(f"Sharing DM process initiated for {message_id}.")

        except Exception as e:
            self.summarizer.logger.error(f"Error in post_top_art_share: {e}", exc_info=True)

    # Removed _replace_user_mentions as it's no longer needed here

