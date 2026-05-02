# Implementation Plan: Identity-Based Admin Chat Routing + Admin-DM Approval Gate (Revision 5)

## Overview
Refactor `admin_chat` from content-based classification to strict identity-based routing (admin → agent, active-intent recipient → deterministic state machine, else → fallthrough). Gate recipient `verified_at` on a text-ack of the on-chain test. Replace `PaymentConfirmView` with a DM-only `AdminApprovalView` for `producer='admin_chat'` NEW-flow real payments, authorized via the already-existing `PaymentActorKind.ADMIN_DM`. Add 5 new agent tools plus `resolve_admin_intent`. Auto-skip the test for verified wallets. Add a 24h timeout sweep. Grants + all hardening/cleanup/polish invariants untouched.

**Revision 5 applies two surgical fixes on top of Revision 4's 14 resolved flags**, addressing the two genuinely distinct iteration-4 concerns:

1. **Invert the orphan-sweep dependency.** Move `_reconcile_admin_chat_orphan_payments` OUT of `AdminChatCog` and INTO `PaymentUICog` itself. The sweep only touches `self.db_handler`, `self.payment_service`, `self.bot.fetch_user`, and `self._send_admin_approval_dm` — all already accessible from `PaymentUICog`. No cross-cog accessor, no main.py bootstrap changes, no test harness updates, no `FakeBot` additions. Closes **FLAG-015, correctness (v4), all_locations (v4), callers (v4)**.

2. **Restore unified atomic batch semantics via a new transient `awaiting_admin_init` state.** All `initiate_batch_payment` entries (verified + unverified) go through ONE `create_admin_payment_intents_batch` call. Verified entries get `status='awaiting_admin_init'`; unverified get `'awaiting_wallet'`. `awaiting_admin_init` is added to the CHECK constraint AND to the state-machine dispatch as a silent-ignore state (like `awaiting_test`, `awaiting_admin_approval`) so it can never reopen the wallet-collection path. After the atomic insert succeeds, verified entries iterate through `_gate_existing_intent` (already defined in Step 9 for the test-receipt-positive path) to transition to `awaiting_admin_approval` + create payment + fire DM. The orphan sweep gains a fifth case: any `awaiting_admin_init` intent older than a grace window with no `final_payment_id` → cancel (no payment was ever created — fail-closed). Closes **FLAG-016, issue_hints (v4), scope (v4)**.

Everything outside these two fixes carries over from Revision 4 verbatim: schema (with the `awaiting_admin_init` addition), DB helpers, producer_flows authorization, state-aware restart filter for legacy `awaiting_confirmation` rows, identity router, parsers, test-receipt handlers, `_gate_existing_intent` / `_gate_fresh_intent_atomic`, `AdminApprovalView`, new agent tools, ADMIN_ONLY_TOOLS / _ADMIN_IDENTITY_INJECTED_TOOLS, 24h timeout sweep, resolve_admin_intent cascade cancellation.

Evidence (unchanged from Rev-4): `PaymentActorKind.ADMIN_DM` at `payment_service.py:30`; `_authorize_actor` at `:539-546`; `PRODUCER_FLOWS['admin_chat']` at `producer_flows.py:21-29`; `PaymentUICog._register_pending_confirmation_views` at `payment_ui_cog.py:110-130`; `db_handler.cancel_payment` at `:2402-2420`; `create_admin_payment_intent` at `:1805-1819`; unique partial index at `sql/admin_payment_intents.sql:47-49`. **New Rev-5 evidence:** `main.py:257-274` loads `PaymentUICog` before `AdminChatCog`; `tests/test_payment_cog_split.py:58-63` and `tests/test_scheduler.py:423-431` exercise `PaymentUICog.cog_load()` with no admin-chat cog present. This confirms inverting the dependency is the only path that doesn't break existing callers.

Scope boundaries (unchanged): do NOT touch `solana_client.py`/`solana_provider.py`, `grants_cog.py`, `main.py` startup ordering, `tests/test_payment_cog_split.py` harness, hardening/cleanup/polish invariants. `MEMBER_SYSTEM_PROMPT`, `_can_user_message_bot`, `_is_directed_at_bot` stay as dead code. `has_active_payment_or_intent` unchanged. `manual_review` stays blocking.

