# Implementation Plan: Payment Subsystem Follow-up Cleanups

## Overview
Behavior-preserving refactor of the Solana payout pipeline to unblock the in-flight admin-DM approval feature. Five changes: centralize confirmation authorization in `PaymentService`, introduce a declarative `PRODUCER_FLOWS` registry, preserve `tx_signature` history across requeues, split `PaymentCog` into worker and UI halves, and add a shared `safe_delete_messages` primitive. Providers (`solana_client.py`, `solana_provider.py`) are out of scope. Hot spots: `PaymentService.confirm_payment` at `src/features/payments/payment_service.py:302`, ad-hoc permission checks in `src/features/payments/payment_cog.py:57` and `src/features/admin_chat/admin_chat_cog.py:284`, `db_handler.requeue_payment` blanking `tx_signature` at `src/common/db_handler.py:2153`, a 576-line `PaymentCog` registering persistent views at `src/features/payments/payment_cog.py:200`, and the cap-breach callback hard-wired to `get_cog('PaymentCog')` at `main.py:65-77`.

## Hardening invariants to preserve (added after the hardening megaplan landed)
The hardening megaplan (`harden-the-solana-payment-20260410-2147`) finished execute before this cleanup runs. Its additions must be preserved. **The executor must not touch or regress any of the following:**

1. **Cap enforcement in `PaymentService.request_payment`** (`payment_service.py:~38-51, 117, 173-273, 545-546`). `__init__` params `per_payment_usd_cap`, `daily_usd_cap`, `capped_providers`, `on_cap_breach`; cap-breach writes `status='manual_hold'` with `last_error`, then `_emit_cap_breach`. **This cleanup rewrites `confirm_payment`, not `request_payment` — do not touch cap logic.**
2. **Idempotency collision / canonical-row wallet-swap defense in `request_payment`** (`payment_service.py:~283-300`). The `canonical_row` / `canonical_wallet` detection path. **Do not refactor.**
3. **Null-recipient fail-closed in `confirm_payment`** (`payment_service.py:~319-328`). Current line 321 rejects when `expected_user_id is None or confirmed_by_user_id is None or int(expected_user_id) != int(confirmed_by_user_id)`. **The new `_authorize_actor` must preserve this null-rejection for all recipient-kind actors.**
4. **Admin success-DM threshold in `PaymentCog`** (`payment_cog.py:~120-134`, `_dm_admin_payment_success`). Env vars `ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD` and `ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS`. The conditional branch inside `_handle_terminal_payment`. Instance attributes `_admin_success_dm_threshold_usd` and `_admin_success_dm_providers` move with `_dm_admin_payment_success` to `PaymentWorkerCog`.
5. **Prompt-injection hardening in `admin_chat/tools.py`** — wallet resolution via `wallet_registry` lookup only. This cleanup does not touch that tool.
6. **RLS documentation comment at the top of `sql/payments.sql`** (lines 1-7). Phase 2 Step 5 adds `tx_signature_history` inside the table body; the RLS preamble stays untouched.
7. **`_redact_wallet` helper duplicated in three files** (`payment_service.py:17`, `payment_cog.py:18`, `db_handler.py:13`). Consolidation is OUT OF SCOPE; duplicate into `PaymentWorkerCog` too.
8. **Producer flow current state**: both `grants` and `admin_chat` use test auto-confirm + recipient final confirmation. `admin_chat` additionally accepts a free-text recipient reply for final confirmation (`admin_chat_cog.py:284-309`) in parallel with the button path created via `send_confirmation_request` (`admin_chat_cog.py:389-402`). **The registry must represent both paths.**

## Phase 1: Authorization consolidation

### Step 1: Define `PaymentActor` vocabulary (`src/features/payments/payment_service.py`)
**Scope:** Small
1. Add `PaymentActorKind(str, Enum)` with `RECIPIENT_CLICK`, `RECIPIENT_MESSAGE`, `AUTO`, `ADMIN_DM`. Subclassing `str` keeps `confirmed_by` DB writes as simple strings.
2. Add `PaymentActor` dataclass `(kind: PaymentActorKind, actor_id: Optional[int])`.
3. Export both from `payment_service.py`.

