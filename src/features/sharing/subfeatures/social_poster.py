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

# Added helper for building Zapier captions/payloads
# Note: This is simplified. You might want different caption logic per platform.
def _build_zapier_payload(platform: str, user_details: Dict, attachment: Dict, generated_title: str, generated_description: str, original_content: Optional[str]) -> Dict:
    """Builds the payload for Zapier webhooks."""
    payload = {}
    attachment_url = attachment.get('url') # Use the original Discord URL for Zapier
    post_jump_url = attachment.get('post_jump_url', '') # Need to ensure this is passed

    # --- Common elements ---
    # Credit
    credit = ""
    handle = None
    if platform == "instagram" and user_details.get('instagram_handle'):
         handle = user_details['instagram_handle']
    elif platform == "tiktok" and user_details.get('tiktok_handle'):
         handle = user_details['tiktok_handle']
    elif platform == "youtube" and user_details.get('youtube_handle'):
         handle = user_details['youtube_handle']
    
    user_name = user_details.get('global_name') or user_details.get('username', 'the artist')
    
    if handle:
         if handle.startswith('http'): credit = handle
         elif not handle.startswith('@'): credit = f"@{handle}"
         else: credit = handle
    else:
         credit = user_name
         
    website = user_details.get('website')
    website_text = f"\n\nMore from them: {website}" if website else ""

    # --- Platform specific ---
    if platform == "instagram":
        caption = f"{generated_description} by {credit}"
        if original_content:
             caption += f"\n\nArtist Comment: \"{_truncate_with_ellipsis(original_content, 1800)}\"" # Approx limit
        caption += website_text
        payload = {
            "jump_url": post_jump_url,
            "video_url": attachment_url,
            "caption": caption.strip()
        }
    elif platform == "tiktok":
         # TikTok captions often include hashtags, consider adding logic for that
        caption = f"{generated_description} by {credit}"
        if original_content:
             caption += f"\n\nArtist Comment: \"{_truncate_with_ellipsis(original_content, 1800)}\"" # Approx limit
        caption += website_text
        payload = {
            "video_url": attachment_url,
            "caption": caption.strip()
            # Add other fields if your Zapier workflow expects them (e.g., specific hashtags)
        }
    elif platform == "youtube":
        # YouTube uses Title + Description separately
        video_title = f"\"{generated_title}\" by {credit}" # Use the dedicated title
        description = f"{generated_description}" # Use the main description
        if original_content:
             description += f"\n\nArtist Comment: \"{_truncate_with_ellipsis(original_content, 4500)}\"" # Approx limit
        description += website_text
        payload = {
            "jump_url": post_jump_url,
            "video_url": attachment_url,
            "video_title_yt": video_title.strip(),
            "caption": description.strip() # Zapier example used 'caption' for description
        }
        
    return payload

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

# --- Main Title Generation Function ---

