import discord
from discord.ext import commands
import logging

from src.common.db_handler import DatabaseHandler
# Import the shared client
from src.common.claude_client import ClaudeClient
from .sharer import Sharer

logger = logging.getLogger('DiscordBot')

class SharingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_handler: DatabaseHandler, claude_client: ClaudeClient):
        self.bot = bot
        self.db_handler = db_handler
        # Store the shared client
        self.claude_client = claude_client
        # Initialize the Sharer instance, passing the client along
        self.sharer_instance = Sharer(
            bot=self.bot, 
            db_handler=self.db_handler, 
            logger_instance=logger,
            claude_client=self.claude_client # Pass client to Sharer
        )
        logger.info("SharingCog initialized.")
        # TODO: Add any sharing-specific commands here using @commands.command or @app_commands.command

    # Example command (can be removed if not needed)
    # @commands.command(name="sharing_status")
    # async def sharing_status(self, ctx):
    #     await ctx.send("Sharing feature is active.")

    # Add other listeners or commands specific to sharing if necessary

async def setup(bot: commands.Bot):
    # Fetch the db_handler and claude_client instances from the bot 
    # This assumes they are initialized and stored on the bot object in your main file
    if not hasattr(bot, 'db_handler'):
         logger.error("Database handler not found on bot object. Cannot load SharingCog.")
         return 
    if not hasattr(bot, 'claude_client'):
         logger.error("Claude client not found on bot object. Cannot load SharingCog.")
         # Initialize if needed and not global: 
         # bot.claude_client = ClaudeClient()
         return

    # Pass the required instances to the Cog
    await bot.add_cog(SharingCog(bot, bot.db_handler, bot.claude_client))
    logger.info("SharingCog added to bot.")

    # IMPORTANT: Update Reactor initialization in main bot file as noted before
    # to get the sharer_instance from the loaded cog:
    # `bot.get_cog('SharingCog').sharer_instance` 