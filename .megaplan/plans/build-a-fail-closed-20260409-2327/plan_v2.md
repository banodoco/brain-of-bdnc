# Implementation Plan: Generalized Fail-Closed Payment Subsystem

## Overview

The bot currently processes Solana grant payouts inline inside `GrantsCog._process_payment()` (`src/features/grants/grants_cog.py:678-779`), with payment state baked into `grant_applications` columns. This works but is non-reusable, tightly coupled, and has limited admin controls.

The goal is a standalone **payment subsystem** modeled on the existing `social_publications` queue pattern (`sql/social_publications.sql`): a payment ledger table, wallet registry, atomic worker claiming via `FOR UPDATE SKIP LOCKED`, a provider abstraction layer, Discord confirmation flow with restart-safe persistent views, manual admin controls (hold/release/retry/cancel/status), and restart-safe recovery. Grants becomes just a *producer* that enqueues payment requests.

**Critical safety invariant:** After a transaction is submitted to the chain, it is **never** automatically retried. The provider contract distinguishes pre-submit failures (safe to mark failed) from post-submit ambiguity (must freeze to `manual_hold`). Unknown/ambiguous status requires admin resolution via `release_payment` before retry is allowed. Private keys never touch DB, logs, or admin chat.

**Key design decisions (locked by user):**
- Test payment amount: fixed tiny global amount (not per-grant)
- Confirmation destination: route-configured per payment request (stored in ledger), not hardcoded to grant thread
- Admin controls: retry, hold, release, cancel, status — all required
- No auto-fallback from Codex to Claude on backend failures

**Repo patterns to follow:**
- `sql/social_publications.sql` — schema + `claim_due_*` RPC function template
- `src/features/sharing/sharing_cog.py` — `@tasks.loop` worker, claim → process → mark
- `src/features/sharing/social_publish_service.py` — service layer between producer and queue
- `src/common/db_handler.py` — Supabase client wrappers with `_gate_check()`
- `src/features/admin_chat/tools.py` — `QUERYABLE_TABLES` whitelist
- `src/features/admin_chat/agent.py:33-52` — hand-written tool catalog in system prompt

---

## Phase 1: Database Foundation — Schema &amp; RPC

### Step 1: Create `sql/payments.sql` — ledger + wallet registry + claim RPC
**Scope:** Medium

