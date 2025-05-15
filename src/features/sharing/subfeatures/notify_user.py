# Placeholder for notify_user functions 

import discord
import logging
from discord.ext import commands
from src.common.db_handler import DatabaseHandler
# Import Sharer using a relative path or adjust sys.path if needed
# This assumes sharer.py is in the parent directory
# If sharer.py is in src/features/sharing/sharer.py:
# from ..sharer import Sharer # REMOVED to break circular import
import asyncio
from typing import TYPE_CHECKING
import os # Added

# Use TYPE_CHECKING block for type hint only
if TYPE_CHECKING:
    from ..sharer import Sharer

logger = logging.getLogger('DiscordBot')

# Helper to format the user details into a readable string for the DM
def _format_user_details_md(user_details: dict) -> str:
    details = f"""
**Sharing Preferences:**
- **Okay to feature on social?** {'✅ Yes' if user_details.get('sharing_consent', False) else '❌ No'}
- **Okay to curate to OpenMuse?** {'✅ Yes' if user_details.get('permission_to_curate', False) else '❌ No'}
- **Receive these DMs?** {'✅ Yes' if user_details.get('dm_preference', True) else '❌ No'}

**Your Socials:** (Edit these below!)
- **Twitter:** {user_details.get('twitter_handle') or 'Not set'}
- **Instagram:** {user_details.get('instagram_handle') or 'Not set'}
- **YouTube:** {user_details.get('youtube_handle') or 'Not set'}
- **TikTok:** {user_details.get('tiktok_handle') or 'Not set'}
- **Website:** {user_details.get('website') or 'Not set'}
"""
    return details.strip()

# Helper to format the main DM message
def _format_dm_message(message: discord.Message, user_details: dict) -> str:
    details_md = _format_user_details_md(user_details)
    msg = f"""Hey {message.author.mention}!

Your message ({message.jump_url}) was flagged and might be featured on our social channels!

{details_md}

Please review and update your details/preferences below. Clicking 'Allow Feature' gives us permission to post messages from you externally - unless you change this.
"""
    return msg.strip()


# --- Discord UI Components ---

class UpdateSocialsModal(discord.ui.Modal, title='Update Your Preferences'):
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
    permission_to_share_input = discord.ui.TextInput(
        label='Okay to share on social? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True,
        max_length=3,
        min_length=2
    )

    permission_to_curate_input = discord.ui.TextInput(
        label='Okay to share on OpenMuse? (yes/no)',
        placeholder='Type "yes" or "no"',
        required=True,
        max_length=3,
        min_length=2
    )

    def __init__(self, user_details: dict, db_handler: DatabaseHandler, original_message: discord.Message, parent_view: 'SharingRequestView'):
        super().__init__()
        self.user_details = user_details
        self.db_handler = db_handler
        self.original_message = original_message # Message that triggered the DM
        self.parent_view = parent_view # To update the original DM view

        # Pre-fill modal
        self.twitter_input.default = user_details.get('twitter_handle')
        self.instagram_input.default = user_details.get('instagram_handle')
        self.youtube_input.default = user_details.get('youtube_handle')

        # Pre-fill new permission inputs based on DB fields (sharing_consent, permission_to_curate)
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
            else:
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
            else:
                await interaction.response.send_message(
                    "Invalid input for 'Okay to share on OpenMuse?'. Please enter 'yes' or 'no'.", 
                    ephemeral=True
                )
                return

            updated_data = {
                'twitter_handle': self.twitter_input.value.strip() or None,
                'instagram_handle': self.instagram_input.value.strip() or None,
                'youtube_handle': self.youtube_input.value.strip() or None,
                'sharing_consent': final_sharing_consent,
                'permission_to_curate': final_permission_to_curate,
            }
            
            # Update DB
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name, 
                global_name=interaction.user.global_name,
                **updated_data # Pass all updated data, including new permissions
            )

            if success:
                new_details = self.db_handler.get_member(interaction.user.id)
                if new_details:
                    self.parent_view.user_details = new_details
                    await interaction.response.edit_message(
                        content=_format_dm_message(self.original_message, new_details),
                        view=self.parent_view
                    )
                    logger.info(f"User {interaction.user.id} updated preferences via modal for message {self.original_message.id}. Data: {updated_data}")
                else:
                    # This case should ideally not happen if create_or_update_member was successful
                    # and get_member is reliable.
                    await interaction.response.send_message("Preferences updated, but failed to refresh display. Please try again or check your DMs.", ephemeral=True)
                    logger.error(f"User {interaction.user.id} updated preferences, but new_details fetch failed for msg {self.original_message.id}.")
            else:
                 await interaction.response.send_message("Failed to update your preferences in the database.", ephemeral=True)
                 logger.error(f"Failed DB update for user {interaction.user.id} preferences via modal.")

        except Exception as e:
            logger.error(f"Error in UpdateSocialsModal on_submit for user {interaction.user.id}: {e}", exc_info=True)
            # Check if interaction is already responded to before sending another response
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while updating your preferences.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred after the initial response while updating your preferences.", ephemeral=True)

