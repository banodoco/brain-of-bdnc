# Implementation Plan: Admin-chat follow-up cleanup pass

## Overview

Four narrow cleanups inside `src/features/admin_chat/` that close quality gaps the identity-routing megaplan explicitly deferred:

1. Wire the dead `self._classifier_client` into `_classify_confirmation` with a tool_use-enforced structured-output call; keep the existing keyword classifier as the fail-closed fallback.
2. Give the recipient a first-reply clarification message inside `_handle_test_receipt_ambiguous` instead of silently incrementing the counter.
3. Add a parallel recipient-directed message alongside the existing admin-tagged escalation in `_handle_test_receipt_negative`.
4. Delete the dead code (`MEMBER_SYSTEM_PROMPT`, the admin_chat cog's own `_can_user_message_bot` and `_is_directed_at_bot`, the member tool palette split, and the `is_admin` branching it fed) that the identity-routing plan deliberately retained.

**Scope fence (critical):** touch only `src/features/admin_chat/admin_chat_cog.py`, `src/features/admin_chat/agent.py`, `src/features/admin_chat/tools.py`, and their tests under `tests/`. Do not touch the payment subsystem, the state-machine transitions, the orphan sweep, `AdminApprovalView`, `upsert_wallet_for_user`, `_ADMIN_IDENTITY_INJECTED_TOOLS`, or the `requester_id` plumbing inside `tools.py`. Preserve fail-closed semantics (classifier exceptions → keyword fallback, not auto-positive).

**Sibling-file caveat (from gate flag FLAG-001):** `src/features/admin/admin_cog.py` (the unrelated `admin` feature, not `admin_chat`) defines its own live `_can_user_message_bot` at `src/features/admin/admin_cog.py:211` and calls it at `src/features/admin/admin_cog.py:363` for DM routing. That file is out of scope and must stay untouched. All grep verifications for `_can_user_message_bot` in this plan are scoped to `src/features/admin_chat/` plus `tests/`, NOT the full `src/` tree.

**Anchor points already mapped:**
- `admin_chat_cog.py:63-67` — `_classifier_model` + `_classifier_client` construction.
- `admin_chat_cog.py:97-143` — existing `_classify_confirmation` classmethod (keyword only).
- `admin_chat_cog.py:491-530` — `_handle_test_receipt_negative`.
- `admin_chat_cog.py:532-544` — `_handle_test_receipt_ambiguous`.
- `admin_chat_cog.py:870-897` — `_is_directed_at_bot` (admin_chat-local, dead).
- `admin_chat_cog.py:903-923` — `_can_user_message_bot` (admin_chat-local, dead; note the same-name method in `src/features/admin/admin_cog.py` is live and out of scope).
- `admin_chat_cog.py:1021-1031` — state-machine dispatch that calls the classifier and the ambiguous/negative handlers.
- `admin_chat_cog.py:1052-1053` — dead-code retention comment.
- `agent.py:166-207` — `MEMBER_SYSTEM_PROMPT` constant.
- `agent.py:210`, `agent.py:246-249`, `agent.py:279-522` — `MEMBER_MAX_CONVERSATION_LENGTH` and every `is_admin` branch in the agent.
- `tools.py:1033-1090` — `MEMBER_TOOLS` / `ADMIN_ONLY_TOOLS` / `get_tools_for_role`.
- `tests/test_admin_payments.py:573-709` and `1419-1537` — existing tests for classifier, router, ambiguous, and negative handlers.
- `tests/test_social_route_tools.py:293-320` and `857-873` — tests that reference `get_tools_for_role` / `ADMIN_ONLY_TOOLS`.
- `tests/conftest.py` (monkeypatches `ANTHROPIC_API_KEY`), and `tests/test_admin_payments.py:573-576` where `admin_chat_env` autouse fixture deletes the API key (keeps keyword path as default in tests).

## Main Phase

### Step 1: Audit current classifier + handlers (`src/features/admin_chat/admin_chat_cog.py`, `src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Confirm** `_classify_confirmation` is invoked only from `_handle_pending_recipient_message` (`admin_chat_cog.py:1022`) and from `tests/test_admin_payments.py:600,606-607`.
2. **Confirm** `self._classifier_client` is never read anywhere today (grep: only construction at line 65 and warning at line 67).
3. **Confirm** the admin_chat-local `_can_user_message_bot` (`admin_chat_cog.py:903`) and `_is_directed_at_bot` (`admin_chat_cog.py:870`) have no live readers outside the admin_chat package and the two test files already identified. Explicitly re-verify that the same-name method in `src/features/admin/admin_cog.py` is a different feature and will remain untouched.
4. **Confirm** `MEMBER_SYSTEM_PROMPT`, `MEMBER_TOOLS`, `ADMIN_ONLY_TOOLS`, `get_tools_for_role`, `MEMBER_MAX_CONVERSATION_LENGTH`, and the `is_admin` parameter on `AdminChatAgent.chat()` have no live readers outside the admin_chat package and the two test files already identified.

### Step 2: Split the keyword classifier out so tests still have a sync entry point (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Rename** the existing `@classmethod def _classify_confirmation` (lines 96-143) to `_classify_confirmation_keyword` with the same body, same classmethod decorator, and same return type `Literal['positive','negative','ambiguous']`.
2. **Update** the parametrized keyword tests at `tests/test_admin_payments.py:597-607` to call `cog._classify_confirmation_keyword(content)` so the deterministic keyword path stays directly testable without async/mocking.

### Step 3: Add the LLM-backed classifier instance method (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** a module-level constant next to the existing keyword tuples:
   ```python
   _CLASSIFIER_TOOL = {
       "name": "classify_payment_reply",
       "description": "Classify a recipient's reply to the bot's test-payment confirmation prompt.",
       "input_schema": {
           "type": "object",
           "properties": {
               "classification": {
                   "type": "string",
                   "enum": ["confirmed", "not_received", "unclear"],
               },
               "reply": {
                   "type": "string",
                   "description": "Optional clarification message for the recipient when classification is 'unclear'. Leave empty otherwise.",
               },
           },
           "required": ["classification"],
       },
   }
   _CLASSIFIER_SYSTEM_PROMPT = (
       "You classify a Discord user's reply to a payout bot that just said: "
       "'I sent you a small test payment — please reply confirmed or yes once "
       "you see the test SOL in your wallet, or not received if you do not see it.' "
       "Rules: return 'confirmed' ONLY if the user clearly indicates they can see "
       "the test SOL in their wallet. 'ok' or 'thanks' alone is NOT confirmed. "
       "'wait let me check', 'give me a sec', 'maybe' are 'unclear', NOT confirmed. "
       "Return 'not_received' only if the user clearly reports the payment is missing. "
       "Everything else is 'unclear'. When unclear, include a short plain-language "
       "'reply' asking the user to respond with 'confirmed' or 'not received'. "
       "You have no memory of prior turns — classify only the single reply you are given."
   )
   ```
2. **Add** an async instance method on `AdminChatCog`:
   ```python
   async def _classify_confirmation(
       self, content: str
   ) -> tuple[Literal['positive', 'negative', 'ambiguous'], Optional[str]]:
       if self._classifier_client is None:
           return self._classify_confirmation_keyword(content), None
       try:
           response = await self._classifier_client.messages.create(
               model=self._classifier_model,
               max_tokens=256,
               system=_CLASSIFIER_SYSTEM_PROMPT,
               tools=[_CLASSIFIER_TOOL],
               tool_choice={"type": "tool", "name": "classify_payment_reply"},
               messages=[{"role": "user", "content": content or ""}],
           )
       except Exception:
           logger.warning(
               "[AdminChat] LLM classifier failed, falling back to keywords",
               exc_info=True,
           )
           return self._classify_confirmation_keyword(content), None

       tool_input: Optional[Dict] = None
       for block in getattr(response, "content", []) or []:
           if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "classify_payment_reply":
               tool_input = getattr(block, "input", None) or {}
               break
       if not isinstance(tool_input, dict):
           return self._classify_confirmation_keyword(content), None

       classification_raw = str(tool_input.get("classification") or "").strip().lower()
       reply = tool_input.get("reply")
       reply_text = reply.strip() if isinstance(reply, str) and reply.strip() else None
       mapped = {
           "confirmed": "positive",
           "not_received": "negative",
           "unclear": "ambiguous",
       }.get(classification_raw)
       if mapped is None:
           return self._classify_confirmation_keyword(content), None
       return mapped, reply_text  # type: ignore[return-value]
   ```
3. **Keep** `self._classifier_model` at the existing string (`"claude-opus-4-6"`) — the brief permits Haiku but explicitly allows "current configured `self._classifier_model`", and swapping the model is out of scope for this cleanup.
4. **Import** `Optional`, `Tuple`, and `Dict` as needed from `typing` (already imported on line 9, extend the import).

### Step 4: Wire the new classifier into the state-machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Update** `_handle_pending_recipient_message` near line 1022 to:
   ```python
   classification, clarification_reply = await self._classify_confirmation(content)
   if classification == 'positive':
       await self._handle_test_receipt_positive(message, intent)
   elif classification == 'negative':
       await self._handle_test_receipt_negative(message, intent)
   else:
       await self._handle_test_receipt_ambiguous(
           message, intent, clarification_reply=clarification_reply
       )
   ```
   (Replace the `handler_name` dict dispatch — it can't forward the clarification kwarg through `getattr`.)

### Step 5: Fix the silent-on-ambiguous UX hole (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Change** `_handle_test_receipt_ambiguous` signature to `async def _handle_test_receipt_ambiguous(self, message, intent, *, clarification_reply: Optional[str] = None)`.
2. **After** the successful increment and **before** the `>= 2` escalation branch, if `int(updated_intent.get('ambiguous_reply_count') or 0) < 2`, post a recipient-facing clarification in-thread:
   ```python
   default_text = (
       f"<@{intent['recipient_user_id']}> I didn't quite understand — please "
       "reply with **confirmed** or **yes** once you see the test SOL in your "
       "wallet, or **not received** if you don't see it."
   )
   text = default_text
   if clarification_reply:
       text = f"<@{intent['recipient_user_id']}> {clarification_reply}"
   await message.channel.send(text)
   ```
3. **Leave** the existing `>= 2` → `_handle_test_receipt_negative` delegation untouched (do **not** forward `clarification_reply` into the negative path — negative is a separate flow).

### Step 6: Fix the negative-path recipient messaging gap (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **In** `_handle_test_receipt_negative`, after building the existing `detail` string and **before** `await message.channel.send(detail)` (line 514), send an additional recipient-directed message:
   ```python
   recipient_notice = (
       f"Hey <@{intent.get('recipient_user_id')}> — I've flagged this for "
       f"admin review. Please wait for {self._admin_mention} to follow up."
   )
   await message.channel.send(recipient_notice)
   ```
   Then keep the existing `await message.channel.send(detail)` and the existing `_notify_intent_admin` DM exactly as-is. Two channel sends, one admin DM, in that order.

### Step 7: Delete the dead code in the cog (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Delete** the admin_chat-local `_is_directed_at_bot` (lines 870-897) and `_can_user_message_bot` (lines 903-923) in full. Do NOT touch the same-name method in `src/features/admin/admin_cog.py` — that is a different feature and is out of scope.
2. **Delete** the `_message_access_cache` instance field initialization (line 59). The `_guild_context_cache` and `_ACCESS_CACHE_TTL_SECONDS` stay — they are used by `_resolve_context_guild_id`.
3. **Delete** the retention comment at lines 1052-1053 inside `_handle_admin_message`.
4. **Grep** `src/features/admin_chat/` and `tests/` for `_can_user_message_bot`, `_is_directed_at_bot`, `_message_access_cache`, and confirm zero hits. The only remaining `_can_user_message_bot` hits anywhere in the repo should be inside the out-of-scope `src/features/admin/admin_cog.py` file (expected and untouched).

### Step 8: Delete the dead code in the agent (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Delete** `MEMBER_SYSTEM_PROMPT` (lines 166-207) and `MEMBER_MAX_CONVERSATION_LENGTH` (line 210).
2. **Simplify** `_trim_conversation` (line 246): drop the `is_admin` parameter and always use `ADMIN_MAX_CONVERSATION_LENGTH`.
3. **Simplify** `AdminChatAgent.chat()` (line 279): drop the `is_admin: bool = True` parameter and every `if is_admin` branch at lines 349, 377-382, 390-391, 453, 472, 522. Always use `SYSTEM_PROMPT` (with the existing server-config override when present), always append `_POM_ADDENDUM`, always pass `available_tools = TOOLS` (from `tools.py`), keep `_ADMIN_IDENTITY_INJECTED_TOOLS` injection unchanged (the `if is_admin and ...` collapses to `if ...`), and always call `execute_tool` with `requester_id=None` (leave the keyword there; the `requester_id` plumbing inside `tools.py` is explicitly out of scope for deletion).
4. **Update** the import in `agent.py:15` from `from .tools import get_tools_for_role, execute_tool` to `from .tools import TOOLS, execute_tool`.
5. **Update** the call site in `admin_chat_cog.py:1144-1151` to drop `is_admin=True` from `self.agent.chat(...)`. Keep `requester_id=None` (plumbing stays).

### Step 9: Delete the dead code in tools (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Delete** `MEMBER_TOOLS` (line 1033), `ADMIN_ONLY_TOOLS` (line 1045), the two `assert` sanity checks (lines 1082-1083), and `get_tools_for_role` (lines 1086-1089).
2. **Keep** `ALL_TOOL_NAMES` by rewriting it as a single post-`TOOLS` derivation (`ALL_TOOL_NAMES = {tool["name"] for tool in TOOLS}`) if any other code references it. Run a `src/` + `tests/` grep first; delete unconditionally only if there are zero external references.
3. **Do not** touch `_ADMIN_IDENTITY_INJECTED_TOOLS`, `requester_id` branches in `execute_tool` (lines 3737-3767), or any query/batch/upsert tool implementations.

### Step 10: Update tests that reference the deleted symbols (`tests/test_social_route_tools.py`, `tests/test_admin_payments.py`)
**Scope:** Medium
1. **`tests/test_social_route_tools.py:293-320`** — rewrite `test_route_tools_are_admin_only` into `test_expected_admin_tools_are_registered`: build `registered = {tool["name"] for tool in admin_tools.TOOLS}` and assert the expected set is a subset. Drop the `member_tool_names` assertions — the member/admin split no longer exists.
2. **`tests/test_social_route_tools.py:857-873`** — rewrite `test_admin_tool_registration_and_identity_injection_sets` to assert the expected tools exist in `{t["name"] for t in admin_tools.TOOLS}` and keep the `_ADMIN_IDENTITY_INJECTED_TOOLS` and `_CHANNEL_POSTING_TOOLS` assertions untouched.
3. **`tests/test_admin_payments.py:697-709`** — delete `test_approved_member_no_longer_routed_to_agent` entirely. It's redundant with `test_identity_router_falls_through_for_non_admin_without_intent` at line 654 and its `_can_user_message_bot` AsyncMock patch references a deleted method.
4. **`tests/test_admin_payments.py:654-668`** — in `test_identity_router_falls_through_for_non_admin_without_intent`, remove the `cog._can_user_message_bot = AsyncMock(...)` line and the corresponding `.assert_not_awaited()` assertion. Keep the core fall-through assertions (`_handle_admin_message.assert_not_awaited`, `_handle_pending_recipient_message.assert_not_awaited`).
5. **`tests/test_admin_payments.py:1496-1537`** — update `test_handle_test_receipt_ambiguous_escalates_on_second_reply`: the first-reply branch now sends a clarification message, so change `assert channel.sent_messages == []` to assert exactly one message was sent containing `"<@42>"` and `"confirmed"` after the first call. The second-call assertions (status `manual_review`, "multiple ambiguous receipt replies") are unchanged.
6. **`tests/test_admin_payments.py:1420-1454`** — update `test_handle_test_receipt_negative_moves_to_manual_review`: assert two channel messages — the first containing `"<@42>"` and `"admin review"`, the second containing the existing `sig-123` / wallet detail. Admin DM assertion stays.
7. **Grep** `tests/` for `MEMBER_SYSTEM_PROMPT`, `MEMBER_MAX_CONVERSATION_LENGTH`, `get_tools_for_role`, `MEMBER_TOOLS`, `ADMIN_ONLY_TOOLS`, `_can_user_message_bot`, `_is_directed_at_bot` and confirm zero hits across `tests/`. (Under `src/`, only `src/features/admin/admin_cog.py` may still hit `_can_user_message_bot` — that file is out of scope.)

### Step 11: Add new tests for the classifier wiring, ambiguous clarification, negative recipient messaging, and dead-code removal (`tests/test_admin_payments.py`)
**Scope:** Medium
1. **Add** `test_classify_confirmation_llm_confirmed_maps_to_positive` (and parallel `_not_received_maps_to_negative`, `_unclear_maps_to_ambiguous_with_reply`):
   - `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` to override the autouse fixture.
   - Construct the cog, then replace `cog._classifier_client = AsyncMock()` and set `cog._classifier_client.messages.create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="classify_payment_reply", input={"classification": "confirmed"})]))`.
   - `result = await cog._classify_confirmation("ok i see it")`; assert `result == ("positive", None)` (and `("negative", None)` / `("ambiguous", "please reply with confirmed or not received")` for the others).
   - Verify `cog._classifier_client.messages.create.await_args.kwargs["tool_choice"] == {"type": "tool", "name": "classify_payment_reply"}` and that the single user message is the raw reply content.
2. **Add** `test_classify_confirmation_falls_back_to_keywords_when_api_key_missing` — rely on the autouse fixture (no API key), assert `await cog._classify_confirmation("confirmed, got it") == ("positive", None)` without any network mock.
3. **Add** `test_classify_confirmation_falls_back_to_keywords_on_api_error`:
   - Set `ANTHROPIC_API_KEY`, patch `cog._classifier_client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))`.
   - Assert `await cog._classify_confirmation("confirmed") == ("positive", None)` (keyword path) and that the mock was awaited once.
4. **Add** `test_handle_test_receipt_ambiguous_first_reply_posts_default_clarification`:
   - Seed an intent with `ambiguous_reply_count=0`, call `_handle_test_receipt_ambiguous(message, intent)`, assert `channel.sent_messages[-1].content` contains `"<@42>"` and `"confirmed"` and `"not received"`, and that `db.intents[...]["ambiguous_reply_count"] == 1`.
5. **Add** `test_handle_test_receipt_ambiguous_first_reply_uses_llm_reply_when_provided`:
   - Same fixture, call with `clarification_reply="Please say yes once you see 0.01 SOL"`, assert the sent message contains that exact string (minus leading mention).
6. **Add** `test_handle_test_receipt_negative_sends_recipient_facing_notice`:
   - Extend the existing setup, assert `len(channel.sent_messages) == 2`, the first contains `"<@42>"` and `"admin review"` and the configured admin mention, the second contains `sig-123` and `VALID_SOL_ADDRESS` (the existing admin-tag line), and the admin DM still arrives.
7. **Add** `test_dead_code_is_removed`:
   - `import src.features.admin_chat.agent as admin_agent` and `import src.features.admin_chat.tools as admin_tools`.
   - `assert not hasattr(admin_agent, "MEMBER_SYSTEM_PROMPT")`.
   - `assert not hasattr(admin_agent, "MEMBER_MAX_CONVERSATION_LENGTH")`.
   - `assert not hasattr(admin_tools, "MEMBER_TOOLS")`.
   - `assert not hasattr(admin_tools, "ADMIN_ONLY_TOOLS")`.
   - `assert not hasattr(admin_tools, "get_tools_for_role")`.
   - `assert not hasattr(AdminChatCog, "_can_user_message_bot")` (the admin_chat cog; the unrelated `src/features/admin/admin_cog.py:AdminCog` is not imported here and is out of scope).
   - `assert not hasattr(AdminChatCog, "_is_directed_at_bot")`.

### Step 12: Validate the change
**Scope:** Small
1. **Run** the targeted admin chat test file first: `pytest tests/test_admin_payments.py -x -q`. This catches the cog, classifier, and handler changes before the broader suite.
2. **Run** `pytest tests/test_social_route_tools.py -x -q` to confirm the rewritten tool-palette tests still pass.
3. **Run** the full suite: `pytest -x -q`. The baseline is ~189 tests; expect baseline +7 new tests and −1 deleted (`test_approved_member_no_longer_routed_to_agent`). Success is "no previously passing test regresses," not an exact count.
4. **Grep** once more for dangling references to the deleted symbols:
   - `MEMBER_SYSTEM_PROMPT`, `MEMBER_MAX_CONVERSATION_LENGTH`, `MEMBER_TOOLS`, `ADMIN_ONLY_TOOLS`, `get_tools_for_role`, `_is_directed_at_bot` — expect zero hits across `src/` and `tests/`.
   - `_can_user_message_bot` — expect zero hits across `src/features/admin_chat/` and `tests/`, and exactly the pre-existing live definition+call inside `src/features/admin/admin_cog.py` (the unrelated admin feature, out of scope and intentionally preserved).

## Execution Order
1. Steps 1-2: understand the current shape and carve out the keyword helper so existing tests have a stable target.
2. Steps 3-4: add the async LLM classifier and wire it into the state-machine dispatch.
3. Steps 5-6: patch the two recipient-facing UX gaps.
4. Steps 7-9: delete dead code across the cog, agent, and tools, in that order (cog first so `is_admin=True` kwarg removal at the call site lines up with the agent signature change).
5. Step 10: update existing tests that exercise the deleted symbols or the changed handler behavior.
6. Step 11: add the new tests covering every new behavior and the dead-code deletion.
7. Step 12: run targeted then full suite, then do the final scoped grep sweep.

## Validation Order
1. `pytest tests/test_admin_payments.py -x -q` (fastest signal on cog changes).
2. `pytest tests/test_social_route_tools.py -x -q` (catches the tools.py/agent.py palette cleanup).
3. `pytest -x -q` (full baseline sweep).
4. Final scoped grep for deleted symbols, per Step 12.4.
