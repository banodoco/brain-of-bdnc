import discord

# Assuming OpenMuseInteractor will be passed or imported appropriately
# from src.common.openmuse_interactor import OpenMuseInteractor

async def handle_upload_to_openmuse(
    reaction: discord.Reaction,
    user: discord.User, # This is the reacting user, who should be the author
    openmuse_interactor, # Expected: OpenMuseInteractor instance
    logger # Expected: logging.Logger instance
):
    """Finds the first attachment, ensures user profile exists, uploads to Supabase Storage, adds media record, and handles video thumbnails."""
    message = reaction.message
    
    logger.info(f"[Reactor][OpenMuseUploader] Action 'upload_to_openmuse' triggered for message {message.id} by user {user.id} with emoji {reaction.emoji}.")
    logger.debug(f"[Reactor][OpenMuseUploader] Entering handle_upload_to_openmuse. Checking author: reacting user {user.id} vs message author {message.author.id}")
    
    if not openmuse_interactor:
         logger.error("[Reactor][OpenMuseUploader] OpenMuse Interactor not available. Cannot execute 'upload_to_openmuse'.")
         try: await message.add_reaction("⚙️") 
         except Exception: pass
         return

    if user.id != message.author.id:
        logger.debug(f"[Reactor][OpenMuseUploader] User {user.id} reacted with {reaction.emoji}, but is not the author ({message.author.id}) of message {message.id}. Skipping action.")
        # try: await message.add_reaction("🤔") except Exception: pass # Optional: feedback reaction
        return

    if not message.attachments:
        logger.warning(f"[Reactor][OpenMuseUploader] No attachments found on message {message.id} for 'upload_to_openmuse' action (triggered by author {user.id}).")
        try: await message.add_reaction("📎")
        except Exception: pass
        return
    
    attachment = message.attachments[0]
    logger.info(f"[Reactor][OpenMuseUploader] Calling OpenMuseInteractor.upload_discord_attachment for '{attachment.filename}'")

    media_record, profile_data = await openmuse_interactor.upload_discord_attachment(
        attachment=attachment,
        author=user, 
        message=message
    )

    if media_record:
        logger.info(f"[Reactor][OpenMuseUploader] Upload successful via Interactor for message {message.id}. Media ID: {media_record.get('id')}")
        try: await message.add_reaction("✔️")
        except Exception as e:
             logger.error(f"[Reactor][OpenMuseUploader] Failed to add success reaction: {e}")
    else:
        logger.warning(f"[Reactor][OpenMuseUploader] OpenMuseInteractor failed to create media record for message {message.id}. Profile Data: {bool(profile_data)}")
        failure_emoji = "❌"
        
        # Check for size error (assuming MAX_FILE_SIZE_BYTES is accessible via interactor)
        if hasattr(openmuse_interactor, 'MAX_FILE_SIZE_BYTES') and attachment.size > openmuse_interactor.MAX_FILE_SIZE_BYTES:
             failure_emoji = "💾"
             dm_message = "Sorry, we're too poor to host files this large right now :("
             try:
                 await user.send(dm_message)
                 logger.info(f"[Reactor][OpenMuseUploader] Sent file size limit DM to user {user.id}.")
             except discord.Forbidden:
                 logger.warning(f"[Reactor][OpenMuseUploader] Failed to send file size limit DM to user {user.id}. They may have DMs disabled.")
             except Exception as e:
                 logger.error(f"[Reactor][OpenMuseUploader] Error sending file size limit DM to user {user.id}: {e}")
        elif not profile_data:
             failure_emoji = "👤"

        try:
            await message.add_reaction(failure_emoji)
        except Exception as e:
             logger.error(f"[Reactor][OpenMuseUploader] Failed to add failure reaction '{failure_emoji}': {e}")

    logger.info(f"[Reactor][OpenMuseUploader] Finished action 'upload_to_openmuse' for message {message.id}.") 