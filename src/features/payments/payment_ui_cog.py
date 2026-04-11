from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from src.common.redaction import redact_wallet as _redact_wallet
from src.common.discord_utils import safe_send_message

from .payment_service import PaymentActor, PaymentActorKind, PaymentService

logger = logging.getLogger('DiscordBot')


class PaymentConfirmView(discord.ui.View):
    """Persistent per-payment confirmation control."""

    def __init__(self, payment_ui_cog: 'PaymentUICog', payment_id: str):
        super().__init__(timeout=None)
        self.payment_ui_cog = payment_ui_cog
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

        existing_payment = self.payment_ui_cog.db_handler.get_payment_request(
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
        if expected_user_id is not None and int(expected_user_id) != interaction.user.id:
            await interaction.followup.send(
                "Only the intended recipient can confirm this payment.",
                ephemeral=True,
            )
            return

        payment = self.payment_ui_cog.payment_service.confirm_payment(
            self.payment_id,
            guild_id=interaction.guild_id,
            actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, interaction.user.id),
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
                    "[PaymentUICog] Failed to disable confirmation view for %s: %s",
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


class PaymentUICog(commands.Cog):
    """Confirmation UI and persistent view registration for payments."""

    def __init__(
        self,
        bot: commands.Bot,
        db_handler,
        payment_service: Optional[PaymentService] = None,
    ):
        self.bot = bot
        self.db_handler = db_handler
        self.payment_service = payment_service or getattr(bot, 'payment_service', None)
        self.bot.payment_service = self.payment_service
        self.bot.payment_ui_cog = self

    async def cog_load(self):
        await self._register_pending_confirmation_views()

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
                "[PaymentUICog] Re-registered %s persistent payment confirmation view(s).",
                len(pending),
            )

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
                "[PaymentUICog] Could not resolve confirmation destination for payment %s",
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

    @app_commands.command(
        name="payment-resolve",
        description="Reconcile one payment against on-chain truth.",
    )
    @app_commands.describe(payment_id="The payment request ID to reconcile")
    async def payment_resolve(self, interaction: discord.Interaction, payment_id: str):
        admin_user_id = os.getenv("ADMIN_USER_ID")
        if str(interaction.user.id) != str(admin_user_id):
            await interaction.response.send_message("admin-only", ephemeral=True)
            return

        if not self.payment_service:
            await interaction.response.send_message("payment_service unavailable", ephemeral=True)
            return

        decision = await self.payment_service.reconcile_with_chain(
            payment_id,
            guild_id=interaction.guild_id,
        )
        payment = self.db_handler.get_payment_request(payment_id, guild_id=interaction.guild_id) or {}
        status = payment.get('status') or 'unknown'
        tx_signature = _redact_wallet(decision.tx_signature or payment.get('tx_signature'))

        await interaction.response.send_message(
            "```text\n"
            f"decision: {decision.decision}\n"
            f"reason: {decision.reason}\n"
            f"status: {status}\n"
            f"tx_signature: {tx_signature}\n"
            "```",
            ephemeral=True,
        )

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
            logger.warning("[PaymentUICog] Failed to fetch destination %s: %s", target_id, exc)
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


async def setup(bot: commands.Bot):
    db_handler = getattr(bot, 'db_handler', None)
    payment_service = getattr(bot, 'payment_service', None)
    if db_handler is None or payment_service is None:
        logger.error("PaymentUICog setup skipped because db_handler or payment_service is missing.")
        return
    await bot.add_cog(PaymentUICog(bot, db_handler, payment_service=payment_service))
