# Implementation Plan: Final Polish & Gremlin-Investigation Pass for Solana Payout Subsystem

## Overview
Fourth pass on the payouts subsystem, landing AFTER the in-flight cleanup megaplan (`payment-subsystem-follow-up-20260410-2339`) and the pending admin-DM approval megaplan. Goal: close remaining gremlin-hiding spots, harden edge cases, produce ops documentation, and establish daily safety monitoring.

Current repo shape (verified 2026-04-11):
- `src/features/payments/payment_service.py` — ~600 lines; `request_payment` is a single ~250-line function (`payment_service.py:69–316`) that will be the A1 refactor target. Already has `PaymentActor`/`PaymentActorKind`, `_authorize_actor`, `confirm_payment`, `execute_payment`, `recover_inflight`. Local `_redact_wallet` at `payment_service.py:33`.
- `src/features/payments/payment_cog.py` — ~580 lines, monolithic worker+UI+DM. Cleanup megaplan splits it into `PaymentWorkerCog` and `PaymentUICog`; C8 DM fallback and A3 redact consolidation target the post-split worker cog.
- `src/features/payments/producer_flows.py` — exists with `PRODUCER_FLOWS` registry and `get_flow`.
- `src/features/grants/solana_client.py` — shared `SolanaClient` used by both providers. C7 (dynamic fees) lives here (`solana_client.py:52–54` static floor, `send_sol` at `67–137`); F16 (structured confirm logging) lives in `confirm_tx` at `139–175`.
- `src/features/admin_chat/tools.py`:
   - `execute_retry_payment` at `2355–2370` and `execute_release_payment` at `2392–2413` currently take only `db_handler`; the dispatcher at `3134–3139` passes only `db_handler`. B4 requires threading `payment_service` through both.
   - `execute_initiate_payment` at `2438+` creates an `admin_payment_intent` and dedupes per `(guild_id, source_channel_id, recipient_user_id)` BEFORE `producer_ref` is set at `tools.py:2486`. **D11 reproduction must target the downstream call site**, not the intent creation.
- `src/features/admin_chat/admin_chat_cog.py` — `_start_admin_payment_flow` (around `admin_chat_cog.py:246`) and `handle_payment_result` (around `admin_chat_cog.py:364`) are the downstream call sites that pass the intent's stored `producer_ref` into `PaymentService.request_payment` when the test/final payment row is created. This is the real D11 collision surface.
- `src/common/db_handler.py`:
   - `mark_payment_confirmed` at `db_handler.py:2147` allows transitions **only from `submitted`**.
   - `mark_payment_failed` at `db_handler.py:2177` allows **only from `processing`/`submitted`**.
   - `requeue_payment` at `db_handler.py:2219` allows **only from `failed`**.
   - `release_payment_hold` at `db_handler.py:2256` allows **only from `manual_hold`**.
   - **Critical consequence**: `reconcile_with_chain` cannot use `mark_payment_confirmed`/`mark_payment_failed` directly when the starting state is `failed`/`manual_hold` — those calls would no-op. Step 4 adds two new reconciliation-specific DB methods that bypass the normal transition guards.
- `sql/payments.sql` — `claim_due_payment_requests` RPC at `166–198` uses `FOR UPDATE SKIP LOCKED` with `where status = 'queued'`. `tx_signature_history` column exists at `209+`. Partial-unique index on `(producer, producer_ref, is_test)` excluding `failed`/`cancelled` at `134`.
- `main.py:167–209` — rent-exempt guard (A2 target) lives at boot; providers constructed as `solana_grants` and `solana_payouts`.
- `scripts/audit_ghost_confirmed_payments.py` — existing audit seed for F15.
- `docs/` — no existing payments doc.
- `tests/test_admin_payments.py` uses `FakeAdminPaymentService`; `tests/test_solana_client.py` exists; `hypothesis` is not in `requirements.txt`.

