from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from src.common.discord_utils import safe_send_message

from .payment_service import PaymentService

logger = logging.getLogger('DiscordBot')


def _redact_wallet(wallet: Optional[str]) -> str:
    if not wallet:
        return 'unknown'
    wallet = str(wallet)
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:4]}...{wallet[-4:]}"


class PaymentConfirmView(discord.ui.View):
    """Persistent per-payment confirmation control."""

    def __init__(self, payment_cog: 'PaymentCog', payment_id: str):
        super().__init__(timeout=None)
        self.payment_cog = payment_cog
        self.payment_id = payment_id

        confirm_button = discord.ui.Button(
            label="Confirm Payment",
            style=discord.ButtonStyle.success,
            custom_id=f"payment_confirm:{payment_id}",
        )
        confirm_button.callback = self._confirm_button_pressed
        self.add_item(confirm_button)

    async def _confirm_button_pressed(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing_payment = self.payment_cog.db_handler.get_payment_request(
            self.payment_id,
            guild_id=interaction.guild_id,
        )
        if not existing_payment:
            await interaction.followup.send(
                "I couldn't find that payment request anymore.",
                ephemeral=True,
            )
            return

        expected_user_id = existing_payment.get('recipient_discord_id')
        if expected_user_id and int(expected_user_id) != interaction.user.id:
            await interaction.followup.send(
                "Only the intended recipient can confirm this payment.",
                ephemeral=True,
            )
            return

        # interaction.user.id already matches existing_payment['recipient_discord_id'] when present.
        payment = self.payment_cog.payment_service.confirm_payment(
            self.payment_id,
            guild_id=interaction.guild_id,
            confirmed_by_user_id=interaction.user.id,
            confirmed_by='discord',
        )
        if not payment:
            await interaction.followup.send("I couldn't queue that payment.", ephemeral=True)
            return

        status = payment.get('status')
        if status == 'queued':
            self._disable_all_items()
            try:
                if interaction.message:
                    await interaction.message.edit(view=self)
            except Exception as exc:
                logger.warning(
                    "[PaymentCog] Failed to disable confirmation view for %s: %s",
                    self.payment_id,
                    exc,
                )
            await interaction.followup.send(
                f"Queued payment `{self.payment_id}` for processing.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Payment `{self.payment_id}` is already `{status}`.",
            ephemeral=True,
        )

    def _disable_all_items(self):
        for child in self.children:
            if hasattr(child, 'disabled'):
                child.disabled = True


class PaymentCog(commands.Cog):
    """Queue worker, restart recovery, and Discord confirmation flow for payments."""

    def __init__(
        self,
        bot: commands.Bot,
        db_handler,
        payment_service: Optional[PaymentService] = None,
    ):
        self.bot = bot
        self.db_handler = db_handler
        self.payment_service = (
            payment_service
            or getattr(bot, 'payment_service', None)
        )
        self.bot.payment_service = self.payment_service
        self._pending_terminal_handoffs: Dict[str, Dict[str, Any]] = {}
        self._replayed_pending_handoffs = False

        self.claim_batch_size = max(int(os.getenv('PAYMENT_CLAIM_LIMIT', '10')), 1)
        self.worker_interval_seconds = max(int(os.getenv('PAYMENT_WORKER_INTERVAL_SECONDS', '30')), 1)
        self._admin_success_dm_threshold_usd = float(os.getenv('ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD', '100'))
        self._admin_success_dm_providers = frozenset(
            provider.strip().lower()
            for provider in os.getenv('ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS', 'solana_payouts').split(',')
            if provider.strip()
        )
        self._startup_synced = False

    async def cog_load(self):
        self.payment_worker.change_interval(seconds=self.worker_interval_seconds)
        if not self.payment_worker.is_running():
            self.payment_worker.start()
            logger.info("[PaymentCog] Payment worker started.")
        if self._bot_is_ready():
            await self._ensure_startup_sync()

    def cog_unload(self):
        if self.payment_worker.is_running():
            self.payment_worker.cancel()
            logger.info("[PaymentCog] Payment worker stopped.")

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_startup_sync()
        if self._replayed_pending_handoffs:
            return
        self._replayed_pending_handoffs = True
        await self._flush_pending_terminal_handoffs()

    def _bot_is_ready(self) -> bool:
        checker = getattr(self.bot, 'is_ready', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    async def _ensure_startup_sync(self):
        if self._startup_synced:
            return
        self._startup_synced = True
        await self._register_pending_confirmation_views()
        await self._recover_inflight_payments()

    @tasks.loop(seconds=30)
    async def payment_worker(self):
        """Claim and execute due queued payment requests."""
        if not self.payment_service:
            return

        claimed = self.db_handler.claim_due_payment_requests(limit=self.claim_batch_size)
        if not claimed:
            return

        logger.info("[PaymentCog] Claimed %s payment request(s).", len(claimed))
        for payment in claimed:
            await self._process_claimed_payment(payment)

    @payment_worker.before_loop
    async def _before_payment_worker(self):
        await self.bot.wait_until_ready()

    async def _register_pending_confirmation_views(self):
        if not self.payment_service:
            return

        pending = self.payment_service.get_pending_confirmation_payments(
            guild_ids=self._get_writable_guild_ids(),
        )
        for payment in pending:
            payment_id = payment.get('payment_id')
            if not payment_id:
                continue
            self.bot.add_view(PaymentConfirmView(self, payment_id))

        if pending:
            logger.info(
                "[PaymentCog] Re-registered %s persistent payment confirmation view(s).",
                len(pending),
            )

    async def _recover_inflight_payments(self):
        if not self.payment_service:
            return

        recovered = await self.payment_service.recover_inflight(
            guild_ids=self._get_writable_guild_ids(),
        )
        for payment in recovered:
            if self._is_terminal(payment):
                await self._handle_terminal_payment(payment)

    async def send_confirmation_request(self, payment_id: str) -> Optional[discord.Message]:
        """Post one confirmation message with a persistent view."""
        payment = self.db_handler.get_payment_request(payment_id)
        if not payment:
            return None
        if payment.get('status') != 'pending_confirmation':
            return None

        destination = await self._resolve_destination(
            payment.get('confirm_channel_id'),
            payment.get('confirm_thread_id'),
        )
        if destination is None:
            logger.warning(
                "[PaymentCog] Could not resolve confirmation destination for payment %s",
                payment_id,
            )
            return None

        view = PaymentConfirmView(self, payment_id)
        self.bot.add_view(view)
        return await self._send_message(
            destination,
            self._build_confirmation_message(payment),
            view=view,
        )

    async def _process_claimed_payment(self, payment: Dict[str, Any]):
        payment_id = payment.get('payment_id')
        guild_id = payment.get('guild_id')
        if not payment_id:
            return

        try:
            result = await self.payment_service.execute_payment(payment_id, guild_id=guild_id)
        except Exception as exc:
            logger.error(
                "[PaymentCog] Unexpected error while executing payment %s: %s",
                payment_id,
                exc,
                exc_info=True,
            )
            current = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
            if current and current.get('status') in {'processing', 'submitted'}:
                self.db_handler.mark_payment_manual_hold(
                    payment_id,
                    reason='Worker hit an unexpected error; payment requires manual review',
                    guild_id=guild_id,
                )
                current = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
            result = current

        if result and self._is_terminal(result):
            await self._handle_terminal_payment(result)

    async def _handle_terminal_payment(self, payment: Dict[str, Any]):
        await self._notify_payment_result(payment)
        provider_key = str(payment.get('provider') or '').strip().lower()
        if (
            payment.get('status') == 'confirmed'
            and not payment.get('is_test')
            and provider_key in self._admin_success_dm_providers
            and float(payment.get('amount_usd') or 0) >= self._admin_success_dm_threshold_usd
        ):
            await self._dm_admin_payment_success(payment)
        if payment.get('status') in {'failed', 'manual_hold'}:
            await self._dm_admin_payment_failure(payment)
        await self._handoff_terminal_result(payment)

    async def _dm_admin_payment_success(self, payment: Dict[str, Any]):
        admin_id_env = os.getenv('ADMIN_USER_ID')
        if not admin_id_env:
            logger.warning(
                "[PaymentCog] ADMIN_USER_ID not set; cannot DM admin about payment %s",
                payment.get('payment_id'),
            )
            return
        try:
            admin_id = int(admin_id_env)
        except ValueError:
            logger.error("[PaymentCog] Invalid ADMIN_USER_ID; cannot DM admin.")
            return

        try:
            admin_user = await self.bot.fetch_user(admin_id)
        except Exception as exc:
            logger.error(
                "[PaymentCog] Failed to fetch admin user %s for payment DM: %s",
                admin_id,
                exc,
            )
            return

        amount_token = float(payment.get('amount_token') or 0)
        amount_usd = float(payment.get('amount_usd') or 0)
        lines = [
            "✅ **Payment Completed**",
            f"- Payment ID: `{payment.get('payment_id')}`",
            f"- Producer: `{payment.get('producer')}` / `{payment.get('producer_ref')}`",
            f"- Provider: `{payment.get('provider')}`",
            f"- Type: {'test payment' if payment.get('is_test') else 'final payment'}",
            f"- Amount: {amount_token:.8f} {self._token_label(payment)}",
            f"- USD: ${amount_usd:.2f}",
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`",
        ]
        if payment.get('tx_signature'):
            lines.append(f"- Transaction: `{payment.get('tx_signature')}`")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "..."

        try:
            await admin_user.send(message)
            logger.info(
                "[PaymentCog] DM'd admin about confirmed payment %s",
                payment.get('payment_id'),
            )
        except discord.Forbidden:
            logger.error("[PaymentCog] Bot forbidden from DMing admin about payment success.")
        except Exception as exc:
            logger.error(
                "[PaymentCog] Failed to DM admin about payment %s: %s",
                payment.get('payment_id'),
                exc,
            )

    async def _dm_admin_payment_failure(self, payment: Dict[str, Any]):
        admin_id_env = os.getenv('ADMIN_USER_ID')
        if not admin_id_env:
            logger.warning(
                "[PaymentCog] ADMIN_USER_ID not set; cannot DM admin about payment %s",
                payment.get('payment_id'),
            )
            return
        try:
            admin_id = int(admin_id_env)
        except ValueError:
            logger.error("[PaymentCog] Invalid ADMIN_USER_ID; cannot DM admin.")
            return

        try:
            admin_user = await self.bot.fetch_user(admin_id)
        except Exception as exc:
            logger.error(
                "[PaymentCog] Failed to fetch admin user %s for payment DM: %s",
                admin_id,
                exc,
            )
            return

        status = str(payment.get('status') or 'unknown').replace('_', ' ').title()
        amount = float(payment.get('amount_token') or 0)
        lines = [
            f"🚨 **Payment {status}**",
            f"- Payment ID: `{payment.get('payment_id')}`",
            f"- Producer: `{payment.get('producer')}` / `{payment.get('producer_ref')}`",
            f"- Provider: `{payment.get('provider')}`",
            f"- Type: {'test payment' if payment.get('is_test') else 'final payment'}",
            f"- Amount: {amount:.8f} {self._token_label(payment)}",
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`",
        ]
        if payment.get('tx_signature'):
            lines.append(f"- Transaction: `{payment.get('tx_signature')}`")
        if payment.get('last_error'):
            lines.append(f"- Detail: {payment.get('last_error')}")
        if payment.get('status') == 'manual_hold':
            lines.append("- ⚠️ Requires manual review — do NOT auto-retry.")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "..."

        try:
            await admin_user.send(message)
            logger.info(
                "[PaymentCog] DM'd admin about %s payment %s",
                payment.get('status'),
                payment.get('payment_id'),
            )
        except discord.Forbidden:
            logger.error("[PaymentCog] Bot forbidden from DMing admin about payment failure.")
        except Exception as exc:
            logger.error(
                "[PaymentCog] Failed to DM admin about payment %s: %s",
                payment.get('payment_id'),
                exc,
            )

    async def _notify_payment_result(self, payment: Dict[str, Any]):
        destination = await self._resolve_destination(
            payment.get('notify_channel_id'),
            payment.get('notify_thread_id'),
        )
        if destination is None:
            logger.warning(
                "[PaymentCog] Could not resolve notify destination for payment %s",
                payment.get('payment_id'),
            )
            return

        await self._send_message(destination, self._build_result_message(payment))

    async def _handoff_terminal_result(self, payment: Dict[str, Any]):
        producer = str(payment.get('producer') or '').strip().lower()
        if not producer:
            return

        cog = None
        for candidate in self._candidate_producer_cog_names(producer):
            cog = self.bot.get_cog(candidate)
            if cog:
                break
        if cog is None or not hasattr(cog, 'handle_payment_result'):
            payment_id = payment.get('payment_id')
            if payment_id:
                self._pending_terminal_handoffs[str(payment_id)] = dict(payment)
            return

        try:
            result = cog.handle_payment_result(payment)
            if inspect.isawaitable(result):
                await result
            payment_id = payment.get('payment_id')
            if payment_id:
                self._pending_terminal_handoffs.pop(str(payment_id), None)
        except Exception as exc:
            logger.error(
                "[PaymentCog] Producer handoff failed for payment %s (%s): %s",
                payment.get('payment_id'),
                producer,
                exc,
                exc_info=True,
            )

    async def _flush_pending_terminal_handoffs(self):
        if not self._pending_terminal_handoffs:
            return

        for payment_id, payment in list(self._pending_terminal_handoffs.items()):
            await self._handoff_terminal_result(payment)
            if payment_id in self._pending_terminal_handoffs:
                logger.warning(
                    "[PaymentCog] Producer handoff still unavailable for recovered payment %s",
                    payment_id,
                )

    def _candidate_producer_cog_names(self, producer: str) -> List[str]:
        title = ''.join(part.capitalize() for part in producer.split('_'))
        return [
            f"{title}Cog",
            f"{producer.capitalize()}Cog",
            title,
            producer,
        ]

    async def _resolve_destination(
        self,
        channel_id: Optional[int],
        thread_id: Optional[int],
    ) -> Optional[discord.abc.Messageable]:
        target_id = int(thread_id) if thread_id else int(channel_id) if channel_id else None
        if target_id is None:
            return None

        destination = self.bot.get_channel(target_id)
        if destination is not None:
            return destination

        try:
            return await self.bot.fetch_channel(target_id)
        except Exception as exc:
            logger.warning("[PaymentCog] Failed to fetch destination %s: %s", target_id, exc)
            return None

    async def _send_message(
        self,
        destination: discord.abc.Messageable,
        content: str,
        *,
        view: Optional[discord.ui.View] = None,
    ) -> Optional[discord.Message]:
        rate_limiter = getattr(self.bot, 'rate_limiter', None)
        if rate_limiter is not None:
            return await safe_send_message(
                self.bot,
                destination,
                rate_limiter,
                logger,
                content=content,
                view=view,
            )
        return await destination.send(content, view=view)

    def _build_confirmation_message(self, payment: Dict[str, Any]) -> str:
        amount = float(payment.get('amount_token') or 0)
        payment_type = 'test payment' if payment.get('is_test') else 'final payment'
        return (
            f"**Payment confirmation required**\n"
            f"- Payment ID: `{payment.get('payment_id')}`\n"
            f"- Producer: `{payment.get('producer')}` / `{payment.get('producer_ref')}`\n"
            f"- Type: {payment_type}\n"
            f"- Amount: {amount:.8f} {self._token_label(payment)}\n"
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`\n"
            f"- Route: `{payment.get('route_key') or 'unrouted'}`"
        )

    def _build_result_message(self, payment: Dict[str, Any]) -> str:
        amount = float(payment.get('amount_token') or 0)
        status = str(payment.get('status') or 'unknown')
        lines = [
            f"**Payment {status.replace('_', ' ').title()}**",
            f"- Payment ID: `{payment.get('payment_id')}`",
            f"- Producer: `{payment.get('producer')}` / `{payment.get('producer_ref')}`",
            f"- Type: {'test payment' if payment.get('is_test') else 'final payment'}",
            f"- Amount: {amount:.8f} {self._token_label(payment)}",
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`",
        ]
        if payment.get('tx_signature'):
            lines.append(f"- Transaction: `{payment.get('tx_signature')}`")
        if payment.get('last_error'):
            lines.append(f"- Detail: {payment.get('last_error')}")
        return "\n".join(lines)

    def _token_label(self, payment: Dict[str, Any]) -> str:
        provider = None
        provider_name = payment.get('provider')
        if provider_name and self.payment_service:
            providers = getattr(self.payment_service, 'providers', None) or {}
            provider = providers.get(str(provider_name).strip().lower())
        if provider is not None:
            try:
                return provider.token_name()
            except Exception:
                pass
        chain = str(payment.get('chain') or '').strip().upper()
        return chain or 'TOKEN'

    def _get_writable_guild_ids(self) -> Optional[List[int]]:
        server_config = getattr(self.db_handler, 'server_config', None)
        if not server_config:
            return None
        guild_ids = [int(server['guild_id']) for server in server_config.get_enabled_servers(require_write=True)]
        return guild_ids or None

    def _is_terminal(self, payment: Optional[Dict[str, Any]]) -> bool:
        if not payment:
            return False
        return payment.get('status') in {'confirmed', 'failed', 'manual_hold', 'cancelled'}


async def setup(bot: commands.Bot):
    db_handler = getattr(bot, 'db_handler', None)
    payment_service = getattr(bot, 'payment_service', None)
    if db_handler is None or payment_service is None:
        logger.error("PaymentCog setup skipped because db_handler or payment_service is missing.")
        return
    await bot.add_cog(PaymentCog(bot, db_handler, payment_service=payment_service))