1. **Create** `sql/payments.sql` with three tables, one RPC function, and triggers. Follow the `sql/social_publications.sql` template exactly for structure (CREATE TABLE, indexes, RLS, triggers, RPC).

   **`wallet_registry` table** — maps Discord users to verified wallets:
   - `wallet_id` UUID PK (gen_random_uuid)
   - `guild_id` bigint NOT NULL
   - `discord_user_id` bigint NOT NULL
   - `chain` text NOT NULL DEFAULT `'solana'`
   - `address` text NOT NULL
   - `label` text — optional human name e.g. "main wallet"
   - `verified_at` timestamptz — null until a test payment confirms to this address
   - `created_at` timestamptz NOT NULL DEFAULT now()
   - `updated_at` timestamptz NOT NULL DEFAULT now()
   - Unique index on `(guild_id, discord_user_id, chain)` — one wallet per user per chain per guild

   **`payment_requests` table** — the ledger:
   - `payment_id` UUID PK (gen_random_uuid)
   - `guild_id` bigint NOT NULL
   - `producer` text NOT NULL — e.g. `'grants'`, future: `'bounties'`, `'arccompute'`
   - `producer_ref` text NOT NULL — e.g. grant `thread_id`, links back to source
   - `recipient_discord_id` bigint — optional, links to wallet_registry
   - `recipient_wallet` text NOT NULL — Solana address (denormalized from registry or direct)
   - `wallet_id` UUID REFERENCES wallet_registry(wallet_id) — optional FK
   - `amount_usd` numeric NOT NULL
   - `amount_token` numeric — computed SOL amount at send time
   - `token_price_usd` numeric — spot price at send time
   - `chain` text NOT NULL DEFAULT `'solana'`
   - `provider` text NOT NULL DEFAULT `'solana_native'`
   - `status` text NOT NULL DEFAULT `'pending_confirmation'` CHECK in (`'pending_confirmation'`, `'queued'`, `'processing'`, `'submitted'`, `'confirmed'`, `'failed'`, `'manual_hold'`, `'cancelled'`)
   - `is_test` boolean NOT NULL DEFAULT false — distinguishes test-payment from final
   - `attempt_count` integer NOT NULL DEFAULT 0
   - `tx_signature` text — set after chain submission
   - `send_phase` text — `'pre_submit'`, `'submitted'`, `'ambiguous'` — records what happened during last send attempt, drives whether auto-retry is safe
   - `last_error` text
   - `retry_after` timestamptz
   - `hold_reason` text — human note when in manual_hold
   - `confirmed_by` text — Discord user ID who confirmed (or 'auto' for test payments)
   - `confirm_channel_id` bigint — where to post the confirmation button/message
   - `confirm_thread_id` bigint — optional thread within confirm channel
   - `notify_channel_id` bigint — where to post payment result notifications
   - `notify_thread_id` bigint — optional thread within notify channel
   - `metadata` jsonb NOT NULL DEFAULT '{}'::jsonb — producer-specific data bag (e.g. grant details for the notification message)
   - `created_at`, `updated_at`, `completed_at` timestamptz
   - `deleted_at` timestamptz — soft delete

   **Key indexes:**
   - `idx_payment_requests_due_queue` on `(retry_after, created_at) WHERE status = 'queued' AND deleted_at IS NULL`
   - `idx_payment_requests_producer` on `(producer, producer_ref)`
   - Unique partial index on `(producer, producer_ref, is_test) WHERE status NOT IN ('failed', 'cancelled')` — prevents double-enqueue of active payments

   **`claim_due_payment_requests(claim_limit, claim_guild_ids)`** RPC function:
   - Mirror `claim_due_social_publications` exactly (`sql/social_publications.sql:93-126`) — CTE with `FOR UPDATE SKIP LOCKED`, atomically sets `status = 'processing'`, increments `attempt_count`.

   **RLS &amp; permissions:** Both tables get RLS enabled, anon/authenticated revoked. Neither table is added to `QUERYABLE_TABLES`.

   **Triggers:** `updated_at` trigger on both tables, same pattern as `social_publications.sql:3-11`.

2. **Note on schema placement:** Follow the `sql/` directory pattern used by `social_publications.sql`. This is the live reference-script convention. The `supabase/migrations/` directory also exists for formal migrations but the reference script in `sql/` is the authoritative template.

### Step 2: Add DB handler methods (`src/common/db_handler.py`)
**Scope:** Medium

1. **Add wallet registry methods:**
   - `upsert_wallet(guild_id, discord_user_id, chain, address)` — insert or update, returns wallet record
   - `get_wallet(guild_id, discord_user_id, chain)` — fetch registered wallet
   - `mark_wallet_verified(wallet_id, guild_id)` — set `verified_at` to now

2. **Add payment request methods** mirroring the social publications pattern:
   - `create_payment_request(record, guild_id)` — insert with `_gate_check()`
   - `get_payment_request(payment_id)` — fetch single
   - `get_payment_requests_by_producer(producer, producer_ref, is_test=None)` — fetch by source, optionally filter by is_test
   - `claim_due_payment_requests(limit, guild_ids)` — RPC call to claim function
   - `mark_payment_submitted(payment_id, tx_signature, amount_token, token_price_usd, send_phase, guild_id)` — set status='submitted', store tx sig and send_phase
   - `mark_payment_confirmed(payment_id, guild_id)` — set status='confirmed', completed_at
   - `mark_payment_failed(payment_id, error, send_phase, guild_id)` — set status='failed', send_phase, completed_at. **Only callable when send_phase='pre_submit'** (definitive pre-send failure).
   - `mark_payment_manual_hold(payment_id, reason, guild_id)` — set status='manual_hold', hold_reason
   - `requeue_payment(payment_id, retry_after, guild_id)` — set status='queued', clear tx_signature, clear send_phase, set retry_after. **Precondition check: current status must be 'failed'** — never from 'manual_hold' or 'submitted'.
   - `release_payment_hold(payment_id, new_status, guild_id)` — transitions from 'manual_hold' to either 'confirmed', 'failed', or back to 'manual_hold'. This is the only path out of manual_hold.
   - `cancel_payment(payment_id, guild_id)` — set status='cancelled'. Only from `pending_confirmation`, `queued`, `failed`, or `manual_hold`.
   - `get_inflight_payments_for_recovery()` — fetch status IN ('processing', 'submitted') for restart recovery
   - `get_pending_confirmation_payments(guild_ids)` — fetch status='pending_confirmation' for button re-registration on restart

