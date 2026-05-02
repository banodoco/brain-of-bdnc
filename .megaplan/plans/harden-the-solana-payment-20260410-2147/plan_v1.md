# Implementation Plan: Harden Solana Payment Subsystem

## Overview

Implement the P0/P1/P2 hardening from the adversarial security review against `src/features/payments/` and `src/features/admin_chat/`. The work is layered: fail-closed correctness first (null recipient, prompt-injection defense, inflight wallet lock), then monetary safety (idempotency collision detection, per-payment + rolling daily USD caps on `solana_payouts`), then observability (admin success DM, log audit, RLS posture doc, git history audit).

**Important corrections to the brief:**

1. **RLS is already enabled** on `payment_requests`, `wallet_registry`, and `payment_channel_routes` (`sql/payments.sql:192-199`). Anon/authenticated roles are fully revoked; the bot accesses via service role which bypasses RLS. The RLS-posture doc step is a *comment block describing existing posture*, not a claim that RLS is off.
2. **P0 prompt-injection is already substantially implemented** — the `initiate_payment` tool schema at `src/features/admin_chat/tools.py:580-599` exposes only `recipient_user_id`, `amount_sol`, `reason`. The wallet comes from `db_handler.get_wallet(guild_id, recipient_user_id, 'solana')` at `tools.py:2474`. The work is: (a) grep all admin_chat tool schemas for any sibling wallet_address field, (b) add a defensive `params.pop('wallet_address', None)` + WARN log and a docstring at the top of `execute_initiate_payment`, (c) add a regression test. This step is *verification + defensive assertion*, not a rewrite.
3. **The subtler idempotency bug** is that `get_payment_requests_by_producer` (`db_handler.py:1853`) already returns *all* statuses (no terminal filter) and `request_payment` at `payment_service.py:54-61` unconditionally returns `existing_rows[0]`. If the most-recent prior row is `failed`, the caller gets back the failed row and treats it as a fresh pending row. The fix is two-pronged: (a) collision detection across *all* prior rows (different recipient_wallet → manual_hold new row + admin DM), and (b) narrow the idempotent early-return to *non-terminal* prior rows so legitimate retries after failure land on a fresh row.
4. **The wallet-swap vector** lives in the combination of (a) `upsert_wallet` having no inflight guard and (b) `request_payment`'s `wallet_id` consistency check at `payment_service.py:84-90` rejecting the final payment when the stored wallet mismatches the frozen wallet string — causing silent dropout of the final payout. Fix is to block `upsert_wallet` while an active payment intent exists for that user.
5. **`execute_payment` already uses the stored `recipient_wallet` string** (`payment_service.py:244`). The work here is a regression-pin test only — no code change.

### Key invariants (non-negotiable)

- **Fail-closed.** On cap breach, collision with different wallet, null recipient, or inflight wallet change: create a `manual_hold` row + admin DM, never silent reject (preserves audit trail).
- **Provider-scoped caps.** Caps apply only to providers in `capped_providers` (default `frozenset({'solana_payouts'})`). `solana_grants` MUST remain unaffected.
- **Do NOT change the partial-index predicate** on `uq_payment_requests_active_producer_ref`. Legitimate retries after failure rely on the `failed/cancelled` exclusion. Collision detection stays in app code.
- **Do NOT re-read `recipient_wallet` in `execute_payment`.** Keep the frozen-string semantics at `payment_service.py:244`. Pin with a regression test.
- **Do NOT add cap enforcement in `execute_payment`.** Caps run only in `request_payment`; the scheduler's `claim_due_payment_requests → execute_payment` path must not hit cap logic.
- **Do NOT widen `upsert_wallet`'s block window.** Block *only* while a non-terminal `payment_requests` row OR an active `admin_payment_intents` row exists for `(guild_id, discord_user_id)`. Terminal (completed/failed/cancelled) rows do NOT block.
- **Preserve `privileged_override=True`** for test-payment auto-advance (`admin_chat_cog.py:258-262`). The test payment row carries `recipient_discord_id` (set at line 244) so it does not hit the new null-recipient branch anyway.
- **No secret logging.** New logging in cap/collision/DM paths must redact wallets via existing `_redact_wallet` helpers and must never log env-var values or key material.
- **Cap config must be injectable via constructor kwargs** (not env-only) so existing PaymentService tests remain green with defaults.

