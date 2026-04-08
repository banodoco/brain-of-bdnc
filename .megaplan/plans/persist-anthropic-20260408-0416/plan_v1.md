
# Implementation Plan: Persist Anthropic Conversation Messages Across Turns in admin_chat Agent

## Overview

**Goal.** Replace the current "stringified recap" conversation memory in `src/features/admin_chat/agent.py` with real persistence of Anthropic `messages` objects (user text, assistant `tool_use` blocks, user `tool_result` blocks) so that on follow-up turns Claude sees the actual structured history instead of a lossy text summary.

**Current shape (`src/features/admin_chat/agent.py`).**
- Module-level `_conversations: Dict[int, List[Dict[str, Any]]]` stores per-user history (`agent.py:23`).
- `get_conversation` / `clear_conversation` / `_trim_conversation` manage it (`agent.py:178-195`).
- Each call to `chat()` builds a **fresh** local `messages = [{"role": "user", "content": full_message}]` (`agent.py:276`), then loops tool calls appending assistant/tool_result into that local list.
- History from prior turns is only used to synthesize a human-readable `PREVIOUS CONVERSATION:` string that's glued onto the new user message (`agent.py:266-274`).
- After the loop, history is updated with string-only recaps: `{"role": "user", "content": user_message}` plus a joined `assistant_parts` string summarizing tool calls and replies (`agent.py:434-453`). No raw `tool_use` / `tool_result` blocks are stored.
- `admin_chat_cog.py:401` calls `clear_conversation` — the only external surface.
- No tests cover this agent.

**Constraints.**
- Anthropic API requires that every `tool_use` block in an assistant turn be followed (in the next user turn) by a matching `tool_result` block before the next user text. Persisted history must preserve that pairing or the next API call will 400.
- The store is in-memory and per-process (already documented). Scope of this task is persistence *across turns within a live session*, not across restarts.
- Abort path mid-turn may leave an assistant `tool_use` without a matching `tool_result`. The existing code injects a synthetic `"Aborted by user"` tool_result for skipped calls (`agent.py:363-368`), which keeps pairing valid — we must preserve that discipline when persisting.
- Trim logic must never split a `tool_use` from its `tool_result`, or the next turn will crash.

**Why this matters.** Claude currently re-sees only a flat string like `[find_messages({...}) → 3 results]` instead of the real tool output, so follow-ups like "show me the second one" or "use that same channel" silently lose fidelity.

## Main Phase

### Step 1: Audit and confirm the insertion points (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Confirm** the store (`agent.py:23`), accessors (`agent.py:178-195`), history-injection (`agent.py:266-274`), and post-turn write (`agent.py:434-453`) are the only touchpoints for conversation state. Grep already confirms `_conversations` is private to this module and `clear_conversation` is the only externally called method (`admin_chat_cog.py:401`).
2. **Note** the abort path at `agent.py:361-370` already emits a synthetic `tool_result` for every skipped `tool_use`, so an aborted turn leaves `messages` in a valid tool_use↔tool_result paired state. The early `break` at `agent.py:425` happens *after* `messages.append(...)` at `agent.py:418-419`, so the pairing invariant holds.

### Step 2: Switch the per-turn `messages` seed to use persisted history (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Remove** the `PREVIOUS CONVERSATION:` string-history synthesis at `agent.py:266-274`. The `full_message` string should contain only the current turn's content (channel context + user message), as it did before history was appended.
2. **Seed** `messages` from the persisted list: replace `agent.py:276` with something equivalent to:
   ```python
   conversation = self.get_conversation(user_id)
   messages: List[Dict[str, Any]] = list(conversation)  # copy, don't alias
   messages.append({"role": "user", "content": full_message})
   ```
   The loop below already mutates `messages` in place; we just need it seeded from real history.
3. **Drop** the now-unused `max_history` computation if it's no longer referenced. Keep `ADMIN_MAX_CONVERSATION_LENGTH` / `MEMBER_MAX_CONVERSATION_LENGTH` — they still drive trimming.

