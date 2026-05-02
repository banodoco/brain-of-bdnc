# Implementation Plan: Identity-Based Admin Chat Routing + Admin-DM Approval Gate (Revision 4)

## Overview
Refactor `admin_chat` from content-based classification to strict identity-based routing (admin → agent, active-intent recipient → deterministic state machine, else → fallthrough). Gate recipient `verified_at` on a text-ack of the on-chain test. Replace `PaymentConfirmView` with a DM-only `AdminApprovalView` for `producer='admin_chat'` NEW-flow real payments, authorized via the already-existing `PaymentActorKind.ADMIN_DM`. Add 5 new agent tools plus `resolve_admin_intent` as the minimum operator surface. Auto-skip the test for verified wallets. Add a 24h timeout sweep. Grants + all hardening/cleanup/polish invariants untouched.

**Revision 4 applies three surgical fixes on top of Revision 3's resolved work**, addressing the iteration-3 gate flags:
1. **No more `awaiting_wallet` placeholder for verified-wallet fast path.** Ordering becomes: (a) duplicate check, (b) create `payment_requests` row first, (c) INSERT the intent atomically with BOTH `status='awaiting_admin_approval'` AND `final_payment_id` set in the SAME insert (not a later update), (d) fire admin DM. No placeholder state ever exists. Closes FLAG-013, correctness-1, callers-1, issue_hints-2, all_locations-2.
2. **Startup orphan-payment reconciliation sweep** runs BEFORE `_register_pending_confirmation_views`. It walks every `pending_confirmation` `producer='admin_chat'` `is_test=False` payment, and based on whether the intent referenced by `payment.metadata.intent_id` exists and is wired, either (i) recovers it by completing the intent update + firing the admin DM (test-receipt crash window), or (ii) cancels the orphan payment via the existing `cancel_payment` primitive (auto-skip crash window). `PaymentConfirmView` is never the crash fallback for new-flow admin_chat real payments — the sweep handles both windows cleanly. Closes FLAG-012, issue_hints-1, scope-1.
3. **`resolve_admin_intent` cascades cancellation to linked `payment_requests`.** The executor fetches the intent, and for each of `test_payment_id` and `final_payment_id` that is non-null and in a cancellable status (`pending_confirmation` / `queued`), calls the existing `db_handler.cancel_payment(payment_id, guild_id)` (mirroring `admin_chat_cog.py:616-620`). Only after the cascade does it update the intent row to `cancelled`. Closes FLAG-014, correctness-2, scope-2, all_locations-1, callers-2.

Everything outside these three surgical edits carries over from Revision 3 unchanged: schema (minus the now-deleted awaiting_wallet placeholder rationale), DB helpers, producer_flows authorization, state-aware restart filter (still needed for LEGACY `awaiting_confirmation` rows only), identity router, deterministic parsers, state-machine dispatch, test-receipt handlers, AdminApprovalView, new agent tools, ADMIN_ONLY_TOOLS / _ADMIN_IDENTITY_INJECTED_TOOLS extensions, 24h timeout sweep, tests.

Evidence carried from earlier revisions (unchanged):
- `PaymentActorKind.ADMIN_DM` at `payment_service.py:30`; `_authorize_actor` at `:539-546`.
- `PRODUCER_FLOWS['admin_chat'].real_confirmed_by` at `producer_flows.py:21-29`.
- `PaymentUICog._register_pending_confirmation_views` at `payment_ui_cog.py:110-130`.
- `admin_chat_cog.on_message` at `:790-817` — the `_is_directed_at_bot` gate drops for ADMIN_USER_ID.
- `ADMIN_ONLY_TOOLS` at `tools.py:908-936`; exhaustive assert at `:938-940`.
- `agent.py:431-441` — `admin_user_id` injection.
- `confirm_payment` at `payment_service.py:550-581` — transitions `pending_confirmation → queued`.
- `handle_payment_result` at `admin_chat_cog.py:416-440` — sole source of the "Payment sent." channel post.
- **`db_handler.cancel_payment(payment_id, guild_id)` at `db_handler.py:2402-2420`** — reusable primitive for cascade cancellation; used today by `admin_chat_cog.py:616-620`.
- **`create_admin_payment_intent` at `db_handler.py:1805-1819`** accepts `status` and `final_payment_id` in the initial payload — a single atomic insert CAN set both fields (verified by reading the method signature which just passes the record dict to Supabase `.insert`).
- **`sql/admin_payment_intents.sql:47-49`** — unique partial index on `(guild_id, channel_id, recipient_user_id) WHERE status NOT IN (completed, failed, cancelled)`. Inserting a second intent in `awaiting_admin_approval` for the same recipient while the first is still active will fail the constraint → DB-level atomicity guard.