### Step 2: `PRODUCER_FLOWS` registry as a frozenset of allowed actor kinds (`src/features/payments/producer_flows.py` — new)
**Scope:** Medium
1. Create `src/features/payments/producer_flows.py`:
   ```python
   @dataclass(frozen=True)
   class ProducerFlow:
       test_confirmed_by: frozenset[PaymentActorKind]
       real_confirmed_by: frozenset[PaymentActorKind]
       thread_cleanup: bool = False
       notify_admin_dm_on_approval: bool = False

   PRODUCER_FLOWS: dict[str, ProducerFlow] = {
       "grants": ProducerFlow(
           test_confirmed_by=frozenset({PaymentActorKind.AUTO}),
           real_confirmed_by=frozenset({PaymentActorKind.RECIPIENT_CLICK}),
       ),
       "admin_chat": ProducerFlow(
           test_confirmed_by=frozenset({PaymentActorKind.AUTO}),
           real_confirmed_by=frozenset({
               PaymentActorKind.RECIPIENT_CLICK,
               PaymentActorKind.RECIPIENT_MESSAGE,
           }),
       ),
   }

   def get_flow(producer: str) -> ProducerFlow: ...   # raises KeyError → fail closed
   ```
2. **Why frozenset, not a single value:** `admin_chat` currently accepts BOTH the payment confirmation button (`admin_chat_cog.py:389-402`) AND a free-text recipient reply (`admin_chat_cog.py:284-309`) for the same final payment row. A single `real_confirmed_by` value cannot represent this; a set captures "any of these actor kinds is acceptable." This is the exact current behavior.
3. `get_flow(producer)` raises on unknown producer → fail closed. Used by `PaymentService._authorize_actor`.
4. Authorization-only in this cleanup. `thread_cleanup` and `notify_admin_dm_on_approval` fields are present for the admin-DM feature to consume but are NOT read in this plan. Flagged as assumption.

### Step 3: Rewrite `PaymentService.confirm_payment` (`src/features/payments/payment_service.py:302`)
**Scope:** Medium
1. Replace signature with `confirm_payment(payment_id, *, actor: PaymentActor, guild_id=None)`. Drop `confirmed_by`, `confirmed_by_user_id`, `privileged_override`.
2. Add a private `_authorize_actor(payment, actor) -> bool`. Algorithm:
   - Resolve `flow = get_flow(payment['producer'])`. Unknown producer → False + WARNING (fail closed).
   - Resolve `allowed = flow.test_confirmed_by if payment['is_test'] else flow.real_confirmed_by`.
   - If `actor.kind not in allowed` → False + WARNING.
   - For recipient-kind actors (`RECIPIENT_CLICK`, `RECIPIENT_MESSAGE`): preserve hardening null-fail-closed — require `payment['recipient_discord_id'] is not None and actor.actor_id is not None and int(payment['recipient_discord_id']) == int(actor.actor_id)`.
   - For `ADMIN_DM`: require `actor.actor_id == int(os.getenv('ADMIN_USER_ID'))` (null check on both sides, fail closed).
   - For `AUTO`: no identity check beyond `actor.kind in allowed` (set membership already gates it to test-mode `AUTO`-permitted producers).
3. Persist `confirmed_by=actor.kind.value`, `confirmed_by_user_id=actor.actor_id` via `mark_payment_confirmed_by_user`. No schema change.
4. Log rejects at WARNING with `payment_id`, `actor.kind`, `actor.actor_id`, concrete reason. Matches the existing line 322 log shape so existing log consumers keep working.
5. Delete the `privileged_override` branch.

### Step 4: Thin the four call sites
**Scope:** Medium
1. `src/features/payments/payment_cog.py:43-71` — `PaymentConfirmView._confirm_button_pressed`: delete manual check at lines 57-63; call `confirm_payment(..., actor=PaymentActor(RECIPIENT_CLICK, interaction.user.id))`. On `None`, ephemerally send "Only the intended recipient can confirm this payment." to preserve user-visible text.
2. `src/features/admin_chat/admin_chat_cog.py:284-309` — `_handle_confirmation_received`: pass `PaymentActor(RECIPIENT_MESSAGE, message.author.id)`.
3. `src/features/grants/grants_cog.py:605-610` — pass `PaymentActor(AUTO, grant['applicant_id'])`.
4. `src/features/admin_chat/admin_chat_cog.py:269-274` — pass `PaymentActor(AUTO, intent['recipient_user_id'])`.
5. `grep "privileged_override" src/ tests/` must return empty.

