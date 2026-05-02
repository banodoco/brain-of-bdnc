# Implementation Plan: Harden Solana Payment Subsystem

## Overview

Implement the P0/P1/P2 hardening from the adversarial review against `src/features/payments/` and `src/features/admin_chat/`. Layered fail-closed: (1) correctness first (null recipient, prompt-injection defense, inflight wallet lock via exception), (2) monetary safety (idempotency collision with index-aware branching, per-payment + rolling daily USD caps that derive USD from `amount_token` when needed), (3) observability (admin success DM, log audit memo, RLS posture comment, git history audit memo).

### Corrections confirmed against the repo

1. **RLS is already enabled** on `wallet_registry`, `payment_channel_routes`, `payment_requests` (`sql/payments.sql:192-199`). Step 12 is comment-only.
2. **P0 prompt-injection is already substantially satisfied**: the `initiate_payment` tool schema at `src/features/admin_chat/tools.py:580-599` omits `wallet_address`, and `execute_initiate_payment` resolves wallets via `db_handler.get_wallet(...)` at `tools.py:2474`. The work here is verification + defensive pop-and-warn + regression test, NOT a rewrite.
3. **`get_payment_requests_by_producer`** at `db_handler.py:1853` already returns rows across all statuses (no terminal filter), so collision detection re-uses it. The bug in `request_payment` at `payment_service.py:54-61` is that `existing_rows[0]` is returned unconditionally — if the first prior row is `failed`, the caller sees a failed row as if it were fresh.
4. **`execute_payment` already uses the stored `recipient_wallet`** (`payment_service.py:244`). No code change — regression pin only.
5. **Admin-chat final payout passes `amount_token` only**, not `amount_usd` (`admin_chat_cog.py:352-365`). Cap logic must derive a USD equivalent from the provider token price to cover this path; fail-closed to `manual_hold` if price is unavailable.
6. **`upsert_wallet` has two live callers**: `admin_chat_cog.py:193-201` and `grants_cog.py:557-565`. Both check `if not wallet_record`. Blocked-path signalling must be an **exception** (`WalletUpdateBlockedError`) so a truthy sentinel cannot be mistaken for a successful record by either caller.
7. **Partial unique index** `uq_payment_requests_active_producer_ref` at `sql/payments.sql:126-128` is `WHERE status NOT IN ('failed','cancelled')`. So `manual_hold`, `pending_confirmation`, `queued`, `processing`, `submitted`, `confirmed` all participate in uniqueness. A new `manual_hold` insert is only safe when every prior row is either `failed` or `cancelled`. Collision handling must branch on that.
8. **PaymentCog is constructed at two sites**: `main.py:233` and the `setup(bot)` extension hook at `src/features/payments/payment_cog.py:503`. To avoid drift, the success-DM threshold/providers are read **inside** `PaymentCog.__init__` from env (with defaults). Neither construction site needs to change.
9. **Inline fake-db test glue** lives in `tests/test_admin_payments.py` and `tests/test_social_publish_service.py` (not only `tests/conftest.py`). Any new `DatabaseHandler` helper the tests depend on must be stubbed in the same module's fake.

### Key invariants (non-negotiable)

