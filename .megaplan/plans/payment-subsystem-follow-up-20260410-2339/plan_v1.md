# Implementation Plan: Payment Subsystem Follow-up Cleanups

## Overview
Behavior-preserving refactor of the Solana payout pipeline to unblock the in-flight admin-DM approval feature. Five changes: centralize confirmation authorization in `PaymentService`, introduce a declarative `PRODUCER_FLOWS` registry, preserve `tx_signature` history across requeues, split `PaymentCog` into worker and UI halves, and add a shared `safe_delete_messages` primitive. Providers (`solana_client.py`, `solana_provider.py`) are out of scope. Hot spots: `PaymentService.confirm_payment` at `src/features/payments/payment_service.py:302`, ad-hoc permission checks in `src/features/payments/payment_cog.py:57` and `src/features/admin_chat/admin_chat_cog.py:284`, `db_handler.requeue_payment` blanking `tx_signature` at `src/common/db_handler.py:2153`, a 576-line `PaymentCog` registering persistent views at `src/features/payments/payment_cog.py:200`.

## Hardening invariants to preserve (added after the hardening megaplan landed)
The hardening megaplan (`harden-the-solana-payment-20260410-2147`) finished execute before this cleanup runs. Its additions must be preserved. **The executor must not touch or regress any of the following:**

1. **Cap enforcement in `PaymentService.request_payment`** (`payment_service.py:~38-51, 117, 173-273, 545-546`).
   - `__init__` params: `per_payment_usd_cap`, `daily_usd_cap`, `capped_providers`, `on_cap_breach`
   - Cap-breach flow: a rejected payment is written with `status='manual_hold'` and `last_error` set, then `_emit_cap_breach(created)` fires the registered callback
   - `_emit_cap_breach` helper at line 545 and the `_on_cap_breach` instance attribute
   - **This cleanup rewrites `confirm_payment`, NOT `request_payment`. Do not touch the cap logic.**

2. **Idempotency collision / canonical-row wallet-swap defense in `PaymentService.request_payment`** (`payment_service.py:~283-300`).
   - The `canonical_row` / `canonical_wallet` detection path that blocks a new request when a prior row for the same `(producer, producer_ref)` has a different `recipient_wallet`.
   - Emits `_emit_cap_breach(canonical_row)` on mismatch.
   - **Do not refactor or simplify — it's the wallet-swap belt-and-suspenders layer.**

3. **Null-recipient fail-closed in `confirm_payment`** (`payment_service.py:~319-328`).
   - Current line 321 reads: `if expected_user_id is None or confirmed_by_user_id is None or int(expected_user_id) != int(confirmed_by_user_id)` — this is the post-hardening fail-closed form.
   - **When rewriting `confirm_payment` in Phase 1 Step 2, the new `_authorize_actor` must preserve this null-rejection semantic for all recipient-kind actors.**

4. **Admin success-DM threshold in `PaymentCog`** (`payment_cog.py:~120-134, +_dm_admin_payment_success method`).
   - New env vars `ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD` (default `100`) and `ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS` (default `solana_payouts`)
   - `_handle_terminal_payment` contains a new conditional branch that calls `_dm_admin_payment_success` when `status='confirmed' and not is_test and provider in capped_providers and amount_usd >= threshold`
   - **Phase 3 Step 7 already lists `_dm_admin_payment_success` in the methods to move — this is correct. The instance attributes `_admin_success_dm_threshold_usd` and `_admin_success_dm_providers` must also move with it to `PaymentWorkerCog`.**

5. **Prompt-injection hardening in `admin_chat/tools.py`** — the `initiate_payment` / intent-creation tool no longer accepts a wallet address from the LLM; wallet resolution is strictly via `wallet_registry` lookup keyed on the mentioned Discord user. This cleanup does not touch that tool.

6. **RLS documentation comment at the top of `sql/payments.sql`** (lines 1-7) — the `-- RLS posture:` preamble. Phase 2 Step 5 adds the `tx_signature_history` column further down in the table definition; the RLS comment block at the top stays untouched.

7. **The `_redact_wallet` helper is now duplicated in three files** (`payment_service.py:17`, `payment_cog.py:18`, `db_handler.py:13`). Consolidating it is OUT OF SCOPE for this cleanup — flagged as a follow-up cleanup. When extracting `PaymentWorkerCog` in Phase 3, duplicate the helper in the new file too; do not try to collapse it.

