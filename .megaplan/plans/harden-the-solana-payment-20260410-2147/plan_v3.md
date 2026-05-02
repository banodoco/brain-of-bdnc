# Implementation Plan: Harden Solana Payment Subsystem

## Overview

Implement the P0/P1/P2 hardening from the adversarial review against `src/features/payments/` and `src/features/admin_chat/`. Layered fail-closed: (1) correctness first (null recipient, prompt-injection defense, inflight wallet lock via exception), (2) monetary safety (idempotency collision with index-aware branching, per-payment + rolling daily USD caps that derive USD from `amount_token` when needed **and persist it onto the stored payment row**), (3) observability (admin success DM, log audit memo, RLS posture comment, git history audit memo).

### Corrections confirmed against the repo

1. **RLS is already enabled** on `wallet_registry`, `payment_channel_routes`, `payment_requests` (`sql/payments.sql:192-199`). Step 12 is comment-only.
2. **P0 prompt-injection is already substantially satisfied**: the `initiate_payment` tool schema at `src/features/admin_chat/tools.py:580-599` omits `wallet_address`, and `execute_initiate_payment` resolves wallets via `db_handler.get_wallet(...)` at `tools.py:2474`. Verification + defensive pop-and-warn + regression test, not a rewrite.
3. **`get_payment_requests_by_producer`** at `db_handler.py:1853` already returns rows across all statuses; collision detection re-uses it directly.
4. **`execute_payment` already uses the stored `recipient_wallet`** (`payment_service.py:244`). Regression-pin test only.
5. **Admin-chat final payout passes `amount_token` only** (`admin_chat_cog.py:352-365`). For capped providers, the derived USD MUST be persisted into `record['amount_usd']` (and `record['token_price_usd']`) so that (a) the rolling-24h aggregation in Step 7 sees it, (b) the success-DM gate in Step 9 sees it, (c) the `last_error` / manual_hold fields are consistent. If the price lookup fails, fail closed to `manual_hold` with `last_error='cap check unavailable: token price missing'`.
6. **`upsert_wallet` has two live callers**: `admin_chat_cog.py:193-201` and `grants_cog.py:557-565`. Blocked-path signalling is a `WalletUpdateBlockedError` exception so neither caller can mistake it for a success.
7. **Partial unique index** `uq_payment_requests_active_producer_ref` at `sql/payments.sql:126-128` is `WHERE status NOT IN ('failed','cancelled')`. Collision handling must branch on whether any prior row is non-excluded before attempting a new insert.
8. **PaymentCog is constructed at two sites**: `main.py:233` and the `setup(bot)` hook at `src/features/payments/payment_cog.py:503`. Success-DM config is read **inside** `PaymentCog.__init__` from env; neither construction site needs to change.
9. **Inline fake-db test glue** lives in `tests/test_admin_payments.py` (classes around lines 497-729) and `tests/test_social_publish_service.py` (lines 474-685) in addition to `tests/conftest.py`. New helpers must be stubbed in all three.
10. **Existing amount_token tests** (`test_request_payment_amount_token` at `tests/test_admin_payments.py:684-709` and `test_social_publish_service.py:481-497`) assert `result['amount_usd'] is None`. Both use **uncapped** provider names (`'solana'`, `'solana_native'`), so stamping `record['amount_usd']` only inside the capped-provider branch leaves them untouched. This is verified by grep prior to implementation.

### Key invariants (non-negotiable)

- **Fail-closed.** Null recipient → reject. Cap breach → `manual_hold` + admin DM. Wallet-swap collision → branch per unique-index rule with admin DM. Inflight wallet change → raise `WalletUpdateBlockedError`.
- **Provider-scoped caps.** Caps and admin success DM apply only to providers in `capped_providers` (default `frozenset({'solana_payouts'})`).
- **Derived USD is persisted end-to-end** (capped-provider amount_token path only). Not a transient local; `record['amount_usd']` and `record['token_price_usd']` are stamped before insert so the rolling helper and success-DM gate see the real value.
- **Do NOT change the partial-index predicate.** Collision handling stays in app code.
- **Do NOT re-read `recipient_wallet` in `execute_payment`.** Frozen-string semantics.
- **Do NOT add cap logic in `execute_payment`.**
- **Do NOT widen `upsert_wallet`'s block window.** Block only when the incoming address differs AND a non-terminal payment/intent exists.
- **Do NOT stamp `amount_usd` for uncapped providers or is_test paths.** That would break existing happy-path tests that assert `amount_usd is None` for token-denominated requests on non-capped providers.
- **Preserve `privileged_override=True`** for any future auto-advance path.
- **No secret logging.** New logs redact wallets via `_redact_wallet`.
- **Cap config is injectable via constructor kwargs** with `None` defaults.

