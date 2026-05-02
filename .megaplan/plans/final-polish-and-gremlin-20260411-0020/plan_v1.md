# Implementation Plan: Final Polish & Gremlin-Investigation Pass for Solana Payout Subsystem

## Overview
This is the fourth pass on the payouts subsystem, landing AFTER the in-flight cleanup megaplan (`payment-subsystem-follow-up-20260410-2339`) and the pending admin-DM approval megaplan. Goal: close remaining gremlin-hiding spots, harden edge cases, produce ops documentation, and establish daily safety monitoring.

Current repo shape (verified 2026-04-11):
- `src/features/payments/payment_service.py` — ~600 lines; `request_payment` is a single ~250-line function (lines 69–316) that will be the refactor target (A1). Already has `PaymentActor`/`PaymentActorKind`, `_authorize_actor`, `confirm_payment`, `execute_payment`, `recover_inflight`. Has a local `_redact_wallet` at line 33.
- `src/features/payments/payment_cog.py` — ~580 lines, still monolithic worker+UI+DM. The cleanup megaplan will split this into `PaymentWorkerCog` and `PaymentUICog`; the C8 DM fallback and A3 redact consolidation target the post-split worker cog.
- `src/features/payments/producer_flows.py` — already exists with `PRODUCER_FLOWS` registry and `get_flow`.
- `src/features/payments/solana_provider.py` — thin adapter around `src/features/grants/solana_client.py`. **The idea text says "solana_client.py" for C7/F16 and it is `src/features/grants/solana_client.py` (not inside `payments/`).** F16 logging lives in `SolanaClient.confirm_tx` (grants/solana_client.py:139–175) and C7 `getRecentPrioritizationFees` lives in `SolanaClient` (grants/solana_client.py:52–54 current static floor, `send_sol` at 67–137).
- `src/features/admin_chat/tools.py` — `execute_retry_payment` at 2355–2370 and `execute_release_payment` at 2392–2413 are the B4 targets. `execute_initiate_payment` at 2438+ constructs `producer_ref = f"{guild_id}_{recipient_user_id}_{int(time.time())}"` at line 2486 — the B-item-free (D11) collision target.
- `sql/payments.sql` — `claim_due_payment_requests` RPC at lines 166–198 with `FOR UPDATE SKIP LOCKED`; `tx_signature_history` column already added at line 209+. `payment_requests` has unique index on `tx_signature` (line 138), a partial-unique index on `(producer, producer_ref, is_test)` excluding `failed/cancelled` (line 134).
- `main.py:167–209` — the rent-exempt guard (A2 target) currently lives at boot, not in `PaymentService.__init__`. Providers are constructed as `solana_grants` and `solana_payouts` (already split).
- `scripts/audit_ghost_confirmed_payments.py` — existing audit script, the seed for F15 productionized invariant checker.
- `docs/` — no existing payments doc; `docs/payments.md` and `docs/runbook-payments.md` are new.
- `tests/test_admin_payments.py` — uses `FakeAdminPaymentService`; `tests/test_solana_client.py` already exists. No property-based (hypothesis) tests yet; `hypothesis` is not in `requirements.txt`.

Constraints that matter:
- **Sequencing**: Phase A depends on cleanup landing because file shapes change. Phases B–F can ship in parallel after cleanup. This plan assumes cleanup + admin-DM megaplan are merged before execution starts.
- **Fail-closed semantics** are non-negotiable. Every change must preserve them. Phase D items are investigation-first — acceptable outputs are "proven safe via test" or "fixed with commit".
- **Read-only audits** must never mutate payment state.
- **B4's on-chain recheck** is the teeth of the plan — it closes the current double-send hole where `execute_retry_payment` nulls `tx_signature` and requeues without verifying the prior signature didn't land.

## Phase A: Code refactor (post-cleanup shape)