## Phase 1: Schema + DB Helpers

### Step 1: Extend `admin_payment_intents` schema (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Add** idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ...` extending the status CHECK to include the new states: `awaiting_test_receipt_confirmation`, `awaiting_admin_approval`, `awaiting_admin_init` (NEW — Rev-5 transient state for atomic batch creation), and `manual_review`. Preserve the existing statuses including `awaiting_confirmation` (legacy in-flight rows).
2. **Add** `ambiguous_reply_count integer not null default 0`.
3. **Add** `receipt_prompt_message_id bigint`.
4. **DO NOT modify the unique partial index.** `awaiting_admin_init` and `manual_review` both stay in the blocking set — an `awaiting_admin_init` row correctly blocks new initiations for the same recipient while the batch is mid-transition, and the orphan sweep cancels stale ones.

### Step 2: DB helpers (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `create_admin_payment_intents_batch(records: List[Dict], guild_id: int) -> Optional[List[Dict]]` — single Supabase `.insert([...])`. All-or-none.
2. **Add** `list_intents_by_status(guild_id: int, status: str) -> List[Dict]`.
3. **Add** `list_stale_test_receipt_intents(cutoff_iso: str) -> List[Dict]` for the 24h sweep.
4. **Add** `list_stale_awaiting_admin_init_intents(cutoff_iso: str) -> List[Dict]` (NEW — Rev-5). Scans `status='awaiting_admin_init' AND updated_at < cutoff`. Used by the orphan sweep (Step 18) to cancel batch entries whose per-entry transition never completed.
5. **Add** `increment_intent_ambiguous_reply_count(intent_id, guild_id)`.
6. **Add** `get_pending_confirmation_admin_chat_intents_by_payment(payment_ids)` — returns `{payment_id: intent_row_or_None}`.
7. **Add** `find_admin_chat_intent_by_payment_id(payment_id)` — fallback lookup for the orphan sweep.
8. **Do NOT** modify `get_active_intent_for_recipient`, `list_active_intents`, or `has_active_payment_or_intent`.

## Phase 2: Payment Policy + Restart Recovery Filter

### Step 3: Authorize `ADMIN_DM` for admin_chat real payments (`src/features/payments/producer_flows.py`)
**Scope:** Small
1. **Add** `PaymentActorKind.ADMIN_DM` to `admin_chat.real_confirmed_by`. Resulting set: `{RECIPIENT_CLICK, RECIPIENT_MESSAGE, ADMIN_DM}`.
2. **Do NOT** add a new enum value.
3. **Do NOT** touch grants.

### Step 4: State-aware restart-recovery filter for `PaymentConfirmView` (`src/features/payments/payment_ui_cog.py`)
**Scope:** Small
1. **Modify** `_register_pending_confirmation_views`. The filter runs AFTER the orphan sweep (Step 18 — now living in the same cog). For each `pending_confirmation` admin_chat real payment: skip if the linked intent is in `awaiting_admin_approval` or `awaiting_admin_init` (new flow; handled by AdminApprovalView or the orphan sweep itself); keep re-registering only if the linked intent is in legacy `awaiting_confirmation`; defensively skip anything else.
2. **Grants:** unaffected.

## Phase 3: Parsers + Identity Router

### Step 5: Deterministic parsers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `_parse_wallet_from_text(content)` with tolerant delimiter regex `[\s`<>\"'(),\[\]{}*_~|]+`, trim trailing punctuation, validate via `is_valid_solana_address`.
2. **Add** `_classify_confirmation(content)` with hardcoded positive/negative keyword sets.
3. **Unit tests.**

### Step 6: Identity router (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Rewrite** `on_message` as a strict three-way switch on `message.author.id`.
2. **Extract** `_handle_admin_message(message)` without the `_is_directed_at_bot` gate and without the `_can_user_message_bot` branch.
3. **Delete** the `_check_pending_payment_reply` call from `on_message`.
4. **Document** dead code.