## Phase 2: Audit-trail preservation

### Step 5: Add `tx_signature_history` column (`sql/payments.sql`)
**Scope:** Small
1. After `sql/payments.sql:112` (inside the `payment_requests` table body, well below the RLS preamble) add `tx_signature_history jsonb not null default '[]'::jsonb,`.
2. Append an idempotent `alter table public.payment_requests add column if not exists tx_signature_history jsonb not null default '[]'::jsonb;` plus a backfill `update ... set tx_signature_history = jsonb_build_array(jsonb_build_object('signature', tx_signature, 'status', status, 'timestamp', coalesce(completed_at, submitted_at, updated_at), 'reason', 'backfill', 'send_phase', send_phase)) where tx_signature is not null and jsonb_array_length(tx_signature_history) = 0;`.
3. Leave the `uq_payment_requests_tx_signature` index at line 137 untouched.

### Step 6: Append history on every attempt (`src/common/db_handler.py`)
**Scope:** Medium
1. Add `_append_tx_signature_history(payment_id, entry, *, guild_id=None)` using `tx_signature_history = tx_signature_history || $entry::jsonb`. Standalone method called after the primary update (simpler than merging into `_update_payment_request_record`).
2. `requeue_payment` at `db_handler.py:2142-2162` — before the update that sets `tx_signature=None`, read the current row and append `{signature, status:'failed', timestamp:now, reason:'requeue', send_phase}`. Keep the `'tx_signature': None` line.
3. Grep `tx_signature` assignments in `db_handler.py` and append history at each concrete write: `mark_payment_submitted` → `{reason:'submit'}`; `mark_payment_confirmed` (or equivalents) → `{reason:'confirm'}`. Consistent entry shape across all reasons.

## Phase 3: PaymentCog split

### Step 7: Extract `PaymentWorkerCog` (`src/features/payments/payment_worker_cog.py` — new)
**Scope:** Large
1. Move from `payment_cog.py`: `payment_worker` loop, `_before_payment_worker`, `_ensure_startup_sync`, `_bot_is_ready`, `_recover_inflight_payments`, `_process_claimed_payment`, `_handle_terminal_payment`, `_handoff_terminal_result`, `_flush_pending_terminal_handoffs`, `_dm_admin_payment_success`, `_dm_admin_payment_failure`, `_notify_payment_result`, `_candidate_producer_cog_names`, `_resolve_destination`, `_send_message`, `_build_result_message`, `_token_label`, `_get_writable_guild_ids`, `_is_terminal`, and the `_pending_terminal_handoffs`/`_replayed_pending_handoffs` state.
2. Also move the success-DM instance state: `self._admin_success_dm_threshold_usd` and `self._admin_success_dm_providers` (env reads in `__init__`). Preserve the conditional branch in `_handle_terminal_payment` that calls `_dm_admin_payment_success` when `status='confirmed' and not is_test and provider in self._admin_success_dm_providers and amount_usd >= self._admin_success_dm_threshold_usd`. **Hardening behavior — do not simplify.**
3. Copy (do not consolidate) the `_redact_wallet` helper into the new file.
4. **Wire `safe_delete_messages` here** (see Step 10). At the end of `_handle_terminal_payment`, after `_notify_payment_result` completes, resolve the terminal payment's channel via `_resolve_destination` and call `await safe_delete_messages(channel, payment.get('metadata', {}).get('cleanup_message_ids') or [], logger=logger)`. No current producer populates `cleanup_message_ids`, so today this is a no-op per call (empty list → returns zero counts); the admin-DM feature will populate the field when it creates payments. This gives item (5) a real, tested caller today without changing user-visible behavior.
5. **Register in `main.py` alongside existing wiring.** Grep `main.py` for `PaymentCog(` and replace the registration with `PaymentWorkerCog` + `PaymentUICog` (added in Step 8).

