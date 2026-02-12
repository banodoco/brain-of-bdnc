"""
Content Moderation utility using WaveSpeed AI API.
Filters inappropriate images before they're posted to the community.
"""

import json
import os
import logging
import asyncio
from typing import Optional, Dict, Any, Callable, Awaitable

import aiohttp

logger = logging.getLogger('DiscordBot')


class ContentModerator:
    """
    Content moderation using WaveSpeed AI's content-moderator/image API.
    
    Checks images for inappropriate content (NSFW, violence, etc.) 
    and returns True if the image should be blocked.
    """
    
    API_URL = "https://api.wavespeed.ai/api/v3/wavespeed-ai/content-moderator/image"
    RESULT_URL_TEMPLATE = "https://api.wavespeed.ai/api/v3/predictions/{request_id}/result"
    
    # Timeout settings
    SUBMIT_TIMEOUT = 30  # seconds
    POLL_TIMEOUT = 60    # seconds total for polling
    POLL_INTERVAL = 0.5  # seconds between polls
    
    def __init__(self):
        self.api_key = os.getenv("WAVESPEED_API_KEY")
        if not self.api_key:
            logger.warning("WAVESPEED_API_KEY not set - content moderation disabled")
    
    def is_enabled(self) -> bool:
        """Check if content moderation is available."""
        return bool(self.api_key)
    
    async def check_image(self, image_url: str) -> Dict[str, Any]:
        """
        Check an image for inappropriate content.
        
        Args:
            image_url: URL of the image to check
            
        Returns:
            Dict with:
                - 'should_block': bool - True if image should be blocked
                - 'categories': dict - Moderation categories detected (if any)
                - 'error': str - Error message if moderation failed (None otherwise)
        """
        if not self.api_key:
            return {'should_block': False, 'categories': {}, 'error': 'API key not configured'}
        
        try:
            # Submit the moderation task
            request_id = await self._submit_task(image_url)
            if not request_id:
                return {'should_block': False, 'categories': {}, 'error': 'Failed to submit task'}
            
            # Poll for results
            result = await self._poll_result(request_id)
            if result is None:
                return {'should_block': False, 'categories': {}, 'error': 'Failed to get result'}
            
            # Parse and evaluate the moderation result
            return self._evaluate_result(result)
            
        except asyncio.TimeoutError:
            logger.warning(f"Content moderation timeout for {image_url}")
            return {'should_block': False, 'categories': {}, 'error': 'Timeout'}
        except Exception as e:
            logger.error(f"Content moderation error for {image_url}: {e}")
            return {'should_block': False, 'categories': {}, 'error': str(e)}
    
    async def _submit_task(self, image_url: str) -> Optional[str]:
        """Submit a moderation task and return the request ID."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "enable_sync_mode": False,
            "image": image_url
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.API_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        request_id = data.get("data", {}).get("id")
                        if request_id:
                            logger.debug(f"Content moderation task submitted: {request_id}")
                            return request_id
                        else:
                            logger.warning(f"No request ID in response: {data}")
                            return None
                    else:
                        text = await response.text()
                        logger.warning(f"Failed to submit moderation task: HTTP {response.status} - {text}")
                        return None
        except Exception as e:
            logger.error(f"Error submitting moderation task: {e}")
            return None
    
    async def _poll_result(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Poll for moderation result until completion or timeout."""
        url = self.RESULT_URL_TEMPLATE.format(request_id=request_id)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        elapsed = 0
        async with aiohttp.ClientSession() as session:
            while elapsed < self.POLL_TIMEOUT:
                try:
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            result = data.get("data", {})
                            status = result.get("status")
                            
                            if status == "completed":
                                logger.debug(f"Content moderation completed for {request_id}")
                                return result
                            elif status == "failed":
                                logger.warning(f"Content moderation failed for {request_id}: {result.get('error')}")
                                return None
                            else:
                                # Still processing
                                await asyncio.sleep(self.POLL_INTERVAL)
                                elapsed += self.POLL_INTERVAL
                        else:
                            text = await response.text()
                            logger.warning(f"Error polling result: HTTP {response.status} - {text}")
                            return None
                except Exception as e:
                    logger.error(f"Error polling moderation result: {e}")
                    return None
        
        logger.warning(f"Content moderation poll timeout for {request_id}")
        return None
    
    def _evaluate_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate the moderation result and determine if image should be blocked.
        
        The WaveSpeed API returns outputs with moderation categories.
        Block if any category returns True.
        """
        outputs = result.get("outputs", [])
        
        # Log the raw result for debugging
        logger.debug(f"Content moderation raw result: {result}")
        
        # The outputs can be:
        # 1. A URL to a JSON file with results
        # 2. A dict with category scores/flags
        # 3. A list of category results
        
        categories = {}
        should_block = False
        
        if outputs:
            # If outputs[0] is a dict (inline result), parse it directly
            if isinstance(outputs[0], dict):
                categories = outputs[0]
                # Check if any category is flagged as True or above threshold
                for key, value in categories.items():
                    if isinstance(value, bool) and value:
                        should_block = True
                        break
                    elif isinstance(value, (int, float)) and value > 0.5:
                        should_block = True
                        break
            elif isinstance(outputs[0], str):
                # It might be a URL to the result or a JSON string
                try:
                    import json
                    parsed = json.loads(outputs[0])
                    if isinstance(parsed, dict):
                        categories = parsed
                        for key, value in categories.items():
                            if isinstance(value, bool) and value:
                                should_block = True
                                break
                            elif isinstance(value, (int, float)) and value > 0.5:
                                should_block = True
                                break
                except (json.JSONDecodeError, TypeError):
                    # Not JSON, might be URL - log it
                    logger.debug(f"Content moderation output is string: {outputs[0][:200]}")
        
        if should_block:
            logger.info(f"Content blocked by moderation. Categories: {categories}")
        
        return {
            'should_block': should_block,
            'categories': categories,
            'error': None
        }


# Singleton instance for easy access
_moderator: Optional[ContentModerator] = None


def get_content_moderator() -> ContentModerator:
    """Get the singleton content moderator instance."""
    global _moderator
    if _moderator is None:
        _moderator = ContentModerator()
    return _moderator


async def should_block_image(image_url: str) -> bool:
    """
    Convenience function to check if an image should be blocked.
    
    Args:
        image_url: URL of the image to check
        
    Returns:
        True if image should be blocked, False otherwise
    """
    moderator = get_content_moderator()
    if not moderator.is_enabled():
        return False
    
    result = await moderator.check_image(image_url)
    return result.get('should_block', False)


# Type alias for message fetcher callback
# Takes (channel_id, message_id) -> returns message object with .attachments or None
MessageFetcher = Callable[[int, str], Awaitable[Any]]


async def filter_summary_media(
    summary_json: str, 
    fetch_message: MessageFetcher
) -> str:
    """
    Filter a summary JSON to remove media that fails content moderation.
    
    Checks each media message's image attachments against the content moderator
    and removes references to blocked content from the JSON.
    
    Args:
        summary_json: JSON string containing summary items with media references
        fetch_message: Async callback to fetch a Discord message.
                      Signature: async (channel_id: int, message_id: str) -> message or None
                      The returned message should have an .attachments attribute.
    
    Returns:
        Filtered JSON string with blocked media references removed
    """
    moderator = get_content_moderator()
    if not moderator.is_enabled():
        logger.debug("Content moderation disabled - skipping filter")
        return summary_json
    
    try:
        items = json.loads(summary_json)
        if not isinstance(items, list):
            return summary_json
        
        blocked_count = 0
        
        for item in items:
            channel_id = item.get('channel_id')
            if not channel_id:
                continue
            
            # Check mainMediaMessageId
            main_media_id = item.get('mainMediaMessageId')
            if main_media_id:
                is_blocked = await _check_message_media_blocked(
                    int(channel_id), str(main_media_id), fetch_message
                )
                if is_blocked:
                    item['mainMediaMessageId'] = None
                    blocked_count += 1
                    logger.info(f"Blocked mainMediaMessageId {main_media_id} from topic '{item.get('title', 'unknown')}'")
            
            # Check subTopicMediaMessageIds
            for sub in item.get('subTopics', []):
                sub_channel_id = sub.get('channel_id', channel_id)
                media_ids = sub.get('subTopicMediaMessageIds', [])
                if media_ids:
                    filtered_ids = []
                    for media_id in media_ids:
                        if media_id:
                            is_blocked = await _check_message_media_blocked(
                                int(sub_channel_id), str(media_id), fetch_message
                            )
                            if is_blocked:
                                blocked_count += 1
                                logger.info(f"Blocked subTopicMediaMessageId {media_id} from subtopic")
                            else:
                                filtered_ids.append(media_id)
                    sub['subTopicMediaMessageIds'] = filtered_ids
        
        if blocked_count > 0:
            logger.info(f"Content moderation blocked {blocked_count} media references from summary")
        
        return json.dumps(items)
        
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Failed to filter summary JSON for moderation: {e}")
        return summary_json


async def _check_message_media_blocked(
    channel_id: int, 
    message_id: str, 
    fetch_message: MessageFetcher
) -> bool:
    """
    Check if any image attachments in a message should be blocked.
    
    Args:
        channel_id: Discord channel ID
        message_id: Discord message ID
        fetch_message: Callback to fetch the message
        
    Returns:
        True if any image attachment is blocked, False otherwise
    """
    try:
        message = await fetch_message(channel_id, message_id)
        if not message or not hasattr(message, 'attachments') or not message.attachments:
            return False
        
        # Check each image attachment
        for attachment in message.attachments:
            content_type = getattr(attachment, 'content_type', '') or ''
            filename = getattr(attachment, 'filename', '') or ''
            
            # Only check images (not videos - API is image-specific)
            is_image = (
                content_type.startswith('image/') or 
                filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
            )
            
            if is_image:
                try:
                    if await should_block_image(attachment.url):
                        logger.info(f"Content moderation blocked {filename} from message {message_id}")
                        return True
                except Exception as e:
                    logger.warning(f"Content moderation check failed for {filename}: {e}")
                    # Don't block on moderation errors
        
        return False
        
    except Exception as e:
        logger.error(f"Error checking message {message_id} for moderation: {e}")
        return False

