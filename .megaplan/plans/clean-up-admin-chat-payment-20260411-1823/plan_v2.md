# Implementation Plan: Clean up admin_chat payment message noise

## Overview

Today each admin_chat payout produces a stream of disconnected channel messages: a "queued test" note, a "reply confirmed when you see it" prompt, a "confirmation received, payout queued" note, a **"Payment to <@…> queued for sending."** update posted by `PaymentUICog` on admin approval, a fat multi-line "Payment sent" card from `AdminChatCog`, and — from `PaymentWorkerCog._notify_payment_result` — a second generic "Payment <Status>" result card written directly from the shared worker. Together they spam the channel and duplicate information the admin_chat flow already renders better.

Goals:
1. Collapse all intermediate status updates for a single payout into **one status message**, edited in place, owned by the intent row via a new `status_message_id` column.
2. When the final payout confirms, finish by **replying** to the status message with a short "Payment sent." that tags the recipient, and suppress the explorer-link embed so we don't re-render the Solana card.
3. **Suppress the generic `PaymentService` result card for `producer='admin_chat'` only** — other producers keep today's behavior.
4. Persist the message id via an idempotent `add column if not exists` block appended to `sql/admin_payment_intents.sql`, and **tolerate the column being absent** on older deployments (so we don't crash if the sql file hasn't been re-applied yet).
5. Update `tests/test_admin_payments.py` accordingly.

Constraints surfaced during flag review:
- `PaymentUICog._post_admin_approval_thread_update` (`src/features/payments/payment_ui_cog.py:550-565`) currently sends its own "queued for sending" channel message on admin approval, and `PaymentUICog._cleanup_admin_intent_messages` (`src/features/payments/payment_ui_cog.py:567-584`) then deletes any message id stored in `prompt_message_id` / `receipt_prompt_message_id`. Any redesign that reuses those slots for the status message must prevent that cleanup from deleting the shared status message, and must fold the "queued for sending" transition into an **edit** of the same status message rather than a new send. **Both payment_ui_cog touch points are in scope.**
- `db_handler.update_admin_payment_intent` (`src/common/db_handler.py:2143-2161`) currently catches all exceptions and returns `None` on failure. Callers like `admin_chat_cog.py:703-710` and `admin_chat_cog.py:771-773` branch on a falsey return to trigger admin review. The column-absent tolerance must **preserve that contract** — it may not start raising exceptions for unrelated errors.

Key touch points:
- `src/features/admin_chat/admin_chat_cog.py`
  - `_start_admin_payment_flow` — `admin_chat_cog.py:380-430` (creates the test payment, sends "I've queued a small test payment" message)
  - `_handle_recipient_test_receipt_confirmation` — `admin_chat_cog.py:680-711` (sends "confirmation received. The payout has been queued.")
  - `handle_payment_result` — `admin_chat_cog.py:713-810` (emits receipt prompt + "Payment sent." card + admin DMs)
- `src/features/payments/payment_ui_cog.py`
  - `_post_admin_approval_thread_update` — `payment_ui_cog.py:550-565`
  - `_cleanup_admin_intent_messages` — `payment_ui_cog.py:567-584`
- `src/features/payments/payment_worker_cog.py`
  - `_handle_terminal_payment` — `payment_worker_cog.py:149-166`
  - `_notify_payment_result` / `_build_result_message` — `payment_worker_cog.py:402-478`
- `src/common/db_handler.py`
  - `update_admin_payment_intent` — `db_handler.py:2143-2161`
- `sql/admin_payment_intents.sql` — existing schema at `sql/admin_payment_intents.sql:1-97`, with the `add column if not exists` pattern already in use (`sql/admin_payment_intents.sql:50-57`).
- `tests/test_admin_payments.py` — `FakeChannel`, `FakeIntentDB`, `FakeBot` around `test_admin_payments.py:71-298`; existing result-flow tests at `test_admin_payments.py:1466`, `:2079`, `:2146`.

## Phase 1: Schema and storage

