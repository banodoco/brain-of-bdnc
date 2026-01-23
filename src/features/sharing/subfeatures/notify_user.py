# Post-share notification functions for first-time share DMs

import discord
import logging
from discord.ext import commands
from src.common.db_handler import DatabaseHandler
from src.common import discord_utils
from typing import Optional
import os

logger = logging.getLogger('DiscordBot')


# ========== Post-Share Notification (First-time share notification) ==========

class PostShareNotificationView(discord.ui.View):
    """View shown AFTER content is shared for the first time.
    
    Allows user to:
    - Delete the shared post (within 6 hours)
    - Opt-out of future sharing
    """
    
    def __init__(
        self, 
        db_handler: DatabaseHandler, 
        discord_message_id: int,
        discord_user_id: int,
        tweet_id: str,
        tweet_url: str,
        bot=None,
        timeout: float = 21600  # 6 hours in seconds
    ):
        super().__init__(timeout=timeout)
        self.db_handler = db_handler
        self.discord_message_id = discord_message_id
        self.discord_user_id = discord_user_id
        self.tweet_id = tweet_id
        self.tweet_url = tweet_url
        self.bot = bot
        self.message: Optional[discord.Message] = None
        self._delete_attempted = False
    
    @discord.ui.button(label="Delete Post", style=discord.ButtonStyle.danger, custom_id="delete_post", emoji="🗑️", row=0)
    async def delete_post_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Delete the shared tweet."""
        if self._delete_attempted:
            await interaction.response.send_message("Delete has already been attempted.", ephemeral=True)
            return
        
        self._delete_attempted = True
        
        # Import here to avoid circular imports
        from .social_poster import delete_tweet
        
        await interaction.response.defer(ephemeral=True)
        
        logger.info(f"User {interaction.user.id} requested deletion of tweet {self.tweet_id}")
        
        success = await delete_tweet(self.tweet_id)
        
        if success:
            # Mark as deleted in DB
            self.db_handler.mark_shared_post_deleted(self.discord_message_id, 'twitter')
            
            # Update the message to show deletion was successful
            button.disabled = True
            button.label = "Post Deleted"
            button.style = discord.ButtonStyle.secondary
            
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception as e:
                    logger.warning(f"Failed to update message after deletion: {e}")
            
            await interaction.followup.send("Your post has been deleted from Twitter.", ephemeral=True)
            logger.info(f"Successfully deleted tweet {self.tweet_id} at user {interaction.user.id}'s request")
        else:
            # Deletion failed - notify admin
            self._delete_attempted = False  # Allow retry
            await interaction.followup.send(
                "Failed to delete the post. The admin has been notified and will handle this manually.", 
                ephemeral=True
            )
            
            # Notify admin (POM)
            admin_user_id_str = os.getenv('ADMIN_USER_ID')
            if admin_user_id_str and self.bot:
                try:
                    admin_user_id = int(admin_user_id_str)
                    admin_user = await self.bot.fetch_user(admin_user_id)
                    if admin_user:
                        admin_dm = await admin_user.create_dm()
                        await admin_dm.send(
                            f"⚠️ **Tweet Deletion Failed**\n\n"
                            f"User <@{interaction.user.id}> requested deletion of their tweet but it failed.\n"
                            f"**Tweet URL:** {self.tweet_url}\n"
                            f"**Tweet ID:** {self.tweet_id}\n"
                            f"**Discord Message ID:** {self.discord_message_id}\n\n"
                            f"Please delete manually."
                        )
                        logger.info(f"Notified admin about failed tweet deletion for {self.tweet_id}")
                except Exception as e:
                    logger.error(f"Failed to notify admin about deletion failure: {e}")
    
    @discord.ui.button(label="Opt-out of Sharing", style=discord.ButtonStyle.secondary, custom_id="opt_out", emoji="🚫", row=0)
    async def opt_out_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Opt out of future content sharing."""
        success = self.db_handler.update_member_sharing_permission(interaction.user.id, False)
        
        if success:
            # Update the role
            if self.bot:
                await discord_utils.update_no_sharing_role(self.bot, interaction.user.id, False, logger)
            
            # Update button
            button.disabled = True
            button.label = "Opted Out"
            
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception as e:
                    logger.warning(f"Failed to update message after opt-out: {e}")
            
            await interaction.response.send_message(
                "You've been opted out of future content sharing. "
                "You can change this anytime using `/update_details`.", 
                ephemeral=True
            )
            logger.info(f"User {interaction.user.id} opted out of sharing via post-share notification")
        else:
            await interaction.response.send_message(
                "Failed to update your preferences. Please try `/update_details` instead.", 
                ephemeral=True
            )
    
    async def on_timeout(self):
        """Called when the 6-hour window expires."""
        logger.info(f"Post-share notification timed out for message {self.discord_message_id}")
        
        if self.message:
            try:
                # Disable the delete button but keep opt-out
                for item in self.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id == "delete_post":
                        item.disabled = True
                        item.label = "Delete Expired"
                
                # Update the message
                await self.message.edit(
                    content=self.message.content + "\n\n_(The 6-hour window to delete your post has expired.)_",
                    view=self
                )
            except Exception as e:
                logger.warning(f"Failed to update message on timeout: {e}")


