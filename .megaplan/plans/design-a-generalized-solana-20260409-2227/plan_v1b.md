# Implementation Plan: Generalized Solana Payment Ledger & Service

## Overview

The bot currently embeds payment logic directly in `GrantsCog` (`src/features/grants/grants_cog.py`), coupling grant assessment with SOL transfers, wallet address collection, and recovery. The goal is to extract a standalone **payment ledger** and **payment service** that any feature (grants, future bounties, etc.) can use as a producer. The service owns the state machine, idempotency, wallet selection, and on-chain interactions; producers just request a payment and receive callbacks.

**Current shape:**
- `GrantsCog` (~780 LOC) handles assessment, approval, wallet collection, SOL sending, and recovery all in one file
- `SolanaClient` (`src/features/grants/solana_client.py`) is a clean async wrapper around Solders — reusable as-is
- `grant_applications` table exists in Supabase but has no checked-in SQL migration
- No existing tests for grants or payments
- `social_publications` SQL + `SocialPublishService` + providers provide the proven pattern to follow

**Key constraints:**
- No double payments under crashes/retries/restarts (idempotency key + state machine)
- Provider-based architecture starting with SOL only
- Variable source wallets selected by stable wallet key
- Discord-mediated recipient confirmation before final payout
- Grants flow refactored to produce payment requests instead of sending SOL directly

---

## Phase 1: Foundation — SQL, Models, Provider Interface

### Step 1: Create `payment_ledger` SQL migration (`sql/payment_ledger.sql`)
**Scope:** Medium
1. **Create** `sql/payment_ledger.sql` following the `social_publications.sql` pattern:
   - Table `payment_ledger` with columns:
     - `payment_id` UUID PK (gen_random_uuid)
     - `idempotency_key` TEXT UNIQUE NOT NULL — caller-supplied, prevents double-creation
     - `guild_id` BIGINT NOT NULL
     - `producer` TEXT NOT NULL — e.g. `'grants'`, `'bounties'`
     - `producer_ref` TEXT — external reference (e.g. thread_id)
     - `recipient_discord_id` BIGINT — who should confirm
     - `recipient_address` TEXT — wallet address, filled after confirmation
     - `wallet_key` TEXT NOT NULL — stable key selecting source wallet (e.g. `'grants_main'`)
     - `currency` TEXT NOT NULL DEFAULT `'SOL'` — for future tokens
     - `amount_crypto` NUMERIC — computed amount in crypto
     - `amount_usd` NUMERIC — original USD value
     - `exchange_rate` NUMERIC — rate at time of send
     - `status` TEXT NOT NULL with CHECK: `'pending_confirmation'|'confirmed'|'sending'|'sent'|'completed'|'failed'|'cancelled'`
     - `tx_signature` TEXT — on-chain tx hash
     - `attempt_count` INTEGER DEFAULT 0
     - `last_error` TEXT
     - `metadata` JSONB DEFAULT `'{}'` — producer-specific data
     - `created_at`, `updated_at`, `completed_at` TIMESTAMPTZ
   - Indexes: `(idempotency_key)` unique, `(guild_id, status)`, `(producer, producer_ref)`, `(status)` partial for recovery
   - `updated_at` trigger (reuse `set_social_updated_at` or create equivalent)
   - RLS enabled, revoke anon/authenticated access
2. **Create** a `payment_wallets` config table (or use server_config JSONB — see Assumptions):
   - Maps `wallet_key` → env var name for the private key, RPC URL override, description
   - Seeded with `grants_main` pointing to `SOLANA_PRIVATE_KEY`

### Step 2: Define models and provider interface (`src/features/payments/models.py`, `src/features/payments/providers/base.py`)
**Scope:** Small
1. **Create** `src/features/payments/models.py` with dataclasses:
   - `PaymentRequest`: idempotency_key, guild_id, producer, producer_ref, recipient_discord_id, wallet_key, currency, amount_usd, metadata
   - `PaymentResult`: payment_id, status, tx_signature, amount_crypto, exchange_rate, error
   - `PaymentStatus` enum matching DB CHECK constraint