8. **Producer flow current state for the `PRODUCER_FLOWS` registry**: both `grants` and `admin_chat` currently use `(test_confirmed_by=AUTO, real_confirmed_by=RECIPIENT_CLICK)`. The admin_chat auto-confirm happens programmatically at `admin_chat_cog.py:269-274` and the grants auto-confirm at `grants_cog.py:605-610` — both call `confirm_payment` with `confirmed_by_user_id=<recipient_id>` right after `request_payment(is_test=True)`. **Step 4's registry initialization must match this exact current behavior, not the admin-DM feature's future state.**

## Phase 1: Authorization consolidation

### Step 1: Define `PaymentActor` vocabulary (`src/features/payments/payment_service.py`)
**Scope:** Small
1. Add a `PaymentActorKind(str, Enum)` with `RECIPIENT_CLICK`, `RECIPIENT_MESSAGE`, `AUTO`, `ADMIN_DM`. Subclassing `str` keeps `confirmed_by` DB writes as simple strings.
2. Add a `PaymentActor` dataclass `(kind: PaymentActorKind, actor_id: Optional[int])` — actor_id is the Discord user id.
3. Export both from `payment_service.py`.

### Step 2: Rewrite `PaymentService.confirm_payment` (`src/features/payments/payment_service.py:302`)
**Scope:** Medium
1. Replace signature with `confirm_payment(payment_id, *, actor: PaymentActor, guild_id=None)`. Drop `confirmed_by`, `confirmed_by_user_id`, `privileged_override`.
2. Move recipient-id match from `payment_service.py:319-328` and from the cog check at `payment_cog.py:57-63` into a private `_authorize_actor(payment, actor)`. Auth table: RECIPIENT_* → `actor_id == recipient_discord_id`; AUTO → `is_test is True` AND flow's `test_confirmed_by == AUTO`; ADMIN_DM → `actor_id == int(ADMIN_USER_ID)` AND flow permits admin DM for this `is_test` branch.
3. Persist `confirmed_by=actor.kind.value` and `confirmed_by_user_id=actor.actor_id` via `mark_payment_confirmed_by_user`. No schema change.
4. Log rejects at WARNING with payment_id + actor kind + reason, matching the existing line 322 log shape.
5. Delete the `privileged_override` branch.

### Step 3: Thin the four call sites
**Scope:** Medium
1. `src/features/payments/payment_cog.py:43-71` — `PaymentConfirmView._confirm_button_pressed`: delete the manual check at lines 57-63; call `confirm_payment(..., actor=PaymentActor(RECIPIENT_CLICK, interaction.user.id))`. If service returns None, ephemerally send "Only the intended recipient can confirm this payment." to preserve user-visible text.
2. `src/features/admin_chat/admin_chat_cog.py:284-309` — `_handle_confirmation_received`: pass `PaymentActor(RECIPIENT_MESSAGE, message.author.id)`.
3. `src/features/grants/grants_cog.py:605-610` — pass `PaymentActor(AUTO, grant['applicant_id'])`.
4. `src/features/admin_chat/admin_chat_cog.py:269-274` — pass `PaymentActor(AUTO, intent['recipient_user_id'])`.
5. `grep "privileged_override" src/ tests/` must return empty.