class SharingRequestView(discord.ui.View):
    def __init__(self, user_details: dict, db_handler: DatabaseHandler, sharer_instance: 'Sharer', original_message: discord.Message, timeout=1800): # Timeout 30 mins
        super().__init__(timeout=timeout)
        self.user_details = user_details
        self.db_handler = db_handler
        self.sharer_instance = sharer_instance
        self.original_message = original_message
        self._update_button_states()

    def _update_button_states(self):
        # Update button labels based on current state
        consent_button = self.get_item("toggle_consent")
        if consent_button:
            consent_button.label = "Allow Feature" if not self.user_details.get('sharing_consent', False) else "Revoke Feature"
            consent_button.style = discord.ButtonStyle.success if not self.user_details.get('sharing_consent', False) else discord.ButtonStyle.danger

        curation_button = self.get_item("toggle_curation")
        if curation_button:
            curation_button.label = "Allow Curation" if not self.user_details.get('permission_to_curate', False) else "Revoke Curation"
            curation_button.style = discord.ButtonStyle.success if not self.user_details.get('permission_to_curate', False) else discord.ButtonStyle.danger

        dm_button = self.get_item("toggle_dms")
        if dm_button:
            dm_button.label = "Disable DMs" if self.user_details.get('dm_preference', True) else "Enable DMs"
            dm_button.style = discord.ButtonStyle.danger if self.user_details.get('dm_preference', True) else discord.ButtonStyle.success

    async def _update_db_and_view(self, interaction: discord.Interaction, updates: dict):
        try:
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name,
                global_name=interaction.user.global_name,
                **updates
            )
            if success:
                # Refresh user details from DB
                self.user_details = self.db_handler.get_member(interaction.user.id)
                self._update_button_states() # Update button appearance
                await interaction.response.edit_message(
                    content=_format_dm_message(self.original_message, self.user_details),
                    view=self
                )
                update_keys = ', '.join(updates.keys())
                logger.info(f"User {interaction.user.id} updated preferences ({update_keys}) for message {self.original_message.id}.")
            else:
                await interaction.response.send_message("Failed to update your preferences in the database.", ephemeral=True)
                logger.error(f"Failed DB update for user {interaction.user.id} preferences.")
        except Exception as e:
            logger.error(f"Error in _update_db_and_view for user {interaction.user.id}: {e}", exc_info=True)
            await interaction.response.send_message("An error occurred while updating your preferences.", ephemeral=True)

    @discord.ui.button(label="Toggle Consent", style=discord.ButtonStyle.success, custom_id="toggle_consent", row=0)
    async def toggle_consent_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_consent = not self.user_details.get('sharing_consent', False)
        await self._update_db_and_view(interaction, {'sharing_consent': new_consent})
        
        # If consent is now True, trigger the finalize_sharing process
        if new_consent:
             logger.info(f"User {interaction.user.id} GRANTED sharing consent for message {self.original_message.id}. Triggering finalize.")
             # Pass channel_id along with user_id and message_id
             asyncio.create_task(self.sharer_instance.finalize_sharing(interaction.user.id, self.original_message.id, self.original_message.channel.id))
        else:
             logger.info(f"User {interaction.user.id} REVOKED sharing consent for message {self.original_message.id}.")

    @discord.ui.button(label="Toggle Curation", style=discord.ButtonStyle.success, custom_id="toggle_curation", row=0)
    async def toggle_curation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_curation = not self.user_details.get('permission_to_curate', False)
        await self._update_db_and_view(interaction, {'permission_to_curate': new_curation})
        
        if new_curation:
            logger.info(f"User {interaction.user.id} GRANTED curation permission for message {self.original_message.id}.")
        else:
            logger.info(f"User {interaction.user.id} REVOKED curation permission for message {self.original_message.id}.")

    @discord.ui.button(label="Edit Socials", style=discord.ButtonStyle.primary, custom_id="edit_socials", emoji="📝", row=0)
    async def edit_socials_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Pass the current user details, db_handler, original message, and this view instance
        modal = UpdateSocialsModal(
             user_details=self.user_details,
             db_handler=self.db_handler,
             original_message=self.original_message,
             parent_view=self
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle DMs", style=discord.ButtonStyle.secondary, custom_id="toggle_dms", row=1)
    async def toggle_dm_preference_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_dm_pref = not self.user_details.get('dm_preference', True)
        await self._update_db_and_view(interaction, {'dm_preference': new_dm_pref})

    async def on_timeout(self):
        user_id = self.user_details.get('member_id')
        message_id = self.original_message.id if self.original_message else None
        channel_id = self.original_message.channel.id if self.original_message else None # Get channel ID
        sharer = self.sharer_instance

        # Check if consent is still True when the view times out
        if user_id and message_id and channel_id and sharer and self.user_details.get('sharing_consent', False):
            logger.info(f"Sharing request DM timed out for user {user_id}, message {message_id}. Consent is TRUE. Triggering finalize_sharing automatically.")
            # Trigger finalize_sharing asynchronously, passing channel_id
            asyncio.create_task(sharer.finalize_sharing(user_id, message_id, channel_id))
        else:
            consent_status = self.user_details.get('sharing_consent', 'Unknown')
            logger.info(f"Sharing request DM timed out for user {user_id}, message {message_id}. Consent is {consent_status}. Finalize not triggered automatically.")
            if not user_id or not message_id or not channel_id or not sharer:
                 logger.warning(f"Missing data needed for automatic finalize on timeout: user_id={user_id}, message_id={message_id}, channel_id={channel_id}, sharer_present={sharer is not None}")

        # Disable buttons and edit message regardless of consent status
        for item in self.children:
            item.disabled = True
        try:
            # Edit the original DM message if possible
            dm_message = getattr(self, 'message', None) # Get the sent DM message reference
            if dm_message:
                await dm_message.edit(content=f"{dm_message.content}\n\n_(This interaction has expired.)_", view=self)
            else:
                 logger.warning(f"Could not edit DM for user {user_id} after timeout (DM message reference not found on view). Message ID {message_id}")
        except discord.NotFound:
            logger.warning(f"Could not edit DM for user {user_id} after timeout (message not found). Message ID {message_id}")
        except Exception as e:
            logger.error(f"Error editing DM on timeout for user {user_id}, message ID {message_id}: {e}", exc_info=True)

# --- Main Function ---

async def send_sharing_request_dm(bot: commands.Bot, user: discord.User, message: discord.Message, db_handler: DatabaseHandler, sharer_instance: 'Sharer'):
    """Sends a DM asking for sharing consent. In dev mode, redirects DM to ADMIN_USER_ID."""
    
    original_author = user # Keep track of the original author
    target_user = user # Default target is the original author
    is_redirected = False

    # --- Dev Mode Redirect Logic --- 
    if bot.dev_mode:
        logger.debug("Dev mode active. Checking for ADMIN_USER_ID to redirect DM.")
        admin_user_id_str = os.getenv('ADMIN_USER_ID')
        if admin_user_id_str:
            try:
                admin_user_id = int(admin_user_id_str)
                admin_user = await bot.fetch_user(admin_user_id)
                if admin_user:
                    target_user = admin_user
                    is_redirected = True
                    logger.info(f"Redirecting sharing request DM for user {original_author.id} (msg: {message.id}) to ADMIN_USER_ID {admin_user_id}.")
                else:
                    logger.error(f"Could not fetch admin user with ID {admin_user_id}. Sending DM to original author.")
            except ValueError:
                logger.error(f"Invalid ADMIN_USER_ID format: '{admin_user_id_str}'. Sending DM to original author.")
            except discord.NotFound:
                logger.error(f"Admin user with ID {admin_user_id_str} not found. Sending DM to original author.")
            except Exception as e:
                logger.error(f"Error fetching admin user {admin_user_id_str}: {e}. Sending DM to original author.")
        else:
            logger.warning("Dev mode active but ADMIN_USER_ID not set in .env. Sending DM to original author.")
    # --- End Redirect Logic --- 

    if target_user.bot:
        # Check if the *final* target is a bot (could be the admin)
        logger.warning(f"Attempted to send sharing request DM to bot user {target_user.id}. Skipping.")
        return

    try:
        # Fetch details for the *original* author to show correct info
        user_details = db_handler.get_member(original_author.id)

        if not user_details:
            logger.info(f"User {original_author.id} not found in DB. Creating entry with default consent=True.")
            db_handler.create_or_update_member(
                member_id=original_author.id,
                username=original_author.name,
                global_name=original_author.global_name,
                sharing_consent=True,
                dm_preference=True 
            )
            user_details = db_handler.get_member(original_author.id) # Re-fetch
            if not user_details:
                 logger.error(f"Failed to create DB entry for user {original_author.id} during sharing request.")
                 return

        # Check DM preference of the *original* author, even if redirecting
        # We might still want to respect their preference about initiating the process
        # Alternatively, you could ignore this check in dev mode if desired.
        if not user_details.get('dm_preference', True):
            logger.info(f"Original author {original_author.id} has DMs disabled. Skipping sharing request DM for message {message.id} (even if redirected).")
            return

        # Create DM with the *target* user (original author or admin)
        dm_channel = await target_user.create_dm()
        
        # The view still operates on the *original* author's details and message
        view = SharingRequestView(user_details, db_handler, sharer_instance, message)
        
        # Format message using original author details
        dm_message_content = _format_dm_message(message, user_details)
        
        # Add a note if redirected
        if is_redirected:
            dm_message_content = f"**(DEV MODE: This DM was intended for {original_author.name} ({original_author.id}))**\n\n" + dm_message_content

        sent_dm = await dm_channel.send(content=dm_message_content, view=view)
        view.message = sent_dm # Store reference for timeout editing
        if is_redirected:
            logger.info(f"Sent REDIRECTED sharing request DM to admin {target_user.id} (originally for {original_author.id}, message {message.id}).")
        else:
            logger.info(f"Sent sharing request DM to user {target_user.id} for message {message.id}.")

    except discord.Forbidden:
        logger.warning(f"Could not send DM to target user {target_user.id} (Forbidden). They may have DMs disabled globally or blocked the bot.")
        # If redirected, log who it was originally for
        if is_redirected:
             logger.warning(f"(DM was originally intended for {original_author.id})")
    except Exception as e:
        logger.error(f"Failed to send sharing request DM (target: {target_user.id}, original: {original_author.id}, msg: {message.id}): {e}", exc_info=True) 