## Phase 1: Fail-Closed Correctness (P0)

### Step 1: Null recipient_discord_id fail-closed (`src/features/payments/payment_service.py`)
**Scope:** Small
1. **Modify** `confirm_payment` at `payment_service.py:193-202`:
   ```python
   if not privileged_override:
       if (
           expected_user_id is None
           or confirmed_by_user_id is None
           or int(expected_user_id) != int(confirmed_by_user_id)
       ):
           self.logger.warning(
               "[PaymentService] rejected confirmation for %s: expected_user=%s confirmed_by=%s",
               payment_id, expected_user_id, confirmed_by_user_id,
           )
           return None
   ```
2. **Audit** every `confirm_payment(` call site. Verify admin-chat test auto-confirm at `admin_chat_cog.py:258-262` and grants auto-confirm at `grants_cog.py:596-601` both pass matching `confirmed_by_user_id` so the tightened rule is satisfied without needing `privileged_override`. Add a one-line comment at each call site.

### Step 2: Prompt-injection verification + defensive assertion (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Grep** `ADMIN_CHAT_TOOLS` schemas for any field named `wallet_address`, `address`, `solana_address`, `recipient_wallet`, `wallet`. Strip + harden if any are found.
2. **Modify** `execute_initiate_payment` at `tools.py:2438`. Add docstring + defensive pop at the top:
   ```python
   if 'wallet_address' in params or 'recipient_wallet' in params:
       logger.warning(
           "[AdminChat] execute_initiate_payment ignoring LLM-sourced wallet fields"
       )
       params.pop('wallet_address', None)
       params.pop('recipient_wallet', None)
   ```
3. **Record in the plan output** that P0 prompt-injection was already substantially satisfied by prior work.

### Step 3: Git history secret audit memo (no code change)
**Scope:** Small
1. **Run** and record:
   - `git log -p -S 'SOLANA_PRIVATE_KEY' -- .`
   - `git log -p -S 'HELIUS_API_KEY' -- .`
   - `git log -p -S 'ANTHROPIC_API_KEY' -- .`
   - `git log -p -S 'from_bytes' -- .`
   - `git log --all --full-history -- '**/*.env*' '**/.env' '**/secrets*'`
   - `git log -p -G 'sk-[a-zA-Z0-9]{20,}' --all`
   - `gitleaks detect --source . --no-banner` (if installed).
2. **Document** findings in a memo block. Any real secret → STOP and flag to the user for rotation.

## Phase 2: Inflight Wallet Lock + Idempotency Collision

### Step 4: Add `has_active_payment_or_intent` helper (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** `DatabaseHandler.has_active_payment_or_intent(self, guild_id: int, discord_user_id: int) -> bool`.
2. **Query 1** — `payment_requests` WHERE `guild_id=?` AND `recipient_discord_id=?` AND `status NOT IN ('confirmed','failed','manual_hold','cancelled')` LIMIT 1.
3. **Query 2** — `admin_payment_intents` WHERE `guild_id=?` AND `recipient_user_id=?` AND `status NOT IN ('completed','failed','cancelled')` LIMIT 1.
4. **Return** True if either query returns a row. Use `_gate_check(guild_id)` + supabase-guard.

### Step 5: Exception-based upsert_wallet blocking (`src/common/db_handler.py`, `src/features/admin_chat/admin_chat_cog.py`, `src/features/grants/grants_cog.py`)
**Scope:** Medium
1. **Add** `class WalletUpdateBlockedError(Exception): pass` to `db_handler.py` (or `src/common/exceptions.py` if one exists — grep first).
2. **Modify** `upsert_wallet` at `db_handler.py:1417-1465`. The existing-wallet lookup at line 1431 runs FIRST, then the block check:
   ```python
   existing = self.get_wallet(guild_id, discord_user_id, chain)
   if existing and existing.get('wallet_address') != address:
       if self.has_active_payment_or_intent(guild_id, discord_user_id):
           logger.warning(
               "[DB] upsert_wallet blocked: active payment/intent for guild %s user %s",
               guild_id, discord_user_id,
           )
           raise WalletUpdateBlockedError(
               "wallet update blocked while an active payment or intent exists"
           )
   ```
