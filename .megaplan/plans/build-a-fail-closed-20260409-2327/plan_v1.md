# Implementation Plan: Generalized Fail-Closed Payment Subsystem

## Overview

The bot currently processes Solana grant payouts inline inside `GrantsCog._process_payment()`, with payment state baked into `grant_applications` columns (`payment_status`, `wallet_address`, `tx_signature`, etc.). This works but is non-reusable, tightly coupled, and has limited admin controls.

The goal is a standalone **payment subsystem** modeled on the existing `social_publications` queue pattern: a ledger table, atomic worker claiming via `FOR UPDATE SKIP LOCKED`, a provider abstraction layer, Discord confirmation flow, manual hold/retry admin controls, and restart-safe recovery. Grants becomes just a *producer* that enqueues payment requests.

**Critical safety invariant:** After a transaction is submitted to the chain, it is **never** automatically retried unless the chain definitively reports `failed`. Unknown/ambiguous status (`not_found`) freezes the payment to `manual_hold` — an admin must resolve it. Private keys never touch DB, logs, or admin chat.

**Repo patterns to follow:**
- `sql/social_publications.sql` — schema + `claim_due_*` RPC function template
- `src/features/sharing/sharing_cog.py` — `@tasks.loop` worker, claim → process → mark
- `src/features/sharing/social_publish_service.py` — service layer between producer and queue
- `src/common/db_handler.py` — Supabase client wrappers with `_gate_check()`
- `src/features/admin_chat/tools.py` — `QUERYABLE_TABLES` whitelist

---

## Phase 1: Database Foundation — Schema &amp; RPC

### Step 1: Create `sql/payments.sql` — ledger + wallet tables + claim RPC
**Scope:** Medium
1. **Create** `sql/payments.sql` with two tables and one RPC function:

   **`payment_requests` table** — the ledger:
   - `payment_id` UUID PK (gen_random_uuid)
   - `guild_id` bigint NOT NULL
   - `producer` text NOT NULL — e.g. `'grants'`, future: `'bounties'`, `'arccompute'`
   - `producer_ref` text NOT NULL — e.g. grant `thread_id`, links back to source
   - `recipient_wallet` text NOT NULL — Solana address
   - `amount_usd` numeric NOT NULL
   - `amount_token` numeric — computed SOL amount at send time
   - `token_price_usd` numeric — spot price at send time
   - `chain` text NOT NULL DEFAULT `'solana'` — future extensibility
   - `provider` text NOT NULL DEFAULT `'solana_native'`
   - `status` text NOT NULL DEFAULT `'pending_confirmation'` CHECK in (`'pending_confirmation'`, `'queued'`, `'processing'`, `'submitted'`, `'confirmed'`, `'failed'`, `'manual_hold'`, `'cancelled'`)
   - `is_test` boolean NOT NULL DEFAULT false — distinguishes test-payment from final
   - `attempt_count` integer NOT NULL DEFAULT 0
   - `tx_signature` text — set after chain submission
   - `last_error` text
   - `retry_after` timestamptz
   - `hold_reason` text — human note when in manual_hold
   - `confirmed_by` text — Discord user ID who confirmed (or 'auto')
   - `created_at`, `updated_at`, `completed_at` timestamptz
   - `deleted_at` timestamptz — soft delete

   **Key indexes:**
   - `idx_payment_requests_due_queue` on `(retry_after, created_at) WHERE status = 'queued' AND deleted_at IS NULL`
   - `idx_payment_requests_producer` on `(producer, producer_ref)`
   - Unique partial index on `(producer, producer_ref, is_test) WHERE status NOT IN ('failed', 'cancelled')` — prevents double-enqueue of active payments

   **`claim_due_payment_requests(claim_limit, claim_guild_ids)`** RPC function:
   - Mirrors `claim_due_social_publications` exactly — CTE with `FOR UPDATE SKIP LOCKED`, atomically sets `status = 'processing'`, increments `attempt_count`

2. **Note:** No wallet *registry* table in v1. Recipient wallets are just text on each payment request. A registry (mapping Discord users → verified wallets) can be added later when there are multiple producers that share recipients. For now, grants already collects wallets inline.

