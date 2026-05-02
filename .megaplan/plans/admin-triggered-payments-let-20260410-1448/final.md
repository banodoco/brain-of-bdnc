# Execution Checklist

- [x] **T1:** Create `sql/admin_payment_intents.sql` — the persisted intent table with status state machine (`awaiting_wallet → awaiting_test → awaiting_confirmation → confirmed → completed | failed | cancelled`), message cursor fields (`prompt_message_id`, `last_scanned_message_id`, `resolved_by_message_id`), wallet/payment FK columns, `requested_amount_sol numeric(38,18) NOT NULL CHECK > 0`, `producer_ref`, `reason`, timestamps, updated_at trigger, RLS, and indexes: unique on `(guild_id, channel_id, recipient_user_id) WHERE status NOT IN terminal states`, index on `(guild_id, status)`, index on `(guild_id, channel_id, status)`. Follow the idempotent pattern from `sql/payments.sql`.
  Executor notes: Created an idempotent `admin_payment_intents` SQL file with the requested state machine, message cursor fields, wallet/payment foreign keys, updated_at trigger reuse, RLS, revoke, and all three required indexes.
  Files changed:
    - sql/admin_payment_intents.sql
  Reviewer verdict: Pass. The SQL file exists and contains the required state machine, cursor fields, RLS, revoke, and indexes.
  Evidence files:
    - sql/admin_payment_intents.sql

- [x] **T2:** Add intent CRUD methods to `src/common/db_handler.py` following the existing payment CRUD pattern (~line 1703+): `create_admin_payment_intent(record, guild_id)`, `get_admin_payment_intent(intent_id, guild_id)`, `get_active_intent_for_recipient(guild_id, channel_id, recipient_user_id)` (single active intent lookup for interceptor — WHERE status NOT IN terminal), `list_active_intents(guild_id)` (for startup recovery), `update_admin_payment_intent(intent_id, payload, guild_id)`. All use `_gate_check(guild_id)`, check `self.supabase`, return None/empty on failure.
  Depends on: T1
  Executor notes: Added the five admin-payment-intent CRUD methods to `DatabaseHandler` using the existing Supabase CRUD style. Verified each method checks `_gate_check(guild_id)`, guards on `self.supabase`, and that the active-intent lookup/list methods filter out terminal statuses.
  Files changed:
    - src/common/db_handler.py
  Reviewer verdict: Pass. The five CRUD helpers are present in `DatabaseHandler` with guild gating and safe empty returns.
  Evidence files:
    - src/common/db_handler.py

- [x] **T3:** Extend `PaymentService.request_payment` (src/features/payments/payment_service.py:32-51) to accept optional `amount_token: Optional[float] = None`. When `amount_token` is provided for non-test payments: validate > 0, set `normalized_amount_usd = None` and `token_price_usd = None`, bypassing USD conversion. The existing `amount_usd` path and test-payment path remain unchanged. The DB schema already supports nullable `amount_usd`/`token_price_usd`. Read the amount resolution block (lines 91-110) carefully and add the new branch between the `is_test` and `amount_usd` branches.
  Executor notes: Extended `PaymentService.request_payment` with an `amount_token` branch between the existing test and USD paths. The new branch validates `> 0`, sets `amount_usd` and `token_price_usd` to null, and leaves the existing test/USD behavior intact.
  Files changed:
    - src/features/payments/payment_service.py
  Reviewer verdict: Pass. `PaymentService.request_payment()` now supports direct token amounts without USD conversion.
  Evidence files:
    - src/features/payments/payment_service.py
    - tests/test_admin_payments.py

- [x] **T4:** Add `initiate_payment` tool definition to `TOOLS` list in `src/features/admin_chat/tools.py` (~line 577, after `cancel_payment`). Schema: `recipient_user_id` (string, required), `amount_sol` (number, required), `reason` (string, optional). No `wallet_address` param. Add `"initiate_payment"` to `ADMIN_ONLY_TOOLS` set (~line 886). Verify the assertion at ~line 916 (`ADMIN_ONLY_TOOLS | MEMBER_TOOLS == ALL_TOOL_NAMES`) still passes by running `python -c "from src.features.admin_chat import tools"`.
  Executor notes: Added the `initiate_payment` admin tool schema with exactly `recipient_user_id`, `amount_sol`, and optional `reason`, and classified it as admin-only. Verified the tool registry import/assertion succeeds.
  Files changed:
    - src/features/admin_chat/tools.py
  Reviewer verdict: Pass. `initiate_payment` is in the tool registry and classified as admin-only.
  Evidence files:
    - src/features/admin_chat/tools.py

