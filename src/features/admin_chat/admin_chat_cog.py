"""Discord cog for privileged admin chat and approved member bot access."""
import asyncio
import json
import os
import logging
import time
from collections import deque
from typing import Dict
import discord
from anthropic import AsyncAnthropic
from discord.ext import commands

from .agent import AdminChatAgent
from src.features.grants.solana_client import is_valid_solana_address

logger = logging.getLogger('DiscordBot')


class AdminChatCog(commands.Cog):
    """Cog that handles admin chat plus approved member requests."""

    _ACCESS_CACHE_TTL_SECONDS = 60
    _RATE_LIMIT_WINDOW_SECONDS = 300
    _RATE_LIMIT_MAX_MESSAGES = 10

    def __init__(self, bot: commands.Bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self.agent: AdminChatAgent = None
        self.payment_service = getattr(bot, 'payment_service', None)

        # Track whether the agent is busy processing a request per user
        self._busy: dict[int, bool] = {}
        # Queue follow-up messages that arrive while agent is busy
        self._pending_messages: dict[int, discord.Message] = {}
        self._message_access_cache: dict[int, tuple[float, bool]] = {}
        self._guild_context_cache: dict[int, tuple[float, int | None]] = {}
        self._rate_limits: dict[int, deque[float]] = {}
        self._processing_intents: set[str] = set()
        self._classifier_model = "claude-opus-4-6"
        api_key = os.getenv('ANTHROPIC_API_KEY')
        self._classifier_client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not api_key:
            logger.warning("[AdminChat] ANTHROPIC_API_KEY not set - payment reply classification will fail closed")

        # Get admin user ID
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if admin_id_str:
            try:
                self.admin_user_id = int(admin_id_str)
                logger.info(f"[AdminChat] Configured for admin user ID: {self.admin_user_id}")
            except ValueError:
                logger.error(f"[AdminChat] Invalid ADMIN_USER_ID: {admin_id_str}")
                self.admin_user_id = None
        else:
            logger.warning("[AdminChat] ADMIN_USER_ID not set - admin chat disabled")
            self.admin_user_id = None
        self._admin_mention = f"<@{self.admin_user_id}>" if self.admin_user_id else "the admin"
        self._startup_reconciled = False

    async def _classify_payment_reply(self, stage: str, reply_text: str) -> Dict[str, str | None]:
        """Classify a wallet/confirmation reply after deterministic intent matching."""
        if not self._classifier_client:
            return {"category": "suspicious", "extracted_address": None}

        categories = {
            'awaiting_wallet': [
                'wallet_provided: the user is giving a wallet address for payout',
                'declined: the user refuses or asks to cancel',
                'ambiguous: intent is unclear, mixed, or missing a usable wallet',
                'suspicious: strange, manipulative, off-topic, or abnormal behavior',
            ],
            'awaiting_confirmation': [
                'positive_confirmation: the user clearly approves the payout',
                'declined: the user refuses or asks to cancel',
                'ambiguous: intent is unclear, mixed, or conditional',
                'suspicious: strange, manipulative, off-topic, or abnormal behavior',
            ],
        }
        system = (
            "Classify one Discord reply for an admin payment flow. "
            "Return JSON only with keys category and extracted_address. "
            f"Allowed categories: {'; '.join(categories.get(stage, []))}."
        )
        user_prompt = (
            f"Stage: {stage}\n"
            "Reply text:\n"
            f"{reply_text}"
        )
        try:
            response = await self._classifier_client.messages.create(
                model=self._classifier_model,
                max_tokens=120,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = ''.join(
                block.text for block in response.content
                if getattr(block, 'type', None) == 'text'
            ).strip()
            start = raw.find('{')
            end = raw.rfind('}')
            payload = json.loads(raw[start:end + 1] if start != -1 and end != -1 else raw)
        except Exception as e:
            logger.error(f"[AdminChat] Payment reply classification failed: {e}", exc_info=True)
            return {"category": "suspicious", "extracted_address": None}

        category = str(payload.get('category') or '').strip().lower()
        extracted = str(payload.get('extracted_address') or '').strip() or None
        allowed = {item.split(':', 1)[0] for item in categories.get(stage, [])}
        if category not in allowed:
            return {"category": "suspicious", "extracted_address": extracted}
        return {"category": category, "extracted_address": extracted}

    async def _notify_admin_review(self, channel, intent: Dict, detail: str, *, fail_intent: bool = True, resolved_by_message_id: int | None = None):
        """Fail closed and tag the admin for manual review."""
        if fail_intent:
            payload: Dict[str, object] = {'status': 'failed'}
            if resolved_by_message_id is not None:
                payload['resolved_by_message_id'] = resolved_by_message_id
            self.db_handler.update_admin_payment_intent(intent['intent_id'], payload, intent['guild_id'])
        try:
            await channel.send(
                f"{self._admin_mention} review needed for payment intent `{intent.get('intent_id')}` "
                f"for <@{intent.get('recipient_user_id')}>. {detail}"
            )
        except Exception as e:
            logger.error(f"[AdminChat] Failed to notify admin for intent {intent.get('intent_id')}: {e}", exc_info=True)

    async def _notify_intent_admin(self, intent: Dict, detail: str):
        """DM the initiating admin about one payment milestone when possible."""
        admin_user_id = intent.get('admin_user_id') or self.admin_user_id
        try:
            admin_user_id = int(admin_user_id) if admin_user_id is not None else None
        except (TypeError, ValueError):
            admin_user_id = None
        if not admin_user_id:
            return
        try:
            admin_user = await self.bot.fetch_user(admin_user_id)
            await admin_user.send(detail)
        except Exception as e:
            logger.warning(
                "[AdminChat] Failed to DM admin %s for intent %s: %s",
                admin_user_id,
                intent.get('intent_id'),
                e,
            )

    def _resolve_payment_destinations(self, channel) -> Dict[str, int | None]:
        """Resolve persisted payment destinations with a safe in-channel fallback."""
        server_config = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        if server_config:
            resolved = server_config.resolve_payment_destinations(channel.guild.id, channel.id, 'admin_chat')
            if resolved:
                return resolved
        parent_id = getattr(channel, 'parent_id', None)
        return {
            'route_key': None,
            'confirm_channel_id': parent_id or channel.id,
            'confirm_thread_id': channel.id if parent_id else None,
            'notify_channel_id': parent_id or channel.id,
            'notify_thread_id': channel.id if parent_id else None,
        }

    def _get_payment_cog(self):
        return self.bot.get_cog('PaymentCog')

    async def _fetch_intent_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    def _format_payment_destination(self, payment: Dict) -> str:
        destination_id = payment.get('confirm_thread_id') or payment.get('confirm_channel_id')
        if destination_id:
            return f"<#{destination_id}>"
        return "the configured payment channel"

    def _explorer_url(self, tx_sig: str) -> str:
        rpc_url = os.getenv('SOLANA_RPC_URL', '')
        if 'devnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=devnet"
        if 'testnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=testnet"
        return f"https://explorer.solana.com/tx/{tx_sig}"

    async def _handle_wallet_received(self, message: discord.Message, intent: Dict, wallet_address: str):
        """Persist a wallet reply and kick off the test-payment flow."""
        wallet_record = self.db_handler.upsert_wallet(
            guild_id=int(intent['guild_id']),
            discord_user_id=int(intent['recipient_user_id']),
            chain='solana',
            address=wallet_address,
            metadata={'producer': 'admin_chat', 'intent_id': intent['intent_id'], 'channel_id': message.channel.id},
        )
        if not wallet_record:
            await self._notify_admin_review(message.channel, intent, "I could not store the recipient wallet.", resolved_by_message_id=message.id)
            return

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {
                'status': 'awaiting_test',
                'wallet_id': wallet_record.get('wallet_id'),
                'resolved_by_message_id': message.id,
            },
            intent['guild_id'],
        )
        if not updated_intent:
            await self._notify_admin_review(message.channel, intent, "I could not update the payment intent after receiving the wallet.", resolved_by_message_id=message.id)
            return
        await self._start_admin_payment_flow(message.channel, updated_intent)

    async def _start_admin_payment_flow(self, channel, intent: Dict):
        """Create and auto-confirm the verification payment for one admin payment intent."""
        if not self.payment_service:
            await self._notify_admin_review(channel, intent, "The payment service is not configured.")
            return
        wallet_id = intent.get('wallet_id')
        wallet_record = self.db_handler.get_wallet_by_id(wallet_id, guild_id=intent['guild_id']) if wallet_id else None
        if not wallet_record:
            await self._notify_admin_review(channel, intent, "No verified wallet record is available for the recipient.")
            return

        destinations = self._resolve_payment_destinations(channel)
        test_payment = await self.payment_service.request_payment(
            producer='admin_chat',
            producer_ref=str(intent['producer_ref']),
            guild_id=int(intent['guild_id']),
            recipient_wallet=wallet_record['wallet_address'],
            chain='solana',
            provider='solana',
            is_test=True,
            confirm_channel_id=destinations['confirm_channel_id'],
            confirm_thread_id=destinations['confirm_thread_id'],
            notify_channel_id=destinations['notify_channel_id'],
            notify_thread_id=destinations['notify_thread_id'],
            recipient_discord_id=int(intent['recipient_user_id']),
            wallet_id=wallet_record.get('wallet_id'),
            route_key=destinations.get('route_key'),
            metadata={'intent_id': intent['intent_id']},
        )
        if not test_payment:
            await self._notify_admin_review(channel, intent, "I could not create the wallet verification payment.")
            return

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'test_payment_id': test_payment.get('payment_id')},
            intent['guild_id'],
        ) or intent
        confirmed = self.payment_service.confirm_payment(
            test_payment['payment_id'],
            guild_id=int(intent['guild_id']),
            confirmed_by='auto',
            confirmed_by_user_id=int(intent['recipient_user_id']),
        )
        if not confirmed:
            await self._notify_admin_review(channel, updated_intent, "I could not queue the wallet verification payment.")
            return

        await channel.send(
            f"<@{intent['recipient_user_id']}> thanks. I've queued a small test payment to verify your wallet.\n\n"
            "Once that lands, I'll ask you to confirm the final payout here."
        )

    async def _handle_confirmation_received(self, message: discord.Message, intent: Dict):
        """Persist a free-text final payout confirmation."""
        if not self.payment_service or not intent.get('final_payment_id'):
            await self._notify_admin_review(message.channel, intent, "The final payment record is unavailable, so I can't accept this confirmation.", resolved_by_message_id=message.id)
            return

        confirmed = self.payment_service.confirm_payment(
            intent['final_payment_id'],
            guild_id=int(intent['guild_id']),
            confirmed_by='free_text',
            confirmed_by_user_id=message.author.id,
        )
        if not confirmed:
            await self._notify_admin_review(message.channel, intent, "The final payout confirmation could not be applied.", resolved_by_message_id=message.id)
            return

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'status': 'confirmed', 'resolved_by_message_id': message.id},
            intent['guild_id'],
        )
        if not updated_intent:
            await self._notify_admin_review(message.channel, intent, "The intent row could not be updated after confirmation.", resolved_by_message_id=message.id)
            return
        await message.channel.send(f"<@{intent['recipient_user_id']}> confirmation received. The payout has been queued.")

    async def handle_payment_result(self, payment: Dict):
        """Receive terminal payment outcomes from the shared payment cog."""
        if str(payment.get('producer') or '').strip().lower() != 'admin_chat':
            return

        metadata = payment.get('metadata') or {}
        intent_id = str(metadata.get('intent_id') or '').strip()
        guild_id = payment.get('guild_id')
        if not intent_id or guild_id is None:
            logger.warning("[AdminChat] Ignoring payment result with missing intent_id/guild_id: %s", payment.get('payment_id'))
            return
        guild_id = int(guild_id)

        intent = self.db_handler.get_admin_payment_intent(intent_id, guild_id)
        if not intent:
            logger.warning("[AdminChat] Missing intent %s for payment %s", intent_id, payment.get('payment_id'))
            return

        channel = await self._fetch_intent_channel(int(intent['channel_id']))
        status = str(payment.get('status') or '').strip().lower()

        if payment.get('is_test'):
            if status != 'confirmed':
                if channel:
                    await self._notify_admin_review(
                        channel,
                        intent,
                        f"The wallet verification payment ended in `{status}`, so no final payout was created.",
                    )
                else:
                    self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
                return

            tx_signature = payment.get('tx_signature')
            explorer_link = self._explorer_url(tx_signature) if tx_signature else None
            explorer_text = f"\nExplorer: {explorer_link}" if explorer_link else ""
            await self._notify_intent_admin(
                intent,
                (
                    f"Test payment confirmed for <@{intent['recipient_user_id']}>.\n"
                    f"Intent: `{intent_id}`\n"
                    f"Amount: {float(payment.get('amount_token') or 0):.4f} SOL"
                    f"{explorer_text}"
                ),
            )

            if not self.payment_service:
                if channel:
                    await self._notify_admin_review(channel, intent, "The payment service is not configured for the final payout.")
                else:
                    self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
                return

            final_payment = await self.payment_service.request_payment(
                producer='admin_chat',
                producer_ref=str(intent['producer_ref']),
                guild_id=int(guild_id),
                recipient_wallet=payment['recipient_wallet'],
                chain=str(payment.get('chain') or 'solana'),
                provider=str(payment.get('provider') or 'solana'),
                is_test=False,
                amount_token=float(intent['requested_amount_sol']),
                confirm_channel_id=int(payment['confirm_channel_id']),
                confirm_thread_id=payment.get('confirm_thread_id'),
                notify_channel_id=int(payment['notify_channel_id']),
                notify_thread_id=payment.get('notify_thread_id'),
                recipient_discord_id=int(intent['recipient_user_id']),
                wallet_id=payment.get('wallet_id') or intent.get('wallet_id'),
                route_key=payment.get('route_key'),
                metadata={'intent_id': intent_id},
            )
            if not final_payment:
                if channel:
                    await self._notify_admin_review(channel, intent, "The wallet test succeeded, but I could not create the final payout request.")
                else:
                    self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
                return

            prompt_message = None
            payment_cog = self._get_payment_cog()
            if payment_cog and final_payment.get('status') == 'pending_confirmation':
                try:
                    await payment_cog.send_confirmation_request(final_payment['payment_id'])
                except Exception as e:
                    logger.error(f"[AdminChat] Failed to send payment confirmation request for {final_payment.get('payment_id')}: {e}", exc_info=True)

            if channel:
                prompt_message = await channel.send(
                    f"<@{intent['recipient_user_id']}> the wallet test payment confirmed.\n\n"
                    f"Please confirm the full payout using the payment prompt in {self._format_payment_destination(final_payment)} "
                    "or reply here with a clear confirmation message."
                )

            payload = {
                'status': 'awaiting_confirmation',
                'final_payment_id': final_payment.get('payment_id'),
                'last_scanned_message_id': None,
            }
            if prompt_message is not None:
                payload['prompt_message_id'] = prompt_message.id
            updated = self.db_handler.update_admin_payment_intent(intent_id, payload, guild_id)
            if not updated and channel:
                await self._notify_admin_review(channel, intent, "The final payout was created, but I could not update the intent row.")
            return

        if status == 'confirmed':
            updated = self.db_handler.update_admin_payment_intent(intent_id, {'status': 'completed'}, guild_id)
            amount = float(payment.get('amount_token') or intent.get('requested_amount_sol') or 0)
            tx_signature = payment.get('tx_signature')
            explorer_link = self._explorer_url(tx_signature) if tx_signature else None
            if channel:
                explorer_text = f"[View on Explorer]({explorer_link})" if explorer_link else "Transaction signature unavailable"
                await channel.send(
                    f"**Payment sent.**\n\n"
                    f"- Recipient: <@{intent['recipient_user_id']}>\n"
                    f"- Amount: {amount:.4f} SOL\n"
                    f"- Transaction: {explorer_text}"
                )
            await self._notify_intent_admin(
                intent,
                (
                    f"Final payment confirmed for <@{intent['recipient_user_id']}>.\n"
                    f"Intent: `{intent_id}`\n"
                    f"Amount: {amount:.4f} SOL\n"
                    f"Explorer: {explorer_link or 'unavailable'}"
                ),
            )
            if not updated and channel:
                await self._notify_admin_review(channel, intent, "The payout completed, but I could not mark the intent as completed.", fail_intent=False)
            return

        if channel:
            await self._notify_admin_review(
                channel,
                intent,
                f"The final payout ended in `{status}`, so I stopped the flow for manual review.",
            )
        else:
            self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)

    async def cog_load(self):
        if self._bot_is_ready():
            await self._ensure_startup_reconciled()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_startup_reconciled()

    def _bot_is_ready(self) -> bool:
        checker = getattr(self.bot, 'is_ready', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    async def _ensure_startup_reconciled(self):
        if self._startup_reconciled:
            return
        self._startup_reconciled = True
        try:
            await self._reconcile_active_intents()
        except Exception as e:
            logger.error(f"[AdminChat] Startup reconciliation failed: {e}", exc_info=True)

    def _get_reconciliation_guild_ids(self) -> list[int]:
        server_config = getattr(self.db_handler, 'server_config', None)
        if server_config:
            return [
                int(server['guild_id'])
                for server in server_config.get_enabled_servers(require_write=True)
                if server.get('guild_id') is not None
            ]
        return [guild.id for guild in getattr(self.bot, 'guilds', [])]

    def _is_terminal_payment(self, payment: Dict | None) -> bool:
        return bool(payment and payment.get('status') in {'confirmed', 'failed', 'manual_hold', 'cancelled'})

    async def _reconcile_payment_status(self, intent: Dict, payment_key: str):
        payment_id = intent.get(payment_key)
        if not payment_id:
            return
        payment = self.db_handler.get_payment_request(payment_id, guild_id=int(intent['guild_id']))
        if self._is_terminal_payment(payment):
            await self.handle_payment_result(payment)

    async def _reconcile_intent_history(self, intent: Dict):
        channel = await self._fetch_intent_channel(int(intent['channel_id']))
        if channel is None:
            logger.warning("[AdminChat] Reconciliation skipped missing channel %s for intent %s", intent.get('channel_id'), intent.get('intent_id'))
            return

        cursor_id = intent.get('last_scanned_message_id') or intent.get('prompt_message_id')
        messages = []
        try:
            if cursor_id:
                async for message in channel.history(
                    limit=200,
                    after=discord.Object(id=int(cursor_id)),
                    oldest_first=True,
                ):
                    messages.append(message)
            else:
                async for message in channel.history(limit=200):
                    messages.append(message)
                messages.reverse()
        except Exception as e:
            logger.warning("[AdminChat] Reconciliation history scan failed for intent %s: %s", intent.get('intent_id'), e)
            return

        if not messages:
            return

        last_seen_message_id = None
        original_status = str(intent.get('status') or '').strip().lower()
        recipient_user_id = int(intent['recipient_user_id'])
        for message in messages:
            last_seen_message_id = message.id
            if getattr(message.author, 'id', None) != recipient_user_id:
                continue
            await self._check_pending_payment_reply(message)
            refreshed = self.db_handler.get_admin_payment_intent(intent['intent_id'], int(intent['guild_id']))
            refreshed_status = str((refreshed or {}).get('status') or '').strip().lower()
            if refreshed is None or refreshed_status != original_status:
                break

        self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'last_scanned_message_id': last_seen_message_id},
            int(intent['guild_id']),
        )
        if len(messages) == 200:
            logger.warning("[AdminChat] Reconciliation capped at 200 messages for intent %s", intent.get('intent_id'))

    async def _reconcile_active_intents(self):
        for guild_id in self._get_reconciliation_guild_ids():
            intents = self.db_handler.list_active_intents(guild_id)
            for intent in intents:
                status = str(intent.get('status') or '').strip().lower()
                if status in {'awaiting_wallet', 'awaiting_confirmation'}:
                    await self._reconcile_intent_history(intent)
                    refreshed = self.db_handler.get_admin_payment_intent(intent['intent_id'], int(intent['guild_id']))
                    if refreshed and str(refreshed.get('status') or '').strip().lower() == 'awaiting_confirmation':
                        await self._reconcile_payment_status(refreshed, 'final_payment_id')
                    continue
                if status == 'awaiting_test':
                    await self._reconcile_payment_status(intent, 'test_payment_id')
                    continue
                if status == 'confirmed':
                    await self._reconcile_payment_status(intent, 'final_payment_id')

    async def _check_pending_payment_reply(self, message: discord.Message) -> bool:
        """Intercept recipient replies for active admin payment intents before normal bot routing."""
        if message.author.bot or message.guild is None:
            return False

        intent = self.db_handler.get_active_intent_for_recipient(message.guild.id, message.channel.id, message.author.id)
        if not intent:
            return False

        intent_id = str(intent.get('intent_id') or '')
        if not intent_id:
            return False
        if intent_id in self._processing_intents:
            return True

        self._processing_intents.add(intent_id)
        try:
            content = (message.content or '').strip()
            if not content:
                return False

            status = str(intent.get('status') or '').strip().lower()
            if status == 'awaiting_wallet':
                if is_valid_solana_address(content):
                    await self._handle_wallet_received(message, intent, content)
                    return True
                verdict = await self._classify_payment_reply('awaiting_wallet', content)
                category = verdict.get('category')
                extracted = verdict.get('extracted_address')
                if category == 'wallet_provided' and extracted and is_valid_solana_address(extracted):
                    await self._handle_wallet_received(message, intent, extracted)
                elif category == 'declined':
                    self.db_handler.update_admin_payment_intent(
                        intent_id,
                        {'status': 'cancelled', 'resolved_by_message_id': message.id},
                        intent['guild_id'],
                    )
                    await message.channel.send(f"<@{intent['recipient_user_id']}> understood. This payment request is cancelled.")
                else:
                    await self._notify_admin_review(
                        message.channel,
                        intent,
                        "The wallet reply was ambiguous or suspicious, so I did not continue the payout.",
                        resolved_by_message_id=message.id,
                    )
                return True

            if status == 'awaiting_confirmation':
                verdict = await self._classify_payment_reply('awaiting_confirmation', content)
                category = verdict.get('category')
                if category == 'positive_confirmation':
                    await self._handle_confirmation_received(message, intent)
                elif category == 'declined':
                    final_payment_id = intent.get('final_payment_id')
                    if final_payment_id:
                        self.db_handler.cancel_payment(
                            final_payment_id,
                            guild_id=int(intent['guild_id']),
                            reason='Recipient declined final payout in channel',
                        )
                    self.db_handler.update_admin_payment_intent(
                        intent_id,
                        {'status': 'cancelled', 'resolved_by_message_id': message.id},
                        intent['guild_id'],
                    )
                    await message.channel.send(f"<@{intent['recipient_user_id']}> understood. This payout is cancelled.")
                else:
                    await self._notify_admin_review(
                        message.channel,
                        intent,
                        "The final payout reply was ambiguous or suspicious, so I did not advance the payout.",
                        resolved_by_message_id=message.id,
                    )
                return True

            return False
        finally:
            self._processing_intents.discard(intent_id)

    def _get_supabase(self):
        """Get the shared Supabase client if available."""
        storage_handler = getattr(self.db_handler, 'storage_handler', None)
        return getattr(storage_handler, 'supabase_client', None)
    
    def _ensure_agent(self):
        """Lazily initialize the agent (to avoid issues during bot startup)."""
        if self.agent is None:
            try:
                self.agent = AdminChatAgent(
                    bot=self.bot,
                    db_handler=self.db_handler,
                    sharer=self.sharer
                )
                logger.info("[AdminChat] Agent initialized")
            except Exception as e:
                logger.error(f"[AdminChat] Failed to initialize agent: {e}", exc_info=True)
                raise
    
    def _is_directed_at_bot(self, message: discord.Message) -> bool:
        """Check if a message is directed at the bot (mention, reply, or DM)."""
        if message.author.bot:
            return False
        if not message.content.strip():
            return False

        # DMs always count
        if isinstance(message.channel, discord.DMChannel):
            return True

        # In public channels, respond if the bot is directly @mentioned (not @everyone/@here)
        if self.bot.user and self.bot.user.mentioned_in(message) and not message.mention_everyone:
            return True

        # Also respond if the bot's managed role is @mentioned
        if self.bot.user and message.role_mentions:
            bot_member = message.guild.get_member(self.bot.user.id) if message.guild else None
            if bot_member and any(role in message.role_mentions for role in bot_member.roles if role.is_bot_managed()):
                return True

        # Also respond if replying to one of the bot's messages
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and ref.author.id == self.bot.user.id:
                return True

        return False

    def _is_admin(self, user_id: int) -> bool:
        """Check if a user is the admin."""
        return self.admin_user_id is not None and user_id == self.admin_user_id

    async def _can_user_message_bot(self, user_id: int) -> bool:
        """Check members.can_message_bot with a short in-memory cache."""
        now = time.monotonic()
        cached = self._message_access_cache.get(user_id)
        if cached and now - cached[0] < self._ACCESS_CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_supabase()
        if client is None:
            return False

        result = await asyncio.to_thread(
            client.table('members')
            .select('can_message_bot')
            .eq('member_id', user_id)
            .limit(1)
            .execute
        )
        allowed = bool(result.data and result.data[0].get('can_message_bot'))
        self._message_access_cache[user_id] = (now, allowed)
        return allowed

    async def _resolve_context_guild_id(self, user_id: int, guild_hint: int | None = None) -> int | None:
        """Resolve a trusted guild context for the requester."""
        if guild_hint is not None:
            server_config = getattr(self.db_handler, 'server_config', None)
            if self.bot.get_guild(guild_hint) is None:
                return None
            if server_config and not server_config.is_guild_enabled(guild_hint):
                return None
            return guild_hint

        now = time.monotonic()
        cached = self._guild_context_cache.get(user_id)
        if cached and now - cached[0] < self._ACCESS_CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_supabase()
        if client is None:
            return None

        result = await asyncio.to_thread(
            client.table('guild_members')
            .select('guild_id')
            .eq('member_id', user_id)
            .execute
        )
        server_config = getattr(self.db_handler, 'server_config', None)
        guild_ids = sorted({
            int(row['guild_id'])
            for row in (result.data or [])
            if row.get('guild_id') is not None
            and self.bot.get_guild(int(row['guild_id'])) is not None
            and (server_config is None or server_config.is_guild_enabled(int(row['guild_id'])))
        })
        if not guild_ids:
            resolved = None
        else:
            default_guild_id = server_config.get_default_guild_id(require_write=False) if server_config else None
            resolved = default_guild_id if default_guild_id in guild_ids else guild_ids[0]
            if len(guild_ids) > 1:
                logger.info(f"[AdminChat] Resolved DM guild for {user_id} to {resolved} from {guild_ids}")

        self._guild_context_cache[user_id] = (now, resolved)
        return resolved

    def _is_rate_limited(self, user_id: int) -> bool:
        """Apply a simple sliding-window limit for non-admin users."""
        now = time.monotonic()
        bucket = self._rate_limits.setdefault(user_id, deque())
        while bucket and now - bucket[0] > self._RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= self._RATE_LIMIT_MAX_MESSAGES:
            return True
        bucket.append(now)
        return False

    def _strip_mention(self, content: str, guild: discord.Guild = None) -> str:
        """Remove the bot @mention and bot role @mention from message content."""
        if self.bot.user:
            content = content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '')
            if guild:
                bot_member = guild.get_member(self.bot.user.id)
                if bot_member:
                    for role in bot_member.roles:
                        if role.is_bot_managed():
                            content = content.replace(f'<@&{role.id}>', '')
        return content.strip()

    _ABORT_PHRASES = {'stop', 'abort', 'cancel', 'halt', 'nevermind', 'never mind', 'quit', 'enough'}

    def _is_abort(self, content: str) -> bool:
        """Check if the message is an abort request."""
        normalised = content.strip().lower().rstrip('!.')
        return normalised in self._ABORT_PHRASES

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle admin chat and approved member bot requests."""

        if await self._check_pending_payment_reply(message):
            return

        if not self._is_directed_at_bot(message):
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_admin = self._is_admin(message.author.id)
        content = message.content if is_dm else self._strip_mention(message.content, message.guild)

        if not content:
            return

        resolved_guild_id = message.guild.id if message.guild else None
        if not is_admin:
            if not await self._can_user_message_bot(message.author.id):
                return
            resolved_guild_id = await self._resolve_context_guild_id(message.author.id, resolved_guild_id)
            if resolved_guild_id is None:
                logger.info(f"[AdminChat] No guild context for approved member {message.author.id}")
                return
            if self._is_rate_limited(message.author.id):
                await message.reply("Slow down a bit. Try again in a few minutes.")
                return

        user_id = message.author.id
        source = "DM" if is_dm else f"#{getattr(message.channel, 'name', 'unknown')}"
        role = "admin" if is_admin else "member"
        logger.info(f"[AdminChat] Received from {role} in {source}: {content[:50]}...")

        # If agent is busy, check if this is an abort or queue it
        if self._busy.get(user_id):
            if self._is_abort(content):
                if self.agent:
                    self.agent.request_abort(user_id)
                    logger.info(f"[AdminChat] Abort requested by user {user_id}")
                await message.add_reaction("\u23f9\ufe0f")  # stop button emoji
                return
            else:
                # Queue the message to process after current run finishes
                self._pending_messages[user_id] = message
                return

        try:
            # Initialize agent if needed
            self._ensure_agent()

            # Build channel context for non-DM messages
            channel_context = None
            if is_dm:
                ch = message.channel
                guild = self.bot.get_guild(resolved_guild_id) if resolved_guild_id else None
                channel_context = {
                    "source": "dm",
                    "guild_id": str(resolved_guild_id) if resolved_guild_id else None,
                    "guild_name": guild.name if guild else None,
                    "channel_id": str(ch.id),
                    "channel_name": "DM",
                }

                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message):
                        channel_context["replied_to"] = {
                            "message_id": str(ref.id),
                            "author": ref.author.display_name,
                            "content": (ref.content or '')[:500],
                        }
                        channel_context["replied_to_anchor_note"] = "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."

                try:
                    recent = []
                    async for msg in ch.history(limit=10):
                        if msg.id == message.id:
                            continue
                        recent.append(f"[{msg.id}] {msg.author.display_name}: {(msg.content or '')[:150]}")
                    recent.reverse()
                    channel_context["recent_messages"] = recent
                except Exception:
                    pass
            else:
                ch = message.channel
                channel_context = {
                    "guild_id": str(resolved_guild_id),
                    "channel_id": str(ch.id),
                    "channel_name": getattr(ch, 'name', 'unknown'),
                }
                # If it's a thread, include parent info
                if isinstance(ch, discord.Thread) and ch.parent:
                    channel_context["is_thread"] = True
                    channel_context["parent_channel_id"] = str(ch.parent_id)
                    channel_context["parent_channel_name"] = ch.parent.name

                # If replying to a message, include it
                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message):
                        channel_context["replied_to"] = {
                            "message_id": str(ref.id),
                            "author": ref.author.display_name,
                            "content": (ref.content or '')[:500],
                        }
                        channel_context["replied_to_anchor_note"] = "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."

                # Grab recent messages for surrounding context
                try:
                    recent = []
                    async for msg in ch.history(limit=10):
                        if msg.id == message.id:
                            continue
                        recent.append(f"[{msg.id}] {msg.author.display_name}: {(msg.content or '')[:150]}")
                    recent.reverse()
                    channel_context["recent_messages"] = recent
                except Exception:
                    pass

            # Mark busy and run agent
            self._busy[user_id] = True
            try:
                responses = await self.agent.chat(
                    user_id=user_id,
                    user_message=content,
                    channel_context=channel_context,
                    channel=message.channel,
                    is_admin=is_admin,
                    requester_id=None if is_admin else user_id,
                )
            finally:
                self._busy[user_id] = False

            # responses is a list of messages, or None if ended without reply
            if responses is None:
                logger.info("[AdminChat] Turn ended without reply (silent action)")
                return

            # Send each response message
            total_chars = 0
            messages_sent = 0

            # In public channels, reply to the original message for the first response
            reply_ref = message if not is_dm else None

            async def _send_with_retry(channel, content_to_send: str, reference=None):
                backoffs = (0.5, 1.5)
                for attempt in range(len(backoffs) + 1):
                    try:
                        return await channel.send(content_to_send, reference=reference)
                    except discord.HTTPException as exc:
                        if exc.status < 500 or attempt == len(backoffs):
                            raise
                        await asyncio.sleep(backoffs[attempt])

            for response in responses:
                # Skip empty responses
                if not response or not response.strip():
                    continue

                # Split on ---SPLIT--- marker for proper media embedding
                # Each part becomes a separate Discord message
                parts = response.split('\n---SPLIT---\n')

                for part in parts:
                    part = part.strip()
                    if not part:
                        continue

                    total_chars += len(part)

                    # Handle long messages by splitting
                    if len(part) <= 2000:
                        await _send_with_retry(message.channel, part, reference=reply_ref)
                        messages_sent += 1
                    else:
                        # Split into chunks
                        chunks = [part[i:i+1990] for i in range(0, len(part), 1990)]
                        for chunk in chunks:
                            if chunk.strip():
                                await _send_with_retry(message.channel, chunk, reference=reply_ref)
                                messages_sent += 1

                    # Only reply-thread the first message
                    reply_ref = None

            logger.info(f"[AdminChat] Sent {messages_sent} message(s) ({total_chars} chars total)")

        except Exception:
            logger.exception("[AdminChat] Error processing message")
            try:
                await message.channel.send("Sorry, something went wrong on my side. Try again in a moment.")
            except Exception:
                logger.exception("[AdminChat] Failed to send neutral error message")

        # Process any message that arrived while we were busy
        pending = self._pending_messages.pop(user_id, None)
        if pending:
            logger.info(f"[AdminChat] Processing queued message from {user_id}")
            await self.on_message(pending)
    
    @commands.command(name='adminchat_clear')
    @commands.is_owner()
    async def clear_history(self, ctx: commands.Context):
        """Clear the admin chat conversation history."""
        if self.agent:
            self.agent.clear_conversation(ctx.author.id)
            await ctx.send("Conversation history cleared.")
        else:
            await ctx.send("Agent not initialized.")


async def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    # These will be passed from main.py
    db_handler = getattr(bot, 'db_handler', None)
    sharer = getattr(bot, 'sharer', None)
    
    if db_handler is None or sharer is None:
        logger.error("[AdminChat] Cannot setup cog - db_handler or sharer not found on bot")
        return
    
    await bot.add_cog(AdminChatCog(bot, db_handler, sharer))
    logger.info("[AdminChat] Cog loaded")