3. **Security:** `payment_requests` gets RLS enabled, anon/authenticated revoked (same pattern as `social_publications`). The table is **not** added to `QUERYABLE_TABLES` in admin chat — step 7 adds a dedicated redacted admin tool instead.

### Step 2: Add DB handler methods (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** methods mirroring the social publications pattern:
   - `create_payment_request(record, guild_id)` — insert with `_gate_check()`
   - `get_payment_request(payment_id)` — fetch single
   - `get_payment_requests_by_producer(producer, producer_ref)` — fetch by source
   - `claim_due_payment_requests(limit, guild_ids)` — RPC call to claim function
   - `mark_payment_submitted(payment_id, tx_signature, amount_token, token_price_usd, guild_id)` — set status='submitted', store tx sig
   - `mark_payment_confirmed(payment_id, guild_id)` — set status='confirmed', completed_at
   - `mark_payment_failed(payment_id, error, guild_id)` — set status='failed', completed_at
   - `mark_payment_manual_hold(payment_id, reason, guild_id)` — set status='manual_hold', hold_reason
   - `requeue_payment(payment_id, retry_after, guild_id)` — set status='queued', retry_after
   - `cancel_payment(payment_id, guild_id)` — set status='cancelled'
   - `get_inflight_payments_for_recovery()` — fetch status IN ('processing', 'submitted') for restart recovery

2. **Pattern:** Each method follows existing db_handler conventions — Supabase client calls, guild_id scoping, error logging, return dict or None.

---

## Phase 2: Provider Abstraction &amp; Payment Service

### Step 3: Create provider interface (`src/features/payments/provider.py`)
**Scope:** Small
1. **Create** `src/features/payments/__init__.py` (empty) and `src/features/payments/provider.py`
2. **Define** a simple abstract base:
   ```python
   class PaymentProvider(ABC):
       @abstractmethod
       async def send(self, recipient: str, amount_token: float) -> str:
           """Submit payment. Returns tx signature. Raises on failure."""
       
       @abstractmethod
       async def check_status(self, tx_signature: str) -> str:
           """Returns 'confirmed', 'failed', or 'not_found'."""
       
       @abstractmethod
       async def get_token_price_usd(self) -> float:
           """Current token price in USD."""
       
       @abstractmethod
       def token_name(self) -> str:
           """e.g. 'SOL'"""
   ```

### Step 4: Create Solana provider (`src/features/payments/solana_provider.py`)
**Scope:** Small
1. **Wrap** the existing `SolanaClient` (`src/features/grants/solana_client.py`) into a `SolanaProvider(PaymentProvider)`.
2. **Delegate** `send()` → `solana_client.send_sol()`, `check_status()` → `solana_client.check_tx_status()`, `get_token_price_usd()` → `pricing.get_sol_price_usd()`.
3. **Keep** `SolanaClient` unchanged — it still works, the provider is a thin adapter.
4. **Key:** The provider holds the `SolanaClient` instance in memory. The keypair never leaves the `SolanaClient` object. No serialization, no DB storage, no logging of key material.

### Step 5: Create payment service (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Create** `PaymentService` class — the core orchestrator between producers and the worker.
2. **Constructor** takes `db_handler` and a dict of `{provider_name: PaymentProvider}`.
3. **`request_payment(producer, producer_ref, guild_id, recipient_wallet, amount_usd, chain, provider, is_test)`** method:
   - Checks for existing active payment for same `(producer, producer_ref, is_test)` — returns it if found (idempotent).
   - Creates `payment_requests` row with status `'pending_confirmation'`.
   - Returns the payment record (caller can use payment_id for Discord confirmation).
4. **`confirm_payment(payment_id, confirmed_by, guild_id)`** method:
   - Transitions from `'pending_confirmation'` → `'queued'`. Sets `confirmed_by`.
   - For test payments with `is_test=True`, can auto-confirm (no Discord interaction needed).
