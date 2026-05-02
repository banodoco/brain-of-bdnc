# Implementation Plan: Generalized Solana Payment Ledger & Service

## Overview

The Discord bot currently sends SOL payments entirely within `GrantsCog` — the status machine, idempotency guards, recovery loop, and Solana RPC calls are all wired directly into the grants feature. The goal is to extract a **reusable payment ledger and service** that any feature (grants, bounties, tips, future flows) can use as a producer, while the service owns the payment lifecycle.

The codebase already has a clean provider-based template in `SocialPublishService` + `SocialPublishProvider` + `social_publications` SQL table. The payment system will follow the same architecture: abstract provider → concrete `SolanaProvider` → service layer → SQL ledger → DB handler methods.

### Current state
- `grants_cog.py` (780 lines): owns the full payment flow inline
- `solana_client.py`: low-level RPC wrapper (send_sol, confirm_tx, check_tx_status) — **reusable as-is**
- `db_handler.py`: has grant-specific payment methods (get_inflight_payments, record_grant_payment, update_grant_status)
- `sql/`: contains `social_publications.sql` as a template; no grants migration
- `tests/test_social_publish_service.py`: FakeDB + FakeProvider pattern

### Key constraints
- No double payments under crashes/retries/restarts (idempotency via on-chain check before re-send)
- Variable source wallets selected by stable wallet key (not just one env var)
- Provider-based architecture starting with SOL only
- Discord-mediated recipient confirmation (reply "yes")
- Optional test payment before final payout
- Grants flow must plug in as a producer

---

## Phase 1: Foundation — SQL, Types, Provider Interface

### Step 1: Create the payment ledger SQL migration (`sql/payment_ledger.sql`)
**Scope:** Small
1. **Create** `sql/payment_ledger.sql` following the `social_publications.sql` pattern. Core columns:
   - `payment_id` (UUID PK)
   - `guild_id` (BIGINT NOT NULL)
   - `producer` (TEXT NOT NULL) — e.g. `'grants'`, `'bounties'` — identifies the calling feature
   - `producer_ref` (TEXT) — feature-specific reference (e.g. thread_id for grants)
   - `wallet_key` (TEXT NOT NULL) — stable key into a wallet registry (e.g. `'grants_main'`, `'bounties_hot'`)
   - `recipient_address` (TEXT NOT NULL)
   - `amount_token` (NUMERIC NOT NULL) — amount in native token (SOL)
   - `amount_usd` (NUMERIC) — USD equivalent at time of creation
   - `token_price_usd` (NUMERIC) — spot price at creation
   - `provider` (TEXT NOT NULL DEFAULT `'solana'`)
   - `status` (TEXT CHECK: `'pending_confirmation'`, `'confirmed'`, `'sending'`, `'sent'`, `'confirmed_onchain'`, `'failed'`, `'cancelled'`)
   - `is_test` (BOOLEAN DEFAULT FALSE) — marks test/dry-run payments
   - `tx_signature` (TEXT)
   - `attempt_count` (INTEGER DEFAULT 0)
   - `last_error` (TEXT)
   - `retry_after` (TIMESTAMPTZ)
   - `request_payload` (JSONB) — full request for reconstruction on retry
   - `confirmed_by_user_id` (BIGINT) — Discord user who confirmed
   - `confirmed_at` (TIMESTAMPTZ)
   - `created_at`, `updated_at`, `completed_at` (TIMESTAMPTZ)
   - Index on `(guild_id, status)`, `(producer, producer_ref)`, `(wallet_key, status)`
   - Unique constraint on `(producer, producer_ref, is_test)` to prevent double-enqueue

### Step 2: Create wallet registry table (`sql/payment_ledger.sql`)
**Scope:** Small
1. **Add** a `wallet_registry` table in the same migration file:
   - `wallet_key` (TEXT PK) — stable human-readable key
   - `guild_id` (BIGINT)
   - `provider` (TEXT DEFAULT `'solana'`)
   - `address` (TEXT NOT NULL) — public address for display/verification
   - `secret_env_var` (TEXT NOT NULL) — name of the env var holding the private key (e.g. `'SOLANA_PRIVATE_KEY'`, `'BOUNTIES_WALLET_KEY'`)
   - `enabled` (BOOLEAN DEFAULT TRUE)
   - `created_at` (TIMESTAMPTZ)
   - This avoids storing private keys in the DB — only the env var name is stored; the actual key is resolved at runtime from the environment.

### Step 3: Define data models and provider interface (`src/features/payments/models.py`, `src/features/payments/providers/__init__.py`)
**Scope:** Medium
1. **Create** `src/features/payments/models.py` with dataclasses:
   - `PaymentRequest`: producer, producer_ref, guild_id, wallet_key, recipient_address, amount_token, amount_usd, token_price_usd, provider (default `'solana'`), is_test, request_payload (dict), requires_confirmation (bool, default True)
   - `PaymentResult`: payment_id, success, status, tx_signature, error