Scope boundaries (unchanged): do NOT touch `solana_client.py`/`solana_provider.py`, `grants_cog.py`, `PaymentService` rebroadcast/fee logic, hardening/cleanup/polish invariants. `MEMBER_SYSTEM_PROMPT`, `_can_user_message_bot`, `_is_directed_at_bot` stay as dead code. `has_active_payment_or_intent` at `db_handler.py:1575-1601` unchanged. `manual_review` stays blocking.

## Phase 1: Schema + DB Helpers

### Step 1: Extend `admin_payment_intents` schema (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Add** idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ...` extending the status CHECK to include `awaiting_test_receipt_confirmation`, `awaiting_admin_approval`, and `manual_review`, alongside existing statuses and `awaiting_confirmation` (kept for legacy in-flight rows).
2. **Add** `ambiguous_reply_count integer not null default 0` via `add column if not exists`.
3. **Add** `receipt_prompt_message_id bigint` via `add column if not exists`.
4. **DO NOT modify the unique partial index.** `manual_review` stays in the blocking set. The existing exclusion `('completed','failed','cancelled')` is correct.

### Step 2: DB helpers (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `create_admin_payment_intents_batch(records: List[Dict], guild_id: int) -> Optional[List[Dict]]` — single Supabase `.insert([...])`. All-or-none.
2. **Add** `list_intents_by_status(guild_id: int, status: str) -> List[Dict]` for persistent-view registration.
3. **Add** `list_stale_test_receipt_intents(cutoff_iso: str) -> List[Dict]` for the 24h sweep.
4. **Add** `increment_intent_ambiguous_reply_count(intent_id: str, guild_id: int) -> Optional[int]`.
5. **Add** `get_pending_confirmation_admin_chat_intents_by_payment(payment_ids: List[str]) -> Dict[str, Optional[Dict]]` — returns `{payment_id: intent_row_or_None}` where the intent row is fetched by `metadata.intent_id` on the payment and filtered to producer='admin_chat'. Used by both the state-aware restart filter (Step 4) and the new orphan sweep (Step 18).
6. **Add** `find_admin_chat_intent_by_payment_id(payment_id: str) -> Optional[Dict]` — helper for the orphan sweep; looks up an intent by `final_payment_id == payment_id` or `test_payment_id == payment_id`. Used when `payment.metadata.intent_id` is missing or stale.
7. **Do NOT** modify `get_active_intent_for_recipient`, `list_active_intents`, or `has_active_payment_or_intent`.

## Phase 2: Payment Policy + Restart Recovery Filter (Interlocked with Phase 4 + 6)

### Step 3: Authorize `ADMIN_DM` for admin_chat real payments (`src/features/payments/producer_flows.py`)
**Scope:** Small
1. **Add** `PaymentActorKind.ADMIN_DM` to `admin_chat.real_confirmed_by`. Resulting set: `{RECIPIENT_CLICK, RECIPIENT_MESSAGE, ADMIN_DM}`. RECIPIENT_* stay as safety-belt for legacy in-flight rows.
2. **Do NOT** add a new enum value; reuse `ADMIN_DM`.
3. **Do NOT** touch grants.

### Step 4: State-aware restart-recovery filter for `PaymentConfirmView` (`src/features/payments/payment_ui_cog.py`)
**Scope:** Small
1. **Modify** `_register_pending_confirmation_views` at `:113-130`. The filter runs AFTER the new orphan sweep (Step 18), so by the time it executes, every `pending_confirmation` admin_chat real payment either has a wired intent in `awaiting_admin_approval` OR has been cancelled. Rule:
   ```python
   for payment in pending:
       payment_id = payment.get('payment_id')
       if not payment_id:
           continue
       if payment.get('producer') == 'admin_chat' and not payment.get('is_test'):
           intent = intent_by_payment.get(payment_id)
           intent_status = (intent or {}).get('status')
           # NEW-flow: intent is awaiting_admin_approval → skip; AdminApprovalView handles it.
           if intent_status == 'awaiting_admin_approval':
               continue
           # LEGACY in-flight: intent status is awaiting_confirmation → keep PaymentConfirmView
           # so pre-deploy rows survive restart.
           # Anything else (orphan sweep would have cancelled) → skip defensively.
           if intent_status != 'awaiting_confirmation':
               continue
       self.bot.add_view(PaymentConfirmView(self, payment_id))
   ```