5. **`execute_payment(payment_id)`** method — called by the worker after claiming:
   - Fetch payment record.
   - Resolve provider from `self.providers[record['provider']]`.
   - Fetch token price → compute `amount_token`.
   - Call `provider.send()` → get tx_signature.
   - Immediately `mark_payment_submitted(payment_id, tx_signature, ...)` — **this is the critical write-before-confirm pattern**.
   - Call `provider.check_status(tx_signature)`:
     - `'confirmed'` → `mark_payment_confirmed()`
     - `'failed'` → `mark_payment_failed()`
     - `'not_found'` → **`mark_payment_manual_hold()`** with reason "chain status ambiguous after submission" — **never auto-retry after submission**.
   - Returns result dict with status + tx_sig.
6. **`recover_inflight()`** method — called on startup:
   - Fetch all `status IN ('processing', 'submitted')`.
   - For `'processing'` with no `tx_signature`: requeue (crashed before send).
   - For `'submitted'` with `tx_signature`: check chain status:
     - `'confirmed'` → mark confirmed.
     - `'failed'` → mark failed (eligible for manual retry).
     - `'not_found'` → mark `manual_hold` — **fail closed**.

---

## Phase 3: Worker Loop &amp; Discord Integration

### Step 6: Create payment worker cog (`src/features/payments/payment_cog.py`)
**Scope:** Medium
1. **Create** `PaymentCog(commands.Cog)` following `sharing_cog.py` pattern:
   - `@tasks.loop(seconds=30)` worker that calls `db_handler.claim_due_payment_requests()` then processes each via `payment_service.execute_payment()`.
   - On startup (`cog_load`): call `payment_service.recover_inflight()`.
   - Configurable via env: `PAYMENT_CLAIM_LIMIT` (default 5), `PAYMENT_WORKER_INTERVAL` (default 30s).
2. **Discord confirmation flow** — when a producer calls `payment_service.request_payment()`, it gets back a payment record in `pending_confirmation` status. The producer (e.g. grants cog) sends a Discord message with a confirm button. The button handler calls `payment_service.confirm_payment()` to queue it.
   - For **test payments** (`is_test=True`): auto-confirm, no button needed. This lets grants do a dry-run to validate wallet + amount before the real payment.
   - For **final payments**: require explicit Discord confirmation from the applicant or admin.
3. **Result notifications:** After `execute_payment()` completes, the worker sends a DM or thread message with tx status + explorer link. On `manual_hold`, it pings the admin mention.

### Step 7: Admin controls — hold, retry, cancel (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** new admin chat tools (do NOT add `payment_requests` to `QUERYABLE_TABLES`):
   - `query_payments` — dedicated tool that returns payment records with `tx_signature` visible but **wallet addresses partially redacted** (show first 4 + last 4 chars). Never returns any key material (there is none in DB, but defense in depth).
   - `hold_payment` — sets a payment to `manual_hold` with a reason string.
   - `retry_payment` — requeues a `failed` or `manual_hold` payment (sets status='queued', clears tx_signature, increments attempt). **Only works if current status is `failed` or `manual_hold`** — cannot retry `submitted` (that would risk double-pay).
   - `cancel_payment` — sets status='cancelled'. Only from `pending_confirmation`, `queued`, `failed`, or `manual_hold`.
2. **Add** tool definitions to `TOOLS` list and handler functions.
3. **Key safety:** The `retry_payment` tool clears `tx_signature` before requeue so the worker treats it as a fresh attempt. A payment in `submitted` state cannot be retried — admin must first resolve to `failed` or `manual_hold` via chain check.

---

## Phase 4: Grants Producer Integration

### Step 8: Refactor grants to produce payment requests (`src/features/grants/grants_cog.py`)
**Scope:** Medium
1. **Replace** `_process_payment()` (lines 678-779) with a call to `payment_service.request_payment()`:
   - After wallet validation, create payment request: `payment_service.request_payment(producer='grants', producer_ref=str(thread_id), guild_id=..., recipient_wallet=wallet, amount_usd=total_usd, chain='solana', provider='solana_native', is_test=False)`.
   - Send Discord confirmation message with payment details + confirm button.
   - On confirm callback: `payment_service.confirm_payment(payment_id, confirmed_by=user_id)`.
