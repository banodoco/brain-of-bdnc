# Implementation Plan: Clean up admin_chat payment message noise

## Overview

Today each admin_chat payout produces a stream of disconnected channel messages: a "queued test" note, a "reply confirmed when you see it" prompt, a "confirmation received, payout queued" note, a fat multi-line "Payment sent" card with an auto-embedded Solana Explorer link, and — from `PaymentWorkerCog._notify_payment_result` — a second generic "Payment <Status>" result card written directly from the shared worker. Together they spam the channel and duplicate information the admin_chat flow already renders better.

Goals:
1. Collapse all intermediate status updates for a single payout into **one status message**, edited in place, owned by the intent row via a new `status_message_id` column.
2. When the final payout confirms, finish by **replying** to the status message with a short "Payment sent." that tags the recipient, and suppress the explorer-link embed so we don't re-render the Solana card.
3. **Suppress the generic `PaymentService` result card for `producer='admin_chat'` only** — other producers keep today's behavior.
4. Persist the message id via a new `sql/` migration that is idempotent (`add column if not exists`), and have the runtime **tolerate the column being absent** on older deployments (so we don't crash if the `sql/` file hasn't been re-applied yet).
5. Update `tests/test_admin_payments.py` accordingly.

Key touch points identified during exploration:
- `src/features/admin_chat/admin_chat_cog.py`
  - `_start_admin_payment_flow` — `admin_chat_cog.py:380-430` (creates the test payment, sends "I've queued a small test payment" message)
  - `_handle_recipient_test_receipt_confirmation` — `admin_chat_cog.py:680-711` (sends "confirmation received. The payout has been queued.")
  - `handle_payment_result` — `admin_chat_cog.py:713-810` (emits receipt prompt + "Payment sent." card + admin DMs)
- `src/features/payments/payment_worker_cog.py`
  - `_handle_terminal_payment` — `payment_worker_cog.py:149-166` calls `_notify_payment_result` for every confirmed payment
  - `_notify_payment_result` / `_build_result_message` — `payment_worker_cog.py:402-478`