## Phase 1: Fail-Closed Correctness (P0 + P0 verification)

### Step 1: Null recipient_discord_id fail-closed (`src/features/payments/payment_service.py`)
**Scope:** Small

1. **Modify** `confirm_payment` at `payment_service.py:193-202`. Replace the current `if not privileged_override and expected_user_id is not None:` with:
   ```python
   if not privileged_override:
       if expected_user_id is None or confirmed_by_user_id is None or int(expected_user_id) != int(confirmed_by_user_id):
           self.logger.warning(
               "[PaymentService] rejected confirmation for %s: expected_user=%s confirmed_by=%s",
               payment_id,
               expected_user_id,
               confirmed_by_user_id,
           )
           return None
   ```
2. **Audit** every `confirm_payment` call site: `payment_cog.py` (button confirmation — always carries a user) and `admin_chat_cog.py:258-262` (test payment auto-confirm via `privileged_override` — actually passes `confirmed_by='auto'` without `privileged_override=True`, see below).
3. **Verify** `admin_chat_cog.py:258-262` — the test payment call passes `confirmed_by='auto'` and `confirmed_by_user_id=int(intent['recipient_user_id'])` but **not** `privileged_override=True`. The test payment row carries `recipient_discord_id=int(intent['recipient_user_id'])` (set at `admin_chat_cog.py:244`), so the equality check succeeds under the new rule. **No change needed at that call site.** Add a comment there explaining why the test still auto-confirms under the tightened rule.

### Step 2: Prompt-injection verification + defensive assertion (`src/features/admin_chat/tools.py`)
**Scope:** Small

1. **Grep** `src/features/admin_chat/tools.py` for every tool input schema (all dicts in `ADMIN_CHAT_TOOLS`) and confirm none accept a field named `wallet_address`, `address`, `solana_address`, `recipient_wallet`, or `wallet`. Current evidence: only `_redact_wallet_address` helper (lines 1072, 1124) uses that name. If any schema is found, strip the field from the schema and from the execute function.
2. **Modify** `execute_initiate_payment` at `tools.py:2438`. At the top of the function (after the `try`/type coercion), add:
   ```python
   # Wallets MUST come from wallet_registry keyed by recipient_user_id.
   # Never trust an LLM-sourced wallet string in the params payload.
   if 'wallet_address' in params or 'recipient_wallet' in params:
       logger.warning(
           "[AdminChat] execute_initiate_payment received wallet_address/recipient_wallet in params; ignoring"
       )
       params.pop('wallet_address', None)
       params.pop('recipient_wallet', None)
   ```
3. **Add** a one-line docstring at `execute_initiate_payment` stating "Wallets are resolved exclusively via `db_handler.get_wallet(...)`; callers MUST NOT pass wallet_address."
4. **Note in plan:** the P0 premise was already substantially satisfied by prior work; this step is *verification + defensive assertion + regression test*, not a rewrite.

### Step 3: Git history secret audit (no code change)
**Scope:** Small

1. **Run** the following audit commands and record findings in the review memo:
   - `git log -p -S 'SOLANA_PRIVATE_KEY' -- .`
   - `git log -p -S 'HELIUS_API_KEY' -- .`
   - `git log -p -S 'ANTHROPIC_API_KEY' -- .`
   - `git log -p -S 'from_bytes' -- .`
   - `git log --all --full-history -- '**/*.env*' '**/.env' '**/secrets*'`
   - `git log -p -G 'sk-[a-zA-Z0-9]{20,}' --all`
   - If `gitleaks` is installed: `gitleaks detect --source . --no-banner`
