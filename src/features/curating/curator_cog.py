# src/features/curating/curator_cog.py

import os
from discord.ext import commands
from src.features.curating.curator import ArtCurator

class CuratorCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.logger.info("Initializing CuratorCog...")

        # Example: environment-based channel IDs
        if self.dev_mode:
            self.art_channel_id = int(os.getenv('DEV_ART_CHANNEL_ID', '1138865343314530324'))
            self.curator_ids = [301463647895683072]  # example
            self.logger.info(f"Using development art channel: {self.art_channel_id}")
            self.logger.info(f"Using development curator IDs: {self.curator_ids}")
        else:
            self.art_channel_id = int(os.getenv('PROD_ART_CHANNEL_ID', '0'))
            # ...
            self.logger.info(f"Using production art channel: {self.art_channel_id}")
            # etc.
        self.art_curator = ArtCurator(logger=logger, dev_mode=dev_mode)

    @commands.Cog.listener()
    async def on_ready(self):
        """If you need extra logic once the bot is connected."""
        self.logger.info("CuratorCog is ready.")

    # If your curator had special tasks or commands, define them here.
    # For example:
    @commands.command(name='curate')
    async def manual_curate(self, ctx):
        """Forces a curation cycle manually, if appropriate."""
        self.logger.info("Running manual curation using ArtCurator...")
        await self.art_curator.manual_curate()
        self.logger.info("Finished ArtCurator's manual curation.")
        await ctx.send("Manual curation cycle completed.")

async def setup(bot: commands.Bot):
    """Sets up the CuratorCog."""
    # Ensure logger and dev_mode are available on the bot instance
    if not hasattr(bot, 'logger'):
        print("ERROR: Logger not found on bot object. Cannot load CuratorCog.")
        return
    if not hasattr(bot, 'dev_mode'):
         print("ERROR: dev_mode attribute not found on bot object. Cannot load CuratorCog.")
         return

    # Retrieve logger and dev_mode from the bot instance
    logger = bot.logger
    dev_mode = bot.dev_mode
    
    await bot.add_cog(CuratorCog(bot, logger, dev_mode=dev_mode))
    logger.info("CuratorCog added to bot.")
