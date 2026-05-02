# Implementation Plan: Generalized Solana-First Payment Ledger & Service

## Overview

The bot currently handles grant payouts directly inside `GrantsCog` — pricing, wallet collection, SOL sending, confirmation polling, and crash recovery are all coupled in one cog. The goal is to extract a reusable payment subsystem modeled after the `social_publications` pattern: a dedicated payment table with atomic claim semantics, a provider interface (Solana first), stable wallet-key routing, an append-only attempt log, and a safe status machine with crash-recovery guarantees. Grants becomes a *producer* that collects the recipient wallet, optional test-payment confirmation, and explicit Discord "yes" before enqueuing a payout into the new service.

**Key constraints:** no double payments under any crash/restart scenario; fail-closed write gating; payment data excluded from broad admin-chat query surfaces; provider-extensible but Solana-only at launch.

---

## Phase 1: Foundation — Schema, Types, DB Methods

### Step 1: Create payment tables SQL (`sql/payments.sql`)
**Scope:** Medium

1. **Create** `payments` table mirroring `social_publications` structure:
   - `payment_id` UUID PK (gen_random_uuid)
   - `guild_id` BIGINT NOT NULL
   - `source_kind` TEXT NOT NULL (e.g. `'grant'`, future: `'bounty'`)
   - `source_ref` TEXT NOT NULL (e.g. thread_id for grants — links back to producer)
   - `wallet_key` TEXT NOT NULL (stable key into a wallet registry, e.g. `'grants_main'`)
   - `provider` TEXT NOT NULL (e.g. `'solana'`)
   - `recipient_address` TEXT NOT NULL
   - `amount_crypto` NUMERIC NOT NULL
   - `amount_usd` NUMERIC NOT NULL
   - `crypto_price_usd` NUMERIC NOT NULL (captured rate at enqueue time)
   - `status` TEXT NOT NULL DEFAULT `'queued'` CHECK in (`'queued'`, `'processing'`, `'submitted'`, `'confirmed'`, `'failed'`, `'cancelled'`)
   - `attempt_count` INTEGER NOT NULL DEFAULT 0
   - `retry_after` TIMESTAMPTZ
   - `last_error` TEXT
   - `tx_signature` TEXT (set on submission)
   - `provider_ref` TEXT (provider-specific confirmation data)
   - `created_at`, `updated_at`, `completed_at` TIMESTAMPTZ
   - Indexes: `(guild_id, status)`, `(source_kind, source_ref)`, partial on `status='queued'` for claim query

2. **Create** `payment_attempts` append-only table:
   - `attempt_id` UUID PK
   - `payment_id` UUID FK → payments
   - `attempt_number` INTEGER NOT NULL
   - `action` TEXT NOT NULL (`'submit'`, `'confirm_check'`, `'recovery_check'`)
   - `tx_signature` TEXT
   - `result` TEXT (`'submitted'`, `'confirmed'`, `'failed'`, `'not_found'`, `'error'`)
   - `error_detail` TEXT
   - `created_at` TIMESTAMPTZ DEFAULT now()
   - Index: `(payment_id, attempt_number)`

3. **Create** `claim_due_payments` Supabase RPC function:
   - Same pattern as `claim_due_social_publications`: SELECT ... FOR UPDATE SKIP LOCKED, atomically set `status='processing'`, increment `attempt_count`, return rows.
   - Filter: `status='queued'`, `retry_after IS NULL OR retry_after <= now()`.

### Step 2: Create payment models (`src/features/payments/models.py`)
**Scope:** Small

1. **Define** dataclasses mirroring the social publishing pattern:
   - `PaymentRequest`: guild_id, source_kind, source_ref, wallet_key, provider, recipient_address, amount_crypto, amount_usd, crypto_price_usd
   - `PaymentResult`: payment_id, success, error, tx_signature, provider_ref
   - `PaymentAttempt`: attempt_id, payment_id, attempt_number, action, tx_signature, result, error_detail

### Step 3: Add DB methods to `DatabaseHandler` (`src/common/db_handler.py`)
**Scope:** Medium

1. **Add** `create_payment(payment_request) -> str` — inserts row, returns payment_id. Checks `is_write_allowed` before insert.
2. **Add** `claim_due_payments(limit, guild_ids) -> List[Dict]` — calls the RPC function.
3. **Add** `update_payment_status(payment_id, status, **kwargs) -> bool` — same pattern as `update_grant_status`.
4. **Add** `mark_payment_submitted(payment_id, tx_signature) -> bool` — sets status='submitted', tx_signature.
5. **Add** `mark_payment_confirmed(payment_id, provider_ref=None) -> bool` — sets status='confirmed', completed_at.
6. **Add** `mark_payment_failed(payment_id, last_error, retry_after=None) -> bool` — sets status='failed' or back to 'queued' if retry_after provided.
7. **Add** `record_payment_attempt(payment_id, attempt_number, action, tx_signature, result, error_detail) -> str`.
8. **Add** `get_payment_by_source(source_kind, source_ref) -> Optional[Dict]` — lookup by producer reference.
9. **Add** `get_inflight_payments_v2(guild_ids=None) -> List[Dict]` — returns payments where status in ('processing', 'submitted').
10. **Do NOT** add `payments` or `payment_attempts` to `QUERYABLE_TABLES` in admin_chat/tools.py — payment data stays off the broad query surface.