### Step 3: Persist the real `messages` turn-state after the loop (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Replace** the string-recap writeback at `agent.py:434-453`. After the loop ends (normal, text-only stop, or abort), persist the full in-loop `messages` list back to the store:
   ```python
   _conversations[user_id] = messages
   ```
   This captures the new user turn, each assistant `tool_use`/text block (as `response.content`, which is already appended at `agent.py:418`), and each paired `tool_result` user message (`agent.py:419`).
2. **Handle the text-only stop case.** At `agent.py:346-351`, when Claude returns text with no tool calls, `response.content` is never appended to `messages`. Add an append of `{"role": "assistant", "content": response.content}` on that branch so the reply is part of persisted history. (Without this, the next turn's seed omits the assistant's last textual answer.)
3. **Handle exceptions.** In the `except` branches (`agent.py:460-466`), do **not** write partial turn state back — if the API call failed mid-turn, `messages` may contain an assistant `tool_use` with no paired `tool_result`. Leave `_conversations[user_id]` as it was at turn start. Simplest implementation: only write back on the success path (after the try body completes normally).
4. **Persist on abort** too — the synthetic-abort tool_result logic already keeps the list valid, so writing `messages` back is safe and desirable (future turns can see "user aborted the previous operation").

### Step 4: Make trimming structure-aware (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Rewrite** `_trim_conversation` (`agent.py:190-195`) to operate on the structured list. The current `len(conv) > max_length * 2` heuristic assumed strictly alternating text turns; with tool loops, a single user turn can produce many messages.
2. **Trim rule:** count only `{"role": "user", "content": <str>}` entries as "user turns" (these are real turn boundaries — tool_result user messages have list content, not str). Keep the most recent N user turns plus everything after the oldest kept user turn. Pseudocode:
   ```python
   def _trim_conversation(self, user_id, is_admin=True):
       conv = _conversations.get(user_id, [])
       max_turns = ADMIN_MAX_CONVERSATION_LENGTH if is_admin else MEMBER_MAX_CONVERSATION_LENGTH
       # Find indices of user-text messages (real turn starts)
       turn_starts = [
           i for i, m in enumerate(conv)
           if m.get("role") == "user" and isinstance(m.get("content"), str)
       ]
       if len(turn_starts) <= max_turns:
           return
       cut = turn_starts[-max_turns]
       _conversations[user_id] = conv[cut:]
   ```
   This guarantees we never cut between an assistant `tool_use` and its paired `tool_result`, because the cut always lands on a user-text message (the start of a new turn).
3. **Call** `_trim_conversation` after the writeback in Step 3, as the existing code already does.

### Step 5: Smoke-verify import and shape (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Import check:** `python -c "from src.features.admin_chat.agent import AdminChatAgent"` to catch syntax/reference errors from the edits.
2. **Manual shape check:** write a short throwaway script (or REPL session) that instantiates a fake agent state and exercises `_trim_conversation` on a hand-built list containing `user-text / assistant-tool_use / user-tool_result / user-text / assistant-text` to confirm trimming keeps pairs intact. No permanent test file needed unless the repo already has a test pattern for this module (grep confirms it doesn't).
3. **End-to-end sanity:** if the user is willing, run the bot locally and send two DMs in a row where the second references the first ("show me the second result"). Confirm the second call succeeds (no Anthropic 400) and that Claude actually uses the prior tool output.

## Execution Order
1. Step 1 (audit) — read-only confirmation.
2. Step 2 (seed `messages` from history) and Step 3 (write real history back) together — these two must land as one edit or the agent will be in an inconsistent state between edits.
3. Step 4 (structure-aware trim) — depends on Step 3's new shape.
4. Step 5 (verification).

## Validation Order
1. Python import / syntax check first (cheapest).
2. Hand-built trim-function check second (no runtime dependencies).
3. Live bot smoke test last (requires Discord + Anthropic credentials; manual, `info`-level).
