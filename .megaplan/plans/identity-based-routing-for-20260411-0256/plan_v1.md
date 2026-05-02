# Implementation Plan: Identity-Based Admin Chat Routing + Admin-DM Approval Gate

## Overview
Refactor `admin_chat` from content-based message classification to a strict identity-based router with three branches: admin → agent (with new tools), pending recipient → deterministic state machine (no LLM), else → fallthrough. Add a new `awaiting_test_receipt_confirmation` state that gates `wallet_registry.verified_at` on a recipient text-ack (not on-chain alone). Introduce an admin-DM-only `AdminApprovalView` that becomes the sole real-payment gate for `producer='admin_chat'` (grants remain on `PaymentConfirmView`). Add `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, and `upsert_wallet_for_user` tools. Auto-skip the test phase when a verified wallet is already on file. Add a 24h timeout sweep. Grants + all hardening/cleanup/polish invariants stay intact.

Concrete evidence from the repo (verified during planning):
- Content-based interceptor lives at `src/features/admin_chat/admin_chat_cog.py:563-640` (`_check_pending_payment_reply`) and is called from `on_message` at `src/features/admin_chat/admin_chat_cog.py:794`. This is the code that must be deleted and replaced by the identity router.
- Current status CHECK at `sql/admin_payment_intents.sql:28-37` lacks the two new states.
- `_start_admin_payment_flow` at `src/features/admin_chat/admin_chat_cog.py:234-284` auto-confirms the test via `PaymentActorKind.AUTO`; terminal test-success branch at `:311-414` inlines final payment creation + calls `payment_ui_cog.send_confirmation_request(final_payment['payment_id'])` at `:393` — this call site is the one to remove. `grants_cog.py:719` call must stay untouched.
- Per-guild reconciliation at `:546-561` only recognizes `awaiting_wallet`/`awaiting_test`/`awaiting_confirmation`/`confirmed` — must be extended.
- `PaymentConfirmView` + `_register_pending_confirmation_views` at `src/features/payments/payment_ui_cog.py:19-130` is the template for the new persistent `AdminApprovalView`.
- `initiate_payment` tool schema at `src/features/admin_chat/tools.py:579-600`; executor at `:2494-2595`. The wallet-injection guard pattern at `:2501-2508` is the template for batch/upsert. Auto-skip branch at `:2544-2574` already filters unverified wallets but still hands off to `_start_admin_payment_flow` which issues the test — that's wrong for the new flow and must branch to a new "real payment direct" path.
- `db_handler.create_admin_payment_intent` at `src/common/db_handler.py:1805-1819` inserts one row; batch creation needs a new method using Supabase `.insert([...])`.
- `is_valid_solana_address` at `src/features/grants/solana_client.py:31-39` — reuse, don't re-implement.
- `mark_wallet_verified` at `src/common/db_handler.py:1649` is the single write path for `verified_at`.

Scope boundaries per the brief constraints: do NOT touch `solana_client.py`/`solana_provider.py`, `grants_cog.py`, `PaymentService` rebroadcast/fee logic, hardening/cleanup/polish invariants. `MEMBER_SYSTEM_PROMPT` stays in place as dead-branch code (do not delete).

## Phase 1: Schema + DB Helpers

### Step 1: Extend `admin_payment_intents` state enum + add columns (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Add** the two new status values to the CHECK constraint via an idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ...` block so the migration is safely re-runnable. New allowed statuses: `awaiting_wallet`, `awaiting_test`, `awaiting_test_receipt_confirmation` (NEW), `awaiting_admin_approval` (NEW), `awaiting_confirmation` (kept for back-compat of any in-flight rows), `confirmed`, `completed`, `failed`, `cancelled`, `manual_review` (NEW — negative/timeout escalation target).
2. **Add** `ambiguous_reply_count int not null default 0` column via `add column if not exists`. (Required for the two-strike ambiguous escalation; `admin_payment_intents` has no JSONB metadata column today, so a dedicated integer is simplest.)
3. **Reuse** the existing `prompt_message_id bigint` column for both the wallet prompt and the receipt-confirmation prompt — only the most recent one matters for safe-delete, and overwriting is fine. Add a short comment explaining the reuse.
4. **Verify** the unique partial index on `(guild_id, channel_id, recipient_user_id) WHERE status NOT IN ('completed','failed','cancelled')` at `sql/admin_payment_intents.sql:47-49` still covers the new non-terminal states (it does — the new states are not in the excluded set).

