from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..models import SocialPublishRequest


class SocialPublishProvider(ABC):
    """Provider interface for outbound social publishing backends."""

    @abstractmethod
    async def publish(self, request: SocialPublishRequest) -> Optional[Dict[str, Any]]:
        """Publish a request and return normalized provider metadata."""

    @abstractmethod
    async def delete(self, publication: Dict[str, Any]) -> bool:
        """Delete a previously published record if the provider supports it."""

    @abstractmethod
    def normalize_target_ref(self, target_ref: Optional[str]) -> Optional[str]:
        """Normalize a provider-specific target reference such as a tweet ID or URL."""