2. **Document** findings (even if empty) in a memo block at the top of the plan. If *any* real secret is found, STOP and flag to the user for rotation — do NOT attempt rewrite-history fixes automatically.

## Phase 2: Inflight Wallet Lock + Idempotency Collision

### Step 4: Add `has_active_payment_or_intent` helper (`src/common/db_handler.py`)
**Scope:** Small

1. **Add** a new method on `DatabaseHandler` after `list_active_intents`:
   ```python
   def has_active_payment_or_intent(self, guild_id: int, discord_user_id: int) -> bool:
       """True when any non-terminal payment_request row or non-terminal
       admin_payment_intent row exists for this user."""
   ```
2. **Implement** two queries:
   - `payment_requests` WHERE `guild_id=?` AND `recipient_discord_id=?` AND `status NOT IN ('confirmed', 'failed', 'manual_hold', 'cancelled')` LIMIT 1.
   - `admin_payment_intents` WHERE `guild_id=?` AND `recipient_user_id=?` AND `status NOT IN ('completed', 'failed', 'cancelled')` LIMIT 1. (Matches the active predicate already used in `get_active_intent_for_recipient` at `db_handler.py:1754` and `list_active_intents` at `db_handler.py:1782`.)
   - Return True if either returns a row.
3. **Use** `_gate_check(guild_id)` and the existing supabase-guard pattern for consistency with neighboring helpers.

### Step 5: Block `upsert_wallet` while an active intent exists (`src/common/db_handler.py`, `src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small

1. **Modify** `upsert_wallet` at `db_handler.py:1417-1465`. Before the `existing` lookup, add:
   ```python
   if existing and existing.get('wallet_address') != address:
       if self.has_active_payment_or_intent(guild_id, discord_user_id):
           logger.warning(
               "[DB] upsert_wallet blocked: active payment/intent for guild %s user %s",
               guild_id,
               discord_user_id,
           )
           return {'__error__': 'active_payment_or_intent'}
   ```
   Note: use a structured sentinel so the caller can distinguish "blocked" from "error" without raising. Alternatively raise a dedicated `WalletUpdateBlockedError`; pick whichever matches the codebase's error-surfacing convention.
2. **Verify** the structured-return pattern against other `db_handler` helpers before committing. If the codebase prefers exceptions at the db layer, switch to raising and catching at the caller. Check `upsert_wallet`'s current callers via grep first.
3. **Modify** `_handle_wallet_received` at `admin_chat_cog.py:193-218`. After the `upsert_wallet` call, detect the blocked sentinel/exception and surface a user-facing error:
   ```python
   if isinstance(wallet_record, dict) and wallet_record.get('__error__') == 'active_payment_or_intent':
       await self._notify_admin_review(
           message.channel, intent,
           "I cannot update this wallet address: an active payment is in flight. "
           "Please wait for it to resolve or cancel it before changing wallets.",
           resolved_by_message_id=message.id,
       )
       return
   ```
   Keep the existing `if not wallet_record:` branch for the general-failure path.

### Step 6: Idempotency collision detection + reorder early-return (`src/features/payments/payment_service.py`, `src/common/db_handler.py`)
**Scope:** Medium