- [x] **T5:** Inject `source_channel_id` into tool_input in `src/features/admin_chat/agent.py` (~line 409), alongside the existing `guild_id` injection. Read the existing guild_id injection pattern first and replicate it for `channel_context.get('channel_id')` → `tool_input['source_channel_id']` (int). Guard with `if 'source_channel_id' not in tool_input` to avoid overwriting.
  Executor notes: Injected `source_channel_id` from `channel_context['channel_id']` using the same guarded pattern as `guild_id`, without overwriting caller-supplied input.
  Files changed:
    - src/features/admin_chat/agent.py
  Reviewer verdict: Pass. `source_channel_id` injection is implemented next to the existing guild-context injection.
  Evidence files:
    - src/features/admin_chat/agent.py

- [x] **T6:** Implement `execute_initiate_payment(bot, db_handler, tool_input)` in `src/features/admin_chat/tools.py` and wire the dispatcher case in `execute_tool` (~line 2986). The function must: (1) validate recipient_user_id (int), amount_sol (> 0), guild_id, source_channel_id — fail closed on any missing/invalid; (2) check `get_active_intent_for_recipient` — return existing intent info if duplicate; (3) generate `producer_ref = f"{guild_id}_{recipient_user_id}_{int(time.time())}"`. Path A (wallet on file via `get_wallet`): create intent with `status='awaiting_test'`, call `cog._start_admin_payment_flow(channel, intent)`. Path B (no wallet): create intent with `status='awaiting_wallet'`, send channel ping `"<@{recipient_user_id}> — a payment of {amount_sol} SOL has been initiated for you. Please reply with your Solana wallet address."`, store `prompt_message_id`. Keep under ~80 lines.
  Depends on: T2, T3, T4, T5
  Executor notes: Implemented `execute_initiate_payment` and wired it into the admin-chat dispatcher. Verified fail-closed validation for missing/invalid guild/channel/user/amount and missing payment service, duplicate intent short-circuiting, verified-wallet handoff, and the no-wallet prompt path with `prompt_message_id` persistence.
  Files changed:
    - src/features/admin_chat/tools.py
  Reviewer verdict: Pass. The executor validates inputs, dedupes active intents, persists no-wallet prompts, and hands off wallet-on-file flows.
  Evidence files:
    - src/features/admin_chat/tools.py

- [x] **T7:** Add the reply interceptor and payment flow helpers to `src/features/admin_chat/admin_chat_cog.py`. This is the core of the feature. Read the file first to understand the `on_message` structure, then:

(A) Add `self._processing_intents: set = set()` and a standalone `self._classifier_client = AsyncAnthropic(api_key=...)` in `__init__` (solves FLAG-ADMIN-PAY-005 — do NOT depend on `_ensure_agent()`).

(B) Add `_check_pending_payment_reply(self, message)` called at the VERY TOP of `on_message`, before `_is_directed_at_bot()`. Fast exit if `message.author.bot` or `message.guild is None`. Deterministic identity gate: `get_active_intent_for_recipient(guild_id, channel_id, author_id)`. Race guard via `_processing_intents` set. Branch on status:
- `awaiting_wallet`: try `is_valid_solana_address(message.content.strip())` first (deterministic). If invalid, run lightweight LLM classification with `_classifier_client` (categories: wallet_provided, declined, ambiguous, suspicious). On valid wallet → `_handle_wallet_received`. On declined → cancel intent. On ambiguous/suspicious → fail closed, tag admin.
- `awaiting_confirmation`: LLM classification (positive_confirmation, declined, ambiguous, suspicious). On positive → `_handle_confirmation_received`. On declined → cancel. On ambiguous/suspicious → fail closed, tag admin.