- **Fail-closed.** Null recipient → reject. Cap breach → `manual_hold` + admin DM. Wallet-swap collision → branch per unique-index rule (see Step 6) with admin DM. Inflight wallet change → raise `WalletUpdateBlockedError`. Never silent.
- **Provider-scoped caps.** Caps and admin success DM apply only to providers in `capped_providers` (default `frozenset({'solana_payouts'})`). `solana_grants` is untouched.
- **Do NOT change the partial-index predicate.** Collision handling stays in app code and respects the existing predicate.
- **Do NOT re-read `recipient_wallet` in `execute_payment`.** Frozen-string semantics at `payment_service.py:244`. Pin with a regression test.
- **Do NOT add cap logic in `execute_payment`.** Caps run only in `request_payment`.
- **Do NOT widen `upsert_wallet`'s block window.** Block only when the incoming address differs AND a non-terminal `payment_requests` row OR a non-terminal `admin_payment_intents` row exists for `(guild_id, discord_user_id)`. Identical-address upserts and upserts with only terminal history remain unblocked.
- **Preserve `privileged_override=True`** for any future auto-advance path. Current admin-chat test auto-confirm passes `confirmed_by_user_id=int(intent['recipient_user_id'])` and the test row carries matching `recipient_discord_id`, so the tightened check passes without needing override.
- **No secret logging.** New logs redact wallets via existing `_redact_wallet` helpers; never log env or key material.
- **Cap config is injectable via constructor kwargs.** Defaults are `None` (disabled) so existing tests construct PaymentService unchanged.

## Phase 1: Fail-Closed Correctness (P0)