2. **Note:** `PaymentConfirmView` is NEVER re-registered for a NEW-flow admin_chat real payment. The only admin_chat case it handles is a legacy `awaiting_confirmation` row. This closes the critique that the "crash fallback" path contradicts the brief — crash recovery is now handled by Step 18, not by reverting to the recipient gate.
3. **Grants:** unaffected.

## Phase 3: Parsers + Identity Router

### Step 5: Deterministic parsers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `_parse_wallet_from_text(content)` with tolerant delimiter regex `[\s`<>\"'(),\[\]{}*_~|]+`, trim trailing punctuation, validate each candidate via `is_valid_solana_address`.
2. **Add** `_classify_confirmation(content) -> Literal['positive','negative','ambiguous']` — hardcoded positive/negative keyword sets, case-insensitive, whole-word for single tokens, emoji substring for `👍`, multi-word phrases via bounded substring.
3. **Unit tests.**

### Step 6: Identity router (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Rewrite** `on_message` as a strict three-way switch on `message.author.id` (no `_is_directed_at_bot` gate for admin).
2. **Extract** `_handle_admin_message(message)` without the content gate and without the `_can_user_message_bot` branch. Preserve abort handling, busy queue, rate-limit exemption, channel context.
3. **Delete** the `_check_pending_payment_reply` call from `on_message`.
4. **Document** dead code (`_can_user_message_bot`, `_is_directed_at_bot`, `MEMBER_SYSTEM_PROMPT`).