### Step 8: Extract `PaymentUICog` (`src/features/payments/payment_ui_cog.py` — new)
**Scope:** Medium
1. Move `PaymentConfirmView` (`payment_cog.py:27-103`), `send_confirmation_request` (`payment_cog.py:219`), `_register_pending_confirmation_views` (`payment_cog.py:189-206`), `_build_confirmation_message`.
2. Register persistent views in `PaymentUICog.cog_load` → calls `_register_pending_confirmation_views`. Runs before `on_ready`, so restart safety is preserved.
3. Cross-cog handoff: callers that currently do `bot.get_cog('PaymentCog').send_confirmation_request(...)` (e.g., `admin_chat_cog.py:390-393`) now do `bot.get_cog('PaymentUICog').send_confirmation_request(...)`. Expose `bot.payment_ui_cog` attribute for ergonomics, mirroring `bot.payment_service`. Grep all call sites of `send_confirmation_request` and `get_cog('PaymentCog')` and update every match.

### Step 9: Retarget startup wiring and delete `payment_cog.py`
**Scope:** Medium
1. **Update `main.py:65-77` `_bind_cap_breach_dm`:** replace `bot.get_cog('PaymentCog')` with `bot.get_cog('PaymentWorkerCog')` and update the attribute check to `hasattr(payment_worker_cog, '_dm_admin_payment_failure')`. The warning log string moves from "PaymentCog" to "PaymentWorkerCog". The callback must fire `_dm_admin_payment_failure` on the worker cog. **Without this, the hardening cap-breach DM stops working after the split.**
2. Grep `main.py` AND all of `src/` / `tests/` for any other `get_cog('PaymentCog')` or `bot.payment_cog` references and retarget each to the correct new cog (worker vs UI) based on which method is being called.
3. Remove `src/features/payments/payment_cog.py` once both new cogs are wired and all `get_cog` references are updated. No compat shim.
4. Grep `src/`, `tests/`, `main.py` for `from src.features.payments.payment_cog` and fix each.

## Phase 4: Thread cleanup primitive

### Step 10: Add `safe_delete_messages` (`src/common/discord_utils.py`)
**Scope:** Small
1. Append to `src/common/discord_utils.py`:
   ```python
   @dataclass
   class DeleteCounts:
       deleted: int
       skipped: int
       errored: int

   async def safe_delete_messages(
       channel,
       message_ids: Iterable[int],
       *,
       logger: logging.Logger,
   ) -> DeleteCounts:
       ...
   ```
2. Per id: fetch via `channel.fetch_message(id)` then `.delete()`. Catch:
   - `discord.NotFound` → `skipped += 1`
   - `discord.Forbidden` → `errored += 1`, log once per call at WARNING
   - `discord.HTTPException` with status 429 → `await asyncio.sleep(getattr(exc, 'retry_after', 1.0))` then retry once, count final outcome
   - other `discord.HTTPException`, `AttributeError` (archived/locked thread lookup failure) → `errored += 1`, log
   - Never raise out of the helper.
3. Pre-check: if `channel is None` → return `DeleteCounts(0, len(ids), 0)`.
4. **Caller:** wired in Step 7 from `PaymentWorkerCog._handle_terminal_payment` against `payment['metadata'].get('cleanup_message_ids')`. Today that list is empty for all producers, so the call is a harmless no-op — no user-visible behavior change. The wiring exists so the admin-DM feature can populate the field without a follow-up refactor. Success criterion "safe_delete_messages is used by the thread cleanup path" is satisfied by the integration in the worker cog plus the corresponding integration test in Step 12.

## Phase 5: Tests

### Step 11: Update existing tests to new API (`tests/test_admin_payments.py`, `tests/conftest.py`, `tests/test_caller_paths.py`)
**Scope:** Medium
1. Grep `tests/` for `confirm_payment` and `privileged_override`; migrate each call to `actor=PaymentActor(...)`.
2. Delete local permission checks in fakes (`FakeAdminPaymentCog`, `FakeFlowCog`) — authorization lives in the real `PaymentService` now.
3. Fakes that patched `bot.get_cog('PaymentCog')` must be updated to stub `PaymentWorkerCog` and/or `PaymentUICog` as appropriate for each test.
4. Keep all user-visible assertions unchanged.

