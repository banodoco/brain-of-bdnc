import discord
from discord.ext import commands # Added for commands.Bot type hint
import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Optional, Any, Union, Dict
import json
import io # Added for io.BytesIO
try: # Added for Pillow
    from PIL import Image, UnidentifiedImageError
except ImportError:
    Image = None # Placeholder if Pillow is not installed
    UnidentifiedImageError = None # Placeholder
try: # Added for moviepy
    import moviepy.editor as mp 
except ImportError:
    mp = None # Placeholder if moviepy is not installed
import os # For path manipulation in GIF conversion
import tempfile # For temporary files in GIF conversion

from src.common.discord_utils import safe_send_message
from src.common.rate_limiter import RateLimiter
# Assuming db_handler, claude_client, openmuse_interactor will be typed properly
# For now, using Any
if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler
    # from src.common.llm.claude_client import ClaudeClient # Define if available
    # from src.common.openmuse_interactor import OpenMuseInteractor # Define if available

# Placeholder for OpenMuseInteractor and ClaudeClient types if not fully defined elsewhere
OpenMuseInteractor = Any 
ClaudeClient = Any


# Lock for one in-flight workflow per user (Section 11)
# Key: user_id (author_id), Value: asyncio.Lock
_user_workflow_locks: dict[int, asyncio.Lock] = {}


class WorkflowUploadView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author: discord.User, curator: discord.User, db_handler: 'DatabaseHandler', logger: logging.Logger, rate_limiter: RateLimiter, timeout=43200): # Changed timeout to 12 hours (43200 seconds)
        super().__init__(timeout=timeout)
        self.bot = bot # Store bot
        self.author = author
        self.curator = curator
        self.db_handler = db_handler
        self.logger = logger
        self.rate_limiter = rate_limiter # Store rate_limiter
        self.interaction_result: Optional[str] = None # To store button choice
        self.message: Optional[discord.Message] = None # To store the DM message for later deletion

    async def on_timeout(self):
        self.logger.info(f"[WorkflowUpload] View timed out for author {self.author.id}.")
        if self.message:
            try:
                await self.message.delete()
                self.logger.info(f"[WorkflowUpload] Deleted initial DM for author {self.author.id} after timeout.")
            except discord.HTTPException as e:
                self.logger.warning(f"[WorkflowUpload] Failed to delete DM for author {self.author.id} on timeout: {e}")
        # Notify curator maybe? Spec: "After a choice is made (or the view times out), delete the interactive DM"

    async def handle_choice(self, interaction: discord.Interaction, choice: str):
        await interaction.response.defer() # Acknowledge interaction immediately
        self.interaction_result = choice
        self.stop() # Stop the view from listening to further interactions

        if self.message:
            try:
                await self.message.delete()
                self.logger.info(f"[WorkflowUpload] Deleted initial DM for author {self.author.id} after choice '{choice}'.")
            except discord.HTTPException as e:
                self.logger.warning(f"[WorkflowUpload] Failed to delete DM for author {self.author.id} after choice: {e}")

    @discord.ui.button(label="Auto Upload It to OpenMuse", style=discord.ButtonStyle.success, custom_id="confirm_upload")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[WorkflowUpload] Author {self.author.id} CONFIRMED upload via DM.")
        await self.handle_choice(interaction, "confirm")

    @discord.ui.button(label="I'm not interested", style=discord.ButtonStyle.secondary, custom_id="decline_upload") # Secondary instead of danger for softer decline
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.logger.info(f"[WorkflowUpload] Author {self.author.id} DECLINED upload via DM.")
        
        # Section 3: Decline handling - set allow_content_sharing to False
        try:
            sc = getattr(self.bot, 'server_config', None)
            _guild_id = sc.bndc_guild_id if sc else None
            success = self.db_handler.update_member_sharing_permission(member_id=self.author.id, allow_content_sharing=False, guild_id=_guild_id)
            if success:
                self.logger.info(f"[WorkflowUpload] Set allow_content_sharing=False for author {self.author.id}.")
                # Add the "no sharing" role to make opt-out visible
                from src.common.discord_utils import update_no_sharing_role
                await update_no_sharing_role(self.bot, self.author.id, False, self.logger)
                try: # ACK DM to user
                    await safe_send_message(bot=self.bot, channel=interaction.user, content="Okay, I understand. I won't ask you about uploading this workflow again. You can always change this preference later if you wish.", logger=self.logger, rate_limiter=self.rate_limiter) # Added rate_limiter
                except Exception as e:
                    self.logger.error(f"[WorkflowUpload] Failed to send decline ACK DM to author {self.author.id}: {e}")
            else:
                self.logger.warning(f"[WorkflowUpload] Failed to update allow_content_sharing for author {self.author.id} in DB.")
        except Exception as e:
            self.logger.error(f"[WorkflowUpload] Error updating allow_content_sharing for author {self.author.id}: {e}")

        # DM the curator (reacting user)
        try:
            await safe_send_message(bot=self.bot, channel=self.curator, content=f"User {self.author.mention} ({self.author.id}) has chosen not to upload their workflow and has been opted out of future curation DMs for this feature.", logger=self.logger, rate_limiter=self.rate_limiter) # Added rate_limiter
        except Exception as e:
            self.logger.error(f"[WorkflowUpload] Failed to send decline notification DM to curator {self.curator.id}: {e}")
            
        await self.handle_choice(interaction, "decline")