Settled orchestrator decisions (from user answers):
1. **C7/F16** target `src/features/grants/solana_client.py` (no new file).
2. **A3** shared redaction helper lives in a **new `src/common/redaction.py`** module (chain-agnostic, dedicated scope).
3. **A3** does NOT unify `admin_chat/tools.py`'s `_redact_wallet_address` / `_redact_wallet_row` / `_redact_payment_row` — separate follow-up.
4. **F15** ships the script plus a scheduler-wire-up section in `docs/runbook-payments.md` flagged as an operator decision. No CI workflow file, no Railway config.
5. **D10** fix (only if the race is real): conditional update gated on `updated_at` older than 150 seconds (5× the 30s worker tick).
6. **D11** fix: millisecond precision in `producer_ref` (Option A). Full UUID suffix is rejected because it would break idempotency on legitimate replay paths.
7. **B4** blockhash-expired heuristic: `submitted_at` older than 150 seconds. No `getLatestBlockhash` round-trip.

Orchestrator security concerns folded into revision:
- **CONCERN A**: Reconcile is exposed ONLY as the `/payment-resolve` slash command. The admin_chat LLM tool `execute_payment_resolve` is **rejected** to keep the prompt-injection surface closed.
- **CONCERN B**: Step 10's verdict cites FOR UPDATE SKIP LOCKED + Postgres read-committed semantics as the proof; the fake-DB test is a sanity check only, not a proof.

Constraints that matter:
- **Fail-closed semantics** are non-negotiable; Phase D items are investigation-first.
- **Read-only audits** must never mutate payment state.
- **B4's on-chain recheck** closes the retry double-send hole and is the teeth of the plan.
- **Reconciliation is administrative** — the two new `force_reconcile_*` DB methods intentionally bypass normal transition guards because they correct history based on authoritative on-chain truth.

## Phase B: Correctness fixes

### Step 1: Add reconciliation DB primitives (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `force_reconcile_payment_to_confirmed(payment_id, *, tx_signature, reason, guild_id)`:
   - Updates `{status: 'confirmed', completed_at: now, last_error: reason, retry_after: null}`.
   - Accepts `allowed_statuses=['submitted','processing','failed','manual_hold']` (explicitly bypasses the normal `submitted-only` gate because reconciliation corrects history from authoritative chain state).
   - Appends to `tx_signature_history` with `reason='reconcile_confirmed'`, the on-chain signature, and the timestamp.
   - Rejects `pending_confirmation`/`queued`/`cancelled` starting states (guardrail: never reconcile-forward a row that never reached the chain).
   - Logs at WARNING with before/after status so reconciliation always leaves a trail.
2. **Add** `force_reconcile_payment_to_failed(payment_id, *, tx_signature, reason, guild_id)`:
   - Same shape but `status='failed'`, `send_phase='submitted'`, history reason `'reconcile_failed'`.
   - Same `allowed_statuses=['submitted','processing','failed','manual_hold']`.
3. **Do not** change existing `mark_payment_confirmed`/`mark_payment_failed` — their strict guards remain the state-machine contract for normal flow.
4. **Test** in `tests/test_admin_payments.py` (or new `tests/test_payment_reconcile.py`): each `allowed_statuses` entry reconciles successfully, appends history, and logs; `pending_confirmation`/`queued`/`cancelled` are rejected.

### Step 2: Add `PaymentService.reconcile_with_chain` (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Add** `reconcile_with_chain(payment_id, *, guild_id) -> ReconcileDecision` returning a frozen dataclass `{decision: Literal['reconciled_confirmed','reconciled_failed','allow_requeue','keep_in_hold','not_applicable'], reason: str, tx_signature: Optional[str]}`.
2. **Logic**:
   - Fetch row; if no `tx_signature`, return `decision='allow_requeue', reason='no prior signature to reconcile'`.
   - Resolve provider via `self._get_provider(row['provider'])`; if missing, return `decision='keep_in_hold', reason='provider unavailable for reconcile'`.
   - Call `provider.check_status(tx_signature)`.
   - `'confirmed'` → call `db_handler.force_reconcile_payment_to_confirmed(...)` from Step 1 → return `reconciled_confirmed`.
   - `'failed'` → call `db_handler.force_reconcile_payment_to_failed(...)` → return `reconciled_failed`.
   - `'not_found'` → compute `now - submitted_at`. If `> 150 seconds`, return `allow_requeue, reason='signature not_found beyond 150s blockhash safety window'`. Otherwise `keep_in_hold, reason='signature not_found but too recent to be safe'`.
   - Any provider exception or new `'rpc_unreachable'` sentinel (Step 10) → `keep_in_hold, reason='RPC unreachable during reconcile'`.
