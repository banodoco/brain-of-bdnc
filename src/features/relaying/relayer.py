import discord
from discord.ext import commands
import logging

# Configuration - Consider moving this to .env or config file
OPENMUSE_FEATURING_CHANNEL_ID = 1366067251694014506

class Relayer:
    def __init__(self, bot: commands.Bot, logger: logging.Logger, dev_mode: bool = False):
        """
        Initializes the Relayer class.

        Args:
            bot: The discord.py Bot instance.
            logger: The logger instance for logging messages.
            dev_mode: Boolean indicating if running in development mode.
        """
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.logger.info(f"[Relayer] Initialized. Dev Mode: {self.dev_mode}")

    async def action_openmuse_featuring(self, username: str, content_type: str, url: str):
        """
        Handles the 'openmuse_featuring' action triggered by a webhook.

        Posts a message to the designated featuring channel.

        Args:
            username: The username associated with the feature request.
            content_type: The type of content being featured (e.g., 'image', 'video', 'track').
            url: The URL of the content to be featured.
        """
        self.logger.info(f"[Relayer] Received openmuse_featuring request: User='{username}', Type='{content_type}', URL='{url}'")

        try:
            channel = self.bot.get_channel(OPENMUSE_FEATURING_CHANNEL_ID)
            if not channel:
                self.logger.error(f"[Relayer] Could not find channel with ID {OPENMUSE_FEATURING_CHANNEL_ID}")
                return

            if not isinstance(channel, discord.TextChannel):
                 self.logger.error(f"[Relayer] Channel {OPENMUSE_FEATURING_CHANNEL_ID} is not a text channel.")
                 return

            # Construct the message
            message_content = f"✨ **OpenMuse Featuring** ✨\n\n"
            message_content += f"👤 **User:** {username}\n"
            message_content += f"📄 **Type:** {content_type}\n"
            message_content += f"🔗 **Link:** {url}"

            await channel.send(message_content)
            self.logger.info(f"[Relayer] Successfully sent 'openmuse_featuring' message to channel {channel.id}")

        except discord.errors.Forbidden:
            self.logger.error(f"[Relayer] Bot lacks permissions to send messages in channel {OPENMUSE_FEATURING_CHANNEL_ID}")
        except Exception as e:
            self.logger.error(f"[Relayer] Error processing 'openmuse_featuring' action: {e}", exc_info=True)

# Example of how it might be called (actual call will come from the Cog)
# async def example_usage(relayer_instance):
#     await relayer_instance.action_openmuse_featuring("ExampleUser", "image", "http://example.com/image.png") 