### Step 7: State-machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_pending_recipient_message(message, intent)` dispatching on `intent['status']`:
   - `awaiting_wallet` → `_parse_wallet_from_text`. If found → `_handle_wallet_received`. Silent otherwise.
   - `awaiting_test_receipt_confirmation` → `_classify_confirmation` → dispatch to `_handle_test_receipt_*`.
   - **All other non-terminal states silently return**, including: `awaiting_test`, `awaiting_admin_init` (NEW — Rev-5), `awaiting_admin_approval`, `manual_review`, `awaiting_confirmation`, `confirmed`. Critically, `awaiting_admin_init` is treated identically to `awaiting_admin_approval` — no recipient-facing handler, no test-payment reopening risk. This mirrors the lesson from FLAG-013: any placeholder-adjacent state must be explicitly enumerated as silent-ignore.
2. **Reuse** `_processing_intents` dedupe set.
3. **Unit test invariant:** Anthropic client mock raises on any call.
4. **Unit test:** posting a Solana address while an intent is in `awaiting_admin_init` → silent return, intent status unchanged, `_handle_wallet_received` NOT called.

## Phase 4: Flow Rewiring + Admin Approval Gate

### Step 8: Rewire `handle_payment_result` test-success branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Replace** the test-success branch at `:332-414`. On `is_test=True + status=='confirmed'`: transition to `awaiting_test_receipt_confirmation`, post receipt prompt, store `receipt_prompt_message_id`. DO NOT create real payment. DELETE the `send_confirmation_request` call at `:393`.
2. Non-confirmed test status: unchanged.

### Step 9: Test-receipt handlers + two gate helpers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_gate_existing_intent(channel, intent, wallet_record, amount_sol) -> Optional[Dict]` — used by the test-receipt positive handler AND by the batch verified-entry fan-out (Step 13, Rev-5). The intent already exists in a non-terminal state (`awaiting_test_receipt_confirmation` or `awaiting_admin_init`). Ordering:
   ```
   A. payment = payment_service.request_payment(is_test=False, producer='admin_chat',
          recipient_wallet=wallet_record['wallet_address'],
          metadata={'intent_id': intent['intent_id']},
          amount_token=amount_sol, ...) → pending_confirmation row.
   B. In ONE update call: update_admin_payment_intent(intent_id,
          {'status': 'awaiting_admin_approval', 'final_payment_id': payment['payment_id']},
          guild_id).
   C. bot.payment_ui_cog._send_admin_approval_dm(payment).
   ```
   **Crash windows covered by the orphan sweep (Step 18):**
   - If the process dies between A and B: the `payment_requests` row exists in `pending_confirmation` with `metadata.intent_id` pointing at an intent whose status is still `awaiting_test_receipt_confirmation` or `awaiting_admin_init` and whose `final_payment_id` is NULL or mismatched. The sweep completes step B and fires the admin DM.
   - If the process dies between B and C: the intent is wired. The sweep's "already wired" case recognizes it and `_register_pending_admin_approval_views` handles DM re-posting (or the admin sees the row next time they check).
2. **Add** `_gate_fresh_intent_atomic(channel, guild_id, recipient_user_id, amount_sol, source_channel_id, wallet_record, admin_user_id, reason, producer_ref) -> Optional[Dict]` — used by the SINGLE `execute_initiate_payment` verified-wallet path (no batch wrapper). Ordering: duplicate check → `request_payment` → atomic `create_admin_payment_intent` with `status='awaiting_admin_approval'` AND `final_payment_id` set in the SAME insert (client-side `uuid4()` for `intent_id`) → admin DM. This is the Rev-4 design and stays intact for single-initiate because there's no N>1 atomic-batch requirement.
3. **Add** `_handle_test_receipt_positive(message, intent)`: fetch wallet_record → `mark_wallet_verified` → `_gate_existing_intent(...)` → post thread ack.
4. **Add** `_handle_test_receipt_negative(message, intent)`: fetch test payment for context → post admin tag → transition to `manual_review` → DM admin.
5. **Add** `_handle_test_receipt_ambiguous(message, intent)`: increment counter; `count == 1` → silent, `count >= 2` → negative-handler escalation.

