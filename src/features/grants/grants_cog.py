"""Cog for compute micro-grants: forum post → LLM review → SOL payment."""

import asyncio
import json
import logging
import os

import discord
from discord.ext import commands

from src.features.grants.assessor import assess_application
from src.features.grants.pricing import GPU_RATES, FEE_MULTIPLIER, calculate_grant_cost, get_sol_price_usd, usd_to_sol
from src.features.grants.solana_client import SolanaClient, is_valid_solana_address

logger = logging.getLogger('DiscordBot')


class GrantsCog(commands.Cog):
    """Micro-grants: applicants post in forum → Claude reviews → SOL payment."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)
        self.claude_client = getattr(bot, 'claude_client', None)
        self.storage = getattr(self.db, 'storage_handler', None) if self.db else None

        channel_id = os.getenv('GRANTS_CHANNEL_ID')
        self.grants_channel_id = int(channel_id) if channel_id else None

        self.solana_client = None
        try:
            self.solana_client = SolanaClient()
        except Exception as e:
            logger.warning(f"GrantsCog: Solana client init failed (payments disabled): {e}")

        self.configured = all([self.grants_channel_id, self.db, self.claude_client])
        if not self.configured:
            logger.warning("GrantsCog: missing config, handlers will no-op")

        # Thread names the bot should ignore (guide + questions)
        self._ignored_thread_names = {"How Micro-Grants Work", "Questions & Discussion"}

        # Forum tags — populated on_ready once we can read the forum channel
        self._tags: dict[str, discord.ForumTag] = {}

        # In-memory guard against concurrent processing of the same thread
        self._processing_threads: set[int] = set()

    # ========== Startup Scan ==========

    @commands.Cog.listener()
    async def on_ready(self):
        """Load forum tags and scan for unprocessed threads."""
        if not self.configured:
            return
        try:
            await self._load_forum_tags()
            await self._recover_inflight_payments()
            await self._scan_missed_threads()
        except Exception as e:
            logger.error(f"GrantsCog: startup failed: {e}", exc_info=True)

    async def _load_forum_tags(self):
        """Cache forum tags by name for applying to threads."""
        for guild in self.bot.guilds:
            forum = guild.get_channel(self.grants_channel_id)
            if forum and isinstance(forum, discord.ForumChannel):
                for tag in forum.available_tags:
                    self._tags[tag.name.lower()] = tag
                logger.info(f"GrantsCog: loaded {len(self._tags)} forum tags: {list(self._tags.keys())}")
                return

    async def _recover_inflight_payments(self):
        """On startup, check any payments that were sent but not confirmed."""
        if not self.solana_client:
            return

        inflight = self.db.get_inflight_payments()
        for grant in inflight:
            thread_id = grant['thread_id']
            tx_sig = grant.get('tx_signature')
            payment_status = grant.get('payment_status')

            if not tx_sig:
                # Was 'sending' but never got a tx — reset to awaiting_wallet
                logger.warning(f"GrantsCog: grant {thread_id} stuck in '{payment_status}' with no tx, resetting")
                self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='none')
                continue

            # Has a tx signature — check on-chain
            try:
                status = await self.solana_client.check_tx_status(tx_sig)
            except Exception as e:
                logger.error(f"GrantsCog: failed to check tx {tx_sig} for thread {thread_id}: {e}")
                continue

            if status == 'confirmed':
                sol_amount = float(grant.get('sol_amount') or 0)
                sol_price = float(grant.get('sol_price_usd') or 0)
                self.db.record_grant_payment(thread_id, tx_sig, sol_amount, sol_price)
                logger.info(f"GrantsCog: recovered confirmed payment {tx_sig} for thread {thread_id}")

                # Notify in thread
                try:
                    for guild in self.bot.guilds:
                        forum = guild.get_channel(self.grants_channel_id)
                        if forum:
                            thread = forum.get_thread(thread_id)
                            if thread:
                                await thread.send(
                                    f"**Payment confirmed!** (recovered on restart)\n\n"
                                    f"- Transaction: [View on Explorer]({self._explorer_url(tx_sig)})"
                                )
                            break
                except Exception:
                    pass

            elif status == 'failed':
                logger.warning(f"GrantsCog: tx {tx_sig} for thread {thread_id} failed on-chain, resetting")
                self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='none',
                                            tx_signature=None)
            else:
                # not_found — could be expired or still propagating, leave as-is for next restart
                logger.info(f"GrantsCog: tx {tx_sig} for thread {thread_id} not found on-chain, will retry next startup")

    async def _scan_missed_threads(self):
        """Find forum threads with no DB record and process them."""
        guild = None
        for g in self.bot.guilds:
            channel = g.get_channel(self.grants_channel_id)
            if channel:
                guild = g
                break
        if not guild:
            return

        forum = guild.get_channel(self.grants_channel_id)
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        # Get all active (non-archived) threads
        threads = forum.threads
        # Also fetch archived threads that might have been missed
        archived = []
        async for thread in forum.archived_threads(limit=50):
            archived.append(thread)

        all_threads = list(threads) + archived
        processed = 0

        for thread in all_threads:
            # Skip guide/questions threads
            if thread.name in self._ignored_thread_names:
                continue

            # If already in DB, backfill missing attachments/avatar then skip
            existing = self.db.get_grant_by_thread(thread.id)
            if existing:
                await self._backfill_media(thread, existing)
                continue

            # Skip locked threads (already handled manually)
            if thread.locked:
                continue

            logger.info(f"GrantsCog: found missed thread {thread.id} ({thread.name}), processing...")
            if thread.id in self._processing_threads:
                continue
            self._processing_threads.add(thread.id)
            try:
                await self._process_new_application(thread)
                processed += 1
            except Exception as e:
                logger.error(f"GrantsCog: error processing missed thread {thread.id}: {e}", exc_info=True)
            finally:
                self._processing_threads.discard(thread.id)

        if processed:
            logger.info(f"GrantsCog: processed {processed} missed thread(s) on startup")

    # ========== Listeners ==========

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Handle new forum posts in the grants channel."""
        if not self.configured:
            return
        if not thread.parent_id or thread.parent_id != self.grants_channel_id:
            return
        if thread.name in self._ignored_thread_names:
            return

        thread_id = thread.id

        # Race condition guard
        if thread_id in self._processing_threads:
            return
        self._processing_threads.add(thread_id)

        try:
            await self._process_new_application(thread)
        except Exception as e:
            logger.error(f"GrantsCog: error processing thread {thread_id}: {e}", exc_info=True)
            try:
                await thread.send("An error occurred while reviewing this application. A team member will follow up.")
            except Exception:
                pass
        finally:
            self._processing_threads.discard(thread_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle replies in grant threads (follow-up info or wallet address)."""
        if not self.configured:
            return
        if message.author.bot or not message.guild:
            return

        # Must be in a thread under the grants channel
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        if not channel.parent_id or channel.parent_id != self.grants_channel_id:
            return

        thread_id = channel.id
        grant = self.db.get_grant_by_thread(thread_id)
        if not grant:
            return

        # Only respond to the original applicant
        if message.author.id != grant['applicant_id']:
            return

        if grant['status'] == 'needs_info':
            # Re-assess with full conversation
            if thread_id in self._processing_threads:
                return
            self._processing_threads.add(thread_id)
            try:
                await self._reassess_application(channel, grant)
            except Exception as e:
                logger.error(f"GrantsCog: re-assessment error for thread {thread_id}: {e}", exc_info=True)
                await channel.send("An error occurred while re-reviewing. A team member will follow up.")
            finally:
                self._processing_threads.discard(thread_id)

        elif grant['status'] == 'awaiting_wallet':
            # Extract wallet address from message
            wallet = message.content.strip()
            if not is_valid_solana_address(wallet):
                await message.reply("That doesn't look like a valid Solana wallet address. Please send a valid base58-encoded address.")
                return

            # Race condition guard
            if thread_id in self._processing_threads:
                return
            self._processing_threads.add(thread_id)

            try:
                await self._process_payment(channel, grant, wallet)
            except Exception as e:
                logger.error(f"GrantsCog: payment error for thread {thread_id}: {e}", exc_info=True)
                # Status stays 'awaiting_wallet' — the application is still approved,
                # only the payment failed. They can re-submit their wallet to retry.
                current = self.db.get_grant_by_thread(thread_id)
                if current and current.get('payment_status') not in ('sent', 'confirmed'):
                    self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='failed')
                await channel.send(
                    f"Payment failed: {e}\n\n"
                    f"Please re-send your Solana wallet address to try again, "
                    f"or a team member will follow up."
                )
            finally:
                self._processing_threads.discard(thread_id)

    # ========== Core Logic ==========

    async def _process_new_application(self, thread: discord.Thread):
        """Assess a new grant application and respond."""
        thread_id = thread.id
        applicant_id = thread.owner_id

        # Join the thread so the bot can post
        await thread.join()

        # Check for existing active grants
        active = self.db.get_active_grants_for_applicant(applicant_id)
        if active:
            await thread.send(
                "You already have an active grant application. "
                "Please wait for it to be completed before submitting a new one."
            )
            try:
                await thread.edit(archived=True)
            except Exception as e:
                logger.warning(f"GrantsCog: failed to archive duplicate thread {thread_id}: {e}")
            return

        # Fetch the starter message content (retry — on_thread_create can fire
        # before the starter message is available via the API)
        starter_message = None
        for attempt in range(4):
            try:
                starter_message = await thread.fetch_message(thread_id)
                break
            except discord.NotFound:
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                else:
                    raise
        thread_content = f"**{thread.name}**\n\n{starter_message.content}"

        # Upload attachments to Supabase storage
        attachment_urls = await self._upload_attachments(thread_id, starter_message)

        # Upload applicant's avatar to permanent storage
        await self._upload_avatar(thread)

        # Record in DB
        self.db.create_grant_application(thread_id, applicant_id, thread_content, attachment_urls=attachment_urls)

        # Fetch grant history for this applicant
        grant_history = self.db.get_grant_history_for_applicant(applicant_id)
        # Exclude the current application we just created
        grant_history = [g for g in grant_history if g.get('status') != 'reviewing' or g.get('thread_id') != thread_id]

        # Assess with LLM
        try:
            assessment = await assess_application(self.claude_client, thread_content, grant_history=grant_history or None)
        except RuntimeError as e:
            logger.error(f"GrantsCog: assessment failed for thread {thread_id}: {e}")
            await thread.send("Unable to process this application right now. A team member will review it manually.")
            return

        await self._handle_assessment(thread, assessment)

    async def _reassess_application(self, thread: discord.Thread, grant: dict):
        """Re-assess after applicant provides more info."""
        thread_id = thread.id
        applicant_id = grant['applicant_id']

        # Gather full conversation: starter message + all follow-up messages
        messages = []
        async for msg in thread.history(limit=100, oldest_first=True):
            if msg.author.bot:
                messages.append(f"[Reviewer]: {msg.content}")
            else:
                messages.append(f"[Applicant]: {msg.content}")

        thread_content = f"**{thread.name}**\n\n" + "\n\n".join(messages)

        # Update stored content
        self.db.update_grant_status(thread_id, 'reviewing', thread_content=thread_content)

        # Fetch grant history
        grant_history = self.db.get_grant_history_for_applicant(applicant_id)
        grant_history = [g for g in grant_history if g.get('status') not in ('reviewing', 'needs_info')]

        try:
            assessment = await assess_application(self.claude_client, thread_content, grant_history=grant_history or None)
        except RuntimeError as e:
            logger.error(f"GrantsCog: re-assessment failed for thread {thread_id}: {e}")
            await thread.send("Unable to re-review right now. A team member will follow up.")
            self.db.update_grant_status(thread_id, 'needs_info')
            return

        await self._handle_assessment(thread, assessment)

    async def _backfill_media(self, thread: discord.Thread, grant: dict):
        """Backfill attachments and avatar for an existing grant if missing."""
        needs_attachments = not grant.get('attachment_urls')
        member = self.db.get_member(thread.owner_id)
        needs_avatar = not (member and member.get('stored_avatar_url'))

        if not needs_attachments and not needs_avatar:
            return

        try:
            if needs_avatar:
                await self._upload_avatar(thread)

            if needs_attachments:
                starter_message = await thread.fetch_message(thread.id)
                if starter_message.attachments:
                    urls = await self._upload_attachments(thread.id, starter_message)
                    if urls:
                        self.db.update_grant_status(thread.id, grant['status'], attachment_urls=urls)
                        logger.info(f"GrantsCog: backfilled {len(urls)} attachment(s) for thread {thread.id}")
        except Exception as e:
            logger.warning(f"GrantsCog: backfill failed for thread {thread.id}: {e}")

    async def _upload_attachments(self, thread_id: int, message: discord.Message) -> list:
        """Download message attachments and upload to Supabase storage.

        Returns a list of dicts with 'filename' and 'url' for each uploaded file.
        """
        if not message.attachments or not self.storage:
            return []

        uploaded = []
        for att in message.attachments:
            storage_path = f"grants/{thread_id}/{att.filename}"
            try:
                url = await self.storage.download_and_upload_url(att.url, storage_path)
                if url:
                    uploaded.append({'filename': att.filename, 'url': url})
                    logger.info(f"GrantsCog: uploaded attachment {att.filename} for thread {thread_id}")
            except Exception as e:
                logger.warning(f"GrantsCog: failed to upload attachment {att.filename}: {e}")
        return uploaded

    async def _upload_avatar(self, thread: discord.Thread):
        """Upload the applicant's Discord avatar to permanent storage."""
        if not self.storage:
            return
        try:
            member = thread.guild.get_member(thread.owner_id)
            if not member or not member.display_avatar:
                return
            avatar_url = str(member.display_avatar.with_size(256).with_format('png'))
            storage_path = f"avatars/{member.id}.png"
            stored_url = await self.storage.download_and_upload_url(avatar_url, storage_path)
            if stored_url:
                self.db.update_member_stored_avatar(member.id, stored_url)
                logger.info(f"GrantsCog: uploaded avatar for member {member.id}")
        except Exception as e:
            logger.warning(f"GrantsCog: failed to upload avatar for thread owner {thread.owner_id}: {e}")

    async def _apply_tag(self, thread: discord.Thread, tag_name: str):
        """Apply a forum tag to a thread if the tag exists."""
        tag = self._tags.get(tag_name.lower())
        if tag:
            try:
                await thread.edit(applied_tags=[tag])
            except Exception as e:
                logger.warning(f"GrantsCog: failed to apply tag '{tag_name}' to thread {thread.id}: {e}")

    async def _handle_assessment(self, thread: discord.Thread, assessment: dict):
        """Handle an LLM assessment result — update DB and reply."""
        thread_id = thread.id
        decision = assessment['decision']
        response = assessment['response']
        reasoning = assessment['reasoning']
        # Store both reasoning and response in llm_assessment
        llm_assessment = json.dumps({'reasoning': reasoning, 'response': response})

        if decision == 'spam':
            self.db.update_grant_status(thread_id, 'rejected', llm_assessment=llm_assessment,
                                        rejected_at='now()')
            logger.info(f"GrantsCog: deleting spam thread {thread_id}: {reasoning[:100]}")
            try:
                await thread.delete()
            except Exception as e:
                logger.warning(f"GrantsCog: failed to delete spam thread {thread_id}: {e}")
            return

        if decision == 'needs_info':
            self.db.update_grant_status(thread_id, 'needs_info', llm_assessment=llm_assessment)
            await thread.send(
                f"**More information needed**\n\n{response}\n\n"
                f"Please reply here with the requested details and I'll re-review your application."
            )

        elif decision == 'rejected':
            self.db.update_grant_status(thread_id, 'rejected', llm_assessment=llm_assessment,
                                        rejected_at='now()')
            await thread.send(f"**Application not approved**\n\n{response}")
            try:
                await thread.edit(archived=True)
            except Exception as e:
                logger.warning(f"GrantsCog: failed to archive rejected thread {thread_id}: {e}")

        elif decision == 'approved':
            gpu_type = assessment['gpu_type']
            hours = assessment['recommended_hours']
            rate = GPU_RATES[gpu_type]
            total_cost = calculate_grant_cost(gpu_type, hours)

            self.db.update_grant_status(
                thread_id, 'awaiting_wallet',
                llm_assessment=llm_assessment,
                gpu_type=gpu_type,
                recommended_hours=hours,
                gpu_rate_usd=rate,
                total_cost_usd=total_cost,
                approved_at='now()',
            )

            await self._apply_tag(thread, 'accepted')
            await thread.send(
                f"<@{thread.owner_id}> **Grant Approved!**\n\n"
                f"{response}\n\n"
                f"**Grant Details:**\n"
                f"- GPU: {gpu_type.replace('_', ' ')}\n"
                f"- Hours: {hours}\n"
                f"- Rate: ${rate:.2f}/hr (+ 10% fee buffer)\n"
                f"- Total: ${total_cost:.2f} USD (paid in SOL)\n\n"
                f"Please reply with your **Solana wallet address** to receive the grant."
            )

    def _explorer_url(self, tx_sig: str) -> str:
        """Build a Solana Explorer URL for a transaction."""
        rpc_url = self.solana_client.rpc_url
        if 'devnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=devnet"
        elif 'testnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=testnet"
        return f"https://explorer.solana.com/tx/{tx_sig}"

    async def _process_payment(self, thread: discord.Thread, grant: dict, wallet: str):
        """Send SOL payment and finalize the grant.

        Payment flow is idempotent — if a previous tx was sent but not confirmed,
        we check its on-chain status before re-sending.
        """
        thread_id = thread.id

        if not self.solana_client:
            await thread.send("Payment system is not configured. A team member will process this manually.")
            return

        # Guard: if we already sent a tx, check its status instead of re-sending
        existing_tx = grant.get('tx_signature')
        if existing_tx and grant.get('payment_status') in ('sent', 'sending'):
            logger.info(f"GrantsCog: found existing tx {existing_tx} for thread {thread_id}, checking status...")
            tx_status = await self.solana_client.check_tx_status(existing_tx)
            if tx_status == 'confirmed':
                # Already paid — record and notify
                sol_amount = float(grant.get('sol_amount', 0))
                sol_price = float(grant.get('sol_price_usd', 0))
                self.db.record_grant_payment(thread_id, existing_tx, sol_amount, sol_price)
                await thread.send(
                    f"**Payment confirmed!** (recovered from previous attempt)\n\n"
                    f"- Transaction: [View on Explorer]({self._explorer_url(existing_tx)})"
                )
                try:
                    await thread.edit(archived=True)
                except Exception:
                    pass
                return
            elif tx_status == 'failed':
                logger.warning(f"GrantsCog: previous tx {existing_tx} failed on-chain, re-sending...")
            else:
                # not_found — tx may still be propagating or expired, safe to retry
                logger.warning(f"GrantsCog: previous tx {existing_tx} not found on-chain, re-sending...")

        # Fetch current SOL price
        sol_price = await get_sol_price_usd()
        total_usd = float(grant['total_cost_usd'])
        sol_amount = usd_to_sol(total_usd, sol_price)

        # Mark as sending with wallet + amounts BEFORE sending money
        self.db.update_grant_status(
            thread_id, 'awaiting_wallet',
            payment_status='sending',
            wallet_address=wallet,
            sol_amount=sol_amount,
            sol_price_usd=sol_price,
        )

        await self._apply_tag(thread, 'in progress')
        await thread.send(
            f"Processing payment of **{sol_amount:.4f} SOL** (~${total_usd:.2f} at ${sol_price:.2f}/SOL)..."
        )

        # Send SOL — returns signature immediately (before confirmation)
        tx_sig = await self.solana_client.send_sol(wallet, sol_amount)

        # Record signature immediately so we can recover if confirmation fails
        self.db.update_grant_status(
            thread_id, 'awaiting_wallet',
            payment_status='sent',
            tx_signature=tx_sig,
        )

        # Now wait for confirmation
        try:
            await self.solana_client.confirm_tx(tx_sig)
        except Exception as e:
            logger.error(f"GrantsCog: tx {tx_sig} sent but confirmation failed: {e}", exc_info=True)
            await thread.send(
                f"Payment was submitted but confirmation timed out. "
                f"Transaction: [View on Explorer]({self._explorer_url(tx_sig)})\n\n"
                f"A team member will verify and follow up."
            )
            return

        # Fully confirmed — finalize
        self.db.record_grant_payment(thread_id, tx_sig, sol_amount, sol_price)

        await thread.send(
            f"**Payment sent!**\n\n"
            f"- Amount: {sol_amount:.4f} SOL (~${total_usd:.2f})\n"
            f"- Wallet: `{wallet}`\n"
            f"- Transaction: [View on Explorer]({self._explorer_url(tx_sig)})\n\n"
            f"Your compute grant for {grant['gpu_type'].replace('_', ' ')} "
            f"({grant['recommended_hours']}hrs) has been funded. Good luck with your project!"
        )

        try:
            await thread.edit(archived=True)
        except Exception as e:
            logger.warning(f"GrantsCog: failed to archive paid thread {thread_id}: {e}")