1. **Add** a new helper `get_payment_requests_for_ref_all(guild_id, producer, producer_ref, is_test)` in `db_handler.py` that wraps the existing query but is explicitly documented as "returns rows in ALL statuses including terminal". In practice this is identical to the existing `get_payment_requests_by_producer` which already has no status filter — so this step may be **just a clarifying rename/alias + docstring**, verifying the behavior. Confirm by reading `db_handler.py:1853-1883` before touching.
2. **Rework** `request_payment` at `payment_service.py:54-61`. Replace the current body with collision-detect-first, idempotent-return-second:
   ```python
   all_prior_rows = self.db_handler.get_payment_requests_by_producer(
       guild_id=guild_id, producer=producer, producer_ref=producer_ref, is_test=is_test,
   )
   normalized_wallet = str(recipient_wallet or '').strip()
   collision = None
   for row in all_prior_rows:
       prior_wallet = str(row.get('recipient_wallet') or '').strip()
       if prior_wallet and normalized_wallet and prior_wallet != normalized_wallet:
           collision = row
           break
   # Non-terminal idempotent return (fresh retries after failed/cancelled must create a new row)
   terminal = {'confirmed', 'failed', 'manual_hold', 'cancelled'}
   active_prior = [r for r in all_prior_rows if str(r.get('status') or '').lower() not in terminal]
   if active_prior and not collision:
       return active_prior[0]
   ```
3. **On collision**: create the new row with `status='manual_hold'`, `last_error='idempotency collision: prior wallet differs'`, and schedule an admin DM. Path: set `record['status'] = 'manual_hold'` and `record['last_error'] = ...` at the record-building step, then after insert call `payment_cog._dm_admin_payment_failure` (or equivalent path) via the terminal handler. Preferred: route through existing `_handle_terminal_payment` so the manual_hold DM fires via the existing path — verify `_handle_terminal_payment` is invoked for newly-inserted manual_hold rows. If not, explicitly fire `_dm_admin_payment_failure` here.
4. **Subtle pitfall** (flagged for the reviewer): the reorder must ensure the early-return at the end of collision-free idempotent flow happens only when prior rows exist *and* none are terminal-only. If all prior rows are terminal (and none are collisions), we must fall through to create a fresh row — this is the bug fix. Add an inline comment explaining this.
5. **Fallback path** at `payment_service.py:167-174` (post-insert re-read for concurrent duplicates): apply the same collision detection against the re-read rows. If the re-read row has a different `recipient_wallet`, log WARN but return the canonical row (DB won this race).

## Phase 3: USD Caps + Admin Success DM

### Step 7: Add `get_rolling_24h_payout_usd` helper (`src/common/db_handler.py`)
**Scope:** Small

1. **Add** `get_rolling_24h_payout_usd(guild_id: int, provider: str) -> float` on `DatabaseHandler`. Query `payment_requests`:
   - `guild_id=?`
   - `provider=?`
   - `is_test = false`
   - `status IN ('pending_confirmation','queued','processing','submitted','confirmed')` (includes all non-terminal + confirmed so burst requests cannot all slip under the cap simultaneously — confirmed stays in the 24h window because the money has been committed).
   - `created_at >= now() - interval '24 hours'`
2. **Sum** `amount_usd` across results; coerce nulls to 0.0. Return float.
3. **Add** a comment noting: "Supabase REST does not provide a serializable transaction; this aggregation races concurrent burst calls. The residual burst window is ~RPC latency (~100-500ms). Downstream manual_hold + admin DM provides the fail-closed backstop."

### Step 8: PaymentService cap enforcement (`src/features/payments/payment_service.py`)
**Scope:** Medium

1. **Extend** `PaymentService.__init__` with new kwargs:
   ```python
   per_payment_usd_cap: Optional[float] = None,
   daily_usd_cap: Optional[float] = None,
   capped_providers: Optional[Iterable[str]] = None,
   ```
   Normalize: `self.per_payment_usd_cap = float(...) if provided else None`, `self.capped_providers = frozenset(p.lower() for p in (capped_providers or ()))`. Defaults are all-None so existing tests (test_admin_payments.py:687, test_social_publish_service.py:474) continue to pass unchanged.
