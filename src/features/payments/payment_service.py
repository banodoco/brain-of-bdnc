from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from src.common.redaction import redact_wallet as _redact_wallet
from src.features.grants.pricing import usd_to_sol

from .provider import PaymentProvider

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger('DiscordBot')
RENT_EXEMPT_LAMPORTS = 890_880
MIN_TEST_LAMPORTS = 2_000_000
RECONCILE_BLOCKHASH_EXPIRY_WINDOW_SECONDS = 150
RECOVERY_RECLAIM_WINDOW_SECONDS = 150
RPC_UNREACHABLE_MANUAL_HOLD_REASON = 'rpc_unreachable: confirmation RPC offline'


class PaymentActorKind(str, Enum):
    RECIPIENT_CLICK = 'recipient_click'
    RECIPIENT_MESSAGE = 'recipient_message'
    AUTO = 'auto'
    ADMIN_DM = 'admin_dm'


@dataclass(frozen=True)
class PaymentActor:
    kind: PaymentActorKind
    actor_id: Optional[int]


@dataclass(frozen=True)
class ReconcileDecision:
    decision: Literal[
        'reconciled_confirmed',
        'reconciled_failed',
        'allow_requeue',
        'keep_in_hold',
        'not_applicable',
    ]
    reason: str
    tx_signature: Optional[str]


@dataclass(frozen=True)
class NormalizedRequest:
    wallet: str
    producer: str
    producer_ref: str
    chain: str
    provider_key: str
    confirm_channel_id: Any
    notify_channel_id: Any
    recipient_discord_id: Optional[int]
    wallet_id: Optional[str]
    confirm_thread_id: Optional[int]
    notify_thread_id: Optional[int]
    route_key: Optional[str]
    metadata_payload: Dict[str, Any]
    wallet_record: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class AmountsResult:
    amount_token: float
    token_price_usd: Optional[float]
    normalized_amount_usd: Optional[float]


@dataclass(frozen=True)
class CapResult:
    cap_breach: Optional[str]
    derived_usd_for_record: Optional[float]
    derived_price_for_record: Optional[float]

def _to_aware_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