### Step 2: Add DB helpers (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `create_admin_payment_intents_batch(records: List[Dict], guild_id: int) -> Optional[List[Dict]]` using `self.supabase.table('admin_payment_intents').insert([...]).execute()`. PostgREST treats a multi-row insert as one atomic request — if any row fails DB validation, all are rejected. Return `None` on any exception (all-or-none).
2. **Add** `list_intents_by_status(guild_id: int, status: str) -> List[Dict]` (thin wrapper for the persistent-view registration pass in Phase 3).
3. **Add** `list_stale_test_receipt_intents(cutoff_iso: str) -> List[Dict]` — cross-guild query for the 24h timeout sweep. Filter `status='awaiting_test_receipt_confirmation' AND updated_at < cutoff`. Keep it cross-guild so one task loop serves all enabled guilds.
4. **Add** `increment_intent_ambiguous_reply_count(intent_id: str, guild_id: int) -> Optional[int]` — fetch current value, update to `current+1`, return new value. (One extra read is acceptable; the ambiguous escalation path runs at most twice per intent.)
5. **Verify** `upsert_wallet` at `src/common/db_handler.py:1469` already raises `WalletUpdateBlockedError` when an active intent exists — reuse as-is.

## Phase 2: Deterministic Parsers + Identity Router

### Step 3: Add deterministic parsers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `_parse_wallet_from_text(content: str) -> Optional[str]` — tokenizes the message and returns the first token where `is_valid_solana_address(token)` is true. Reuses the already-imported function at `src/features/grants/solana_client.py:31-39`. No regex magic beyond token splitting.
2. **Add** `_classify_confirmation(content: str) -> Literal['positive','negative','ambiguous']` with hardcoded keyword sets:
   - Positive: `{'confirmed','received','got it','yes','yep','confirm','👍'}`
   - Negative: `{'no','didnt',"didn't",'not received','missing','nothing'}`
   - Case-insensitive, whole-word boundary via a `\b` regex (emoji match via substring since `👍` is not word-boundary friendly), punctuation stripped. "ambiguous" = neither matched OR both matched.
3. **Keep** `_classify_payment_reply` (the Claude-based classifier at `:64-`) as dead code for now — do not delete in the same commit to keep diff reviewable. A later cleanup commit can remove it if truly unused.

### Step 4: Identity router (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Rewrite** `on_message` (`src/features/admin_chat/admin_chat_cog.py:790`) as a strict three-way switch on `message.author.id`:
   ```python
   if message.author.bot:
       return
   author_id = message.author.id
   if self._is_admin(author_id):
       await self._handle_admin_message(message)   # existing agent path extracted
       return
   if message.guild is not None:
       intent = self.db_handler.get_active_intent_for_recipient(
           message.guild.id, message.channel.id, author_id
       )
       if intent:
           await self._handle_pending_recipient_message(message, intent)
           return
   return  # fallthrough — other cogs handle
   ```
2. **Extract** the current admin agent path (`:797-end`) into `_handle_admin_message(message)` so the switch stays readable. Preserve all existing behavior: `_is_directed_at_bot` gating, abort handling, channel context, busy/pending queue, rate limiter.
3. **Delete** the `await self._check_pending_payment_reply(message)` call at `:794`. Leave `_check_pending_payment_reply` defined temporarily — the startup reconciler still calls it from `_reconcile_intent_history:532`; Step 9 updates the reconciler.
4. **Note in plan:** approved non-admin members (`_can_user_message_bot=true`) no longer reach the agent under identity routing. This is the intentional scoping reduction. `MEMBER_SYSTEM_PROMPT` stays in `agent.py` as dead code; do not delete.