### Step 1: Decompose `PaymentService.request_payment` into named helpers (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Extract** the following pure-ish helpers from `request_payment` (currently `payment_service.py:69–316`), keeping behavior and signatures unchanged from the caller's perspective:
   - `_normalize_inputs(**kwargs) -> NormalizedRequest` — returns a dataclass with `wallet`, `producer`, `producer_ref`, `chain`, `provider_key`, etc., or `None` on validation failure. Absorbs lines 91–98 and the provider normalization at 139.
   - `_detect_collision_and_idempotent_row(normalized, guild_id, is_test) -> tuple[Optional[row], Optional[row], Optional[row]]` — returns `(collision_row, blocking_prior_row, idempotent_row)`. Absorbs lines 99–122.
   - `_enforce_caps(normalized, amount_token, amount_usd, provider) -> CapResult` — returns `(cap_breach, derived_usd, derived_price)`. Absorbs lines 189–223.
   - `_derive_amounts(normalized, is_test, amount_usd, amount_token, provider) -> AmountsResult` — absorbs lines 161–188.
   - `_persist_row(record, normalized, guild_id, is_test, collision_row, blocking_prior_row) -> Optional[dict]` — absorbs the `create_payment_request` + duplicate re-read fallback at lines 286–316.
2. **Rewrite** `request_payment` as a thin orchestrator (~40 lines) calling the helpers in sequence. Keep public signature identical.
3. **Do not** change any return semantics, log messages, or side-effect ordering. This is a behavior-preserving refactor.
4. **Run** `tests/test_admin_payments.py` and any existing payment_service tests unchanged — they must pass without modification.

### Step 2: Move rent-exempt guard into `PaymentService.__init__` (`src/features/payments/payment_service.py`, `main.py`)
**Scope:** Small
1. **Add** a module-level constant `MIN_TEST_LAMPORTS = 2_000_000` and `RENT_EXEMPT_LAMPORTS = 890_880` in `payment_service.py`.
2. **Validate** in `PaymentService.__init__` (line 45+): compute `int(self.test_payment_amount * 1_000_000_000)` and raise `ValueError` with the same message currently at `main.py:178–184` if below `MIN_TEST_LAMPORTS`.
3. **Remove** the duplicate check from `main.py:173–184`, keeping only the env var read. Comment can stay as a one-liner pointing to `PaymentService` for the reason.
4. **Add** a unit test in `tests/test_admin_payments.py` (or a new `tests/test_payment_service.py`) that asserts `PaymentService(test_payment_amount=0.0001, ...)` raises `ValueError`.

### Step 3: Consolidate `_redact_wallet` helper (`src/common/discord_utils.py`, `src/features/payments/payment_service.py`, `src/features/payments/payment_cog.py` — or post-cleanup worker/UI cogs, `src/common/db_handler.py`)
**Scope:** Small
1. **Add** `redact_wallet(wallet: Optional[str]) -> str` to `src/common/discord_utils.py` (or a new `src/common/redaction.py` if the user prefers non-Discord-specific placement — see Question 2).
2. **Replace** the local `_redact_wallet` definitions at `payment_service.py:33`, `payment_cog.py:18` (and any post-cleanup worker cog variant), and `db_handler.py:13` with an import.
3. **Do not** consolidate `admin_chat/tools.py:_redact_wallet_address` — it handles dict rows and has a distinct signature; flag for a follow-up if the user wants that unified (see Question 3).

## Phase B: Correctness fixes

### Step 4: On-chain recheck gate for retry/release (`src/features/admin_chat/tools.py`, `src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Add** a new `PaymentService.reconcile_with_chain(payment_id, *, guild_id) -> ReconcileDecision` method. Logic:
   - Fetch payment row; if no `tx_signature`, return `decision='allow_requeue'` (nothing to double-send).
   - Call `provider.check_status(tx_signature)`.
   - If `'confirmed'`: call `mark_payment_confirmed` and return `decision='reconciled_confirmed'`.
   - If `'failed'`: call `mark_payment_failed` (if not already) and return `decision='reconciled_failed'`.
   - If `'not_found'`: check blockhash-expiry heuristic — since we can't cheaply check blockhash expiry from here, treat `not_found` as safe to requeue **only if** `submitted_at` is older than a conservative window (e.g. 150 seconds, longer than Solana's ~90s blockhash lifetime). Otherwise return `decision='keep_in_hold'` with reason 'signature not_found but too recent to be safe'.
   - On provider exception: return `decision='keep_in_hold'` with reason 'RPC unreachable during reconcile'.