class PaymentService:
    """Core fail-closed payment orchestration shared by payment producers."""

    def __init__(
        self,
        db_handler: 'DatabaseHandler',
        providers: Dict[str, PaymentProvider],
        test_payment_amount: float,
        logger_instance: Optional[logging.Logger] = None,
        per_payment_usd_cap: Optional[float] = None,
        daily_usd_cap: Optional[float] = None,
        capped_providers=None,
        on_cap_breach=None,
    ):
        self.db_handler = db_handler
        self.providers = {str(name).lower(): provider for name, provider in (providers or {}).items()}
        self.test_payment_amount = float(test_payment_amount)
        test_payment_lamports = int(self.test_payment_amount * 1_000_000_000)
        if test_payment_lamports < MIN_TEST_LAMPORTS:
            raise ValueError(
                f"PAYMENT_TEST_AMOUNT_SOL={self.test_payment_amount} is "
                f"{test_payment_lamports} lamports, below the required "
                f"minimum of {MIN_TEST_LAMPORTS} lamports "
                f"(rent-exempt floor is {RENT_EXEMPT_LAMPORTS}). "
                "Raise PAYMENT_TEST_AMOUNT_SOL to at least 0.002."
            )
        self.logger = logger_instance or logger
        self.per_payment_usd_cap = float(per_payment_usd_cap) if per_payment_usd_cap is not None else None
        self.daily_usd_cap = float(daily_usd_cap) if daily_usd_cap is not None else None
        self.capped_providers = frozenset(
            str(name).strip().lower()
            for name in (capped_providers or ())
            if str(name).strip()
        )
        self._on_cap_breach = on_cap_breach

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
        normalized = self._normalize_inputs(
            recipient_wallet=recipient_wallet,
            producer=producer,
            producer_ref=producer_ref,
            chain=chain,
            provider=provider,
            guild_id=guild_id,
            confirm_channel_id=confirm_channel_id,
            notify_channel_id=notify_channel_id,
            recipient_discord_id=recipient_discord_id,
            wallet_id=wallet_id,
            confirm_thread_id=confirm_thread_id,
            notify_thread_id=notify_thread_id,
            route_key=route_key,
            metadata=metadata,
        )
        if normalized is None:
            return None

        collision_row, blocking_prior_row, idempotent_row = self._detect_collision_and_idempotent_row(
            normalized,
            guild_id=guild_id,
            is_test=is_test,
        )
        collision_error = 'idempotency collision: prior wallet differs'

        if collision_row and blocking_prior_row is not None:
            self.logger.warning(
                "[PaymentService] producer_ref collision blocked for %s:%s in guild %s: incoming=%s existing=%s status=%s",
                normalized.producer,
                normalized.producer_ref,
                guild_id,
                _redact_wallet(normalized.wallet),
                _redact_wallet(blocking_prior_row.get('recipient_wallet')),
                blocking_prior_row.get('status'),
            )
            await self._emit_cap_breach(blocking_prior_row)
            return None

        if not collision_row and idempotent_row is not None:
            return idempotent_row

        payment_provider = self._get_provider(provider)
        if not payment_provider:
            self.logger.error(f"[PaymentService] Unsupported payment provider: {provider}")
            return None

        amounts = await self._derive_amounts(
            normalized,
            is_test=is_test,
            amount_usd=amount_usd,
            amount_token=amount_token,
            payment_provider=payment_provider,
        )
        if amounts is None:
            return None

        cap_result = await self._enforce_caps(
            normalized,
            is_test=is_test,
            amount_token=amounts.amount_token,
            amount_usd=amounts.normalized_amount_usd,
            payment_provider=payment_provider,
            guild_id=guild_id,
        )
        return await self._persist_row(
            normalized,
            guild_id=guild_id,
            is_test=is_test,
            collision_row=collision_row,
            blocking_prior_row=blocking_prior_row,
            collision_error=collision_error,
            amounts=amounts,
            cap_result=cap_result,
        )

    def _normalize_inputs(
        self,
        **kwargs,
    ) -> Optional[NormalizedRequest]:
        normalized_wallet = str(kwargs.get('recipient_wallet') or '').strip()
        normalized_producer = str(kwargs.get('producer') or '').strip().lower()
        normalized_producer_ref = str(kwargs.get('producer_ref') or '').strip()
        normalized_chain = str(kwargs.get('chain') or '').strip().lower()
        if not normalized_wallet or not normalized_producer or not normalized_producer_ref or not normalized_chain:
            self.logger.error("[PaymentService] request_payment missing required normalized fields")
            return None

        wallet_id = kwargs.get('wallet_id')
        wallet_record = None
        if wallet_id:
            wallet_record = self.db_handler.get_wallet_by_id(wallet_id, guild_id=kwargs.get('guild_id'))
            if not wallet_record:
                self.logger.error(
                    f"[PaymentService] request_payment could not find wallet_id={wallet_id} in guild {kwargs.get('guild_id')}"
                )
                return None
            if wallet_record.get('wallet_address') != normalized_wallet:
                self.logger.error(
                    "[PaymentService] request_payment wallet mismatch for wallet_id=%s in guild %s",
                    wallet_id,
                    kwargs.get('guild_id'),
                )
                return None

        return NormalizedRequest(
            wallet=normalized_wallet,
            producer=normalized_producer,
            producer_ref=normalized_producer_ref,
            chain=normalized_chain,
            provider_key=str(kwargs.get('provider')).strip().lower(),
            confirm_channel_id=kwargs.get('confirm_channel_id'),
            notify_channel_id=kwargs.get('notify_channel_id'),
            recipient_discord_id=kwargs.get('recipient_discord_id'),
            wallet_id=wallet_id,
            confirm_thread_id=kwargs.get('confirm_thread_id'),
            notify_thread_id=kwargs.get('notify_thread_id'),
            route_key=kwargs.get('route_key'),
            metadata_payload=dict(kwargs.get('metadata') or {}),
            wallet_record=wallet_record,
        )

    def _detect_collision_and_idempotent_row(
        self,
        normalized: NormalizedRequest,
        *,
        guild_id: int,
        is_test: bool,
    ):
        index_excluded = {'failed', 'cancelled'}
        terminal_for_return = {'confirmed', 'failed', 'manual_hold', 'cancelled'}
        collision_row = None
        blocking_prior_row = None
        idempotent_row = None
        all_prior_rows = self.db_handler.get_payment_requests_by_producer(
            guild_id=guild_id,
            producer=normalized.producer,
            producer_ref=normalized.producer_ref,
            is_test=is_test,
        )
        for prior_row in all_prior_rows:
            prior_wallet = str(prior_row.get('recipient_wallet') or '').strip()
            prior_status = str(prior_row.get('status') or '').strip().lower()
            if prior_wallet != normalized.wallet and collision_row is None:
                collision_row = prior_row
            if prior_status not in index_excluded and blocking_prior_row is None:
                blocking_prior_row = prior_row
            if prior_wallet == normalized.wallet and prior_status not in terminal_for_return and idempotent_row is None:
                idempotent_row = prior_row

        return collision_row, blocking_prior_row, idempotent_row

    async def _derive_amounts(
        self,
        normalized: NormalizedRequest,
        *,
        is_test: bool,
        amount_usd: Optional[float],
        amount_token: Optional[float],
        payment_provider: PaymentProvider,
    ) -> Optional[AmountsResult]:
        if is_test:
            return AmountsResult(
                amount_token=self.test_payment_amount,
                token_price_usd=None,
                normalized_amount_usd=None,
            )

        if amount_token is not None:
            normalized_amount_token = float(amount_token)
            if normalized_amount_token <= 0:
                self.logger.error("[PaymentService] request_payment amount_token must be > 0")
                return None
            return AmountsResult(
                amount_token=normalized_amount_token,
                token_price_usd=None,
                normalized_amount_usd=None,
            )

        if amount_usd is None:
            self.logger.error("[PaymentService] request_payment requires amount_usd for non-test payments")
            return None

        normalized_amount_usd = float(amount_usd)
        if normalized_amount_usd <= 0:
            self.logger.error("[PaymentService] request_payment amount_usd must be > 0")
            return None

        token_price_usd = await payment_provider.get_token_price_usd()
        normalized_amount_token = usd_to_sol(normalized_amount_usd, token_price_usd)
        if normalized_amount_token <= 0:
            self.logger.error("[PaymentService] request_payment derived a non-positive amount_token")
            return None

        return AmountsResult(
            amount_token=normalized_amount_token,
            token_price_usd=token_price_usd,
            normalized_amount_usd=normalized_amount_usd,
        )

    async def _enforce_caps(
        self,
        normalized: NormalizedRequest,
        *,
        is_test: bool,
        amount_token: float,
        amount_usd: Optional[float],
        payment_provider: PaymentProvider,
        guild_id: int,
    ) -> CapResult:
        cap_breach = None
        derived_usd_for_record = None
        derived_price_for_record = None
        if not is_test and normalized.provider_key in self.capped_providers:
            cap_usd = amount_usd
            if amount_usd is None and (self.per_payment_usd_cap is not None or self.daily_usd_cap is not None):
                try:
                    price_result = await payment_provider.get_token_price_usd()
                    derived_price_for_record = float(price_result) if price_result is not None else None
                except Exception as e:
                    self.logger.warning(
                        "[PaymentService] cap price lookup failed for %s payout in guild %s to %s: %s",
                        normalized.provider_key,
                        guild_id,
                        _redact_wallet(normalized.wallet),
                        e,
                    )
                    derived_price_for_record = None
                if derived_price_for_record and derived_price_for_record > 0:
                    derived_usd_for_record = float(derived_price_for_record) * amount_token
                    cap_usd = derived_usd_for_record
                else:
                    cap_breach = 'cap check unavailable: token price missing'

            if cap_breach is None and cap_usd is not None:
                if self.per_payment_usd_cap is not None and cap_usd > self.per_payment_usd_cap:
                    cap_breach = (
                        f"per-payment cap exceeded: ${cap_usd:.2f} > ${self.per_payment_usd_cap:.2f}"
                    )
                if cap_breach is None and self.daily_usd_cap is not None:
                    rolling_usd = self.db_handler.get_rolling_24h_payout_usd(guild_id, normalized.provider_key)
                    if rolling_usd + cap_usd > self.daily_usd_cap:
                        cap_breach = (
                            f"rolling daily cap exceeded: ${rolling_usd + cap_usd:.2f} > ${self.daily_usd_cap:.2f}"
                        )

        return CapResult(
            cap_breach=cap_breach,
            derived_usd_for_record=derived_usd_for_record,
            derived_price_for_record=derived_price_for_record,
        )

    async def _persist_row(
        self,
        normalized: NormalizedRequest,
        *,
        guild_id: int,
        is_test: bool,
        collision_row,
        blocking_prior_row,
        collision_error: str,
        amounts: AmountsResult,
        cap_result: CapResult,
    ) -> Optional[Dict[str, Any]]:
        wallet_record = normalized.wallet_record
        record = {
            'guild_id': guild_id,
            'producer': normalized.producer,
            'producer_ref': normalized.producer_ref,
            'wallet_id': wallet_record.get('wallet_id') if wallet_record else normalized.wallet_id,
            'recipient_discord_id': normalized.recipient_discord_id,
            'recipient_wallet': normalized.wallet,
            'chain': normalized.chain,
            'provider': normalized.provider_key,
            'is_test': bool(is_test),
            'route_key': normalized.route_key,
            'confirm_channel_id': normalized.confirm_channel_id,
            'confirm_thread_id': normalized.confirm_thread_id,
            'notify_channel_id': normalized.notify_channel_id,
            'notify_thread_id': normalized.notify_thread_id,
            'amount_token': amounts.amount_token,
            'amount_usd': amounts.normalized_amount_usd,
            'token_price_usd': amounts.token_price_usd,
            'metadata': normalized.metadata_payload,
            'status': 'pending_confirmation',
            'request_payload': {
                'producer': normalized.producer,
                'producer_ref': normalized.producer_ref,
                'guild_id': guild_id,
                'recipient_wallet': normalized.wallet,
                'chain': normalized.chain,
                'provider': normalized.provider_key,
                'is_test': bool(is_test),
                'amount_token': amounts.amount_token,
                'amount_usd': amounts.normalized_amount_usd,
                'token_price_usd': amounts.token_price_usd,
                'recipient_discord_id': normalized.recipient_discord_id,
                'wallet_id': wallet_record.get('wallet_id') if wallet_record else normalized.wallet_id,
                'confirm_channel_id': normalized.confirm_channel_id,
                'confirm_thread_id': normalized.confirm_thread_id,
                'notify_channel_id': normalized.notify_channel_id,
                'notify_thread_id': normalized.notify_thread_id,
                'route_key': normalized.route_key,
                'metadata': normalized.metadata_payload,
            },
        }
        if (
            not is_test
            and normalized.provider_key in self.capped_providers
            and cap_result.derived_usd_for_record is not None
        ):
            record['amount_usd'] = cap_result.derived_usd_for_record
            record['token_price_usd'] = cap_result.derived_price_for_record
            record['request_payload']['amount_usd'] = cap_result.derived_usd_for_record
            record['request_payload']['token_price_usd'] = cap_result.derived_price_for_record
        if collision_row and blocking_prior_row is None:
            record['status'] = 'manual_hold'
            record['last_error'] = collision_error
        if cap_result.cap_breach:
            self.logger.warning(
                "[PaymentService] cap breach for %s payout in guild %s to %s: %s",
                normalized.provider_key,
                guild_id,
                _redact_wallet(normalized.wallet),
                cap_result.cap_breach,
            )
            record['status'] = 'manual_hold'
            record['last_error'] = cap_result.cap_breach

        created = self.db_handler.create_payment_request(record, guild_id=guild_id)
        if created:
            if record.get('status') == 'manual_hold':
                await self._emit_cap_breach(created)
            return created

        existing_rows = self.db_handler.get_payment_requests_by_producer(
            guild_id=guild_id,
            producer=normalized.producer,
            producer_ref=normalized.producer_ref,
            is_test=is_test,
        )
        canonical_row = existing_rows[0] if existing_rows else None
        if not canonical_row:
            return None

        canonical_wallet = str(canonical_row.get('recipient_wallet') or '').strip()
        if canonical_wallet != normalized.wallet:
            self.logger.warning(
                "[PaymentService] duplicate reread wallet mismatch for %s:%s in guild %s: incoming=%s canonical=%s status=%s",
                normalized.producer,
                normalized.producer_ref,
                guild_id,
                _redact_wallet(normalized.wallet),
                _redact_wallet(canonical_wallet),
                canonical_row.get('status'),
            )
            await self._emit_cap_breach(canonical_row)
            return None
        return canonical_row

    def _authorize_actor(self, payment: Dict[str, Any], actor: PaymentActor) -> bool:
        from .producer_flows import get_flow

        producer = str(payment.get('producer') or '').strip().lower()
        try:
            flow = get_flow(producer)
        except KeyError:
            self.logger.warning(
                "[PaymentService] unknown producer for confirmation authorization: %s",
                producer or 'unknown',
            )
            return False

        allowed_actor_kinds = flow.test_confirmed_by if payment.get('is_test') else flow.real_confirmed_by
        if actor.kind not in allowed_actor_kinds:
            return False

        if actor.kind in {PaymentActorKind.RECIPIENT_CLICK, PaymentActorKind.RECIPIENT_MESSAGE}:
            expected_user_id = payment.get('recipient_discord_id')
            if expected_user_id is None or actor.actor_id is None:
                return False
            return int(expected_user_id) == int(actor.actor_id)

        if actor.kind == PaymentActorKind.ADMIN_DM:
            admin_user_id = os.getenv('ADMIN_USER_ID')
            if admin_user_id is None or actor.actor_id is None:
                return False
            try:
                return int(actor.actor_id) == int(admin_user_id)
            except (TypeError, ValueError):
                return False

        return actor.kind == PaymentActorKind.AUTO

    def confirm_payment(
        self,
        payment_id: str,
        *,
        actor: PaymentActor,
        guild_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Move a payment from pending_confirmation into the queue."""
        payment = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return None

        if payment.get('status') != 'pending_confirmation':
            return payment

        if not self._authorize_actor(payment, actor):
            self.logger.warning(
                "[PaymentService] rejected confirmation for %s: expected recipient %s, got %s",
                payment_id,
                payment.get('recipient_discord_id'),
                actor.actor_id,
            )
            return None

        if not self.db_handler.mark_payment_confirmed_by_user(
            payment_id,
            guild_id=payment.get('guild_id'),
            confirmed_by_user_id=actor.actor_id,
            confirmed_by=actor.kind.value,
        ):
            return None
        return self.db_handler.get_payment_request(payment_id, guild_id=payment.get('guild_id'))

    async def reconcile_with_chain(
        self,
        payment_id: str,
        *,
        guild_id: Optional[int] = None,
    ) -> ReconcileDecision:
        payment = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return ReconcileDecision(
                decision='not_applicable',
                reason='payment not found',
                tx_signature=None,
            )

        status = str(payment.get('status') or '').strip().lower()
        if status in {'pending_confirmation', 'queued', 'confirmed', 'cancelled'}:
            return ReconcileDecision(
                decision='not_applicable',
                reason=f"status '{status or 'unknown'}' does not require chain reconciliation",
                tx_signature=payment.get('tx_signature'),
            )

        tx_signature = payment.get('tx_signature')
        if not tx_signature:
            return ReconcileDecision(
                decision='allow_requeue',
                reason='no prior signature',
                tx_signature=None,
            )

        payment_provider = self._get_provider(payment.get('provider'))
        if not payment_provider:
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='provider unavailable',
                tx_signature=tx_signature,
            )

        try:
            chain_status = await payment_provider.check_status(tx_signature)
        except Exception:
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='RPC unreachable during reconcile',
                tx_signature=tx_signature,
            )

        if chain_status == 'rpc_unreachable':
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='RPC unreachable during reconcile',
                tx_signature=tx_signature,
            )

        effective_guild_id = guild_id or payment.get('guild_id')
        if chain_status == 'confirmed':
            reconciled = self.db_handler.force_reconcile_payment_to_confirmed(
                payment_id,
                tx_signature=tx_signature,
                reason='chain reported confirmed during reconcile',
                guild_id=effective_guild_id,
            )
            if reconciled:
                return ReconcileDecision(
                    decision='reconciled_confirmed',
                    reason='chain reported confirmed during reconcile',
                    tx_signature=tx_signature,
                )
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='failed to persist confirmed reconciliation',
                tx_signature=tx_signature,
            )

        if chain_status == 'failed':
            reconciled = self.db_handler.force_reconcile_payment_to_failed(
                payment_id,
                tx_signature=tx_signature,
                reason='chain reported failed during reconcile',
                guild_id=effective_guild_id,
            )
            if reconciled:
                return ReconcileDecision(
                    decision='reconciled_failed',
                    reason='chain reported failed during reconcile',
                    tx_signature=tx_signature,
                )
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='failed to persist failed reconciliation',
                tx_signature=tx_signature,
            )

        if chain_status == 'not_found':
            submitted_at = _to_aware_utc(payment.get('submitted_at'))
            if submitted_at is None:
                return ReconcileDecision(
                    decision='keep_in_hold',
                    reason='submitted_at missing for blockhash safety check',
                    tx_signature=tx_signature,
                )
            age_seconds = (datetime.now(timezone.utc) - submitted_at).total_seconds()
            if age_seconds > RECONCILE_BLOCKHASH_EXPIRY_WINDOW_SECONDS:
                return ReconcileDecision(
                    decision='allow_requeue',
                    reason='beyond 150s blockhash safety window',
                    tx_signature=tx_signature,
                )
            return ReconcileDecision(
                decision='keep_in_hold',
                reason='too recent',
                tx_signature=tx_signature,
            )

        return ReconcileDecision(
            decision='keep_in_hold',
            reason=f'unexpected reconcile status: {chain_status}',
            tx_signature=tx_signature,
        )

    def migrate_legacy_provider_rows(self, guild_ids: Optional[List[int]] = None) -> int:
        migrated_count = 0
        legacy_rows = self.db_handler.get_legacy_provider_payment_requests(guild_ids=guild_ids)

        for payment in legacy_rows:
            payment_id = payment.get('payment_id')
            guild_id = payment.get('guild_id')
            producer = str(payment.get('producer') or '').strip().lower()
            if not payment_id or guild_id is None:
                continue

            if producer == 'grants':
                target_provider = 'solana_grants'
                updated = self.db_handler._update_payment_request_record(
                    payment_id,
                    {'provider': target_provider},
                    guild_id=guild_id,
                )
                if updated:
                    migrated_count += 1
                    self.logger.warning(
                        "[PaymentService] migrated legacy provider row %s in guild %s: %s -> %s",
                        payment_id,
                        guild_id,
                        payment.get('provider'),
                        target_provider,
                    )
                continue

            if producer == 'admin_chat':
                target_provider = 'solana_payouts'
                updated = self.db_handler._update_payment_request_record(
                    payment_id,
                    {'provider': target_provider},
                    guild_id=guild_id,
                )
                if updated:
                    migrated_count += 1
                    self.logger.warning(
                        "[PaymentService] migrated legacy provider row %s in guild %s: %s -> %s",
                        payment_id,
                        guild_id,
                        payment.get('provider'),
                        target_provider,
                    )
                continue

            reason = f"legacy provider could not be mapped: unknown producer={producer or 'unknown'}"
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason=reason,
                guild_id=guild_id,
            )
            self.logger.warning(
                "[PaymentService] migrated legacy provider row %s in guild %s: %s -> manual_hold",
                payment_id,
                guild_id,
                payment.get('provider'),
            )

        return migrated_count

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
            reference_timestamp = payment.get('updated_at')
            if status == 'submitted' and payment.get('submitted_at'):
                reference_timestamp = payment.get('submitted_at')
            reclaim_reference = _to_aware_utc(reference_timestamp)
            if reclaim_reference is not None:
                age_seconds = (datetime.now(timezone.utc) - reclaim_reference).total_seconds()
                if age_seconds < RECOVERY_RECLAIM_WINDOW_SECONDS:
                    refreshed = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
                    if refreshed:
                        recovered_rows.append(refreshed)
                    continue

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
                    elif chain_status == 'rpc_unreachable':
                        self.db_handler.mark_payment_manual_hold(
                            payment_id,
                            reason=RPC_UNREACHABLE_MANUAL_HOLD_REASON,
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
        elif confirmation_status == 'rpc_unreachable':
            self.db_handler.mark_payment_manual_hold(
                payment_id,
                reason=RPC_UNREACHABLE_MANUAL_HOLD_REASON,
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

    async def _emit_cap_breach(self, payment: Optional[Dict[str, Any]]) -> None:
        callback = getattr(self, '_on_cap_breach', None)
        if callback and payment:
            await callback(payment)


__all__ = ['PaymentActor', 'PaymentActorKind', 'PaymentService', 'ReconcileDecision']
