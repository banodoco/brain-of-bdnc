# src/features/admin/admin_cog.py
import discord
import logging
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime
import random

from src.common.db_handler import DatabaseHandler
from src.common import discord_utils
from src.common.supabase_sync_handler import SupabaseSyncHandler

logger = logging.getLogger('DiscordBot')

# --- Modal for Updating Socials --- 
class AdminUpdateSocialsModal(discord.ui.Modal):
    twitter_input = discord.ui.TextInput(
        label='Twitter Handle (e.g., @username)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    reddit_input = discord.ui.TextInput(
        label='Reddit Username (e.g., u/username)',
        required=False,
        placeholder='Leave blank to remove',
        max_length=100
    )
    include_in_updates_input = discord.ui.TextInput(
        label='Include in updates/transcripts? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True,
        max_length=3,
        min_length=2 
    )
    allow_content_sharing_input = discord.ui.TextInput(
        label='Okay to share my content? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True,
        max_length=3,
        min_length=2
    )

    def __init__(self, user_details: dict, db_handler: DatabaseHandler, bot=None):
        super().__init__(title='Update Your Preferences')
        self.user_details = user_details
        self.db_handler = db_handler
        self.bot = bot  # Store bot for role updates

        # Pre-fill modal
        self.twitter_input.default = user_details.get('twitter_handle')
        self.reddit_input.default = user_details.get('reddit_handle')

        # Pre-fill permission inputs based on DB fields
        # Note: these default to TRUE in DB, so None or True = "Yes", only False = "No"
        include_updates = user_details.get('include_in_updates')
        allow_sharing = user_details.get('allow_content_sharing')
        self.include_in_updates_input.default = "No" if include_updates is False else "Yes"
        self.allow_content_sharing_input.default = "No" if allow_sharing is False else "Yes"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            include_updates_raw = self.include_in_updates_input.value.strip().lower()
            allow_sharing_raw = self.allow_content_sharing_input.value.strip().lower()

            final_include_in_updates = None
            final_allow_content_sharing = None

            # Validate and convert for include_in_updates
            if include_updates_raw == 'yes':
                final_include_in_updates = True
            elif include_updates_raw == 'no':
                final_include_in_updates = False
            else:
                await interaction.response.send_message(
                    "Invalid input for 'Include in updates/transcripts?'. Please enter 'yes' or 'no'.", 
                    ephemeral=True
                )
                return

            # Validate and convert for allow_content_sharing
            if allow_sharing_raw == 'yes':
                final_allow_content_sharing = True
            elif allow_sharing_raw == 'no':
                final_allow_content_sharing = False
            else:
                await interaction.response.send_message(
                    "Invalid input for 'Okay to share my content?'. Please enter 'yes' or 'no'.", 
                    ephemeral=True
                )
                return
            
            updated_data = {
                'twitter_handle': self.twitter_input.value.strip() or None,
                'reddit_handle': self.reddit_input.value.strip() or None,
                'include_in_updates': final_include_in_updates,
                'allow_content_sharing': final_allow_content_sharing,
            }
            
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name, 
                global_name=interaction.user.global_name,
                **updated_data
            )

            if success:
                # Update the "no sharing" role based on the new allow_content_sharing value
                if self.bot:
                    await discord_utils.update_no_sharing_role(
                        self.bot, interaction.user.id, final_allow_content_sharing, logger
                    )
                
                await interaction.response.send_message("Your preferences have been updated successfully!", ephemeral=True)
                logger.info(f"User {interaction.user.id} updated preferences via /update_details. Data: {updated_data}")
            else:
                 await interaction.response.send_message("Failed to update your preferences. Please try again later.", ephemeral=True)
                 logger.error(f"Failed DB update for user {interaction.user.id} preferences via /update_details.")

        except Exception as e:
            logger.error(f"Error in AdminUpdateSocialsModal on_submit for user {interaction.user.id}: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while updating your preferences.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred after the initial response while updating your preferences.", ephemeral=True)

