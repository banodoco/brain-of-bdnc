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

# Use TYPE_CHECKING block for type hint only
if TYPE_CHECKING:
    from ..sharer import Sharer

logger = logging.getLogger('DiscordBot')

# Helper to format the user details into a readable string for the DM
def _format_user_details_md(user_details: dict) -> str:
    details = f"""
**Sharing Preferences:**
- **Okay to Feature?** {'✅ Yes' if user_details.get('sharing_consent', False) else '❌ No'}
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

Please review and update your details/preferences below. Clicking 'Allow Feature' gives us permission to post this specific message's content/attachments externally.
"""
    return msg.strip()


# --- Discord UI Components ---

class UpdateSocialsModal(discord.ui.Modal, title='Update Social Handles & Website'):
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
        self.tiktok_input.default = user_details.get('tiktok_handle')
        self.website_input.default = user_details.get('website')

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Update details in the dictionary first
            updated_data = {
                'twitter_handle': self.twitter_input.value.strip() or None,
                'instagram_handle': self.instagram_input.value.strip() or None,
                'youtube_handle': self.youtube_input.value.strip() or None,
                'tiktok_handle': self.tiktok_input.value.strip() or None,
                'website': self.website_input.value.strip() or None,
            }
            
            # Update DB
            success = self.db_handler.create_or_update_member(
                member_id=interaction.user.id,
                username=interaction.user.name, # Keep username sync (optional)
                global_name=interaction.user.global_name,
                # Pass only updated social fields
                **updated_data
                # Keep existing consent/prefs
            )

            if success:
                # Fetch potentially updated user details to refresh the view
                new_details = self.db_handler.get_member(interaction.user.id)
                self.parent_view.user_details = new_details # Update parent view's state

                # Edit the original DM message with updated info and refreshed view
                await interaction.response.edit_message(
                    content=_format_dm_message(self.original_message, new_details),
                    view=self.parent_view
                )
                logger.info(f"User {interaction.user.id} updated social details via modal for message {self.original_message.id}.")
            else:
                 await interaction.response.send_message("Failed to update your details in the database.", ephemeral=True)
                 logger.error(f"Failed DB update for user {interaction.user.id} social details via modal.")

        except Exception as e:
            logger.error(f"Error in UpdateSocialsModal on_submit for user {interaction.user.id}: {e}", exc_info=True)
            await interaction.response.send_message("An error occurred while updating your details.", ephemeral=True)

class SharingRequestView(discord.ui.View):
    def __init__(self, user_details: dict, db_handler: DatabaseHandler, sharer_instance: 'Sharer', original_message: discord.Message, timeout=1800): # Timeout 30 mins
        super().__init__(timeout=timeout)
        self.user_details = user_details
        self.db_handler = db_handler
        self.sharer_instance = sharer_instance # Store the Sharer instance
        self.original_message = original_message # Message that triggered the DM
        self._update_button_states()

    # Dynamically update button labels/styles based on current state
    def _update_button_states(self):
        # Consent Button
        consent_button = self.children[0] # Assuming it's the first button
        if self.user_details.get('sharing_consent', False):
            consent_button.label = "Revoke Feature Consent"
            consent_button.style = discord.ButtonStyle.danger
            consent_button.emoji = "❌"
        else:
            consent_button.label = "Allow Feature"
            consent_button.style = discord.ButtonStyle.success
            consent_button.emoji = "✅"

        # DM Preference Button
        dm_pref_button = self.children[2] # Assuming it's the third button
        if self.user_details.get('dm_preference', True):
            dm_pref_button.label = "Disable These DMs"
            dm_pref_button.style = discord.ButtonStyle.secondary
            dm_pref_button.emoji = "🔕"
        else:
            dm_pref_button.label = "Enable These DMs"
            dm_pref_button.style = discord.ButtonStyle.primary
            dm_pref_button.emoji = "🔔"

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
             # Ensure finalize_sharing is called asynchronously without blocking the interaction response
             # interaction.response is already handled by _update_db_and_view
             asyncio.create_task(self.sharer_instance.finalize_sharing(interaction.user.id, self.original_message.id))
             # Optional: Give immediate feedback
             # await interaction.followup.send("Thanks! We'll start processing your post for sharing.", ephemeral=True)
        else:
             logger.info(f"User {interaction.user.id} REVOKED sharing consent for message {self.original_message.id}.")


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
        sharer = self.sharer_instance

        # Check if consent is still True when the view times out
        if user_id and message_id and sharer and self.user_details.get('sharing_consent', False):
            logger.info(f"Sharing request DM timed out for user {user_id}, message {message_id}. Consent is TRUE. Triggering finalize_sharing automatically.")
            # Trigger finalize_sharing asynchronously
            asyncio.create_task(sharer.finalize_sharing(user_id, message_id))
        else:
            consent_status = self.user_details.get('sharing_consent', 'Unknown')
            logger.info(f"Sharing request DM timed out for user {user_id}, message {message_id}. Consent is {consent_status}. Finalize not triggered automatically.")
            if not user_id or not message_id or not sharer:
                 logger.warning(f"Missing data needed for automatic finalize on timeout: user_id={user_id}, message_id={message_id}, sharer_present={sharer is not None}")

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
    """Sends a DM to the user asking for sharing consent and social details."""
    if user.bot:
        logger.warning(f"Attempted to send sharing request DM to bot user {user.id}. Skipping.")
        return

    try:
        user_details = db_handler.get_member(user.id)

        if not user_details:
            # User not in DB, create with defaults (consent=True, dm_pref=True)
            logger.info(f"User {user.id} not found in DB. Creating entry with default consent=True.")
            db_handler.create_or_update_member(
                member_id=user.id,
                username=user.name,
                global_name=user.global_name,
                sharing_consent=True, # Default to True
                dm_preference=True    # Default to True
            )
            user_details = db_handler.get_member(user.id) # Re-fetch after creation
            if not user_details: # Check if creation failed
                 logger.error(f"Failed to create DB entry for user {user.id} during sharing request.")
                 # Maybe notify admin channel?
                 return

        # Check DM preference before sending
        if not user_details.get('dm_preference', True):
            logger.info(f"User {user.id} has DMs disabled. Skipping sharing request DM for message {message.id}.")
            return

        dm_channel = await user.create_dm()
        # Pass sharer_instance to the view
        view = SharingRequestView(user_details, db_handler, sharer_instance, message)
        dm_message_content = _format_dm_message(message, user_details)

        sent_dm = await dm_channel.send(content=dm_message_content, view=view)
        view.message = sent_dm # Store the message reference in the view for timeout editing
        logger.info(f"Sent sharing request DM to user {user.id} for message {message.id}.")

    except discord.Forbidden:
        logger.warning(f"Could not send DM to user {user.id} (Forbidden). They may have DMs disabled globally or blocked the bot.")
        # Optional: Store this preference in DB? (e.g., dm_preference=False)
        # db_handler.create_or_update_member(member_id=user.id, username=user.name, global_name=user.global_name, dm_preference=False)
    except Exception as e:
        logger.error(f"Failed to send sharing request DM to user {user.id} for message {message.id}: {e}", exc_info=True) 