2. **Gate** `execute_retry_payment` at `tools.py:2355–2370`: call `payment_service.reconcile_with_chain` first; only proceed to `db_handler.requeue_payment` if decision is `allow_requeue`. For reconciled_confirmed/reconciled_failed, return a clear success message reporting the reconciliation; for `keep_in_hold`, call `mark_payment_manual_hold` with the reason and return a non-success result.
3. **Gate** `execute_release_payment` at `tools.py:2392–2413` the same way — before `release_payment_hold`, reconcile; if on-chain state says confirmed/failed, refuse the release and return the reconciled truth instead.
4. **Test** in `tests/test_admin_payments.py`:
   - Fake provider returning `'confirmed'` → retry refuses requeue, marks confirmed.
   - Fake provider returning `'failed'` → retry refuses requeue, marks failed.
   - Fake provider returning `'not_found'` with recent `submitted_at` → keep_in_hold.
   - Fake provider returning `'not_found'` with old `submitted_at` → allow requeue.
   - Fake provider raising → keep_in_hold.

### Step 5: New `!payment-resolve <payment_id>` admin-only command (`src/features/payments/payment_cog.py` or post-cleanup UI cog, `src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** a slash command `/payment-resolve` (admin-only via `@app_commands.checks.has_permissions(administrator=True)` or ADMIN_USER_ID check) that accepts `payment_id`, calls `payment_service.reconcile_with_chain` from Step 4, writes an audit entry to `tx_signature_history`, and replies with the decision.
2. **Also expose** it as an admin_chat tool `execute_payment_resolve` that wraps the same service method, so the agent can trigger it from chat.
3. **Append** to `tx_signature_history` on every reconcile action with `reason='admin_resolve'`.
4. **Test**: call `reconcile_with_chain` against a fixture that mocks on-chain states and assert the audit entry is appended and the reply string matches each decision branch.

### Step 6: Provider name migration path (`src/features/payments/payment_service.py`, `src/common/db_handler.py`)
**Scope:** Small
1. **Add** `PaymentService.migrate_legacy_provider_rows(guild_ids) -> int` that:
   - Queries `payment_requests` for rows with `provider='solana'` in writable guilds (new `db_handler.get_legacy_provider_payment_requests`).
   - For each row: `producer='grants'` → rewrite provider to `solana_grants`; `producer='admin_chat'` → `solana_payouts`; unknown → `mark_payment_manual_hold` with reason `'legacy provider could not be mapped: unknown producer={producer}'`.
   - Log one `logger.warning` per row at INFO level with before/after for observability.
2. **Call** it once at worker startup from `PaymentWorkerCog.cog_load` (post-cleanup) or `PaymentCog.cog_load` (today).
3. **Test**: fake DB with three legacy rows (grants, admin_chat, unknown); assert correct rewrites and one manual_hold.

## Phase C: Robustness edge cases