# --- Admin Cog Class --- 
class AdminCog(commands.Cog):
    _commands_synced = False  # Add flag to track if commands have been synced

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_handler = bot.db_handler if hasattr(bot, 'db_handler') else None
        # Initialize Supabase sync handler
        self.supabase_sync = SupabaseSyncHandler(
            self.db_handler, 
            logger, 
            sync_interval=300  # 5 minutes
        ) if self.db_handler else None
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
                except discord.app_commands.errors.CommandSyncFailure as e:
                    error_str = str(e)
                    if "redirect_uris" in error_str:
                        # This is a Discord Developer Portal OAuth2 configuration issue
                        # Log once and continue - commands may still work from cache
                        logger.warning(
                            f"Command sync skipped due to Discord OAuth2 config issue. "
                            f"Fix in Discord Developer Portal > OAuth2 > Redirects. Error: {error_str[:100]}"
                        )
                        # Don't raise - this is an external config issue, not a code bug
                    else:
                        logger.error(f"Command sync failure: {e}")
                        raise
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
        
        # Auto-start Supabase background sync (only if not using direct writes)
        if self.supabase_sync and not self.supabase_sync.direct_writes_enabled:
            if not self.supabase_sync.get_sync_status()['is_running']:
                try:
                    logger.info("Auto-starting Supabase background sync...")
                    success = await self.supabase_sync.start_background_sync()
                    if success:
                        logger.info("✅ Supabase background sync started automatically on bot ready")
                    else:
                        logger.warning("❌ Failed to auto-start Supabase background sync")
                except Exception as sync_error:
                    logger.error(f"Error auto-starting Supabase sync: {sync_error}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user: return
        if message.guild is not None: return

        # Check if this DM should go to admin chat instead
        admin_user_id_str = os.getenv('ADMIN_USER_ID')
        if admin_user_id_str:
            try:
                admin_user_id = int(admin_user_id_str)
                if message.author.id == admin_user_id:
                    # This DM is for admin chat - let AdminChatCog handle it
                    return
            except ValueError:
                pass  # Invalid ADMIN_USER_ID, continue with normal flow

        # Reply with robot sounds to all other DMs
        logger.info(f"Received DM from user {message.author.id}. Replying with robot sounds.")
        robot_sounds = ["beep", "boop", "blarp", "zorp", "clank", "whirr", "buzz", "vroom"]
        try:
            reply_content = " ".join(random.sample(robot_sounds, 3)).capitalize() + "."
            await message.channel.send(reply_content)
        except discord.Forbidden:
            logger.warning(f"Cannot send robot sounds DM reply to {message.author.id}.")
        except Exception as e:
            logger.error(f"Failed to send robot sounds DM reply to {message.author.id}: {e}", exc_info=True)

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
                     'reddit_handle': None,
                     'include_in_updates': None,  # Will default to TRUE behavior
                     'allow_content_sharing': None,  # Will default to TRUE behavior
                }
            
            # Create and send the modal, pass bot for role updates
            modal = AdminUpdateSocialsModal(user_details=user_details, db_handler=self.db_handler, bot=self.bot)
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

    @app_commands.command(name="supabase_sync", description="Manually trigger Supabase sync (Admin only)")
    @app_commands.describe(
        sync_type="Type of sync to perform",
        limit="Limit number of records to sync (for testing)"
    )
    @app_commands.choices(sync_type=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Messages", value="messages"),
        app_commands.Choice(name="Members", value="members"),
        app_commands.Choice(name="Channels", value="channels")
    ])
    async def supabase_sync(self, interaction: discord.Interaction, sync_type: str = "all", limit: int = None):
        """Manually trigger a Supabase sync operation."""
        # Check if user is bot owner
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        
        if not self.supabase_sync:
            await interaction.response.send_message("Supabase sync is not available (missing database handler or credentials).", ephemeral=True)
            return
        
        # Defer the response since sync might take time
        await interaction.response.defer(ephemeral=True)
        
        try:
            logger.info(f"Admin {interaction.user.id} triggered manual Supabase sync: {sync_type}, limit: {limit}")
            
            # Perform the sync
            results = await self.supabase_sync.manual_sync(sync_type, limit)
            
            # Create response embed
            embed = discord.Embed(
                title="Supabase Sync Results",
                color=discord.Color.green() if sum(results.values()) > 0 else discord.Color.orange()
            )
            
            total_synced = sum(results.values())
            if total_synced > 0:
                embed.description = f"Successfully synced {total_synced} records to Supabase."
                for data_type, count in results.items():
                    if count > 0:
                        embed.add_field(name=data_type.title(), value=f"{count} records", inline=True)
            else:
                embed.description = "No new records to sync."
            
            # Add sync info
            sync_status = self.supabase_sync.get_sync_status()
            embed.add_field(
                name="Sync Status",
                value=f"Background sync: {'Running' if sync_status['is_running'] else 'Stopped'}",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error during manual Supabase sync: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="Sync Error",
                description=f"An error occurred during sync: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @app_commands.command(name="supabase_status", description="Check Supabase sync status (Admin only)")
    async def supabase_status(self, interaction: discord.Interaction):
        """Check the status of Supabase sync."""
        # Check if user is bot owner
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        
        if not self.supabase_sync:
            await interaction.response.send_message("Supabase sync is not available (missing database handler or credentials).", ephemeral=True)
            return
        
        try:
            # Test connection first
            connection_ok = await self.supabase_sync.test_connection()
            
            # Get sync status
            status = self.supabase_sync.get_sync_status()
            
            # Create status embed
            embed = discord.Embed(
                title="Supabase Sync Status",
                color=discord.Color.green() if connection_ok else discord.Color.red()
            )
            
            embed.add_field(
                name="Connection",
                value="✅ Connected" if connection_ok else "❌ Connection failed",
                inline=True
            )
            
            embed.add_field(
                name="Background Sync",
                value="🟢 Running" if status['is_running'] else "🔴 Stopped",
                inline=True
            )
            
            embed.add_field(
                name="Sync Interval",
                value=f"{status['sync_interval']} seconds",
                inline=True
            )
            
            if status['last_sync_time']:
                last_sync = datetime.fromisoformat(status['last_sync_time'].replace('Z', '+00:00'))
                embed.add_field(
                    name="Last Sync",
                    value=f"<t:{int(last_sync.timestamp())}:R>",
                    inline=True
                )
            
            if status['next_sync_in'] is not None:
                next_sync_seconds = int(status['next_sync_in'])
                if next_sync_seconds > 0:
                    embed.add_field(
                        name="Next Sync",
                        value=f"In {next_sync_seconds} seconds",
                        inline=True
                    )
                else:
                    embed.add_field(
                        name="Next Sync",
                        value="Due now",
                        inline=True
                    )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error checking Supabase status: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="Status Check Error",
                description=f"An error occurred while checking status: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)

    @app_commands.command(name="supabase_toggle", description="Start/stop background Supabase sync (Admin only)")
    async def supabase_toggle(self, interaction: discord.Interaction):
        """Toggle the background Supabase sync on/off."""
        # Check if user is bot owner
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        
        if not self.supabase_sync:
            await interaction.response.send_message("Supabase sync is not available (missing database handler or credentials).", ephemeral=True)
            return
        
        try:
            status = self.supabase_sync.get_sync_status()
            
            if status['is_running']:
                # Stop the sync
                await self.supabase_sync.stop_background_sync()
                embed = discord.Embed(
                    title="Background Sync Stopped",
                    description="Supabase background sync has been stopped.",
                    color=discord.Color.orange()
                )
                logger.info(f"Admin {interaction.user.id} stopped background Supabase sync")
            else:
                # Start the sync
                success = await self.supabase_sync.start_background_sync()
                if success:
                    embed = discord.Embed(
                        title="Background Sync Started",
                        description=f"Supabase background sync has been started with {status['sync_interval']}s intervals.",
                        color=discord.Color.green()
                    )
                    logger.info(f"Admin {interaction.user.id} started background Supabase sync")
                else:
                    embed = discord.Embed(
                        title="Failed to Start Sync",
                        description="Failed to start background sync. Check logs for details.",
                        color=discord.Color.red()
                    )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error toggling Supabase sync: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="Toggle Error",
                description=f"An error occurred while toggling sync: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)

    @app_commands.command(name="delete_user_messages", description="Delete all stored messages from a user (Admin only)")
    @app_commands.describe(
        user_id="The Discord user ID whose messages should be deleted",
        dry_run="Preview how many messages would be deleted without actually deleting them"
    )
    async def delete_user_messages(self, interaction: discord.Interaction, user_id: str, dry_run: bool = True):
        """Delete all messages from a specific user in the database."""
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return

        if not self.db_handler or not self.db_handler.storage_handler or not self.db_handler.storage_handler.supabase_client:
            await interaction.response.send_message("Database connection is unavailable.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            client = self.db_handler.storage_handler.supabase_client

            # Look up the user
            member = client.table('discord_members').select('username,global_name,server_nick').eq('member_id', user_id).execute()
            display_name = "Unknown user"
            if member.data:
                m = member.data[0]
                display_name = m.get('server_nick') or m.get('global_name') or m.get('username') or display_name

            # Count messages
            count_result = client.table('discord_messages').select('message_id', count='exact').eq('author_id', user_id).execute()
            message_count = count_result.count if count_result.count is not None else len(count_result.data)

            if message_count == 0:
                await interaction.followup.send(f"No messages found for user `{user_id}` ({display_name}).", ephemeral=True)
                return

            if dry_run:
                embed = discord.Embed(
                    title="Dry Run — Delete User Messages",
                    description=f"**{message_count}** messages found for **{display_name}** (`{user_id}`).\n\nRe-run with `dry_run: False` to delete them.",
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                result = client.table('discord_messages').delete().eq('author_id', user_id).execute()
                deleted_count = len(result.data)
                logger.info(f"Admin {interaction.user.id} deleted {deleted_count} messages from user {user_id} ({display_name})")

                embed = discord.Embed(
                    title="Messages Deleted",
                    description=f"Deleted **{deleted_count}** messages from **{display_name}** (`{user_id}`).",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in delete_user_messages for user {user_id}: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

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