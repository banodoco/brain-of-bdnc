# src/features/admin/admin_cog.py
import discord
import logging
from discord.ext import commands
from discord import app_commands
import asyncio
import sys
import json
import os
from datetime import datetime, timedelta
import random

from src.common.db_handler import DatabaseHandler
from src.common import discord_utils
# Assuming constants.py has get_project_root()
# If not, we might need os.path.dirname multiple times
# try:
#     from src.common.constants import get_project_root # REMOVE THIS IMPORT
# except ImportError:
#     # Basic fallback if constants doesn't have it
#     get_project_root = lambda: os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
#     logging.warning("src.common.constants.get_project_root not found, using fallback path logic.")

logger = logging.getLogger('DiscordBot')

# --- Modal for Updating Socials --- 
class AdminUpdateSocialsModal(discord.ui.Modal, title='Update Your Preferences'):
    twitter_input = discord.ui.TextInput(
        label='Twitter Handle (e.g., @username)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    instagram_input = discord.ui.TextInput(
        label='Instagram Handle (e.g., @username)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    youtube_input = discord.ui.TextInput(
        label='YouTube Channel URL (full URL)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    # REMOVE TIKTOK AND WEBSITE
    # tiktok_input = discord.ui.TextInput(
    #     label='TikTok Handle (e.g., @username or full URL)',
    #     required=False,
    #     placeholder='Leave blank to remove',
    #     max_length=100
    # )
    # website_input = discord.ui.TextInput(
    #     label='Website URL',
    #     required=False,
    #     placeholder='Leave blank to remove',
    #     style=discord.TextStyle.short,
    #     max_length=200
    # )

    permission_to_share_input = discord.ui.TextInput(
        label='Okay to share on social? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True, # Making it required to ensure user makes a choice or confirms current
        max_length=3,
        min_length=2 
    )

    permission_to_curate_input = discord.ui.TextInput(
        label='Okay to share on OpenMuse? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True, # Making it required
        max_length=3,
        min_length=2
    )

    def __init__(self, user_details: dict, db_handler: DatabaseHandler):
        super().__init__()
        self.user_details = user_details
        self.db_handler = db_handler

        # Pre-fill modal
        self.twitter_input.default = user_details.get('twitter_handle')
        self.instagram_input.default = user_details.get('instagram_handle')
        self.youtube_input.default = user_details.get('youtube_handle')
        # self.tiktok_input.default = user_details.get('tiktok_handle') # REMOVED
        # self.website_input.default = user_details.get('website') # REMOVED

        # Pre-fill new permission inputs based on DB fields (sharing_consent, permission_to_curate)
        # Assuming these fields store 1 (for yes) or 0 (for no)
        # Display with first letter capitalized for the user
        self.permission_to_share_input.default = "Yes" if user_details.get('sharing_consent') == 1 else "No"
        self.permission_to_curate_input.default = "Yes" if user_details.get('permission_to_curate') == 1 else "No"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            share_social_input_raw = self.permission_to_share_input.value.strip().lower()
            curate_openmuse_input_raw = self.permission_to_curate_input.value.strip().lower()

            final_sharing_consent = None
            final_permission_to_curate = None

            # Validate and convert for sharing_consent
            if share_social_input_raw == 'yes':
                final_sharing_consent = 1
            elif share_social_input_raw == 'no':
                final_sharing_consent = 0
            else: # Invalid input (should be caught by min/max_length, but good to have)
                await interaction.response.send_message(
                    "Invalid input for 'Okay to share on social?'. Please enter 'yes' or 'no'.", 
                    ephemeral=True
                )
                return

            # Validate and convert for permission_to_curate
            if curate_openmuse_input_raw == 'yes':
                final_permission_to_curate = 1
            elif curate_openmuse_input_raw == 'no':
                final_permission_to_curate = 0
            else: # Invalid input
                await interaction.response.send_message(
                    "Invalid input for 'Okay to share on OpenMuse?'. Please enter 'yes' or 'no'.", 
                    ephemeral=True
                )
                return
            
            updated_data = {
                'twitter_handle': self.twitter_input.value.strip() or None,
                'instagram_handle': self.instagram_input.value.strip() or None,
                'youtube_handle': self.youtube_input.value.strip() or None,
                # 'tiktok_handle': self.tiktok_input.value.strip() or None, # REMOVED
                # 'website': self.website_input.value.strip() or None, # REMOVED
                'sharing_consent': final_sharing_consent,       # Use correct DB field name
                'permission_to_curate': final_permission_to_curate, # Use correct DB field name
            }
            
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name, 
                global_name=interaction.user.global_name,
                **updated_data
            )

            if success:
                await interaction.response.send_message("Your preferences have been updated successfully!", ephemeral=True)
                logger.info(f"User {interaction.user.id} updated preferences via /update_details. Data: {updated_data}")
            else:
                 await interaction.response.send_message("Failed to update your preferences. Please try again later.", ephemeral=True)
                 logger.error(f"Failed DB update for user {interaction.user.id} preferences via /update_details.")

        except Exception as e:
            logger.error(f"Error in AdminUpdateSocialsModal on_submit for user {interaction.user.id}: {e}", exc_info=True)
            # Check if interaction is already responded to before sending another response
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while updating your preferences.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred after the initial response while updating your preferences.", ephemeral=True)