3. **Do not** mutate state when returning `allow_requeue` or `keep_in_hold` (except for the caller follow-up — `allow_requeue` does not by itself requeue).
4. **Unit-test** all six decision branches with a fake provider and a fake DB.

### Step 3: Thread `payment_service` into the admin_chat retry/release tools (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Change** signatures:
   - `execute_retry_payment(bot, db_handler, params)` — fetch `payment_service = getattr(bot, 'payment_service', None)`.
   - `execute_release_payment(bot, db_handler, params)` — same.
2. **Update** the dispatcher at `tools.py:3134–3139` to pass `bot` alongside `db_handler` for both `retry_payment` and `release_payment`, matching the pattern already used by `initiate_payment` at `tools.py:3142–3143`.
3. **On missing `payment_service`**: return `{"success": False, "error": "payment_service unavailable"}` — fail closed, do not fall through to legacy DB mutation.
4. **Gate** `execute_retry_payment`: await `payment_service.reconcile_with_chain(payment_id, guild_id=guild_id)`.
   - `reconciled_confirmed` → return success with reconciled row, do NOT call `requeue_payment`.
   - `reconciled_failed` → return success with reconciled row, do NOT call `requeue_payment`.
   - `allow_requeue` → proceed to existing `db_handler.requeue_payment` path.
   - `keep_in_hold` → call `db_handler.mark_payment_manual_hold` with the decision reason, return non-success result.
5. **Gate** `execute_release_payment` the same way. If reconcile says `reconciled_confirmed`/`reconciled_failed`, refuse the requested `release_payment_hold` and return the reconciled truth. If `allow_requeue`, proceed to `release_payment_hold`. If `keep_in_hold`, call `mark_payment_manual_hold` with the reason.
6. **Test** in `tests/test_admin_payments.py` — five branches per tool (five for retry, five for release) using a fake `payment_service` with injectable reconcile decisions.