# Remove claude_client from signature
async def generate_media_title(attachment: Dict, original_comment: Optional[str], post_id: int) -> str:
    """Generates a social media title using the LLM dispatcher for videos or images."""
    media_local_path = attachment.get('local_path')
    content_type = attachment.get('content_type', '')
    is_video = content_type.startswith('video') or (media_local_path and Path(media_local_path).suffix.lower() in ['.mp4', '.mov', '.webm', '.avi', '.mkv'])
    
    temp_frames_dir = None
    title = "Featured Creation" # Default title
    
    if not media_local_path or not os.path.exists(media_local_path):
        logger.error(f"Media file not found for title generation: {media_local_path}, Post ID: {post_id}")
        return title

    content_blocks = []
    try:
        # --- Prepare Media Content --- 
        if is_video:
            temp_frames_dir = Path(f"./temp_title_frames_{post_id}_{os.urandom(4).hex()}")
            logger.debug(f"Creating temp frame dir for title gen: {temp_frames_dir}")
            frames_extracted = _extract_frames(video_path=media_local_path, num_frames=5, save_dir=temp_frames_dir)
            if not frames_extracted:
                 logger.warning(f"Could not extract frames for video: {media_local_path}, Post ID: {post_id}")
                 # Proceed without frames if extraction failed?
            else:
                 for frame_file in sorted(temp_frames_dir.glob('frame_*.jpg'))[:5]: # Limit frames
                     mime_type = _get_media_type(str(frame_file)) # Use helper
                     base64_data = _image_to_base64(str(frame_file))
                     if mime_type and base64_data:
                         content_blocks.append({
                             "type": "image",
                             "source": {"type": "base64", "media_type": mime_type, "data": base64_data}
                         })
        elif content_type.startswith('image'):
             mime_type = _get_media_type(media_local_path) or content_type
             base64_data = _image_to_base64(media_local_path)
             if mime_type and base64_data:
                 content_blocks.append({
                     "type": "image",
                     "source": {"type": "base64", "media_type": mime_type, "data": base64_data}
                 })
        else:
            logger.warning(f"Unsupported media type for title generation: {content_type}, Post ID: {post_id}")
            # No media blocks added, rely on comment if available

        # --- Prepare Prompts --- 
        # System Prompt (Instructions)
        system_prompt = """Analyze the provided media (image frames or a single image) and the optional artist comment. 
Create an appropriate title for the media, suitable for platforms like YouTube or social media.
Rules:
- Make the title interesting, unique, and short (max 3-4 words).
- Avoid cliches.
- If the artist's comment seems to contain a title (e.g., enclosed in quotes or clearly stated), prioritize using that.
- Output ONLY the generated title text, with no additional explanation, quotation marks, or formatting."""
        
        # User Prompt (Text part of the multimodal message)
        user_prompt_text = "Generate a title for the attached media."
        if original_comment and len(original_comment.strip()) > 0:
             user_prompt_text += f"\n\nArtist's comment (consider this for title extraction or context): \"{original_comment}\""
             
        # Add the text block to the start of the content list
        content_blocks.insert(0, {"type": "text", "text": user_prompt_text})
        
        # Check if we have *any* content to send (at least the text block)
        if not content_blocks:
             logger.error(f"No content (text or media) available to generate title for Post ID: {post_id}")
             # Cleanup already done in finally block
             return title # Return default title
             
        # ---- Call LLM Dispatcher ----
        logger.info(f"Requesting title via dispatcher (Claude Sonnet) for post {post_id}...")
        
        messages = [{"role": "user", "content": content_blocks}]
        
        generated_title_text = await get_llm_response(
            client_name="claude",
            model="claude-3-5-sonnet-20240620", # Or choose Haiku if preferred for speed/cost
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=50, # Keep max_tokens reasonable for a title
        )
        
        # ---- Handle LLM response ----
        # Dispatcher raises on error, so we expect string if successful
        if generated_title_text and isinstance(generated_title_text, str):
            # Simple cleaning: remove potential quotes/newlines
            cleaned_title = generated_title_text.strip().strip('\"\'\n') 
            # Additional check for overly long titles despite max_tokens
            if len(cleaned_title.split()) > 6: # Heuristic check for too many words
                 logger.warning(f"Generated title seems long for post {post_id}: '{cleaned_title}'. Truncating.")
                 title = " ".join(cleaned_title.split()[:5]) # Take first 5 words
            else:
                 title = cleaned_title
            logger.info(f"LLM dispatcher generated title for post {post_id}: {title}")
        else:
             # This case should ideally not be reached if dispatcher works correctly
             logger.warning(f"LLM dispatcher title generation failed or returned invalid type for post {post_id}, using default: {title}")

    except Exception as e:
        logger.error(f"Error in title generation process for post {post_id}: {e}", exc_info=True)
        # Keep default title on error
    finally:
        # Clean up temporary frames directory if it was created
        if temp_frames_dir and temp_frames_dir.exists():
            try:
                shutil.rmtree(temp_frames_dir)
                logger.debug(f"Cleaned up temporary frame directory: {temp_frames_dir}")
            except Exception as e:
                logger.error(f"Error removing temporary frame directory {temp_frames_dir}: {e}", exc_info=True)
                
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

def post_to_instagram_via_zapier(payload: Dict):
    """Sends data to the Instagram Zapier webhook."""
    if not ZAPIER_INSTAGRAM_URL:
        logger.error("Cannot post to Instagram, ZAPIER_INSTAGRAM_URL not set.")
        return False
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(ZAPIER_INSTAGRAM_URL, headers=headers, data=json.dumps(payload), timeout=30)
        response.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)
        logger.info(f"Successfully sent post data to Instagram Zapier webhook for jump_url: {payload.get('jump_url')}. Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to Instagram Zapier webhook: {e}", exc_info=True)
        return False

def post_to_tiktok_via_zapier(payload: Dict):
    """Sends data to the TikTok Zapier webhook (via Buffer in example)."""
    if not ZAPIER_TIKTOK_BUFFER_URL:
        logger.error("Cannot post to TikTok, ZAPIER_TIKTOK_BUFFER_URL not set.")
        return False
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(ZAPIER_TIKTOK_BUFFER_URL, headers=headers, data=json.dumps(payload), timeout=30)
        response.raise_for_status()
        logger.info(f"Successfully sent post data to TikTok Zapier webhook for video_url: {payload.get('video_url')}. Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to TikTok Zapier webhook: {e}", exc_info=True)
        return False

def post_to_youtube_via_zapier(payload: Dict):
    """Sends data to the YouTube Zapier webhook."""
    if not ZAPIER_YOUTUBE_URL:
        logger.error("Cannot post to YouTube, ZAPIER_YOUTUBE_URL not set.")
        return False
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(ZAPIER_YOUTUBE_URL, headers=headers, data=json.dumps(payload), timeout=30)
        response.raise_for_status()
        logger.info(f"Successfully sent post data to YouTube Zapier webhook for jump_url: {payload.get('jump_url')}. Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting to YouTube Zapier webhook: {e}", exc_info=True)
        return False 