2. **Replace** `_recover_inflight_payments()` (lines 87-193) — this is now handled by `PaymentCog.cog_load` → `payment_service.recover_inflight()`. Remove the grants-specific recovery code entirely.
3. **Update** `grant_applications` status flow:
   - Remove `payment_status`, `wallet_address`, `sol_amount`, `sol_price_usd`, `tx_signature` columns from grant usage (they can stay in the table for historical data but new payments go through `payment_requests`).
   - Grant statuses simplify to: `reviewing` → `needs_info`/`needs_review`/`rejected`/`spam` → `approved` → `awaiting_wallet` → `payment_requested` → `paid`.
   - `payment_requested`: set when `payment_service.request_payment()` succeeds. Store `payment_id` in a new `payment_ref` column on `grant_applications`.
   - `paid`: set when payment_cog notifies grants (via callback or event) that payment is confirmed.
4. **Wire** notification: payment_cog emits a Discord event or calls a callback when a payment reaches terminal state. Grants listens and updates its status + sends the thread message.
5. **Test payment flow:** When grant is approved and wallet submitted, first enqueue a test payment (`is_test=True`, amount = 0 or minimal dust amount like 0.000001 SOL). If test confirms, auto-enqueue the real payment with Discord confirmation. This validates the wallet is receivable before committing real funds.

### Step 9: Register cog and wire startup (`main.py`)
**Scope:** Small
1. **Instantiate** `SolanaProvider` wrapping the existing `SolanaClient`.
2. **Instantiate** `PaymentService` with db_handler and `{'solana_native': solana_provider}`.
3. **Load** `PaymentCog` with payment_service and db_handler.
4. **Update** `GrantsCog` constructor to accept `payment_service` instead of `solana_client` directly.

---

## Phase 5: Tests &amp; Validation

### Step 10: Unit tests (`tests/test_payment_service.py`)
**Scope:** Medium
1. **Create** `tests/test_payment_service.py` with FakeSupabase pattern from existing `tests/test_social_route_tools.py`:
   - Test idempotent `request_payment` — calling twice with same producer+ref returns same record.
   - Test `confirm_payment` transitions from `pending_confirmation` → `queued`.
   - Test `execute_payment` happy path: provider.send() → submitted → provider.check_status() → confirmed.
   - Test `execute_payment` ambiguous: provider.check_status() returns `not_found` → `manual_hold` (the critical fail-closed test).
   - Test `execute_payment` failure: provider.send() raises → `failed`.
   - Test `recover_inflight` with various stuck states.
   - Test admin controls: retry only from `failed`/`manual_hold`, cancel restrictions, no retry from `submitted`.
   - Test double-payment prevention: unique constraint prevents two active payments for same producer_ref.
2. **Run** `pytest tests/test_payment_service.py -v`.

### Step 11: Integration smoke test
**Scope:** Small
1. **Verify** existing grant tests (if any) still pass — grants_cog changes should not break assessment/review flow.
2. **Run** full `pytest` suite to check for regressions.
3. **Manual verification points** (documented for human tester):
   - Deploy to staging, create a grant application, approve it, submit wallet, observe test-payment → confirmation → real-payment flow.
   - Kill bot mid-payment, restart, verify recovery picks up correctly.
   - Use admin chat `hold_payment` / `retry_payment` / `cancel_payment` tools.

## Execution Order
1. **Phase 1 first** — DB schema is the foundation everything depends on.
2. **Phase 2 next** — provider abstraction and service layer, testable in isolation.
3. **Phase 3** — worker cog and admin tools, depends on Phase 2.
4. **Phase 4** — grants integration, depends on Phase 3.
5. **Phase 5 throughout** — write tests alongside each phase, run full suite at the end.

## Validation Order
1. SQL migration is syntactically valid (can be reviewed, applied to staging).
2. Unit tests for payment_service pass (Phase 2 + Phase 5 step 10).
3. Admin tools respond correctly to various payment states.
4. Grants cog compiles and loads without errors.
5. Full pytest suite green.
6. Manual smoke test on staging (info-level, cannot automate).
