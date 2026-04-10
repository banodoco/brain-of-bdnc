from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.features.grants.pricing import usd_to_sol

from .provider import PaymentProvider

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger('DiscordBot')


class PaymentService:
    """Core fail-closed payment orchestration shared by payment producers."""

    def __init__(
        self,
        db_handler: 'DatabaseHandler',
        providers: Dict[str, PaymentProvider],
        test_payment_amount: float,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.db_handler = db_handler
        self.providers = {str(name).lower(): provider for name, provider in (providers or {}).items()}
        self.test_payment_amount = float(test_payment_amount)
        self.logger = logger_instance or logger

    async def request_payment(
        self,
        *,
        producer: str,
        producer_ref: str,
        guild_id: int,
        recipient_wallet: str,
        chain: str,
        provider: str,
        is_test: bool,
        confirm_channel_id: int,
        notify_channel_id: int,
        amount_usd: Optional[float] = None,
        amount_token: Optional[float] = None,
        recipient_discord_id: Optional[int] = None,
        wallet_id: Optional[str] = None,
        confirm_thread_id: Optional[int] = None,
        notify_thread_id: Optional[int] = None,
        route_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create one idempotent payment request row."""
        existing_rows = self.db_handler.get_payment_requests_by_producer(
            guild_id=guild_id,
            producer=producer,
            producer_ref=producer_ref,
            is_test=is_test,
        )
        if existing_rows:
            return existing_rows[0]

        payment_provider = self._get_provider(provider)
        if not payment_provider:
            self.logger.error(f"[PaymentService] Unsupported payment provider: {provider}")
            return None

        normalized_wallet = str(recipient_wallet or '').strip()
        normalized_producer = str(producer or '').strip().lower()
        normalized_producer_ref = str(producer_ref or '').strip()
        normalized_chain = str(chain or '').strip().lower()
        if not normalized_wallet or not normalized_producer or not normalized_producer_ref or not normalized_chain:
            self.logger.error("[PaymentService] request_payment missing required normalized fields")
            return None

        wallet_record = None
        if wallet_id:
            wallet_record = self.db_handler.get_wallet_by_id(wallet_id, guild_id=guild_id)
            if not wallet_record:
                self.logger.error(
                    f"[PaymentService] request_payment could not find wallet_id={wallet_id} in guild {guild_id}"
                )
                return None
            if wallet_record.get('wallet_address') != normalized_wallet:
                self.logger.error(
                    "[PaymentService] request_payment wallet mismatch for wallet_id=%s in guild %s",
                    wallet_id,
                    guild_id,
                )
                return None

        amount_token: float
        token_price_usd: Optional[float]
        normalized_amount_usd: Optional[float]
        if is_test:
            amount_token = self.test_payment_amount
            token_price_usd = None
            normalized_amount_usd = None
        elif amount_token is not None:
            amount_token = float(amount_token)
            if amount_token <= 0:
                self.logger.error("[PaymentService] request_payment amount_token must be > 0")
                return None
            token_price_usd = None
            normalized_amount_usd = None
        else:
            if amount_usd is None:
                self.logger.error("[PaymentService] request_payment requires amount_usd for non-test payments")
                return None
            normalized_amount_usd = float(amount_usd)
            if normalized_amount_usd <= 0:
                self.logger.error("[PaymentService] request_payment amount_usd must be > 0")
                return None
            token_price_usd = await payment_provider.get_token_price_usd()
            amount_token = usd_to_sol(normalized_amount_usd, token_price_usd)
            if amount_token <= 0:
                self.logger.error("[PaymentService] request_payment derived a non-positive amount_token")
                return None

        metadata_payload = dict(metadata or {})
        record = {
            'guild_id': guild_id,
            'producer': normalized_producer,
            'producer_ref': normalized_producer_ref,
            'wallet_id': wallet_record.get('wallet_id') if wallet_record else wallet_id,
            'recipient_discord_id': recipient_discord_id,
            'recipient_wallet': normalized_wallet,
            'chain': normalized_chain,
            'provider': str(provider).strip().lower(),
            'is_test': bool(is_test),
            'route_key': route_key,
            'confirm_channel_id': int(confirm_channel_id),
            'confirm_thread_id': confirm_thread_id,
            'notify_channel_id': int(notify_channel_id),
            'notify_thread_id': notify_thread_id,
            'amount_token': amount_token,
            'amount_usd': normalized_amount_usd,
            'token_price_usd': token_price_usd,
            'metadata': metadata_payload,
            'status': 'pending_confirmation',
            'request_payload': {
                'producer': normalized_producer,
                'producer_ref': normalized_producer_ref,
                'guild_id': guild_id,
                'recipient_wallet': normalized_wallet,
                'chain': normalized_chain,
                'provider': str(provider).strip().lower(),
                'is_test': bool(is_test),
                'amount_token': amount_token,
                'amount_usd': normalized_amount_usd,
                'token_price_usd': token_price_usd,
                'recipient_discord_id': recipient_discord_id,
                'wallet_id': wallet_record.get('wallet_id') if wallet_record else wallet_id,
                'confirm_channel_id': int(confirm_channel_id),
                'confirm_thread_id': confirm_thread_id,
                'notify_channel_id': int(notify_channel_id),
                'notify_thread_id': notify_thread_id,
                'route_key': route_key,
                'metadata': metadata_payload,
            },
        }

        created = self.db_handler.create_payment_request(record, guild_id=guild_id)
        if created:
            return created

        # Fail closed under concurrent duplicate requests by re-reading the canonical row.
        existing_rows = self.db_handler.get_payment_requests_by_producer(
            guild_id=guild_id,
            producer=normalized_producer,
            producer_ref=normalized_producer_ref,
            is_test=is_test,
        )
        return existing_rows[0] if existing_rows else None

    def confirm_payment(
        self,
        payment_id: str,
        *,
        guild_id: Optional[int] = None,
        confirmed_by_user_id: Optional[int] = None,
        confirmed_by: str = 'user',
        privileged_override: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Move a payment from pending_confirmation into the queue."""
        payment = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return None

        if payment.get('status') != 'pending_confirmation':
            return payment

        expected_user_id = payment.get('recipient_discord_id')
        if not privileged_override and expected_user_id is not None:
            if confirmed_by_user_id is None or int(expected_user_id) != int(confirmed_by_user_id):
                self.logger.warning(
                    "[PaymentService] rejected confirmation for %s: expected recipient %s, got %s",
                    payment_id,
                    expected_user_id,
                    confirmed_by_user_id,
                )
                return None

        if not self.db_handler.mark_payment_confirmed_by_user(
            payment_id,
            guild_id=payment.get('guild_id'),
            confirmed_by_user_id=confirmed_by_user_id,
            confirmed_by=confirmed_by,
        ):
            return None
        return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

    async def execute_payment(self, payment_id: str, *, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Execute one claimed payment request under the fail-closed state machine."""
        payment = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return None

        status = payment.get('status')
        if status in {'confirmed', 'failed', 'manual_hold', 'cancelled'}:
            return payment

        payment_provider = self._get_provider(payment.get('provider'))
        if not payment_provider:
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason=f"Unsupported payment provider: {payment.get('provider')}",
                guild_id=payment.get('guild_id'),
            )
            return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

        if status == 'submitted':
            return await self._confirm_submitted_payment(payment, payment_provider)

        if status != 'processing':
            self.logger.warning(
                "[PaymentService] execute_payment received payment %s in unsupported status %s",
                payment_id,
                status,
            )
            return payment

        send_result = await payment_provider.send(
            recipient=str(payment.get('recipient_wallet') or ''),
            amount_token=float(payment.get('amount_token') or 0),
        )

        if send_result.phase == 'pre_submit':
            self.db_handler.mark_payment_failed(
                payment_id,
                error=send_result.error or 'pre-submit payment failure',
                send_phase='pre_submit',
                guild_id=payment.get('guild_id'),
            )
            return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

        if send_result.phase == 'ambiguous':
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason=f"Ambiguous send error: {send_result.error or 'unknown send error'}",
                guild_id=payment.get('guild_id'),
            )
            return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

        if send_result.phase != 'submitted' or not send_result.signature:
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason="Provider returned an invalid submitted-state result",
                guild_id=payment.get('guild_id'),
            )
            return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

        self.db_handler.mark_payment_submitted(
            payment_id,
            tx_signature=send_result.signature,
            amount_token=float(payment.get('amount_token') or 0),
            token_price_usd=payment.get('token_price_usd'),
            send_phase='submitted',
            guild_id=payment.get('guild_id'),
        )
        submitted_payment = self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))
        if not submitted_payment:
            return None
        return await self._confirm_submitted_payment(submitted_payment, payment_provider)

    async def recover_inflight(self, guild_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Recover processing/submitted rows safely after restart."""
        recovered_rows: List[Dict[str, Any]] = []
        inflight_rows = self.db_handler.get_inflight_payments_for_recovery(guild_ids=guild_ids)

        for payment in inflight_rows:
            payment_id = payment.get('payment_id')
            guild_id = payment.get('guild_id')
            if not payment_id or guild_id is None:
                continue

            status = payment.get('status')
            tx_signature = payment.get('tx_signature')
            send_phase = payment.get('send_phase')

            if status == 'processing':
                if tx_signature:
                    self.db_handler.mark_payment_manual_hold(
                        payment_id,
                        reason="Recovery found processing payment with stored tx signature",
                        guild_id=guild_id,
                    )
                elif send_phase == 'ambiguous':
                    self.db_handler.mark_payment_manual_hold(
                        payment_id,
                        reason="Recovery found ambiguous processing payment",
                        guild_id=guild_id,
                    )
                else:
                    # A processing row without a signature is still safe to retry,
                    # but we respect the DB handler's failed-only retry transition.
                    self.db_handler.mark_payment_failed(
                        payment_id,
                        error='Recovery determined payment never reached submission',
                        send_phase='pre_submit',
                        guild_id=guild_id,
                    )
                    self.db_handler.requeue_payment(
                        payment_id,
                        retry_after=datetime.now(timezone.utc),
                        guild_id=guild_id,
                    )
            elif status == 'submitted':
                payment_provider = self._get_provider(payment.get('provider'))
                if not tx_signature:
                    self.db_handler.mark_payment_manual_hold(
                        payment_id,
                        reason="Recovery found submitted payment without tx signature",
                        guild_id=guild_id,
                    )
                elif not payment_provider:
                    self.db_handler.mark_payment_manual_hold(
                        payment_id,
                        reason=f"Recovery cannot check provider {payment.get('provider')}",
                        guild_id=guild_id,
                    )
                else:
                    chain_status = await payment_provider.check_status(tx_signature)
                    if chain_status == 'confirmed':
                        self.db_handler.mark_payment_confirmed(payment_id, guild_id=guild_id)
                        if payment.get('is_test') and payment.get('wallet_id'):
                            self.db_handler.mark_wallet_verified(payment['wallet_id'], guild_id=guild_id)
                    elif chain_status == 'failed':
                        self.db_handler.mark_payment_failed(
                            payment_id,
                            error='Chain reported submitted payment as failed during recovery',
                            send_phase='submitted',
                            guild_id=guild_id,
                        )
                    else:
                        self.db_handler.mark_payment_manual_hold(
                            payment_id,
                            reason='Recovery could not determine submitted transaction status',
                            guild_id=guild_id,
                        )

            refreshed = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
            if refreshed:
                recovered_rows.append(refreshed)

        return recovered_rows

    def get_pending_confirmations(self, guild_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Return pending confirmation rows for restart-safe Discord view registration."""
        return self.db_handler.get_pending_confirmation_payments(guild_ids=guild_ids)

    def get_pending_confirmation_payments(self, guild_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Compatibility wrapper for pending confirmation lookups."""
        return self.get_pending_confirmations(guild_ids=guild_ids)

    async def _confirm_submitted_payment(
        self,
        payment: Dict[str, Any],
        payment_provider: PaymentProvider,
    ) -> Optional[Dict[str, Any]]:
        payment_id = payment.get('payment_id')
        guild_id = payment.get('guild_id')
        tx_signature = payment.get('tx_signature')
        if not payment_id or guild_id is None:
            return payment
        if not tx_signature:
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason='Submitted payment is missing a tx signature',
                guild_id=guild_id,
            )
            return self.db_handler.get_payment_request(payment_id, guild_id=guild_id)

        confirmation_status = await payment_provider.confirm_tx(tx_signature)
        if confirmation_status == 'confirmed':
            self.db_handler.mark_payment_confirmed(payment_id, guild_id=guild_id)
            if payment.get('is_test') and payment.get('wallet_id'):
                self.db_handler.mark_wallet_verified(payment['wallet_id'], guild_id=guild_id)
        elif confirmation_status == 'failed':
            self.db_handler.mark_payment_failed(
                payment_id,
                error='Chain reported submitted payment as failed',
                send_phase='submitted',
                guild_id=guild_id,
            )
        else:
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason='Confirmation timed out after submission',
                guild_id=guild_id,
            )
        return self.db_handler.get_payment_request(payment_id, guild_id=guild_id)

    def _get_provider(self, provider_name: Optional[str]) -> Optional[PaymentProvider]:
        if not provider_name:
            return None
        return self.providers.get(str(provider_name).strip().lower())


__all__ = ['PaymentService']