2. **Inject** cap logic in `request_payment` *after* amount_token derivation (lines 90-119) and *before* `create_payment_request` (line 163). Only when `not is_test` AND `normalized_amount_usd is not None` AND `provider.lower() in self.capped_providers`:
   ```python
   cap_breach = None
   if self.per_payment_usd_cap is not None and normalized_amount_usd > self.per_payment_usd_cap:
       cap_breach = f"per-payment cap exceeded: ${normalized_amount_usd:.2f} > ${self.per_payment_usd_cap:.2f}"
   elif self.daily_usd_cap is not None:
       rolling = self.db_handler.get_rolling_24h_payout_usd(guild_id, provider.lower())
       if rolling + normalized_amount_usd > self.daily_usd_cap:
           cap_breach = f"daily cap exceeded: ${rolling:.2f} + ${normalized_amount_usd:.2f} > ${self.daily_usd_cap:.2f}"
   if cap_breach:
       self.logger.warning("[PaymentService] cap breach for %s:%s — %s", producer, producer_ref, cap_breach)
       record['status'] = 'manual_hold'
       record['last_error'] = cap_breach
   ```
3. **Set** `status='manual_hold'` in the `record` dict before the insert (not after) so a single atomic insert lands the row in its terminal hold state.
4. **Trigger** admin DM: since the row is inserted as manual_hold directly, the downstream `_handle_terminal_payment` path does NOT fire automatically (that path is triggered by `handle_payment_result`/webhook, not insertion). After the insert, if `cap_breach` and `created`, explicitly invoke the payment_cog admin DM path. Cleanest: add `self._on_cap_breach_dm_callback` as an optional constructor kwarg that main.py wires to `payment_cog._dm_admin_payment_failure`, and call it here. If wiring is too invasive, alternatively fire via a bot attribute lookup pattern. Pick whichever matches surrounding code (verify by checking how other cross-cog DMs are routed).
5. **Do NOT** add cap logic in `execute_payment`. Verify the scheduler path at `tests/test_scheduler.py:390-584` by running those tests after the change — they should pass untouched.

### Step 9: Admin success DM over threshold (`src/features/payments/payment_cog.py`, `main.py`)
**Scope:** Small

1. **Add** `_dm_admin_payment_success` sibling in `payment_cog.py` modeled on `_dm_admin_payment_failure` (lines 273-333) but with:
   - Distinct prefix: `✅ **Payment Completed**` (versus `🚨 **Payment {Status}**`).
   - No "requires manual review" line.
   - Includes `amount_usd` in the body alongside `amount_token`.
2. **Call it** from `_handle_terminal_payment` at line 267-271:
   ```python
   if payment.get('status') == 'confirmed' and not payment.get('is_test'):
       provider = str(payment.get('provider') or '').lower()
       amount_usd = float(payment.get('amount_usd') or 0)
       if provider in self._admin_success_dm_providers and amount_usd >= self._admin_success_dm_threshold_usd:
           await self._dm_admin_payment_success(payment)
   ```
3. **Add** `self._admin_success_dm_threshold_usd` and `self._admin_success_dm_providers` attributes on `PaymentCog.__init__`, sourced from env defaults or cog constructor kwargs. Default threshold: $100. Default providers: `frozenset({'solana_payouts'})`.
4. **Do NOT** modify `_dm_admin_payment_failure`.

### Step 10: Wire caps + success DM into main (`main.py`)
**Scope:** Small

1. **Modify** `main.py:157-165`. Read env with defaults:
   ```python
   per_payment_usd_cap = float(os.getenv('ADMIN_PAYOUT_PER_PAYMENT_USD_CAP', '500'))
   daily_usd_cap = float(os.getenv('ADMIN_PAYOUT_DAILY_USD_CAP', '2000'))
   admin_success_dm_threshold = float(os.getenv('ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD', '100'))
   ```
2. **Pass** `per_payment_usd_cap`, `daily_usd_cap`, `capped_providers=('solana_payouts',)` into `PaymentService(...)`.
3. **Pass** the cap-breach DM callback plumbing (whatever pattern Step 8 lands on).
4. **Wire** the success DM threshold/providers into `PaymentCog` wherever it is instantiated (grep for `PaymentCog(` to find the single construction site).
5. **Log** the effective caps at startup: `logger.info("PaymentService caps: per_payment=$%.2f daily=$%.2f providers=%s", ...)`. Do NOT log any secrets.

