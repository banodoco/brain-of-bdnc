# Implementation Plan: Identity-Based Admin Chat Routing + Admin-DM Approval Gate (Revision 2)

## Overview
Refactor `admin_chat` from content-based classification to strict identity-based routing (admin → agent, active-intent recipient → deterministic state machine, else → fallthrough). Gate recipient `verified_at` on a text-ack of the on-chain test. Replace `PaymentConfirmView` with a DM-only `AdminApprovalView` for `producer='admin_chat'` real payments, authorized via the **already-existing** `PaymentActorKind.ADMIN_DM`. Add 5 new agent tools: `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user`. Auto-skip the test for verified wallets. Add a 24h timeout sweep. Grants + all hardening/cleanup/polish invariants untouched.

**This revision is not a re-scope** — the same architectural change — but tightens correctness against 19 gate flags surfaced in iteration 1. Critically: the gate found that the admin DM gate does not work end-to-end without **three interlocked changes** — (1) `producer_flows.py` must authorize `ADMIN_DM` for admin_chat real, (2) `_register_pending_confirmation_views` must skip admin_chat real payments or they revert to `PaymentConfirmView` on restart, (3) `PaymentActorKind.ADMIN_DM` already exists (no new enum needed). Each is now an explicit step.

Concrete evidence verified during this revision:
- `PaymentActorKind.ADMIN_DM` exists at `src/features/payments/payment_service.py:30` and `_authorize_actor` at `:539-546` already enforces `interaction.user.id == ADMIN_USER_ID` for it — reuse, do not add a new enum value.
- `PRODUCER_FLOWS['admin_chat'].real_confirmed_by` at `src/features/payments/producer_flows.py:23-28` is `{RECIPIENT_CLICK, RECIPIENT_MESSAGE}` — `ADMIN_DM` is NOT in the set, so `AdminApprovalView` clicks fail `_authorize_actor` today. Must add `ADMIN_DM`.
- `PaymentUICog.cog_load` at `src/features/payments/payment_ui_cog.py:110-130` re-registers `PaymentConfirmView` for EVERY `pending_confirmation` payment via `db_handler.get_pending_confirmation_payments` at `src/common/db_handler.py:2479-2503`. On restart, admin_chat real payments would re-acquire the recipient-click gate. Must filter either the DB query or the registration pass by producer.
- `admin_chat_cog.on_message` at `:791-817` currently guards the agent path with `_is_directed_at_bot(message)` at `:797`. Under identity routing, admin messages must reach the agent unconditionally — that gate must be dropped for `ADMIN_USER_ID`.
- `ADMIN_ONLY_TOOLS` at `src/features/admin_chat/tools.py:908-936` is asserted exhaustive vs `ALL_TOOL_NAMES` at `:938-940`. Adding new tools without extending `ADMIN_ONLY_TOOLS` will break the module assert at import time.
- `agent.py:431-441` only injects `admin_user_id` for `initiate_payment`. Must extend to `initiate_batch_payment` and `upsert_wallet_for_user`.
- `confirm_payment` at `payment_service.py:550-581` only transitions `pending_confirmation → queued`. The worker loop performs the actual send. The existing `handle_payment_result` at `admin_chat_cog.py:416-440` already posts "Payment sent." in the channel when `status=='confirmed'` for non-test payments — reuse that path. The admin-click handler must post "queued for sending", NOT "payment sent".
- `_is_directed_at_bot`, busy queue, abort handling, rate limiter, channel context building all live in the current admin branch (`:790-end`) and must move into `_handle_admin_message` (but with the `_is_directed_at_bot` content gate removed, and the `_can_user_message_bot` member gate removed).

Scope boundaries (unchanged): do NOT touch `solana_client.py`/`solana_provider.py`, `grants_cog.py`, `PaymentService` rebroadcast/fee logic, hardening/cleanup/polish invariants. `MEMBER_SYSTEM_PROMPT` stays as dead code.

## Phase 1: Schema + DB Helpers