2. **Create** `src/features/payments/providers/base.py` with abstract provider:
   ```python
   class PaymentProvider(ABC):
       async def get_exchange_rate(self, currency: str) -> float: ...
       async def send(self, recipient_address: str, amount: float, wallet_key: str) -> str: ...  # returns tx_sig
       async def confirm(self, tx_signature: str) -> str: ...  # returns 'confirmed'|'failed'|'not_found'
       async def get_balance(self, wallet_key: str) -> float: ...
   ```

### Step 3: Implement SOL provider (`src/features/payments/providers/sol_provider.py`)
**Scope:** Medium
1. **Create** `src/features/payments/providers/sol_provider.py`:
   - Wraps existing `SolanaClient` logic but with wallet key resolution
   - Constructor takes a `wallet_registry: dict[str, SolanaClient]` — maps wallet_key to initialized client
   - `get_exchange_rate('SOL')` delegates to existing `get_sol_price_usd()`
   - `send()` delegates to the appropriate `SolanaClient.send_sol()`
   - `confirm()` delegates to `SolanaClient.check_tx_status()`
2. **Keep** `src/features/grants/solana_client.py` unchanged — the provider wraps it, doesn't replace it

---

## Phase 2: Core Service — State Machine & DB Layer

### Step 4: Payment DB methods (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** methods to `DBHandler` (after existing grant methods, ~line 2070):
   - `create_payment(request: dict) -> Optional[dict]` — INSERT with idempotency_key conflict handling (ON CONFLICT DO NOTHING pattern via try/except on unique violation, then SELECT)
   - `get_payment(payment_id: str) -> Optional[dict]`
   - `get_payment_by_idempotency_key(key: str) -> Optional[dict]`
   - `update_payment_status(payment_id: str, status: str, **kwargs) -> bool` — with optimistic concurrency: include `WHERE status IN (valid_transitions_from)` to prevent invalid state jumps
   - `get_inflight_payments_ledger() -> list[dict]` — payments in `sending|sent` for recovery
   - `get_pending_confirmations() -> list[dict]` — payments in `pending_confirmation`

### Step 5: Payment service (`src/features/payments/payment_service.py`)
**Scope:** Large
1. **Create** `src/features/payments/payment_service.py` — the central orchestrator:
   - Constructor: `PaymentService(db_handler, providers: dict[str, PaymentProvider], wallet_registry)`
   - **`request_payment(request: PaymentRequest) -> PaymentResult`**: Creates ledger entry in `pending_confirmation` status. Returns payment_id for the producer to track. Idempotent via idempotency_key — if already exists, returns existing record.
   - **`confirm_and_send(payment_id: str, recipient_address: str) -> PaymentResult`**: Called after Discord user confirms. Validates address, fetches exchange rate, computes crypto amount, transitions `pending_confirmation → sending → sent → completed`. The core send flow:
     1. Validate wallet address
     2. Fetch exchange rate, compute amount
     3. Update status to `sending` with address + amounts
     4. Call provider.send() — get tx_sig
     5. Update status to `sent` with tx_sig
     6. Call provider.confirm() — wait for on-chain confirmation
     7. Update status to `completed`
     8. On provider.send() failure: update to `failed` with error
     9. On provider.confirm() timeout: leave as `sent` for recovery
   - **`recover_inflight() -> list[PaymentResult]`**: Startup recovery — check on-chain status of `sending`/`sent` payments, update accordingly
   - **`cancel_payment(payment_id: str) -> bool`**: Transition `pending_confirmation → cancelled`
   - **`get_payment_status(payment_id: str) -> Optional[PaymentResult]`**: Read-only status check
2. **State machine transitions** (enforced in DB update WHERE clause):
   - `pending_confirmation → confirmed → sending → sent → completed`
   - `pending_confirmation → cancelled`
   - `sent → failed` (only if on-chain confirms failure)
   - `sending → failed` (if send call raises)

### Step 6: Optional test payment flow (`src/features/payments/payment_service.py`)
**Scope:** Small
1. **Add** `test_payment(payment_id: str, recipient_address: str) -> PaymentResult`:
   - Sends a tiny amount (0.001 SOL / ~$0.15) to validate the address is receivable
   - Records as a separate ledger entry with `producer='test_payment'` and `metadata.parent_payment_id`
   - Producer can call this before `confirm_and_send` for high-value payments

---

## Phase 3: Integration — Wire Grants to Payment Service

