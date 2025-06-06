import discord
import re
import logging

class MessageLinker:
    def __init__(self, bot: discord.Client, logger: logging.Logger, allowed_channel_ids: list[int] | None = None):
        self.bot = bot
        self.logger = logger
        self.allowed_channel_ids = allowed_channel_ids if allowed_channel_ids is not None else []
        # Regex to find Discord message links
        # Handles https://discord.com/channels/GUILD_ID/CHANNEL_ID/MESSAGE_ID
        self.message_link_regex = re.compile(
            r"https?://(?:www\.)?discord(?:app)?\.com/channels/"
            r"(?P<guild_id>\d+)/"
            r"(?P<channel_id>\d+)/"
            r"(?P<message_id>\d+)"
        )
        if self.allowed_channel_ids:
            self.logger.info(f"[MessageLinker] Initialized. Active for channel IDs: {self.allowed_channel_ids}")
        else:
            self.logger.info("[MessageLinker] Initialized. No specific channels configured; will not process message links unless channel list is updated.")

    async def process_message_links(self, message: discord.Message):
        """
        Checks a message for Discord message links and posts the content
        and media of the linked messages, if the current channel is allowed.
        """
        if message.author.bot:
            return

        # If no channels are configured, or if the message's channel is not in the allowed list, do nothing.
        if not self.allowed_channel_ids or message.channel.id not in self.allowed_channel_ids:
            if not self.allowed_channel_ids:
                # This log might be noisy if it happens for every message. Consider logging only at init.
                # self.logger.debug(f"[MessageLinker] No allowed channels configured. Skipping message {message.id}.")
                pass
            # else: # Message is not in an allowed channel, already logged at init if channels are set.
            return
        
        self.logger.info(f"[MessageLinker] Processing message {message.id} in allowed channel {message.channel.id} for links.")

        matches = self.message_link_regex.finditer(message.content)
        linked_messages_processed = 0

        for match in matches:
            try:
                guild_id = int(match.group("guild_id"))
                channel_id = int(match.group("channel_id"))
                message_id = int(match.group("message_id"))

                self.logger.info(f"[MessageLinker] Found Discord message link: Guild={guild_id}, Channel={channel_id}, Message={message_id} in message {message.id}")

                # Ensure the link is not to the message itself or in a different guild (optional, but good practice)
                if message.guild and guild_id != message.guild.id:
                    self.logger.warning(f"[MessageLinker] Linked message {message_id} is in a different guild. Skipping.")
                    continue

                target_channel = self.bot.get_channel(channel_id)
                if not target_channel:
                    self.logger.warning(f"[MessageLinker] Could not find channel {channel_id} from link. Attempting to fetch.")
                    try:
                        target_channel = await self.bot.fetch_channel(channel_id)
                    except discord.NotFound:
                        self.logger.error(f"[MessageLinker] Channel {channel_id} not found via API.")
                        continue
                    except discord.Forbidden:
                        self.logger.error(f"[MessageLinker] No permission to fetch channel {channel_id}.")
                        continue
                    except Exception as e:
                        self.logger.error(f"[MessageLinker] Error fetching channel {channel_id}: {e}")
                        continue
                
                if not isinstance(target_channel, discord.TextChannel) and not isinstance(target_channel, discord.Thread):
                    self.logger.warning(f"[MessageLinker] Channel {channel_id} is not a text channel or thread. Skipping message link.")
                    continue

                try:
                    linked_message = await target_channel.fetch_message(message_id)
                    self.logger.info(f"[MessageLinker] Successfully fetched linked message {linked_message.id} from channel {target_channel.name}")
                except discord.NotFound:
                    self.logger.warning(f"[MessageLinker] Linked message {message_id} not found in channel {target_channel.id}.")
                    await message.channel.send(f"> _Could not find the linked message._", reference=message, mention_author=False)
                    continue
                except discord.Forbidden:
                    self.logger.error(f"[MessageLinker] No permission to fetch message {message_id} from channel {target_channel.id}.")
                    await message.channel.send(f"> _I don't have permission to fetch the linked message._", reference=message, mention_author=False)
                    continue
                except Exception as e:
                    self.logger.error(f"[MessageLinker] Error fetching linked message {message_id}: {e}")
                    await message.channel.send(f"> _An error occurred while trying to fetch the linked message._", reference=message, mention_author=False)
                    continue

                # Post the content of the linked message
                if linked_message.content:
                    # Escape markdown in the content to prevent unwanted formatting
                    escaped_content = discord.utils.escape_markdown(linked_message.content)
                    # Add quote style if not already present
                    if not escaped_content.startswith('>'):
                        # Perform the replacement outside the f-string expression part
                        processed_content = escaped_content.replace('\n', '\n> ')
                        display_content = f"> {processed_content}"
                    else:
                        display_content = escaped_content
                    
                    header = f"**Content from {linked_message.author.mention} in {linked_message.channel.mention} (message link by {message.author.mention}):**"
                    
                    # Send content in chunks if it's too long
                    max_length = 1950 # discord message length limit
                    
                    # Ensure unique users for allowed_mentions
                    mention_users = list(set([linked_message.author, message.author]))

                    if len(header) + len(display_content) < max_length:
                        await message.channel.send(f"{header}\n{display_content}", allowed_mentions=discord.AllowedMentions(users=mention_users))
                    else:
                        # For the header part, only the message.author (who posted the link) is strictly necessary for the context of *this* message.
                        # However, the header itself mentions linked_message.author.
                        # To be safe and consistent, we can use the unique list here too, or just message.author if preferred.
                        # Let's use the unique list to allow both mentions if they are distinct.
                        await message.channel.send(header, allowed_mentions=discord.AllowedMentions(users=mention_users))
                        # Send content in chunks
                        for i in range(0, len(display_content), max_length):
                            chunk = display_content[i:i+max_length]
                            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                                      
                else: # If no text content, still announce the attachments if any
                     if linked_message.attachments:
                        # Ensure unique users for allowed_mentions
                        mention_users = list(set([linked_message.author, message.author]))
                        await message.channel.send(f"**Attachments from {linked_message.author.mention} in {linked_message.channel.mention} (message link by {message.author.mention}):**", allowed_mentions=discord.AllowedMentions(users=mention_users))


                # Post each attachment/media separately
                if linked_message.attachments:
                    self.logger.info(f"[MessageLinker] Found {len(linked_message.attachments)} attachments in linked message {linked_message.id}.")
                    for attachment in linked_message.attachments:
                        # You might want to check file types or sizes here if needed
                        await message.channel.send(attachment.url) # Posts the direct link to the media
                
                # Post embeds if present (e.g., images, videos from links like YouTube)
                # Be careful with embeds, as they can be numerous or large.
                # We will post the URL of the embed if it's a rich type (like image, video)
                if linked_message.embeds:
                    self.logger.info(f"[MessageLinker] Found {len(linked_message.embeds)} embeds in linked message {linked_message.id}.")
                    for embed in linked_message.embeds:
                        if embed.url and embed.type in ['image', 'video', 'gifv', 'article', 'link']:
                             # If it's an image or video embed, its URL might be what we want to share
                            await message.channel.send(embed.url)
                        elif embed.thumbnail and embed.thumbnail.url:
                             await message.channel.send(embed.thumbnail.url)


                linked_messages_processed += 1

            except Exception as e:
                self.logger.error(f"[MessageLinker] General error processing a message link in message {message.id}: {e}", exc_info=True)
                # Check if message.channel exists and has send attribute before trying to send
                if message.channel and hasattr(message.channel, 'send'):
                    await message.channel.send(f"> _An unexpected error occurred while processing a message link._", reference=message, mention_author=False)
        
        if linked_messages_processed > 0:
             self.logger.info(f"[MessageLinker] Finished processing {linked_messages_processed} link(s) from message {message.id} in channel {message.channel.id}.")