### Step 1: Extend `admin_payment_intents` schema (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Add** idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ...` to extend the status CHECK to: `awaiting_wallet`, `awaiting_test`, `awaiting_test_receipt_confirmation` (NEW), `awaiting_admin_approval` (NEW), `awaiting_confirmation` (kept for back-compat of any in-flight rows), `confirmed`, `completed`, `failed`, `cancelled`, `manual_review` (NEW — negative-ack / 24h-timeout escalation target).
2. **Add** `ambiguous_reply_count integer not null default 0` via `add column if not exists` for the two-strike ambiguous counter.
3. **Add** `receipt_prompt_message_id bigint` via `add column if not exists` — tracks the "please reply confirmed" prompt SEPARATELY from the wallet prompt. This resolves **correctness-3** (we cannot reuse the single `prompt_message_id` column because safe-delete needs BOTH the wallet prompt and the receipt prompt to clear stale flow messages in the thread after admin approval).
4. **Update** the partial unique index at `sql/admin_payment_intents.sql:47-49` so `manual_review` is in the EXCLUDED set alongside `completed`, `failed`, `cancelled`. Rationale: `manual_review` must NOT block new intents for the same recipient (see **FLAG-006 / callers-3**). Drop+recreate the index idempotently. Extend the active-intent exclusion list in `db_handler.get_active_intent_for_recipient` and `list_active_intents` to match (Step 2).

### Step 2: DB helpers + active-intent exclusion (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `create_admin_payment_intents_batch(records: List[Dict], guild_id: int) -> Optional[List[Dict]]` via `self.supabase.table('admin_payment_intents').insert([...]).execute()`. All-or-none: `None` on any exception.
2. **Add** `list_intents_by_status(guild_id: int, status: str) -> List[Dict]` for the persistent-view registration pass.
3. **Add** `list_stale_test_receipt_intents(cutoff_iso: str) -> List[Dict]` — cross-guild scan for the 24h sweep (`status='awaiting_test_receipt_confirmation' AND updated_at < cutoff`).
4. **Add** `increment_intent_ambiguous_reply_count(intent_id: str, guild_id: int) -> Optional[int]` — read current value, write `+1`, return new value.
5. **Modify** `get_active_intent_for_recipient` at `:1842-1870` and `list_active_intents` at `:1872-1891` — extend the `not_.in_('status', [...])` exclusion set from `['completed','failed','cancelled']` to `['completed','failed','cancelled','manual_review']`. This ensures `manual_review` intents do not block new initiations and do not re-route recipient messages into the state machine. Resolves **FLAG-006, callers-3**.
6. **Add** `get_pending_confirmation_payments_excluding_producers(producers: List[str], guild_ids: ...) -> List[Dict]` OR extend `get_pending_confirmation_payments` with an optional `exclude_producers` filter. Used by Step 9 to stop re-registering `PaymentConfirmView` for `producer='admin_chat'` real payments on restart. Resolves **FLAG-001, all_locations-1**.

## Phase 2: Payment Policy + Restart Recovery Filter (Interlocked with Phase 3)

### Step 3: Authorize `ADMIN_DM` for admin_chat real payments (`src/features/payments/producer_flows.py`)
**Scope:** Small
1. **Add** `PaymentActorKind.ADMIN_DM` to `admin_chat.real_confirmed_by` at `src/features/payments/producer_flows.py:23-28`. Resulting set: `{RECIPIENT_CLICK, RECIPIENT_MESSAGE, ADMIN_DM}`. `_authorize_actor` at `payment_service.py:539-546` already enforces `actor_id == ADMIN_USER_ID` for `ADMIN_DM`, so no further payment_service change is needed. Resolves **FLAG-002, callers-2, correctness-2, scope-1**.
2. **Do NOT** add a new `PaymentActorKind.ADMIN_CLICK` enum value — `ADMIN_DM` already exists at `payment_service.py:30` with full authorization logic. Reuse it.
3. **Do NOT** touch `grants.real_confirmed_by` at `:17-19` (stays `{RECIPIENT_CLICK}`).

### Step 4: Filter `PaymentConfirmView` restart recovery (`src/features/payments/payment_ui_cog.py`)
**Scope:** Small
1. **Modify** `_register_pending_confirmation_views` at `src/features/payments/payment_ui_cog.py:113-130`: filter out `producer='admin_chat'` real payments so they are NOT re-registered with `PaymentConfirmView` on restart. Two options, pick (a):
   - (a) Pass `exclude_producers=['admin_chat']` to the new Step 2.6 method and only register for grants.
   - (b) Inline-filter the returned list: `for payment in pending: if payment['producer'] == 'admin_chat' and not payment.get('is_test'): continue`.
2. Option (a) is preferred because it keeps the filter at the DB layer and makes the intent explicit. Add a unit test asserting that a pending-confirmation admin_chat real payment is NOT in the re-registration pass. Resolves **FLAG-001, all_locations-1**.
3. **Note:** grants real payments (`producer='grants'`) remain in the re-registration pass — grants keeps `PaymentConfirmView`.