3. **Pattern:** Each method follows existing db_handler conventions — Supabase client calls, guild_id scoping, error logging, return dict or None.

---

## Phase 2: Provider Abstraction &amp; Payment Service

### Step 3: Create provider interface (`src/features/payments/provider.py`)
**Scope:** Small

1. **Create** `src/features/payments/__init__.py` (empty) and `src/features/payments/provider.py`.
2. **Define** a structured send result:
   ```python
   @dataclass
   class SendResult:
       signature: Optional[str]  # tx sig if we got one back
       phase: str  # 'pre_submit' | 'submitted' | 'ambiguous'
       error: Optional[str]  # error message if any
   ```
   - `phase='pre_submit'` + error → definitive failure before tx was broadcast (bad address, insufficient balance). Safe to mark `failed` and allow retry.
   - `phase='submitted'` + signature → tx was broadcast, got signature back. Proceed to confirmation.
   - `phase='ambiguous'` + error → network error or exception *after* tx may have been broadcast. **Must freeze to `manual_hold`** — never auto-retry.

3. **Define** the abstract provider:
   ```python
   class PaymentProvider(ABC):
       @abstractmethod
       async def send(self, recipient: str, amount_token: float) -> SendResult:
           """Submit payment. Returns SendResult with phase indicator."""

       @abstractmethod
       async def confirm_tx(self, tx_signature: str) -> str:
           """Wait for tx confirmation. Returns 'confirmed', 'failed', or 'timeout'."""

       @abstractmethod
       async def check_status(self, tx_signature: str) -> str:
           """One-shot status check (for recovery). Returns 'confirmed', 'failed', or 'not_found'."""

       @abstractmethod
       async def get_token_price_usd(self) -> float:
           """Current token price in USD."""

       @abstractmethod
       def token_name(self) -> str:
           """e.g. 'SOL'"""
   ```

   **Key difference from v1:** The `send()` method returns `SendResult` instead of a bare signature string, and `confirm_tx()` is a separate method that polls/waits (wrapping `SolanaClient.confirm_tx`), distinct from the one-shot `check_status()` used only for recovery.

### Step 4: Create Solana provider (`src/features/payments/solana_provider.py`)
**Scope:** Small