3. **Modify** `_handle_wallet_received` at `admin_chat_cog.py:193-218`:
   ```python
   try:
       wallet_record = self.db_handler.upsert_wallet(...)
   except WalletUpdateBlockedError:
       await self._notify_admin_review(
           message.channel, intent,
           "I cannot update this wallet address: an active payment is in flight. "
           "Please wait for it to resolve or cancel the current intent.",
           resolved_by_message_id=message.id,
       )
       return
   ```
4. **Modify** `_start_payment_flow` at `grants_cog.py:550-565`:
   ```python
   try:
       wallet_record = self.db.upsert_wallet(...)
   except WalletUpdateBlockedError:
       await thread.send(
           "I cannot register this wallet right now: there is an active payment "
           "in progress for your account. Please wait for it to finalize and try again."
       )
       return
   ```
5. **Grep** `.upsert_wallet(` to confirm these are the only two callers. If a third exists, add equivalent handling.

### Step 6: Idempotency collision detection with index-aware branching (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Read** `db_handler.py:1853-1883` to confirm `get_payment_requests_by_producer` returns all statuses. Add an inline comment documenting the invariant.
2. **Rework** `request_payment` at `payment_service.py:54-61`:
   ```python
   all_prior_rows = self.db_handler.get_payment_requests_by_producer(
       guild_id=guild_id, producer=producer,
       producer_ref=producer_ref, is_test=is_test,
   )
   normalized_wallet = str(recipient_wallet or '').strip()
   INDEX_EXCLUDED = {'failed', 'cancelled'}
   TERMINAL_FOR_RETURN = {'confirmed', 'failed', 'manual_hold', 'cancelled'}

   collision_row = None
   blocking_prior_row = None
   for row in all_prior_rows:
       prior_wallet = str(row.get('recipient_wallet') or '').strip()
       prior_status = str(row.get('status') or '').lower()
       if prior_status not in INDEX_EXCLUDED:
           blocking_prior_row = row
       if prior_wallet and normalized_wallet and prior_wallet != normalized_wallet:
           collision_row = row
           break

   # Idempotent early-return only for a live, same-wallet prior row. Terminal
   # rows must NOT short-circuit here — otherwise a `failed` prior row is
   # handed back to the caller as if it were fresh `pending_confirmation`.
   if not collision_row:
       active_same_wallet = [
           r for r in all_prior_rows
           if str(r.get('status') or '').lower() not in TERMINAL_FOR_RETURN
           and str(r.get('recipient_wallet') or '').strip() == normalized_wallet
       ]
       if active_same_wallet:
           return active_same_wallet[0]
   ```
3. **Collision handling — two explicit branches** (immediately after the loop):
   - **Branch A — slot reuse after terminal failure** (`blocking_prior_row is None`): every prior row is `failed`/`cancelled`, so the partial unique index permits a new insert. Fall through to build `record` as normal but then set `record['status'] = 'manual_hold'` and `record['last_error'] = 'idempotency collision: prior wallet differs'`. Insert via `create_payment_request`; after insert, fire the `on_cap_breach` callback (Step 8).
   - **Branch B — prior row still covered by the index** (`blocking_prior_row is not None`): do NOT attempt the insert. Log WARN, fire `on_cap_breach` (with the prior row as the payload for operator context), and `return None`. Include an inline comment explaining the invariant.
4. **Fallback path** at `payment_service.py:167-174` (post-insert duplicate re-read): apply the same wallet-mismatch check against the re-read row. On mismatch: log WARN, fire `on_cap_breach`, `return None`. On match: return the canonical row.

## Phase 3: USD Caps + Admin Success DM

### Step 7: Add `get_rolling_24h_payout_usd` helper (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** `get_rolling_24h_payout_usd(self, guild_id: int, provider: str) -> float`. Query `payment_requests`:
   - `guild_id=?`
   - `provider=?`
   - `is_test = false`
   - `status IN ('pending_confirmation','queued','processing','submitted','confirmed')`
   - `created_at >= now() - interval '24 hours'`