### Step 1: Add `status_message_id` column (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Append** an `add column if not exists status_message_id bigint;` block to `sql/admin_payment_intents.sql` immediately after the existing additive migrations at `sql/admin_payment_intents.sql:57`, matching the surrounding idempotent style.
2. **Do not** touch indexes, constraints, or the status enum — the column is plain metadata.

### Step 2: Tolerate the column being absent in `update_admin_payment_intent` (`src/common/db_handler.py`)
**Scope:** Small
1. **Keep** the existing outer `try/except Exception → log + return None` contract at `db_handler.py:2143-2161` intact. Callers rely on a falsey return for their fail-closed recovery paths and must not start seeing new exceptions.
2. **Add** a narrow inner retry path *inside* the existing `try` block: wrap the Supabase `.update(...).execute()` call in its own nested `try`. If it raises and both `'status_message_id' in payload` and `'status_message_id' in str(exc)` hold, log once at `info`, pop the key, and re-run the update. Any other error re-raises out of the nested try and is caught by the outer handler (which still returns `None`). This way non-`status_message_id` failures behave exactly as they do today.
3. **Do not** introduce a generic "ignore unknown column" helper, and do not change the return type or contract.

    ```python
    try:
        payload = self._serialize_supabase_value(dict(record))
        try:
            result = self.supabase.table('admin_payment_intents').update(payload).eq(...).execute()
        except Exception as inner_exc:
            if 'status_message_id' in payload and 'status_message_id' in str(inner_exc):
                logger.info(
                    "update_admin_payment_intent: status_message_id column absent; retrying without it"
                )
                payload.pop('status_message_id', None)
                result = self.supabase.table('admin_payment_intents').update(payload).eq(...).execute()
            else:
                raise
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error updating admin payment intent {intent_id}: {e}", exc_info=True)
        return None
    ```

## Phase 2: Worker-side suppression

### Step 3: Skip the generic result card for admin_chat (`src/features/payments/payment_worker_cog.py`)
**Scope:** Small
1. **Modify** `_handle_terminal_payment` at `payment_worker_cog.py:149-166` so the `_notify_payment_result(payment)` call at `payment_worker_cog.py:155` is skipped when `str(payment.get('producer') or '').strip().lower() == 'admin_chat'`.
2. **Keep** `_dm_admin_payment_success` / `_dm_admin_payment_failure` / `_handoff_terminal_result` / cleanup calls unchanged — only the public notify step is suppressed, only for admin_chat.
3. **Do not** change behavior for any other producer or for non-confirmed statuses.

## Phase 3: admin_chat message collapsing

### Step 4: Introduce the single in-place status message (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** a private helper `_set_intent_status_message(intent, channel, content)` on `AdminChatCog` that:
   - If `intent.get('status_message_id')` is set, tries `channel.fetch_message(int(intent['status_message_id']))` and calls `message.edit(content=content, suppress=True)`; returns the message and the (unchanged) intent.
   - Else calls `channel.send(content, suppress_embeds=True)`, then calls `db_handler.update_admin_payment_intent(intent_id, {'status_message_id': message.id}, guild_id)` (tolerated by Step 2). If the update returns a row, use it; otherwise mutate `intent['status_message_id']` in place so the caller keeps a cached reference. Returns the message and the updated intent.
   - On `discord.NotFound` when fetching the stored id, clears the cached id and recurses once to send a fresh one.
   - Uses `suppress=True` / `suppress_embeds=True` so the Solana Explorer link never renders a card. Same pattern as `src/features/curating/curator.py:189-193`.