---

## Phase 2: Provider Interface & Solana Provider

### Step 4: Create payment provider interface (`src/features/payments/providers/__init__.py`)
**Scope:** Small

1. **Define** `PaymentProvider` ABC:
   ```python
   class PaymentProvider(ABC):
       @abstractmethod
       async def send(self, recipient_address: str, amount: float) -> Dict[str, Any]:
           """Returns {'tx_signature': str}. Raises on failure."""
       
       @abstractmethod
       async def check_status(self, tx_signature: str) -> str:
           """Returns 'confirmed', 'failed', or 'not_found'."""
       
       @abstractmethod
       def validate_address(self, address: str) -> bool:
           """Returns True if address is valid for this provider."""
   ```

### Step 5: Create Solana provider (`src/features/payments/providers/solana_provider.py`)
**Scope:** Medium

1. **Wrap** existing `SolanaClient` functionality into `SolanaProvider(PaymentProvider)`.
2. **Constructor** takes a `Keypair` (not env var name) — the caller resolves wallet key to keypair.
3. **Implement** `send()` using existing `send_sol` logic (balance check, versioned tx, blockhash retry).
4. **Implement** `check_status()` using existing `check_tx_status` logic.
5. **Implement** `validate_address()` using existing `is_valid_solana_address`.
6. Keep `SolanaClient` in place for now — `SolanaProvider` delegates to it or inlines the logic. Avoid breaking the existing grants path until Step 8.

### Step 6: Implement wallet-key registry (`src/features/payments/wallet_registry.py`)
**Scope:** Small

1. **Create** a simple registry that maps stable wallet keys (e.g. `'grants_main'`) to env var names (e.g. `SOLANA_PRIVATE_KEY`).
2. **Function** `get_provider_for_wallet(wallet_key: str, provider_name: str) -> PaymentProvider` — looks up the env var, constructs keypair, returns a `SolanaProvider`.
3. **Design:** Start with a hardcoded dict; this can later move to DB/config. The key invariant is that payment records reference the stable wallet_key string, not the raw secret.

---

## Phase 3: Payment Service & Recovery

### Step 7: Create payment service (`src/features/payments/payment_service.py`)
**Scope:** Large

1. **Create** `PaymentService` class (mirrors `SocialPublishService`):
   ```python
   class PaymentService:
       def __init__(self, db_handler, wallet_registry):
           ...
       
       async def enqueue(self, request: PaymentRequest) -> PaymentResult:
           """Validate, create payment record, return payment_id."""
       
       async def execute_payment(self, payment_id: str) -> PaymentResult:
           """Claim-and-execute: submit tx, confirm, record attempts."""
       
       async def recover_inflight(self, guild_ids=None) -> List[PaymentResult]:
           """Check on-chain status of all submitted payments."""
   ```

2. **`enqueue()`**: Validates address via provider, checks write_allowed, calls `create_payment()`. Does NOT send — just queues.

3. **`execute_payment()`**: The core execution path:
   - Fetch payment record; verify status is 'processing' (already claimed).
   - Check for existing `tx_signature` — if present, check on-chain status first (idempotency guard).
   - If no prior tx or prior tx failed: get provider via wallet_registry, call `provider.send()`.
   - Record attempt in `payment_attempts`.
   - On send success: `mark_payment_submitted(payment_id, tx_signature)`.
   - Poll `provider.check_status()` with timeout.
   - On confirm: `mark_payment_confirmed()`, record attempt.
   - On timeout: leave as 'submitted' for recovery.
   - On definitive failure: `mark_payment_failed()` with no retry_after.

4. **`recover_inflight()`**: Called at startup:
   - Fetch all payments with status in ('processing', 'submitted').
   - For 'submitted': check on-chain status. Confirmed → mark confirmed. Failed → mark failed or requeue with retry_after. Not found → leave for next recovery pass (tx may still be propagating).
   - For 'processing' with no tx_signature: likely crashed before send — requeue as 'queued'.
   - Record all checks in `payment_attempts`.

5. **Status machine rules** (enforced in service, not just DB):
   - `queued` → `processing` (only via atomic claim RPC)
   - `processing` → `submitted` (tx sent to chain)
   - `processing` → `failed` (pre-send failure, e.g. balance)
   - `processing` → `queued` (crash recovery, no tx was sent)
   - `submitted` → `confirmed` (on-chain confirmation)
   - `submitted` → `failed` (on-chain failure)
   - `submitted` → `queued` (recovery decides to retry — only if chain says failed/not_found AND attempt_count < max)
   - Never: `submitted` → `processing` (would risk double-send)