2. **Sum** `amount_usd` across results; nulls → 0.0; return float.
3. **Comment**: "Supabase REST has no serializable transaction — residual burst window is ~RPC latency (~100-500ms). Manual_hold + admin DM is the fail-closed backstop. Relies on upstream request_payment persisting derived USD for amount_token-only capped callers."
4. **Stub** in the inline fakes (Step 13). Default return 0.0.

### Step 8: PaymentService cap enforcement with persisted derived USD (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Extend** `PaymentService.__init__`:
   ```python
   per_payment_usd_cap: Optional[float] = None,
   daily_usd_cap: Optional[float] = None,
   capped_providers: Optional[Iterable[str]] = None,
   on_cap_breach: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
   ```
   Normalize; defaults disable cap logic entirely so existing tests stay green.
2. **Inject cap logic in `request_payment`** *after* amount_token derivation (lines 90-119) and *before* `create_payment_request` (line 163). This block covers **both** the `amount_usd` and `amount_token` callers. **Critically, when USD is derived from `amount_token * token_price_usd` for a capped provider, the derived values are persisted into `record['amount_usd']` and `record['token_price_usd']` so downstream observability (rolling cap helper, success-DM gate) sees the real value.**
   ```python
   provider_key = str(provider).strip().lower()
   cap_usd: Optional[float] = None
   derived_usd_for_record: Optional[float] = None
   derived_price_for_record: Optional[float] = None

   if not is_test and provider_key in self.capped_providers:
       if normalized_amount_usd is not None:
           cap_usd = normalized_amount_usd
       else:
           # amount_token-only caller (e.g. admin_chat final payout at
           # admin_chat_cog.py:352-365). Derive USD from the provider token
           # price so the cap check, rolling-24h helper, and success-DM
           # gate all see the same value. Fail-closed to manual_hold if
           # the price lookup fails.
           price = token_price_usd
           if price is None:
               try:
                   price = await payment_provider.get_token_price_usd()
               except Exception as exc:
                   self.logger.warning(
                       "[PaymentService] token price lookup failed for cap check: %s", exc,
                   )
                   price = None
           if price and price > 0:
               cap_usd = float(amount_token) * float(price)
               derived_usd_for_record = cap_usd
               derived_price_for_record = float(price)
           # else: cap_usd stays None → fail-closed below
   ```
3. **Compute `cap_breach`**:
   ```python
   cap_breach: Optional[str] = None
   if not is_test and provider_key in self.capped_providers:
       if cap_usd is None:
           cap_breach = "cap check unavailable: token price missing"
       else:
           if self.per_payment_usd_cap is not None and cap_usd > self.per_payment_usd_cap:
               cap_breach = (
                   f"per-payment cap exceeded: ${cap_usd:.2f} > ${self.per_payment_usd_cap:.2f}"
               )
           elif self.daily_usd_cap is not None:
               rolling = float(
                   self.db_handler.get_rolling_24h_payout_usd(guild_id, provider_key) or 0.0
               )
               if rolling + cap_usd > self.daily_usd_cap:
                   cap_breach = (
                       f"daily cap exceeded: ${rolling:.2f} + ${cap_usd:.2f} > ${self.daily_usd_cap:.2f}"
                   )
   ```
4. **Persist derived USD into `record` BEFORE the insert** so it flows through `create_payment_request` and into `request_payload` (which mirrors the top-level fields at lines 141-160). Do this **only** inside the capped-provider + derived branch — uncapped providers and is_test rows keep `amount_usd=None` as today:
   ```python
   if derived_usd_for_record is not None:
       record['amount_usd'] = derived_usd_for_record
       record['token_price_usd'] = derived_price_for_record
       # Keep request_payload mirror in sync so downstream reporting agrees.
       record['request_payload']['amount_usd'] = derived_usd_for_record
       record['request_payload']['token_price_usd'] = derived_price_for_record
   if cap_breach:
       self.logger.warning(
           "[PaymentService] cap breach for %s:%s — %s", producer, producer_ref, cap_breach,
       )
       record['status'] = 'manual_hold'
       record['last_error'] = cap_breach
   ```
