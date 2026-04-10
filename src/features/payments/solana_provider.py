from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.features.grants.pricing import get_sol_price_usd
from src.features.grants.solana_client import SolanaClient, is_valid_solana_address

from .provider import PaymentProvider, SendResult

logger = logging.getLogger('DiscordBot')


class SolanaProvider(PaymentProvider):
    """Fail-closed adapter around the existing Solana client."""

    FEE_BUFFER_SOL = 10_000 / 1_000_000_000

    def __init__(
        self,
        solana_client: Optional[SolanaClient] = None,
        confirm_timeout_seconds: float = 60.0,
    ):
        self.solana_client = solana_client or SolanaClient()
        self.confirm_timeout_seconds = confirm_timeout_seconds

    async def send(self, recipient: str, amount_token: float) -> SendResult:
        try:
            await self._validate_pre_submit(recipient, amount_token)
        except Exception as e:
            return SendResult(signature=None, phase='pre_submit', error=str(e))

        try:
            signature = await self.solana_client.send_sol(recipient, amount_token)
            return SendResult(signature=signature, phase='submitted', error=None)
        except Exception as e:
            # send_sol() may fail after a prior broadcast attempt, so we must fail closed.
            logger.warning(
                "[SolanaProvider] ambiguous send failure for %s SOL to %s: %s",
                amount_token,
                recipient,
                e,
            )
            return SendResult(signature=None, phase='ambiguous', error=str(e))

    async def confirm_tx(self, tx_signature: str) -> str:
        try:
            await asyncio.wait_for(
                self.solana_client.confirm_tx(tx_signature),
                timeout=self.confirm_timeout_seconds,
            )
            return 'confirmed'
        except asyncio.TimeoutError:
            return 'timeout'
        except Exception as e:
            logger.warning("[SolanaProvider] confirm_tx fallback for %s: %s", tx_signature, e)
            status = await self.check_status(tx_signature)
            if status in {'confirmed', 'failed'}:
                return status
            return 'timeout'

    async def check_status(self, tx_signature: str) -> str:
        return await self.solana_client.check_tx_status(tx_signature)

    async def get_token_price_usd(self) -> float:
        return float(await get_sol_price_usd())

    def token_name(self) -> str:
        return 'SOL'

    async def _validate_pre_submit(self, recipient: str, amount_token: float) -> None:
        if amount_token <= 0:
            raise ValueError("Payment amount must be greater than zero")
        if not is_valid_solana_address(recipient):
            raise ValueError("Invalid Solana wallet address")

        balance_sol = await self.solana_client.get_balance_sol()
        required_balance = amount_token + self.FEE_BUFFER_SOL
        if balance_sol < required_balance:
            raise RuntimeError(
                f"Insufficient balance: {balance_sol:.4f} SOL, need {amount_token:.4f} SOL + fees"
            )


__all__ = ['SolanaProvider']