async def setup(bot: discord.Client, logger: logging.Logger, allowed_channel_ids: list[int] | None = None):
    """
    Setup function to add the MessageLinker to the bot.
    This might be called from your main cog or bot setup.
    """
    actual_allowed_channel_ids = allowed_channel_ids if allowed_channel_ids is not None else []
    linker_instance = MessageLinker(bot, logger, allowed_channel_ids=actual_allowed_channel_ids)
    # Instead of adding as a cog, we will likely call its methods from ReactorCog
    # For now, let's assume it will be instantiated and used by ReactorCog.
    # To make it accessible, we can store it on the bot object, similar to reactor_instance
    if not hasattr(bot, 'message_linker_instance'):
        bot.message_linker_instance = linker_instance
        logger.info(f"[MessageLinker] MessageLinker instance created and attached to bot. Allowed channels: {actual_allowed_channel_ids}")
    else:
        # If it already exists, we might want to update its allowed channels
        # For simplicity now, we assume it's set up once.
        # If re-configuration is needed, ReactorCog would need to handle replacing/updating the instance.
        logger.info(f"[MessageLinker] MessageLinker instance already exists on bot. Current allowed channels: {getattr(bot.message_linker_instance, 'allowed_channel_ids', 'N/A')}")

# Example of how it might be called (for testing or integration):
# async def handle_message_for_linking(bot, message, logger):
#     if not hasattr(bot, 'message_linker_instance'):
#         # This setup would need to get allowed_channel_ids from config
#         # For example, from os.getenv or another config source
#         # allowed_ids_from_config = [int(cid) for cid in os.getenv("MESSAGE_LINKER_CHANNELS", "").split(",") if cid]
#         # await setup(bot, logger, allowed_channel_ids=allowed_ids_from_config) 