5. **After** `create_payment_request(record, ...)`, if `cap_breach` and `created` and `self._on_cap_breach`, `await self._on_cap_breach(created)`. This also handles Step 6 Branch A (which reuses this same `record`-stamp + post-insert-hook path).
6. **Do NOT** add cap logic in `execute_payment`. Scheduler path untouched.
7. **Verify non-regression of existing amount_token tests** before landing:
   - `tests/test_admin_payments.py:684-709` (`test_request_payment_amount_token`) uses provider `"solana"` (NOT in `capped_providers`), asserts `result['amount_usd'] is None` AND `provider.price_calls == 0`. Because the derivation branch only runs for capped providers, this test stays green and no extra price call happens.
   - `tests/test_social_publish_service.py:474-497` uses provider `"solana_native"` and `is_test=True`; both gates exclude it, so `amount_usd` stays `None`.

### Step 9: Admin success DM over threshold (`src/features/payments/payment_cog.py`)
**Scope:** Small
1. **Extend** `PaymentCog.__init__` to read env **inside the constructor**:
   ```python
   import os
   self._admin_success_dm_threshold_usd = float(
       os.getenv('ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD', '100')
   )
   self._admin_success_dm_providers = frozenset(
       p.strip().lower()
       for p in (os.getenv('ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS', 'solana_payouts').split(','))
       if p.strip()
   )
   ```
2. **Add** sibling `_dm_admin_payment_success(self, payment)` modeled on `_dm_admin_payment_failure` at `payment_cog.py:273-333` but with:
   - Distinct prefix `✅ **Payment Completed**`.
   - No "requires manual review" line.
   - Include `amount_usd` (now populated end-to-end for capped amount_token callers, per Step 8) alongside `amount_token`.
   - Wallet redacted via `_redact_wallet`.
3. **Invoke** from `_handle_terminal_payment` at 267-271, alongside (not replacing) the failure path:
   ```python
   if payment.get('status') == 'confirmed' and not payment.get('is_test'):
       provider = str(payment.get('provider') or '').lower()
       amount_usd = float(payment.get('amount_usd') or 0)
       if (
           provider in self._admin_success_dm_providers
           and amount_usd >= self._admin_success_dm_threshold_usd
       ):
           await self._dm_admin_payment_success(payment)
   ```
4. **Do NOT** modify `_dm_admin_payment_failure`.

### Step 10: Wire caps + cap-breach DM into main (`main.py`)
**Scope:** Small
1. **Modify** `main.py:157-165`:
   ```python
   per_payment_usd_cap = float(os.getenv('ADMIN_PAYOUT_PER_PAYMENT_USD_CAP', '500'))
   daily_usd_cap = float(os.getenv('ADMIN_PAYOUT_DAILY_USD_CAP', '2000'))
   ```
2. **Build** `PaymentService(...)` with new kwargs:
   ```python
   bot.payment_service = PaymentService(
       bot.db_handler, providers, test_payment_amount,
       per_payment_usd_cap=per_payment_usd_cap,
       daily_usd_cap=daily_usd_cap,
       capped_providers=('solana_payouts',),
       on_cap_breach=_bind_cap_breach_dm(bot),
   )
   ```
   where `_bind_cap_breach_dm(bot)` returns a small `async def` closure that looks up `bot.get_cog('PaymentCog')` and calls `_dm_admin_payment_failure(payment)` — the failure DM wording already fits ("requires manual review") since cap-breach and collision-blocked rows both land in the admin manual-review queue.
3. **Log** effective caps at startup:
   ```python
   logger.info(
       "PaymentService caps: per_payment=$%.2f daily=$%.2f providers=%s",
       per_payment_usd_cap, daily_usd_cap, ('solana_payouts',),
   )
   ```
4. **Do not touch** `payment_cog.py:503`.

## Phase 4: Observability + Docs (P2)

### Step 11: Log audit memo (`src/common/log_handler.py`, `src/features/payments/`, `src/features/admin_chat/`)
**Scope:** Small
1. **Grep** for:
   - `SOLANA_PRIVATE_KEY`, `HELIUS_API_KEY`, `ANTHROPIC_API_KEY` — only in `os.environ`/`os.getenv`, never in `logger.*` args.
   - `Keypair.from_bytes`, `Keypair(`, `SolanaClient(` — no `exc_info=True` paths near key material.
   - `logger\..*os\.environ`, `logger\..*private_key`, `logger\..*secret` (case-insensitive).