### Step 4: `/payment-resolve` slash command (`src/features/payments/payment_cog.py` — or post-cleanup UI cog)
**Scope:** Small
1. **Add** `/payment-resolve payment_id:<str>` as a `discord.app_commands.Command` guarded by an explicit `ADMIN_USER_ID` check inside the callback (not a permission check — match the rest of the repo's admin gating). On non-admin caller, reply ephemeral "admin-only" and return.
2. **Body**: `decision = await self.payment_service.reconcile_with_chain(payment_id, guild_id=interaction.guild_id)`; reply ephemeral with a structured embed or code block reporting `decision.decision`, `decision.reason`, the row's new `status`, and the (redacted) signature.
3. **Do not** expose this as an admin_chat LLM tool. The reconciliation surface is kept off the LLM path to preserve the hardening-pass prompt-injection closure.
4. **Audit trail**: Step 1's `force_reconcile_*` methods already append to `tx_signature_history`; no extra append needed here.
5. **Test**: direct unit test of the callback against a fake `payment_service` and a fake `interaction`; assert reply content per decision branch.

### Step 5: Provider name migration path (`src/features/payments/payment_service.py`, `src/common/db_handler.py`)
**Scope:** Small
1. **Add** `db_handler.get_legacy_provider_payment_requests(guild_ids) -> list[dict]` that selects rows where `provider='solana'`.
2. **Add** `PaymentService.migrate_legacy_provider_rows(guild_ids) -> int`:
   - For each legacy row: `producer='grants'` → update `provider='solana_grants'`; `producer='admin_chat'` → `solana_payouts`; any other producer → `db_handler.mark_payment_manual_hold(reason='legacy provider could not be mapped: unknown producer={producer}')`.
   - Log one WARNING per row with `payment_id`, `before`, `after`.
3. **Call** it once at worker startup from `PaymentWorkerCog.cog_load` (post-cleanup) — or `PaymentCog.cog_load` if this lands before the split.
4. **Test**: fake DB with three legacy rows (grants, admin_chat, unknown) — assert two rewrites and one manual_hold.

## Phase C: Robustness edge cases

### Step 6: Dynamic priority fees via `getRecentPrioritizationFees` (`src/features/grants/solana_client.py`)
**Scope:** Medium
1. **Add** `SolanaClient._get_dynamic_priority_fee(client) -> int`:
   - Call `client.get_recent_prioritization_fees()` (verify the exact SDK method at impl time — may need to fall through to raw RPC JSON via `provider._provider_core.make_request`).
   - Compute the 75th percentile of non-zero `prioritization_fee` values.
   - Clamp: `floor=self.priority_fee_micro_lamports`, `ceiling=int(os.getenv('SOLANA_PRIORITY_FEE_CEILING_MICRO_LAMPORTS', '1000000'))`.
2. **Fallback**: on exception or empty response → return `self.priority_fee_micro_lamports`, log WARNING with the error string.
3. **Use** the result at `solana_client.py:94–98` inside `send_sol`, replacing the static `compute_unit_price = self.priority_fee_micro_lamports`. Log the chosen value alongside the existing priority-fee log.
4. **Test** in `tests/test_solana_client.py`: mock the SDK call; assert 75th percentile math, floor clamp, ceiling clamp, fallback-on-error.

### Step 7: Admin DM fallback channel (`src/features/payments/payment_cog.py` — or post-cleanup worker cog)
**Scope:** Small
1. **Add** `ADMIN_FALLBACK_CHANNEL_ID` env read in the worker cog `__init__`.
2. **Extract** a helper `_deliver_admin_alert(message: str)` and route both `_dm_admin_payment_success` (`payment_cog.py:288–344`) and `_dm_admin_payment_failure` (`payment_cog.py:346–406`) through it:
   - Try DM to `ADMIN_USER_ID` via `fetch_user(...).send(message)`.
   - On `discord.Forbidden` or `discord.HTTPException` with rate-limit status: resolve `ADMIN_FALLBACK_CHANNEL_ID` and post via `safe_send_message`.
   - On both failing: `logger.error('[PaymentCog] admin alert undeliverable', extra={'message_preview': message[:120]})`.
3. **Test**: fake bot where DM raises `Forbidden` → assert fallback channel `send` called. Fake bot where both fail → assert ERROR log.

### Step 8: RPC-down vs timeout distinction (`src/features/payments/solana_provider.py`, `src/features/payments/payment_service.py`)
**Scope:** Small
1. **Add** `'rpc_unreachable'` sentinel to `SolanaProvider.confirm_tx` and `check_status` return values. Emit it **only** on connection-family exceptions: `aiohttp.ClientConnectionError`, `aiohttp.ClientConnectorError`, `asyncio.TimeoutError` during connect, `solana.rpc.core.RPCException` with a transport cause (verify exact types at impl time). A legitimate confirmation timeout (wait-budget expired but the RPC is responsive) remains `'timeout'`.
2. **Update** `PaymentService._confirm_submitted_payment` (`payment_service.py:548+`) to branch on `'rpc_unreachable'` → `mark_payment_manual_hold` with reason `'rpc_unreachable: confirmation RPC offline'` — distinct from `'Confirmation timed out after submission'`.
3. **Update** `PaymentService.recover_inflight` (`payment_service.py:458+`) same way for the `submitted` branch.
4. **Bridge** to Step 2: `reconcile_with_chain` treats provider returning `'rpc_unreachable'` as `keep_in_hold`, matching the exception path.
5. **Test**: fake provider returning `'rpc_unreachable'` → assert distinct manual_hold reason in both call sites.

## Phase A: Refactor and polish (post-cleanup shape)

### Step 9: Decompose `PaymentService.request_payment` into named helpers (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Extract** from `payment_service.py:69–316`, keeping behavior and public signature identical:
   - `_normalize_inputs(**kwargs) -> NormalizedRequest` (absorbs lines 91–98 and provider key normalization at 139). Returns `None` on validation failure.
   - `_detect_collision_and_idempotent_row(normalized, guild_id, is_test) -> tuple[Optional[row], Optional[row], Optional[row]]` (absorbs lines 99–122).
   - `_derive_amounts(normalized, is_test, amount_usd, amount_token, provider) -> AmountsResult` (absorbs lines 161–188).
   - `_enforce_caps(normalized, amount_token, amount_usd, provider) -> CapResult` (absorbs lines 189–223).
   - `_persist_row(record, normalized, guild_id, is_test, collision_row, blocking_prior_row) -> Optional[dict]` (absorbs lines 286–316, including the duplicate re-read fallback).
2. **Rewrite** `request_payment` as a thin orchestrator (~40 lines) calling the helpers in sequence; the public signature and all log messages stay identical.
3. **Run** the existing test suite unchanged — it must pass with zero modifications. This is the behavior-preservation gate.

### Step 10: Move rent-exempt guard into `PaymentService.__init__` (`src/features/payments/payment_service.py`, `main.py`)
**Scope:** Small
1. **Add** module-level constants `RENT_EXEMPT_LAMPORTS = 890_880` and `MIN_TEST_LAMPORTS = 2_000_000` in `payment_service.py`.
2. **In** `PaymentService.__init__`, compute `int(self.test_payment_amount * 1_000_000_000)` and raise `ValueError` with the existing message from `main.py:178–184` if below `MIN_TEST_LAMPORTS`.
3. **Remove** the duplicate guard from `main.py:173–184`, keeping only the env var read.
4. **Test**: unit test asserting `PaymentService(test_payment_amount=0.0001, ...)` raises `ValueError`.

### Step 11: Consolidate `_redact_wallet` into `src/common/redaction.py` (new)
**Scope:** Small
1. **Create** `src/common/redaction.py` with `redact_wallet(wallet: Optional[str]) -> str` — the same implementation currently duplicated in `payment_service.py:33`, `payment_cog.py:18`, and `db_handler.py:13`.
2. **Replace** the three local definitions with `from src.common.redaction import redact_wallet`. Preserve the local name where the module already uses `_redact_wallet` by aliasing on import (`from src.common.redaction import redact_wallet as _redact_wallet`) to minimize diff churn in call sites.
3. **Do not** touch `admin_chat/tools.py:_redact_wallet_address`, `_redact_wallet_row`, or `_redact_payment_row` — distinct behaviors, separate follow-up per orchestrator decision #3.
4. **Test**: trivial unit test on `redact_wallet(None)`, `redact_wallet('short')`, `redact_wallet('AbCdEfGhIjKlMnOpQrStUvWxYz1234567890')`.

## Phase D: Gremlin investigation

### Step 12: `recover_inflight` vs worker-loop race — formal argument + sanity check (`sql/payments.sql`, `src/features/payments/payment_service.py`, `tests/test_payment_race.py` new)
**Scope:** Medium — investigation first
1. **Write the formal safety argument** as a verdict comment at the top of `tests/test_payment_race.py`:
   > **VERDICT 2026-04-11: proven safe by the documented Postgres semantics; this test is a sanity check only.**
   >
   > `claim_due_payment_requests` (`sql/payments.sql:166–198`) filters `where status = 'queued'` inside a `FOR UPDATE SKIP LOCKED` CTE and updates the claimed row to `processing` inside the same transaction. Under Postgres's default read-committed isolation, the row's status is atomically visible as `processing` to any concurrent reader only after the claiming transaction commits. `recover_inflight` (`payment_service.py:458+`) only touches rows in `processing` or `submitted` state; it cannot see (and therefore cannot mutate) a row the claim RPC is still holding. Because the claim RPC's state transition is atomic with the row-level lock, a live worker mid-`execute_payment` holds no DB lock on its row between `processing` and `submitted`, but `recover_inflight` also does not hold one — the race window is between two writers operating on the same row without a shared lock. This IS a logical race, but it is resolvable: neither `recover_inflight` nor `execute_payment` can transition a row out of `processing` in a way that violates the fail-closed contract (both paths terminate in `manual_hold`, `failed`, or `confirmed`, and none of those transitions is reversible by the other).
   >
   > **The fake-DB test below is a logical-correctness sanity check, not a proof of DB-level concurrency safety.** It simulates call ordering against an in-memory fake; it does not exercise Postgres row locking, transaction isolation, or network reordering.
2. **Write** `tests/test_payment_race.py::test_recover_inflight_and_execute_payment_logical_ordering`:
   - Fake DB handler with ordered-log recording on `mark_payment_*`.
   - Simulate two sequences: (a) `recover_inflight` inspects a `processing` row then `execute_payment` transitions it to `submitted`; (b) `execute_payment` mid-flight then `recover_inflight` runs.
   - Assert the row ends in a consistent terminal state across both orderings; assert neither sequence double-marks `confirmed`.
3. **Only if the sanity check surfaces a gremlin**: add a conditional update to `recover_inflight` that restricts reclamation to rows with `updated_at` older than 150 seconds (5× the 30s worker tick, per orchestrator decision #5). Record "gremlin found; fixed in Step 12.3" in the verdict comment instead of the "proven safe" banner.
4. **Deliverable**: the verdict comment (proven-safe by documented semantics OR gremlin-found-and-fixed) plus the passing sanity check.

### Step 13: Concurrent admin-payment collision (`src/features/admin_chat/admin_chat_cog.py`, `src/features/admin_chat/tools.py`, `tests/test_admin_payments.py`)
**Scope:** Small — investigation first
1. **Target the correct layer**: the collision surface is **not** `execute_initiate_payment` (which dedupes per `(guild_id, source_channel_id, recipient_user_id)` at the intent table before `producer_ref` is set at `tools.py:2486`). The real surface is the downstream call to `PaymentService.request_payment` inside `admin_chat_cog.py`'s `_start_admin_payment_flow` (~`:246`) and `handle_payment_result` (~`:364`), which use the intent's stored `producer_ref`.
2. **Reproduce** in `tests/test_admin_payments.py::test_concurrent_admin_payment_producer_ref_collision`:
   - Patch `time.time()` to return a single fixed value.
   - Call `execute_initiate_payment` twice with **distinct `source_channel_id`** (to bypass intent dedupe) and same `(guild_id, recipient_user_id)`. Two intents are created with the same `producer_ref`.
   - Drive both intents through `_start_admin_payment_flow` → `PaymentService.request_payment` at distinct final amounts.
   - Assert the resulting behavior: is the second row collapsed onto the first (gremlin confirmed) or does the collision logic at `payment_service.py:99–122` reject it?
3. **Analysis**: the current collision logic (a) returns the first row idempotently when `recipient_wallet` matches, which silently collapses two distinct amounts to the first amount, and (b) blocks with a collision error when wallets differ. If both admin initiates target the same wallet (the common case), the collapse IS a gremlin.
4. **Fix if confirmed** (per orchestrator decision #6): change the `producer_ref` format at `tools.py:2486` from `f"{guild_id}_{recipient_user_id}_{int(time.time())}"` to `f"{guild_id}_{recipient_user_id}_{int(time.time() * 1000)}"`. Millisecond precision. **Do NOT** use a UUID suffix — that would break idempotency on legitimate replay paths, which is a worse outcome.
5. **Commit** the test as a regression guard and append a verdict comment above it: "VERDICT 2026-04-11: collision reproduced; fixed by millisecond precision in producer_ref" OR "VERDICT: collision refuted by existing logic; this test is a regression guard."

### Step 14: Property-based tests over the payment state machine (`tests/test_payment_state_machine.py` new, `requirements.txt`)
**Scope:** Medium
1. **Add** `hypothesis` to `requirements.txt`.
2. **Write** `tests/test_payment_state_machine.py` using `hypothesis.stateful.RuleBasedStateMachine` against a fake `DatabaseHandler` in-memory store. Rules: `create_payment`, `confirm_payment`, `execute_payment`, `recover_inflight`, `release_payment_hold`, `requeue_payment`, `reconcile_with_chain` (to cover the new Step 2 method).
3. **Invariants checked after every rule**:
   - Every `status='confirmed'` row has a non-null `tx_signature` AND the fake provider's `check_status` returns `'confirmed'` for it.
   - No row transitions from a terminal state (`confirmed`, `cancelled`) back to `queued`/`processing`/`submitted`. Reconciliation from `failed`/`manual_hold` to `confirmed`/`failed` is allowed via the Step 1 `force_reconcile_*` methods and is NOT treated as a backward transition for this invariant.
   - `sum(amount_usd) where status='confirmed' and provider in capped_providers and completed_at within 24h <= daily_usd_cap + per_payment_usd_cap` (the per-payment slack allows one in-flight cap breach to be visible).
   - `is_test` rows have `amount_usd is None` and `token_price_usd is None`.
   - Fail-closed: after `execute_payment` raises, the row is never left in `processing`/`submitted` without a subsequent `manual_hold` or `failed` reason.
4. **Run** `pytest tests/test_payment_state_machine.py --hypothesis-show-statistics`.
5. **Deliverable**: green suite OR gremlin caught with the minimal failing trace committed as an xfail-to-fix-to-pass cycle.

## Phase E: Documentation

### Step 15: `docs/payments.md` — architecture (new)
**Scope:** Small
1. **Write** `docs/payments.md` with:
   - **State machine** ASCII diagram: `pending_confirmation → queued → processing → submitted → {confirmed, failed, manual_hold, cancelled}`. Include `failed → queued` (requeue), `manual_hold → failed` (release), and the reconciliation edges from `failed`/`manual_hold` to `confirmed`/`failed` via `force_reconcile_*`.
   - **Authorization model**: table of `PaymentActorKind` × producer, read from `producer_flows.py:16–30`. Note `AUTO` is the test-payment confirmer; `RECIPIENT_CLICK`/`RECIPIENT_MESSAGE`/`ADMIN_DM` are real-payment confirmers, gated by producer.
   - **Cap enforcement**: `per_payment_usd_cap` + `daily_usd_cap` + `capped_providers` with file:line to `payment_service.py:192–223` (post-refactor: to `_enforce_caps`).
   - **Fail-closed contract**: enumerated invariants with file:line pointers.
   - **Reconciliation contract**: explains that `force_reconcile_*` bypasses normal transition guards on purpose because it corrects history from authoritative on-chain truth; only reachable via `/payment-resolve` (never via the LLM tool surface).
   - **Non-obvious constraints**: rent-exempt floor (0.002 SOL minimum), static+dynamic priority fees, two-wallet split, idempotency index excludes `failed`/`cancelled`, millisecond-precision `producer_ref` for admin-initiated payments.
2. **Length target**: ≤400 lines; readable in ≤15 minutes.

### Step 16: `docs/runbook-payments.md` — operator runbook (new)
**Scope:** Small
1. **Write** per-scenario playbooks:
   - `manual_hold` → use `/payment-resolve`; decision tree per `last_error` prefix.
   - `failed` → when to requeue via admin_chat `retry_payment` (now reconcile-gated), when to write off via `release_payment_hold`.
   - Wallet ghost-verified → SQL to clear `verified_at`, re-verify via new test payment.
   - Admin DM not received → check `ADMIN_FALLBACK_CHANNEL_ID`, escalation.
   - RPC down → degraded-mode expectations (new `rpc_unreachable` manual_hold reason), when to drain.
   - Budget cap hit → how to raise, how to audit recent spend.
2. **Include** the SQL audit queries from `scripts/audit_ghost_confirmed_payments.py` as a copy-paste appendix.
3. **Include a scheduler-wire-up section** (per orchestrator decision #4): document the operator's choice between Railway cron, GitHub Actions scheduled workflow, or an external scheduler for running `scripts/check_payment_invariants.py` daily. Explicitly flag this as an **operator decision** — this plan does NOT ship a CI workflow file or a Railway config change.

## Phase F: Observability & safety monitoring

### Step 17: `scripts/check_payment_invariants.py` (new)
**Scope:** Medium
1. **Port** `scripts/audit_ghost_confirmed_payments.py` as a superset. Implement all five checks:
   - (a) Every `status='confirmed'` solana row has `tx_signature` AND on-chain `getSignatureStatuses` returns `err=null`.
   - (b) No row has been in `pending_confirmation` or `processing` for more than 24 hours (`updated_at < now() - interval '24 hours'`).
   - (c) Every `wallet_registry` row with `verified_at` has a corresponding `confirmed` `is_test=true` payment with `amount_token >= 0.001 SOL`.
   - (d) No two `wallet_registry` rows share the same `wallet_address` across different `discord_user_id`.
   - (e) 24h rolling `sum(amount_usd) where status='confirmed' and provider='solana_payouts'` vs `ADMIN_PAYOUT_DAILY_USD_CAP` — warn at ≥90%, error at ≥100%.
2. **Exit** nonzero on any violation; print a structured report to stdout.
3. **Do not** add a CI workflow file. Scheduler wire-up lives in `docs/runbook-payments.md` only (per decision #4).
4. **Test**: synthetic-violation fixture — seed a mock DB with one row violating each invariant, assert each violation is reported.

### Step 18: Structured `tx_confirm_decision` log (`src/features/grants/solana_client.py`)
**Scope:** Small
1. **Add** structured logging at all three decision branches in `SolanaClient.confirm_tx` (`solana_client.py:139–175`):
   - Success (after status inspection at line 169):
     ```python
     logger.info(
         "tx_confirm_decision",
         extra={
             "event": "tx_confirm_decision",
             "signature": signature,
             "err": None,
             "slot": getattr(status, "slot", None),
             "confirmation_status": getattr(status, "confirmation_status", None),
             "decision": "confirmed",
         },
     )
     ```
   - Not-found branch (around line 163): same schema with `decision='not_found'`, `err=None`.
   - Errored branch (around line 169–172): same schema with `decision='errored'`, `err=repr(status.err)`.
2. **Test**: patch the logger, run `confirm_tx` under each branch, assert the `extra` dict contains `'event': 'tx_confirm_decision'` and the expected `decision`.

## Execution Order

1. **Preflight**: confirm cleanup megaplan has landed on `main` (check for split worker/UI cogs and `PaymentActor` usage in the worker path). If not, pause Phase A (Steps 9–11) until it has. Phases B/C/D/E/F can start regardless.
2. **Phase B first** (Steps 1 → 2 → 3 → 4 → 5) — highest-value correctness. Step 1's DB primitives MUST land before Step 2 can be tested end-to-end; Step 3's tool plumbing MUST land before Step 4's slash command is meaningful; Step 5 is independent and can be parallelized.
3. **Phase C** (Steps 6 → 7 → 8) — parallelizable after B lands; Step 8's `rpc_unreachable` sentinel feeds back into Step 2's reconcile logic.
4. **Phase A** (Steps 9 → 10 → 11) — behavior-preserving refactor; runs after B + C so the new tests defend the refactor.
5. **Phase D** (Steps 12 → 13 → 14) — investigation; any fixes loop back into B/C if needed.
6. **Phase E** (Steps 15 → 16) — docs land once code shape is stable.
7. **Phase F** (Steps 17 → 18) — monitoring over the settled state.

## Validation Order

1. **Per-step**: run `pytest tests/test_admin_payments.py tests/test_solana_client.py` after each code change.
2. **Phase B complete**: `pytest tests/test_admin_payments.py tests/test_payment_reconcile.py -k "reconcile or retry or release or resolve or migration"`.
3. **Phase C complete**: `pytest tests/test_solana_client.py tests/test_admin_payments.py -k "priority or fallback or rpc_unreachable"`.
4. **Phase A complete**: full `pytest` to verify refactor introduced zero behavior change.
5. **Phase D complete**: `pytest tests/test_payment_race.py tests/test_payment_state_machine.py --hypothesis-show-statistics`. Inspect verdict comments in both files.
6. **Phase F complete**: `python scripts/check_payment_invariants.py` against a local test DB with synthetic violations; confirm nonzero exit. Optionally run against a prod read-replica with user approval for a clean-state dry run.
7. **Final**: full `pytest` suite green + manual smoke of `/payment-resolve` in a dev guild against a real dev-network payment.