(C) Add `_handle_wallet_received(self, message, intent, wallet_address)`: upsert_wallet → update intent to `awaiting_test` with `resolved_by_message_id` → call `_start_admin_payment_flow`.

(D) Add `_start_admin_payment_flow(self, channel, intent)`: resolve destinations via `server_config.resolve_payment_destinations(guild_id, channel.id, 'admin_chat')`. Call `payment_service.request_payment(producer='admin_chat', producer_ref=..., is_test=True, metadata={'intent_id': ...})`. Auto-confirm test: `confirm_payment(test_id, confirmed_by='auto', ...)`. Update intent `test_payment_id`. On failure: intent → failed, tag admin.

(E) Add `_handle_confirmation_received(self, message, intent)`: call `confirm_payment(final_payment_id, confirmed_by='free_text', confirmed_by_user_id=message.author.id)`. Update intent → confirmed, set resolved_by_message_id. On failure: tag admin.

Reference `src/features/grants/grants_cog.py:550-710` for the exact pattern.
  Depends on: T6
  Executor notes: Added the admin-payment reply interceptor at the top of `on_message`, plus the standalone classifier client, wallet/confirmation handlers, and test-payment starter flow. Verified the module compiles/imports, existing scheduler/admin-chat tests still pass, and a focused runtime harness confirmed interception order, classifier independence from `self.agent`, fail-closed ambiguous handling, admin tagging, and `_processing_intents` cleanup.
  Files changed:
    - src/features/admin_chat/admin_chat_cog.py
  Reviewer verdict: Pass. The interceptor, standalone classifier bootstrap, wallet handling, and test-payment starter helpers are implemented in `AdminChatCog`.
  Evidence files:
    - src/features/admin_chat/admin_chat_cog.py

- [x] **T8:** Add `handle_payment_result(self, payment)` to `AdminChatCog` in `admin_chat_cog.py`, mirroring `GrantsCog` structure (grants_cog.py:640-714). Guard: producer must be 'admin_chat'. Extract `intent_id` from `payment.metadata`. Fetch intent.

Test confirmed: create final payment via `request_payment(is_test=False, amount_token=requested_amount_sol)`. On success → update intent to `awaiting_confirmation`, set `final_payment_id`. Post PaymentConfirmView button via `PaymentCog.send_confirmation_request`. Post channel message prompting recipient for free-text confirmation. Update `prompt_message_id`, reset `last_scanned_message_id`.

Test failed: intent → failed, tag admin, do NOT create final payment.

Final confirmed: intent → completed. Post success message with amount and explorer link.

Final failed: intent → failed, tag admin.

IMPORTANT (FLAG-ADMIN-PAY-004 resolution): Both button and free-text paths converge here because `_handoff_terminal_result` fires for ALL terminal states. When the button confirms the payment, PaymentCog processes it to terminal, then `_handoff_terminal_result` calls this `handle_payment_result` which updates the intent row. No PaymentConfirmView changes needed.
  Depends on: T7
  Executor notes: Added `handle_payment_result` to `AdminChatCog` for admin-chat producer handoff. Verified test-payment failure aborts without creating a final payment, test-payment confirmation creates the final payment with `amount_token`, sends the shared confirmation prompt plus the in-channel free-text prompt, and final confirmed terminal results mark the intent completed and send a success message.
  Files changed:
    - src/features/admin_chat/admin_chat_cog.py
  Reviewer verdict: Pass. `handle_payment_result()` converges test/final outcomes and updates the intent state machine correctly.
  Evidence files:
    - src/features/admin_chat/admin_chat_cog.py
    - src/features/payments/payment_cog.py