async def process_workflow_upload_request(
    bot: commands.Bot,
    reaction: discord.Reaction,
    curator_user: discord.User, # User who added the reaction
    logger: logging.Logger,
    rate_limiter: RateLimiter,
    db_handler: 'DatabaseHandler',
    claude_client: Optional[ClaudeClient] = None,
    openmuse_interactor: Optional[OpenMuseInteractor] = None
):
    message = reaction.message
    author = message.author
    logger.info(f"[WorkflowUpload] Processing request for message {message.id} by author {author.id}, triggered by curator {curator_user.id}.")

    # Section 11: One in-flight workflow per user
    if author.id not in _user_workflow_locks:
        _user_workflow_locks[author.id] = asyncio.Lock()
    
    user_lock = _user_workflow_locks[author.id]
    if user_lock.locked():
        logger.info(f"[WorkflowUpload] Author {author.id} already has an active workflow upload process. Aborting.")
        # Optionally notify curator or author
        # await safe_send_message(curator_user, "This user already has an active workflow upload in progress.", logger=logger)
        return

    async with user_lock: # Lock will be released when the function exits or an exception occurs
        logger.info(f"[WorkflowUpload] Acquired lock for author {author.id}.")

        # Section 2: Trigger & Eligibility - Author Opt-Out Check
        # Check allow_content_sharing: defaults to TRUE, only FALSE is explicit opt-out
        try:
            member_data = db_handler.get_member(author.id)
            if member_data:
                allow_content_sharing = member_data.get('allow_content_sharing')
                # Only allow_content_sharing = False (explicit opt-out) blocks the workflow
                if allow_content_sharing is False:
                    logger.info(f"[WorkflowUpload] Author {author.id} has opted out (allow_content_sharing=False). Aborting.")
                    # Notify curator
                    await safe_send_message(bot=bot, channel=curator_user, content=f"Skipping workflow upload for {author.mention} ({author.id}) as they have opted out of content sharing.", logger=logger, rate_limiter=rate_limiter)
                    return
                logger.info(f"[WorkflowUpload] Author {author.id} allow_content_sharing: {allow_content_sharing}. Proceeding.")
            else:
                # New member, or member not in DB. Assume permission is granted (defaults to TRUE).
                logger.info(f"[WorkflowUpload] Author {author.id} not found in DB. Assuming eligible (default allow_content_sharing=True).")
        except Exception as e:
            logger.error(f"[WorkflowUpload] Error checking member permission for author {author.id}: {e}")
            # Notify curator about the error
            await safe_send_message(bot=bot, channel=curator_user, content=f"An error occurred while checking permissions for {author.mention} ({author.id}). Cannot proceed.", logger=logger, rate_limiter=rate_limiter)
            return

        # Section 3: Initial DM (Call-to-Action)
        message_link = message.jump_url
        dm_content = (
            f"Hi {author.mention}! {curator_user.mention} thought your workflow here seemed very impressive: {message_link}\n\n"
            "Would you be up for sharing it on OpenMuse? This helps others learn, and between you and me, make OpenMuse look less empty.\n\n"
            "If you press below, it'll upload it and relevant attachments and send you a link to edit! No problem if you're not interested."
        )
        
        view = WorkflowUploadView(bot=bot, author=author, curator=curator_user, db_handler=db_handler, logger=logger, rate_limiter=rate_limiter) # Pass bot Pass rate_limiter

        dm_message_sent = None
        try:
            # Using author directly as channel for DM
            dm_message_sent = await safe_send_message(
                bot=bot, # safe_send_message expects bot if sending to a user object
                channel=author, 
                content=dm_content, 
                view=view, 
                logger=logger, 
                rate_limiter=rate_limiter
            )
            view.message = dm_message_sent # Store the sent message for later deletion
            logger.info(f"[WorkflowUpload] Sent initial DM to author {author.id} for message {message.id}.")
        except discord.Forbidden:
            logger.warning(f"[WorkflowUpload] Failed to send DM to author {author.id} (Forbidden). They might have DMs disabled.")
            # DM the curator
            await safe_send_message(bot=bot, channel=curator_user, content=f"Could not DM {author.mention} ({author.id}) to ask about workflow upload (DMs might be disabled).", logger=logger, rate_limiter=rate_limiter)
            return 
        except Exception as e:
            logger.error(f"[WorkflowUpload] Error sending initial DM to author {author.id}: {e}")
            # DM the curator
            await safe_send_message(bot=bot, channel=curator_user, content=f"An error occurred while trying to DM {author.mention} ({author.id}) about workflow upload.", logger=logger, rate_limiter=rate_limiter)
            return

        if not dm_message_sent:
             logger.error(f"[WorkflowUpload] DM message object not returned by safe_send_message for author {author.id}. Cannot proceed with view.")
             # DM curator if safe_send_message itself failed without specific exception above
             await safe_send_message(bot=bot, channel=curator_user, content=f"Failed to send DM to {author.mention} ({author.id}). Cannot proceed.", logger=logger, rate_limiter=rate_limiter)
             return

        # Wait for the view to complete (button press or timeout)
        await view.wait()

        if view.interaction_result == "confirm":
            logger.info(f"[WorkflowUpload] Author {author.id} confirmed. Proceeding with workflow pipeline for message {message.id}.")
            
            source_material = await _collect_source_material(
                bot=bot, 
                original_message=message, 
                author=author, 
                logger=logger, 
                curator_user=curator_user,
                rate_limiter=rate_limiter
            )

            if not source_material:
                logger.info(f"[WorkflowUpload] Failed to collect source material (no workflow JSON found). Aborting further steps.")
                # Notifications about failure are handled within _collect_source_material
                return # Exit if material collection failed

            # Determine which workflow source to use
            explicit_json_attachment = source_material.get("json_attachment")
            embedded_workflow_str = source_material.get("embedded_workflow_str")
            
            final_workflow_payload: Union[discord.Attachment, io.BytesIO, None] = None
            final_workflow_filename: Optional[str] = None
            final_workflow_content_type: Optional[str] = None
            asset_description_source_attachment: Optional[discord.Attachment] = None # For .description attribute

            if explicit_json_attachment:
                final_workflow_payload = explicit_json_attachment
                final_workflow_filename = explicit_json_attachment.filename
                final_workflow_content_type = explicit_json_attachment.content_type or "application/json"
                asset_description_source_attachment = explicit_json_attachment # Keep for potential .description use
                logger.info(f"[WorkflowUpload] Using explicit JSON attachment: {final_workflow_filename}")
            elif embedded_workflow_str:
                try:
                    # Validate JSON structure again before creating BytesIO, as it's critical for upload
                    json.loads(embedded_workflow_str) 
                    final_workflow_payload = io.BytesIO(embedded_workflow_str.encode('utf-8'))
                    # Create a somewhat unique filename for embedded workflows
                    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    final_workflow_filename = f"embedded_workflow_{message.id}_{timestamp}.json"
                    final_workflow_content_type = "application/json"
                    # asset_description_source_attachment remains None, as embedded workflows don't have a discord.Attachment.description
                    logger.info(f"[WorkflowUpload] Using embedded workflow extracted from an image: {final_workflow_filename}")
                except json.JSONDecodeError as e:
                    logger.error(f"[WorkflowUpload] Embedded workflow string failed final JSON validation before upload: {e}. Aborting.")
                    await safe_send_message(bot=bot, channel=author, content="Sorry, the extracted workflow data was corrupted and could not be uploaded. Please ensure it's a valid ComfyUI workflow JSON.", logger=logger, rate_limiter=rate_limiter)
                    await safe_send_message(bot=bot, channel=curator_user, content=f"Critical error: Extracted embedded workflow for {author.mention} (message {message.jump_url}) was invalid JSON during final check. Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                    return # Abort
            else:
                # This case should ideally be caught by _collect_source_material returning None 
                # and handled before this point. This is a safeguard.
                logger.error(f"[WorkflowUpload] Critical: No workflow source (explicit or embedded) found after _collect_source_material call seemed to succeed. This indicates a logic error. Aborting message {message.id}.")
                await safe_send_message(bot=bot, channel=curator_user, content=f"A critical internal error occurred (missing workflow data post-collection) for {author.mention} ({message.jump_url}). Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                return # Abort

            if not final_workflow_payload or not final_workflow_filename:
                # This should not be reached if the logic above is correct and _collect_source_material returned valid data or None (handled earlier)
                logger.error(f"[WorkflowUpload] Payload/filename definition failed unexpectedly for message {message.id}. Aborting.")
                return

            context_messages_text = source_material["context_messages_text"]
            media_attachments = source_material["media_attachments"] # from context messages
            # original_message_content is also in source_material if needed directly

            logger.info(f"[WorkflowUpload] Finalizing with workflow '{final_workflow_filename}'. Context: {len(context_messages_text)} chars, {len(media_attachments)} media items.")
            
            # Section 5: Generate Workflow Name
            workflow_name = await _generate_workflow_name(
                claude_client=claude_client,
                context_text=context_messages_text,
                logger=logger
            )
            logger.info(f"[WorkflowUpload] Generated workflow name: '{workflow_name}'")

            # Section 6: Determine Model & Variant
            model_info = await _determine_model_and_variant(
                openmuse_interactor=openmuse_interactor,
                claude_client=claude_client,
                context_text=context_messages_text,
                channel_name=message.channel.name if message.channel else "unknown_channel",
                logger=logger
            )
            
            asset_lora_base_model = None
            asset_model_variant = None
            if model_info:
                asset_lora_base_model = model_info.get("model")
                asset_model_variant = model_info.get("variant")
                logger.info(f"[WorkflowUpload] Determined model: '{asset_lora_base_model}', variant: '{asset_model_variant}'")
            else:
                logger.info("[WorkflowUpload] Could not determine model/variant from Claude.")

            # Section 6.5: Ensure Author Profile Exists
            author_member_id = None
            author_db_profile_data: Optional[Dict[str, Any]] = None
            if openmuse_interactor and hasattr(openmuse_interactor, 'find_or_create_profile'):
                try:
                    logger.info(f"[WorkflowUpload] Ensuring OpenMuse profile exists for author {author.id}...")
                    profile_info = await openmuse_interactor.find_or_create_profile(author)

                    if isinstance(profile_info, tuple) and len(profile_info) == 2:
                        author_db_profile_data = profile_info[0]
                        author_member_id = profile_info[1]
                        logger.info(
                            f"[WorkflowUpload] Author {author.id} OpenMuse member ID: "
                            f"{author_member_id}"
                        )
                        logger.debug(
                            f"[WorkflowUpload] Author profile payload: {author_db_profile_data}"
                        )
                    else:
                        logger.warning(
                            f"[WorkflowUpload] find_or_create_profile returned unexpected data: "
                            f"{profile_info} (type: {type(profile_info)})."
                        )

                    if author_member_id:
                        logger.info(f"[WorkflowUpload] Author {author.id} member ID: {author_member_id}")
                    else:
                        logger.error(f"[WorkflowUpload] Failed to find or create OpenMuse profile for author {author.id}, or member ID not found in response.")
                        await safe_send_message(bot=bot, channel=curator_user, content=f"Critical error: Could not ensure OpenMuse profile for {author.mention}. Asset creation aborted.", logger=logger, rate_limiter=rate_limiter)
                        await safe_send_message(bot=bot, channel=author, content="Sorry, there was an issue setting up your OpenMuse profile. The workflow could not be uploaded.", logger=logger, rate_limiter=rate_limiter)
                        return
                except Exception as e:
                    logger.error(f"[WorkflowUpload] Error calling find_or_create_profile for author {author.id}: {e}")
                    await safe_send_message(bot=bot, channel=curator_user, content=f"Error ensuring OpenMuse profile for {author.mention}: {e}. Asset creation aborted.", logger=logger, rate_limiter=rate_limiter) # Added rate_limiter
                    await safe_send_message(bot=bot, channel=author, content="Sorry, there was an issue setting up your OpenMuse profile due to an error. The workflow could not be uploaded.", logger=logger, rate_limiter=rate_limiter) # Added rate_limiter
                    return # Abort
            else:
                logger.error("[WorkflowUpload] OpenMuseInteractor or find_or_create_profile method not available. Cannot ensure author profile.")
                await safe_send_message(bot=bot, channel=curator_user, content="Critical error: OpenMuse profile system not available. Asset creation aborted.", logger=logger, rate_limiter=rate_limiter) # Added rate_limiter
                await safe_send_message(bot=bot, channel=author, content="Sorry, the OpenMuse profile system is currently unavailable. The workflow could not be uploaded.", logger=logger, rate_limiter=rate_limiter) # Added rate_limiter
                return # Abort

            # Section 4.1 (deferred): Upload Primary JSON attachment to Supabase "workflows" bucket
            workflow_json_url = None
            if openmuse_interactor and hasattr(openmuse_interactor, 'upload_file_to_storage'): 
                try:
                    logger.info(f"[WorkflowUpload] Uploading workflow '{final_workflow_filename}' to Supabase...")
                    file_path_in_bucket = f"workflows/{author_member_id}/{message.id}/{final_workflow_filename}"
                    
                    # Prepare the actual source for upload_file_to_storage
                    actual_upload_source: Union[discord.Attachment, bytes]
                    if isinstance(final_workflow_payload, io.BytesIO):
                        actual_upload_source = final_workflow_payload.read() # Convert io.BytesIO to bytes
                    elif isinstance(final_workflow_payload, discord.Attachment):
                        actual_upload_source = final_workflow_payload
                    else:
                        # This case should ideally not be reached if prior logic is correct.
                        logger.error(f"[WorkflowUpload] final_workflow_payload is of an unexpected type: {type(final_workflow_payload)}. Aborting upload for message {message.id}.")
                        await safe_send_message(bot=bot, channel=author, content="Sorry, an internal error occurred with the workflow data. Upload failed.", logger=logger, rate_limiter=rate_limiter)
                        await safe_send_message(bot=bot, channel=curator_user, content=f"Critical internal error: Unexpected workflow payload type for {author.mention} (message {message.jump_url}). Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                        return # Abort

                    workflow_json_url = await openmuse_interactor.upload_file_to_storage(
                        file_source=actual_upload_source, # Now correctly discord.Attachment or bytes
                        bucket_name="workflows", 
                        storage_path=file_path_in_bucket,
                        content_type=final_workflow_content_type # Already determined
                    )
                    
                    if workflow_json_url:
                        logger.info(f"[WorkflowUpload] Workflow '{final_workflow_filename}' uploaded successfully: {workflow_json_url}")
                    else:
                        logger.error(f"[WorkflowUpload] Failed to upload workflow '{final_workflow_filename}'. No URL returned.")
                        await safe_send_message(bot=bot, channel=author, content="Sorry, there was an error uploading your workflow file. Please try again later.", logger=logger, rate_limiter=rate_limiter)
                        await safe_send_message(bot=bot, channel=curator_user, content=f"Failed to upload workflow JSON ('{final_workflow_filename}') for message {message.jump_url} by {author.mention}. Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                        return # Abort
                except Exception as e:
                    logger.error(f"[WorkflowUpload] Exception uploading workflow '{final_workflow_filename}': {e}")
                    await safe_send_message(bot=bot, channel=author, content="Sorry, an unexpected error occurred while uploading your workflow file. Please try again later.", logger=logger, rate_limiter=rate_limiter)
                    await safe_send_message(bot=bot, channel=curator_user, content=f"Error uploading workflow JSON ('{final_workflow_filename}') for {author.mention} (message {message.jump_url}): {e}. Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                    return # Abort
            else:
                logger.error("[WorkflowUpload] OpenMuseInteractor or required upload method 'upload_file_to_storage' not available.")
                await safe_send_message(bot=bot, channel=author, content="Sorry, the file upload system is currently unavailable for workflows.", logger=logger, rate_limiter=rate_limiter)
                await safe_send_message(bot=bot, channel=curator_user, content="Critical error: Workflow JSON upload system not available. Aborting.", logger=logger, rate_limiter=rate_limiter)
                return # Abort

            # Section 7: Persist Asset Record
            created_asset_uuid = None
            if openmuse_interactor and hasattr(openmuse_interactor, 'supabase'):
                description = source_material.get("original_message_content", "")[:160] 
                # Use discord.Attachment's description only if message content is empty AND we used an explicit attachment
                if not description and asset_description_source_attachment and asset_description_source_attachment.description:
                    description = asset_description_source_attachment.description[:160]

                asset_data = {
                    "type": "workflow",
                    "name": workflow_name, # From Section 5
                    "member_id": int(author_member_id), # From Section 6.5
                    "description": description,
                    "download_link": workflow_json_url, # From JSON upload step
                    "admin_status": "Listed",
                    "user_status": "Listed",
                    "lora_base_model": asset_lora_base_model, # From Section 6
                    "model_variant": asset_model_variant, # From Section 6
                    # Other fields like lora_type, lora_link, etc., default to NULL or are not set yet
                }
                # Remove None keys to allow DB defaults
                asset_data = {k: v for k, v in asset_data.items() if v is not None}

                try:
                    logger.info(f"[WorkflowUpload] Inserting asset record into Supabase: {asset_data}")
                    response = await asyncio.to_thread(
                        openmuse_interactor.supabase.table('assets').insert(asset_data).execute
                    )
                    # Supabase insert usually returns a list of inserted records in response.data
                    if response and hasattr(response, 'data') and response.data and len(response.data) > 0:
                        created_asset = response.data[0]
                        created_asset_uuid = created_asset.get('id')
                        if created_asset_uuid:
                            logger.info(f"[WorkflowUpload] Asset record created successfully. Asset UUID: {created_asset_uuid}")
                        else:
                            logger.error(f"[WorkflowUpload] Asset record insert response did not contain an ID. Response: {response.data}")
                            # Section 10: Supabase failures
                            await safe_send_message(bot=bot, channel=author, content="Sorry, there was an issue saving your workflow information after upload (ID missing). Please contact support.", logger=logger, rate_limiter=rate_limiter)
                            await safe_send_message(bot=bot, channel=curator_user, content=f"Failed to save asset record for {author.mention} (message {message.jump_url}) - ID missing in response. Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                            return # Abort
                    else:
                        logger.error(f"[WorkflowUpload] Failed to insert asset record or no data in response. Response: {getattr(response, 'data', 'No data attribute')}")
                        # Section 10: Supabase failures
                        await safe_send_message(bot=bot, channel=author, content="Sorry, there was an issue saving your workflow information after upload. Please try again later.", logger=logger, rate_limiter=rate_limiter)
                        await safe_send_message(bot=bot, channel=curator_user, content=f"Failed to save asset record for {author.mention} (message {message.jump_url}). Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                        return # Abort
                except Exception as e:
                    logger.error(f"[WorkflowUpload] Exception inserting asset record: {e}")
                    # Section 10: Supabase failures
                    await safe_send_message(bot=bot, channel=author, content="Sorry, an unexpected error occurred while saving your workflow information. Please try again later.", logger=logger, rate_limiter=rate_limiter)
                    await safe_send_message(bot=bot, channel=curator_user, content=f"Error inserting asset record for {author.mention} (message {message.jump_url}): {e}. Upload aborted.", logger=logger, rate_limiter=rate_limiter)
                    return # Abort

                if created_asset_uuid and asset_lora_base_model:
                    try:
                        model_response = await asyncio.to_thread(
                            openmuse_interactor.supabase.table('models')
                            .select('id')
                            .eq('display_name', asset_lora_base_model)
                            .limit(1)
                            .execute
                        )
                        if model_response.data:
                            model_id = model_response.data[0]['id']
                            await asyncio.to_thread(
                                openmuse_interactor.supabase.table('asset_models')
                                .insert({
                                    'asset_id': created_asset_uuid,
                                    'model_id': model_id,
                                    'compatibility_note': 'inferred from workflow context',
                                })
                                .execute
                            )
                            logger.info(
                                f"[WorkflowUpload] Linked asset {created_asset_uuid} to model "
                                f"{model_id} ({asset_lora_base_model})"
                            )
                    except Exception as link_error:
                        logger.warning(f"[WorkflowUpload] Failed to create asset_models link: {link_error}")
            else:
                logger.error("[WorkflowUpload] OpenMuseInteractor or Supabase client not available for asset insert.")
                await safe_send_message(bot=bot, channel=author, content="Sorry, the system is currently unable to save workflow information.", logger=logger, rate_limiter=rate_limiter)
                await safe_send_message(bot=bot, channel=curator_user, content="Critical error: Asset persistence system not available. Aborting.", logger=logger, rate_limiter=rate_limiter)
                return # Abort

            await safe_send_message(bot=bot, channel=curator_user, content=f"{author.mention} confirmed! JSON uploaded. Asset ID: {created_asset_uuid}. (Placeholder for media & DMs)", logger=logger, rate_limiter=rate_limiter)
            
            # Section 8: Upload Media & Create Relationships
            if created_asset_uuid and media_attachments:
                logger.info(f"[WorkflowUpload] Starting upload of {len(media_attachments)} media attachments for asset {created_asset_uuid}.")
                is_first_media = True
                for media_att_item in media_attachments: # Iterate through items (tuples)
                    media_att, parent_msg, original_filename, is_converted_gif = media_att_item # Unpack, add original_filename and is_converted_gif
                    
                    if not (openmuse_interactor and hasattr(openmuse_interactor, 'upload_discord_attachment')):
                        logger.error(f"[WorkflowUpload][Media] OpenMuseInteractor.upload_discord_attachment not available. Skipping media '{original_filename}'.")
                        break # Stop trying if interactor is missing
                    
                    try:
                        logger.info(f"[WorkflowUpload][Media] Uploading media attachment '{original_filename}' (converted: {is_converted_gif})...")
                        # Assuming upload_discord_attachment takes the discord.Attachment or bytes and the author (discord.User)
                        # It handles media table insertion and returns the media_record dict.
                        
                        # Prepare the source for upload:
                        # If it's a converted GIF, media_att is already bytes.
                        # If it's a regular discord.Attachment, it remains as is.
                        upload_source = media_att 
                        
                        # Determine content type for upload
                        content_type_for_upload = None
                        if is_converted_gif:
                            content_type_for_upload = "video/mp4"
                        elif isinstance(media_att, discord.Attachment):
                            content_type_for_upload = media_att.content_type
                        
                        # The filename for storage should reflect the conversion if it happened
                        filename_for_storage = original_filename
                        if is_converted_gif:
                            # Change extension from .gif to .mp4
                            base, ext = os.path.splitext(original_filename)
                            filename_for_storage = f"{base}.mp4"


                        # upload_discord_attachment might need to be adapted or we might need a new method
                        # if it strictly expects a discord.Attachment and cannot handle bytes directly
                        # For now, assuming it can handle 'upload_source' (bytes or discord.Attachment)
                        # and that it can take an explicit content_type and filename.
                        # This part might need adjustment based on OpenMuseInteractor's capabilities.

                        # Placeholder for actual upload call - this needs to be robust
                        # For now, let's assume upload_discord_attachment can handle it,
                        # or a similar new function `upload_media_data` might be needed.
                        # Let's refine this:
                        media_record_dict = None
                        media_id_val = None # Changed variable name from media_id to avoid conflict

                        if isinstance(upload_source, discord.Attachment):
                             # Existing logic for discord.Attachment
                            media_record_dict, updated_profile_data = await openmuse_interactor.upload_discord_attachment(
                                attachment=upload_source,
                                author=author, # author is discord.User
                                message=parent_msg
                            )
                            # updated_profile_data may or may not be returned by upload_discord_attachment
                            # if it is, it might contain the updated discord_connected status.
                            # For simplicity, we don't explicitly use updated_profile_data here yet.
                            if media_record_dict and media_record_dict.get('id'):
                                media_id_val = media_record_dict.get('id')
                        elif isinstance(upload_source, bytes): # Converted GIF (bytes)
                            # Ensure author_member_id is available (from earlier profile check)
                            if not author_member_id:
                                logger.error(f"[WorkflowUpload][Media] Author member ID not available. Cannot upload converted GIF '{filename_for_storage}'.")
                            elif hasattr(openmuse_interactor, 'upload_file_to_storage') and hasattr(openmuse_interactor, 'create_media_record'):
                                # 1. Upload bytes to storage
                                media_storage_path = f"user_media/{author_member_id}/{parent_msg.id}/{filename_for_storage}"
                                media_url = await openmuse_interactor.upload_file_to_storage(
                                    file_source=upload_source, # bytes
                                    bucket_name="videos", # Assuming 'videos' bucket as per upload_discord_attachment
                                    storage_path=media_storage_path,
                                    content_type=content_type_for_upload # "video/mp4"
                                )
                                if media_url:
                                    logger.info(f"[WorkflowUpload][Media] Converted media '{filename_for_storage}' uploaded to storage: {media_url}")
                                    media_record_dict = await openmuse_interactor.create_media_record(
                                        member_id=author_member_id,
                                        media_url=media_url,
                                        filename=filename_for_storage, # The new .mp4 filename
                                        content_type=content_type_for_upload, # "video/mp4"
                                        file_size=len(upload_source), # Size of the MP4 bytes
                                        description=parent_msg.content, # Content of the message that had the GIF
                                        message=parent_msg, # The discord.Message for context
                                        author_discord_user=author, # The discord.User object for DM
                                        profile_data=author_db_profile_data, # The profile dictionary for welcome DM logic
                                        # title can be omitted for videos or use filename
                                        # placeholder_image_url and calculated_aspect_ratio are not generated for converted GIFs here
                                    )
                                    if media_record_dict and media_record_dict.get('id'):
                                        media_id_val = media_record_dict.get('id')
                                    else:
                                        logger.error(f"[WorkflowUpload][Media] Failed to create media record for converted media '{filename_for_storage}'.")
                                else:
                                    logger.error(f"[WorkflowUpload][Media] Failed to upload converted media '{filename_for_storage}' to storage.")
                            else:
                                logger.error(f"[WorkflowUpload][Media] OpenMuseInteractor missing methods for byte upload/record creation. Cannot upload converted media '{filename_for_storage}'.")
                        else:
                            logger.error(f"[WorkflowUpload][Media] upload_source is of unexpected type: {type(upload_source)}")


                        if media_record_dict and media_id_val: # Use media_id_val for the ID
                            logger.info(f"[WorkflowUpload][Media] Media '{original_filename}' (as '{filename_for_storage}') processed. Media ID: {media_id_val}")
                            
                            # Link in asset_media table
                            asset_media_data = {
                                "asset_id": created_asset_uuid,
                                "media_id": media_id_val, # Use the extracted ID
                                "is_primary": is_first_media,
                                "status": "Listed"
                            }
                            try:
                                logger.info(f"[WorkflowUpload][MediaLink] Linking asset {created_asset_uuid} to media {media_id_val} as primary: {is_first_media}.")
                                await asyncio.to_thread(
                                    openmuse_interactor.supabase.table('asset_media').insert(asset_media_data).execute
                                )
                                logger.info(f"[WorkflowUpload][MediaLink] Successfully linked asset {created_asset_uuid} to media {media_id_val}.")
                                is_first_media = False # Only first one is primary
                            except Exception as link_e:
                                logger.error(f"[WorkflowUpload][MediaLink] Failed to link asset {created_asset_uuid} to media {media_id_val}: {link_e}")
                        else:
                            logger.warning(f"[WorkflowUpload][Media] Failed to process media '{original_filename}' or media record incomplete. Response: {media_record_dict}")

                    except Exception as upload_e:
                        logger.error(f"[WorkflowUpload][Media] Exception processing media '{original_filename}': {upload_e}")
            elif media_attachments:
                logger.info("[WorkflowUpload] Media attachments were found, but asset creation failed. Skipping media upload.")
            else:
                logger.info("[WorkflowUpload] No media attachments to upload.")

            # Section 9: Notifications
            if created_asset_uuid:
                # URL: https://openmuse.ai/assets/loras/{asset_uuid}
                # Using "loras" as per spec, though it's a workflow. Consider if this path is correct.
                workflow_url = f"https://openmuse.ai/assets/loras/{created_asset_uuid}"

                # Author DM
                author_dm_content = f"🚀 Your workflow '{workflow_name}' has been uploaded to OpenMuse!\n\n"
                author_dm_content += f"You can view and manage it here: {workflow_url}\n\n"
                author_dm_content += "Thank you for sharing!"
                try:
                    await safe_send_message(bot=bot, channel=author, content=author_dm_content, logger=logger, rate_limiter=rate_limiter)
                    logger.info(f"[WorkflowUpload] Sent success DM to author {author.id} for asset {created_asset_uuid}.")
                except Exception as e:
                    logger.error(f"[WorkflowUpload] Failed to send success DM to author {author.id}: {e}")
            
                # Admin (Curator) DM
                admin_dm_content = f"✅ Workflow upload complete for {author.mention} ({author.id}).\n\n"
                admin_dm_content += f"Asset Name: '{workflow_name}'\n"
                admin_dm_content += f"Asset ID: {created_asset_uuid}\n"
                admin_dm_content += f"OpenMuse URL: {workflow_url}"
                try:
                    await safe_send_message(bot=bot, channel=curator_user, content=admin_dm_content, logger=logger, rate_limiter=rate_limiter)
                    logger.info(f"[WorkflowUpload] Sent summary DM to curator {curator_user.id} for asset {created_asset_uuid}.")
                except Exception as e:
                    logger.error(f"[WorkflowUpload] Failed to send summary DM to curator {curator_user.id}: {e}")
            else:
                logger.warning("[WorkflowUpload] Asset UUID not available. Skipping final notifications.")

            # End of pipeline if successful
            logger.info(f"[WorkflowUpload] Successfully completed workflow upload process for asset {created_asset_uuid if created_asset_uuid else '(unknown_id)'}.")
        elif view.interaction_result == "decline":
            logger.info(f"[WorkflowUpload] Author {author.id} declined. Opt-out handled in view. No further action for message {message.id}.")
            # Notification to curator already handled in the view's decline_button
        else: # Timeout
            logger.info(f"[WorkflowUpload] DM view timed out for author {author.id}. No action taken for message {message.id}.")
            # Notify curator about timeout
            await safe_send_message(bot=bot, channel=curator_user, content=f"The request to {author.mention} ({author.id}) for workflow upload timed out.", logger=logger, rate_limiter=rate_limiter)

    logger.info(f"[WorkflowUpload] Finished processing request for author {author.id}, message {message.id}.")


async def _collect_source_material(
    bot: commands.Bot,
    original_message: discord.Message, 
    author: discord.User, 
    logger: logging.Logger, 
    curator_user: discord.User,
    rate_limiter: RateLimiter
) -> Optional[Dict[str, Any]]: # Changed return type hint
    """
    Collects source material:
    1. Primary JSON attachment from the original message OR an embedded workflow from a PNG on the original message.
    2. If neither found on original message, checks PNGs in context messages for an embedded workflow.
    3. Text from surrounding messages by the same author, and their media attachments.
    Handles error reporting if no workflow source (explicit JSON or embedded) is found.
    """
    explicit_json_attachment: Optional[discord.Attachment] = None
    embedded_workflow_str: Optional[str] = None
    
    # 1. Check for Primary explicit JSON Attachment on the original message
    if original_message.attachments:
        for att in original_message.attachments:
            if att.filename.lower().endswith(".json"):
                explicit_json_attachment = att
                logger.info(f"[WorkflowUpload][CollectMaterial] Found explicit JSON attachment: {att.filename} (ID: {att.id})")
                break
    
    # Helper function to extract workflow from image bytes
    async def extract_workflow_from_image_bytes(image_bytes: bytes, att_filename: str) -> Optional[str]:
        if Image is None or UnidentifiedImageError is None: # Check if Pillow is available
            logger.warning("[WorkflowUpload][CollectMaterial] Pillow library (PIL) is not installed. Cannot extract embedded workflows from images.")
            return None
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                if img.format == "PNG":
                    workflow_json_str = None
                    # ComfyUI often stores workflow in 'prompt' or 'workflow' metadata keys.
                    # Pillow's .info dictionary stores tEXt, zTXt, and iTXt chunks.
                    # For PngImagePlugin, .text attribute (dict) is also available for text chunks.
                    # Let's try .text first if available, then .info.
                    metadata_source = None
                    if hasattr(img, 'text') and isinstance(img.text, dict):
                        metadata_source = img.text
                    elif hasattr(img, 'info') and isinstance(img.info, dict):
                         metadata_source = img.info
                    
                    if metadata_source:
                        if 'workflow' in metadata_source and isinstance(metadata_source['workflow'], str):
                            workflow_json_str = metadata_source['workflow']
                            logger.info(f"[WorkflowUpload][CollectMaterial] Found 'workflow' key in PNG metadata for {att_filename}.")
                        elif 'prompt' in metadata_source and isinstance(metadata_source['prompt'], str):
                            workflow_json_str = metadata_source['prompt'] # Often contains the full API format workflow
                            logger.info(f"[WorkflowUpload][CollectMaterial] Found 'prompt' key in PNG metadata for {att_filename}.")
                        # A common key for Stable Diffusion webui (A1111) generated images is 'parameters'
                        # ComfyUI via some nodes might also use this or custom keys.
                        # For now, focusing on 'workflow' and 'prompt'.

                        if workflow_json_str:
                            try:
                                # Basic validation: does it look like JSON?
                                test_json = json.loads(workflow_json_str)
                                # More specific check: ComfyUI JSON usually is a dict
                                if isinstance(test_json, dict):
                                    logger.info(f"[WorkflowUpload][CollectMaterial] Successfully extracted and validated embedded workflow from {att_filename}.")
                                    return workflow_json_str
                                else:
                                    logger.warning(f"[WorkflowUpload][CollectMaterial] Metadata for {att_filename} was JSON but not a dict as expected for ComfyUI workflow.")
                                    return None
                            except json.JSONDecodeError:
                                logger.warning(f"[WorkflowUpload][CollectMaterial] Metadata key content from {att_filename} was not valid JSON.")
                                return None
                    else:
                        logger.info(f"[WorkflowUpload][CollectMaterial] No 'text' or 'info' metadata found in PNG {att_filename}.")
                else:
                    logger.info(f"[WorkflowUpload][CollectMaterial] Attachment {att_filename} is not a PNG (format: {img.format}), skipping embedded workflow check.")
        except UnidentifiedImageError:
            logger.warning(f"[WorkflowUpload][CollectMaterial] Pillow could not identify image format for {att_filename} when checking for embedded workflow.")
        except Exception as e:
            logger.error(f"[WorkflowUpload][CollectMaterial] Error processing image {att_filename} for embedded workflow with Pillow: {e}")
        return None

    # 2. If no explicit JSON, check original message's attachments for embedded workflow in PNGs
    if not explicit_json_attachment and Image: # Only proceed if Pillow is available
        logger.info(f"[WorkflowUpload][CollectMaterial] No explicit JSON found on original message {original_message.id}. Checking for embedded workflows in its attachments.")
        for att in original_message.attachments:
            if att.content_type == "image/png" or att.filename.lower().endswith(".png"):
                try:
                    logger.info(f"[WorkflowUpload][CollectMaterial] Reading PNG attachment {att.filename} from original message to check for embedded workflow.")
                    image_bytes = await att.read()
                    extracted_str = await extract_workflow_from_image_bytes(image_bytes, att.filename)
                    if extracted_str:
                        embedded_workflow_str = extracted_str
                        logger.info(f"[WorkflowUpload][CollectMaterial] Using embedded workflow from {att.filename} on original message.")
                        break # Use the first one found on the original message
                except discord.HTTPException as e: # More specific exception for att.read()
                    logger.error(f"[WorkflowUpload][CollectMaterial] Failed to read attachment {att.filename} from original message (HTTPException): {e}")
                except Exception as e:
                    logger.error(f"[WorkflowUpload][CollectMaterial] Generic error processing attachment {att.filename} from original message: {e}")
            if embedded_workflow_str: # If found, no need to check other attachments of original message
                break
    
    # 3. Collect Surrounding Context Messages and their media
    context_messages_text_list: list[str] = []
    # Store as (attachment_or_bytes, parent_message, original_filename, is_converted_gif_flag)
    media_attachments_from_context: list[tuple[Union[discord.Attachment, bytes], discord.Message, str, bool]] = [] 

    time_window = datetime.timedelta(hours=12) # Changed to 12 hours
    start_time = original_message.created_at - time_window
    end_time = original_message.created_at + time_window
    
    if not original_message.channel or not hasattr(original_message.channel, 'history'):
        logger.error(f"[WorkflowUpload][CollectMaterial] Original message {original_message.id} channel is not a TextChannel or not accessible.")
        await safe_send_message(bot=bot, channel=curator_user, content=f"Error accessing channel history for message {original_message.jump_url}. Cannot collect context.", logger=logger, rate_limiter=rate_limiter)
        if not explicit_json_attachment and not embedded_workflow_str:
            return None 
    else:
        logger.info(f"[WorkflowUpload][CollectMaterial] Fetching context messages for author {author.id} in channel {original_message.channel.id} from {start_time} to {end_time}.")
        collected_messages_for_context: list[discord.Message] = []
        try:
            # Fetch messages before
            async for msg in original_message.channel.history(limit=None, after=start_time, before=original_message.created_at, oldest_first=True):
                if msg.author.id == author.id:
                    collected_messages_for_context.append(msg)
            
            # Add original message's text to context if present
            if original_message.content:
                 context_messages_text_list.append(original_message.content)

            # Fetch messages after
            async for msg in original_message.channel.history(limit=None, after=original_message.created_at, before=end_time, oldest_first=True):
                if msg.author.id == author.id:
                    collected_messages_for_context.append(msg)
            
            # Sort all collected messages (excluding original message text which is already added)
            collected_messages_for_context.sort(key=lambda m: m.created_at)

            # Truncate if necessary
            if len(collected_messages_for_context) > 200:
                logger.warning(f"[WorkflowUpload][CollectMaterial] Found {len(collected_messages_for_context)} context messages (excluding original), truncating to most recent 200.")
                # Sort by created_at descending to get most recent
                collected_messages_for_context.sort(key=lambda m: m.created_at, reverse=True)
                collected_messages_for_context = collected_messages_for_context[:200]
                # Re-sort to chronological for processing
                collected_messages_for_context.sort(key=lambda m: m.created_at)

            for msg_context in collected_messages_for_context: # Iterate through sorted, truncated messages
                if msg_context.content: # Add text content
                    context_messages_text_list.append(msg_context.content)
                
                for att_context in msg_context.attachments: # Process attachments of this context message
                    logger.info(f"[WorkflowUpload][CollectMaterialDebug] Context Msg {msg_context.id} Attachment: Filename='{att_context.filename}', Content-Type='{att_context.content_type}', Size={att_context.size}")

                    is_media_for_list = False
                    # Check if it's general media for the media_attachments_from_context list
                    if att_context.content_type and (att_context.content_type.startswith("image/") or att_context.content_type.startswith("video/")):
                        logger.info(f"[WorkflowUpload][CollectMaterialDebug] Classified as MEDIA (by content_type): {att_context.filename}")
                        
                        if att_context.content_type == "image/gif":
                            logger.info(f"[WorkflowUpload][CollectMaterialDebug] GIF detected: {att_context.filename}. Attempting conversion to MP4.")
                            gif_bytes = await att_context.read()
                            mp4_bytes = await _convert_gif_to_mp4(gif_bytes, att_context.filename, logger)
                            if mp4_bytes:
                                media_attachments_from_context.append((mp4_bytes, msg_context, att_context.filename, True))
                                logger.info(f"[WorkflowUpload][CollectMaterialDebug] Successfully queued converted MP4 for: {att_context.filename}")
                            else: # Conversion failed, append original GIF
                                media_attachments_from_context.append((att_context, msg_context, att_context.filename, False))
                                logger.warning(f"[WorkflowUpload][CollectMaterialDebug] Failed to convert GIF {att_context.filename}, using original.")
                        else:
                            media_attachments_from_context.append((att_context, msg_context, att_context.filename, False))
                        is_media_for_list = True
                    elif att_context.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.mov', '.avi', '.mkv', '.webm')):
                        if not is_media_for_list: 
                            logger.info(f"[WorkflowUpload][CollectMaterialDebug] Classified as MEDIA (by extension): {att_context.filename}")
                            if att_context.filename.lower().endswith(".gif"):
                                logger.info(f"[WorkflowUpload][CollectMaterialDebug] GIF (by extension) detected: {att_context.filename}. Attempting conversion to MP4.")
                                gif_bytes = await att_context.read() # Reading here, ensure not to double read
                                mp4_bytes = await _convert_gif_to_mp4(gif_bytes, att_context.filename, logger)
                                if mp4_bytes:
                                    media_attachments_from_context.append((mp4_bytes, msg_context, att_context.filename, True))
                                    logger.info(f"[WorkflowUpload][CollectMaterialDebug] Successfully queued converted MP4 for: {att_context.filename} (by extension)")
                                else:
                                    media_attachments_from_context.append((att_context, msg_context, att_context.filename, False))
                                    logger.warning(f"[WorkflowUpload][CollectMaterialDebug] Failed to convert GIF {att_context.filename} (by extension), using original.")
                            else:
                                media_attachments_from_context.append((att_context, msg_context, att_context.filename, False))
                        is_media_for_list = True 
                    else:
                        logger.info(f"[WorkflowUpload][CollectMaterialDebug] NOT classified as media: {att_context.filename}")

                    # 4. If no explicit JSON and no embedded workflow found yet (from original message),
                    # check this context PNG attachment for an embedded workflow.
                    if not explicit_json_attachment and not embedded_workflow_str and Image and \
                       (att_context.content_type == "image/png" or att_context.filename.lower().endswith(".png")):
                        logger.info(f"[WorkflowUpload][CollectMaterial] Checking context PNG attachment {att_context.filename} (from message {msg_context.id}) for embedded workflow.")
                        try:
                            image_bytes_context = await att_context.read()
                            extracted_str_context = await extract_workflow_from_image_bytes(image_bytes_context, att_context.filename)
                            if extracted_str_context:
                                embedded_workflow_str = extracted_str_context
                                logger.info(f"[WorkflowUpload][CollectMaterial] Using embedded workflow from context attachment {att_context.filename} (message {msg_context.id}).")
                                # Stop checking further attachments in this message and further messages for embedded workflow
                                break 
                        except discord.HTTPException as e:
                            logger.error(f"[WorkflowUpload][CollectMaterial] Failed to read context attachment {att_context.filename} (HTTPException): {e}")
                        except Exception as e:
                            logger.error(f"[WorkflowUpload][CollectMaterial] Generic error processing context attachment {att_context.filename}: {e}")
                
                if embedded_workflow_str: # If found in this message's attachments, stop checking other context messages
                    break 
            
            logger.info(f"[WorkflowUpload][CollectMaterial] Collected {len(context_messages_text_list)} text snippets and {len(media_attachments_from_context)} media attachments from context messages.")

        except discord.Forbidden:
            logger.error(f"[WorkflowUpload][CollectMaterial] Forbidden to read history in channel {original_message.channel.id}.")
            await safe_send_message(bot=bot, channel=curator_user, content=f"Bot lacks permission to read message history in {original_message.channel.mention}. Cannot collect context.", logger=logger, rate_limiter=rate_limiter)
            if not explicit_json_attachment and not embedded_workflow_str: return None 
        except discord.HTTPException as e:
            logger.error(f"[WorkflowUpload][CollectMaterial] HTTP error fetching history in channel {original_message.channel.id}: {e}")
            await safe_send_message(bot=bot, channel=curator_user, content=f"Error fetching message history in {original_message.channel.mention}: {e}. Cannot collect context.", logger=logger, rate_limiter=rate_limiter)
            if not explicit_json_attachment and not embedded_workflow_str: return None
    
    # 5. Final Check and Error Reporting if no workflow found
    if not explicit_json_attachment and not embedded_workflow_str:
        logger.warning(f"[WorkflowUpload][CollectMaterial] No explicit .json or embedded workflow found for message {original_message.id}.")
        error_msg_author = "The message you reacted to doesn't seem to have a .json workflow file, nor could I find an embedded workflow in any attached PNG images (from the original message or recent context). I can't proceed without it."
        error_msg_curator = f"The reacted message by {author.mention} ({original_message.jump_url}) is missing the required .json workflow attachment, and no embedded workflow was found in its PNGs or recent context PNGs. Upload aborted."
        try:
            await safe_send_message(bot=bot, channel=author, content=error_msg_author, logger=logger, rate_limiter=rate_limiter)
        except Exception as e: # Catch broad errors for DM sending
            logger.error(f"[WorkflowUpload][CollectMaterial] Failed to DM author {author.id} about missing JSON/embedded workflow: {e}")
        try:
            await safe_send_message(bot=bot, channel=curator_user, content=error_msg_curator, logger=logger, rate_limiter=rate_limiter)
        except Exception as e: # Catch broad errors for DM sending
            logger.error(f"[WorkflowUpload][CollectMaterial] Failed to DM curator {curator_user.id} about missing JSON/embedded workflow: {e}")
        return None

    # Concatenate text content from context messages (original message text is already in the list if present)
    full_context_text = "\n".join(context_messages_text_list)

    return {
        "json_attachment": explicit_json_attachment, 
        "embedded_workflow_str": embedded_workflow_str, 
        "context_messages_text": full_context_text,
        "media_attachments": media_attachments_from_context, 
        "original_message_content": original_message.content if original_message.content else ""
    }


async def _convert_gif_to_mp4(gif_bytes: bytes, filename: str, logger: logging.Logger) -> Optional[bytes]:
    """Converts GIF bytes to MP4 bytes using moviepy."""
    if mp is None:
        logger.warning("[WorkflowUpload][GIFConv] moviepy library is not installed. Cannot convert GIF to MP4.")
        return None
    
    try:
        logger.info(f"[WorkflowUpload][GIFConv] Attempting to convert GIF '{filename}' to MP4.")
        # Create a temporary file for the GIF
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp_gif:
            tmp_gif.write(gif_bytes)
            tmp_gif_path = tmp_gif.name
        
        # Output path for MP4
        # Ensure filename has .mp4 extension for moviepy
        base, _ = os.path.splitext(filename)
        mp4_filename_for_moviepy = f"{base}.mp4" # moviepy might need this for format detection
        tmp_mp4_path = tempfile.mktemp(suffix=".mp4")

        # Perform conversion
        clip = mp.VideoFileClip(tmp_gif_path)
        clip.write_videofile(tmp_mp4_path, codec="libx264", audio=False, logger=None) # No audio for GIFs, logger=None to suppress moviepy console output
        clip.close()

        # Read the converted MP4 bytes
        with open(tmp_mp4_path, "rb") as f_mp4:
            mp4_bytes = f_mp4.read()
        
        logger.info(f"[WorkflowUpload][GIFConv] Successfully converted '{filename}' to MP4 (size: {len(mp4_bytes)} bytes).")
        return mp4_bytes
    except Exception as e:
        logger.error(f"[WorkflowUpload][GIFConv] Failed to convert GIF '{filename}' to MP4: {e}")
        return None
    finally:
        # Clean up temporary files
        if 'tmp_gif_path' in locals() and os.path.exists(tmp_gif_path):
            os.remove(tmp_gif_path)
        if 'tmp_mp4_path' in locals() and os.path.exists(tmp_mp4_path):
            os.remove(tmp_mp4_path)


async def _generate_workflow_name(
    claude_client: Optional[ClaudeClient],
    context_text: str,
    logger: logging.Logger,
    max_chars_for_claude: int = 10000 # Approx 3k tokens, adjust as needed
) -> str:
    """
    Generates a workflow name using Claude from the provided context text.
    Returns a fallback name if Claude is unavailable or fails.
    """
    fallback_name = "Workflow"

    if not claude_client:
        logger.warning("[WorkflowUpload][NameGen] Claude client not available. Using fallback name.")
        return fallback_name

    # ADDED: Check for empty or whitespace-only context_text
    if not context_text or context_text.isspace():
        logger.info("[WorkflowUpload][NameGen] Context text is empty or whitespace. Using default name 'Workflow'.")
        return "Workflow"

    # Truncate context_text to stay within reasonable token limits for the name generation prompt
    # This is a rough character-based truncation. A proper tokenizer would be better.
    if len(context_text) > max_chars_for_claude:
        logger.info(f"[WorkflowUpload][NameGen] Context text length ({len(context_text)}) > {max_chars_for_claude} chars. Truncating.")
        # Truncate from the end, assuming more recent messages might be more relevant or to preserve start
        # Or from the beginning if that makes more sense for context flow. For now, from end.
        context_text = context_text[:max_chars_for_claude] 

    prompt = (
        "Given the user's messages, propose an accurate, technical workflow name ≤ 36 characters. "
        "Prefer wording drawn directly from the user's text. "
        "Return ONLY the name."
    )
    
    # Construct messages for Claude. Assuming generate_chat_completion takes a list of messages.
    # The exact format depends on claude_client implementation.
    # Common pattern: list of dicts with "role" and "content".
    messages_payload = [
        {"role": "user", "content": f"{prompt}\n\nUser messages:\n\n{context_text}"}
    ]

    try:
        # Assuming claude_client.generate_chat_completion returns a string response directly (the name)
        # Or it might return a more complex object from which the name needs to be extracted.
        # This will need adjustment based on the actual ClaudeClient interface.
        logger.info("[WorkflowUpload][NameGen] Calling Claude to generate workflow name...")
        response = await claude_client.generate_chat_completion(
            messages=messages_payload, 
            max_tokens=50, # Max tokens for the name itself, plus some buffer
            model="claude-sonnet-4-5-20250929",
            system_prompt="You are an expert in naming technical workflows concisely." # System prompt
        )

        if response and isinstance(response, str):
            generated_name = response.strip()
            
            # ADDED: Check for Claude's "I can't answer" messages
            non_answer_phrases = [
                "no user messages provided",
                "cannot propose a workflow name",
                "please share the messages",
                "need more context",
                "i cannot propose a workflow name" # Added variation
            ]
            # Check if the core of Claude's message indicates an inability to name
            # This is a heuristic check.
            if any(phrase in generated_name.lower() for phrase in non_answer_phrases) and len(generated_name) > 50 : # If it's a long message and contains these phrases
                logger.warning(f"[WorkflowUpload][NameGen] Claude indicated no context or inability to name: '{generated_name}'. Using 'Workflow'.")
                return "Workflow"

            # Ensure length constraint (spec says <= 36)
            if len(generated_name) > 36:
                logger.warning(f"[WorkflowUpload][NameGen] Claude generated name '{generated_name}' is > 36 chars. Truncating.")
                generated_name = generated_name[:36]
            
            if not generated_name: # Empty response after strip/truncate
                logger.warning("[WorkflowUpload][NameGen] Claude returned an effectively empty name after processing. Using 'Workflow'.")
                return "Workflow"
            
            logger.info(f"[WorkflowUpload][NameGen] Claude generated name: '{generated_name}'")
            return generated_name
        else:
            logger.warning(f"[WorkflowUpload][NameGen] Unexpected response from Claude: {response}. Using fallback name.")
            return fallback_name

    except Exception as e:
        logger.error(f"[WorkflowUpload][NameGen] Error calling Claude for workflow name generation: {e}")
        # Section 10: Claude errors -> fallback name
        return fallback_name


async def _determine_model_and_variant(
    openmuse_interactor: Optional[OpenMuseInteractor],
    claude_client: Optional[ClaudeClient],
    context_text: str,
    channel_name: str,
    logger: logging.Logger,
    max_chars_for_claude: int = 8000, # Adjusted for potentially larger model list
    max_models_to_send_claude: int = 200 # Limit number of models in prompt
) -> Optional[dict]:
    """
    Determines the model and variant using Claude based on context text and models from Supabase.
    Returns a dict {"model": ..., "variant": ...} or None if determination fails.
    """
    if not openmuse_interactor or not hasattr(openmuse_interactor, 'supabase'):
        logger.warning("[WorkflowUpload][ModelDet] OpenMuseInteractor or Supabase client not available. Cannot fetch models.")
        return None
    if not claude_client:
        logger.warning("[WorkflowUpload][ModelDet] Claude client not available. Cannot determine model/variant.")
        return None

    # 1. Fetch models from Supabase
    all_models_data = []
    try:
        logger.info("[WorkflowUpload][ModelDet] Fetching models from Supabase...")
        page = 0
        limit = 100 # Supabase docs often mention 1000, but let's be safe or use actual interactor method if available
        
        # Keep trying to get Supabase client path correctly
        supabase_client = openmuse_interactor.supabase

        while True:
            response = await asyncio.to_thread(
                supabase_client.table('models')
                .select('*') # Changed from 'id, name, variant' to '*'
                .range(page * limit, (page + 1) * limit - 1)
                .execute
            )
            # Actual data is often in response.data for Supabase Python client
            # Need to confirm the exact structure of 'response' from the Supabase client.
            # Assuming response object has a 'data' attribute list.
            # Based on spec: `models = await asyncio.to_thread( openmuse_interactor.supabase.table('models').select('*').execute)`
            # then `if .data length == 1000`. So `response.data` is likely correct.
            
            data_rows = getattr(response, 'data', []) # Safely access data
            if not data_rows:
                break
            all_models_data.extend(data_rows)
            if len(data_rows) < limit:
                break
            page += 1
            if page > 20: # Safety break for runaway loops (e.g. 20 * 100 = 2000 models)
                logger.warning("[WorkflowUpload][ModelDet] Exceeded 20 pages fetching models. Breaking.")
                break
        logger.info(f"[WorkflowUpload][ModelDet] Fetched {len(all_models_data)} models from Supabase.")
        if not all_models_data:
            logger.warning("[WorkflowUpload][ModelDet] No models found in Supabase. Cannot proceed with model determination.")
            return None

    except Exception as e:
        logger.error(f"[WorkflowUpload][ModelDet] Error fetching models from Supabase: {e}")
        return None # Cannot proceed without model list

    # Limit the number of models sent to Claude to avoid huge prompts
    if len(all_models_data) > max_models_to_send_claude:
        logger.info(f"[WorkflowUpload][ModelDet] Too many models ({len(all_models_data)}), sending only first {max_models_to_send_claude} to Claude.")
        # Potentially sort by relevance or popularity if possible, for now just take first N
        models_for_claude = all_models_data[:max_models_to_send_claude]
    else:
        models_for_claude = all_models_data

    try:
        # Adapt to the provided schema: display_name for model name, default_variant for variant
        models_prompt_data = []
        for m in models_for_claude:
            model_name = m.get('display_name')
            variant_name = m.get('default_variant') # This is text, so should be directly usable
            if model_name: # Only include if display_name is present
                models_prompt_data.append({"model_display_name": model_name, "default_variant": variant_name if variant_name else "default"}) # Use "default" if null for clarity
        
        if not models_prompt_data:
            logger.warning("[WorkflowUpload][ModelDet] No models with display_name found after fetching. Cannot proceed.")
            return None
        models_json_array_string = json.dumps(models_prompt_data)

    except Exception as e:
        logger.error(f"[WorkflowUpload][ModelDet] Error serializing models to JSON: {e}")
        return None

    # Truncate context_text
    if len(context_text) > max_chars_for_claude:
        logger.info(f"[WorkflowUpload][ModelDet] Context text length ({len(context_text)}) > {max_chars_for_claude} chars. Truncating.")
        context_text = context_text[:max_chars_for_claude]

    # Check combined size (very rough estimate)
    combined_prompt_approx_len = len(context_text) + len(models_json_array_string)
    if combined_prompt_approx_len > 25000: # Another arbitrary limit for safety
        logger.warning(f"[WorkflowUpload][ModelDet] Approx combined prompt length {combined_prompt_approx_len} is too large. Skipping Claude call.")
        return None

    prompt_template = """Based on the following user messages, the channel name this discussion occurred in, and the provided list of available models, please identify the most relevant model and its variant.

User messages:
---
{user_messages}
---
Channel Name: {channel_name}
---

Available models (model_display_name, default_variant):
---
{model_list_json}
---

Return your answer as a JSON object with two keys: "model" (matching a model_display_name from the list) and "variant".
- For "model": If the user messages or channel name strongly suggest a model from the list, use that. If the context from user messages is very weak, try to infer a suitable model based on the channel name if it seems relevant (e.g., a channel named 'sdxl-creations' might imply an SDXL model). If still uncertain, return null.
- For "variant": If the user messages mention a specific variant for the chosen model, use that. Otherwise, use the "default_variant" listed for that model. If the chosen model has no "default_variant" listed or no model was chosen, return null for the variant.

The model name you return MUST EXACTLY MATCH one of the "model_display_name" values from the provided list. The variant name you return, if not null, should ideally match a variant associated with that model if such information is implicitly available or explicitly listed as a default.
If no specific model can be confidently determined, return null for both "model" and "variant".
Example: {{"model": "Stable Diffusion XL", "variant": "1.0 Base"}}
Example if no model found: {{"model": null, "variant": null}}
Example if model found but no specific variant mentioned and default exists: {{"model": "SomeModel V3", "variant": "v3.0-default"}}
Example if model found but no specific variant and no default: {{"model": "AnotherModel", "variant": null}}
"""
    
    final_prompt = prompt_template.format(user_messages=context_text, channel_name=channel_name, model_list_json=models_json_array_string)
    
    messages_payload = [
        {"role": "user", "content": final_prompt}
    ]

    try:
        logger.info("[WorkflowUpload][ModelDet] Calling Claude to determine model/variant...")
        # Assuming generate_chat_completion can handle a `json_mode` or similar if the client supports it
        # For now, expect string output that is valid JSON.
        response_str = await claude_client.generate_chat_completion(
            messages=messages_payload, 
            max_tokens=100, # Enough for a JSON response like {"model": "name", "variant": "name"}
            model="claude-sonnet-4-5-20250929",
            system_prompt="You are an expert in identifying software models and variants from text and a list, and responding in JSON." # System prompt
        )

        if not response_str or not isinstance(response_str, str):
            logger.warning(f"[WorkflowUpload][ModelDet] Claude returned no or invalid response type: {response_str}")
            return None

        # Parse the JSON response with improved error handling for extraneous text
        final_model_data = None
        try:
            response_str_stripped = response_str.strip()
            final_model_data = json.loads(response_str_stripped)
            logger.info(f"[WorkflowUpload][ModelDet] Successfully parsed Claude response as JSON directly: {response_str_stripped}")
        except json.JSONDecodeError as e:
            problematic_doc = e.doc # This is the string that json.loads was trying to parse (response_str_stripped)
            logger.info(f"[WorkflowUpload][ModelDet] Initial JSON decode failed for full response ('{problematic_doc}'). Error at pos {e.pos}. Attempting to extract JSON prefix.")
            
            # Check if the problematic document starts like a JSON object/array
            # and if the error position is within this document and suggests a valid prefix.
            if problematic_doc and \
               (problematic_doc.startswith('{') or problematic_doc.startswith('[')) and \
               0 < e.pos <= len(problematic_doc):
                
                potential_json_str = problematic_doc[:e.pos].strip() # Extract and strip the potential JSON part
                logger.info(f"[WorkflowUpload][ModelDet] Extracted potential JSON prefix: '{potential_json_str}'")
                try:
                    final_model_data = json.loads(potential_json_str)
                    logger.info(f"[WorkflowUpload][ModelDet] Successfully parsed extracted JSON prefix.")
                except json.JSONDecodeError as e2:
                    logger.warning(f"[WorkflowUpload][ModelDet] Failed to parse extracted JSON prefix '{potential_json_str}'. Error: {e2}. Original full response: '{response_str}'")
                    return None 
            else:
                # The string didn't start with a typical JSON character, or e.pos was not useful.
                logger.warning(f"[WorkflowUpload][ModelDet] Claude response was not valid JSON and prefix extraction was not applicable. Original response: '{response_str}'")
                return None 
        
        if final_model_data is None:
             logger.error("[WorkflowUpload][ModelDet] final_model_data is None after parsing attempts, indicating an issue. Original response: '{response_str}'")
             return None

        # Validate the structure of final_model_data
        if not isinstance(final_model_data, dict) or "model" not in final_model_data or "variant" not in final_model_data:
            logger.warning(f"[WorkflowUpload][ModelDet] Parsed Claude response is not a dict or is missing 'model'/'variant' keys: {final_model_data}")
            return None
            
        model_name = final_model_data.get("model")
        variant_name = final_model_data.get("variant")

        if (model_name is not None and not isinstance(model_name, str)) or \
           (variant_name is not None and not isinstance(variant_name, str)):
            logger.warning(f"[WorkflowUpload][ModelDet] Model/variant in JSON are not strings or null: {final_model_data}")
            return None

        logger.info(f"[WorkflowUpload][ModelDet] Claude determined model: '{model_name}', variant: '{variant_name}'")
        return {"model": model_name, "variant": variant_name}

    except Exception as e:
        logger.error(f"[WorkflowUpload][ModelDet] Error calling Claude for model/variant determination or processing its response: {e}")
        # Section 10: Claude errors -> skip model fields (handled by returning None)
        return None

# Make sure to pass 'bot' to _collect_source_material call site

# Example of how this might be called from reactor.py (for context, not part of this file)
# async def on_reaction_add(reaction, user):
#     if str(reaction.emoji) == "🔧": # Example trigger emoji
#         # Assuming bot, logger, rate_limiter, db_handler, etc. are available in reactor's scope
#         await process_workflow_upload_request(
#             bot=bot, 
#             reaction=reaction, 
#             curator_user=user, 
#             logger=reactor_logger, 
#             rate_limiter=global_rate_limiter,
#             db_handler=db_handler_instance,
#             claude_client=claude_client_instance, # if needed
#             openmuse_interactor=openmuse_interactor_instance # if needed
#         )