2. **Rewrite** `_start_admin_payment_flow` (`admin_chat_cog.py:380-430`): replace the fresh `channel.send("I've queued a small test payment…")` at `admin_chat_cog.py:427-430` with a call to `_set_intent_status_message(intent, channel, "Verifying wallet with a small test payment…")`. The status message is created here and the id persisted immediately.
3. **Rewrite** `handle_payment_result` (`admin_chat_cog.py:713-810`):
   - For `is_test && status == 'confirmed'` (currently `admin_chat_cog.py:734-774`): edit the existing status message to `"<@recipient> Test payment confirmed. Please reply here with `confirmed` once you see it in your wallet."` instead of sending a new `receipt_prompt_message`. Because the edit keeps the same discord message id, set `receipt_prompt_message_id = intent['status_message_id']` on the update payload so the stale-receipt sweep (`_sweep_stale_test_receipts` → `list_stale_test_receipt_intents` via `admin_chat_cog.py:832`) still has a marker.
   - For final `status == 'confirmed'` (currently `admin_chat_cog.py:776-800`): edit the status message to `"Payout sending…"` right before/after queuing, then after the db update to `completed`, send a Discord **reply** to the status message with:
     ```python
     await status_message.reply(
         f"<@{intent['recipient_user_id']}> Payment sent — {amount:.4f} SOL. {explorer_link}",
         mention_author=True,
         suppress_embeds=True,
     )
     ```
     Drop the multi-line "Payment sent." block at `admin_chat_cog.py:783-788`. Keep the admin DM branch at `admin_chat_cog.py:789-797` unchanged.
   - For failure / manual-review branches (`admin_chat_cog.py:802-809`): edit the status message to a terminal failure line and skip sending extra channel messages.
4. **Update** `_handle_recipient_test_receipt_confirmation` (`admin_chat_cog.py:680-711`): replace the `channel.send("<@…> confirmation received…")` at `admin_chat_cog.py:711` with `_set_intent_status_message(intent, channel, …)` using a line such as `"Confirmation received. Awaiting admin approval for the final payout."`.
5. **Keep** `_notify_intent_admin` / admin DMs untouched — only channel-visible messages change.

### Step 5: Fold admin-approval transitions into the same status message (`src/features/payments/payment_ui_cog.py`)
**Scope:** Medium
1. **Rewrite** `_post_admin_approval_thread_update` at `payment_ui_cog.py:550-565`:
   - Keep resolving the intent via `self._find_admin_intent_for_payment(payment.get('payment_id'))`.
   - If `intent.get('status_message_id')` is set, resolve the channel, `fetch_message` the status message, and `edit(content="Payout queued for sending.", suppress=True)`. Do **not** `channel.send(...)` a new message.
   - If the status message cannot be fetched (NotFound, HTTPException, no id), fall back to the current `channel.send(...)` behavior exactly as today so non-admin_chat edge cases and legacy rows still get a user-visible update.
   - Keep the existing `try/except` structure so failures here continue to log and return without raising.
2. **Amend** `_cleanup_admin_intent_messages` at `payment_ui_cog.py:567-584`:
   - Exclude `intent.get('status_message_id')` from the `message_ids` list passed to `safe_delete_messages`, so even if a legacy `prompt_message_id` or `receipt_prompt_message_id` equals the status message id (which happens in Step 4 when the test-receipt transition points receipt_prompt at the status message), the shared status message is preserved.
   - No other behavior changes — existing prompt cleanup for non-admin_chat flows is untouched.
3. **Do not** add any new cross-cog imports; the helper uses the intent dict fields already available.

## Phase 4: Tests

### Step 6: Update and extend tests (`tests/test_admin_payments.py`)
**Scope:** Medium
1. **Extend** `FakeChannel` at `tests/test_admin_payments.py:71-95`:
   - Make `send` return a message object that records the `suppress_embeds` kwarg and exposes an `edit(content=..., suppress=...)` coroutine that appends to an `edits` list and mutates its own `content`.
   - Give the returned object a `reply(content, **kwargs)` coroutine that appends to `channel.sent_messages` with a `reference` attribute pointing at the parent and records `mention_author` / `suppress_embeds` kwargs.
   - Add `fetch_message(message_id)` returning the recorded message or raising a stubbed `NotFound`.