---

## Phase 4: Grants Integration

### Step 8: Refactor `GrantsCog` to use payment service (`src/features/grants/grants_cog.py`)
**Scope:** Large

1. **Add** Discord confirmation flow before final payout:
   - After wallet is provided and validated, bot asks: "Send X SOL (~$Y USD) to `<wallet>`? Reply **yes** to confirm."
   - New status: `awaiting_confirmation` (between `awaiting_wallet` and enqueue).
   - On "yes" reply: enqueue payment via `PaymentService.enqueue()`, update grant status to `payment_queued`.
   - On anything else: remind them to reply "yes" or provide a different wallet.

2. **Optional test payment flow**:
   - After wallet provided, before the main confirmation, offer: "Would you like a test payment of 0.001 SOL first? Reply **test** or **yes** to skip straight to the full amount."
   - If test requested: enqueue a 0.001 SOL payment with source_kind='grant_test', wait for confirmation, then proceed to full amount confirmation.
   - Store test_payment_id on the grant record for traceability.

3. **Remove** direct Solana sending from `_process_payment()` — replace with `payment_service.enqueue()`.
4. **Remove** `_recover_inflight_payments()` from GrantsCog — recovery is now in PaymentService.
5. **Update** grant status values:
   - Keep: `reviewing`, `needs_info`, `needs_review`, `awaiting_wallet`, `rejected`, `spam`
   - Add: `awaiting_confirmation`, `payment_queued`
   - Rename: `paid` stays but is set when payment service confirms (via callback or polling)
   - Remove: `payment_status` field on grants — payment lifecycle is in the payments table now.

6. **Add** a lightweight poller or callback: after enqueue, GrantsCog periodically checks `get_payment_by_source('grant', thread_id)` to detect confirmation → posts "Payment confirmed! tx: ..." in the thread and updates grant to `paid`.

### Step 9: Create payment worker cog (`src/features/payments/payment_cog.py`)
**Scope:** Medium

1. **Create** `PaymentCog` — a discord.py cog with a background task loop (mirrors the scheduled publication worker pattern):
   - `payment_worker()`: runs on interval (e.g. 30s), calls `claim_due_payments()`, then `execute_payment()` for each claimed row.
   - `_before_payment_worker()`: waits for bot ready.
   - On cog load: calls `payment_service.recover_inflight()` once.

2. **Register** in `main.py` as an optional cog, same pattern as GrantsCog.

---

## Phase 5: Tests & Validation

### Step 10: Write payment service tests (`tests/test_payment_service.py`)
**Scope:** Medium

1. **Mirror** the `FakeSupabase` pattern from `test_social_publications.py`.
2. **Test: no double payment** — `test_submitted_tx_is_never_resent_until_chain_state_or_explicit_failure_allows_it`:
   - Enqueue payment, execute (submits tx), simulate crash (leave as 'submitted').
   - Call `recover_inflight()` — should check on-chain, NOT re-send.
   - Only re-send if chain returns 'failed' or 'not_found' AND attempt budget remains.

3. **Test: confirmation flow** — `test_grants_flow_requires_wallet_then_yes_confirmation_before_final_payout`:
   - Simulate: wallet provided → bot asks confirmation → "yes" reply → payment enqueued.
   - Verify: no payment created before "yes".
   - Verify: payment_request has correct wallet, amount, wallet_key.

4. **Test: status machine** — verify illegal transitions are rejected.
5. **Test: recovery** — processing with no tx_signature → requeued; submitted + confirmed on chain → marked confirmed.
6. **Test: test payment flow** — small amount sent first, then full amount after confirmation.

### Step 11: Update existing tests and verify
**Scope:** Small

1. **Verify** `tests/test_social_publications.py` still passes (no changes expected to social publishing).
2. **Verify** `tests/test_scheduler.py` still passes.
3. **Update** any existing grant tests if they assert on removed fields like `payment_status`.

---

## Execution Order

1. **Phase 1 first** (Steps 1–3): schema and DB methods are the foundation everything depends on.
2. **Phase 2 next** (Steps 4–6): provider interface and wallet registry, needed before the service.
3. **Phase 3** (Step 7): payment service, the core logic.
4. **Phase 4** (Steps 8–9): grants integration and worker cog — these depend on the service existing.
5. **Phase 5 last** (Steps 10–11): tests validate everything.

Within phases, steps can proceed sequentially as listed — each builds on the previous.

## Validation Order

1. After Step 3: manually verify DB methods work against FakeSupabase (quick sanity).
2. After Step 7: run `test_payment_service.py` focused tests for status machine and idempotency.
3. After Step 9: run full test suite to catch regressions.
4. After Step 11: all tests green, including the two fail_to_pass tests from the brief.