async def send_post_share_notification(
    bot: commands.Bot,
    user: discord.User,
    discord_message: discord.Message,
    tweet_id: str,
    tweet_url: str,
    db_handler: DatabaseHandler
):
    """Sends a DM to user after their content is shared for the first time.
    
    Args:
        bot: Discord bot instance
        user: The user whose content was shared
        discord_message: The original Discord message that was shared
        tweet_id: ID of the posted tweet
        tweet_url: URL of the posted tweet
        db_handler: Database handler instance
    """
    logger.info(f"Sending post-share notification to user {user.id} for tweet {tweet_id}")
    
    # Build the DM message
    dm_content = f"""Hi there!

Your content has been shared by the Banodoco Twitter account:
{tweet_url}

**Original post:** {discord_message.jump_url}

If you'd like to opt-out of your content being shared in the future, or delete this specific post, use the buttons below.

⏰ **Note:** The delete option is only available for 6 hours.

You can always update your preferences by using `/update_details`."""
    
    # Handle dev mode redirect
    target_user = user
    is_redirected = False
    
    if getattr(bot, 'dev_mode', False):
        admin_user_id_str = os.getenv('ADMIN_USER_ID')
        if admin_user_id_str:
            try:
                admin_user_id = int(admin_user_id_str)
                fetched_admin = await bot.fetch_user(admin_user_id)
                if fetched_admin:
                    target_user = fetched_admin
                    is_redirected = True
                    dm_content = f"**(DEV MODE: This DM was intended for {user.display_name} ({user.id}))**\n\n" + dm_content
            except Exception as e:
                logger.warning(f"Could not redirect to admin in dev mode: {e}")
    
    try:
        dm_channel = await target_user.create_dm()
        
        view = PostShareNotificationView(
            db_handler=db_handler,
            discord_message_id=discord_message.id,
            discord_user_id=user.id,
            tweet_id=tweet_id,
            tweet_url=tweet_url,
            bot=bot
        )
        
        # Send DM
        rate_limiter = getattr(bot, 'rate_limiter', None)
        if rate_limiter:
            sent_message = await discord_utils.safe_send_message(
                bot, dm_channel, rate_limiter, logger,
                content=dm_content, view=view
            )
        else:
            sent_message = await dm_channel.send(content=dm_content, view=view)
        
        if sent_message:
            view.message = sent_message
            log_msg = f"Sent post-share notification to {target_user.id}"
            if is_redirected:
                log_msg += f" (redirected, originally for {user.id})"
            log_msg += f" for tweet {tweet_id}"
            logger.info(log_msg)
        else:
            logger.error(f"Failed to send post-share notification to {target_user.id}")
            
    except discord.Forbidden:
        logger.warning(f"Could not send post-share notification to {target_user.id} (Forbidden)")
    except Exception as e:
        logger.error(f"Error sending post-share notification to {target_user.id}: {e}", exc_info=True)
