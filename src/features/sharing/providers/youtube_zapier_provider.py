from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

from ..models import SocialPublishRequest
from . import SocialPublishProvider

logger = logging.getLogger('DiscordBot')


class YouTubeZapierProvider(SocialPublishProvider):
    """YouTube publishing provider backed by a Zapier catch-hook."""

    def __init__(self, webhook_env_var: str = 'ZAPIER_YOUTUBE_URL'):
        self.webhook_env_var = webhook_env_var

    async def publish(self, request: SocialPublishRequest) -> Optional[Dict[str, Any]]:
        if request.action != 'post':
            logger.error("[YouTubeZapierProvider] Unsupported action for YouTube: %s", request.action)
            return None

        webhook_url = self._resolve_webhook_url(request)
        if not webhook_url:
            logger.error("[YouTubeZapierProvider] %s is not configured.", self.webhook_env_var)
            return None

        media_urls = self._media_urls(request.media_hints)
        if not media_urls:
            logger.error("[YouTubeZapierProvider] YouTube publish requires at least one media URL.")
            return None

        payload = self._build_payload(request, media_urls)
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(webhook_url, json=payload) as response:
                    response_text = await response.text()
                    if response.status < 200 or response.status >= 300:
                        logger.error(
                            "[YouTubeZapierProvider] Zapier webhook failed with %s: %s",
                            response.status,
                            response_text[:500],
                        )
                        return None

                    response_json = await self._safe_response_json(response, response_text)

            provider_ref = self._provider_ref(response_json)
            provider_url = self._provider_url(response_json)
            return {
                'provider_ref': provider_ref,
                'provider_url': provider_url,
                'delete_supported': False,
                'youtube_response': response_json,
            }
        except Exception as e:
            logger.error("[YouTubeZapierProvider] Error posting to Zapier: %s", e, exc_info=True)
            return None

    async def delete(self, publication: Dict[str, Any]) -> bool:
        return False

    def normalize_target_ref(self, target_ref: Optional[str]) -> Optional[str]:
        return None

    def _resolve_webhook_url(self, request: SocialPublishRequest) -> Optional[str]:
        route_config = self._route_config(request)
        webhook_env_var = str(route_config.get('webhook_env_var') or self.webhook_env_var).strip()
        return os.getenv(webhook_env_var)

    def _build_payload(self, request: SocialPublishRequest, media_urls: List[str]) -> Dict[str, Any]:
        metadata = self._request_metadata(request)
        route_config = self._route_config(request)
        title = self._youtube_title(request, metadata, route_config)
        description = self._youtube_description(request, metadata, route_config)
        tags = self._youtube_tags(metadata, route_config)

        return {
            'platform': 'youtube',
            'action': request.action,
            'title': title,
            'description': description,
            'media_url': media_urls[0],
            'media_urls': media_urls,
            'privacy_status': route_config.get('privacy_status') or metadata.get('privacy_status') or 'private',
            'tags': tags,
            'playlist_id': route_config.get('playlist_id') or metadata.get('playlist_id'),
            'made_for_kids': bool(route_config.get('made_for_kids') or metadata.get('made_for_kids') or False),
            'message_id': request.message_id,
            'channel_id': request.channel_id,
            'guild_id': request.guild_id,
            'user_id': request.user_id,
            'route_key': self._route_key(request),
            'source_kind': request.source_kind,
            'source_metadata': metadata,
        }

    def _media_urls(self, media_hints: List[Dict[str, Any]]) -> List[str]:
        urls = []
        for item in media_hints or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get('url') or item.get('media_url') or '').strip()
            if url:
                urls.append(url)
        return urls

    def _request_metadata(self, request: SocialPublishRequest) -> Dict[str, Any]:
        if request.source_context and request.source_context.metadata:
            return dict(request.source_context.metadata)
        return {}

    def _route_config(self, request: SocialPublishRequest) -> Dict[str, Any]:
        route_override = request.route_override
        if isinstance(route_override, dict) and isinstance(route_override.get('route_config'), dict):
            return dict(route_override.get('route_config') or {})
        return {}

    def _route_key(self, request: SocialPublishRequest) -> Optional[str]:
        route_override = request.route_override
        if isinstance(route_override, dict) and route_override.get('route_key') is not None:
            return str(route_override.get('route_key'))
        return None

    def _youtube_title(
        self,
        request: SocialPublishRequest,
        metadata: Dict[str, Any],
        route_config: Dict[str, Any],
    ) -> str:
        raw_title = (
            metadata.get('youtube_title')
            or route_config.get('default_title')
            or self._first_line(request.text)
            or 'Untitled video'
        )
        return str(raw_title).strip()[:100]

    def _youtube_description(
        self,
        request: SocialPublishRequest,
        metadata: Dict[str, Any],
        route_config: Dict[str, Any],
    ) -> str:
        description = (
            metadata.get('youtube_description')
            or request.text
            or metadata.get('original_content')
            or route_config.get('default_description')
            or ''
        )
        return str(description).strip()

    def _youtube_tags(self, metadata: Dict[str, Any], route_config: Dict[str, Any]) -> List[str]:
        raw_tags = metadata.get('youtube_tags') or route_config.get('default_tags') or []
        if isinstance(raw_tags, str):
            raw_tags = [tag.strip() for tag in raw_tags.split(',')]
        if not isinstance(raw_tags, list):
            return []
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]

    def _first_line(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        for line in str(value).splitlines():
            cleaned = line.strip()
            if cleaned:
                return cleaned
        return None

    async def _safe_response_json(self, response: aiohttp.ClientResponse, response_text: str) -> Dict[str, Any]:
        try:
            parsed = await response.json(content_type=None)
            if isinstance(parsed, dict):
                return parsed
            return {'response': parsed}
        except Exception:
            return {'response_text': response_text}

    def _provider_ref(self, response_json: Dict[str, Any]) -> str:
        for key in ('youtube_video_id', 'video_id', 'id', 'attempt'):
            value = response_json.get(key)
            if value:
                return str(value)
        return 'zapier-youtube'

    def _provider_url(self, response_json: Dict[str, Any]) -> Optional[str]:
        for key in ('youtube_url', 'video_url', 'url'):
            value = response_json.get(key)
            if value:
                return str(value)
        video_id = response_json.get('youtube_video_id') or response_json.get('video_id')
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return None