- [x] **T9:** Add startup reconciliation to `AdminChatCog` in `admin_chat_cog.py`. Add `cog_load(self)` that calls `bot.wait_until_ready()` then `_reconcile_active_intents()`. The reconciler: queries `list_active_intents(guild_id)` for each enabled guild, then for each intent:
- `awaiting_wallet` / `awaiting_confirmation`: scan channel history from `last_scanned_message_id` (or `prompt_message_id`) forward, up to 200 messages. For each message from recipient, run through the same classification logic as the live interceptor. Update `last_scanned_message_id` cursor.
- `awaiting_test`: check test payment status via DB. If terminal and unhandled → call `handle_payment_result` manually.
- `confirmed`: check final payment status. If terminal → advance intent.
Rate-limit: max 200 messages per intent per pass. Log and skip if too large.
  Depends on: T8
  Executor notes: Added startup reconciliation to `AdminChatCog` via `cog_load()` and a one-pass active-intent reconciler. Verified `wait_until_ready()` runs before reconciliation, history replay is capped at 200 messages with `last_scanned_message_id` updates, missing channels are skipped without crashing, and terminal test/final payments are handed back through `handle_payment_result()` on startup.
  Files changed:
    - src/features/admin_chat/admin_chat_cog.py
  Reviewer verdict: Pass. Startup reconciliation scans active intents, replays missed replies, and re-hands off terminal payments.
  Evidence files:
    - src/features/admin_chat/admin_chat_cog.py

- [x] **T10:** Update system prompt in `src/features/admin_chat/agent.py` (~line 61, 'Doing things' section) to document `initiate_payment(recipient_user_id, amount_sol, reason?)` — amount is in SOL, pings user for wallet if not on file, must be called from guild channel.
  Executor notes: Updated the admin system prompt to document `initiate_payment(recipient_user_id, amount_sol, reason?)`, that the amount is in SOL, that missing wallets are requested in-channel, and that the tool must be used from a guild channel.
  Files changed:
    - src/features/admin_chat/agent.py
  Reviewer verdict: Pass. The admin system prompt documents the new payment initiation tool and guild-channel requirement.
  Evidence files:
    - src/features/admin_chat/agent.py

- [x] **T11:** Update existing test `tests/test_social_route_tools.py`: add `"initiate_payment"` to expected admin-only tools subset in `test_route_tools_are_admin_only` (~line 264). Add assertion `assert "initiate_payment" in admin_agent.SYSTEM_PROMPT` in `test_agent_prompt_mentions_payment_tools` (~line 413). Read the test file first to find exact locations.
  Depends on: T4, T10
  Executor notes: Updated the existing admin-chat tool test to expect `initiate_payment` in the admin-only tool set and to assert it is documented in the admin system prompt. Verified the full `tests/test_social_route_tools.py` module passes.
  Files changed:
    - tests/test_social_route_tools.py
  Reviewer verdict: Pass. Existing admin-chat tool tests were updated to include `initiate_payment` and its prompt documentation.
  Evidence files:
    - tests/test_social_route_tools.py

- [x] **T12:** Create `tests/test_admin_payments.py` with comprehensive tests. Must cover all 13 test cases from Step 13:
1. `test_initiate_payment_wallet_on_file` — wallet exists → intent created awaiting_test, test payment created with producer='admin_chat'
2. `test_initiate_payment_no_wallet` — no wallet → intent awaiting_wallet, channel ping sent
3. `test_initiate_payment_validation` — bad user ID, amount <= 0, missing guild/channel each return success=False
4. `test_initiate_payment_duplicate_intent` — existing active intent → no duplicate created
5. `test_wallet_reply_valid` — valid Solana address reply → upsert_wallet called, intent → awaiting_test
6. `test_wallet_reply_invalid_then_classified` — invalid address, agent says ambiguous → admin tagged
7. `test_wallet_reply_no_intent` — no active intent → returns False
8. `test_handle_payment_result_test_confirmed` — test confirmed → final payment created with amount_token, send_confirmation_request called
9. `test_handle_payment_result_test_failed` — test failed → no final payment, intent → failed
10. `test_confirmation_reply` — positive confirmation → confirm_payment called on final payment
11. `test_concurrent_intents_same_channel` — two recipients, correct intent matched
12. `test_request_payment_amount_token` — PaymentService amount_token parameter works
13. `test_startup_reconciliation` — active intent + channel history → intent advanced

