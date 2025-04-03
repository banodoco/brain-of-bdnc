# src/features/admin/admin_cog.py
import discord
import logging
from discord.ext import commands
from discord import app_commands

from src.common.db_handler import DatabaseHandler

logger = logging.getLogger('DiscordBot')

# --- Modal for Updating Socials --- 
class AdminUpdateSocialsModal(discord.ui.Modal, title='Update Your Social Handles & Website'):
    twitter_input = discord.ui.TextInput(
        label='Twitter Handle (e.g., @username or full URL)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    instagram_input = discord.ui.TextInput(
        label='Instagram Handle (@username or URL)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    youtube_input = discord.ui.TextInput(
        label='YouTube Handle (e.g., @channel or full URL)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    tiktok_input = discord.ui.TextInput(
        label='TikTok Handle (e.g., @username or full URL)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    website_input = discord.ui.TextInput(
        label='Website URL',
        required=False,
        placeholder='Leave blank to remove',
        style=discord.TextStyle.short,
        max_length=200
    )

    def __init__(self, user_details: dict, db_handler: DatabaseHandler):
        super().__init__()
        self.user_details = user_details
        self.db_handler = db_handler

        # Pre-fill modal
        self.twitter_input.default = user_details.get('twitter_handle')
        self.instagram_input.default = user_details.get('instagram_handle')
        self.youtube_input.default = user_details.get('youtube_handle')
        self.tiktok_input.default = user_details.get('tiktok_handle')
        self.website_input.default = user_details.get('website')

    async def on_submit(self, interaction: discord.Interaction):
        try:
            updated_data = {
                'twitter_handle': self.twitter_input.value.strip() or None,
                'instagram_handle': self.instagram_input.value.strip() or None,
                'youtube_handle': self.youtube_input.value.strip() or None,
                'tiktok_handle': self.tiktok_input.value.strip() or None,
                'website': self.website_input.value.strip() or None,
            }
            
            # Update DB
            # Assuming db_handler has a synchronous method or handles async internally
            # If it's async, you might need asyncio.to_thread or ensure it's called from async context correctly
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name, 
                global_name=interaction.user.global_name,
                **updated_data
            )

            if success:
                await interaction.response.send_message("Your social details have been updated successfully!", ephemeral=True)
                logger.info(f"User {interaction.user.id} updated social details via /update_details command.")
            else:
                 await interaction.response.send_message("Failed to update your details in the database. Please try again later.", ephemeral=True)
                 logger.error(f"Failed DB update for user {interaction.user.id} social details via /update_details.")

        except Exception as e:
            logger.error(f"Error in AdminUpdateSocialsModal on_submit for user {interaction.user.id}: {e}", exc_info=True)
            await interaction.response.send_message("An error occurred while updating your details.", ephemeral=True)

# --- Admin Cog Class --- 
class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Access db_handler assuming it's attached to the bot instance
        self.db_handler: DatabaseHandler = getattr(bot, 'db_handler', None)
        if not self.db_handler:
             logger.error("AdminCog initialized without access to db_handler on the bot!")
             # Depending on strictness, you might raise an error here

    @app_commands.command(name="update_details", description="Update your social media handles and website link.")
    async def update_details(self, interaction: discord.Interaction):
        """Allows a user to update their social media details via a modal."""
        if not self.db_handler:
             await interaction.response.send_message("Database connection is unavailable. Please try again later.", ephemeral=True)
             return

        try:
            user_id = interaction.user.id
            # Fetch user details
            user_details = self.db_handler.get_member(user_id)

            if not user_details:
                # If user not found, create a default entry or structure to pre-fill modal
                logger.info(f"User {user_id} not found in DB for /update_details. Creating temporary dict.")
                # We don't necessarily need to create them in the DB *before* the modal,
                # create_or_update_member handles inserts. Just need defaults for the modal.
                user_details = {
                     'member_id': user_id,
                     'username': interaction.user.name,
                     'global_name': interaction.user.global_name,
                     'twitter_handle': None,
                     'instagram_handle': None,
                     'youtube_handle': None,
                     'tiktok_handle': None,
                     'website': None
                }
            
            # Create and send the modal
            modal = AdminUpdateSocialsModal(user_details=user_details, db_handler=self.db_handler)
            await interaction.response.send_modal(modal)
            logger.info(f"User {user_id} triggered /update_details command.")

        except Exception as e:
            logger.error(f"Error executing /update_details for user {interaction.user.id}: {e}", exc_info=True)
            if not interaction.response.is_done():
                 try:
                      await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)
                 except discord.InteractionResponded:
                      await interaction.followup.send("An error occurred after the initial response.", ephemeral=True)
                 except Exception as followup_e:
                      logger.error(f"Failed to send error message for /update_details: {followup_e}")

async def setup(bot: commands.Bot):
    if not hasattr(bot, 'db_handler'):
         logger.error("Cannot setup AdminCog: db_handler not found on bot instance.")
         return # Prevent setup if dependency is missing
    await bot.add_cog(AdminCog(bot)) 