2. **Create** `src/features/payments/providers/__init__.py` with abstract base:
   ```python
   class PaymentProvider(ABC):
       @abstractmethod
       async def send(self, recipient: str, amount: float, keypair_bytes: bytes) -> str:
           """Send tokens. Returns tx signature."""
       @abstractmethod
       async def confirm(self, tx_signature: str) -> bool:
           """Wait for on-chain confirmation."""
       @abstractmethod
       async def check_status(self, tx_signature: str) -> str:
           """Returns 'confirmed', 'failed', or 'not_found'."""
       @abstractmethod
       async def get_balance(self, keypair_bytes: bytes) -> float:
           """Return balance in native token."""
   ```
   - The provider receives raw keypair bytes, not env var names — the service layer resolves wallet_key → env var → bytes.

### Step 4: Implement Solana provider (`src/features/payments/providers/solana_provider.py`)
**Scope:** Medium
1. **Create** `src/features/payments/providers/solana_provider.py` — a thin adapter around the existing `SolanaClient`:
   - Constructor takes `rpc_url` (defaults to env var `SOLANA_RPC_URL`)
   - `send()`: creates a `SolanaClient`-like send using the provided keypair bytes (not a hardcoded env var). Reuse the existing `send_sol` logic (MessageV0, retry on blockhash errors, skip preflight).
   - `confirm()`: delegates to existing `confirm_tx` logic
   - `check_status()`: delegates to existing `check_tx_status` logic
   - `get_balance()`: delegates to existing balance check
2. **Keep** `src/features/grants/solana_client.py` intact for now — `SolanaProvider` can import and wrap it, or duplicate the ~60 lines of send logic to decouple. Prefer wrapping initially to minimize risk.

---

## Phase 2: Service Layer & DB Integration

### Step 5: Add payment ledger DB methods (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** methods to `DatabaseHandler`, following the `social_publication` method pattern:
   - `create_payment(data: Dict, guild_id: Optional[int]) -> Optional[Dict]` — inserts into `payment_ledger`, returns the row
   - `get_payment_by_id(payment_id: str) -> Optional[Dict]`
   - `get_payment_by_producer_ref(producer: str, producer_ref: str, is_test: bool = False) -> Optional[Dict]` — for idempotency checks
   - `mark_payment_confirmed_by_user(payment_id: str, user_id: int)` — sets confirmed_by_user_id, confirmed_at, status → `'confirmed'`
   - `mark_payment_sending(payment_id: str, attempt_count: int)`
   - `mark_payment_sent(payment_id: str, tx_signature: str)`
   - `mark_payment_completed(payment_id: str, tx_signature: str)`
   - `mark_payment_failed(payment_id: str, error: str, retry_after: Optional[datetime] = None)`
   - `mark_payment_cancelled(payment_id: str)`
   - `get_inflight_payments_ledger(guild_id: Optional[int] = None) -> List[Dict]` — status IN ('sending', 'sent') for recovery
   - `get_wallet_config(wallet_key: str) -> Optional[Dict]` — reads from `wallet_registry`
   - All methods use `_gate_check` for guild isolation.

### Step 6: Implement PaymentService (`src/features/payments/payment_service.py`)
**Scope:** Large — this is the core of the system
1. **Create** `src/features/payments/payment_service.py`:
   ```python
   class PaymentService:
       def __init__(self, db_handler, providers: Dict[str, PaymentProvider] = None):
           self.db = db_handler
           self.providers = providers or {'solana': SolanaProvider()}
   ```
2. **Core methods:**
   - `async def request_payment(self, request: PaymentRequest) -> PaymentResult`:
     - Check for existing payment via `get_payment_by_producer_ref` (idempotency)
     - If `requires_confirmation`: create with status `'pending_confirmation'` — caller handles Discord UX
     - If not: create with status `'confirmed'` and proceed to send
     - Return PaymentResult with payment_id
   - `async def confirm_payment(self, payment_id: str, user_id: int) -> PaymentResult`:
     - Validate status is `'pending_confirmation'`
     - Mark confirmed, then call `_execute_payment`
   - `async def _execute_payment(self, payment_id: str) -> PaymentResult`:
     - Resolve wallet: `get_wallet_config(wallet_key)` → `secret_env_var` → `os.environ[env_var]` → decode keypair bytes
     - If `is_test`: send a tiny amount (0.000001 SOL) to validate the address, then mark completed
     - Mark `'sending'`, increment attempt_count
     - Call `provider.send()` → mark `'sent'` with tx_signature
     - Call `provider.confirm()` → mark completed
     - On failure: mark failed with error, set retry_after
   - `async def recover_inflight(self)`:
     - Fetch all `'sending'`/`'sent'` payments
     - For each: check on-chain status via provider
     - Confirmed → mark completed; Failed → mark failed; Not found → leave (propagating)
   - `async def send_test_payment(self, request: PaymentRequest) -> PaymentResult`:
     - Force `is_test=True`, `amount_token` to dust amount
     - Proceeds directly without confirmation
   - `async def cancel_payment(self, payment_id: str) -> bool`:
     - Only if status in (`'pending_confirmation'`, `'confirmed'`) — not yet sent