### Step 10: `AdminApprovalView` + admin DM helper (`src/features/payments/payment_ui_cog.py`)
**Scope:** Large
1. **Add** `AdminApprovalView(discord.ui.View)` with `timeout=None`, `custom_id=f"payment_admin_approve:{payment_id}"`. Callback: defer ephemeral, admin check, `confirm_payment(..., actor=PaymentActor(PaymentActorKind.ADMIN_DM, interaction.user.id))`. On `queued`: edit DM to "✅ approved — queued for sending", disable button; follow-up DM; channel post "queued for sending"; `safe_delete_messages` with both prompt ids. **Do NOT** post "Payment sent" — that's emitted by existing `handle_payment_result`.
2. **Add** `PaymentUICog._send_admin_approval_dm(payment) -> Optional[discord.Message]`. `Forbidden`/`HTTPException` → ERROR log, return `None`.
3. **Add** `PaymentUICog._register_pending_admin_approval_views()` called from `cog_load` AFTER the orphan sweep.

## Phase 5: New Agent Tools + Tool Infrastructure

### Step 11: Auto-skip verified-wallet path in `execute_initiate_payment` (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Modify** `execute_initiate_payment`: verified wallet → call `cog._gate_fresh_intent_atomic(...)` (Step 9.2). No placeholder state; single atomic insert.
2. Unverified path unchanged.

### Step 12: Read-only query tools (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. Add schemas for `query_payment_state`, `query_wallet_state`, `list_recent_payments`.
2. Add executors with redaction + pop-and-warn guard.
3. Wire dispatch.