- `src/common/db_handler.py`
  - `update_admin_payment_intent` — `db_handler.py:2143-2161` (passes a raw dict straight to Supabase; will 4xx if the column doesn't exist)
- `sql/admin_payment_intents.sql` — existing schema at `sql/admin_payment_intents.sql:1-97`, with the `add column if not exists` pattern already in use (`sql/admin_payment_intents.sql:50-57`).
- `tests/test_admin_payments.py` — `FakeChannel`, `FakeIntentDB`, `FakeBot` around `test_admin_payments.py:71-298`; existing result-flow tests at `test_admin_payments.py:1466`, `:2079`, `:2146`.

## Phase 1: Schema and storage

### Step 1: Add `status_message_id` column (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Append** an `add column if not exists status_message_id bigint;` block to `sql/admin_payment_intents.sql` immediately after the existing additive migrations at `sql/admin_payment_intents.sql:57`, matching the surrounding idempotent style.
2. **Do not** touch indexes, constraints, or the status enum — the column is plain metadata.

### Step 2: Tolerate the column being absent in updates (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** a narrow fallback path inside `update_admin_payment_intent` (`db_handler.py:2143`) so that when the `status_message_id` key is present in the payload and the Supabase call raises an `undefined_column`/`PGRST204`-style error (match on `'status_message_id'` being in the exception string to keep the check strict), the helper logs once at `info` and retries the update with `status_message_id` removed from the payload. Do not broaden the fallback to arbitrary columns.
2. **Return** the retry result unchanged so the rest of the flow sees a normal row back. Do not swallow unrelated errors.

    ```python
    payload = self._serialize_supabase_value(dict(record))
    try:
        result = self.supabase.table('admin_payment_intents').update(payload)...execute()
    except Exception as exc:
        if 'status_message_id' in payload and 'status_message_id' in str(exc):
            payload.pop('status_message_id', None)
            result = self.supabase.table('admin_payment_intents').update(payload)...execute()
        else:
            raise
    ```

## Phase 2: Worker-side suppression

### Step 3: Skip the generic result card for admin_chat (`src/features/payments/payment_worker_cog.py`)
**Scope:** Small
1. **Modify** `_handle_terminal_payment` at `payment_worker_cog.py:149-166` so the `_notify_payment_result(payment)` call at `payment_worker_cog.py:155` is skipped when `str(payment.get('producer') or '').strip().lower() == 'admin_chat'`.
2. **Keep** `_dm_admin_payment_success` / `_dm_admin_payment_failure` / `_handoff_terminal_result` / cleanup calls unchanged — only the public notify step is suppressed, only for admin_chat, and only for this one producer.
3. **Do not** change behavior for any other producer or for non-confirmed statuses.

## Phase 3: admin_chat message collapsing

### Step 4: Introduce a single in-place status message (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** a private helper `_set_intent_status_message(intent, channel, content)` on `AdminChatCog` that:
   - If `intent.get('status_message_id')` is set, tries `channel.fetch_message(int(intent['status_message_id']))` and calls `message.edit(content=content, suppress=True)`; returns the message.
   - Else calls `channel.send(content, suppress_embeds=True)`, then calls `update_admin_payment_intent(intent_id, {'status_message_id': message.id}, guild_id)` (tolerated by Step 2) and updates the cached intent dict in place; returns the message.
   - On `discord.NotFound` when fetching the stored id, clears the id and recurses once to send a fresh one.
   - Uses `suppress=True` / `suppress_embeds=True` so the Solana Explorer link never renders a card. Pattern already used at `src/features/curating/curator.py:189-193`.
2. **Rewrite** `_start_admin_payment_flow` (`admin_chat_cog.py:380-430`): replace the fresh `channel.send("I've queued a small test payment…")` at `admin_chat_cog.py:427-430` with a call to `_set_intent_status_message(intent, channel, "Verifying wallet with a small test payment…")`. The status message is created here and the id persisted immediately.
3. **Rewrite** `handle_payment_result` (`admin_chat_cog.py:713-810`):
   - For `is_test && status == 'confirmed'` (currently `admin_chat_cog.py:734-774`): edit the existing status message to `"Test payment confirmed. Awaiting your reply to confirm receipt."` instead of sending a new `receipt_prompt_message`. Keep `receipt_prompt_message_id` pointed at the edited status message's id so stale-sweep logic (`_sweep_stale_test_receipts`) still works with a single source of truth. Retag the recipient inside the edited content so the notification still fires.
   - For final `status == 'confirmed'` (currently `admin_chat_cog.py:776-800`): edit the status message to `"Payout sending…"` right before queuing, then after the db update to `completed`, send a Discord **reply** to the status message with:
     ```python
     await status_message.reply(
         f"<@{intent['recipient_user_id']}> Payment sent — {amount:.4f} SOL. {explorer_link}",
         mention_author=True,
         suppress_embeds=True,
     )
     ```
     Drop the multi-line "Payment sent." block at `admin_chat_cog.py:783-788`. Keep the admin DM branch at `admin_chat_cog.py:789-797` unchanged.
   - For failure / manual-review branches (`admin_chat_cog.py:802-809`): edit the status message to a terminal failure line and skip sending extra channel messages.
4. **Update** `_handle_recipient_test_receipt_confirmation` (`admin_chat_cog.py:680-711`): replace the `channel.send("<@…> confirmation received…")` at `admin_chat_cog.py:711` with `_set_intent_status_message(intent, channel, …)` using an updated line such as `"Confirmation received. Payout queued."`.
5. **Keep** `_notify_intent_admin` / admin DMs untouched — only channel-visible messages change.
6. **Do not** introduce a fallback that re-sends a fresh status message when `suppress=True` fails; let the exception bubble so we notice at test time.

## Phase 4: Tests

### Step 5: Update and extend tests (`tests/test_admin_payments.py`)
**Scope:** Medium
1. **Extend** `FakeChannel` at `tests/test_admin_payments.py:71-95`:
   - Make `send` return a message object that records the `suppress_embeds` kwarg and exposes an `edit(content=..., suppress=...)` coroutine that appends to an `edits` list and mutates its own `content`.
   - Give the returned object a `reply(content, **kwargs)` coroutine that appends to `channel.sent_messages` with a `reference` attribute pointing at the parent and records `mention_author` / `suppress_embeds` kwargs.
   - Add `fetch_message(message_id)` returning the recorded message or raising a stubbed `NotFound`.
2. **Extend** `FakeIntentDB` (around `test_admin_payments.py:298`): allow `update_admin_payment_intent` to persist a `status_message_id` field; add a toggle (e.g., `FakeIntentDB(reject_status_message_id=True)`) that raises on payloads containing `status_message_id`, so we can test the Step 2 fallback.
3. **Update** existing tests broken by the flow change:
   - `test_handle_payment_result_test_confirmed` (`test_admin_payments.py:1466`): assert the channel received one `send` + one `edit`, that the final text contains `"reply confirmed"` (or whatever Step 4 settles on) and that `status_message_id` is persisted.
   - `test_handle_payment_result_final_confirmed_notifies_admin` (`test_admin_payments.py:2146`): assert the channel saw exactly one `send` (the status message), at least one `edit`, and one `reply` whose content contains `<@42>` and `"Payment sent"`, and whose `suppress_embeds=True`.
   - Any other test that asserts on the literal `"Payment sent."` card or the "confirmation received" literal.
4. **Add** new tests:
   - `test_handle_payment_result_suppresses_result_card_for_admin_chat`: instantiate `PaymentWorkerCog`, mock `_notify_payment_result`, feed a `producer='admin_chat'` confirmed payment through `_handle_terminal_payment`, assert `_notify_payment_result` was **not** awaited. Mirror the existing style at `test_admin_payments.py:2256-2285`.
   - `test_handle_payment_result_still_notifies_other_producers`: same thing with `producer='bounty'` (or any non-admin_chat string), assert `_notify_payment_result` **was** awaited — guards against the fix being overbroad.
   - `test_update_admin_payment_intent_tolerates_missing_status_message_column`: drive `db_handler.update_admin_payment_intent` with the `reject_status_message_id=True` fake, assert the retried payload omits the key and the update succeeds.
   - `test_admin_chat_status_message_reused_across_transitions`: run the full sequence (start → test confirmed → receipt confirmed → final confirmed) and assert only a single new message id ever appears, plus one final reply.
5. **Run** tests in order of cheapness:
   - `pytest tests/test_admin_payments.py -k "status_message or suppresses_result_card or tolerates_missing_status or reused_across_transitions" -x`
   - Then `pytest tests/test_admin_payments.py -x`
   - Finally `pytest -x` for the whole suite, to confirm no collateral breakage in other payment tests.

## Execution Order
1. Land Step 1 (sql) and Step 2 (db_handler fallback) first — these are independently safe and unblock everything else.
2. Land Step 3 (worker-side suppression) next — small, isolated, and can ship without Step 4 because `handle_payment_result` still runs.
3. Land Step 4 (admin_chat message flow) once Steps 1–3 are in place, since it depends on the new column and on the worker no longer double-posting.
4. Update tests (Step 5) alongside Step 4 — do not merge Step 4 without green tests.

## Validation Order
1. Run the four new focused tests first (cheap, targeted).
2. Run the full `tests/test_admin_payments.py` module.
3. Run the full repo test suite last.
4. Manual smoke (info-only): on a staging deployment that has re-applied `sql/admin_payment_intents.sql`, run a real admin payout end-to-end and confirm a single channel message is edited in place and a single "Payment sent" reply appears with no Solana embed card.