# --- Admin Dashboard View ---
class AdminDashboardView(discord.ui.View):
    # Note: bot.dev_mode and bot.owner_ids need to be set on your bot instance
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None) # Persistent view needs timeout=None
        self.bot = bot
        # Calculate project root relative to this file's location
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.project_root = os.path.abspath(os.path.join(current_dir, '..', '..', '..'))
        self.script_path = os.path.join(self.project_root, 'scripts', 'monthly_equity_shortlist.py')
        # Ensure the script path is correct and the script exists
        if not os.path.isfile(self.script_path):
             logger.error(f"AdminDashboardView: Script not found at calculated path: {self.script_path}")
             # Disable the button if script is missing? Or handle in button click.
             # Let's handle in button click for now.

    @discord.ui.button(label="Get Monthly Equity Shortlist", style=discord.ButtonStyle.primary, custom_id="admin_get_shortlist")
    async def get_shortlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Defer immediately (ephemeral to hide the initial ack)
        await interaction.response.defer(ephemeral=True)
        logger.info(f"Admin user {interaction.user.id} triggered 'Get Monthly Equity Shortlist'")

        # 2. Send ephemeral confirmation message
        confirmation_message = (
            "Running the shortlist script... this may take up to 20 minutes. "
            "I will post a new message in this channel with the results when complete."
        )
        await interaction.followup.send(confirmation_message, ephemeral=True)

        # Store channel for later use
        target_channel = interaction.channel

        # Calculate last month in YYYY-MM format
        today = datetime.utcnow()
        first_day_current_month = today.replace(day=1)
        last_day_previous_month = first_day_current_month - timedelta(days=1)
        previous_month_str = last_day_previous_month.strftime('%Y-%m')
        logger.info(f"Calculated target month as: {previous_month_str}")

        cmd = [sys.executable, self.script_path]

        # Add the required month argument
        cmd.extend(["-m", previous_month_str])

        # Check if bot is in dev mode (assuming bot has a 'dev_mode' attribute)
        is_dev = getattr(self.bot, 'dev_mode', False)
        if is_dev:
            cmd.append("--dev")
            logger.info(f"Running monthly_equity_shortlist.py for month {previous_month_str} in --dev mode.")
        else:
            logger.info(f"Running monthly_equity_shortlist.py for month {previous_month_str} in production mode.")

        # Prepare final embed/message content (will be populated in try/except)
        final_content = None
        final_embed = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Ensure the script runs from the project root if it relies on relative paths
                cwd=self.project_root, # Use the calculated project root
                env=os.environ # Pass the current environment variables
            )
            stdout, stderr = await process.communicate() # Wait for completion

            # Decode stdout/stderr safely
            output_str = stdout.decode('utf-8', errors='ignore') if stdout else ""
            error_str = stderr.decode('utf-8', errors='ignore') if stderr else ""

            # Log regardless of return code
            logger.info(f"Script process completed with return code: {process.returncode}")
            if output_str:
                 logger.debug(f"Script stdout (truncated):\n{output_str[:1000]}")
            if error_str:
                 logger.warning(f"Script stderr (truncated):\n{error_str[:1000]}")

            if process.returncode == 0:
                try:
                    result_json = json.loads(output_str)
                    logger.info("Successfully executed script and parsed JSON output.")

                    # Format the response
                    embed = discord.Embed(
                        title="Monthly Equity Shortlist",
                        color=discord.Color.green()
                    )
                    if result_json and "candidates" in result_json and result_json["candidates"]:
                        # Build the bullet point list
                        candidate_lines = []
                        for candidate in result_json["candidates"]:
                            handle = candidate.get('handle', 'Unknown Handle')
                            justification = candidate.get('justification', 'No justification provided.')
                            # Ensure justification isn't too long
                            if len(justification) > 1000: # Adjust limit as needed for description
                                justification = justification[:997] + "..."
                            candidate_lines.append(f"- @{handle} - {justification}")
                        
                        bullet_list_str = "\n".join(candidate_lines)
                        embed.description = f"Found {len(result_json['candidates'])} potential candidates for {previous_month_str}:\n\n{bullet_list_str}"
                    elif result_json and "candidates" in result_json and not result_json["candidates"]:
                         embed.description = f"The script ran successfully for {previous_month_str} but found no candidates based on the criteria for the period."
                         embed.color = discord.Color.orange()
                    else:
                         embed.description = f"Script executed for {previous_month_str}, but the output format was unexpected."
                         embed.add_field(name="Raw Output (truncated)", value=f"```json\n{output_str[:1000]}\n```")
                         embed.color = discord.Color.orange()

                    # Set the final embed for sending later
                    final_embed = embed

                except json.JSONDecodeError as json_err:
                    logger.error(f"Failed to decode JSON from script output for {interaction.user.id}. Error: {json_err}. Stdout: {output_str[:500]} Stderr: {error_str[:500]}", exc_info=False)
                    error_embed = discord.Embed(
                        title=f"Error: Invalid Script Output ({'Dev' if getattr(self.bot, 'dev_mode', False) else 'Prod'}) - {previous_month_str}",
                        description="The script ran, but its output was not valid JSON.",
                        color=discord.Color.red()
                    )
                    error_embed.add_field(name="Stdout (truncated)", value=f"```\n{output_str[:1000]}\n```", inline=False)
                    error_embed.add_field(name="Stderr (truncated)", value=f"```\n{error_str[:1000]}\n```", inline=False)
                    # Set the final error embed for sending later
                    final_embed = error_embed
            else:
                # Script failed
                logger.error(f"Script execution failed for {interaction.user.id} with return code {process.returncode}. Stderr: {error_str[:1000]} Stdout: {output_str[:500]}")
                error_embed = discord.Embed(
                    title=f"Error: Script Execution Failed ({'Dev' if getattr(self.bot, 'dev_mode', False) else 'Prod'}) - {previous_month_str}",
                    description=f"The script exited with code {process.returncode}. Check bot logs for stderr.",
                    color=discord.Color.red()
                )
                error_embed.add_field(name="Stderr (truncated)", value=f"```\n{error_str[:1000]}\n```", inline=False)
                if output_str: # Also show stdout if available
                    error_embed.add_field(name="Stdout (truncated)", value=f"```\n{output_str[:1000]}\n```", inline=False)
                # Set the final error embed for sending later
                final_embed = error_embed

        except FileNotFoundError:
             logger.error(f"Script not found at path: {self.script_path}")
             # Set the final error content for sending later
             final_content = f"Error: Could not find the script at `{self.script_path}`."
        except Exception as e:
            logger.error(f"An unexpected error occurred during script execution: {e}", exc_info=True)
            # Set the final error content for sending later
            final_content = f"An unexpected error occurred while trying to run the script: {e}"

        # 4. Send the final result/error as a NEW message in the channel
        if target_channel:
            try:
                if not hasattr(self.bot, 'rate_limiter'):
                    logger.error("[AdminDashboardView] Rate limiter not found on self.bot. Sending shortlist result directly.")
                    await target_channel.send(content=final_content, embed=final_embed)
                else:
                    await discord_utils.safe_send_message(
                        self.bot, 
                        target_channel, 
                        self.bot.rate_limiter, 
                        logger, # Global logger
                        content=final_content, 
                        embed=final_embed
                    )
                logger.info(f"Sent final shortlist result/error message to channel {target_channel.id}")
            except discord.Forbidden:
                logger.error(f"Failed to send final shortlist message to channel {target_channel.id}: Forbidden.")
                # Optionally notify the user via DM?
            except discord.HTTPException as http_err:
                logger.error(f"Failed to send final shortlist message to channel {target_channel.id}: HTTPException: {http_err}")
            except Exception as send_err:
                 logger.error(f"Unexpected error sending final shortlist message to channel {target_channel.id}: {send_err}", exc_info=True)
        else:
            logger.error("Target channel was None, could not send final shortlist message.")

