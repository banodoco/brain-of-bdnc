# src/features/curating/curator_cog.py

import asyncio
import traceback
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

    async def cog_load(self):
        """Called once the cog is loaded."""
        pass

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