2. **Document** findings in the review memo. Only add a scrubbing filter in `log_handler.py` if a real leak path is found.

### Step 12: RLS posture doc comment (`sql/payments.sql`)
**Scope:** Small
1. **Add** a comment block at the very top of `sql/payments.sql`:
   ```sql
   -- RLS posture:
   --   * Row-level security is ENABLED on wallet_registry, payment_channel_routes,
   --     and payment_requests (see `alter table ... enable row level security` below).
   --   * All access is mediated by DatabaseHandler using the Supabase service role,
   --     which bypasses RLS. No anon/authenticated clients access these tables.
   --   * anon and authenticated roles have ALL privileges revoked; no policies are
   --     defined because no non-service-role access path exists.
   --   * Writes are parameterized via supabase-py; no raw SQL string interpolation.
   --   * Changing this posture (e.g. exposing tables to a browser client) requires
   --     writing explicit row-level policies first — do NOT simply grant.
   ```
2. **Do NOT** `ALTER TABLE`. Comment-only.

## Phase 5: Tests

### Step 13: Extend PaymentService test suite (`tests/test_admin_payments.py`, `tests/conftest.py`, `tests/test_social_publish_service.py`)
**Scope:** Medium
1. **Inspect** `tests/conftest.py` and the inline fake-db classes in `tests/test_admin_payments.py:497-729` and `tests/test_social_publish_service.py:474-685`. Extend all three additively with:
   - `has_active_payment_or_intent(guild_id, user_id)` method.
   - `get_rolling_24h_payout_usd(guild_id, provider)` method (default 0.0).
   - `upsert_wallet` raising `WalletUpdateBlockedError` when configured.
   - Import `WalletUpdateBlockedError` from wherever Step 5 lands it.
2. **Preserve existing assertions** that key off `amount_usd is None` for uncapped providers. Before writing any new test, re-grep `amount_usd` in both test files and confirm every current assertion uses a non-capped provider name (`"solana"`, `"solana_native"`).
3. **Add** fail-to-pass tests:
   - `test_confirm_rejects_null_recipient_discord_id` — `recipient_discord_id=None`, `privileged_override=False` → None + WARN. `privileged_override=True` still works.
   - `test_confirm_rejects_mismatched_user` — mismatched `confirmed_by_user_id` → None.
   - `test_request_payment_per_payment_cap_manual_holds` — `per_payment_usd_cap=100`, `capped_providers=('solana_payouts',)`, $101 → `manual_hold` + `on_cap_breach` invoked; grants provider unaffected.
   - `test_request_payment_amount_token_path_cap_breach` — capped provider, `amount_token=5.0`, provider price $220, cap $1000 → derived USD $1100 → `manual_hold`. Pins admin-chat final-payout path.
   - `test_request_payment_amount_token_path_stamps_amount_usd` — **new**: capped provider, `amount_token=2.0`, provider price $150, cap $1000 → row is created (NOT manual_hold) AND `created['amount_usd'] == 300.0` AND `created['token_price_usd'] == 150.0`. Pins that derived USD is persisted so the rolling helper and success-DM gate see it.
   - `test_request_payment_amount_token_path_missing_price_holds` — capped provider, price returns None/0 → `manual_hold` with `last_error` containing "cap check unavailable".
   - `test_request_payment_amount_token_uncapped_provider_preserves_none` — **new**: uncapped provider (`"solana"`), `amount_token=1.5` → `result['amount_usd'] is None` AND provider price lookup NOT invoked. Regression pin against the existing `test_request_payment_amount_token` behavior.
   - `test_request_payment_rolling_daily_cap_manual_holds` — seed $1900 rolling 24h solana_payouts confirmed; new $200 → manual_hold. Solana_grants $5000 seeded → does NOT count.
   - `test_request_payment_rolling_daily_cap_sees_derived_usd` — **new**: seed fake with a prior confirmed capped row created via the amount_token path (so its stored `amount_usd` came from the Step 8 stamp); verify `get_rolling_24h_payout_usd` returns the stamped total and a follow-up amount_token request accounts for it against the daily cap. Pins the end-to-end flow the critique flagged.
   - `test_slot_reuse_collision_detected_after_failure` — Branch A: only failed prior rows with different wallet → fresh `manual_hold` row inserted + on_cap_breach fires.
   - `test_slot_reuse_collision_blocked_when_prior_active` — Branch B: `pending_confirmation` prior row with different wallet → returns None without attempting insert; prior row untouched; on_cap_breach fires.
   - `test_slot_reuse_same_wallet_creates_fresh_row_after_failure` — only terminal prior rows with matching wallet → fresh `pending_confirmation` row.
   - `test_idempotent_return_for_nonterminal` — non-terminal prior row, matching wallet → returned unchanged.
   - `test_upsert_wallet_raises_during_active_intent` — active payment row → `WalletUpdateBlockedError`. Follow-ups: `_handle_wallet_received` catches + calls `_notify_admin_review`; `_start_payment_flow` (grants) catches + sends thread fallback.
   - `test_upsert_wallet_unblocked_after_terminal` — only terminal prior rows → upsert proceeds. Identical-address upsert while active intent exists → upsert proceeds (no block).
   - `test_admin_success_dm_over_threshold` — confirmed, non-test, solana_payouts, `amount_usd=150` → `_dm_admin_payment_success` called. Under threshold → not called. Failure path unchanged.
   - `test_admin_success_dm_sees_derived_usd` — **new**: seed a confirmed payment whose `amount_usd` was stamped via the Step 8 derivation path (amount_token-only capped caller). Passing it through `_handle_terminal_payment` triggers the success DM because the gate reads `payment.get('amount_usd')`. Pins the end-to-end wiring.
   - `test_execute_payment_uses_stored_wallet` — regression pin.
   - `test_initiate_payment_tool_rejects_wallet_address_arg` — evil `wallet_address` in params popped + WARN; wallet resolves via `db_handler.get_wallet`.
