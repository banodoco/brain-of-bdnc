# Placeholder for social_poster functions 

import tweepy
import os
import asyncio
import logging
import json
import requests
import cv2
import shutil
import base64
from typing import Dict, Optional, List
from pathlib import Path

from src.common.llm import get_llm_response

logger = logging.getLogger('DiscordBot')

# --- Environment Variable Check ---
# Check for keys at import time to fail fast
CONSUMER_KEY = os.getenv("TWITTER_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

# Added Zapier URL checks
ZAPIER_TIKTOK_BUFFER_URL = os.getenv("ZAPIER_TIKTOK_BUFFER_URL")
ZAPIER_INSTAGRAM_URL = os.getenv("ZAPIER_INSTAGRAM_URL")
ZAPIER_YOUTUBE_URL = os.getenv("ZAPIER_YOUTUBE_URL")

if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
    logger.critical("Twitter API credentials missing in environment variables!")
    # raise ValueError("Missing Twitter API credentials") # Consider uncommenting

if not ZAPIER_TIKTOK_BUFFER_URL:
    logger.warning("ZAPIER_TIKTOK_BUFFER_URL missing from environment variables!")
if not ZAPIER_INSTAGRAM_URL:
    logger.warning("ZAPIER_INSTAGRAM_URL missing from environment variables!")
if not ZAPIER_YOUTUBE_URL:
    logger.warning("ZAPIER_YOUTUBE_URL missing from environment variables!")

# --- Helper Functions ---

def _truncate_with_ellipsis(text: str, max_length: int) -> str:
    """Truncates text to max_length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    else:
        # Adjust length to account for ellipsis and potential space
        return text[:max_length-4] + "..."

def _build_tweet_caption(base_description: str, user_details: Dict, original_content: Optional[str]) -> str:
    """Builds the final tweet caption, prioritizing base_description and then adding credit."""
    
    user_global_name = user_details.get('global_name')
    user_discord_name = user_details.get('username')
    raw_twitter_handle = user_details.get('twitter_handle')
    artist_credit_text = None

    if raw_twitter_handle:
        handle_val = raw_twitter_handle.strip()
        extracted_username = None
        is_url_like_structure = '://' in handle_val or \
                               'x.com/' in handle_val.lower() or \
                               'twitter.com/' in handle_val.lower()
        if handle_val.startswith('@') and is_url_like_structure:
            handle_val = handle_val[1:]
        if '://' in handle_val:
            path_after_scheme = handle_val.split('://', 1)[-1]
            domain_and_path_lower = path_after_scheme.lower() 
            if domain_and_path_lower.startswith('twitter.com/'):
                extracted_username = path_after_scheme[len('twitter.com/'):].split('/')[0]
            elif domain_and_path_lower.startswith('www.twitter.com/'):
                extracted_username = path_after_scheme[len('www.twitter.com/'):].split('/')[0]
            elif domain_and_path_lower.startswith('x.com/'):
                extracted_username = path_after_scheme[len('x.com/'):].split('/')[0]
            elif domain_and_path_lower.startswith('www.x.com/'):
                extracted_username = path_after_scheme[len('www.x.com/'):].split('/')[0]
        elif 'x.com/' in handle_val.lower():
            match_pattern = 'x.com/'
            start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
            extracted_username = handle_val[start_idx:].split('/')[0]
        elif 'twitter.com/' in handle_val.lower():
            match_pattern = 'twitter.com/'
            start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
            extracted_username = handle_val[start_idx:].split('/')[0]
        else:
            extracted_username = handle_val
        if extracted_username:
            cleaned_username = extracted_username.split('?')[0].split('#')[0]
            if cleaned_username.startswith('@'):
                cleaned_username = cleaned_username[1:]
            if cleaned_username:
                artist_credit_text = f"@{cleaned_username}"

    if not artist_credit_text: 
        if user_global_name:
            artist_credit_text = user_global_name
        elif user_discord_name:
            artist_credit_text = user_discord_name
        else:
            artist_credit_text = "the artist" # Fallback if no names found
    
    was_base_description_provided = base_description and base_description.strip()

    if was_base_description_provided:
        final_caption = base_description.strip() # Use the provided base_description directly
    else:
        # If no base_description, construct the default "Top art post..." and add artist comment
        final_caption = f"Top art post of the day by {artist_credit_text}"

        if original_content and original_content.strip():
            comment_to_add = original_content.strip()
            # Using a simpler format for the artist comment part from the default flow
            # to distinguish from the potentially more custom format passed in base_description.
            # Max length for tweet is 280. Ellipsis is 3 chars. "\n\nArtist's Comment: \"...\"" is ~24 chars.
            # So, caption + comment_structure should be less than 280.
            # Available for comment_to_add = 280 - len(final_caption) - 24
            comment_format_overhead = len("\n\nArtist's Comment: \"\"") 
            full_comment_section = f"\n\nArtist's Comment: \"{comment_to_add}\""

            if len(final_caption) + len(full_comment_section) <= 280:
                final_caption += full_comment_section
            else:
                available_len_for_comment_text = 280 - len(final_caption) - comment_format_overhead
                if available_len_for_comment_text > 10: # Ensure some space for a meaningful truncated comment
                    truncated_comment = _truncate_with_ellipsis(comment_to_add, available_len_for_comment_text)
                    final_caption += f"\n\nArtist's Comment: \"{truncated_comment}\""
                else:
                     # Not enough space to add even a truncated comment meaningfully.
                     logger.warning(f"Caption (default flow) for '{artist_credit_text}' too long to add artist comment: '{comment_to_add[:30]}...'")
            
    # Final length check and truncation if necessary (applies to both cases)
    if len(final_caption) > 280:
        logger.warning(f"Final constructed caption exceeds 280 chars. Truncating. Original: '{final_caption}'")
        # A more generic truncation if it still exceeds, regardless of how it was formed.
        final_caption = _truncate_with_ellipsis(final_caption, 280)

    return final_caption.strip()

# --- Added Title Generation Helpers ---

def _image_to_base64(image_path: str) -> Optional[str]:
    """Converts an image file to a base64 encoded string."""
    try:
        with open(image_path, 'rb') as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding image {image_path} to base64: {e}", exc_info=True)
        return None

def _extract_frames(video_path: str, num_frames: int, save_dir: Path) -> bool:
    """Extracts a specified number of evenly distributed frames from a video."""
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        vidcap = cv2.VideoCapture(video_path)
        if not vidcap.isOpened():
            logger.error(f"Failed to open video file: {video_path}")
            return False

        total_frames = int(vidcap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames < 1:
            logger.warning(f"Video {video_path} has no frames.")
            vidcap.release()
            return False
        
        # Ensure num_frames is not greater than total_frames
        num_frames = min(num_frames, total_frames)
        if num_frames < 1:
            logger.warning(f"Cannot extract less than 1 frame from {video_path}.")
            vidcap.release()
            return False
            
        # Calculate interval, ensuring it's at least 1 frame
        frames_interval = max(1, total_frames // num_frames)
        
        extracted_count = 0
        for i in range(num_frames):
            frame_id = i * frames_interval
            vidcap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
            success, image = vidcap.read()
            if success:
                save_path = save_dir / f"frame_{extracted_count}.jpg"
                cv2.imwrite(str(save_path), image)
                extracted_count += 1
            else:
                logger.warning(f"Failed to read frame {frame_id} from {video_path}")
                # Optionally break if frame read fails

        vidcap.release()
        logger.info(f"Extracted {extracted_count} frames from {video_path} to {save_dir}")
        return extracted_count > 0
    except Exception as e:
        logger.error(f"Error extracting frames from {video_path}: {e}", exc_info=True)
        if vidcap.isOpened(): vidcap.release() # Ensure release on error
        return False

# --- Claude Interaction (Video Frames) --- REFACTORED ---
# Make the function async
async def _make_claude_title_request(frames_dir: Path,
                                     original_comment: Optional[str]) -> Optional[str]:
    """Makes a request via LLM dispatcher to generate a title from video frames."""
    try:
        # Prepare ≤ 5 JPEG frames (already extracted elsewhere) as base64
        image_paths = list(frames_dir.glob("*.jpg"))[:5]
        content_blocks = []
        for image_path in image_paths:
            base64_image = _image_to_base64(str(image_path))
            if base64_image:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_image
                    }
                })

        if not any(item['type'] == 'image' for item in content_blocks):
            logger.warning(f"No valid image frames found in {frames_dir} to send via dispatcher.")
            return None

        # Define System Prompt
        system_prompt = (
            "Analyze these video frames. Create a short, interesting, unique "
            "title (max 3-4 words). Avoid clichés. If the user comment suggests "
            "a title, prioritize that. Output ONLY the title."
        )
        
        # Define User Prompt Text Block
        user_prompt_text = "Generate a title for the attached video frames."
        if original_comment and original_comment.strip():
             user_prompt_text += f'\n\nArtist\'s comment: "{original_comment}"'

        # Add text block *first* for dispatcher format
        content_blocks.insert(0, {"type": "text", "text": user_prompt_text})
        
        # Prepare messages list for dispatcher
        messages_for_dispatcher = [{"role": "user", "content": content_blocks}]

        # ---- Call LLM Dispatcher ----
        logger.info("Requesting video title via dispatcher (Claude Sonnet)...")
        generated_title = await get_llm_response(
            client_name="claude",
            model="claude-3-5-sonnet-20240620", # Or appropriate model
            system_prompt=system_prompt,
            messages=messages_for_dispatcher,
            max_tokens=50,
            temperature=0.5 # Pass other kwargs if needed
        )

        # Dispatcher should return string or raise error
        if generated_title and isinstance(generated_title, str):
            cleaned_title = generated_title.strip().strip('\"\'')
            logger.info(f"Dispatcher generated title from video frames: {cleaned_title}")
            return cleaned_title
        else:
            # This case might indicate an issue with dispatcher response handling if reached
            logger.warning(f"Dispatcher returned unexpected response for video title: {generated_title}")
            return None

    except Exception as e:
        # Catch errors from dispatcher or content preparation
        logger.error(f"Error generating title from video frames via dispatcher: {e}", exc_info=True)
        return None

# --- Main Title Generation Function --- REFACTORED ---
async def generate_media_title(attachment: Dict, original_comment: Optional[str], post_id: int) -> str:
    """
    Generates a social media title using the LLM Dispatcher (ClaudeClient).
    For videos: Extracts frames, prepares content, calls dispatcher.
    For images: Prepares base64 image content, calls dispatcher.
    """
    media_local_path = attachment.get('local_path')
    content_type = attachment.get('content_type', '')
    title = "Featured Artwork"  # Default title
    temp_frames_dir = None

    if not media_local_path or not os.path.exists(media_local_path):
        logger.error(f"Media file not found for title generation: {media_local_path}, Post ID: {post_id}")
        return title

    is_video = content_type.startswith('video') or Path(media_local_path).suffix.lower() in ['.mp4', '.mov', '.webm', '.avi', '.mkv']
    is_image = content_type.startswith('image') # Includes gifs

    try:
        generated_title_text = None

        if is_video:
            logger.info(f"Generating title for video via dispatcher: {media_local_path} (Post ID: {post_id})")
            temp_frames_dir = Path(f"./temp_title_frames_{post_id}_{os.urandom(4).hex()}")
            if _extract_frames(video_path=media_local_path, num_frames=5, save_dir=temp_frames_dir):
                # Call the refactored async helper directly
                generated_title_text = await _make_claude_title_request(temp_frames_dir, original_comment)
            else:
                logger.warning(f"Frame extraction failed for {media_local_path}. Cannot generate title from video.")

        elif is_image:
            logger.info(f"Generating title for image via dispatcher: {media_local_path} (Post ID: {post_id})")
            base64_image = _image_to_base64(media_local_path)
            if base64_image:
                 # Determine mime type
                 mime_type = content_type if content_type.startswith('image/') else 'image/jpeg'
                 suffix = Path(media_local_path).suffix.lower()
                 if suffix == '.gif': mime_type = 'image/gif'
                 elif suffix == '.png': mime_type = 'image/png'
                 elif suffix == '.webp': mime_type = 'image/webp'
                 
                 # Prepare content blocks for dispatcher
                 content_blocks = [
                     { # Text block first
                        "type": "text",
                         "text": (
                             f"Generate a title for the attached image." +
                             (f'\n\nArtist\'s comment: "{original_comment}"' if original_comment and original_comment.strip() else "")
                         )
                     },
                     { # Image block
                        "type": "image",
                         "source": {
                             "type": "base64",
                             "media_type": mime_type,
                             "data": base64_image
                         }
                     }
                 ]
                 
                 # System prompt for images
                 system_prompt = (
                     "Analyze this image. Create a short, interesting, unique "
                     "title (max 3-4 words). Avoid clichés. If the user comment suggests "
                     "a title, prioritize that. Output ONLY the title."
                 )

                 # Prepare messages list for dispatcher
                 messages_for_dispatcher = [{"role": "user", "content": content_blocks}]

                 # ---- Call LLM Dispatcher ----
                 logger.info("Requesting image title via dispatcher (Claude Sonnet)...")
                 llm_response = await get_llm_response(
                     client_name="claude",
                     model="claude-3-5-sonnet-20240620", # Or appropriate model
                     system_prompt=system_prompt,
                     messages=messages_for_dispatcher,
                     max_tokens=50,
                     temperature=0.5
                 )
                 
                 if llm_response and isinstance(llm_response, str):
                     generated_title_text = llm_response.strip().strip('\"\'')
                     logger.info(f"Dispatcher generated title from image: {generated_title_text}")
                 else:
                     logger.warning(f"Dispatcher returned unexpected response for image title: {llm_response}")

            else:
                logger.warning(f"Could not encode image {media_local_path} to base64. Cannot generate title.")
        else:
            logger.warning(f"Unsupported media type for title generation: {content_type}, Post ID: {post_id}. Using default title.")

        # Update title if generation was successful
        if generated_title_text:
            title = generated_title_text

    except Exception as e:
        logger.error(f"Error in generate_media_title (dispatcher path) for Post ID {post_id}: {e}", exc_info=True)
        # Fallback to default title is handled by initial assignment

    finally:
        # Clean up temporary frame directory if it was created
        if temp_frames_dir and temp_frames_dir.exists():
            try:
                shutil.rmtree(temp_frames_dir)
                logger.debug(f"Cleaned up temp frame directory: {temp_frames_dir}")
            except Exception as e_clean:
                logger.error(f"Error cleaning up temp frame directory {temp_frames_dir}: {e_clean}", exc_info=True)

    return title

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

# --- Added Zapier Posting Functions ---

async def post_to_instagram_via_zapier(user_details: Dict, attachments: List[Dict], caption: str, jump_url: str):
    """Posts media to Instagram using a Zapier webhook."""
    # TODO: Replace with the correct ZAPIER_INSTAGRAM_URL from environment variables
    zapier_url = ZAPIER_INSTAGRAM_URL or "https://hooks.zapier.com/hooks/catch/19278166/your_instagram_hook/"
    if not zapier_url or "your_instagram_hook" in zapier_url:
        logger.error("Cannot post to Instagram: ZAPIER_INSTAGRAM_URL is not set or is a placeholder.")
        return
    if not attachments:
        logger.warning("post_to_instagram_via_zapier called without attachments.")
        return

    payload = {
        "user_details": user_details,
        "media_url": attachments[0].get('url'),
        "caption": caption,
        "jump_url": jump_url
    }
    
    try:
        response = await asyncio.to_thread(requests.post, zapier_url, json=payload)
        response.raise_for_status()
        logger.info(f"Successfully sent post data to Instagram Zapier webhook for jump_url: {jump_url}. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to Instagram Zapier webhook: {e}")

async def post_to_tiktok_via_zapier(user_details: Dict, attachments: List[Dict], caption: str, jump_url: str):
    """Posts a video to TikTok using a Zapier Buffer webhook."""
    # TODO: Replace with the correct ZAPIER_TIKTOK_BUFFER_URL from environment variables
    zapier_url = ZAPIER_TIKTOK_BUFFER_URL or "https://hooks.zapier.com/hooks/catch/19278166/your_tiktok_hook/"
    if not zapier_url or "your_tiktok_hook" in zapier_url:
        logger.error("Cannot post to TikTok: ZAPIER_TIKTOK_BUFFER_URL is not set or is a placeholder.")
        return
    if not attachments:
        logger.warning("post_to_tiktok_via_zapier called without attachments.")
        return

    payload = {
        "user_details": user_details,
        "media_url": attachments[0].get('url'),
        "caption": caption,
        "jump_url": jump_url
    }

    try:
        response = await asyncio.to_thread(requests.post, zapier_url, json=payload)
        response.raise_for_status()
        logger.info(f"Successfully sent post data to TikTok Zapier webhook for jump_url: {jump_url}. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to TikTok Zapier webhook: {e}")

async def post_to_youtube_via_zapier(user_details: Dict, attachments: List[Dict], title: str, description: str, jump_url: str):
    """Uploads a video to YouTube via a Zapier webhook."""
    if not ZAPIER_YOUTUBE_URL:
        logger.error("Cannot post to YouTube: ZAPIER_YOUTUBE_URL is not set.")
        return
    if not attachments:
        logger.warning("post_to_youtube_via_zapier called without attachments.")
        return

    payload = {
        "user_details": user_details,
        "media_url": attachments[0].get('url'),
        "video_title": title,
        "video_description": description,
        "jump_url": jump_url
    }

    try:
        response = await asyncio.to_thread(requests.post, ZAPIER_YOUTUBE_URL, json=payload)
        response.raise_for_status()
        logger.info(f"Successfully sent post data to YouTube Zapier webhook for jump_url: {jump_url}. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to YouTube Zapier webhook: {e}")

    if not tweet_url:
        logger.error(f"Failed to post tweet for attachments: {[att.get('filename') for att in attachments]}")
    
    return tweet_url 