2. **Extend** `FakeIntentDB` (around `test_admin_payments.py:298`): persist `status_message_id` on `update_admin_payment_intent`; add a toggle (e.g., `FakeIntentDB(reject_status_message_id=True)`) whose underlying `supabase` stub raises on payloads containing `status_message_id`, so we can exercise the Step 2 retry path directly against the real `DatabaseHandler.update_admin_payment_intent` wrapper.
3. **Update** existing tests broken by the flow change:
   - `test_handle_payment_result_test_confirmed` (`test_admin_payments.py:1466`): assert one `send` + one `edit`, that the edited content mentions the recipient and includes a "reply" prompt, and that `status_message_id` is persisted on the intent.
   - `test_handle_payment_result_final_confirmed_notifies_admin` (`test_admin_payments.py:2146`): assert exactly one `send` (the status message), at least one `edit`, and one `reply` whose content contains `<@42>` + `"Payment sent"`, whose `mention_author=True`, and whose `suppress_embeds=True`.
   - Any other test that asserts on the literal `"Payment sent."` card or the "confirmation received" literal.
4. **Add** new tests:
   - `test_handle_payment_result_suppresses_result_card_for_admin_chat`: instantiate `PaymentWorkerCog`, mock `_notify_payment_result`, feed a `producer='admin_chat'` confirmed payment through `_handle_terminal_payment`, assert `_notify_payment_result` was **not** awaited. Mirror the existing style at `test_admin_payments.py:2256-2285`.
   - `test_handle_payment_result_still_notifies_non_admin_chat_producers`: same thing with a non-admin_chat producer, assert `_notify_payment_result` **was** awaited — guards against the fix being overbroad.
   - `test_update_admin_payment_intent_tolerates_missing_status_message_column`: drive `DatabaseHandler.update_admin_payment_intent` with a Supabase stub that raises the first call when `status_message_id` is in the payload, assert the retried payload omits the key, the method returns the row, **and** that a stub raising for an unrelated column still results in `update_admin_payment_intent` returning `None` (contract preservation for FLAG-002).
   - `test_admin_chat_status_message_reused_across_transitions`: run the sequence start → test confirmed → receipt confirmed → final confirmed and assert only a single new message id ever appears in `channel.sent_messages`, with three `edit` calls and one `reply`.
   - `test_post_admin_approval_thread_update_edits_status_message`: fake intent with `status_message_id` set, call `PaymentUICog._post_admin_approval_thread_update`, assert the fake channel recorded an `edit` and **no** new `send`.
   - `test_cleanup_admin_intent_messages_preserves_status_message_id`: fake intent where `receipt_prompt_message_id == status_message_id`, assert `safe_delete_messages` is called without the shared id (stub `safe_delete_messages` or assert on the filtered list).
5. **Run** tests in order of cheapness:
   - `pytest tests/test_admin_payments.py -k "status_message or suppresses_result_card or tolerates_missing_status or reused_across_transitions or post_admin_approval_thread_update_edits or cleanup_admin_intent_messages_preserves" -x`
   - Then `pytest tests/test_admin_payments.py -x`
   - Finally `pytest -x` for the whole suite, to confirm no collateral breakage.

## Execution Order
1. Land Step 1 (sql) and Step 2 (db_handler retry) first — independently safe, unblock everything else.
2. Land Step 3 (worker-side suppression) next — small, isolated, and can ship without the admin_chat rewrite because `handle_payment_result` still runs.
3. Land Step 4 (admin_chat message flow) and Step 5 (payment_ui_cog edit-in-place + cleanup exclusion) together, since Step 4's assumption that the status message survives admin approval depends on Step 5.
4. Update tests (Step 6) alongside Steps 4 and 5 — do not merge without green tests.

## Validation Order
1. Run the focused new tests first (cheap, targeted).
2. Run the full `tests/test_admin_payments.py` module.
3. Run the full repo test suite last.
4. Manual smoke (info-only): on a staging deployment that has re-applied `sql/admin_payment_intents.sql`, run a real admin payout end-to-end and confirm a single channel message is edited in place across all transitions (including admin approval) and a single "Payment sent" reply appears with no Solana embed card.