## Phase 3: Parsers + Identity Router

### Step 5: Deterministic parsers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `_parse_wallet_from_text(content: str) -> Optional[str]` with tolerant token extraction (resolves **issue_hints-2**):
   ```python
   _WALLET_DELIMITERS = re.compile(r"[\s`<>\"'(),\[\]{}*_~|]+")
   def _parse_wallet_from_text(content: str) -> Optional[str]:
       if not content:
           return None
       for token in _WALLET_DELIMITERS.split(content.strip()):
           token = token.strip('.,;:!?')
           if token and is_valid_solana_address(token):
               return token
       return None
   ```
   Strips common markdown/punctuation delimiters before splitting and trims trailing punctuation from each candidate. Validates via the already-imported `is_valid_solana_address`.
2. **Add** `_classify_confirmation(content: str) -> Literal['positive','negative','ambiguous']`:
   - Positive keywords: `{'confirmed','received','got it','yes','yep','confirm','👍'}`.
   - Negative keywords: `{'no','didnt',"didn't",'not received','missing','nothing'}`.
   - Lowercase + strip; for word keywords use `\b{kw}\b` regex on the lowercased text; for emoji `👍` use substring match. Handle multi-word phrases ("got it", "not received") as substring with surrounding whitespace/punctuation. Ambiguous = neither set matched OR both sets matched.
3. **Unit tests** for both parsers — pure functions, fastest signal.

### Step 6: Identity router + drop content gating for admin (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Rewrite** `on_message` at `src/features/admin_chat/admin_chat_cog.py:790-` as a strict three-way switch on `message.author.id`:
   ```python
   if message.author.bot:
       return
   author_id = message.author.id
   if self._is_admin(author_id):
       await self._handle_admin_message(message)
       return
   if message.guild is not None:
       intent = self.db_handler.get_active_intent_for_recipient(
           message.guild.id, message.channel.id, author_id,
       )
       if intent:
           await self._handle_pending_recipient_message(message, intent)
       return
   # DMs from non-admin: fallthrough.
   ```
2. **Extract** `_handle_admin_message(message)` from the current `:797-end` block **with the `_is_directed_at_bot(message)` gate and the `_can_user_message_bot` approved-member branch REMOVED**. Admin messages reach the agent unconditionally — DM, mention, reply, OR plain channel message. Resolves **FLAG-003, issue_hints-1**. Keep: abort handling, busy/pending queue, rate limiter (though rate limit should only apply in safety-net scenarios — admin was rate-limit-exempt in the original via the `if not is_admin:` branch at `:808`, preserve that).
3. **Delete** `_check_pending_payment_reply` from `on_message`. Leave the method defined temporarily (Step 13 removes it once the reconciler no longer calls it).
4. **Note explicitly:** `_can_user_message_bot` and approved-member flow are now dead under identity routing — documented, not deleted.

### Step 7: State machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_pending_recipient_message(message, intent)` — dispatch on `intent['status']`:
   - `awaiting_wallet` → `_parse_wallet_from_text(message.content)`. If found → existing `_handle_wallet_received(message, intent, wallet)`. If not → silent return.
   - `awaiting_test_receipt_confirmation` → `_classify_confirmation(message.content)` → dispatch to `_handle_test_receipt_positive/negative/ambiguous` (Step 9).
   - Other states (`awaiting_test`, `awaiting_admin_approval`, `manual_review` [shouldn't happen: excluded by Step 2.5], `confirmed`) → silent return.
2. **Reuse** the `_processing_intents` dedupe set at `:575-578`.
3. **Unit-test invariant:** `self._classifier_client = Mock(side_effect=AssertionError("no LLM"))` — no path in the state machine may invoke the Anthropic client.

## Phase 4: Flow Rewiring + Admin Approval Gate

### Step 8: Rewire `handle_payment_result` test-success branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Replace** the test-success branch at `:332-414`. On `is_test=True + status=='confirmed'`:
   - Transition intent to `awaiting_test_receipt_confirmation`.
   - Post the receipt prompt in the source channel: `"<@{recipient}> test payment confirmed on-chain. Please check your wallet and reply 'confirmed' once you see {amount} SOL."`
   - Store the returned message id in `receipt_prompt_message_id` (NEW column from Step 1.3) — do NOT overwrite the wallet prompt's `prompt_message_id`.
   - DO NOT create the real payment here.
   - DO NOT call `send_confirmation_request`. Delete the call at `:393`.
   - Keep `_notify_intent_admin` status note at `:347-355`.
2. **Non-confirmed test status:** unchanged (`_notify_admin_review` + fail intent).

### Step 9: Test-receipt confirmation handlers + shared gate helper (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** a shared helper `_create_real_payment_and_gate(channel, intent, wallet_record, amount_sol) -> Optional[Dict]` used by BOTH the test-receipt positive handler AND the auto-skip verified-wallet path (Step 11). Logic:
   - Call `payment_service.request_payment(is_test=False, producer='admin_chat', recipient_wallet=wallet_record['wallet_address'], metadata={'intent_id': ...}, amount_token=amount_sol, ...)` → returns a row with `status='pending_confirmation'`.
   - Update intent: `status='awaiting_admin_approval'`, `final_payment_id=new_payment['payment_id']`.
   - Invoke `bot.payment_ui_cog._send_admin_approval_dm(new_payment)` — this attaches the persistent `AdminApprovalView` and DMs the admin.
   - On any failure at any stage, transition intent to `failed` and `_notify_admin_review`. Return payment dict or `None`.
2. **Add** `_handle_test_receipt_positive(message, intent)`:
   - `wallet_record = db_handler.get_wallet_by_id(intent['wallet_id'], guild_id=intent['guild_id'])`.
   - `db_handler.mark_wallet_verified(intent['wallet_id'], guild_id=intent['guild_id'])` — the SOLE new-flow write of `verified_at`.
   - Call `_create_real_payment_and_gate(...)`.
   - Post thread ack: `"<@{recipient}> thanks — admin approval requested."`
3. **Add** `_handle_test_receipt_negative(message, intent)`:
   - Fetch test payment via `db_handler.get_payment_request(intent['test_payment_id'], ...)` for `tx_signature` + wallet address.
   - Post in the intent channel: `"{admin_mention} negative ack from <@{recipient}> on intent `{intent_id}`. test tx: `{tx_sig}`, wallet: `{redact_wallet(wallet)}`. Moving to manual_review."`
   - Transition intent to `status='manual_review'`.
   - Also DM admin via `_notify_intent_admin`.
4. **Add** `_handle_test_receipt_ambiguous(message, intent)`:
   - `count = db_handler.increment_intent_ambiguous_reply_count(intent_id, guild_id)`.
   - `count == 1` → silent return.
   - `count >= 2` → same path as `_handle_test_receipt_negative`.

### Step 10: `AdminApprovalView` + admin DM helper (`src/features/payments/payment_ui_cog.py`)
**Scope:** Large
1. **Add** `AdminApprovalView(discord.ui.View)` next to `PaymentConfirmView` at `:19`. Pattern:
   - `timeout=None`, single `Approve Payment` button with `custom_id=f"payment_admin_approve:{payment_id}"` (grep-verified: does NOT collide with the existing `f"payment_confirm:{payment_id}"` at `:30`).
   - Callback:
     - `await interaction.response.defer(ephemeral=True)`.
     - Defense-in-depth admin check: `int(interaction.user.id) != int(os.getenv('ADMIN_USER_ID'))` → ephemeral `"Only the admin can approve this payment."` and return.
     - Fetch payment via `db_handler.get_payment_request(payment_id)`.
     - Call `payment_service.confirm_payment(payment_id, guild_id=payment['guild_id'], actor=PaymentActor(PaymentActorKind.ADMIN_DM, interaction.user.id))`. **Reuse the existing `ADMIN_DM` kind** (resolves **correctness-2**) — `producer_flows.py` authorization was added in Step 3.
     - On success (result has `status='queued'`): edit the DM to `"✅ approved — queued for sending"`, disable the button. Send follow-up DM `"Payment `{payment_id}` queued. You'll see the 'sent' confirmation in the source channel once the worker processes it."` Post in the intent's source channel: `"<@{recipient}> your payment has been approved by the admin and queued for sending."` Call `safe_delete_messages(channel, [intent['prompt_message_id'], intent['receipt_prompt_message_id']], logger=logger)` — deletes BOTH the wallet prompt AND the receipt prompt (resolves **correctness-3**).
     - **Do NOT** post "Payment sent to @user" here — that message is posted later by the existing `handle_payment_result` at `admin_chat_cog.py:416-440` when the worker transitions the payment to `confirmed`. Resolves **FLAG-005, correctness-1**.
2. **Add** `PaymentUICog._send_admin_approval_dm(payment) -> Optional[discord.Message]`:
   - Build content: amount, recipient mention, redacted wallet, intent_id, jump link to the intent's source channel.
   - `admin = await self.bot.fetch_user(int(os.getenv('ADMIN_USER_ID')))`.
   - `view = AdminApprovalView(self, payment['payment_id']); self.bot.add_view(view); await admin.send(content, view=view)`.
   - On `discord.Forbidden` / rate limit: log at ERROR with `payment_id` + `intent_id`, leave intent in `awaiting_admin_approval`, return `None` (fail-closed, no public fallback).
3. **Add** `PaymentUICog._register_pending_admin_approval_views()` called from `cog_load` alongside `_register_pending_confirmation_views`:
   - For each writable guild, `db_handler.list_intents_by_status(guild_id, 'awaiting_admin_approval')`.
   - For each intent with `final_payment_id`, `self.bot.add_view(AdminApprovalView(self, intent['final_payment_id']))`.
4. **Grants:** no changes to `PaymentConfirmView` registration for grants — Step 4 already filters admin_chat out of the pending-confirmation re-registration pass.

## Phase 5: New Agent Tools + Tool Infrastructure

### Step 11: Auto-skip verified-wallet path in `execute_initiate_payment` (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Modify** `execute_initiate_payment` at `src/features/admin_chat/tools.py:2543-2595`:
   - When `wallet_record` (verified) is present, DO NOT call `cog._start_admin_payment_flow` (which issues a test payment).
   - Instead, create the intent directly with `status='awaiting_admin_approval'` AND immediately call the new `cog._create_real_payment_and_gate(channel, intent, wallet_record, amount_sol)` helper from Step 9.1. This creates the real payment and fires the admin DM in one step. No intermediate `awaiting_test` state.
   - Unverified wallet path: unchanged — intent starts in `awaiting_wallet`, wallet prompt posted.
2. **Do not** modify `_start_admin_payment_flow` — it stays as the test-only entry point for unverified paths. When the test confirms, `handle_payment_result` (Step 8) routes to `awaiting_test_receipt_confirmation`.

### Step 12: Read-only query tools (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** three tool schemas near the existing `list_payments`/`get_payment_status` at `:455-559`:
   - `query_payment_state(payment_id?: str, user_id?: str)` — exactly one required.
   - `query_wallet_state(user_id: str)` — required.
   - `list_recent_payments(producer?: str, user_id?: str, limit?: int=20)`.
2. **Add** executors — thin wrappers over `db_handler.get_payment_request` / `list_payment_requests` / `list_wallets` with existing `_redact_payment_row` / `_redact_wallet_row`.
3. **Wire** dispatch in the `execute_tool` switch near `:3184-3199`.
4. **Pop-and-warn guard:** at the top of each executor, `params.pop('wallet_address', None); params.pop('recipient_wallet', None)` and log a warning if either was set — matches the pattern at `:2501-2508`. Resolves the query-tool portion of the LLM-injection guard.

### Step 13: `initiate_batch_payment` tool (`src/features/admin_chat/tools.py`)
**Scope:** Large
1. **Add** schema: `initiate_batch_payment(payments: List[{recipient_user_id: str, amount_sol: number, reason?: string}])`. `payments` length 1..20.
2. **Add** `execute_initiate_batch_payment(bot, db_handler, params)`:
   - Pop-and-warn `wallet_address`/`recipient_wallet` from EACH payment dict (per-element guard).
   - Validate every element before any DB write: valid `recipient_user_id > 0`, `amount_sol > 0`, no active duplicate via `get_active_intent_for_recipient(guild_id, source_channel_id, recipient_user_id)`. On any failure → return `{success: False, error: ..., index: N}` with ZERO DB writes.
   - **Look up each recipient's `wallet_record` BEFORE the batch insert** and mark each element as verified (has `verified_at`) or unverified.
   - Build records — **critical correctness fix for issue_hints-3**: verified-wallet elements set `status='awaiting_admin_approval'` directly; unverified elements set `status='awaiting_wallet'`. NO element ever goes through `awaiting_test` at intent creation time. (The `awaiting_test` state is only reached by unverified-wallet intents later, AFTER the wallet is provided by the recipient and `_handle_wallet_received` transitions them.)
   - Call `db_handler.create_admin_payment_intents_batch(records, guild_id)`. On `None` → return failure (all-or-none).
   - On success, iterate returned intents:
     - Verified → call `cog._create_real_payment_and_gate(channel, intent, wallet_record, amount_sol)` — one admin DM per intent.
     - Unverified → post the wallet prompt message (matching single-intent `:2577-2591`).
   - A 16-payment batch produces 16 admin DMs (for verified) or 16 wallet prompts (for unverified) or a mix.
3. **Wire** dispatch in `execute_tool`.

### Step 14: `upsert_wallet_for_user` tool (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schema: `upsert_wallet_for_user(user_id: str, wallet_address: str, chain?: string='solana', reason?: string)`.
2. **Add** executor:
   - Defense-in-depth admin check: require trusted `admin_user_id` injected by the agent caller (Step 16) and verify `int(admin_user_id) == int(os.getenv('ADMIN_USER_ID'))`.
   - Validate `wallet_address` via `is_valid_solana_address` (reject invalid).
   - Call `db_handler.upsert_wallet(guild_id, discord_user_id=user_id, chain='solana', address=wallet_address, metadata={'producer': 'admin_chat', 'reason': reason, 'triggered_by': 'upsert_wallet_for_user'})`.
   - On `WalletUpdateBlockedError` → return `{success: False, error: "Active payment intent in flight — wallet change blocked"}`.
   - On success: return a redacted wallet + explicit note that `verified_at=NULL` and will be set after a real test round-trip. The agent's reply tool surfaces the redacted confirmation to the admin.
3. **Wire** dispatch in `execute_tool`.

### Step 15: Register new tools in `ADMIN_ONLY_TOOLS` (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user` to `ADMIN_ONLY_TOOLS` at `:908-936`. Resolves **FLAG-004, all_locations-2** — without this, the `MEMBER_TOOLS | ADMIN_ONLY_TOOLS == ALL_TOOL_NAMES` assert at `:939` fires at import time.
2. **Verify** `MEMBER_TOOLS & ADMIN_ONLY_TOOLS == set()` still holds (they should be disjoint — no membership overlap).

### Step 16: Extend caller-side admin identity injection (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Modify** `agent.py:438-441` — the `initiate_payment` admin_user_id injection. Generalize to a set of tools that need admin identity:
   ```python
   _ADMIN_IDENTITY_INJECTED_TOOLS = frozenset({
       "initiate_payment",
       "initiate_batch_payment",
       "upsert_wallet_for_user",
   })
   if is_admin and tool_name in _ADMIN_IDENTITY_INJECTED_TOOLS and 'admin_user_id' not in tool_input:
       if tool_input is tool_use.input:
           tool_input = dict(tool_input)
       tool_input['admin_user_id'] = user_id
   ```
   Resolves **FLAG-004, callers-1**.
2. **Add** `initiate_batch_payment` to `_CHANNEL_POSTING_TOOLS` at `agent.py:21-26` (matching `initiate_payment`) to suppress duplicate chat-text replies.

### Step 17: System prompt updates (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Add** five new tool bullets to the `**Doing things:**` section after `get_payment_status` at `:77`:
   - `query_payment_state(payment_id?, user_id?)` — read-only canonical payment state.
   - `query_wallet_state(user_id)` — read-only wallet_registry rows for a user.
   - `list_recent_payments(producer?, user_id?, limit?=20)` — read-only recent payments, redacted.
   - `initiate_batch_payment(payments=[...])` — up to 20 admin-initiated payouts, all-or-none creation.
   - `upsert_wallet_for_user(user_id, wallet_address, chain?, reason?)` — register a wallet for a user; verification still requires a test payment round-trip.
2. **Add** a new subsection after `**Use the right route tools.**` (around `:101`):
   > **State questions → query first.** For any question about the state of a payment, wallet, or intent — call `query_payment_state`, `query_wallet_state`, or `list_recent_payments` FIRST. Do not claim state from memory or channel context. This is guidance, not enforcement.
3. **Do not** touch `MEMBER_SYSTEM_PROMPT` (dead code).

## Phase 6: Reconciliation, Timeout Sweep, Intent Resolution

### Step 18: Startup reconciliation (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Extend** `_reconcile_active_intents` at `:546-561`:
   - `awaiting_test_receipt_confirmation` → `_reconcile_intent_history` + replay via `_handle_pending_recipient_message` (not the deleted `_check_pending_payment_reply`).
   - `awaiting_admin_approval` → log count (no history scan; `AdminApprovalView` persistence via Step 10.3 handles restart recovery).
2. **Modify** `_reconcile_intent_history` at `:498-544` to call `_handle_pending_recipient_message(message, refreshed_intent)` per recipient message instead of `_check_pending_payment_reply(message)`. Re-fetch the intent per-message since state can change mid-iteration.
3. **Once** no caller remains, delete `_classify_payment_reply` and `_check_pending_payment_reply` in this same step (grep-verify first).

### Step 19: 24h timeout sweep (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `@tasks.loop(minutes=15) _sweep_stale_test_receipts` on `AdminChatCog`.
2. **Logic:** `cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()`; `db_handler.list_stale_test_receipt_intents(cutoff)`; for each, transition to `manual_review`, post admin tag in the source channel with full context (intent_id, recipient, test_payment_id, tx signature), DM admin via `_notify_intent_admin`. Wrap per-intent in try/except so one bad row doesn't kill the loop.
3. **Start** in `cog_load` after `_ensure_startup_reconciled`; **stop** in `cog_unload`.

### Step 20: Operator resolution surface for `manual_review` intents (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** a new admin tool `resolve_admin_intent(intent_id: str, resolution: 'cancelled' | 'completed', note?: str)`:
   - Admin-only. Defense-in-depth via `admin_user_id` injection (Step 16).
   - `db_handler.update_admin_payment_intent(intent_id, {'status': resolution}, guild_id)`.
   - On success, return the updated row (redacted) and DM admin confirmation via the agent's reply.
2. **Register** in `ADMIN_ONLY_TOOLS` (Step 15 — add to the list). Wire dispatch.
3. Combined with Step 2.5 (excluding `manual_review` from active-intent lookups), this fully resolves **FLAG-006 / scope-2**: `manual_review` does not block new intents AND the admin has a tool to explicitly mark it cancelled/completed for ledger hygiene.
4. **Note:** this is the MINIMUM operator surface. A fancier `/payment-resolve`-style slash command could follow later, but is out of scope.

## Phase 7: Tests + Verification

### Step 21: Rewrite `tests/test_admin_payments.py`
**Scope:** Large
1. **Remove** all tests that mock `_classify_payment_reply` — that code path is dead.
2. **Add** parser unit tests:
   - `test_parse_wallet_from_text_punctuation_wrapped` — wallet in backticks, quotes, parentheses, code block — all must extract.
   - `test_parse_wallet_from_text_rejects_non_addresses`.
   - `test_classify_confirmation_positive_parametrized` — all keywords incl. `👍`, mixed case, surrounded by punctuation.
   - `test_classify_confirmation_negative_parametrized`.
   - `test_classify_confirmation_ambiguous_when_both_or_neither`.
3. **Add** identity router tests (fail_to_pass):
   - `test_identity_router_admin_to_agent` — admin + Solana-address-shaped text → agent called, no state machine. ALSO: admin plain channel message without mention → agent called (proves `_is_directed_at_bot` gate was dropped — **FLAG-003**).
   - `test_identity_router_pending_recipient_to_state_machine` — `self._classifier_client = Mock(side_effect=AssertionError)` and assert state machine runs.
   - `test_identity_router_fallthrough`.
   - `test_approved_member_no_longer_routed_to_agent` — approved non-admin member with bot mention → agent NOT called.
4. **Add** state-machine tests: awaiting_wallet valid/ignore, test-receipt positive (→ `mark_wallet_verified`, real payment created, admin DM), negative (→ manual_review + admin tag), ambiguous (first silent, second escalates).
5. **Add** payment-gate tests:
   - `test_producer_flows_admin_chat_real_authorizes_admin_dm` — parameterized via `_authorize_actor`.
   - `test_admin_approval_view_only_admin_can_click` — non-admin interaction ephemeral-rejected.
   - `test_admin_approval_view_persists_across_restart` — `PaymentUICog.cog_load` re-registers views for every `awaiting_admin_approval` intent.
   - `test_admin_approval_click_queues_payment_and_posts_queued_message` — assert DM edit "queued for sending" (NOT "Payment sent"), follow-up DM, channel post, `safe_delete_messages` called with BOTH `prompt_message_id` and `receipt_prompt_message_id`, `confirm_payment` invoked with `ADMIN_DM` actor.
   - `test_admin_dm_fail_closed_on_forbidden`.
   - `test_payment_confirm_view_not_sent_for_admin_chat_real_payments`.
   - `test_payment_confirm_view_not_re_registered_for_admin_chat_on_restart` — primes an admin_chat real payment in `pending_confirmation`, runs `_register_pending_confirmation_views`, asserts `bot.add_view` was NOT called for it; grants pending_confirmation row still gets re-registered. Resolves **FLAG-001, all_locations-1**.
6. **Add** auto-skip + batch tests:
   - `test_auto_skip_verified_wallet_creates_real_payment_directly` — no `is_test=True` row, intent created directly in `awaiting_admin_approval`.
   - `test_initiate_batch_payment_atomic_all_or_none` — invalid element → zero DB writes; valid → all N intents, exactly N admin DMs, verified entries skip `awaiting_test`, unverified entries start in `awaiting_wallet`. Resolves **issue_hints-3**.
7. **Add** new-tool tests: `test_upsert_wallet_for_user_admin_only`, `test_upsert_wallet_for_user_respects_wallet_update_blocked`, `test_query_payment_state_read_only`, `test_query_wallet_state_read_only`, `test_query_tools_reject_llm_injected_wallet`.
8. **Add** lifecycle tests:
   - `test_manual_review_does_not_block_new_intents` — populate a `manual_review` intent for a recipient, call `get_active_intent_for_recipient` → returns `None`; call `initiate_payment` for the same recipient → succeeds.
   - `test_resolve_admin_intent_cancelled` — admin tool marks a `manual_review` intent cancelled.
   - `test_24h_timeout_sweep_escalates_stuck_test_receipt` — forge `updated_at < now - 24h`, run sweep, assert transition + admin DM.
9. **Add** infrastructure tests:
   - `test_admin_only_tools_includes_new_tools` — asserts the five new tools are in `ADMIN_ONLY_TOOLS` and the module-level assert still holds.
   - `test_agent_injects_admin_user_id_for_new_tools` — parameterized over `initiate_batch_payment` and `upsert_wallet_for_user`.

### Step 22: Validation runs
**Scope:** Small
1. **Parser unit tests first** — fastest feedback.
2. `pytest tests/test_admin_payments.py -x`.
3. **Invariant suites:** `pytest tests/test_payment_state_machine.py tests/test_payment_cog_split.py tests/test_solana_client.py tests/test_payment_race.py tests/test_payment_reconcile.py tests/test_payment_authorization.py tests/test_safe_delete_messages.py tests/test_check_payment_invariants.py tests/test_tx_signature_history.py`.
4. **Full suite:** `pytest`.
5. **Manual smoke (info-only):** dev-guild end-to-end — first-time flow, auto-skip flow, batch mixed verified/unverified, negative ack, ambiguous 2x, admin DM click, restart mid `awaiting_admin_approval`, 24h sweep, upsert success + blocked, grants unchanged, query tools, approved-member no-route.

## Execution Order
1. **Phase 1** (schema + DB helpers) — self-contained, lowest risk. Run migration in dev first.
2. **Phase 2** (producer_flows + restart-recovery filter) — small but CRITICAL: without Step 3 the `AdminApprovalView` click is rejected by `_authorize_actor`; without Step 4 `PaymentConfirmView` comes back on restart. These two plus Step 10 are the interlocked triple.
3. **Phase 3** (parsers + identity router) — unit-testable in isolation.
4. **Phase 4** (flow rewiring + `AdminApprovalView`) — core behavior change; relies on Phases 2 and 3.
5. **Phase 5** (new agent tools + infrastructure) — Steps 15 and 16 MUST land together with Step 12–14 or the module assert fires.
6. **Phase 6** (reconciliation + 24h sweep + resolve_admin_intent) — cleanup + ops surface.
7. **Phase 7** (tests + verification) — written alongside each phase; gating suite run at the end.

## Validation Order
1. **Unit tests:** parsers (`_parse_wallet_from_text` with punctuation-wrapped addresses; `_classify_confirmation` parametrized).
2. **Identity router + state machine tests** with Anthropic client mocked to raise.
3. **`producer_flows` authorization tests** — admin_chat real + `ADMIN_DM` now allowed; grants real + `ADMIN_DM` still rejected.
4. **Restart recovery filter test** — `PaymentConfirmView` NOT re-registered for admin_chat real payments; grants untouched.
5. **`AdminApprovalView`** — click handler posts "queued", not "sent"; deletes both prompt ids; `confirm_payment` invoked with `ADMIN_DM` actor.
6. **Batch atomicity** — verified path skips `awaiting_test`; invalid element → zero writes.
7. **Lifecycle tests** — `manual_review` does not block future intents; `resolve_admin_intent` works.
8. **Full `test_admin_payments.py`**.
9. **Invariant suites** (pass_to_pass).
10. **Full `pytest` run**.
11. **Manual end-to-end smoke** in dev guild.