# --- Admin Cog Class --- 
class AdminCog(commands.Cog):
    # Flag to ensure the persistent view is added only once
    _persistent_view_added = False
    _commands_synced = False  # Add flag to track if commands have been synced

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_handler = bot.db_handler if hasattr(bot, 'db_handler') else None
        # Assuming self.bot will have self.bot.rate_limiter initialized by main bot setup
        logger.info("AdminCog initialized")

    async def cog_load(self):
        """Called when the cog is loaded."""
        logger.info("AdminCog cog_load called")
        # Log all registered commands
        commands = [cmd.name for cmd in self.bot.tree.get_commands()]
        logger.info(f"Currently registered commands: {commands}")

    @commands.Cog.listener()
    async def on_ready(self):
        """Called when the bot is ready and connected to Discord."""
        logger.info("AdminCog on_ready called")
        if not self._commands_synced:
            try:
                # Log current commands before sync
                commands_before = [cmd.name for cmd in self.bot.tree.get_commands()]
                logger.info(f"Commands before sync: {commands_before}")

                # First sync to the global scope
                logger.info("Attempting global sync...")
                try:
                    synced = await self.bot.tree.sync()
                    logger.info(f"Global sync completed. Synced {len(synced)} command(s): {[cmd.name for cmd in synced]}")
                except discord.Forbidden as e:
                    logger.error(f"Bot lacks permissions to sync commands globally: {e}")
                    raise
                except discord.HTTPException as e:
                    logger.error(f"Discord API error during global sync: {e}")
                    raise
                
                # Then sync to the guild scope if we're in dev mode
                if getattr(self.bot, 'dev_mode', False):
                    logger.info("Bot is in dev mode, syncing to guilds...")
                    for guild in self.bot.guilds:
                        logger.info(f"Syncing commands to guild {guild.id} ({guild.name})...")
                        try:
                            guild_synced = await self.bot.tree.sync(guild=guild)
                            logger.info(f"Successfully synced {len(guild_synced)} command(s) to guild {guild.id}: {[cmd.name for cmd in guild_synced]}")
                        except discord.Forbidden as e:
                            logger.error(f"Bot lacks permissions to sync commands to guild {guild.id}: {e}")
                            continue
                        except discord.HTTPException as e:
                            logger.error(f"Discord API error syncing to guild {guild.id}: {e}")
                            continue
                        except Exception as guild_sync_error:
                            logger.error(f"Failed to sync commands to guild {guild.id}: {guild_sync_error}", exc_info=True)
                            continue
                else:
                    logger.info("Bot is in production mode, skipping guild sync")
                
                # Log commands after sync
                commands_after = [cmd.name for cmd in self.bot.tree.get_commands()]
                logger.info(f"Commands after sync: {commands_after}")
                
                self._commands_synced = True
                logger.info("Successfully completed all command syncing")
            except Exception as e:
                logger.error(f"Failed to sync commands: {e}", exc_info=True)
                # Don't set _commands_synced to True so we can retry on next ready event
                raise  # Re-raise to ensure we know if sync fails

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user: return
        if message.guild is not None: return

        if not await self.bot.is_owner(message.author):
            logger.info(f"Received DM from non-owner user {message.author.id}. Replying with robot sounds.")
            robot_sounds = ["beep", "boop", "blarp", "zorp", "clank", "whirr", "buzz", "vroom"]
            try:
                reply_content = " ".join(random.sample(robot_sounds, 3)).capitalize() + "."
                await message.channel.send(reply_content) # Keep this direct send for simplicity
            except discord.Forbidden:
                logger.warning(f"Cannot send robot sounds DM reply to {message.author.id}.")
            except Exception as e:
                logger.error(f"Failed to send robot sounds DM reply to {message.author.id}: {e}", exc_info=True)
            return

        logger.info(f"Received DM from owner {message.author.id}. Sending admin dashboard.")
        try:
            # UPDATED CALL for sending Admin Dashboard DM
            if not hasattr(self.bot, 'rate_limiter'):
                logger.error("Rate limiter not found on self.bot for AdminCog. Sending dashboard directly.")
                await message.channel.send("Admin Dashboard:", view=AdminDashboardView(self.bot))
            else:
                await discord_utils.safe_send_message(
                    self.bot,
                    message.channel, # DM channel with the owner
                    self.bot.rate_limiter,
                    logger, # Global logger from the file
                    content="Admin Dashboard:",
                    view=AdminDashboardView(self.bot)
                )
        except discord.Forbidden:
             logger.warning(f"Cannot send admin dashboard DM to {message.author.id}. Missing permissions or user blocked bot?")
        except Exception as e:
             logger.error(f"Failed to send admin dashboard to {message.author.id}: {e}", exc_info=True)

    @app_commands.command(name="update_details", description="Update your social media handles and website link.")
    async def update_details(self, interaction: discord.Interaction):
        """Allows a user to update their social media details via a modal."""
        logger.info(f"update_details command triggered by user {interaction.user.id} in {'DM' if interaction.guild is None else f'guild {interaction.guild.id}'}")
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
    """Sets up the AdminCog."""
    logger.info("Setting up AdminCog")
    # Check for db_handler dependency
    if not hasattr(bot, 'db_handler'):
         logger.error("Cannot setup AdminCog: db_handler not found on bot instance.")
         return

    # Add the AdminCog instance
    await bot.add_cog(AdminCog(bot))
    logger.info("AdminCog added to bot")

    # Add the persistent view *once*
    if not AdminCog._persistent_view_added:
        if not hasattr(bot, 'owner_ids') and not bot.owner_id:
            logger.error("Cannot add AdminDashboardView: Bot owner ID(s) are required.")
        else:
            bot.add_view(AdminDashboardView(bot))
            AdminCog._persistent_view_added = True
            logger.info("AdminDashboardView added as a persistent view.") 