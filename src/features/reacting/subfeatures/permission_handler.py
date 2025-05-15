import discord
import asyncio
import logging # Added to define logger for constants section if needed
from urllib.parse import quote

# Assuming DatabaseHandler and OpenMuseInteractor will be passed or imported appropriately
# from src.common.db_handler import DatabaseHandler
# from src.common.openmuse_interactor import OpenMuseInteractor

# --- BEGIN VIEW DEFINITION ---
class PermissionRequestView(discord.ui.View):
    def __init__(self, *, timeout=86400, author: discord.User, curator: discord.User, message: discord.Message, message_link: str, db_handler, logger, openmuse_interactor):
        super().__init__(timeout=timeout)
        self.author = author
        self.curator = curator
        self.message = message
        self.message_link = message_link
        self.db_handler = db_handler
        self.logger = logger
        self.openmuse_interactor = openmuse_interactor
        self.response_message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return False
        return True

    async def disable_all_items(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.response_message:
            try:
                await self.response_message.edit(view=self)
                self.logger.debug(f"[Reactor][PermissionView] Disabled buttons for DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to disable view items for message {self.response_message.id}: {e}")
        else:
             self.logger.warning(f"[Reactor][PermissionView] response_message not set, cannot disable buttons via edit.")

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.green, custom_id="permission_allow")
    async def allow_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[Reactor][PermissionView] Author {self.author.id} clicked 'Allow' for message {self.message.id}.")
        await interaction.response.defer(ephemeral=True, thinking=True)

        success = False
        try:
            self.logger.info(f"[Reactor][PermissionView] Updating permission to TRUE for author {self.author.id}.")
            success = await asyncio.to_thread(
                self.db_handler.update_member_permission_status,
                self.author.id,
                True
            )
            if not success:
                self.logger.error(f"[Reactor][PermissionView] Failed to update permission to TRUE (returned False).")
            else:
                 self.logger.info(f"[Reactor][PermissionView] Successfully updated permission to TRUE.")
        except Exception as db_err:
            self.logger.error(f"[Reactor][PermissionView] Exception updating permission to TRUE: {db_err}", exc_info=True)
            await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please try again later or contact support.", ephemeral=True)
            self.stop()
            return

        if not success:
             await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
             self.stop()
             return

        if self.response_message:
            try:
                await self.response_message.delete()
                self.logger.info(f"[Reactor][PermissionView] Deleted original permission request DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to delete original permission DM {self.response_message.id}: {e}")
        else:
            self.logger.warning("[Reactor][PermissionView] response_message not set, cannot delete original DM.")

        profile_record = None
        if not self.message or not self.message.attachments:
             self.logger.warning(f"[Reactor][PermissionView] Original message/attachments missing for {getattr(self.message, 'id', 'Unknown')}. Skipping upload.")
             upload_success_count = 0
             upload_fail_count = 0
             if self.openmuse_interactor:
                 try:
                     _, profile_record = await self.openmuse_interactor.find_or_create_profile(self.author)
                 except Exception as profile_err:
                      self.logger.error(f"[Reactor][PermissionView] Error fetching profile data when attachments missing: {profile_err}")
        elif not self.openmuse_interactor:
             self.logger.error(f"[Reactor][PermissionView] OpenMuseInteractor not available. Cannot upload.")
             await interaction.followup.send("Permission granted, but an internal error occurred preventing the upload. Please contact support.", ephemeral=True)
             self.stop()
             return
        else:
            upload_success_count = 0
            upload_fail_count = 0
            for attachment in self.message.attachments:
                self.logger.info(f"[Reactor][PermissionView] Uploading attachment '{attachment.filename}' for message {self.message.id}.")
                try:
                    media_record, current_profile_record = await self.openmuse_interactor.upload_discord_attachment(
                        attachment=attachment,
                        author=self.author,
                        message=self.message,
                        admin_status='Curated'
                    )
                    if media_record:
                        self.logger.info(f"[Reactor][PermissionView] Upload success: '{attachment.filename}'. Media ID: {media_record.get('id')}")
                        upload_success_count += 1
                        if current_profile_record:
                            profile_record = current_profile_record
                    else:
                        self.logger.error(f"[Reactor][PermissionView] Upload failure: '{attachment.filename}' (media_record is None).")
                        upload_fail_count += 1
                        if current_profile_record and not profile_record:
                            profile_record = current_profile_record
                except Exception as upload_ex:
                    self.logger.error(f"[Reactor][PermissionView] Exception during upload of '{attachment.filename}': {upload_ex}", exc_info=True)
                    upload_fail_count += 1

        profile_url = None
        if profile_record:
             original_username = profile_record.get('username')
             if original_username:
                  try:
                      sanitized_for_url_path = original_username.replace('#', '_').replace('.', '_')
                      sanitized_for_url_path = sanitized_for_url_path.strip('_')
                      if sanitized_for_url_path:
                          profile_url = f"https://openmuse.ai/profile/{quote(sanitized_for_url_path)}"
                          self.logger.info(f"[Reactor][PermissionView] Constructed profile URL: {profile_url} (from original: '{original_username}')")
                      else:
                          self.logger.warning(f"[Reactor][PermissionView] Username '{original_username}' became empty after sanitization for URL path. No profile URL will be generated.")
                  except Exception as e:
                      self.logger.error(f"[Reactor][PermissionView] Error processing username '{original_username}' for URL construction: {e}", exc_info=True)
             else:
                  self.logger.warning(f"[Reactor][PermissionView] Username not found or empty in profile_record for user {self.author.id}.")
        else:
             self.logger.warning(f"[Reactor][PermissionView] profile_record not available after upload attempt for user {self.author.id}. Cannot generate profile link.")
        
        if profile_url:
             final_content = f"Thanks! You can find your profile [here]({profile_url}) and just log in with Discord to update or edit it."
        else:
             final_content = "Thanks! Your permission has been recorded."
             if upload_fail_count > 0:
                  final_content += " Some uploads may have failed, please check with curators."

        try:
             await self.author.send(content=final_content)
             self.logger.info(f"[Reactor][PermissionView] Sent final confirmation DM to author {self.author.id}.")
        except (discord.HTTPException, discord.Forbidden) as send_err:
             self.logger.error(f"[Reactor][PermissionView] Failed to send final confirmation DM to author {self.author.id}: {send_err}")

        curator_feedback = f"{self.author.mention} granted permission for {self.message_link}. Upload result: {upload_success_count} succeeded, {upload_fail_count} failed."
        try:
            await self.curator.send(curator_feedback)
            self.logger.info(f"[Reactor][PermissionView] Sent upload status feedback to curator {self.curator.id}.")
        except discord.Forbidden:
            self.logger.warning(f"[Reactor][PermissionView] Could not send upload status DM feedback to curator {self.curator.id}.")
        except Exception as e:
             self.logger.error(f"[Reactor][PermissionView] Error sending feedback to curator {self.curator.id}: {e}")
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="permission_deny")
    async def deny_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[Reactor][PermissionView] Author {self.author.id} clicked 'Deny' for message {self.message.id}.")
        await interaction.response.defer(ephemeral=True)

        success = False
        try:
            self.logger.info(f"[Reactor][PermissionView] Updating permission to FALSE for author {self.author.id}.")
            success = await asyncio.to_thread(
                self.db_handler.update_member_permission_status,
                self.author.id,
                False
            )
            if not success:
                 self.logger.error(f"[Reactor][PermissionView] Failed to update permission to FALSE (returned False).")
            else:
                 self.logger.info(f"[Reactor][PermissionView] Successfully updated permission to FALSE.")
        except Exception as db_err:
            self.logger.error(f"[Reactor][PermissionView] Exception updating permission to FALSE: {db_err}", exc_info=True)
            await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
            self.stop()
            return

        if not success:
             await interaction.followup.send(content="Sorry, there was a database error updating your permission. Please contact support.", ephemeral=True)
             self.stop()
             return

        if self.response_message:
            try:
                await self.response_message.delete()
                self.logger.info(f"[Reactor][PermissionView] Deleted original permission request DM {self.response_message.id}.")
            except discord.HTTPException as e:
                self.logger.warning(f"[Reactor][PermissionView] Failed to delete original permission DM {self.response_message.id}: {e}")
        else:
             self.logger.warning("[Reactor][PermissionView] response_message not set, cannot delete original DM.")

        final_content = "No problem, thank you for your response!"
        try:
            await self.author.send(content=final_content)
            self.logger.info(f"[Reactor][PermissionView] Sent denial confirmation DM to author {self.author.id}.")
        except (discord.HTTPException, discord.Forbidden) as send_err:
            self.logger.error(f"[Reactor][PermissionView] Failed to send denial confirmation DM to author {self.author.id}: {send_err}")

        curator_feedback = f"{self.author.mention} denied permission for {self.message_link}."
        try:
            await self.curator.send(curator_feedback)
            self.logger.info(f"[Reactor][PermissionView] Sent denial feedback to curator {self.curator.id}.")
        except discord.Forbidden:
            self.logger.warning(f"[Reactor][PermissionView] Could not send denial DM feedback to curator {self.curator.id}.")
        except Exception as e:
            self.logger.error(f"[Reactor][PermissionView] Error sending denial feedback to curator {self.curator.id}: {e}")
        self.stop()

    async def on_timeout(self):
        self.logger.info(f"[Reactor][PermissionView] Permission request view timed out for author {self.author.id}, message {getattr(self.message, 'id', 'Unknown')}.")
        if self.response_message:
            try:
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True
                await self.response_message.edit(content=f"This permission request for {self.message_link} has expired.", view=self)
            except discord.HTTPException as e:
                 self.logger.warning(f"[Reactor][PermissionView] Failed to edit message on timeout: {e}")