### Step 7: State-machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_pending_recipient_message(message, intent)` dispatching on `intent['status']`:
   - `awaiting_wallet` → `_parse_wallet_from_text`. If found → `_handle_wallet_received`. Silent otherwise.
   - `awaiting_test_receipt_confirmation` → `_classify_confirmation` → dispatch to `_handle_test_receipt_*` (Step 9).
   - All other non-terminal states (`awaiting_test`, `awaiting_admin_approval`, `manual_review`, `awaiting_confirmation`, `confirmed`) → silent return. Because `awaiting_admin_approval` never appears as a "placeholder" state (Rev-4 fix #1), a recipient posting an address-shaped message will never incorrectly reopen the test path.
2. **Reuse** `_processing_intents` dedupe set.
3. **Unit test invariant:** Anthropic client mock raises on any call.

## Phase 4: Flow Rewiring + Admin Approval Gate

### Step 8: Rewire `handle_payment_result` test-success branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Replace** the test-success branch at `:332-414`. On `is_test=True + status=='confirmed'`:
   - Transition intent to `awaiting_test_receipt_confirmation`.
   - Post the receipt prompt; store id in `receipt_prompt_message_id` (new column), do NOT overwrite `prompt_message_id`.
   - DO NOT create the real payment here.
   - DO NOT call `send_confirmation_request`. Delete the call at `:393`.
2. **Non-confirmed test status:** unchanged.

### Step 9: Test-receipt handlers + two gate helpers — no placeholder states (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_gate_existing_intent(channel, intent, wallet_record, amount_sol) -> Optional[Dict]` — used by the test-receipt positive handler (Step 9.3) where the intent ALREADY exists in `awaiting_test_receipt_confirmation`. Ordering:
   ```
   A. payment = payment_service.request_payment(is_test=False, producer='admin_chat',
          recipient_wallet=wallet_record['wallet_address'],
          metadata={'intent_id': intent['intent_id']},
          amount_token=amount_sol, ...) → pending_confirmation row.
   B. In ONE update call: update_admin_payment_intent(intent_id,
          {'status': 'awaiting_admin_approval', 'final_payment_id': payment['payment_id']}, guild_id).
   C. bot.payment_ui_cog._send_admin_approval_dm(payment).
   ```
   If the process dies between A and B: Step 18's orphan sweep on restart finds the `pending_confirmation` payment with `metadata.intent_id` pointing at an intent whose `final_payment_id != payment_id`. The sweep completes step B (update the intent) and fires the admin DM. Recovery is deterministic and does NOT reintroduce `PaymentConfirmView`.
2. **Add** `_gate_fresh_intent_atomic(channel, guild_id, recipient_user_id, amount_sol, source_channel_id, wallet_record, admin_user_id, reason, producer_ref) -> Optional[Dict]` — used by the auto-skip verified-wallet paths (Steps 11, 13) where NO intent exists yet. Ordering:
   ```
   A. Duplicate check: db_handler.get_active_intent_for_recipient(guild_id, source_channel_id,
          recipient_user_id). If an active intent exists → abort with duplicate error.
   B. payment = payment_service.request_payment(is_test=False, producer='admin_chat',
          recipient_wallet=wallet_record['wallet_address'],
          metadata={'intent_id': <reserved uuid>},  # generated client-side
          amount_token=amount_sol, ...) → pending_confirmation row.
       The reserved intent_id uuid is generated via uuid4() and stashed for step C.
   C. INSERT the intent row in a SINGLE atomic write:
          db_handler.create_admin_payment_intent({
              'intent_id': <reserved uuid>,
              'guild_id': guild_id,
              'channel_id': source_channel_id,
              'admin_user_id': admin_user_id,
              'recipient_user_id': recipient_user_id,
              'wallet_id': wallet_record['wallet_id'],
              'requested_amount_sol': amount_sol,
              'producer_ref': producer_ref,
              'reason': reason,
              'status': 'awaiting_admin_approval',   # NEVER a placeholder
              'final_payment_id': payment['payment_id'],
          }, guild_id=guild_id)
       The DB unique partial index on (guild_id, channel_id, recipient_user_id) WHERE status NOT IN (terminal) provides the atomicity guarantee for concurrent rapid-fire inits.
   D. bot.payment_ui_cog._send_admin_approval_dm(payment).
   ```
   If the process dies between B and C: orphan payment exists with no intent referencing it. Step 18's orphan sweep finds it via `metadata.intent_id` lookup returning `None` and cancels the orphan via `cancel_payment`. The admin gets nothing (safe — fail-closed), and the admin can simply re-run `initiate_payment` to create a fresh flow. No placeholder state, no `PaymentConfirmView` fallback.
3. **Add** `_handle_test_receipt_positive(message, intent)`:
   - `wallet_record = db_handler.get_wallet_by_id(intent['wallet_id'], guild_id=intent['guild_id'])`.
   - `db_handler.mark_wallet_verified(intent['wallet_id'], guild_id=intent['guild_id'])` — sole `verified_at` write path.
   - `_gate_existing_intent(channel, intent, wallet_record, intent['requested_amount_sol'])`.
   - Post thread ack.
4. **Add** `_handle_test_receipt_negative(message, intent)`:
   - Fetch test payment for `tx_signature` + wallet.
   - Post admin-tagged message in the source channel.
   - Transition intent to `manual_review`.
   - DM admin via `_notify_intent_admin`.
5. **Add** `_handle_test_receipt_ambiguous(message, intent)`:
   - `count = increment_intent_ambiguous_reply_count(...)`.
   - `count == 1` → silent. `count >= 2` → escalate via negative handler.

### Step 10: `AdminApprovalView` + admin DM helper (`src/features/payments/payment_ui_cog.py`)
**Scope:** Large
1. **Add** `AdminApprovalView(discord.ui.View)` with `timeout=None`, single `Approve Payment` button at `custom_id=f"payment_admin_approve:{payment_id}"`. Callback:
   - Defer ephemeral.
   - Defense-in-depth admin check vs `ADMIN_USER_ID` env.
   - `confirm_payment(payment_id, guild_id=payment['guild_id'], actor=PaymentActor(PaymentActorKind.ADMIN_DM, interaction.user.id))`.
   - On `queued`: edit DM to "✅ approved — queued for sending", disable button; follow-up DM; channel post "payment has been approved by the admin and queued for sending"; `safe_delete_messages` with BOTH `prompt_message_id` and `receipt_prompt_message_id`.
   - **Do NOT** post "Payment sent" — existing `handle_payment_result` handles it after the worker completes.
2. **Add** `PaymentUICog._send_admin_approval_dm(payment) -> Optional[discord.Message]`:
   - Build DM content (amount, recipient, redacted wallet, intent_id, jump link).
   - `admin = await bot.fetch_user(int(os.getenv('ADMIN_USER_ID')))`.
   - `view = AdminApprovalView(self, payment['payment_id']); bot.add_view(view); await admin.send(content, view=view)`.
   - On `Forbidden` / HTTPException → ERROR log with `payment_id` + `intent_id`, return `None`.
3. **Add** `PaymentUICog._register_pending_admin_approval_views()` called from `cog_load` AFTER the orphan sweep (Step 18) has run:
   - For each writable guild: `db_handler.list_intents_by_status(guild_id, 'awaiting_admin_approval')`.
   - For each intent with `final_payment_id`: `bot.add_view(AdminApprovalView(self, intent['final_payment_id']))`.
   - Because Rev-4 fix #1 guarantees `awaiting_admin_approval` implies `final_payment_id` is set, the defensive filter here should never skip.
4. **Grants:** untouched.

## Phase 5: New Agent Tools + Tool Infrastructure

### Step 11: Auto-skip verified-wallet path in `execute_initiate_payment` (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Modify** `execute_initiate_payment` at `:2543-2595`:
   - Verified wallet: DO NOT create an intent in `awaiting_wallet`. Instead, call `cog._gate_fresh_intent_atomic(channel, guild_id, recipient_user_id, amount_sol, source_channel_id, wallet_record, admin_user_id, reason, producer_ref)` (Step 9.2). That helper encapsulates duplicate check + payment create + atomic intent insert + admin DM. No placeholder ever exists.
   - Unverified wallet: unchanged — intent starts in `awaiting_wallet`, wallet prompt posted.
2. **Do not** modify `_start_admin_payment_flow`.

### Step 12: Read-only query tools (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schemas for `query_payment_state`, `query_wallet_state`, `list_recent_payments`.
2. **Add** executors — thin wrappers + redaction + pop-and-warn guard.
3. **Wire** dispatch.

### Step 13: `initiate_batch_payment` tool (`src/features/admin_chat/tools.py`)
**Scope:** Large
1. **Add** schema: `initiate_batch_payment(payments: [...])` 1..20 entries.
2. **Add** executor:
   - Per-element pop-and-warn.
   - Validate every element (`recipient_user_id > 0`, `amount_sol > 0`, no active duplicate). All-or-none rejection.
   - Look up `wallet_record` for each recipient. Partition into VERIFIED and UNVERIFIED lists.
   - **UNVERIFIED branch:** build intent records with `status='awaiting_wallet'` (correctly — these intents genuinely need a wallet). Call `db_handler.create_admin_payment_intents_batch(unverified_records, guild_id)`. On success, post wallet prompt for each.
   - **VERIFIED branch:** DO NOT create batch intents for these. Instead, iterate one-at-a-time and call `cog._gate_fresh_intent_atomic(...)` per entry — each call performs duplicate check + payment create + atomic intent insert + admin DM. This means a verified entry is NOT part of the atomic Supabase batch insert (because it needs a per-entry `payment_requests` row first). The per-entry ordering still preserves crash safety via Step 18's orphan sweep. Document in the step: all-or-none atomicity applies to the VALIDATION phase and to the UNVERIFIED batch insert; VERIFIED entries are each atomic-per-entry but not atomic-across-entries. A mid-batch crash leaves some verified payments as orphans which the sweep cancels, and the admin must re-invoke for those.
   - 16-payment batch → 16 admin DMs (if all verified) OR 16 wallet prompts (if all unverified) OR a mix.
3. **Wire** dispatch.

### Step 14: `upsert_wallet_for_user` tool with explicit admin DM (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schema.
2. **Add** executor:
   - Defense-in-depth admin check via injected `admin_user_id`.
   - Validate wallet via `is_valid_solana_address`.
   - `db_handler.upsert_wallet(...)`. `WalletUpdateBlockedError` → return error.
   - **Explicitly DM admin** via `bot.fetch_user(ADMIN_USER_ID).send(...)`, wrapped in try/except for `Forbidden`/`HTTPException` (log ERROR but don't fail the tool).
   - Return a redacted result.
3. **Wire** dispatch.

### Step 15: Register new tools (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** all six new tools (`query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user`, `resolve_admin_intent`) to `ADMIN_ONLY_TOOLS`.
2. **Verify** module-level asserts hold.

### Step 16: Extend caller-side admin identity injection (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Modify** `:438-441` to use a frozenset including all four admin-identity tools:
   ```python
   _ADMIN_IDENTITY_INJECTED_TOOLS = frozenset({
       "initiate_payment",
       "initiate_batch_payment",
       "upsert_wallet_for_user",
       "resolve_admin_intent",
   })
   ```
2. **Add** `initiate_batch_payment` to `_CHANNEL_POSTING_TOOLS`.

### Step 17: System prompt updates (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Add** six tool bullets including `resolve_admin_intent(intent_id, note?)` — cancel a stuck admin payment intent and cascade-cancel any linked pending/queued `payment_requests`.
2. **Add** "State questions → query first" subsection.
3. **Do not** touch `MEMBER_SYSTEM_PROMPT`.

## Phase 6: Reconciliation, Orphan Sweep, 24h Timeout, Intent Resolution

### Step 18: Startup orphan-payment sweep + reconciliation (`src/features/admin_chat/admin_chat_cog.py` + `src/features/payments/payment_ui_cog.py`)
**Scope:** Medium
1. **Add** a new startup coroutine `_reconcile_admin_chat_orphan_payments()` on `AdminChatCog`. Called from `_ensure_startup_reconciled` BEFORE `_reconcile_active_intents` AND exposed via a new `bot.admin_chat_cog.reconcile_orphan_payments_for_ui_cog()` accessor so `PaymentUICog.cog_load` can await it before calling `_register_pending_confirmation_views`.
2. **Logic:**
   ```
   writable_guild_ids = self._get_reconciliation_guild_ids()
   pending = payment_service.get_pending_confirmation_payments(guild_ids=writable_guild_ids)
   for payment in pending:
       if payment.get('producer') != 'admin_chat' or payment.get('is_test'):
           continue
       metadata = payment.get('metadata') or {}
       intent_id = metadata.get('intent_id')
       intent = db_handler.get_admin_payment_intent(intent_id, payment['guild_id']) if intent_id else None
       if intent is None:
           # Fallback: find by payment id in case metadata is stale or missing.
           intent = db_handler.find_admin_chat_intent_by_payment_id(payment['payment_id'])
       if intent is None:
           # AUTO-SKIP CRASH: orphan payment, no intent. Cancel the orphan.
           db_handler.cancel_payment(payment['payment_id'], guild_id=payment['guild_id'],
                                     reason='orphan admin_chat real payment on startup — no intent')
           logger.error("[AdminChat] Cancelled orphan admin_chat real payment %s (no intent)", payment['payment_id'])
           continue
       intent_status = (intent.get('status') or '').lower()
       if intent_status == 'awaiting_admin_approval' and intent.get('final_payment_id') == payment['payment_id']:
           # Already wired — _register_pending_admin_approval_views handles it.
           continue
       if intent_status == 'awaiting_confirmation':
           # LEGACY in-flight row — leave for PaymentConfirmView re-registration.
           continue
       if intent_status == 'awaiting_test_receipt_confirmation':
           # TEST-RECEIPT CRASH WINDOW: the positive ack created the payment but died before
           # the intent update. Complete the update and fire the admin DM.
           updated = db_handler.update_admin_payment_intent(intent['intent_id'], {
               'status': 'awaiting_admin_approval',
               'final_payment_id': payment['payment_id'],
           }, intent['guild_id'])
           if updated:
               await bot.payment_ui_cog._send_admin_approval_dm(payment)
               logger.info("[AdminChat] Recovered test-receipt crash for intent %s", intent['intent_id'])
           continue
       # Any other intent status with a stray pending_confirmation payment is anomalous.
       # Fail-closed: cancel the orphan and leave the intent alone for ops review.
       db_handler.cancel_payment(payment['payment_id'], guild_id=payment['guild_id'],
                                 reason=f'orphan admin_chat real payment on startup — intent in {intent_status}')
       logger.error("[AdminChat] Cancelled anomalous admin_chat real payment %s (intent %s in %s)",
                    payment['payment_id'], intent['intent_id'], intent_status)
   ```
3. **Modify** `PaymentUICog.cog_load` so it awaits `bot.admin_chat_cog.reconcile_orphan_payments_for_ui_cog()` BEFORE calling `_register_pending_confirmation_views` and `_register_pending_admin_approval_views`. Ordering guarantee: orphans are resolved before any view decision is made.
4. **Extend** the existing `_reconcile_active_intents` walker:
   - `awaiting_test_receipt_confirmation` → `_reconcile_intent_history` + replay via `_handle_pending_recipient_message`.
   - `awaiting_admin_approval` → log count.
   - `manual_review` → log count.
5. **Modify** `_reconcile_intent_history` to call `_handle_pending_recipient_message(message, refreshed_intent)` per recipient message.
6. **Delete** `_classify_payment_reply` and `_check_pending_payment_reply` once no callers remain (grep-verify).

### Step 19: 24h timeout sweep (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `@tasks.loop(minutes=15) _sweep_stale_test_receipts`.
2. **Logic:** scan `list_stale_test_receipt_intents(cutoff)`; transition to `manual_review`, post admin tag, DM admin. Per-intent try/except.
3. **Start** in `cog_load`; stop in `cog_unload`.

### Step 20: `resolve_admin_intent` with cascade cancellation (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** schema: `resolve_admin_intent(intent_id: str, note?: str)`. No `resolution` parameter — the tool always writes `cancelled`.
2. **Add** executor:
   - Require trusted `admin_user_id`; verify vs `ADMIN_USER_ID`.
   - Fetch intent; refuse already-terminal statuses (`completed`/`failed`/`cancelled`).
   - **Cascade-cancel linked `payment_requests` rows BEFORE updating the intent** (closes FLAG-014, correctness-2, scope-2, all_locations-1, callers-2):
     ```python
     for payment_field in ('final_payment_id', 'test_payment_id'):
         linked_payment_id = intent.get(payment_field)
         if not linked_payment_id:
             continue
         linked_row = db_handler.get_payment_request(linked_payment_id, guild_id=guild_id)
         if linked_row and linked_row.get('status') in ('pending_confirmation', 'queued'):
             try:
                 db_handler.cancel_payment(
                     linked_payment_id,
                     guild_id=guild_id,
                     reason=f'resolve_admin_intent cascade: {note or "(no note)"}',
                 )
             except Exception as e:
                 logger.error("[AdminChat] Cascade cancel failed for %s: %s", linked_payment_id, e)
                 return {"success": False, "error": f"Cascade cancel failed for {payment_field}={linked_payment_id}: {e}"}
         elif linked_row and linked_row.get('status') in ('submitted', 'confirmed', 'manual_hold'):
             # Refuse to resolve intents whose payment is mid-flight or already confirmed —
             # operator must use /payment-resolve first.
             return {"success": False, "error": f"{payment_field}={linked_payment_id} is in non-cancellable status '{linked_row['status']}'; use /payment-resolve first"}
     ```
     Mirrors the existing decline handler at `admin_chat_cog.py:616-620` which cancels `final_payment_id` before marking the intent cancelled.
   - After cascade succeeds: `db_handler.update_admin_payment_intent(intent_id, {'status': 'cancelled'}, guild_id)`.
   - Explicit admin DM (same pattern as Step 14).
3. **Wire** dispatch. Registered via Step 15 and Step 16.

## Phase 7: Tests + Verification

### Step 21: Rewrite `tests/test_admin_payments.py`
**Scope:** Large
1. **Remove** tests mocking `_classify_payment_reply`.
2. **Parser unit tests** (punctuation-wrapped wallet, keyword parametrization).
3. **Identity router tests** (admin plain-channel, pending-recipient with Anthropic mock raising, fallthrough, approved-member no-route).
4. **State-machine tests** (awaiting_wallet valid/ignore, test-receipt positive/negative/ambiguous).
5. **Payment-gate tests:**
   - `test_producer_flows_admin_chat_real_authorizes_admin_dm`.
   - `test_admin_approval_view_only_admin_can_click`.
   - `test_admin_approval_view_persists_across_restart`.
   - `test_admin_approval_click_queues_payment_and_posts_queued_message`.
   - `test_admin_dm_fail_closed_on_forbidden`.
   - `test_payment_confirm_view_not_sent_for_admin_chat_real_payments`.
   - `test_restart_filter_state_aware` — (a) admin_chat + `awaiting_admin_approval` intent → `PaymentConfirmView` NOT re-registered; (b) admin_chat + legacy `awaiting_confirmation` intent → re-registered; (c) grants → re-registered.
6. **Auto-skip + batch tests:**
   - `test_auto_skip_verified_wallet_creates_intent_in_awaiting_admin_approval_atomically` — assert the intent is INSERTED with `status='awaiting_admin_approval'` AND `final_payment_id` set in the same create call. No intermediate status exists at any point.
   - `test_auto_skip_no_placeholder_awaiting_wallet_intent` — assert NO intent is ever observed in `awaiting_wallet` during the verified-wallet path.
   - `test_initiate_batch_payment_atomic_verified_and_unverified_partitions` — invalid element → zero writes; valid all-verified → N payment_requests rows + N intents atomic per-entry + N admin DMs; valid all-unverified → single atomic batch insert + N wallet prompts; mixed → unverified batch atomic + verified per-entry atomic.
7. **Orphan sweep tests (Step 18):**
   - `test_orphan_sweep_auto_skip_crash_cancels_payment` — create a `pending_confirmation` admin_chat real payment with no matching intent; run sweep; assert `cancel_payment` was called and the payment is cancelled.
   - `test_orphan_sweep_test_receipt_crash_completes_recovery` — create a `pending_confirmation` admin_chat real payment whose metadata points at an intent still in `awaiting_test_receipt_confirmation`; run sweep; assert the intent is transitioned to `awaiting_admin_approval` with `final_payment_id` set AND `_send_admin_approval_dm` is called.
   - `test_orphan_sweep_leaves_legacy_awaiting_confirmation_alone` — admin_chat real payment with intent in `awaiting_confirmation` → sweep does nothing; `_register_pending_confirmation_views` re-registers the `PaymentConfirmView`.
   - `test_orphan_sweep_leaves_already_wired_intent_alone` — admin_chat real payment with intent in `awaiting_admin_approval` + matching `final_payment_id` → sweep skips; `_register_pending_admin_approval_views` re-registers the view.
   - `test_orphan_sweep_runs_before_view_registration` — assert call order in `PaymentUICog.cog_load`: orphan sweep → then `_register_pending_confirmation_views` → then `_register_pending_admin_approval_views`.
8. **New-tool tests:** upsert admin-only + explicit admin DM (mock `bot.fetch_user(...).send`), query tool safety, `WalletUpdateBlockedError` path.
9. **Lifecycle + resolve tests:**
   - `test_manual_review_blocks_new_intents`.
   - `test_resolve_admin_intent_cascade_cancels_final_payment` — intent has `final_payment_id` in `pending_confirmation`; call `resolve_admin_intent`; assert `cancel_payment` was called for the linked payment AND the intent transitions to `cancelled`.
   - `test_resolve_admin_intent_cascade_cancels_test_payment` — same with `test_payment_id`.
   - `test_resolve_admin_intent_refuses_non_cancellable_linked_payment` — linked payment is `submitted` or `confirmed`; assert the tool returns an error and the intent is NOT cancelled.
   - `test_resolve_admin_intent_admin_injection` — non-admin caller rejected.
   - `test_resolve_admin_intent_rejects_completed_schema_parameter` — no `resolution` param accepted.
   - `test_24h_timeout_sweep_escalates_stuck_test_receipt`.
10. **Infrastructure tests:** `ADMIN_ONLY_TOOLS` and `_ADMIN_IDENTITY_INJECTED_TOOLS` exhaustive checks.

### Step 22: Validation runs
**Scope:** Small
1. Parser unit tests.
2. `pytest tests/test_admin_payments.py -x`.
3. Invariant suites: `test_payment_state_machine`, `test_payment_cog_split`, `test_solana_client`, `test_payment_race`, `test_payment_reconcile`, `test_payment_authorization`, `test_safe_delete_messages`, `test_check_payment_invariants`, `test_tx_signature_history`.
4. `pytest` full suite.
5. Manual smoke (info-only): first-time flow, verified-wallet auto-skip, crash BETWEEN `request_payment` and intent insert (kill bot; assert orphan sweep cancels on restart), crash BETWEEN test-receipt positive payment creation and intent update (kill bot; assert orphan sweep recovers and DMs admin on restart), batch mixed, negative ack, ambiguous 2x, admin DM click → queued messaging, restart mid `awaiting_admin_approval` → view re-registered, restart with legacy `awaiting_confirmation` → `PaymentConfirmView` re-registered, 24h sweep, upsert success + admin DM from public channel, upsert during active intent blocked, grants unchanged, query tools, approved-member no-route, `resolve_admin_intent` cascade-cancels linked payment AND clears intent, `resolve_admin_intent` refuses to touch a `submitted` payment.

## Execution Order
1. **Phase 1** (schema + DB helpers incl. `find_admin_chat_intent_by_payment_id`, `get_pending_confirmation_admin_chat_intents_by_payment`).
2. **Phase 2** (producer_flows + restart-recovery filter).
3. **Phase 3** (parsers + identity router).
4. **Phase 4** (flow rewiring + `_gate_existing_intent` + `_gate_fresh_intent_atomic` + `AdminApprovalView`).
5. **Phase 5** (agent tools + infrastructure).
6. **Phase 6** (orphan sweep FIRST, then existing reconciliation, then 24h sweep, then `resolve_admin_intent` cascade).
7. **Phase 7** (tests + verification).

## Validation Order
1. Parser unit tests.
2. Identity router + state machine with Anthropic mock raising.
3. `producer_flows` authorization.
4. State-aware restart recovery filter (three scenarios).
5. Atomic verified-wallet fast path (no placeholder, atomic insert).
6. Orphan sweep (four scenarios: auto-skip crash, test-receipt crash, legacy in-flight, already wired).
7. `AdminApprovalView` click handler.
8. Batch atomicity (verified + unverified partitions).
9. `manual_review` blocking + `resolve_admin_intent` cascade + refuse-non-cancellable.
10. `upsert_wallet_for_user` explicit DM.
11. Infrastructure tests.
12. Full `test_admin_payments.py`.
13. Invariant suites.
14. Full `pytest`.
15. Manual smoke including two crash-window scenarios.