4. **Verify** by running `pytest tests/test_admin_payments.py` after each phase.

### Step 14: Verify scheduler + social publish tests untouched
**Scope:** Small
1. **Run** `pytest tests/test_scheduler.py tests/test_social_publish_service.py`. These should pass unchanged because:
   - Scheduler path (`claim_due_payment_requests → execute_payment`) is untouched.
   - `test_social_publish_service.py` constructs `PaymentService` without the new kwargs → caps default to None → no enforcement → no `amount_usd` stamping for its uncapped `"solana_native"` provider, preserving its `amount_usd is None` assertion.
2. **If either fails**, fix the code (not the test) unless the test violates a new invariant.

### Step 15: Run the full suite
**Scope:** Small
1. **Run** `pytest tests/` end-to-end.
2. Investigate any unrelated failures.

## Execution Order
1. Phase 1 (null-recipient + prompt-injection verification + git-history audit memo).
2. Phase 2 (has_active_payment_or_intent → `WalletUpdateBlockedError` → admin_chat + grants caller handling → collision detection with index-aware branching).
3. Phase 3 (rolling 24h helper → cap enforcement with persisted derived USD → success DM sibling → main.py wiring).
4. Phase 4 (log audit memo + RLS comment).
5. Phase 5 (targeted tests after each phase, full suite at the end).

Trip-wire subsets between phases:
- After Phase 1: `pytest tests/test_admin_payments.py -k "confirm or initiate_payment"`.
- After Phase 2: `pytest tests/test_admin_payments.py -k "wallet or slot or collision or idempotent"`.
- After Phase 3: `pytest tests/test_admin_payments.py -k "cap or success_dm or token_path or stamps or derived"` + `pytest tests/test_scheduler.py tests/test_social_publish_service.py`.
- After Phase 5: full `pytest tests/`.

## Validation Order
1. **Unit tests (new + existing)** — `pytest tests/test_admin_payments.py`.
2. **Adjacent suites** — `pytest tests/test_scheduler.py tests/test_social_publish_service.py` (regression pins that caps don't leak into scheduler or social flows, and that uncapped providers still see `amount_usd is None`).
3. **Full suite** — `pytest tests/`.
4. **Manual read-through** — re-audit `payment_service.request_payment` and both `upsert_wallet` callers against the invariants list. Specifically: (a) does any path silently drop a final payout, (b) does an amount_token-only capped caller flow its derived USD all the way into the stored row AND the rolling helper AND the success-DM gate, (c) does any wallet swap mid-flow sneak through, (d) does any code attempt a new `manual_hold` insert while a non-terminal prior row still holds the unique-index slot.
5. **Git-history + log audit memos** recorded in the review output.