Mock PaymentService, db_handler, bot, Discord channel/message objects. Use the existing test patterns from tests/test_scheduler.py and tests/test_social_route_tools.py.
  Depends on: T6, T7, T8, T9
  Executor notes: Created `tests/test_admin_payments.py` with all 13 requested cases covering tool initiation, wallet collection, free-text confirmation, terminal payment handoff, concurrent recipient gating, `amount_token`, and startup reconciliation. Verified the full new module passes and that the existing related test modules still pass unchanged.
  Files changed:
    - tests/test_admin_payments.py
  Reviewer verdict: Pass. The new admin payment test module contains the requested 13 cases and passes in review.
  Evidence files:
    - tests/test_admin_payments.py

- [x] **T13:** Run the full test suite and fix any failures. Execute in order: (1) `python -c "from src.features.admin_chat import tools"` — tool classification assertion. (2) `python -m pytest tests/test_admin_payments.py -x` — new tests. (3) `python -m pytest tests/test_social_route_tools.py tests/test_scheduler.py -x` — existing tests. (4) `python -m pytest tests/ -x` — full suite. If any test fails, read the error, fix the code, and re-run until all pass.
  Depends on: T11, T12
  Executor notes: Ran the required validation commands in order: tool import, new admin-payment tests, existing related modules, and the full `tests/` suite. All passed. Also ran a throwaway repro script for restart-safe free-text confirmation recovery and deleted it after confirming the fixed behavior.
  Reviewer verdict: Pass. The required validation commands were rerun successfully during review, including the full `tests` suite.
  Evidence files:
    - tests/test_admin_payments.py
    - tests/test_social_route_tools.py
    - tests/test_scheduler.py

## Watch Items

- FLAG-ADMIN-PAY-004: Button-intent sync — handle_payment_result MUST update intent to confirmed/completed on final-payment terminal states. This is what closes the gap: _handoff_terminal_result fires for ALL terminal states regardless of whether button or free-text was used. Verify this works by checking payment_cog.py:251-299.
- FLAG-ADMIN-PAY-005: Anthropic client bootstrap — the interceptor (_check_pending_payment_reply) fires BEFORE _ensure_agent(). Create a standalone AsyncAnthropic client in AdminChatCog.__init__ for classification, independent of AdminChatAgent. Do NOT call _ensure_agent() from the interceptor.
- DEBT admin-payments-intent-state: The button path does not directly update admin_payment_intents — it goes PaymentConfirmView → confirm_payment → PaymentCog processes to terminal → _handoff_terminal_result → handle_payment_result → updates intent. This indirect path is correct but must be verified end-to-end.
- Duplicate terminal messages: PaymentCog._handle_terminal_payment() posts a generic notify message BEFORE calling _handoff_terminal_result. AdminChatCog.handle_payment_result may post its own message. Ensure these go to different channels/threads or that the producer-specific message adds value beyond the generic one.
- The unique index on payment_requests (producer, producer_ref, is_test) WHERE status NOT IN ('failed','cancelled') means producer_ref must be unique per active payment. The timestamp-based ref strategy must not collide if the admin initiates two payments within the same second — consider using a UUID suffix if needed.
- Race condition: _processing_intents set prevents double-processing of the same intent reply. Ensure it's used with try/finally to always clean up.
- Startup recovery must be rate-limited: max 200 messages per intent per reconciliation pass. Don't scan channels with very old unresolved intents indefinitely.
- The classifier prompt must be concise and return structured output (JSON with category field). Use the same model as admin chat but with a very short system prompt focused only on classification.

## Sense Checks

- **SC1** (T1): Does the SQL file follow the idempotent pattern from payments.sql (CREATE TABLE IF NOT EXISTS, trigger function, RLS, revoke from anon/authenticated)? Are all three indexes present: unique active-intent per (guild, channel, recipient), status lookup, and interceptor lookup?
  Executor note: Confirmed the new SQL uses `create table if not exists`, reuses the shared updated_at trigger function, enables RLS, revokes anon/authenticated access, and includes the active-intent unique index plus the two guild/channel status lookup indexes.
  Verdict: Confirmed via direct SQL inspection.

- **SC2** (T2): Do all five CRUD methods use _gate_check(guild_id) and check self.supabase? Does get_active_intent_for_recipient filter by status NOT IN ('completed', 'failed', 'cancelled') and LIMIT 1?
  Executor note: Confirmed all five new intent methods gate on `_gate_check(guild_id)` and return `None` or `[]` if `self.supabase` is unavailable. `get_active_intent_for_recipient` applies `NOT IN ('completed', 'failed', 'cancelled')` and `.limit(1)`.
  Verdict: Confirmed via direct `db_handler.py` inspection.