### Step 1: Null recipient_discord_id fail-closed (`src/features/payments/payment_service.py`)
**Scope:** Small
1. **Modify** `confirm_payment` at `payment_service.py:193-202`. Replace the current `if not privileged_override and expected_user_id is not None:` with:
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
2. **Audit** every `confirm_payment` call site via `Grep` for `confirm_payment(`. Verify button confirmations always carry a user, grants auto-confirm at `grants_cog.py:596-601` passes `confirmed_by_user_id=grant['applicant_id']`, and the admin-chat test auto-confirm at `admin_chat_cog.py:258-262` passes `confirmed_by_user_id=int(intent['recipient_user_id'])` (matching the row's `recipient_discord_id` at line 244). Add a 1-line comment at each call site explaining why the tightened rule still permits the call.

### Step 2: Prompt-injection verification + defensive assertion (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Grep** `src/features/admin_chat/tools.py` for every tool schema in `ADMIN_CHAT_TOOLS`. Confirm none contain `wallet_address`, `address`, `solana_address`, `recipient_wallet`, `wallet`. If any are found, strip them from both the schema and the corresponding execute function.
2. **Modify** `execute_initiate_payment` at `tools.py:2438`. Add a short docstring and a defensive pop at the top of the function body (after arg-coercion):
   ```python
   # Wallets are resolved exclusively via db_handler.get_wallet(...). The LLM
   # MUST NOT supply wallet strings; reject silently with a WARN log if it does.
   if 'wallet_address' in params or 'recipient_wallet' in params:
       logger.warning(
           "[AdminChat] execute_initiate_payment ignoring LLM-sourced wallet fields"
       )
       params.pop('wallet_address', None)
       params.pop('recipient_wallet', None)
   ```
3. **Record in the plan output** that P0 prompt-injection was already substantially satisfied by prior work; this step is verification + defensive assertion + regression test.

### Step 3: Git history secret audit memo (no code change)
**Scope:** Small
1. **Run** and record output in the review memo:
   - `git log -p -S 'SOLANA_PRIVATE_KEY' -- .`
   - `git log -p -S 'HELIUS_API_KEY' -- .`
   - `git log -p -S 'ANTHROPIC_API_KEY' -- .`
   - `git log -p -S 'from_bytes' -- .`
   - `git log --all --full-history -- '**/*.env*' '**/.env' '**/secrets*'`
   - `git log -p -G 'sk-[a-zA-Z0-9]{20,}' --all`
   - `gitleaks detect --source . --no-banner` (if installed).
2. **Document** findings (even if empty) in a memo block at the top of the review output. If any real secret is found, STOP and flag to the user for rotation.

## Phase 2: Inflight Wallet Lock + Idempotency Collision

### Step 4: Add `has_active_payment_or_intent` helper (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** a new `DatabaseHandler.has_active_payment_or_intent(self, guild_id: int, discord_user_id: int) -> bool` alongside `list_active_intents`.
2. **Query 1** — `payment_requests` WHERE `guild_id=?` AND `recipient_discord_id=?` AND `status NOT IN ('confirmed','failed','manual_hold','cancelled')` LIMIT 1.
3. **Query 2** — `admin_payment_intents` WHERE `guild_id=?` AND `recipient_user_id=?` AND `status NOT IN ('completed','failed','cancelled')` LIMIT 1. (Matches `get_active_intent_for_recipient` at `db_handler.py:1754` and `list_active_intents` at `db_handler.py:1782`.)
4. **Return** True if either query returns a row. Use `_gate_check(guild_id)` and the supabase-guard pattern for consistency with neighbors.

### Step 5: Exception-based upsert_wallet blocking (`src/common/db_handler.py`, `src/features/admin_chat/admin_chat_cog.py`, `src/features/grants/grants_cog.py`)
**Scope:** Medium
1. **Add** a new exception class at the top of `db_handler.py` (or in a small `exceptions.py` neighbour if preferred): `class WalletUpdateBlockedError(Exception): pass`. Export from `db_handler` so callers can import it.
2. **Modify** `upsert_wallet` at `db_handler.py:1417-1465`. **Order matters** — the existing-wallet lookup at line 1431 must run FIRST, then the block check, then the update. Insert the guard **after** `existing = self.get_wallet(...)`:
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
   Identical-address upserts (and upserts with no prior row) are unaffected.
3. **Modify** `_handle_wallet_received` at `admin_chat_cog.py:193-218`. Wrap the `upsert_wallet` call in `try/except WalletUpdateBlockedError` and surface the error:
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
   if not wallet_record:
       # existing general-failure branch unchanged
       ...
   ```
4. **Modify** `_start_payment_flow` at `grants_cog.py:550-565`. Wrap the `upsert_wallet` call in the same `try/except WalletUpdateBlockedError`. Surface a user-facing message to the grant thread and early-return (do NOT continue to `request_payment` with a missing wallet):
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
5. **Grep** `self.db.upsert_wallet(` and `db_handler.upsert_wallet(` and `.upsert_wallet(` to confirm those are the only two callers. If a third caller is discovered, add equivalent handling there.

### Step 6: Idempotency collision detection with index-aware branching (`src/features/payments/payment_service.py`)
**Scope:** Medium
1. **Read** `db_handler.py:1853-1883` to confirm `get_payment_requests_by_producer` returns all statuses with no terminal filter. If confirmed, reuse it directly — no new helper needed. Add an inline comment in `payment_service.py` clarifying the invariant so future refactors do not add a filter.
2. **Rework** `request_payment` at `payment_service.py:54-61`. Replace the unconditional `return existing_rows[0]` with collision-first, index-aware branching:
   ```python
   all_prior_rows = self.db_handler.get_payment_requests_by_producer(
       guild_id=guild_id, producer=producer,
       producer_ref=producer_ref, is_test=is_test,
   )
   normalized_wallet = str(recipient_wallet or '').strip()
   INDEX_EXCLUDED = {'failed', 'cancelled'}
   # Terminal for the idempotency-return check — rows we should NOT reuse as a
   # fresh pending row (caller would treat a failed row as a live request).
   TERMINAL_FOR_RETURN = {'confirmed', 'failed', 'manual_hold', 'cancelled'}

   collision_row = None
   blocking_prior_row = None  # prior row still covered by the unique index
   for row in all_prior_rows:
       prior_wallet = str(row.get('recipient_wallet') or '').strip()
       prior_status = str(row.get('status') or '').lower()
       if prior_status not in INDEX_EXCLUDED:
           blocking_prior_row = row  # any non-excluded row blocks a fresh insert
       if prior_wallet and normalized_wallet and prior_wallet != normalized_wallet:
           collision_row = row  # remember the first wallet mismatch we saw
           break

   # Legitimate idempotent early-return: only when a non-terminal prior row
   # with the SAME wallet exists. Terminal rows must NOT short-circuit here —
   # otherwise a failed prior row is surfaced to the caller as if fresh.
   if not collision_row:
       active_same_wallet = [
           r for r in all_prior_rows
           if str(r.get('status') or '').lower() not in TERMINAL_FOR_RETURN
           and str(r.get('recipient_wallet') or '').strip() == normalized_wallet
       ]
       if active_same_wallet:
           return active_same_wallet[0]
   ```
3. **Handle collision — two explicit branches** (immediately after the loop):
   - **Branch A — slot reuse after terminal failure** (`blocking_prior_row is None`, i.e. every prior row is in `failed`/`cancelled`): the partial unique index permits a new insert. Proceed to build `record` as normal with `status='manual_hold'`, `last_error='idempotency collision: prior wallet differs'`. Fire admin DM via the cap-breach callback (Step 8). Insert via `create_payment_request`.
   - **Branch B — prior row still covered by the index** (`blocking_prior_row is not None`): a new insert would violate the unique index. Do NOT attempt the insert. Log WARN, DM admin via the cap-breach callback, and `return None`. The caller (admin_chat / grants) already handles `None` as "final payout could not be created" and surfaces to the admin channel. Include a comment explaining that this path intentionally does NOT try to insert — the existing non-terminal row represents a legitimate in-flight payment whose wallet cannot be overwritten mid-flow.
4. **Fallback path** at `payment_service.py:167-174` (post-insert duplicate re-read for concurrent races): after the re-read, apply the same collision check against the re-read row. If the re-read row has a different wallet, log WARN, fire the cap-breach DM, and `return None`. If the wallet matches, return the canonical row.
5. **Inline comment** at the reorder explaining the invariant: "Terminal rows must not short-circuit idempotent return — otherwise a `failed` prior row is handed back to the caller as if fresh. Caller expects a live `pending_confirmation` row or `None`."

## Phase 3: USD Caps + Admin Success DM

### Step 7: Add `get_rolling_24h_payout_usd` helper (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** `get_rolling_24h_payout_usd(self, guild_id: int, provider: str) -> float` on `DatabaseHandler`. Query `payment_requests`:
   - `guild_id=?`
   - `provider=?`
   - `is_test = false`
   - `status IN ('pending_confirmation','queued','processing','submitted','confirmed')`
   - `created_at >= now() - interval '24 hours'`
2. **Sum** `amount_usd` across results; coerce nulls to 0.0; return float.
3. **Comment**: "Supabase REST has no serializable transaction — this aggregation races concurrent burst calls. Residual window is ~RPC latency (~100-500ms). Downstream `manual_hold` + admin DM is the fail-closed backstop."
4. **Stub** this helper in the inline fakes used by `tests/test_admin_payments.py` and `tests/test_social_publish_service.py` — return 0.0 by default so existing tests (which pass no caps) are unaffected.

### Step 8: PaymentService cap enforcement covering amount_usd AND amount_token (`src/features/payments/payment_service.py`, `main.py`)
**Scope:** Medium
1. **Extend** `PaymentService.__init__` with:
   ```python
   per_payment_usd_cap: Optional[float] = None,
   daily_usd_cap: Optional[float] = None,
   capped_providers: Optional[Iterable[str]] = None,
   on_cap_breach: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
   ```
   Normalize: `self.per_payment_usd_cap = float(...) if provided else None`, `self.capped_providers = frozenset((p or '').lower() for p in (capped_providers or ()))`, `self._on_cap_breach = on_cap_breach`. Defaults are all `None`/empty so existing test constructions at `tests/test_admin_payments.py:687+` and `tests/test_social_publish_service.py:474-685` remain compatible.
2. **Inject cap logic in `request_payment`** *after* amount_token derivation (lines 90-119) and *before* `create_payment_request` (line 163). This runs for **both** the `amount_usd` and `amount_token` callers:
   ```python
   cap_usd: Optional[float] = None
   if not is_test and provider.strip().lower() in self.capped_providers:
       if normalized_amount_usd is not None:
           cap_usd = normalized_amount_usd
       else:
           # amount_token-only caller (e.g. admin_chat final payout). Derive
           # USD from the provider token price for cap-check purposes. If
           # price is unavailable, fail closed to manual_hold.
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
           else:
               cap_usd = None  # sentinel: price unavailable → fail closed below
   ```
3. **Compute `cap_breach`** and stamp `record` before the insert:
   ```python
   cap_breach: Optional[str] = None
   if not is_test and provider.strip().lower() in self.capped_providers:
       if cap_usd is None:
           cap_breach = "cap check unavailable: token price missing"
       else:
           if self.per_payment_usd_cap is not None and cap_usd > self.per_payment_usd_cap:
               cap_breach = (
                   f"per-payment cap exceeded: ${cap_usd:.2f} > ${self.per_payment_usd_cap:.2f}"
               )
           elif self.daily_usd_cap is not None:
               rolling = float(
                   self.db_handler.get_rolling_24h_payout_usd(guild_id, provider.strip().lower()) or 0.0
               )
               if rolling + cap_usd > self.daily_usd_cap:
                   cap_breach = (
                       f"daily cap exceeded: ${rolling:.2f} + ${cap_usd:.2f} > ${self.daily_usd_cap:.2f}"
                   )
   if cap_breach:
       self.logger.warning(
           "[PaymentService] cap breach for %s:%s — %s", producer, producer_ref, cap_breach,
       )
       record['status'] = 'manual_hold'
       record['last_error'] = cap_breach
   ```
4. **After** `create_payment_request(record, ...)`, if `cap_breach` and `created` and `self._on_cap_breach`, `await self._on_cap_breach(created)`. This fires the admin DM for cap-breach AND for the collision Branch-A case (Step 6) — the collision path stamps the same fields and then re-uses this post-insert hook.
5. **Do NOT** add cap logic in `execute_payment`. Verify by running `pytest tests/test_scheduler.py` after the change (scheduler path unchanged).

### Step 9: Admin success DM over threshold (`src/features/payments/payment_cog.py`)
**Scope:** Small
1. **Extend** `PaymentCog.__init__` to read env with defaults **inside the constructor** (so both construction sites at `main.py:233` and the `setup(bot)` hook at `payment_cog.py:503` pick it up without changes):
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
2. **Add** a sibling method `_dm_admin_payment_success(self, payment)` modeled on `_dm_admin_payment_failure` at `payment_cog.py:273-333`, but with:
   - Distinct prefix `✅ **Payment Completed**` (failure uses `🚨 **Payment {Status}**`).
   - No "requires manual review" line.
   - Include `amount_usd` alongside `amount_token`.
   - Wallet redacted via existing `_redact_wallet` helpers.
3. **Invoke** from `_handle_terminal_payment` at lines 267-271, alongside (not replacing) the failure path:
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
1. **Modify** `main.py:157-165`. Read env with defaults:
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
   where `_bind_cap_breach_dm(bot)` returns a small `async def` closure that looks up `bot.get_cog('PaymentCog')` and calls `_dm_admin_payment_failure(payment)` (or a new `_dm_admin_payment_cap_breach` sibling if the failure DM's wording does not fit — decide during implementation after reading `_dm_admin_payment_failure`'s body).
3. **Log** effective caps at startup:
   ```python
   logger.info(
       "PaymentService caps: per_payment=$%.2f daily=$%.2f providers=%s",
       per_payment_usd_cap, daily_usd_cap, ('solana_payouts',),
   )
   ```
4. **Do not touch** `payment_cog.py:503` (the `setup(bot)` hook); the constructor reads env directly so it stays in sync with `main.py:233`.

## Phase 4: Observability + Docs (P2)

### Step 11: Log audit memo (`src/common/log_handler.py`, `src/features/payments/`, `src/features/admin_chat/`)
**Scope:** Small
1. **Grep** across `src/` for:
   - `SOLANA_PRIVATE_KEY`, `HELIUS_API_KEY`, `ANTHROPIC_API_KEY` — assert they only appear in `os.environ`/`os.getenv`, never in `logger.*` args.
   - `Keypair.from_bytes`, `Keypair(`, `SolanaClient(` — ensure no `exc_info=True` sites that would serialize key material into a traceback.
   - `logger\..*os\.environ`, `logger\..*private_key`, `logger\..*secret` — case-insensitive.
2. **Document** findings in the review memo. Default expectation: no code change, memo only. Only add a scrubbing filter in `log_handler.py` if a real leak path is found — and flag it explicitly.

### Step 12: RLS posture doc comment (`sql/payments.sql`)
**Scope:** Small
1. **Add** a comment block at the very top of `sql/payments.sql` (before `create extension`):
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
1. **Inspect** the currently-untracked `tests/conftest.py` and the inline fake-db classes in `tests/test_admin_payments.py:497-729` and `tests/test_social_publish_service.py:474-685`. Whichever fake is used, extend it with:
   - A `has_active_payment_or_intent(guild_id, user_id)` method.
   - A `get_rolling_24h_payout_usd(guild_id, provider)` method (returns `0.0` by default).
   - A `raise WalletUpdateBlockedError` path on `upsert_wallet` when configured to simulate an active intent.
   - Import `WalletUpdateBlockedError` from wherever Step 5 lands it.
   Keep the changes additive — existing happy-path tests must keep passing with the new defaults.
2. **Add** fail-to-pass tests (each against a real `PaymentService` wired to the fake `DatabaseHandler`):
   - `test_confirm_rejects_null_recipient_discord_id` — payment with `recipient_discord_id=None`, `privileged_override=False` → returns None + WARN. Passes with `privileged_override=True`.
   - `test_confirm_rejects_mismatched_user` — `recipient_discord_id=111`, `confirmed_by_user_id=222` → None.
   - `test_request_payment_per_payment_cap_manual_holds` — `per_payment_usd_cap=100`, `capped_providers=('solana_payouts',)`; $101 → `manual_hold` with `last_error` containing "per-payment cap"; grants provider unaffected; on_cap_breach callback invoked.
   - `test_request_payment_amount_token_path_cap_breach` — **new**: caller passes `amount_token=5.0` with no `amount_usd`, provider token price is $220 (via provider mock), `per_payment_usd_cap=1000` → derived cap USD = $1100 → `manual_hold`. Pins the admin-chat path.
   - `test_request_payment_amount_token_path_missing_price_holds` — **new**: `amount_token` path, provider returns `None`/`0` for price, cap enabled → `manual_hold` with "cap check unavailable". Fail-closed pin.
   - `test_request_payment_rolling_daily_cap_manual_holds` — seed fake with $1900 rolling 24h of solana_payouts at `confirmed`; new $200 → manual_hold. Solana_grants $5000 seeded → does NOT count.
   - `test_slot_reuse_collision_detected_after_failure` — prior row `status='failed'` with different wallet; new request inserts a fresh `manual_hold` row (Branch A) + on_cap_breach fires.
   - `test_slot_reuse_collision_blocked_when_prior_active` — **new**: prior row `status='pending_confirmation'` with different wallet; new request returns `None` + on_cap_breach fires; the prior row is NOT replaced (pins Branch B — no insert attempt under live unique index).
   - `test_slot_reuse_same_wallet_creates_fresh_row_after_failure` — only terminal prior rows with matching wallet; new request creates a fresh `pending_confirmation` row (pins reordered early-return).
   - `test_idempotent_return_for_nonterminal` — non-terminal prior row with matching wallet → returned unchanged.
   - `test_upsert_wallet_raises_during_active_intent` — fake db has an active `payment_requests` row for the user; `upsert_wallet` raises `WalletUpdateBlockedError`; a follow-up test confirms `_handle_wallet_received` catches it and calls `_notify_admin_review`. A second follow-up confirms `_start_payment_flow` in grants_cog catches it and sends the thread fallback message.
   - `test_upsert_wallet_unblocked_after_terminal` — only terminal prior rows → upsert proceeds normally.
   - `test_admin_success_dm_over_threshold` — confirmed, non-test, `solana_payouts`, `amount_usd=150` → `_dm_admin_payment_success` called. Under threshold → not called. Failure path unchanged.
   - `test_execute_payment_uses_stored_wallet` — seed payment row with frozen `recipient_wallet='STORED'`; mutate `wallet_registry` to `'NEW'`; `execute_payment` still passes `'STORED'` to `provider.send`.
   - `test_initiate_payment_tool_rejects_wallet_address_arg` — call `execute_initiate_payment` with `params={'wallet_address': 'EVIL', ...}`; assert wallet resolves via `db_handler.get_wallet` and the evil string never reaches `PaymentService.request_payment`.
3. **Verify** existing happy-path tests still pass with default (None) caps by running `pytest tests/test_admin_payments.py` after each adjacent change, not only at the end.

### Step 14: Verify scheduler + social publish tests untouched
**Scope:** Small
1. **Run** `pytest tests/test_scheduler.py tests/test_social_publish_service.py` after all code changes. These should pass unchanged because:
   - `test_scheduler.py` exercises `claim_due_payment_requests → execute_payment` (untouched).
   - `test_social_publish_service.py` constructs `PaymentService` without the new kwargs → caps default to `None` → no enforcement.
2. **If either fails**, fix the code (not the test) unless the test violates a new invariant; flag any test changes in the plan output.

### Step 15: Run the full suite
**Scope:** Small
1. **Run** `pytest tests/` end-to-end.
2. **Stop and investigate** any unrelated failures — no skips.

## Execution Order
1. Phase 1 (null-recipient + prompt-injection verification + git-history audit memo).
2. Phase 2 (has_active_payment_or_intent helper → `WalletUpdateBlockedError` → admin_chat + grants caller handling → collision detection with index-aware branching).
3. Phase 3 (rolling 24h helper → cap enforcement covering both `amount_usd` and `amount_token` paths → success DM sibling → main.py wiring).
4. Phase 4 (log audit memo + RLS comment).
5. Phase 5 (targeted tests after each phase, full suite at the end).

Between phases, run the cheap trip-wire subsets:
- After Phase 1: `pytest tests/test_admin_payments.py -k "confirm or initiate_payment"`.
- After Phase 2: `pytest tests/test_admin_payments.py -k "wallet or slot or collision or idempotent"`.
- After Phase 3: `pytest tests/test_admin_payments.py -k "cap or success_dm or token_path"` + `pytest tests/test_scheduler.py`.
- After Phase 5: full `pytest tests/`.

## Validation Order
1. **Unit tests (new + existing)** — `pytest tests/test_admin_payments.py`.
2. **Adjacent suites** — `pytest tests/test_scheduler.py tests/test_social_publish_service.py` (regression pins that caps don't leak into scheduler or social flows).
3. **Full suite** — `pytest tests/`.
4. **Manual read-through** — re-audit `payment_service.request_payment` and `admin_chat_cog._handle_wallet_received` and `grants_cog._start_payment_flow` against the invariants list, looking specifically for any path that (a) silently drops a final payout, (b) lets an `amount_token`-only caller bypass cap enforcement, (c) allows a wallet swap mid-flow, or (d) attempts a new `manual_hold` insert while a non-terminal prior row still holds the unique-index slot.
5. **Git-history + log audit memos** recorded in the review output. If any real secret leak is found, STOP and escalate.