3. **Error handling:**
   - Pre-send errors (bad address, insufficient balance) → mark failed immediately
   - Post-send errors (timeout, network) → leave status as `'sent'` for recovery
   - This mirrors the existing grants pattern exactly.

---

## Phase 3: Grants Integration & Discord UX

### Step 7: Refactor GrantsCog to use PaymentService (`src/features/grants/grants_cog.py`)
**Scope:** Large — rewiring the existing flow
1. **Initialize** `PaymentService` in `GrantsCog.__init__` (alongside existing setup)
2. **Replace** `_process_payment` body:
   - When grant is approved and user sends wallet address:
     - Call `payment_service.request_payment(PaymentRequest(producer='grants', producer_ref=str(thread_id), wallet_key='grants_main', recipient_address=wallet, amount_token=sol_amount, amount_usd=total_usd, token_price_usd=sol_price, requires_confirmation=True))`
     - Post Discord message: "Payment of X SOL to `wallet`. Reply **yes** to confirm."
   - When user replies "yes":
     - Call `payment_service.confirm_payment(payment_id, user_id=message.author.id)`
     - Post result (success with explorer link, or error)
   - Optional test flow: if configured, call `send_test_payment` first, then request the real payment after test succeeds
3. **Replace** `_recover_inflight_payments`:
   - Call `payment_service.recover_inflight()` instead of the inline loop
   - The service handles on-chain checks; the cog only handles Discord messaging for recovered payments
4. **Keep** grant-specific DB methods (`create_grant_application`, `update_grant_status`, etc.) — these track grant lifecycle, not payment lifecycle. The payment ledger is a separate concern.
5. **Remove** direct `SolanaClient` usage from GrantsCog — it should only go through `PaymentService`.

### Step 8: Wire "yes" confirmation listener (`src/features/grants/grants_cog.py`)
**Scope:** Small
1. **Update** the `on_message` handler for `awaiting_wallet` status:
   - After wallet address is received → create payment request → store `payment_id` in grant record (new column or in-memory mapping)
   - Add a new status check: if grant has a pending payment and user replies with "yes" (case-insensitive, stripped) → call `confirm_payment`
   - This replaces the current behavior where wallet submission immediately triggers payment

---

## Phase 4: Tests & Validation

### Step 9: Unit tests for PaymentService (`tests/test_payment_service.py`)
**Scope:** Medium
1. **Create** `tests/test_payment_service.py` following `test_social_publish_service.py` patterns:
   - `FakePaymentDB`: in-memory mock implementing all payment ledger DB methods
   - `FakePaymentProvider`: mock returning predictable tx signatures
   - `FailingPaymentProvider`: raises on send/confirm
2. **Test cases:**
   - `test_request_payment_creates_pending_record`
   - `test_confirm_payment_executes_and_completes`
   - `test_duplicate_producer_ref_returns_existing` (idempotency)
   - `test_send_failure_marks_failed_with_error`
   - `test_confirm_timeout_leaves_sent_status` (recovery path)
   - `test_recover_inflight_confirms_onchain`
   - `test_recover_inflight_marks_failed_onchain`
   - `test_cancel_pending_payment`
   - `test_cancel_sent_payment_rejected`
   - `test_test_payment_sends_dust_amount`
   - `test_wallet_key_resolution` (env var lookup)

### Step 10: Integration smoke test (`tests/test_payment_integration.py`)
**Scope:** Small
1. **Create** a lightweight integration test that:
   - Uses FakeDB + FakeProvider
   - Simulates the full grants flow: request → confirm → complete
   - Verifies the grant record and payment ledger record are both updated correctly
   - Verifies recovery after simulated crash (payment stuck in 'sent')

---

## Execution Order
1. Phase 1 first (SQL + types + provider) — no existing code changes, pure additions
2. Phase 2 next (DB methods + service) — still additive, no grants changes yet
3. Phase 3 (grants rewiring) — only after service + tests prove the new path works
4. Phase 4 tests can be written alongside Phase 2, run before Phase 3

## Validation Order
1. SQL migration parses and is consistent with existing patterns
2. Unit tests for PaymentService pass (Step 9)
3. Integration smoke test passes (Step 10)
4. Grants flow works end-to-end with the new service (manual or extended integration test)
5. Recovery logic handles all edge cases (stuck in sending, sent, failed)
