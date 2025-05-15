import discord
import logging

# Assuming Sharer class will be passed or imported appropriately
# from src.features.sharing.sharer import Sharer

async def handle_send_tweet_about_message(
    reaction: discord.Reaction,
    user: discord.User, # Reacting user
    sharer_instance, # Expected: Sharer instance
    logger # Expected: logging.Logger instance
):
    """Initiates the sharing process via the Sharer class."""
    message = reaction.message
    logger.info(f"[Reactor][TweetSharerBridge] Action 'send_tweet_about_message' triggered for message: {message.id} by user {user.id}. Initiating sharing process.")

    if sharer_instance:
        await sharer_instance.initiate_sharing_process_from_reaction(reaction, user)
    else:
        logger.error("[Reactor][TweetSharerBridge] Sharer instance not available. Cannot initiate sharing process.") 