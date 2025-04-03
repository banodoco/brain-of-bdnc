# Placeholder for social_poster functions 

import tweepy
import os
import asyncio
import logging
from typing import Dict, Optional, List
from pathlib import Path

logger = logging.getLogger('DiscordBot')

# --- Environment Variable Check ---
# Check for keys at import time to fail fast
CONSUMER_KEY = os.getenv("TWITTER_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
    logger.critical("Twitter API credentials missing in environment variables!")
    # You might want to raise an exception here or handle it appropriately
    # raise ValueError("Missing Twitter API credentials")

# --- Helper Functions ---

def _truncate_with_ellipsis(text: str, max_length: int) -> str:
    """Truncates text to max_length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    else:
        # Adjust length to account for ellipsis and potential space
        return text[:max_length-4] + "..."

def _build_tweet_caption(base_description: str, user_details: Dict, original_content: Optional[str]) -> str:
    """Builds the final tweet caption, adding user handles and context."""
    caption_parts = [base_description]
    max_len = 280 # Twitter limit
    current_len = len(base_description)

    # Add Artist Credit
    artist_credit = "" 
    twitter_handle = user_details.get('twitter_handle')
    user_name = user_details.get('global_name') or user_details.get('username', 'the artist') # Fallback name
    
    if twitter_handle:
        # Format handle correctly (@username or full URL)
        if twitter_handle.startswith('http'):
             handle_text = twitter_handle
        elif not twitter_handle.startswith('@'):
             handle_text = f"@{twitter_handle}"
        else:
             handle_text = twitter_handle
        artist_credit = f" by {handle_text}"
    else:
        artist_credit = f" by {user_name}"
    
    if current_len + len(artist_credit) <= max_len:
         caption_parts.append(artist_credit)
         current_len += len(artist_credit)
    else:
         logger.warning("Caption too long to add artist credit.")

    # Add Original Comment (if space permits)
    if original_content and len(original_content.strip()) > 0:
        comment_prefix = "\n\nArtist Comment: \""
        comment_suffix = "\""
        available_len = max_len - current_len - len(comment_prefix) - len(comment_suffix)
        if available_len > 20: # Need some minimum space for the comment
             truncated_comment = _truncate_with_ellipsis(original_content.strip(), available_len)
             caption_parts.append(f"{comment_prefix}{truncated_comment}{comment_suffix}")
             current_len += len(comment_prefix) + len(truncated_comment) + len(comment_suffix)
        else:
             logger.warning("Caption too long to add original comment.")

    # Add Website (if space permits)
    website = user_details.get('website')
    if website:
        website_prefix = "\n\nMore from them: "
        available_len = max_len - current_len - len(website_prefix)
        if available_len > len(website): # Check if the full URL fits
             caption_parts.append(f"{website_prefix}{website}")
             current_len += len(website_prefix) + len(website)
        else:
             logger.warning("Caption too long to add website link.")

    return "".join(caption_parts).strip()


# --- Main Posting Function ---

async def post_tweet(generated_description: str, user_details: Dict, attachments: List[Dict], original_content: Optional[str]) -> Optional[str]:
    """Uploads media and posts a tweet with a generated caption."""
    
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
         logger.error("Cannot post tweet, API credentials missing.")
         return None

    if not attachments:
        logger.error("Cannot post tweet, no attachments provided.")
        return None

    # Assume the first attachment is the primary one to post
    # TODO: Handle multiple attachments if Twitter API allows/needed
    attachment = attachments[0]
    media_path = attachment.get('local_path')
    if not media_path or not os.path.exists(media_path):
        logger.error(f"Cannot post tweet, media file path invalid or file missing: {media_path}")
        return None
        
    filename = attachment.get('filename', Path(media_path).name)
    file_extension = Path(filename).suffix.lower()

    # Build the final caption
    final_caption = _build_tweet_caption(generated_description, user_details, original_content)
    logger.info(f"Final Tweet Caption: {final_caption}") # Log the caption being used

    try:
        # --- Media Upload (v1.1 API) ---
        auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
        auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth)
        
        loop = asyncio.get_event_loop()
        
        logger.info(f"Uploading media ({filename}) to Twitter...")
        if file_extension == '.gif':
            # GIFs need chunked upload and specific media category
            media = await loop.run_in_executor(None,
                lambda: api_v1.media_upload(media_path, chunked=True, media_category="tweet_gif")
            )
        else:
             # Other types (images/videos) - use standard upload (chunked is good practice for videos)
             # Tweepy v1's media_upload handles chunking automatically if file is large enough
             media = await loop.run_in_executor(None,
                 lambda: api_v1.media_upload(media_path, chunked=True)
             )

        media_id = media.media_id_string
        logger.info(f"Twitter Media Upload successful. Media ID: {media_id}")

        # --- Create Tweet (v2 API) ---
        client_v2 = tweepy.Client(
            consumer_key=CONSUMER_KEY,
            consumer_secret=CONSUMER_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )
        
        logger.info("Creating tweet...")
        tweet = await loop.run_in_executor(None,
             lambda: client_v2.create_tweet(text=final_caption, media_ids=[media_id])
        )

        tweet_id = tweet.data['id']
        tweet_url = f"https://twitter.com/user/status/{tweet_id}" # Generic URL, replace 'user' if you know the bot's handle
        # Better: Extract username from authenticated user if possible, or use a config value
        # Example (might need API call): bot_user = api_v1.verify_credentials() -> tweet_url = f"https://twitter.com/{bot_user.screen_name}/status/{tweet_id}"
        
        logger.info(f"Tweet posted successfully: {tweet_url}")
        return tweet_url

    except tweepy.errors.TweepyException as e:
        logger.error(f"Twitter API error during posting: {e}", exc_info=True)
        # Specific error handling can be added here (e.g., rate limits, media processing errors)
        if "duplicate content" in str(e).lower():
             logger.warning("Tweet failed due to duplicate content.")
             # Decide if you want to return a specific marker or None
        return None
    except Exception as e:
        logger.error(f"Unexpected error during Twitter posting: {e}", exc_info=True)
        return None 