## Phase 4: Observability + Docs (P2)

### Step 11: Log audit (`src/common/log_handler.py`, `src/features/payments/`, `src/features/admin_chat/`)
**Scope:** Small

1. **Grep** for potential secret leakage:
   - `SOLANA_PRIVATE_KEY`, `HELIUS_API_KEY`, `ANTHROPIC_API_KEY` — grep across `src/` and assert they only appear in `os.environ`/`os.getenv` reads, not in `logger.*` args.
   - `Keypair.from_bytes`, `Keypair(`, `SolanaClient(` — ensure no `exc_info=True` paths near key material.
   - `logger\..*os\.environ` — ensure nothing dumps env dicts.
   - `logger\..*private_key|logger\..*secret` — case-insensitive.
2. **Document** findings in the review memo. Add scrubbing filter in `log_handler.py` ONLY if a real leak path is found. Default expectation: no code change, memo only.

### Step 12: RLS posture doc comment (`sql/payments.sql`)
**Scope:** Small

1. **Add** a comment block at the top of `sql/payments.sql` (before `create extension`):
   ```sql
   -- RLS posture:
   --   * Row-level security is ENABLED on wallet_registry, payment_channel_routes,
   --     and payment_requests (see `alter table ... enable row level security` below).
   --   * All read/write access is mediated by the bot's DatabaseHandler using the
   --     Supabase service role, which bypasses RLS. No anon or authenticated-role
   --     clients access these tables directly.
   --   * anon and authenticated roles have ALL privileges revoked; no policies are
   --     defined because no non-service-role access path exists.
   --   * Parameterized writes only; no raw SQL string interpolation from user input.
   --   * Changing this posture (e.g. exposing tables to a browser client) requires
   --     writing explicit row-level policies first — do NOT simply grant.
   ```
2. **Do NOT** `ALTER TABLE` anything. Comment-only change.

## Phase 5: Tests

### Step 13: Extend PaymentService test suite (`tests/test_admin_payments.py`, `tests/conftest.py`)
**Scope:** Medium

1. **Inspect** `tests/conftest.py` (currently untracked per git status) and `tests/test_admin_payments.py:497-729` to understand the existing in-memory db fake pattern. If `conftest.py` already provides in-memory `wallet_registry` + `payment_requests` + `admin_payment_intents` stores, reuse. Otherwise extend with whatever helpers the new tests need.
2. **Add** the following fail-to-pass tests (each independent, each against a real `PaymentService` with a fake db_handler):
   - `test_confirm_rejects_null_recipient_discord_id` — payment row has `recipient_discord_id=None`; `confirm_payment(...)` returns None and logs WARN; with `privileged_override=True` it still advances.
   - `test_confirm_rejects_mismatched_user` — payment has `recipient_discord_id=111`, `confirmed_by_user_id=222` → None.
   - `test_request_payment_per_payment_cap_manual_holds` — construct with `per_payment_usd_cap=100`, `capped_providers=('solana_payouts',)`; a $101 request lands as `manual_hold`; grants provider ignored.
   - `test_request_payment_rolling_daily_cap_manual_holds` — seed fake db with $1900 of rolling 24h solana_payouts at `confirmed` status; new $200 request → manual_hold. Seed a `solana_grants` request of $5000 and verify it does NOT count against the cap.
   - `test_slot_reuse_collision_detected` — seed fake db with a failed prior row for `(producer, producer_ref, is_test)` with `recipient_wallet='ABC'`; new `request_payment` with `recipient_wallet='XYZ'` → new row created with `status='manual_hold'`, `last_error` contains "collision".
   - `test_slot_reuse_same_wallet_creates_fresh_row_after_failure` — seed failed prior row with same wallet; new request creates a fresh `pending_confirmation` row (pins the reordered early-return fix).
   - `test_idempotent_return_for_nonterminal` — seed a `pending_confirmation` row; new `request_payment` returns the existing row unchanged (pins idempotency still works).
   - `test_upsert_wallet_blocked_during_active_intent` — fake db: active `payment_requests` row non-terminal for user; `upsert_wallet` returns error sentinel; `_handle_wallet_received` surfaces error to admin review channel.
   - `test_upsert_wallet_unblocked_after_terminal` — only terminal rows; upsert proceeds.
   - `test_admin_success_dm_over_threshold` — confirmed, non-test, solana_payouts, amount_usd=$150 → `_dm_admin_payment_success` called. Under-threshold → not called. Failure path unchanged.
   - `test_execute_payment_uses_stored_wallet` — seed payment row with frozen `recipient_wallet='STORED'`; `execute_payment` passes `'STORED'` to provider.send even when `wallet_registry` has been changed to `'NEW'`. Pins regression.
   - `test_initiate_payment_tool_rejects_wallet_address_arg` — call `execute_initiate_payment` with `params={'wallet_address': 'EVIL', ...}`; verify the wallet comes from `db_handler.get_wallet(...)` and the params wallet was popped + WARN logged.