### Step 13: `initiate_batch_payment` with unified atomic insert (`src/features/admin_chat/tools.py`)
**Scope:** Large
1. **Add** schema: `initiate_batch_payment(payments: [...])` 1..20 entries.
2. **Add** `execute_initiate_batch_payment(bot, db_handler, params)` with unified atomic semantics (Rev-5 restoration of the brief's all-or-none contract):
   - Per-element pop-and-warn for `wallet_address`/`recipient_wallet`.
   - Validate every element (all-or-none): `recipient_user_id > 0`, `amount_sol > 0`, no active duplicate via `get_active_intent_for_recipient`. On any failure → return error with ZERO DB writes.
   - Look up each recipient's `wallet_record` and mark verified/unverified.
   - **Build UNIFIED records list** for a SINGLE atomic insert:
     - Verified entry → `status='awaiting_admin_init'` (NEW Rev-5 transient state), `wallet_id=wallet_record['wallet_id']`, `final_payment_id=NULL` (will be populated by the fan-out phase).
     - Unverified entry → `status='awaiting_wallet'`, `wallet_id=NULL`.
   - **SINGLE atomic call:** `db_handler.create_admin_payment_intents_batch(records, guild_id)`. On `None` → failure, zero DB writes persisted (Supabase `.insert([...])` is atomic per-request on PostgREST). This restores the "all intents created atomically or none" contract from the brief.
   - **After the atomic insert succeeds,** iterate the returned intents and fan out per-entry:
     - **VERIFIED entries** (`status='awaiting_admin_init'`) → iterate and call `cog._gate_existing_intent(channel, intent, wallet_record, amount_sol)`. The helper creates the `payment_requests` row and transitions the intent to `awaiting_admin_approval` + `final_payment_id`. The intent row already exists from the atomic batch insert — the helper just updates it. If a verified entry's gate transition fails mid-way (network error, payment service down, etc.), the intent stays in `awaiting_admin_init` and the orphan sweep picks it up on the next run to cancel it (fail-closed).
     - **UNVERIFIED entries** (`status='awaiting_wallet'`) → post the wallet prompt message.
   - **Crash-safety:** a mid-fan-out crash leaves some verified intents in `awaiting_admin_init` with no payment. On restart, the orphan sweep's fifth case (Step 18) finds `awaiting_admin_init` intents older than a grace window (e.g., 5 minutes — enough to survive a transient startup delay but short enough to avoid user confusion) and cancels them. No orphan `payment_requests` rows exist because the payment is only created AFTER the intent is already in place.
   - **Atomicity semantics documented in the tool description and test:** atomicity means "all N intent rows persisted or zero intent rows persisted, in ONE DB call". Per-entry fan-out is a necessarily non-atomic follow-up phase (each entry needs its own `payment_requests` row). The orphan sweep is the recovery mechanism. A 16-payment batch produces at most 16 admin DMs (mid-fan-out crash may produce fewer; the orphan sweep cancels the rest and the admin re-invokes for those).
3. **Wire** dispatch.

### Step 14: `upsert_wallet_for_user` with explicit admin DM (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. Add schema + executor with admin check, `is_valid_solana_address` validation, `upsert_wallet` call, `WalletUpdateBlockedError` surfacing, explicit admin DM via `bot.fetch_user(ADMIN_USER_ID).send(...)` wrapped in try/except.
2. Wire dispatch.

### Step 15: Register new tools (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. Add all six new tools to `ADMIN_ONLY_TOOLS`.
2. Verify asserts hold.

### Step 16: Extend caller-side admin identity injection (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. Modify `agent.py:438-441` to use `_ADMIN_IDENTITY_INJECTED_TOOLS = {initiate_payment, initiate_batch_payment, upsert_wallet_for_user, resolve_admin_intent}`.
2. Add `initiate_batch_payment` to `_CHANNEL_POSTING_TOOLS`.

### Step 17: System prompt updates (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. Add six tool bullets.
2. Add "State questions → query first" subsection.
3. Do not touch `MEMBER_SYSTEM_PROMPT`.

## Phase 6: Reconciliation, Orphan Sweep, 24h Timeout, Intent Resolution

### Step 18: Orphan-payment reconciliation sweep LIVES IN PaymentUICog (`src/features/payments/payment_ui_cog.py`)
**Scope:** Medium
1. **Add** `PaymentUICog._reconcile_admin_chat_orphans()` as an instance method on `PaymentUICog` itself — NOT on `AdminChatCog` (Rev-5 dependency inversion). Rationale: the sweep only needs `self.db_handler`, `self.payment_service`, `self.bot`, and `self._send_admin_approval_dm` — all already accessible. Moving it into `PaymentUICog` eliminates the cross-cog ordering problem entirely: no `bot.admin_chat_cog.reconcile_orphan_payments_for_ui_cog()` accessor, no main.py bootstrap reorder, no test harness updates, no `FakeBot` additions. The tests in `tests/test_payment_cog_split.py` and `tests/test_scheduler.py` that instantiate `PaymentUICog` with no `AdminChatCog` continue to work unchanged because the sweep no longer references `AdminChatCog` at all.
2. **Call order inside `PaymentUICog.cog_load`** (the only change to `cog_load`):
   ```python
   async def cog_load(self):
       await self._reconcile_admin_chat_orphans()        # NEW — Rev-5 Step 18
       await self._register_pending_confirmation_views() # Rev-4 Step 4 state-aware filter
       await self._register_pending_admin_approval_views() # Rev-4 Step 10.3
   ```
   The orphan sweep runs first, so by the time the view-registration passes execute, every `pending_confirmation` admin_chat real payment is either (a) wired to an intent in `awaiting_admin_approval` OR (b) cancelled. And every stale `awaiting_admin_init` intent is either (a) completed (transitioned + DM fired) OR (b) cancelled.
3. **Sweep logic** (six cases — Rev-5 adds case e and handles `awaiting_admin_init`):
   ```python
   async def _reconcile_admin_chat_orphans(self):
       writable_guild_ids = self._get_writable_guild_ids()
       # PHASE 1: pending_confirmation admin_chat real payments
       pending = self.payment_service.get_pending_confirmation_payments(guild_ids=writable_guild_ids)
       for payment in pending:
           if payment.get('producer') != 'admin_chat' or payment.get('is_test'):
               continue
           metadata = payment.get('metadata') or {}
           intent_id = metadata.get('intent_id')
           intent = self.db_handler.get_admin_payment_intent(intent_id, payment['guild_id']) if intent_id else None
           if intent is None:
               intent = self.db_handler.find_admin_chat_intent_by_payment_id(payment['payment_id'])
           if intent is None:
               # (a) AUTO-SKIP CRASH (single initiate path): no intent. Cancel orphan.
               self.db_handler.cancel_payment(payment['payment_id'], guild_id=payment['guild_id'],
                                              reason='orphan admin_chat real payment on startup — no intent')
               logger.error("[PaymentUICog] Cancelled orphan admin_chat real payment %s (no intent)", payment['payment_id'])
               continue
           intent_status = (intent.get('status') or '').lower()
           if intent_status == 'awaiting_admin_approval' and intent.get('final_payment_id') == payment['payment_id']:
               # (b) Already wired. _register_pending_admin_approval_views handles it.
               continue
           if intent_status == 'awaiting_confirmation':
               # (c) LEGACY in-flight. Leave for _register_pending_confirmation_views.
               continue
           if intent_status in ('awaiting_test_receipt_confirmation', 'awaiting_admin_init'):
               # (d) TEST-RECEIPT or BATCH-FAN-OUT crash window: intent exists but
               # never got its final_payment_id update. Complete step B and fire DM.
               updated = self.db_handler.update_admin_payment_intent(intent['intent_id'], {
                   'status': 'awaiting_admin_approval',
                   'final_payment_id': payment['payment_id'],
               }, intent['guild_id'])
               if updated:
                   await self._send_admin_approval_dm(payment)
                   logger.info("[PaymentUICog] Recovered crash-window intent %s", intent['intent_id'])
               continue
           # Anomalous: cancel orphan, leave intent for ops review.
           self.db_handler.cancel_payment(payment['payment_id'], guild_id=payment['guild_id'],
                                          reason=f'orphan admin_chat real payment on startup — intent in {intent_status}')
           logger.error("[PaymentUICog] Cancelled anomalous admin_chat real payment %s (intent %s in %s)",
                        payment['payment_id'], intent['intent_id'], intent_status)
       # PHASE 2 (NEW Rev-5): awaiting_admin_init intents that never got a payment row.
       # These are batch verified-fan-out crashes where the crash happened BEFORE request_payment.
       cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
       stale_init = self.db_handler.list_stale_awaiting_admin_init_intents(cutoff)
       for intent in stale_init:
           # Defensive: if final_payment_id is set, the intent was actually wired between
           # the stale query and this iteration — skip.
           if intent.get('final_payment_id'):
               continue
           # (e) Stale awaiting_admin_init with no payment row exists. Cancel the intent
           # (fail-closed). No payment_requests cleanup needed because no payment exists.
           self.db_handler.update_admin_payment_intent(intent['intent_id'], {
               'status': 'cancelled',
           }, intent['guild_id'])
           logger.error("[PaymentUICog] Cancelled stale awaiting_admin_init intent %s (batch fan-out crash)", intent['intent_id'])
   ```
4. **Defensive `bot.payment_ui_cog` attribute:** since `_gate_existing_intent` (in `admin_chat_cog.py`) references `bot.payment_ui_cog._send_admin_approval_dm(...)`, and this call happens only AFTER the orphan sweep has run (production flow), there is no initialization-order problem. The attribute is set in `PaymentUICog.__init__` via `self.bot.payment_ui_cog = self` (Rev-4 kept this from the original file).
5. **Extend** the existing `AdminChatCog._reconcile_active_intents` walker to handle `awaiting_test_receipt_confirmation` (replay via `_handle_pending_recipient_message`) and to log counts for `awaiting_admin_approval` / `awaiting_admin_init` / `manual_review`. This is the ONLY Step 18 change that stays in `AdminChatCog` — it's pure intent-lifecycle reconciliation, not payment-row reconciliation.
6. **Modify** `_reconcile_intent_history` to call `_handle_pending_recipient_message(message, refreshed_intent)`.
7. **Delete** `_classify_payment_reply` and `_check_pending_payment_reply` once grep confirms no callers.

### Step 19: 24h timeout sweep (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `@tasks.loop(minutes=15) _sweep_stale_test_receipts` in `AdminChatCog`.
2. Logic: scan `list_stale_test_receipt_intents(cutoff)`; transition to `manual_review`; DM admin.
3. Start/stop in `cog_load`/`cog_unload`.

### Step 20: `resolve_admin_intent` with cascade cancellation (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** schema: `resolve_admin_intent(intent_id: str, note?: str)` — no `resolution` parameter; always writes `cancelled`.
2. **Executor:**
   - Admin check via injected `admin_user_id`.
   - Fetch intent; refuse already-terminal statuses.
   - **Cascade-cancel linked `payment_requests` rows** (test_payment_id + final_payment_id) via `db_handler.cancel_payment` if in cancellable status. Refuse if any linked payment is in `submitted`/`confirmed`/`manual_hold` (direct admin to `/payment-resolve`).
   - After cascade succeeds: update intent to `cancelled`.
   - Explicit admin DM.
3. Wire dispatch.

## Phase 7: Tests + Verification

### Step 21: Rewrite `tests/test_admin_payments.py`
**Scope:** Large
1. **Remove** tests mocking `_classify_payment_reply`.
2. **Parser unit tests.**
3. **Identity router tests.**
4. **State-machine tests** including:
   - `test_state_machine_silently_ignores_awaiting_admin_init` — recipient posts an address-shaped message while their intent is in `awaiting_admin_init`; assert `_handle_wallet_received` is NOT called and the intent status is unchanged.
5. **Payment-gate tests:**
   - `test_producer_flows_admin_chat_real_authorizes_admin_dm`.
   - `test_admin_approval_view_only_admin_can_click`.
   - `test_admin_approval_view_persists_across_restart`.
   - `test_admin_approval_click_queues_payment_and_posts_queued_message`.
   - `test_admin_dm_fail_closed_on_forbidden`.
   - `test_payment_confirm_view_not_sent_for_admin_chat_real_payments`.
   - `test_restart_filter_state_aware` — three scenarios.
6. **Auto-skip + batch tests:**
   - `test_auto_skip_single_initiate_uses_gate_fresh_intent_atomic` — verified single initiate lands in `awaiting_admin_approval` with `final_payment_id` set in the initial create call. No placeholder intermediate state.
   - **`test_initiate_batch_payment_atomic_all_or_none` (brief's fail_to_pass test)** — (a) batch with one invalid element → zero DB writes (single `create_admin_payment_intents_batch` returns `None` OR validation phase rejects before any DB call); (b) valid mixed batch → SINGLE atomic insert persists all N intent rows in one call; verified entries start in `awaiting_admin_init`, unverified in `awaiting_wallet`. This test verifies the RESTORED atomicity contract from Rev-5 fix #2.
   - `test_initiate_batch_payment_verified_fan_out_transitions_to_awaiting_admin_approval` — after the atomic insert, each verified entry is transitioned via `_gate_existing_intent` to `awaiting_admin_approval` with a `final_payment_id` and an admin DM fired. No verified entry is ever observed with `status='awaiting_wallet'` or `status='awaiting_test'` at any point.
   - `test_initiate_batch_payment_fan_out_crash_leaves_awaiting_admin_init_for_sweep` — simulate a crash mid-fan-out (mock `request_payment` to raise on the 3rd of 5 verified entries). Assert: first 2 entries are in `awaiting_admin_approval` + have payments + fired DMs; last 3 remain in `awaiting_admin_init` with no `final_payment_id`. Then run the orphan sweep (via `PaymentUICog._reconcile_admin_chat_orphans()`), assert the last 3 intents are transitioned to `cancelled` and no `payment_requests` rows exist for them.
7. **Orphan sweep tests — LIVE IN THE UI COG TEST FILE (`tests/test_payment_cog_split.py`) not admin_chat tests:**
   - `test_orphan_sweep_auto_skip_crash_cancels_payment`.
   - `test_orphan_sweep_test_receipt_crash_completes_recovery`.
   - `test_orphan_sweep_batch_fan_out_crash_cancels_stale_awaiting_admin_init` — NEW Rev-5 test for case (e).
   - `test_orphan_sweep_leaves_legacy_awaiting_confirmation_alone`.
   - `test_orphan_sweep_leaves_already_wired_intent_alone`.
   - `test_orphan_sweep_runs_before_view_registration` — assert `cog_load` call order: `_reconcile_admin_chat_orphans` → `_register_pending_confirmation_views` → `_register_pending_admin_approval_views`.
   - **`test_payment_ui_cog_loads_without_admin_chat_cog`** — the critical regression test for the Rev-5 dependency inversion: instantiate `PaymentUICog` with a `FakeBot` that has NO `admin_chat_cog` attribute, await `cog_load()`, assert no `AttributeError` and the sweep ran to completion (with an empty pending-confirmation list). This test directly validates that `main.py` bootstrap order and `tests/test_scheduler.py` continue to work without modification.
8. **New-tool tests:** query tool safety, upsert admin-only + explicit admin DM, `WalletUpdateBlockedError` path.
9. **Lifecycle + resolve tests:**
   - `test_manual_review_blocks_new_intents`.
   - `test_resolve_admin_intent_cascade_cancels_final_payment`.
   - `test_resolve_admin_intent_cascade_cancels_test_payment`.
   - `test_resolve_admin_intent_refuses_non_cancellable_linked_payment`.
   - `test_resolve_admin_intent_admin_injection`.
   - `test_resolve_admin_intent_rejects_completed_schema_parameter`.
   - `test_24h_timeout_sweep_escalates_stuck_test_receipt`.
10. **Infrastructure tests:** `ADMIN_ONLY_TOOLS` and `_ADMIN_IDENTITY_INJECTED_TOOLS` exhaustive checks.

### Step 22: Validation runs
**Scope:** Small
1. Parser unit tests.
2. `pytest tests/test_admin_payments.py tests/test_payment_cog_split.py tests/test_scheduler.py -x` (including the new `test_payment_ui_cog_loads_without_admin_chat_cog`).
3. Invariant suites: full pass_to_pass list.
4. `pytest` full suite.
5. Manual smoke — same scenarios as Rev-4 plus: (r) inject `request_payment` failure mid-batch-fan-out for a batch of 5 verified entries; assert orphan sweep cancels the stale `awaiting_admin_init` rows on next startup.

## Execution Order
1. **Phase 1** (schema + DB helpers, incl. `awaiting_admin_init` in CHECK, `list_stale_awaiting_admin_init_intents` helper).
2. **Phase 2** (producer_flows + state-aware restart filter).
3. **Phase 3** (parsers + identity router, incl. `awaiting_admin_init` silent-ignore in state-machine dispatch).
4. **Phase 4** (flow rewiring + AdminApprovalView). `_gate_existing_intent` is the shared helper for test-receipt-positive AND batch-verified-fan-out.
5. **Phase 5** (agent tools). Step 13 `initiate_batch_payment` is the unified atomic insert + per-entry fan-out.
6. **Phase 6** (orphan sweep in PaymentUICog — NOT AdminChatCog — then existing reconciliation, then 24h sweep, then `resolve_admin_intent` cascade).
7. **Phase 7** (tests + verification, including the critical `test_payment_ui_cog_loads_without_admin_chat_cog` regression test).

## Validation Order
1. Parser unit tests.
2. Identity router + state machine (incl. `awaiting_admin_init` silent-ignore test) with Anthropic mock raising.
3. `producer_flows` authorization.
4. State-aware restart filter (three scenarios).
5. Single-initiate verified atomic insert via `_gate_fresh_intent_atomic`.
6. **Batch unified atomic insert test** — the fail_to_pass `test_initiate_batch_payment_atomic_all_or_none` MUST pass with the restored single-DB-call semantics.
7. Batch fan-out crash → orphan sweep recovery test.
8. `PaymentUICog` orphan sweep — six scenarios including the new stale-`awaiting_admin_init` case.
9. **`test_payment_ui_cog_loads_without_admin_chat_cog`** — critical regression test for the Rev-5 dependency inversion.
10. `AdminApprovalView` click handler.
11. `manual_review` blocking + `resolve_admin_intent` cascade + refuse-non-cancellable.
12. `upsert_wallet_for_user` explicit DM.
13. Infrastructure tests.
14. Full `test_admin_payments.py`, `test_payment_cog_split.py`, `test_scheduler.py`.
15. Invariant suites.
16. Full `pytest`.
17. Manual smoke including batch fan-out crash scenario.