# --- END VIEW DEFINITION ---

async def handle_request_curation_permission(
    reaction: discord.Reaction,
    curator: discord.User,
    db_handler, # Expected: DatabaseHandler instance
    openmuse_interactor, # Expected: OpenMuseInteractor instance
    logger # Expected: logging.Logger instance
):
    """Sends a DM requesting permission or uploads if permission granted."""
    message = reaction.message
    author = message.author
    message_link = message.jump_url

    if not openmuse_interactor:
         logger.error("[Reactor][PermissionHandler] OpenMuseInteractor not available. Cannot proceed.")
         return

    logger.info(f"[Reactor][PermissionHandler] Action 'request_curation_permission' triggered by curator {curator.id} ({curator.display_name}) on message {message.id} by author {author.id} ({author.display_name}).")

    if author.bot:
        logger.info(f"[Reactor][PermissionHandler] Author {author.id} is a bot. Skipping permission request.")
        return

    if not db_handler:
        logger.error("[Reactor][PermissionHandler] Database handler not available. Cannot request curation permission.")
        return

    try:
        author_member_data = await asyncio.to_thread(db_handler.get_member, author.id)

        if not author_member_data:
             logger.info(f"[Reactor][PermissionHandler] Author {author.id} not found in DB. Creating member entry.")
             success = await asyncio.to_thread(
                 db_handler.create_or_update_member,
                 member_id=author.id,
                 username=author.name,
                 display_name=getattr(author, 'display_name', None),
                 global_name=getattr(author, 'global_name', None),
                 avatar_url=str(author.display_avatar.url) if author.display_avatar else None,
                 discriminator=getattr(author, 'discriminator', None),
                 bot=author.bot,
                 system=author.system
             )
             if not success:
                logger.error(f"[Reactor][PermissionHandler] Failed to create database entry for author {author.id}. Aborting permission request.")
                return
             logger.info(f"[Reactor][PermissionHandler] Member entry created for author {author.id}. Proceeding with DM.")
             permission_status = None
        else:
            permission_status = author_member_data.get('permission_to_curate')
            logger.info(f"[Reactor][PermissionHandler] Author {author.id} found in DB. Current permission_to_curate status: {permission_status}")

        if permission_status is not None:
            if permission_status:
                logger.info(f"[Reactor][PermissionHandler] Author {author.id} has already granted permission (status: {permission_status}). Proceeding with upload for message {message.id}.")
                if not message.attachments:
                    logger.warning(f"[Reactor][PermissionHandler] Message {message.id} has no attachments to upload, despite existing permission.")
                    return

                upload_success_count = 0
                upload_fail_count = 0
                for attachment in message.attachments:
                    logger.info(f"[Reactor][PermissionHandler] Uploading attachment '{attachment.filename}' for message {message.id} due to existing permission.")
                    try:
                        media_record, _ = await openmuse_interactor.upload_discord_attachment(
                            attachment=attachment,
                            author=author,
                            message=message,
                            admin_status='Curated'
                        )
                        if media_record:
                            logger.info(f"[Reactor][PermissionHandler] Successfully uploaded attachment '{attachment.filename}' for message {message.id}.")
                            upload_success_count += 1
                        else:
                            logger.error(f"[Reactor][PermissionHandler] Failed to upload attachment '{attachment.filename}' for message {message.id} (media_record is None).")
                            upload_fail_count += 1
                    except Exception as upload_ex:
                        logger.error(f"[Reactor][PermissionHandler] Exception during upload of attachment '{attachment.filename}': {upload_ex}", exc_info=True)
                        upload_fail_count += 1
                
                feedback_msg = f"Attempted upload for {author.display_name}'s message ({message_link}) based on existing permission: {upload_success_count} succeeded, {upload_fail_count} failed."
                try:
                     await curator.send(feedback_msg)
                     logger.info(f"[Reactor][PermissionHandler] Sent upload status feedback to curator {curator.id}.")
                except discord.Forbidden:
                     logger.warning(f"[Reactor][PermissionHandler] Could not send upload status DM feedback to curator {curator.id}.")
                except Exception as react_ex:
                     logger.warning(f"[Reactor][PermissionHandler] Could not add reaction to message {message.id} after upload attempt: {react_ex}")
                return
            else:
                status_str = "denied"
                logger.info(f"[Reactor][PermissionHandler] Author {author.id} has already {status_str} permission (status: {permission_status}). No action needed.")
                return

        logger.info(f"[Reactor][PermissionHandler] Permission status for author {author.id} is NULL. Sending permission request DM.")
        dm_content = (
            f"Hi {author.mention}! {curator.mention} would like to curate your work to [OpenMuse](https://openmuse.ai/. ): {message_link}\\n\\n"
            f"It will be hosted under your profile name there - for you to edit and update if/when you claim an account by signing up with your Discord account.\\n\\n"
            f"Do you give permission? (This request expires in 24 hours)"
        )
        view = PermissionRequestView(
            author=author,
            curator=curator,
            message=message,
            message_link=message_link,
            db_handler=db_handler,
            logger=logger,
            openmuse_interactor=openmuse_interactor
        )
        try:
            sent_message = await author.send(content=dm_content, view=view)
            view.response_message = sent_message
            logger.info(f"[Reactor][PermissionHandler] Successfully sent permission request DM to author {author.id} for message {message.id}. DM ID: {sent_message.id}")
        except discord.Forbidden:
            logger.warning(f"[Reactor][PermissionHandler] Could not send permission request DM to author {author.id}. They may have DMs disabled.")
        except Exception as e:
            logger.error(f"[Reactor][PermissionHandler] Error sending permission request DM to author {author.id}: {e}", exc_info=True)

    except Exception as e:
         logger.error(f"[Reactor][PermissionHandler] Unexpected error during 'request_curation_permission' for message {message.id}: {e}", exc_info=True) 