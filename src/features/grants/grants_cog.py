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

        admin_id = os.getenv('ADMIN_USER_ID')
        self.admin_id = int(admin_id) if admin_id else None

        self.configured = all([self.grants_channel_id, self.db, self.claude_client])
        if not self.configured:
            logger.warning("GrantsCog: missing config, handlers will no-op")

        # Thread names the bot should ignore (guide + questions)
        self._ignored_thread_names = {"How Micro-Grants Work", "Questions & Discussion"}

        # Forum tags — populated on_ready once we can read the forum channel
        self._tags: dict[str, discord.ForumTag] = {}

        # In-memory guard against concurrent processing of the same thread
        self._processing_threads: set[int] = set()

    @property
    def _admin_mention(self) -> str:
        return f"<@{self.admin_id}>" if self.admin_id else "@admin"

    # ========== Startup Scan ==========

    @commands.Cog.listener()
    async def on_ready(self):
        """Load forum tags and scan for unprocessed threads."""
        if not self.configured:
            return
        try:
            await self._load_forum_tags()
            await self._scan_missed_threads()
            # Run payment recovery as a background task — it makes HTTP calls
            # that can get cancelled if they block the on_ready handler too long
            self.bot.loop.create_task(self._recover_inflight_payments())
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
        """On startup, recover in-flight payments and retry failed ones."""
        if not self.solana_client:
            return

        inflight = self.db.get_inflight_payments()
        for grant in inflight:
            thread_id = grant['thread_id']
            tx_sig = grant.get('tx_signature')
            payment_status = grant.get('payment_status')
            wallet = grant.get('wallet_address')

            # Resolve the thread for messaging
            thread = None
            try:
                for guild in self.bot.guilds:
                    forum = guild.get_channel(self.grants_channel_id)
                    if forum:
                        thread = forum.get_thread(thread_id)
                        break
            except Exception:
                pass

            if payment_status == 'retry':
                # Previous payment failed — retry with stored wallet
                if not wallet:
                    logger.warning(f"GrantsCog: grant {thread_id} needs retry but no wallet stored")
                    continue
                if not thread:
                    logger.warning(f"GrantsCog: can't retry payment for thread {thread_id} — thread not found")
                    continue

                # Retry up to 3 times with increasing delays
                max_retries = 3
                succeeded = False
                for retry in range(max_retries):
                    logger.info(f"GrantsCog: payment attempt {retry + 1}/{max_retries} for thread {thread_id} to {wallet}")
                    try:
                        await self._process_payment(thread, grant, wallet)
                        succeeded = True
                        break
                    except Exception as e:
                        logger.warning(f"GrantsCog: payment attempt {retry + 1}/{max_retries} failed for thread {thread_id}: {e}")
                        if retry < max_retries - 1:
                            await asyncio.sleep(5 * (retry + 1))  # 5s, 10s

                if not succeeded:
                    logger.error(f"GrantsCog: all {max_retries} payment attempts failed for thread {thread_id}")
                    await thread.send(
                        f"Payment failed after {max_retries} attempts.\n\n"
                        f"{self._admin_mention} will follow up."
                    )
                continue

            if not tx_sig:
                # Was 'sending' but never got a tx — mark for retry
                logger.warning(f"GrantsCog: grant {thread_id} stuck in '{payment_status}' with no tx")
                if wallet:
                    self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='retry')
                else:
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
                if thread:
                    try:
                        await thread.send(
                            f"**Payment confirmed!** (recovered on restart)\n\n"
                            f"- Transaction: [View on Explorer]({self._explorer_url(tx_sig)})"
                        )
                    except Exception:
                        pass

            elif status == 'failed':
                logger.warning(f"GrantsCog: tx {tx_sig} for thread {thread_id} failed on-chain")
                if wallet:
                    self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='retry',
                                                tx_signature=None)
                else:
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
                await thread.send(f"An error occurred while reviewing this application. {self._admin_mention} will follow up.")
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
                await channel.send(f"An error occurred while re-reviewing. {self._admin_mention} will follow up.")
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
                # Check current state — don't mark 'failed' if money may be in flight.
                # Only mark failed for definitive pre-send errors (bad address, insufficient
                # balance). Transient errors (429, timeout, network) leave payment_status
                # as-is so startup recovery can verify on-chain.
                current = self.db.get_grant_by_thread(thread_id)
                ps = current.get('payment_status', 'none') if current else 'none'
                if ps in ('sent', 'confirmed'):
                    # Money may be on-chain — don't touch, recovery will handle it
                    await channel.send(
                        f"Payment was submitted but encountered an error: {e}\n\n"
                        f"The transaction may still confirm — {self._admin_mention} will verify."
                    )
                else:
                    # Payment never landed — mark for auto-retry on next startup
                    self.db.update_grant_status(thread_id, 'awaiting_wallet', payment_status='retry')
                    await channel.send(
                        f"Payment encountered an error: {e}\n\n"
                        f"It will be retried automatically shortly."
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

        # Fetch grant history and engagement data for this applicant
        grant_history = self.db.get_grant_history_for_applicant(applicant_id)
        # Exclude the current application we just created
        grant_history = [g for g in grant_history if g.get('status') != 'reviewing' or g.get('thread_id') != thread_id]
        engagement = self.db.get_member_engagement(applicant_id)

        # Assess with LLM
        try:
            assessment = await assess_application(
                self.claude_client, thread_content,
                grant_history=grant_history or None,
                engagement=engagement,
            )
        except RuntimeError as e:
            logger.error(f"GrantsCog: assessment failed for thread {thread_id}: {e}")
            await thread.send(f"Unable to process this application right now. {self._admin_mention} will review it manually.")
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

        # Fetch grant history and engagement
        grant_history = self.db.get_grant_history_for_applicant(applicant_id)
        grant_history = [g for g in grant_history if g.get('status') not in ('reviewing', 'needs_info')]
        engagement = self.db.get_member_engagement(applicant_id)

        try:
            assessment = await assess_application(
                self.claude_client, thread_content,
                grant_history=grant_history or None,
                engagement=engagement,
            )
        except RuntimeError as e:
            logger.error(f"GrantsCog: re-assessment failed for thread {thread_id}: {e}")
            await thread.send(f"Unable to re-review right now. {self._admin_mention} will follow up.")
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

        elif decision == 'needs_review':
            self.db.update_grant_status(thread_id, 'needs_review', llm_assessment=llm_assessment)
            # Include LLM's recommended GPU/hours if provided
            details = ""
            if assessment.get('gpu_type') and assessment.get('recommended_hours'):
                gpu_type = assessment['gpu_type']
                hours = assessment['recommended_hours']
                cost = calculate_grant_cost(gpu_type, hours)
                details = (
                    f"\n\n**LLM recommendation (if approved):** "
                    f"{gpu_type.replace('_', ' ')} / {hours}hrs / ${cost:.2f}"
                )
            await thread.send(
                f"<@{thread.owner_id}> {response}\n\n"
                f"If you have any additional links, examples, or details that would strengthen "
                f"your application, please share them here.\n\n"
                f"{self._admin_mention} **Manual review needed**{details}\n"
                f"**Reasoning:** {reasoning}"
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
            await thread.send(f"Payment system is not configured. {self._admin_mention} will process this manually.")
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
                f"{self._admin_mention} will verify and follow up."
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