1. **Wrap** the existing `SolanaClient` (`src/features/grants/solana_client.py`) into `SolanaProvider(PaymentProvider)`.
2. **`send()` implementation** — the critical safety wrapper:
   - Catch pre-send errors (address validation, balance check at `solana_client.py:70-75`) and return `SendResult(signature=None, phase='pre_submit', error=str(e))`.
   - For the actual `send_sol()` call: wrap in try/except. If `send_sol()` returns a signature, return `SendResult(signature=sig, phase='submitted', error=None)`. If `send_sol()` raises, we cannot know if the tx was broadcast (the exception could be a network timeout after broadcast — see `solana_client.py:97-114` where exceptions from `send_transaction` don't prove the chain rejected). Return `SendResult(signature=None, phase='ambiguous', error=str(e))`.
   - **Implementation detail:** To distinguish pre-submit from ambiguous, split the `send_sol` logic: do balance check separately (pre-submit failures), then call `send_sol`. Any exception from `send_sol` itself is ambiguous because the retry loop at `solana_client.py:85-114` may have successfully submitted on an earlier attempt before a later retry raised.
3. **`confirm_tx()`** → delegates to `solana_client.confirm_tx()` (`solana_client.py:118-124`). Returns `'confirmed'` on success, `'timeout'` if the await times out, `'failed'` if chain reports error.
4. **`check_status()`** → delegates to `solana_client.check_tx_status()` (`solana_client.py:126-140`). Returns `'confirmed'`, `'failed'`, or `'not_found'`.
5. **`get_token_price_usd()`** → delegates to `pricing.get_sol_price_usd()`.
6. **Keep** `SolanaClient` unchanged — it still works, the provider is a thin adapter.
7. **Key:** The provider holds the `SolanaClient` instance in memory. The keypair never leaves the `SolanaClient` object. No serialization, no DB storage, no logging of key material.

### Step 5: Create payment service (`src/features/payments/payment_service.py`)
**Scope:** Medium

1. **Create** `PaymentService` class — the core orchestrator between producers and the worker.
2. **Constructor** takes `db_handler`, a dict of `{provider_name: PaymentProvider}`, and a `test_payment_amount` float (the fixed tiny global test amount, e.g. 0.000001 SOL equivalent in USD).
3. **`request_payment(producer, producer_ref, guild_id, recipient_wallet, amount_usd, chain, provider, is_test, recipient_discord_id=None, wallet_id=None, confirm_channel_id, confirm_thread_id=None, notify_channel_id, notify_thread_id=None, metadata=None)`** method:
   - Checks for existing active payment for same `(producer, producer_ref, is_test)` — returns it if found (idempotent).
   - For `is_test=True`, overrides `amount_usd` to the fixed global test amount.
   - Creates `payment_requests` row with status `'pending_confirmation'` and all routing columns populated.
   - Returns the payment record (caller uses payment_id for Discord confirmation flow).
4. **`confirm_payment(payment_id, confirmed_by, guild_id)`** method:
   - Transitions from `'pending_confirmation'` → `'queued'`. Sets `confirmed_by`.
   - For test payments (`is_test=True`), the caller may pass `confirmed_by='auto'` to auto-confirm without a button interaction.
5. **`execute_payment(payment_id)`** method — called by the worker after claiming:
   - Fetch payment record.
   - Resolve provider from `self.providers[record['provider']]`.
   - Fetch token price → compute `amount_token` from `amount_usd`.
   - Call `provider.send(recipient_wallet, amount_token)` → get `SendResult`.
   - **If `result.phase == 'pre_submit'`:** definitive pre-send failure → `mark_payment_failed(payment_id, result.error, send_phase='pre_submit')`. This payment can be retried via admin.
   - **If `result.phase == 'ambiguous'`:** post-send ambiguity → `mark_payment_manual_hold(payment_id, reason=f"Ambiguous send error: {result.error}")`. **Never auto-retry.**
   - **If `result.phase == 'submitted'`:** tx was broadcast.
     - Immediately `mark_payment_submitted(payment_id, result.signature, amount_token, token_price_usd, send_phase='submitted')` — **write tx_sig to DB before doing anything else**.
     - Call `provider.confirm_tx(result.signature)` — this polls/waits for confirmation (wraps `SolanaClient.confirm_tx` which calls `client.confirm_transaction`).
       - `'confirmed'` → `mark_payment_confirmed()`. If wallet_id present, call `mark_wallet_verified(wallet_id)`.
       - `'failed'` → `mark_payment_failed(payment_id, 'chain rejected', send_phase='submitted')` — note: even though status is 'failed', the `send_phase='submitted'` means admin must be cautious.
       - `'timeout'` → `mark_payment_manual_hold(payment_id, reason='confirmation timed out after submission')` — **fail closed, never auto-retry**.
   - Returns result dict with status + tx_sig.
6. **`recover_inflight()`** method — called on startup:
   - Fetch all `status IN ('processing', 'submitted')`.
   - For `'processing'` with no `tx_signature`: crashed before send → requeue (safe because nothing was submitted).
   - For `'processing'` with `send_phase='ambiguous'`: crashed after ambiguous send → `manual_hold`.
   - For `'submitted'` with `tx_signature`: check chain status via `provider.check_status()` (one-shot, with `search_transaction_history=True` as the existing client does at `solana_client.py:133`):
     - `'confirmed'` → mark confirmed.
     - `'failed'` → mark failed.
     - `'not_found'` → mark `manual_hold` — **fail closed**. (This is recovery, not immediate post-submit, so `not_found` is genuinely ambiguous here.)
7. **`get_pending_confirmations(guild_ids)`** method — returns `pending_confirmation` payments for button re-registration.

---

## Phase 3: Worker Loop &amp; Discord Integration

### Step 6: Create payment worker cog (`src/features/payments/payment_cog.py`)
**Scope:** Medium

1. **Create** `PaymentCog(commands.Cog)` following `sharing_cog.py` pattern:
   - `@tasks.loop(seconds=30)` worker that calls `db_handler.claim_due_payment_requests()` then processes each via `payment_service.execute_payment()`.
   - Configurable via env: `PAYMENT_CLAIM_LIMIT` (default 5), `PAYMENT_WORKER_INTERVAL` (default 30s).
2. **Startup recovery** in `cog_load`:
   - Call `payment_service.recover_inflight()`.
   - Call `payment_service.get_pending_confirmations(guild_ids)` and re-register persistent views for each (see step 6.4).
3. **Discord confirmation flow** — when a producer calls `payment_service.request_payment()`, it gets back a payment record. The producer (e.g. grants cog) creates a `PaymentConfirmView` and sends it to the `confirm_channel_id`/`confirm_thread_id` from the payment record.
4. **Persistent views for restart safety:**
   - Define `PaymentConfirmView(discord.ui.View)` with `timeout=None` and a `custom_id` derived from the payment_id (e.g. `f"payment_confirm:{payment_id}"`).
   - On `cog_load`, call `bot.add_view(PaymentConfirmView(payment_id=pid))` for each `pending_confirmation` payment. This re-registers the button handler so it survives bot restarts.
   - The confirm button callback calls `payment_service.confirm_payment(payment_id, confirmed_by=interaction.user.id)`.
5. **Result notifications:** After `execute_payment()` completes, the worker sends a message to `notify_channel_id`/`notify_thread_id` with tx status + explorer link. On `manual_hold`, it pings the configured admin mention.
6. **Test payment auto-confirm:** For `is_test=True` payments, the producer calls `payment_service.confirm_payment(payment_id, confirmed_by='auto')` immediately after request — no Discord button needed. A notification is still sent to the notification target when the test completes.

### Step 7: Admin controls (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium

1. **Add** new admin chat tools (do NOT add `payment_requests` or `wallet_registry` to `QUERYABLE_TABLES`):
   - `query_payments(filters)` — dedicated tool that returns payment records with `tx_signature` visible but **wallet addresses partially redacted** (show first 4 + last 4 chars). Returns status, producer, amount, timestamps, hold_reason. Never returns any key material.
   - `check_payment_status(payment_id)` — re-checks chain for any payment with a `tx_signature` (status `submitted` or `manual_hold`). Calls `provider.check_status()` and reports current on-chain state without changing DB state. Admin uses this to decide next action.
   - `hold_payment(payment_id, reason)` — sets a payment to `manual_hold` with a reason string. Works from any non-terminal status.
   - `release_payment(payment_id, resolution)` — **the only path out of `manual_hold`**. Admin provides `resolution`: `'confirmed'` (payment went through — mark confirmed), `'failed'` (payment definitively did not go through — mark failed, now eligible for retry), or `'hold'` (stay on hold with updated reason). This prevents accidental retry of ambiguously-submitted payments.
   - `retry_payment(payment_id)` — requeues a `failed` payment (sets status='queued', clears tx_signature and send_phase). **Only works from `failed` status** — cannot retry from `manual_hold` or `submitted`. Admin must first `release_payment` with `resolution='failed'` to move from hold to failed before retrying.
   - `cancel_payment(payment_id, reason)` — sets status='cancelled'. Only from `pending_confirmation`, `queued`, `failed`, or `manual_hold`.
   - `query_wallets(discord_user_id?, guild_id?)` — lists registered wallets with verified status.

2. **Update** `src/features/admin_chat/agent.py` system prompt (`agent.py:33-52`) to include the new payment tools in the hand-written tool catalog. Add under a new **Payments:** heading:
   ```
   - query_payments — search payment ledger. Wallet addresses are redacted. Filters: payment_id, producer, producer_ref, status, is_test.
   - check_payment_status — re-check on-chain status for a submitted/held payment. Read-only.
   - hold_payment — freeze a payment to manual_hold with a reason.
   - release_payment — resolve a manual_hold: confirmed (went through), failed (did not), or hold (keep holding).
   - retry_payment — requeue a failed payment for another attempt. Only works from failed status.
   - cancel_payment — cancel a pending/queued/failed/held payment.
   - query_wallets — list registered wallets for a user.
   ```

3. **Add** tool definitions to `TOOLS` list and handler functions in `tools.py`.

4. **Key safety:** The state machine enforced by the DB handler methods means:
   - `manual_hold` → can only go to `confirmed`, `failed`, or stay on `hold` via `release_payment`
   - `failed` → can go to `queued` via `retry_payment`, or `cancelled` via `cancel_payment`
   - `submitted` → cannot be retried or cancelled directly; admin must wait or use `hold_payment` first

---

## Phase 4: Grants Producer Integration

### Step 8: Refactor grants to produce payment requests (`src/features/grants/grants_cog.py`)
**Scope:** Medium

1. **Replace** `_process_payment()` (`grants_cog.py:678-779`) with calls to the payment service:
   - After wallet validation (`grants_cog.py:367-380`), register wallet in registry: `db.upsert_wallet(guild_id, applicant_discord_id, 'solana', wallet)`.
   - Enqueue test payment: `payment_service.request_payment(producer='grants', producer_ref=str(thread_id), guild_id=..., recipient_wallet=wallet, amount_usd=test_amount, is_test=True, recipient_discord_id=applicant_id, wallet_id=wallet_record['wallet_id'], confirm_channel_id=thread.parent_id, confirm_thread_id=thread.id, notify_channel_id=thread.parent_id, notify_thread_id=thread.id, metadata={'grant_type': grant['gpu_type'], 'total_usd': grant['total_cost_usd']})`.
   - Auto-confirm test: `payment_service.confirm_payment(test_payment_id, confirmed_by='auto')`.
   - The payment worker processes the test payment. On success, the notification handler (in payment_cog) triggers the real payment:
     - `payment_service.request_payment(producer='grants', producer_ref=str(thread_id), ..., is_test=False, amount_usd=grant['total_cost_usd'])`.
     - Send `PaymentConfirmView` button to the confirmation target channel/thread.
   - On final payment confirmed: notification handler updates grant status to `paid` and archives thread.

2. **Replace** `_recover_inflight_payments()` (`grants_cog.py:87-193`) — remove entirely. Recovery is now handled by `PaymentCog.cog_load` → `payment_service.recover_inflight()`.

3. **Update** `grant_applications` status flow:
   - Old columns (`payment_status`, `wallet_address`, `sol_amount`, `sol_price_usd`, `tx_signature`) left in table for historical data. New payments flow through `payment_requests` exclusively.
   - Grant statuses: `reviewing` → `needs_info`/`needs_review`/`rejected`/`spam` → `approved` → `awaiting_wallet` → `payment_requested` → `paid`.
   - `payment_requested`: set when `payment_service.request_payment()` succeeds for the test payment. No `payment_ref` column needed — the payment ledger is queried by `(producer='grants', producer_ref=thread_id)`.
   - `paid`: set when the payment_cog notification reports the final (non-test) payment as confirmed.

4. **Update active-grant checks** (`src/common/db_handler.py:2034-2048`):
   - Change `get_active_grants_for_applicant()` to include `payment_requested` in the active status list: `.in_('status', ['reviewing', 'awaiting_wallet', 'payment_requested'])`.
   - This prevents duplicate grant applications while payment is in progress.

5. **Update assessor prompt** (`src/features/grants/assessor.py:65`):
   - Change the active-grant description to: `"Be VERY hesitant to approve someone who already has an open/active grant (status: reviewing, awaiting_wallet, payment_requested)."`

6. **Wire payment result callback:** PaymentCog listens for confirmed/failed payments where `producer='grants'`. On confirmed final payment → update grant to `paid`, send thread message, archive. On failed → send thread message with admin mention.

### Step 9: Register cog and wire startup (`main.py`)
**Scope:** Small

1. **Instantiate** `SolanaProvider` wrapping the existing `SolanaClient`.
2. **Instantiate** `PaymentService` with db_handler, `{'solana_native': solana_provider}`, and `test_payment_amount` from env (e.g. `PAYMENT_TEST_AMOUNT_USD`, default 0.001).
3. **Load** `PaymentCog` with payment_service, db_handler, and bot reference.
4. **Update** `GrantsCog` constructor to accept `payment_service` instead of `solana_client` directly.

---

## Phase 5: Tests &amp; Validation

### Step 10: Unit tests (`tests/test_payment_service.py`)
**Scope:** Medium

1. **Create** `tests/test_payment_service.py` with FakeSupabase pattern from existing `tests/test_social_route_tools.py`:
   - Test idempotent `request_payment` — calling twice with same producer+ref+is_test returns same record.
   - Test `confirm_payment` transitions from `pending_confirmation` → `queued`.
   - Test `execute_payment` happy path: provider.send() returns `submitted` → confirm_tx returns `confirmed` → payment confirmed.
   - Test `execute_payment` pre-submit failure: provider.send() returns `pre_submit` error → payment marked `failed`.
   - Test `execute_payment` ambiguous: provider.send() returns `ambiguous` → payment marked `manual_hold` (**critical fail-closed test**).
   - Test `execute_payment` confirm timeout: provider.send() returns `submitted`, confirm_tx returns `timeout` → `manual_hold` (**critical fail-closed test**).
   - Test `recover_inflight`: processing with no tx_sig → requeue; processing with ambiguous phase → hold; submitted with tx_sig → check chain → confirm/fail/hold.
   - Test admin controls: `retry_payment` only from `failed` (not `manual_hold`, not `submitted`). `release_payment` only from `manual_hold`. `cancel_payment` blocked from `submitted` and `confirmed`.
   - Test double-payment prevention: unique constraint prevents two active payments for same producer+ref+is_test.
   - Test wallet registry: upsert, get, verify.
   - Test routing columns: `request_payment` stores confirm/notify channel/thread IDs correctly.
2. **Run** `pytest tests/test_payment_service.py -v`.

### Step 11: Integration smoke test
**Scope:** Small

1. **Verify** existing grant tests (if any) still pass — grants_cog changes should not break assessment/review flow.
2. **Run** full `pytest` suite to check for regressions.
3. **Manual verification points** (documented for human tester):
   - Deploy to staging, create a grant application, approve it, submit wallet, observe test-payment → auto-confirm → real-payment → button confirm → confirmed flow.
   - Kill bot mid-payment, restart, verify recovery picks up correctly and persistent views re-register.
   - Use admin chat: `query_payments`, `check_payment_status`, `hold_payment`, `release_payment` (with all three resolutions), `retry_payment`, `cancel_payment`.
   - Verify an applicant cannot open a second grant while payment is in `payment_requested` status.

## Execution Order
1. **Phase 1 first** — DB schema is the foundation everything depends on.
2. **Phase 2 next** — provider abstraction and service layer, testable in isolation.
3. **Phase 3** — worker cog and admin tools, depends on Phase 2.
4. **Phase 4** — grants integration, depends on Phase 3.
5. **Phase 5 throughout** — write tests alongside each phase, run full suite at the end.

## Validation Order
1. SQL schema is syntactically valid (review, apply to staging).
2. Unit tests for payment_service pass (Phase 2 + Phase 5 step 10).
3. Admin tools respond correctly to various payment states, and are reachable from admin chat (agent prompt updated).
4. Persistent views re-register on restart for pending_confirmation payments.
5. Grants cog compiles and loads without errors; active-grant checks include `payment_requested`.
6. Full pytest suite green.
7. Manual smoke test on staging (info-level, cannot automate).
