from discord.ext import commands
import logging

from src.common.db_handler import DatabaseHandler
# Remove old client import
# from src.common.claude_client import ClaudeClient 
from .sharer import Sharer

logger = logging.getLogger('DiscordBot')

class SharingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_handler: DatabaseHandler):
        self.bot = bot
        self.db_handler = db_handler
        # Remove storing the old client
        # self.claude_client = claude_client 
        # Initialize the Sharer instance without passing the client
        self.sharer_instance = Sharer(
            bot=self.bot, 
            db_handler=self.db_handler, 
            logger_instance=logger,
            # claude_client=self.claude_client # Remove client pass-through
        )
        logger.info("SharingCog initialized.")
        # TODO: Add any sharing-specific commands here using @commands.command or @app_commands.command

    # Example command (can be removed if not needed)
    # @commands.command(name="sharing_status")
    # async def sharing_status(self, ctx):
    #     await ctx.send("Sharing feature is active.")

    # Add other listeners or commands specific to sharing if necessary

async def setup(bot: commands.Bot):
    # Fetch the db_handler instance from the bot 
    if not hasattr(bot, 'db_handler'):
         logger.error("Database handler not found on bot object. Cannot load SharingCog.")
         return 
    # Remove check for claude_client
    # if not hasattr(bot, 'claude_client'):
    #      logger.error("Claude client not found on bot object. Cannot load SharingCog.")
    #      return

    try:
        logger.info("About to create SharingCog instance...")
        cog_instance = SharingCog(bot, bot.db_handler)
        logger.info("SharingCog instance created, adding to bot...")
        await bot.add_cog(cog_instance)
        logger.info("SharingCog added to bot.")
    except Exception as e:
        logger.error(f"Error in SharingCog setup: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

    # IMPORTANT: Update Reactor initialization in main bot file as noted before
    # to get the sharer_instance from the loaded cog:
    # `bot.get_cog('SharingCog').sharer_instance` 