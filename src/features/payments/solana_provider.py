from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional

import aiohttp
import httpx
from solana.rpc.core import RPCException

from src.features.grants.pricing import get_sol_price_usd
from src.features.grants.solana_client import (
    SendResult as SolanaSendResult,
    SolanaClient,
    is_valid_solana_address,
)

from .provider import PaymentProvider, SendResult

logger = logging.getLogger('DiscordBot')


def _iter_exception_chain(exc: BaseException):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_rpc_connection_error(exc: BaseException) -> bool:
    transport_types = (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        httpx.TransportError,
        httpx.ConnectError,
        httpx.TimeoutException,
        asyncio.TimeoutError,
    )
    for current in _iter_exception_chain(exc):
        if isinstance(current, transport_types):
            return True
        if isinstance(current, RPCException):
            for arg in current.args:
                if isinstance(arg, BaseException) and _is_rpc_connection_error(arg):
                    return True
    return False


class SolanaProvider(PaymentProvider):
    """Fail-closed adapter around the existing Solana client."""

    FEE_BUFFER_SOL = 10_000 / 1_000_000_000
    # Bound the in-memory rebroadcast cache so we never grow unbounded if
    # confirmations are skipped. 32 is more than enough headroom for any
    # realistic rate of concurrent-in-flight payouts.
    _REBROADCAST_CACHE_MAX = 32

    def __init__(
        self,
        solana_client: Optional[SolanaClient] = None,
        confirm_timeout_seconds: float = 60.0,
    ):
        self.solana_client = solana_client or SolanaClient()
        self.confirm_timeout_seconds = confirm_timeout_seconds
        # Maps signature -> SolanaSendResult so confirm_tx() can run the
        # rebroadcast loop. Populated by send() and consumed by confirm_tx().
        # On cache miss (e.g. process restart, recovery path) we fall back to
        # the single-shot confirm_tx path on the underlying client.
        self._rebroadcast_cache: "OrderedDict[str, SolanaSendResult]" = OrderedDict()

    def _cache_send_result(self, result: SolanaSendResult) -> None:
        self._rebroadcast_cache[result.signature] = result
        self._rebroadcast_cache.move_to_end(result.signature)
        while len(self._rebroadcast_cache) > self._REBROADCAST_CACHE_MAX:
            self._rebroadcast_cache.popitem(last=False)

    def _pop_send_result(self, signature: str) -> Optional[SolanaSendResult]:
        return self._rebroadcast_cache.pop(signature, None)

    async def send(self, recipient: str, amount_token: float) -> SendResult:
        try:
            await self._validate_pre_submit(recipient, amount_token)
        except Exception as e:
            return SendResult(signature=None, phase='pre_submit', error=str(e))

        try:
            send_result = await self.solana_client.send_sol(recipient, amount_token)
            self._cache_send_result(send_result)
            return SendResult(
                signature=send_result.signature,
                phase='submitted',
                error=None,
            )
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
        cached = self._pop_send_result(tx_signature)
        try:
            if cached is not None:
                # Happy path: we have the signed tx on hand, so run the
                # rebroadcast loop. The loop's own max_wait_seconds handles
                # the timeout budget; the outer wait_for remains as a safety
                # belt in case the loop somehow fails to self-terminate.
                await asyncio.wait_for(
                    self.solana_client.confirm_tx_with_rebroadcast(
                        cached,
                        max_wait_seconds=self.confirm_timeout_seconds,
                    ),
                    timeout=self.confirm_timeout_seconds + 10,
                )
            else:
                # Cache miss (e.g. restart recovery). Fall back to the
                # single-shot confirm path — we can't rebroadcast without
                # the signed payload, but we can still observe final status.
                await asyncio.wait_for(
                    self.solana_client.confirm_tx(tx_signature),
                    timeout=self.confirm_timeout_seconds,
                )
            return 'confirmed'
        except asyncio.TimeoutError:
            # Before giving up as 'timeout', reconcile against the chain: a
            # finalized-but-errored tx may have arrived at confirmation slowly,
            # and we don't want to lose visibility on failed state just because
            # our wait budget expired.
            try:
                status = await self.check_status(tx_signature)
            except Exception as lookup_err:
                logger.warning(
                    "[SolanaProvider] check_status after timeout failed for %s: %s",
                    tx_signature,
                    lookup_err,
                )
                if _is_rpc_connection_error(lookup_err):
                    return 'rpc_unreachable'
                return 'timeout'
            if status in {'confirmed', 'failed', 'rpc_unreachable'}:
                return status
            return 'timeout'
        except Exception as e:
            logger.warning("[SolanaProvider] confirm_tx fallback for %s: %s", tx_signature, e)
            try:
                status = await self.check_status(tx_signature)
            except Exception as lookup_err:
                logger.warning(
                    "[SolanaProvider] confirm_tx fallback status lookup failed for %s: %s",
                    tx_signature,
                    lookup_err,
                )
                if _is_rpc_connection_error(e) or _is_rpc_connection_error(lookup_err):
                    return 'rpc_unreachable'
                return 'timeout'
            if status in {'confirmed', 'failed', 'rpc_unreachable'}:
                return status
            if _is_rpc_connection_error(e):
                return 'rpc_unreachable'
            return 'timeout'

    async def check_status(self, tx_signature: str) -> str:
        try:
            return await self.solana_client.check_tx_status(tx_signature)
        except Exception as exc:
            if _is_rpc_connection_error(exc):
                logger.warning("[SolanaProvider] RPC unreachable while checking %s: %s", tx_signature, exc)
                return 'rpc_unreachable'
            raise

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