### Step 12: New targeted tests
**Scope:** Medium
1. `tests/test_payment_authorization.py` — unit suite over `PaymentService.confirm_payment`:
   - recipient_click wrong id → `None`; right id → confirms
   - recipient_message against `grants` (not in the set) → `None`; against `admin_chat` → confirms
   - recipient_click against `admin_chat` → confirms (proves both branches of the set are honored)
   - null `recipient_discord_id` with recipient-kind actor → `None` (preserves hardening null-fail-closed)
   - auto on non-test payment → `None`; auto on test payment for grants → confirms
   - admin_dm with wrong id → `None`; with right id against a flow that permits it → confirms
   - unknown producer → `None` + WARNING log
2. `tests/test_tx_signature_history.py` — verifies `requeue_payment` appends an entry with the prior signature + `reason='requeue'`, blanks the pointer, and two submit→requeue cycles produce two history entries. Uses the in-memory fake `db_handler`.
3. `tests/test_safe_delete_messages.py` — `FakeChannel` raising each of `NotFound`/`Forbidden`/`HTTPException(429)`/generic `HTTPException`; assert counts and that no exception escapes. Include a `channel=None` case.
4. `tests/test_payment_cog_split.py` — minimal bot harness registering both cogs with a seeded `pending_confirmation` payment; asserts `bot.add_view` was called (proves restart safety). Also asserts `_bind_cap_breach_dm` wiring resolves `PaymentWorkerCog` and routes a fake cap-breach event to its `_dm_admin_payment_failure`.
5. `tests/test_terminal_cleanup_wiring.py` — boots a `PaymentWorkerCog` with a stub `safe_delete_messages` and a stub `_notify_payment_result`, feeds a terminal payment whose `metadata.cleanup_message_ids = [101, 102]`, and asserts `safe_delete_messages` was called with those ids. Also asserts an empty/missing list results in a no-op call with `DeleteCounts(0,0,0)` — proves the no-behavior-change claim for current producers.

### Step 13: Run the suites
**Scope:** Small
1. `pytest tests/test_payment_authorization.py tests/test_tx_signature_history.py tests/test_safe_delete_messages.py tests/test_payment_cog_split.py tests/test_terminal_cleanup_wiring.py -x`.
2. `pytest tests/test_admin_payments.py tests/test_caller_paths.py -x`.
3. Full `pytest`. Final grep for `privileged_override` / `confirmed_by='user'` literals and any lingering `get_cog('PaymentCog')`.

## Execution Order
1. Phase 1 Steps 1-4 land together (authorization + registry) — self-contained prereq slice.
2. Phase 2 Steps 5-6 (history column) land independently — additive schema + write path.
3. Phase 4 Step 10 (`safe_delete_messages` helper) lands before Phase 3 Step 7 so the worker cog can import it.
4. Phase 3 Steps 7-9 (cog split + startup rewiring) land after Phase 1 and Phase 4 Step 10 are stable.
5. Phase 5 tests interleave with their steps; Step 13 runs at the end.

## Validation Order
1. `pytest tests/test_payment_authorization.py` — cheapest proof after Phase 1.
2. `pytest tests/test_tx_signature_history.py` after Phase 2.
3. `pytest tests/test_safe_delete_messages.py` after Phase 4 Step 10.
4. `pytest tests/test_payment_cog_split.py tests/test_terminal_cleanup_wiring.py` after Phase 3 — persistent-view restart safety, cap-breach rewire, and terminal cleanup wiring.
5. `pytest tests/test_admin_payments.py tests/test_caller_paths.py` — behavioral regression.
6. Full `pytest`.
7. Manual smoke (info-only): grants test auto-confirm + real recipient-click payout in a test guild; restart with `pending_confirmation` payment and click the pre-restart button; trigger a cap-breach manual-hold and verify the admin DM still fires.