3. **Verify** existing happy-path tests still pass with default (None) caps by running `pytest tests/test_admin_payments.py` after each adjacent code change, not just at the end.

### Step 14: Verify scheduler + social publish tests untouched (`tests/test_scheduler.py`, `tests/test_social_publish_service.py`)
**Scope:** Small

1. **Run** `pytest tests/test_scheduler.py tests/test_social_publish_service.py` after all code changes. Caps run only in `request_payment`; these tests should pass without modification because:
   - `test_scheduler.py` exercises `claim_due_payment_requests → execute_payment` which we did NOT touch.
   - `test_social_publish_service.py` constructs `PaymentService` without the new kwargs → they default to None → no cap enforcement.
2. **If either fails**, read the failure and fix the code (not the test) unless the test is making an assumption that violates the new invariants; flag any test changes in the plan output.

### Step 15: Run the full suite (`tests/`)
**Scope:** Small

1. **Run** `pytest tests/` end-to-end. Every test must pass.
2. **If any unrelated test fails**, stop and investigate — do not hand-wave or skip.

## Execution Order

1. Phase 1 (null-recipient fail-closed + prompt-injection verification + git history audit) — low-risk, independently shippable.
2. Phase 2 (upsert_wallet inflight lock + idempotency collision) — the subtle wallet-swap and idempotency fixes.
3. Phase 3 (caps + success DM + main wiring) — monetary safety, depends on Phase 2's understanding of `request_payment`.
4. Phase 4 (log audit memo + RLS comment) — docs/observability.
5. Phase 5 (tests) — run targeted new tests after each phase, then the full suite at the end.

Between phases, run the relevant subset of tests as a cheap trip-wire before moving on:
- After Phase 1: `pytest tests/test_admin_payments.py -k confirm`.
- After Phase 2: `pytest tests/test_admin_payments.py -k "wallet or slot or idempotent"`.
- After Phase 3: `pytest tests/test_admin_payments.py -k "cap or success_dm"` + `pytest tests/test_scheduler.py`.
- After Phase 5: full `pytest tests/`.

## Validation Order

1. **Unit tests** (new + existing) — `pytest tests/test_admin_payments.py`.
2. **Adjacent suites** — `pytest tests/test_scheduler.py tests/test_social_publish_service.py` (regression pins that caps don't leak into scheduler or social flows).
3. **Full suite** — `pytest tests/`.
4. **Manual read-through** — re-audit `payment_service.request_payment` and `admin_chat_cog._handle_wallet_received` against the invariants list above, looking specifically for any path that (a) silently drops a final payout, (b) bypasses cap enforcement for `solana_payouts`, or (c) allows a wallet swap mid-flow.
5. **Git history audit memo** — ensure Step 3's findings are recorded in the review output. If any real secret was found, STOP and raise to the user for rotation before landing.