- **SC3** (T3): Does the amount_token branch set normalized_amount_usd=None and token_price_usd=None? Is the existing is_test branch and amount_usd branch completely unchanged? Does the CHECK constraint on payment_requests allow null amount_usd for non-test payments?
  Executor note: Confirmed the new non-test `amount_token` branch sets `normalized_amount_usd=None` and `token_price_usd=None`, sits between the unchanged `is_test` and `amount_usd` branches, and the existing `payment_requests` check constraint already permits null `amount_usd` for non-test rows.
  Verdict: Confirmed via direct `payment_service.py` inspection and test rerun.

- **SC4** (T4): Does `python -c 'from src.features.admin_chat import tools'` succeed without assertion errors? Is initiate_payment in ADMIN_ONLY_TOOLS but NOT in MEMBER_TOOLS? Does the tool schema have exactly recipient_user_id, amount_sol, reason (no wallet_address)?
  Executor note: Verified `python -c "from src.features.admin_chat import tools"` exits cleanly, `initiate_payment` is only in `ADMIN_ONLY_TOOLS`, and the schema exposes only `recipient_user_id`, `amount_sol`, and optional `reason`.
  Verdict: Confirmed via tool import rerun and tool registry inspection.

- **SC5** (T5): Is source_channel_id injected as int from channel_context['channel_id']? Does the guard prevent overwriting if source_channel_id is already in tool_input? Does it match the existing guild_id injection pattern?
  Executor note: Confirmed `source_channel_id` is injected as `int(channel_context['channel_id'])`, uses an existence guard to avoid overwriting, and mirrors the adjacent `guild_id` injection pattern.
  Verdict: Confirmed via `agent.py` inspection.

- **SC6** (T6): Does the executor fail closed on: missing guild_id, missing source_channel_id, invalid recipient_user_id, amount_sol <= 0, missing payment_service? Does it return existing intent info on duplicate? Is the channel ping sent for no-wallet path with prompt_message_id stored?
  Executor note: Confirmed the executor fails closed for missing or invalid `guild_id`, `source_channel_id`, `recipient_user_id`, non-positive `amount_sol`, and missing `payment_service`; returns existing intent info on duplicate; and in the no-wallet path sends the channel prompt and persists `prompt_message_id`.
  Verdict: Confirmed via `execute_initiate_payment()` inspection and passing validation tests.

- **SC7** (T7): Does _check_pending_payment_reply fire BEFORE _is_directed_at_bot in on_message? Does the classifier use its own Anthropic client (not self.agent)? Does every non-positive classification path (ambiguous, suspicious) fail closed and tag admin? Is the _processing_intents guard used with try/finally?
  Executor note: Confirmed `_check_pending_payment_reply` now runs before `_is_directed_at_bot`, the classifier uses its own `AsyncAnthropic` client stored on `self._classifier_client`, ambiguous or suspicious replies fail closed with an admin tag, and `_processing_intents` is managed with `try/finally` so it always clears.
  Verdict: Confirmed via `AdminChatCog` inspection.

- **SC8** (T8): Does handle_payment_result update the intent row to 'confirmed' on final-confirmed and 'completed' after final payment succeeds? Does test-failed abort without creating final payment? Does test-confirmed create the final payment with amount_token (not amount_usd)?
  Executor note: Confirmed the test-payment terminal handler creates the final payment with `amount_token=float(intent['requested_amount_sol'])`, leaves `amount_usd` unused, updates the intent to `awaiting_confirmation` with `final_payment_id`, and test-payment failures stop the flow without creating a final payment. Confirmed final-payment terminal success updates the intent to `completed`; the earlier free-text confirmation path still sets `status='confirmed'` before terminal settlement.
  Verdict: Confirmed via `handle_payment_result()` inspection and passing tests.