### Step 4: `PRODUCER_FLOWS` registry (`src/features/payments/producer_flows.py` — new)
**Scope:** Medium
1. Create the new module defining `ProducerFlow(test_confirmed_by, real_confirmed_by, thread_cleanup=False, notify_admin_dm_on_approval=False)` and a `PRODUCER_FLOWS` dict. Entries: `grants` → `(AUTO, RECIPIENT_CLICK)`; `admin_chat` → `(AUTO, RECIPIENT_CLICK)` matching **current** behavior (not the brief's future-state `(RECIPIENT_CLICK, ADMIN_DM)` — that's the admin-DM feature's job and would break the behavior-preservation constraint).
2. `get_flow(producer)` raises on unknown producer → fail closed. `PaymentService._authorize_actor` consults it.
3. Do NOT drive additional state transitions from the registry in this plan — anything beyond authorization is out of scope and flagged as assumption.

## Phase 2: Audit-trail preservation

### Step 5: Add `tx_signature_history` column (`sql/payments.sql`)
**Scope:** Small
1. After `sql/payments.sql:112` add `tx_signature_history jsonb not null default '[]'::jsonb,`.
2. Append an idempotent `alter table ... add column if not exists ...` plus a backfill `update ... set tx_signature_history = jsonb_build_array(jsonb_build_object('signature', tx_signature, 'status', status, 'timestamp', coalesce(completed_at, submitted_at, updated_at), 'reason', 'backfill', 'send_phase', send_phase)) where tx_signature is not null and jsonb_array_length(tx_signature_history) = 0;`.
3. Leave the `uq_payment_requests_tx_signature` index at line 137 untouched — `tx_signature` is still the most-recent-attempt pointer.

### Step 6: Append history on every attempt (`src/common/db_handler.py`)
**Scope:** Medium
1. Add `_append_tx_signature_history(payment_id, entry)` using `tx_signature_history = tx_signature_history || $entry::jsonb`. Simpler as a standalone method called right after the primary update than trying to merge into `_update_payment_request_record`.
2. `requeue_payment` at `db_handler.py:2142-2162` — before the update that sets `tx_signature=None`, read the current row and append an entry `{signature, status:'failed', timestamp:now, reason:'requeue', send_phase}`. Keep the `'tx_signature': None` line.
3. Audit every `tx_signature` assignment in `db_handler.py` (grep `tx_signature`): in `mark_payment_submitted`/`mark_payment_confirmed` (or equivalents), append `{reason:'submit'}` or `{reason:'confirm'}` after the write.

## Phase 3: PaymentCog split

### Step 7: Extract `PaymentWorkerCog` (`src/features/payments/payment_worker_cog.py` — new)
**Scope:** Large
1. Move from `payment_cog.py`: `payment_worker` loop, `_before_payment_worker`, `_ensure_startup_sync`, `_bot_is_ready`, `_recover_inflight_payments`, `_process_claimed_payment`, `_handle_terminal_payment`, `_handoff_terminal_result`, `_flush_pending_terminal_handoffs`, `_dm_admin_payment_success`, `_dm_admin_payment_failure`, `_notify_payment_result`, `_candidate_producer_cog_names`, `_resolve_destination`, `_send_message`, `_build_result_message`, `_token_label`, `_get_writable_guild_ids`, `_is_terminal`, and the `_pending_terminal_handoffs`/`_replayed_pending_handoffs` state.
2. Also move the success-DM instance state: `self._admin_success_dm_threshold_usd` and `self._admin_success_dm_providers` (the env-var reads in `__init__`). These are tightly coupled to `_dm_admin_payment_success` and belong on `PaymentWorkerCog`.
3. Preserve the conditional dispatch inside `_handle_terminal_payment` that calls `_dm_admin_payment_success` when `status='confirmed' and not is_test and provider in self._admin_success_dm_providers and amount_usd >= self._admin_success_dm_threshold_usd`. This is hardening behavior — do not simplify or remove.
4. Copy (do not move) the `_redact_wallet` helper from `payment_cog.py:18` into the new file — it's used by result-message formatting. The duplication across files is a known follow-up cleanup; do not consolidate in this pass.
5. Register in `main.py` alongside the existing wiring. Grep `main.py` for `PaymentCog(`.
6. **Cap-breach wiring check:** the hardening `on_cap_breach` callback is registered where `PaymentService` is constructed (in `main.py`, not in the cog). Moving the cog does not affect the callback wiring. Verify no existing code paths from within `PaymentCog` call `PaymentService._emit_cap_breach` directly — the cog should not be the callback target; `main.py` is.

### Step 8: Extract `PaymentUICog` (`src/features/payments/payment_ui_cog.py` — new)
**Scope:** Medium
1. Move `PaymentConfirmView` (`payment_cog.py:27-103`), `send_confirmation_request` (`payment_cog.py:219`), `_register_pending_confirmation_views` (`payment_cog.py:189-206`), `_build_confirmation_message`.
2. Register persistent views in `PaymentUICog.cog_load` — calls `_register_pending_confirmation_views`. Runs before `on_ready`, so restart safety is preserved.
3. Cross-cog handoff: worker calls `self.bot.payment_ui_cog.send_confirmation_request(payment_id)`. Expose `bot.payment_ui_cog` similarly to `bot.payment_service`. Grep for current `send_confirmation_request` callers and update.

### Step 9: Delete `payment_cog.py`
**Scope:** Small
1. Remove `src/features/payments/payment_cog.py` once both new cogs are wired. No compat shim.
2. Grep `src/`, `tests/`, `main.py` for `from src.features.payments.payment_cog` and `PaymentCog(` and fix.

## Phase 4: Thread cleanup primitive

### Step 10: Add `safe_delete_messages` (`src/common/discord_utils.py`)
**Scope:** Small
1. Append `DeleteCounts` dataclass `(deleted, skipped, errored)` and `async def safe_delete_messages(channel, message_ids, *, logger) -> DeleteCounts`.
2. Per id: catch `discord.NotFound` → skipped; `discord.Forbidden` → errored + log once at WARNING; `HTTPException` with 429 → sleep `retry_after` then retry once; other `HTTPException`/`AttributeError` → errored + log. Never raise.
3. Pre-check: if `channel is None` → all skipped. Archived-thread unarchive is out of scope; Forbidden/HTTPException is what surfaces and it's already handled.
4. Caller: grep for existing `.delete()` calls on messages in `src/features/payments/` and `src/features/grants/`; if any terminal-path cleanup exists today, route it through the helper. If not, the helper stands alone for the admin-DM feature — flagged as question.

## Phase 5: Tests

### Step 11: Update existing tests to new API (`tests/test_admin_payments.py`, `tests/conftest.py`, `tests/test_caller_paths.py`)
**Scope:** Medium
1. Grep `tests/` for `confirm_payment` and `privileged_override`; migrate each call to `actor=PaymentActor(...)`.
2. Delete local permission checks in fakes (`FakeAdminPaymentCog`, `FakeFlowCog`) — authorization lives in the real `PaymentService` now.
3. Keep all user-visible assertions unchanged.

### Step 12: New targeted tests
**Scope:** Medium
1. `tests/test_payment_authorization.py` — unit suite over `PaymentService.confirm_payment`: recipient_click wrong/right id, auto on real vs test payment, admin_dm wrong/right admin id against a flow that permits it, unknown producer → None (fail closed).
2. `tests/test_tx_signature_history.py` — verifies `requeue_payment` appends a history entry with the prior signature + `reason='requeue'`, blanks the pointer, and two submit→requeue cycles produce two entries.
3. `tests/test_safe_delete_messages.py` — `FakeChannel` raising each of NotFound/Forbidden/HTTPException(429)/HTTPException; assert counts and that no exception escapes.
4. `tests/test_payment_cog_split.py` — minimal bot harness registering both cogs with a seeded `pending_confirmation` payment; asserts `bot.add_view` was called — proves "restart safety."

### Step 13: Run the suites
**Scope:** Small
1. `pytest tests/test_payment_authorization.py tests/test_tx_signature_history.py tests/test_safe_delete_messages.py tests/test_payment_cog_split.py -x`.
2. `pytest tests/test_admin_payments.py tests/test_caller_paths.py -x`.
3. Full `pytest`. Final grep for `privileged_override` / `confirmed_by='user'` literals.

## Execution Order
1. Phase 1 Steps 1-4 land together (authorization + registry) — self-contained prereq slice.
2. Phase 2 Steps 5-6 (history column) land independently — additive schema + write path.
3. Phase 3 Steps 7-9 (cog split) land after Phase 1 so the split consumes the stable `PaymentActor` API.
4. Phase 4 Step 10 can land any time.
5. Phase 5 tests interleave with their steps; Step 13 runs at the end.

## Validation Order
1. `pytest tests/test_payment_authorization.py` — cheapest proof after Phase 1.
2. `pytest tests/test_tx_signature_history.py` after Phase 2.
3. `pytest tests/test_safe_delete_messages.py` after Phase 4.
4. `pytest tests/test_payment_cog_split.py` after Phase 3 — persistent-view restart safety.
5. `pytest tests/test_admin_payments.py tests/test_caller_paths.py` — behavioral regression.
6. Full `pytest`.
7. Manual smoke (info-only): grants test auto-confirm + real recipient-click payout in a test guild.