### Step 5: State machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_pending_recipient_message(message, intent)` with three branches on `intent['status']`:
   - `awaiting_wallet` → call `_parse_wallet_from_text(message.content)`. If found → existing `_handle_wallet_received(message, intent, wallet)` path (unchanged). If not found → **silent return** (no reply, no admin tag, no state change — prevents wallet-prompt spam).
   - `awaiting_test_receipt_confirmation` → call `_classify_confirmation(message.content)`, dispatch to the new `_handle_test_receipt_*` helpers from Step 7.
   - Any other state (`awaiting_test`, `awaiting_admin_approval`, `confirmed`) → silent return.
2. **Reuse** the existing `_processing_intents` dedupe set at `:575-578` to prevent concurrent processing of the same intent.
3. **Assert** in a unit test that no Anthropic client is invoked on any `_handle_pending_recipient_message` path — patch `self._classifier_client` to `Mock(side_effect=AssertionError("no LLM"))`.

## Phase 3: Test-Receipt Flow + Admin Approval Gate

### Step 6: Rewire `handle_payment_result` test-success branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Replace** the test-success branch at `:332-414`. New behavior:
   - On `is_test=True` + `status='confirmed'`: transition intent to `awaiting_test_receipt_confirmation`, post a new prompt in the source channel: `"<@{recipient}> test payment sent. Please check your wallet and reply 'confirmed' once you see {amount} SOL land."`, store `prompt_message_id` (overwrites the wallet prompt id — that's fine).
   - DO NOT call `request_payment` for the real payment here.
   - DO NOT call `send_confirmation_request` here. Delete line `:393`.
   - Keep `_notify_intent_admin` with the "test payment confirmed" status update at `:347-355`.
2. **On** test-success + non-confirmed status: unchanged (`_notify_admin_review` + intent fail).

### Step 7: Test-receipt confirmation handlers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_test_receipt_positive(message, intent)`:
   - Resolve `wallet_id = intent['wallet_id']`; call `db_handler.mark_wallet_verified(wallet_id, guild_id=intent['guild_id'])` — this is the one and only place in the new flow where `verified_at` gets set.
   - Call `payment_service.request_payment(is_test=False, ...)` with the already-known recipient wallet, amount, channel destinations, and `metadata={'intent_id': intent_id}`.
   - On success, update intent to `status='awaiting_admin_approval'` + `final_payment_id=new_payment['payment_id']`, then call the new `PaymentUICog._send_admin_approval_dm(payment)` helper from Step 8.
   - Post a small ack in the thread: `"<@{recipient}> thanks — admin approval requested."`
2. **Add** `_handle_test_receipt_negative(message, intent)`:
   - Fetch `test_payment = db_handler.get_payment_request(intent['test_payment_id'])` for tx_signature + wallet address.
   - Build a full-context admin mention: `"{admin_mention} negative ack from <@{recipient}> on intent {intent_id}. test tx: {tx_sig}, wallet: {wallet}. Please review."` and post it in the intent channel.
   - Transition intent to `status='manual_review'`.
   - Also DM admin via existing `_notify_intent_admin`.
3. **Add** `_handle_test_receipt_ambiguous(message, intent)`:
   - Call `increment_intent_ambiguous_reply_count(intent_id, guild_id)`.
   - If new count == 1 → silent return (no state change, no reply).
   - If new count >= 2 → escalate via the same path as `_handle_test_receipt_negative` (post admin tag with context, transition to `manual_review`).

### Step 8: `AdminApprovalView` + admin DM helper (`src/features/payments/payment_ui_cog.py`)
**Scope:** Large
1. **Add** a new `AdminApprovalView(discord.ui.View)` class next to `PaymentConfirmView` at `src/features/payments/payment_ui_cog.py:19`. Pattern-match `PaymentConfirmView`:
   - `timeout=None`, `custom_id=f"payment_admin_approve:{payment_id}"` (verify by grep that this prefix does NOT collide with `payment_confirm:` at `:30`).
   - Single `Approve Payment` button.
   - Callback: defer ephemeral, verify `interaction.user.id == int(os.getenv('ADMIN_USER_ID'))` (defense-in-depth even though the button lives in a DM), fetch payment by id, call `payment_service.confirm_payment(payment_id, guild_id=payment['guild_id'], actor=PaymentActor(PaymentActorKind.ADMIN_CLICK, interaction.user.id))` — **note:** verify `PaymentActorKind.ADMIN_CLICK` exists or use an existing kind like `PaymentActorKind.AUTO` with a comment that admin manual approval is the new auditing signal. If it doesn't exist, add a new enum member in `payment_service.py` (one-line change, permitted by task scope since admin approval is a new actor kind).
   - On success: edit the original DM message content to "✅ approved"; send a follow-up DM "payment confirmed, sending"; post a reply in the intent's source thread `"Payment sent to <@{recipient}>"`; call `safe_delete_messages(channel, [prompt_message_id, ...stale], logger=logger)` to clear stale flow messages in the thread.
2. **Add** `PaymentUICog._send_admin_approval_dm(payment)` helper:
   - Build message: `"**Admin approval required**\n- Intent: `{intent_id}`\n- Recipient: <@{recipient}>\n- Amount: {amount:.4f} SOL\n- Wallet: `{redact_wallet(...)}`\n- Jump: {jump_link}"`.
   - Fetch admin user via `bot.fetch_user(int(os.getenv('ADMIN_USER_ID')))`.
   - `view = AdminApprovalView(self, payment_id); self.bot.add_view(view); await admin.send(content, view=view)`.
   - On `discord.Forbidden` / rate limit: log at ERROR level with the payment_id and intent_id, leave intent in `awaiting_admin_approval`, return `None` (fail-closed — no public channel fallback).
3. **Add** `PaymentUICog._register_pending_admin_approval_views()` and call it from `cog_load` alongside the existing `_register_pending_confirmation_views` (`:110-130`):
   - For each enabled guild, `db_handler.list_intents_by_status(guild_id, 'awaiting_admin_approval')`.
   - For each intent with a `final_payment_id`, `bot.add_view(AdminApprovalView(self, final_payment_id))` — keyed by payment_id, matching the custom_id.
4. **Gate** `send_confirmation_request`: do NOT modify `send_confirmation_request` itself (it stays producer-agnostic for grants). The call-site removal at `admin_chat_cog.py:393` from Step 6 is the sole gating mechanism. Grants at `src/features/grants/grants_cog.py:719` is untouched.

## Phase 4: New Agent Tools

### Step 9: Read-only query tools (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** three tool schemas to the `TOOLS` list near the existing `list_payments`/`get_payment_status` schemas at `src/features/admin_chat/tools.py:455-559`:
   - `query_payment_state(payment_id?: str, user_id?: str)` — exactly one of the two must be provided.
   - `query_wallet_state(user_id: str)` — required.
   - `list_recent_payments(producer?: str, user_id?: str, limit?: int=20)`.
2. **Add** executors in the same file:
   - `execute_query_payment_state` — thin wrapper over `db_handler.get_payment_request` (by id) or `db_handler.list_payment_requests(recipient_discord_id=user_id, limit=20)`. Apply existing `_redact_payment_row`.
   - `execute_query_wallet_state` — `db_handler.list_wallets(discord_user_id=user_id, guild_id=...)`. Apply `_redact_wallet_row`. Return verified_at/created_at fields unredacted.
   - `execute_list_recent_payments` — `db_handler.list_payment_requests(producer=..., recipient_discord_id=..., limit=min(limit, 100))` with `_redact_payment_row`.
3. **Wire** dispatch in the `execute_tool` switch near `:3184-3199`.
4. **Do not** accept LLM-supplied wallet strings anywhere — these are read-only by user_id. Add a unit test asserting that passing a `wallet_address` param to any query tool is silently ignored and logged (reuse the pop-and-warn pattern from `:2501-2508`).

### Step 10: `initiate_batch_payment` tool (`src/features/admin_chat/tools.py`)
**Scope:** Large
1. **Add** schema: `initiate_batch_payment(payments: List[{recipient_user_id: str, amount_sol: number, reason?: string}])`. Required: `payments` (1..20).
2. **Add** executor `execute_initiate_batch_payment(bot, db_handler, params)`:
   - Pop `wallet_address`/`recipient_wallet` from EACH payment dict using the same guard pattern as `:2501-2508`.
   - Validate every payment element before any DB write: `guild_id`, `source_channel_id`, `recipient_user_id > 0`, `amount_sol > 0`, no active duplicate (`get_active_intent_for_recipient`). On any failure, return `{success: False, error: ..., index: N}` with **zero** DB writes.
   - Build the full list of records (identical shape to the single-intent executor, including `status='awaiting_test' if verified else 'awaiting_wallet'` based on each recipient's wallet state).
   - Call `db_handler.create_admin_payment_intents_batch(records, guild_id)`. If `None` → all-or-none rollback path, return failure.
   - On success, **iterate the returned intents and fan out per-intent mechanics**: for each intent with a verified wallet, create the real payment directly and call `_send_admin_approval_dm` (Step 11 auto-skip helper); for each unverified, post the wallet prompt message in the source channel (matching `:2577-2591`). A 16-payment batch produces 16 admin DMs (for verified wallets) or 16 wallet prompts (for unverified).
3. **Wire** dispatch in `execute_tool`.

### Step 11: Auto-skip verified-wallet branch (`src/features/admin_chat/tools.py` + `admin_chat_cog.py`)
**Scope:** Medium
1. **Modify** `execute_initiate_payment` (`src/features/admin_chat/tools.py:2563-2574`):
   - Current code: when `wallet_record` (verified) exists, it calls `cog._start_admin_payment_flow` which creates a TEST payment. That's wrong for the new flow.
   - New behavior: when a verified wallet exists, directly call `payment_service.request_payment(is_test=False, ...)` with the verified wallet, transition the intent to `awaiting_admin_approval` with `final_payment_id=new_payment['payment_id']`, then call `bot.payment_ui_cog._send_admin_approval_dm(new_payment)`. Extract this into a new helper `_create_real_payment_and_gate(cog, channel, intent, wallet_record, amount_sol)` inside `admin_chat_cog.py` so both `execute_initiate_payment` and `execute_initiate_batch_payment` reuse it.
2. **Do not** modify `_start_admin_payment_flow` — it stays the test-only path for unverified wallets (but its test auto-confirm behavior is already correct for the "test then wait for receipt ack" flow). After the test lands, `handle_payment_result` routes it to the new `awaiting_test_receipt_confirmation` state from Step 6.

### Step 12: `upsert_wallet_for_user` tool (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schema: `upsert_wallet_for_user(user_id: str, wallet_address: str, chain?: string='solana', reason?: string)`. Required: `user_id`, `wallet_address`.
2. **Add** executor:
   - Admin-only check: the agent system only runs this tool when the author is ADMIN_USER_ID (enforced at cog level since only admin messages reach the agent under identity routing). As defense-in-depth, require a trusted `admin_user_id` injected into `trusted_tool_input` by the cog and verify it matches `ADMIN_USER_ID` env.
   - Validate `wallet_address` via `is_valid_solana_address`.
   - Call `db_handler.upsert_wallet(guild_id, discord_user_id=user_id, chain='solana', address=wallet_address, metadata={...})`.
   - On `WalletUpdateBlockedError` → return `{success: False, error: "Active payment intent in flight — wallet change blocked"}` so the agent can surface it.
   - On success: verify `verified_at` is `NULL` (the row is newly set or the address changed — `upsert_wallet` does not preserve verified_at on address change; confirm this in the existing implementation at `src/common/db_handler.py:1469`). DM admin: `"wallet for <@{user_id}> set to {redact_wallet(address)}, will be verified on next payment"`.
3. **Wire** dispatch.

### Step 13: System prompt updates (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Add** five new tool bullet lines to the `**Doing things:**` section (after `get_payment_status` at `:77`): `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user` — each with one-line descriptions mirroring the existing style.
2. **Add** a new subsection after `**Use the right route tools.**` (around `:101`):
   ```
   **State questions → query first.** For any question about the state of a payment, wallet, or intent — call query_payment_state, query_wallet_state, or list_recent_payments FIRST. Do not claim state from memory or channel context. This is guidance, not enforcement, but operators notice hallucinations quickly.
   ```
3. **Add** `initiate_batch_payment` to `_CHANNEL_POSTING_TOOLS` at `src/features/admin_chat/agent.py:21-26` (same as `initiate_payment`) so the agent doesn't emit a duplicate "OK I'll do that" chat reply when batch is invoked.
4. **Do not** touch `MEMBER_SYSTEM_PROMPT` — leave as dead code.

## Phase 5: Reconciliation + 24h Timeout

### Step 14: Update startup reconciliation (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Extend** `_reconcile_active_intents` at `:546-561` to handle the new states:
   - `awaiting_test_receipt_confirmation` → re-scan channel history via `_reconcile_intent_history`, feed recipient messages to the new `_handle_pending_recipient_message` (synthetic replay) instead of the old `_check_pending_payment_reply`.
   - `awaiting_admin_approval` → no history scan needed; the `AdminApprovalView` persistence (Step 8.3) handles restart recovery. Optionally log a count for observability.
2. **Update** `_reconcile_intent_history` at `:498-544` to call `_handle_pending_recipient_message(message, refreshed_intent)` instead of `_check_pending_payment_reply(message)`. The synthetic replay needs the current intent state, so fetch it per-message.
3. **Once** the reconciler no longer calls `_check_pending_payment_reply`, it is safe to delete that method and `_classify_payment_reply` entirely. Prefer deleting in the same commit once no caller remains — grep to confirm.

### Step 15: 24-hour timeout sweep (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** a `@tasks.loop(minutes=15)` background task `_sweep_stale_test_receipts` on `AdminChatCog`. (Add to `AdminChatCog` rather than `PaymentWorkerCog` because it owns intent lifecycle.)
2. **Logic:** `cutoff = (datetime.now(tz=utc) - timedelta(hours=24)).isoformat()`; call `db_handler.list_stale_test_receipt_intents(cutoff)`; for each, transition to `manual_review` and DM admin via `_notify_intent_admin` with full context (intent_id, recipient, test_payment_id, tx signature if resolvable, wallet).
3. **Start** the task in `cog_load` after `_ensure_startup_reconciled`; stop in `cog_unload`. Wrap iteration in try/except so one bad row doesn't kill the loop.
4. **Interval** is 15 min (well above the ≥5 min constraint) to avoid DB hammering.

## Phase 6: Tests

### Step 16: State machine + router unit tests (`tests/test_admin_payments.py`)
**Scope:** Large
1. **Rewrite** broad sections of `tests/test_admin_payments.py`. Expected churn: tests mocking `_classify_payment_reply` must be removed/replaced.
2. **Add** fail_to_pass tests matching the brief's test expectation list verbatim. Specifically:
   - `test_identity_router_admin_to_agent` — author=ADMIN, message is a Solana-address-shaped string → `_handle_admin_message` invoked, agent path entered, no state machine invoked.
   - `test_identity_router_pending_recipient_to_state_machine` — author has active intent → state machine; assert `self._classifier_client.messages.create` is NOT called (or `_classifier_client = Mock(side_effect=AssertionError)`).
   - `test_identity_router_fallthrough` — neither admin nor pending → on_message returns without handling.
   - `test_state_machine_awaiting_wallet_valid_address` — `_handle_wallet_received` path triggers test payment.
   - `test_state_machine_awaiting_wallet_ignores_non_address` — silent return, intent unchanged, no reply sent.
   - `test_state_machine_confirmation_positive_keywords` — parametrized over the full keyword set; each triggers `mark_wallet_verified`, real payment creation, transition to `awaiting_admin_approval`, admin DM fired.
   - `test_state_machine_confirmation_negative_keywords` — parametrized; triggers admin tag + `manual_review` transition.
   - `test_state_machine_confirmation_ambiguous_first_then_escalate` — ambiguous #1 silent; ambiguous #2 escalates; ambiguous count persisted.
   - `test_auto_skip_verified_wallet_creates_real_payment_directly` — no `is_test=True` row created; real payment goes straight to `awaiting_admin_approval`.
   - `test_initiate_batch_payment_atomic_all_or_none` — batch with one invalid entry leaves zero intents in DB; valid batch creates N intents + N admin DMs.
   - `test_upsert_wallet_for_user_admin_only` — non-admin caller rejected; success creates wallet_registry row with `verified_at=NULL` and DMs admin.
   - `test_upsert_wallet_for_user_respects_wallet_update_blocked` — surfaces `WalletUpdateBlockedError`.
   - `test_admin_approval_view_only_admin_can_click` — non-admin interaction rejected ephemerally.
   - `test_admin_approval_view_persists_across_restart` — `PaymentUICog.cog_load` re-registers views for all `awaiting_admin_approval` intents.
   - `test_admin_approval_click_advances_payment` — DM edit, follow-up DM, thread post, `safe_delete_messages` call, `confirm_payment` invoked.
   - `test_admin_dm_fail_closed_on_forbidden` — `discord.Forbidden` on DM send → intent stays in `awaiting_admin_approval`, ERROR log emitted.
   - `test_payment_confirm_view_not_sent_for_admin_chat_real_payments` — assert `send_confirmation_request` is not called for producer='admin_chat' real payments; still called for grants.
   - `test_24h_timeout_sweep_escalates_stuck_test_receipt` — populate an intent with `updated_at < now - 24h`, run sweep once, assert transition + admin DM.
   - `test_query_payment_state_read_only`, `test_query_wallet_state_read_only`, `test_query_tools_reject_llm_injected_wallet`.
   - `test_approved_member_no_longer_routed_to_agent` — approved member message with bot mention → `_handle_admin_message` NOT called.
3. **Keep** all pass_to_pass tests listed in the brief untouched; verify they stay green.

### Step 17: Verification runs
**Scope:** Small
1. **Run** the cheapest targeted file first: `pytest tests/test_admin_payments.py -x`.
2. **Run** invariant suites: `pytest tests/test_payment_state_machine.py tests/test_payment_cog_split.py tests/test_solana_client.py tests/test_payment_race.py tests/test_payment_reconcile.py tests/test_payment_authorization.py tests/test_safe_delete_messages.py tests/test_check_payment_invariants.py tests/test_tx_signature_history.py`.
3. **Run** full suite last: `pytest`.
4. **Manual smoke** (info-only): simulate full flow in a dev guild if available — (a) admin initiates payment for unverified recipient; (b) recipient posts wallet; (c) test payment lands; (d) recipient replies "confirmed"; (e) admin receives DM, clicks Approve; (f) real payment sends. Then repeat against the now-verified wallet to confirm auto-skip.

## Execution Order
1. **Phase 1** (schema + db helpers) lands first — self-contained and unblocks the flow changes. Run the SQL migration against the dev DB before touching code.
2. **Phase 2** (parsers + identity router) — smallest code footprint, testable in isolation with unit tests.
3. **Phase 3** (flow rewiring + AdminApprovalView) — the core behavior change; relies on Phase 2 router being in place.
4. **Phase 4** (new agent tools) — independent of Phase 3's cog changes but shares the auto-skip helper with Phase 3.
5. **Phase 5** (reconciliation + 24h sweep) — cleanup of the old classifier + new background task.
6. **Phase 6** (tests) — written alongside each phase; gating suite run at the end.

## Validation Order
1. **Unit tests first:** deterministic parsers (`_parse_wallet_from_text`, `_classify_confirmation`) — pure functions, fastest signal.
2. **Router + state-machine tests** — exercised with mocked DB + `Mock` Anthropic client that raises on call.
3. **AdminApprovalView tests** — persistent view registration + click handler with mocked interactions.
4. **Batch tool atomicity** — in-memory db_handler stub that rejects the whole list on any invalid row.
5. **Full `test_admin_payments.py`** — broad test file covering the rewritten flow.
6. **Invariant suites** (`test_payment_state_machine`, `test_payment_race`, `test_payment_reconcile`, `test_payment_authorization`, `test_solana_client`, `test_safe_delete_messages`, `test_check_payment_invariants`, `test_tx_signature_history`) — must stay green.
7. **Full pytest run**.
8. **Manual end-to-end smoke** in dev guild (info-only).