### Step 7: Create `__init__.py` and wire service initialization (`src/features/payments/__init__.py`, `main.py`)
**Scope:** Small
1. **Create** `src/features/payments/__init__.py` exporting `PaymentService`, `SolProvider`
2. **Update** `main.py` (bot initialization) to:
   - Build wallet registry from env vars (initially just `grants_main → SOLANA_PRIVATE_KEY`)
   - Initialize `SolProvider` with wallet registry
   - Initialize `PaymentService` with db_handler and providers
   - Attach to bot as `bot.payment_service`
   - Call `payment_service.recover_inflight()` on startup

### Step 8: Refactor `GrantsCog` to use `PaymentService` (`src/features/grants/grants_cog.py`)
**Scope:** Large
1. **Replace** `_process_payment` method (~line 678-779):
   - Instead of directly calling `SolanaClient`, call `bot.payment_service.confirm_and_send()`
   - The payment_id comes from the grant record (stored when grant is approved)
2. **Update** `_handle_assessment` for approved grants (~line 637-667):
   - After DB update, call `payment_service.request_payment()` with idempotency_key=`grant:{thread_id}:{guild_id}`
   - Store returned `payment_id` in the grant record
3. **Replace** `_recover_inflight_payments` (~line 87-193):
   - Delegate to `payment_service.recover_inflight()` at startup
   - Keep the Discord notification logic (posting to threads) in the cog, but payment state management moves to the service
4. **Remove** direct `SolanaClient` usage from cog — the cog no longer imports or holds a `SolanaClient` reference
5. **Keep** wallet address validation in `on_message` handler — but after validation, call `payment_service.confirm_and_send()` instead of `_process_payment()`

### Step 9: Add `payment_ledger` column to grant_applications or link table
**Scope:** Small
1. **Add** `payment_id` column to `grant_applications` table (nullable UUID, references `payment_ledger.payment_id`)
2. **Update** `create_grant_application` and `update_grant_status` in db_handler to handle `payment_id`
3. Grants can query their payment status via `payment_service.get_payment_status(payment_id)`

---

## Phase 4: Tests & Validation

### Step 10: Fake provider and DB for unit tests (`tests/conftest.py`, `tests/test_payment_service.py`)
**Scope:** Medium
1. **Create** `FakeSolProvider` implementing `PaymentProvider`:
   - `send()` returns a deterministic fake tx signature
   - `confirm()` returns `'confirmed'`
   - `get_exchange_rate()` returns fixed rate (e.g. 150.0)
   - Configurable to simulate failures
2. **Create** `FakePaymentDB` — in-memory dict storage following the pattern in `tests/conftest.py`
3. **Write** `tests/test_payment_service.py`:
   - Happy path: request → confirm → send → complete
   - Idempotency: duplicate request_payment returns same payment_id
   - Double-send prevention: confirm_and_send on already-sent payment checks on-chain instead of re-sending
   - Crash recovery: payment in `sent` status recovers correctly
   - Cancellation: pending_confirmation → cancelled
   - Invalid state transitions are rejected
   - Wallet key resolution: correct source wallet selected

### Step 11: Integration-style tests for grants flow (`tests/test_grants_payment.py`)
**Scope:** Medium
1. **Write** tests that verify grants cog correctly:
   - Creates a payment request on approval
   - Passes wallet address through to payment service on user reply
   - Handles payment service errors gracefully
   - Recovery delegates to payment service

### Step 12: Run full test suite and validate
**Scope:** Small
1. **Run** `pytest tests/` to confirm no regressions
2. **Validate** SQL migration parses correctly
3. **Verify** no remaining direct `SolanaClient` usage in `GrantsCog`

---

## Execution Order
1. Phase 1 (Steps 1-3): Foundation — can be built and tested independently
2. Phase 2 (Steps 4-6): Core service — depends on models from Phase 1
3. Phase 3 (Steps 7-9): Integration — depends on working service from Phase 2
4. Phase 4 (Steps 10-12): Tests — write fake provider early (Step 10 can parallel with Phase 2), integration tests after Phase 3

## Validation Order
1. SQL migration parses and creates valid schema (Step 1)
2. Unit tests for payment service pass (Step 10)
3. Integration tests for grants flow pass (Step 11)
4. Full test suite passes with no regressions (Step 12)
5. Manual: approve a grant in dev, confirm wallet, verify payment lands on devnet