- **SC9** (T9): Does cog_load call wait_until_ready() before reconciliation? Is channel history scanning limited to 200 messages? Are last_scanned_message_id cursors updated after scanning? Does it handle missing channels gracefully?
  Executor note: Confirmed `cog_load()` calls `wait_until_ready()` before `_reconcile_active_intents()`, history scans use a hard `limit=200`, `last_scanned_message_id` is persisted after replay, and missing channels are logged and skipped without crashing reconciliation.
  Verdict: Confirmed via reconciliation code inspection and passing reconciliation test.

- **SC10** (T10): Does the system prompt mention amount_sol (not amount_usd), that the tool pings users for wallet if not on file, and that it must be called from a guild channel?
  Executor note: Confirmed the system prompt now names `amount_sol`, says the tool pings the user for a wallet when none is on file, and says the action starts from a guild channel.
  Verdict: Confirmed via `SYSTEM_PROMPT` inspection.

- **SC11** (T11): Is 'initiate_payment' added to the expected admin-only tools subset? Is there an assertion that 'initiate_payment' appears in the system prompt?
  Executor note: Confirmed `initiate_payment` is in the expected admin-only subset and the existing prompt test now asserts that `initiate_payment` appears in `admin_agent.SYSTEM_PROMPT`.
  Verdict: Confirmed via `test_social_route_tools.py` inspection.

- **SC12** (T12): Are all 13 test cases present and do they test the correct assertions? Do mocks properly isolate DB, PaymentService, and Discord API? Do concurrent intent tests verify correct identity-gated matching?
  Executor note: Confirmed the new module contains all 13 requested test cases, uses isolated fake DB/payment/Discord/classifier objects, and includes a same-channel multi-recipient test that verifies identity-gated intent matching by recipient user ID before any semantic handling.
  Verdict: Confirmed via `tests/test_admin_payments.py` inspection and rerun.

- **SC13** (T13): Do all four validation commands pass: tool import, new tests, existing tests, full suite? Are there zero test failures?
  Executor note: Confirmed all four validation commands passed: tool import, new tests, existing related tests, and the full `tests/` suite. The additional restart-recovery repro script also passed, and there were zero test failures.
  Verdict: Confirmed via rerunning the import check, targeted tests, and full test suite.

## Meta

This is a large feature spanning 7+ files with a persisted state machine, async reply interception, LLM classification, and startup recovery. Key execution guidance:

**Biggest risk: the Anthropic client bootstrap.** The interceptor fires before _ensure_agent(), so self.agent may be None. The executor MUST create a standalone AsyncAnthropic client in AdminChatCog.__init__ for classification. Do NOT use self.agent.client or call _ensure_agent() from the interceptor — that would trigger full agent initialization on every non-bot message in a channel with an active intent.

**Button-intent sync is already solved by the existing architecture.** The gate disputed FLAG-ADMIN-PAY-004 but the resolution is correct: _handoff_terminal_result (payment_cog.py:269-299) fires for ALL terminal states, regardless of whether the button or free-text confirmed the payment. handle_payment_result will be called either way and can update the intent row. No PaymentConfirmView changes needed.

**Producer name 'admin_chat' auto-discovers AdminChatCog.** Verified: _candidate_producer_cog_names('admin_chat') generates ['AdminChatCog', ...]. Zero changes to PaymentCog needed for producer routing.

**Duplicate terminal messages concern.** PaymentCog posts a generic notify before _handoff_terminal_result. The producer-specific message from handle_payment_result should go to the same channel but add admin-payment-specific context (intent status, recipient ping). This is the same pattern GrantsCog uses — the grant-specific message goes to the grant thread while PaymentCog's generic goes to the notify channel. If resolve_payment_destinations returns the same channel for both, the executor should ensure the producer-specific message adds enough value to justify the duplication, or skip it when destinations overlap.

**T4-T5-T10 can run in parallel** with T1-T2-T3 since they touch different files. The executor should do Phase 1 (T1-T3) and the independent tool/prompt work (T4-T5-T10) concurrently, then T6 after both complete.

**Classification prompt design.** Keep it very short: system prompt defining the 5 categories with one-line descriptions, user message is the raw reply text plus brief context (what we asked for). Return JSON `{"category": "...", "extracted_address": "..."}`. Use the same model as admin chat. The classification is a single API call, not a chat turn.
