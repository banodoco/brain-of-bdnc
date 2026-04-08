# Execution Checklist

- [x] **T1:** In src/features/admin_chat/agent.py, refactor chat() to split the user turn into persisted_user_msg (raw user_message text only) and request_user_msg (full_message with channel context). Remove the PREVIOUS CONVERSATION: string synthesis at agent.py:266-274. Seed local messages from list(self.get_conversation(user_id)) then append request_user_msg. Drop the now-unused max_history computation but keep ADMIN_MAX_CONVERSATION_LENGTH / MEMBER_MAX_CONVERSATION_LENGTH constants.
  Executor notes: Verified the chat() refactor now seeds request messages from persisted history plus a request-only user message, removes PREVIOUS CONVERSATION synthesis/max_history usage, keeps raw user text isolated in persisted_user_msg, and still imports cleanly.
  Files changed:
    - src/features/admin_chat/agent.py

- [x] **T2:** In src/features/admin_chat/agent.py, replace the string-recap writeback at agent.py:434-453 with a structured writeback that stores native Anthropic message dicts. Build the persisted list as list(conversation) + [persisted_user_msg] + messages[len(conversation)+1:] so channel context never enters the store. Add messages.append({'role':'assistant','content':response.content}) on the text-only stop branch at agent.py:346-351 so terminal assistant replies are persisted. Ensure writeback runs ONLY on the success path — exception branches at agent.py:460-466 must not mutate _conversations[user_id] (a partial turn may contain an unpaired tool_use). Abort path still writes back since synthetic tool_results keep pairing valid.
  Depends on: T1
  Executor notes: Verified the text-only stop branch appends the assistant response before break, structured writeback stores `persisted_user_msg` plus the real Anthropic turn payloads, and only the post-loop success or abort paths mutate `_conversations` while both exception branches return without writeback.
  Files changed:
    - src/features/admin_chat/agent.py

- [x] **T3:** In src/features/admin_chat/agent.py, rewrite _trim_conversation (currently agent.py:190-195) to enforce both a user-turn count cap (ADMIN_MAX_CONVERSATION_LENGTH=20 / MEMBER_MAX_CONVERSATION_LENGTH=10) and a byte budget MAX_CONVERSATION_BYTES=80_000 (add as a module constant). Identify turn starts as messages where role=='user' AND isinstance(content, str) so tool_result list-content user messages are never mistaken for boundaries. Cut only at turn starts to preserve every tool_use/tool_result pair. Algorithm: (1) if turn count exceeds cap, slice from turn_starts[-max_turns]; (2) while json.dumps(conv, default=str) > MAX_CONVERSATION_BYTES and len(turn_starts) > 1, drop everything before turn_starts[1]. If a single turn alone exceeds budget, keep it rather than emptying history. Ensure _trim_conversation is still called after writeback in the success path.
  Depends on: T2
  Executor notes: Verified `_trim_conversation` now detects turn starts only where `role == 'user'` and `content` is a string, applies the admin/member turn cap before byte-budget trimming, measures serialized history in bytes against `MAX_CONVERSATION_BYTES`, and still trims only at persisted user-turn boundaries so tool pairs remain intact; the success path still writes back and then calls `_trim_conversation`, and the import check passed.
  Files changed:
    - src/features/admin_chat/agent.py

- [ ] **T4:** Verify the changes. (a) Run `python -c 'from src.features.admin_chat.agent import AdminChatAgent'` to confirm import/syntax. (b) Write a short throwaway script that monkey-patches src.features.admin_chat.agent._conversations with a synthetic history containing [user-text, assistant(tool_use), user(tool_result), user-text, assistant-text] plus a fat synthetic tool_result >80KB, calls _trim_conversation, and asserts: (i) trimming never lands mid-pair (no orphan tool_use without matching tool_result), (ii) byte cap evicts oldest turn-groups, (iii) a single oversize turn is preserved rather than zeroing the list. Run it, confirm pass, then delete the script. (c) Run the project's existing test suite (pytest) on anything related to admin_chat — if no admin_chat tests exist, run the broader suite to ensure no import-time regressions. Fix any failures and re-run until clean.
  Depends on: T3

