from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional


SendPhase = Literal['pre_submit', 'submitted', 'ambiguous']


@dataclass(frozen=True)
class SendResult:
    signature: Optional[str]
    phase: SendPhase
    error: Optional[str] = None


class PaymentProvider(ABC):
    """Abstract provider interface for outbound payments."""

    @abstractmethod
    async def send(self, recipient: str, amount_token: float) -> SendResult:
        """Submit a payment and return a fail-closed send result."""

    @abstractmethod
    async def confirm_tx(self, tx_signature: str) -> str:
        """Wait for transaction confirmation and return confirmed, failed, or timeout."""

    @abstractmethod
    async def check_status(self, tx_signature: str) -> str:
        """Check one transaction status and return confirmed, failed, or not_found."""

    @abstractmethod
    async def get_token_price_usd(self) -> float:
        """Return the provider token price in USD."""

    @abstractmethod
    def token_name(self) -> str:
        """Return the provider token symbol."""


__all__ = ['PaymentProvider', 'SendPhase', 'SendResult']