### Step 7: Dynamic priority fees via `getRecentPrioritizationFees` (`src/features/grants/solana_client.py`)
**Scope:** Medium
1. **Add** `SolanaClient._get_dynamic_priority_fee(client) -> int` that calls `client.get_recent_prioritization_fees()` (or raw RPC if the SDK method doesn't exist — verify at implementation time), computes the 75th percentile of non-zero fees from the returned array, and returns `clamp(percentile, floor=self.priority_fee_micro_lamports, ceiling=int(os.getenv('SOLANA_PRIORITY_FEE_CEILING_MICRO_LAMPORTS', '1000000')))`.
2. **On RPC error or empty response**, fall back to `self.priority_fee_micro_lamports` (current static floor) and log at WARNING.
3. **Call** it inside `send_sol` at `solana_client.py:94–98` to replace the static `compute_unit_price = self.priority_fee_micro_lamports` with the dynamic value; log the chosen value alongside the existing priority-fee log.
4. **Test** in `tests/test_solana_client.py`: mock `get_recent_prioritization_fees`; assert 75th percentile math, floor clamp, ceiling clamp, and fallback-on-error behavior.

### Step 8: Admin DM fallback channel (`src/features/payments/payment_cog.py` or post-cleanup worker cog)
**Scope:** Small
1. **Add** `ADMIN_FALLBACK_CHANNEL_ID` env read in `__init__`.
2. **Refactor** `_dm_admin_payment_success` (`payment_cog.py:288–344`) and `_dm_admin_payment_failure` (`payment_cog.py:346–406`) to share a helper `_deliver_admin_alert(message: str)`:
   - Try DM to `ADMIN_USER_ID`.
   - On `discord.Forbidden` or `discord.HTTPException` rate-limit: post to `ADMIN_FALLBACK_CHANNEL_ID` via `safe_send_message`.
   - On both failing: `logger.error('[PaymentCog] admin alert undeliverable', extra={'message_preview': ...})`.
3. **Test**: fake bot where DM raises Forbidden; assert fallback channel `send` is called. Fake bot where both fail; assert ERROR log.

### Step 9: RPC-down vs timeout distinction (`src/features/payments/payment_service.py`, `src/features/payments/solana_provider.py`)
**Scope:** Small
1. **Add** a new sentinel status `'rpc_unreachable'` alongside `'timeout'` in `SolanaProvider.confirm_tx` and `.check_status`. Return it only when the underlying RPC call raises a connection-error family exception (`aiohttp.ClientConnectionError`, `asyncio.TimeoutError` on the connect phase, `solana.rpc.core.RPCException` with a connection subtype — verify exact types at impl time).
2. **Update** `PaymentService._confirm_submitted_payment` (line 548+) to branch on `'rpc_unreachable'`: `mark_payment_manual_hold` with reason `'rpc_unreachable: confirmation RPC offline'` — distinct from the existing `'Confirmation timed out after submission'` message.
3. **Do the same** in `PaymentService.recover_inflight` (line 458+) for the submitted branch.
4. **Test**: fake provider returning `'rpc_unreachable'` → assert distinct manual_hold reason.

## Phase D: Gremlin investigation

### Step 10: Verify `recover_inflight` vs worker-loop race (`sql/payments.sql`, `tests/test_payment_race.py` new)
**Scope:** Medium — investigation first
1. **Read** `claim_due_payment_requests` at `sql/payments.sql:166–198`. The RPC filters `status = 'queued'` (line 178), which means it cannot claim a `processing` or `submitted` row. `recover_inflight` only touches `processing`/`submitted` rows. **Initial hypothesis: these cannot collide.** Verify this is true under the worker-loop's `execute_payment` transition from `processing → submitted → confirmed`:
   - Worker claims a `queued` row and transitions it to `processing` inside the RPC (line 188).
   - While the worker is mid-`send`, restart happens.
   - New process `recover_inflight` sees the row in `processing` and moves it to `manual_hold` or `failed+queued`.
   - Meanwhile, the worker that survived (or a second worker) might also be writing to the same row.
2. **Write** `tests/test_payment_race.py::test_recover_inflight_race` using a fake DB handler that tracks call order to `mark_payment_*`: simulate `execute_payment` mid-flight and `recover_inflight` running simultaneously; assert the row ends in a consistent terminal state (no double-`mark_payment_confirmed`, no processing→confirmed-from-stale-read).
3. **Deliverable**: a written verdict comment at the top of the test file (`# VERDICT 2026-04-11: proven safe by this test` or `# VERDICT: gremlin found — fixed in Step N`). If a race is discovered, add a fix: e.g., wrap `recover_inflight` in a conditional update that requires `status IN ('processing','submitted') AND updated_at < now() - interval '2 minutes'` to avoid stealing a row a live worker is actively handling.

### Step 11: Verify concurrent admin-payment collision (`src/features/admin_chat/tools.py`, `tests/test_admin_payments.py`)
**Scope:** Small — investigation first
1. **Reproduce**: write a test where two calls to `execute_initiate_payment` with the same `(guild_id, recipient_user_id)` within the same wall-clock second both compute the same `producer_ref` at `tools.py:2486`. Run both through `PaymentService.request_payment`.
2. **Analyze** the existing collision logic at `payment_service.py:99–122`: if both requests are the same wallet, idempotent return hands back the same row — **not a double-send**. If the two admin requests are for different amounts to the same wallet in the same second, the second request silently gets the first request's row. **This may be a gremlin** — two different amounts collapse to one.
3. **Deliverable**: If the test confirms the collision collapses two distinct amounts, either:
   - **Fix option A**: change `producer_ref` to include microsecond precision: `f"{guild_id}_{recipient_user_id}_{int(time.time() * 1000)}"`. Cheapest fix, matches current semantics.
   - **Fix option B**: include a random UUID suffix: `f"{guild_id}_{recipient_user_id}_{uuid4().hex[:8]}"` — strongest, breaks all idempotency on this path (which is acceptable because admin-initiated payments aren't meant to be retried via idempotency).
   - Prefer Option A unless the test shows millisecond collisions are plausible. Commit the chosen fix and a regression test.

### Step 12: Property-based tests with hypothesis (`tests/test_payment_state_machine.py` new, `requirements.txt`)
**Scope:** Medium
1. **Add** `hypothesis` to `requirements.txt`.
2. **Write** `tests/test_payment_state_machine.py` that uses `hypothesis.stateful.RuleBasedStateMachine` with a fake `DatabaseHandler` backing store. Rules: `create_payment`, `confirm_payment`, `execute_payment`, `recover_inflight`, `release_payment_hold`, `requeue_payment`.
3. **Invariants** (checked after every rule):
   - Every `status='confirmed'` row has `tx_signature` (non-null) AND the fake provider's `check_status` returns `'confirmed'` for it.
   - No transition from a terminal state (`confirmed`, `cancelled`) back to `queued`/`processing`/`submitted`.
   - `sum(amount_usd for status='confirmed' in last 24h for capped provider) <= daily_usd_cap + per_payment_usd_cap` (allowing one-in-flight slack).
   - `is_test` rows never populate `amount_usd` or `token_price_usd`.
   - Fail-closed: if `execute_payment` returns early because a provider raised, the row is never left in `processing` without a `manual_hold` reason.
4. **Run** `pytest tests/test_payment_state_machine.py --hypothesis-show-statistics` to get output.
5. **Deliverable**: the test suite passing, or the gremlin caught and fixed with the offending rule-sequence recorded in the test.

## Phase E: Documentation

### Step 13: `docs/payments.md` — architecture (`docs/payments.md` new)
**Scope:** Small
1. **Write** `docs/payments.md` with these sections:
   - **State machine**: ASCII diagram covering `pending_confirmation → queued → processing → submitted → {confirmed, failed, manual_hold, cancelled}`. Include arrows for `failed → queued` (requeue) and `manual_hold → failed`.
   - **Authorization model**: table of `PaymentActorKind` × producer with who can confirm what (read from `producer_flows.py:16–30`). Note that `AUTO` is the test-payment confirmer and `RECIPIENT_CLICK/RECIPIENT_MESSAGE/ADMIN_DM` are real-payment confirmers, gated by producer.
   - **Cap enforcement**: `per_payment_usd_cap` + `daily_usd_cap` + `capped_providers` with file:line to `payment_service.py:192–223`.
   - **Fail-closed contract**: enumerated invariants with file:line pointers.
   - **Non-obvious constraints**: rent-exempt test amount minimum (0.002 SOL), static+dynamic priority fees (post-Phase C), two-wallet split (`solana_grants` vs `solana_payouts`), idempotency index excludes `failed`/`cancelled`.
2. **Length target**: ≤400 lines; readable in ≤15 minutes.

### Step 14: `docs/runbook-payments.md` — operator runbook (`docs/runbook-payments.md` new)
**Scope:** Small
1. **Write** `docs/runbook-payments.md` with per-scenario playbooks:
   - `manual_hold` → use `/payment-resolve`, decision tree per `last_error` prefix.
   - `failed` → when to requeue via admin_chat retry (now gated by reconcile), when to write off via `release_payment_hold` to `failed`.
   - Wallet ghost-verified → clear `verified_at` via SQL, re-verify via new test payment.
   - Admin DM not received → check `ADMIN_FALLBACK_CHANNEL_ID`, escalation.
   - RPC down → degraded-mode expectations, what queues, when to manually drain.
   - Budget cap hit → how to raise, how to audit recent spend.
2. **Include** the SQL audit queries from `scripts/audit_ghost_confirmed_payments.py` as a copy-paste appendix.

## Phase F: Observability & safety monitoring

### Step 15: `scripts/check_payment_invariants.py` (`scripts/check_payment_invariants.py` new)
**Scope:** Medium
1. **Port** `audit_ghost_confirmed_payments.py` as a superset. Add checks:
   - (a) Every `status='confirmed'` solana row has `tx_signature` AND on-chain `getSignatureStatuses` returns `err=null`.
   - (b) No row has been in `pending_confirmation` or `processing` for more than 24 hours (`updated_at < now() - interval '24 hours'`).
   - (c) Every `wallet_registry` row with `verified_at` has a corresponding `confirmed` `is_test=true` payment with `amount_token >= 0.001 SOL`.
   - (d) No two `wallet_registry` rows share the same `wallet_address` across different `discord_user_id` (detect address reuse).
   - (e) Daily cap aggregate: `sum(amount_usd) where status='confirmed' and provider='solana_payouts' and completed_at > now() - interval '24 hours'` compared against `ADMIN_PAYOUT_DAILY_USD_CAP` — flag if ≥90% (warning) or ≥100% (error).
2. **Exit nonzero** on any violation. Print a structured report to stdout.
3. **Add** a GitHub Actions workflow or README note wiring this into daily CI (see Question 4). For now, the script itself is sufficient; the scheduling wire-up is info-priority.
4. **Test**: a synthetic-violation fixture — a seeded test DB with one row violating each invariant, asserting each violation is reported.

### Step 16: Structured `tx_confirm_decision` log (`src/features/grants/solana_client.py`)
**Scope:** Small
1. **In** `SolanaClient.confirm_tx` at `solana_client.py:139–175`, after the status inspection at line 169, add:
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
2. **Also log** on the error branches (lines 163–172) with `decision='not_found'` / `decision='errored'` and the full `err` repr.
3. **Test**: patch `logger` and assert the `extra` dict contains `'event': 'tx_confirm_decision'`.

## Execution Order

1. **Preflight** (before any changes): confirm cleanup megaplan has landed on `main` by checking for the split worker/UI cogs and `PaymentActor` usage in the worker path. If not yet landed, pause Phase A until it is. Phases B/C/D/E/F can start regardless.
2. **Phase B** first (B4 → B5 → B6) — highest-value correctness (closes the retry double-send hole).
3. **Phase A** (A1 → A2 → A3) — behavior-preserving refactor is safer once B has added its tests.
4. **Phase C** (C7 → C8 → C9) — parallelizable after B lands.
5. **Phase D** (D10 → D11 → D12) — investigation; fixes feed back into B/C if found.
6. **Phase E** (E13 → E14) — docs finalize once code shape is stable.
7. **Phase F** (F15 → F16) — monitoring over the settled state.

## Validation Order

1. **Per-step**: run `pytest tests/test_admin_payments.py tests/test_solana_client.py` after each code change.
2. **Phase B complete**: targeted `pytest tests/test_admin_payments.py -k "retry or release or resolve or migration"`.
3. **Phase A complete**: full `pytest` to verify refactor didn't break anything.
4. **Phase D complete**: `pytest tests/test_payment_race.py tests/test_payment_state_machine.py` + hypothesis statistics banner.
5. **Phase F complete**: run `python scripts/check_payment_invariants.py` against a local test DB (and against prod read-replica if available, with user approval) and confirm it fails on synthetic violations and passes on a clean DB.
6. **Final**: full `pytest` suite green + manual smoke of `/payment-resolve` in a dev guild.