## Watch Items

- Anthropic API requires every assistant tool_use block to be paired with a matching tool_result in the next user message — trimming and exception paths must NEVER split a pair.
- persisted_user_msg must contain ONLY raw user_message text. If full_message (channel context, recent-message snapshots, DM headers) leaks into _conversations, stale context will be replayed on later turns (FLAG-001).
- Byte budget MAX_CONVERSATION_BYTES=80_000 must be enforced or a single heavy find_messages/inspect_message will bloat the next API call (FLAG-002).
- Turn boundary detection must check role=='user' AND isinstance(content, str) — tool_result user messages have list content and must NOT be mistaken for turn starts.
- On exception mid-turn, do NOT write back to _conversations[user_id]; messages may contain an unpaired tool_use. Roll back to pre-turn history.
- On the abort path, writeback IS safe because synthetic tool_results are appended for skipped tool_use blocks (agent.py:363-368) before the break.
- Text-only stop branch (agent.py:346-351): response.content is never appended to messages in the loop — must be appended explicitly before writeback or the assistant's reply is lost from history.
- clear_conversation contract must remain unchanged — admin_chat_cog.py:401 is the only external caller.
- json.dumps(..., default=str) is needed because Anthropic SDK content blocks are pydantic objects, not plain dicts.
- If a single turn alone exceeds MAX_CONVERSATION_BYTES, keep it (stop trimming when len(turn_starts) <= 1) — sending one oversized turn is better than emptying history.

## Sense Checks

- **SC1** (T1): Does the diff cleanly separate persisted_user_msg (raw user_message) from request_user_msg (full_message with channel context), and is the PREVIOUS CONVERSATION: string synthesis fully removed?
  Executor note: Grep confirmed PREVIOUS CONVERSATION is fully removed; visual diff review confirmed both message variables exist and only persisted_user_msg is used for the user-message writeback path.

- **SC2** (T2): Does writeback substitute persisted_user_msg for request_user_msg in the stored list, append the assistant response on the text-only stop branch, and skip writeback entirely on exception paths while still writing on the abort path?
  Executor note: Traced success-with-tools, success-text-only, abort, and exception exits: the persisted list swaps in `persisted_user_msg`, the text-only branch appends `response.content` before break, abort still reaches writeback after synthetic `tool_result`s, and both exception handlers return before any `_conversations` mutation.

- **SC3** (T3): Does _trim_conversation enforce both turn-count and byte caps, only cut at user-text turn boundaries (preserving tool_use/tool_result pairing), and preserve a single oversized turn rather than emptying history?
  Executor note: Confirmed the while-loop guard remains `len(turn_starts) > 1`, so a single oversized turn is preserved, and every trim step still cuts only from one persisted user-text boundary to the next.

- **SC4** (T4): Did the import check pass, did the synthetic-history trim script confirm pairing/byte/oversize invariants, and does the existing test suite still pass?
  Executor note: Run import check first (cheap), then the throwaway script, then pytest. Delete the throwaway script after it passes.

## Meta

Steps 2 and 3 of the plan must land as a single atomic edit because the new persist/request user-message split, structured writeback, text-only-branch append, and exception-path rollback all interlock \u2014 partial edits will leave the file in a broken state. Apply T1+T2 together if it's easier. The trim rewrite (T3) depends on the new structured shape so do it after. Key gotcha: when computing the persisted list as `list(conversation) + [persisted_user_msg] + messages[len(conversation)+1:]`, double-check the slice index \u2014 `len(conversation)` was the seed length and `+1` skips the request_user_msg that was appended in T1; everything after that is loop-appended assistant/tool_result content which should carry over verbatim. The abort path must still write back: synthetic tool_results at agent.py:363-368 keep the list API-valid, and persisting it lets the next turn know the user interrupted. Do not introduce a try/finally that writes back unconditionally \u2014 that would defeat the exception rollback. For T4's REPL check, the simplest path is to call _trim_conversation as a method on a minimally-constructed AdminChatAgent or invoke it directly on the module-level _conversations dict.

## Coverage Gaps

- Tasks without executor updates: 1
