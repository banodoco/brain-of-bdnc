
# Implementation Plan: Persist Anthropic Conversation Messages Across Turns in admin_chat Agent

## Overview

**Goal.** Replace the "stringified recap" conversation memory in `src/features/admin_chat/agent.py` with real persistence of Anthropic `messages` objects (user text, assistant `tool_use` blocks, user `tool_result` blocks) so follow-up turns see structured history instead of a lossy text summary. Two refinements from gate critique:
1. Persisted history must store **clean user text only**, not the request-scoped `full_message` blob (which carries fresh channel context + recent-message snapshots that go stale).
2. Persistence must enforce **both a turn count and a byte budget** so a single high-volume `find_messages`/`inspect_message` doesn't bloat the next API call.

**Current shape (`src/features/admin_chat/agent.py`).**
- Module-level `_conversations: Dict[int, List[Dict[str, Any]]]` (`agent.py:23`).
- Accessors `get_conversation` / `clear_conversation` / `_trim_conversation` (`agent.py:178-195`).
- `chat()` builds a fresh local `messages = [{"role": "user", "content": full_message}]` (`agent.py:276`); `full_message` = channel context + recent messages + user text, assembled at `agent.py:228-265`.
- Prior history is currently flattened into a `PREVIOUS CONVERSATION:` string and appended to `full_message` at `agent.py:266-274`.
- Post-turn writeback at `agent.py:434-453` stores only string recaps; raw `tool_use`/`tool_result` blocks are dropped.
- `admin_chat_cog.py:401` is the only external caller (calls `clear_conversation`).
- No tests cover this agent.
- Tool result payloads can be large: `find_messages` returns a preformatted summary plus a full results array (`src/features/admin_chat/tools.py:835-840`), and `inspect_message` returns context, replies, and media (`tools.py:972-981`).

**Constraints.**
- Anthropic requires every assistant `tool_use` block to be paired with a matching `tool_result` block in the next user message. Trimming and exception paths must never split a pair.
- The store is in-memory and per-process; this task does not change that.
- Abort path (`agent.py:361-370`) already injects synthetic `"Aborted by user"` tool_results for skipped calls, keeping pairing valid — preserve that.
- **Channel context is request-scoped.** It must drive only the current API call, never bleed into persisted state.
- **Memory budget must bound bytes, not just turn count**, because one search can dwarf twenty short turns.

## Main Phase

### Step 1: Audit the touchpoints (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Confirm** the four touchpoints: store (`agent.py:23`), accessors (`agent.py:178-195`), context+history injection (`agent.py:228-274`), post-turn writeback (`agent.py:434-453`). Grep confirms `_conversations` is module-private and `clear_conversation` is the only external surface (`admin_chat_cog.py:401`).
2. **Confirm** the abort path keeps `messages` valid: synthetic tool_result is appended for every skipped tool_use (`agent.py:363-368`), then `messages.append(...)` runs at `agent.py:418-419` *before* the abort `break` at `agent.py:425`.

### Step 2: Separate persisted-history seed from request-only context (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Remove** the `PREVIOUS CONVERSATION:` synthesis at `agent.py:266-274`.
2. **Split** the new user turn into two distinct values:
   - `persisted_user_msg = {"role": "user", "content": user_message}` — raw user text only, what we persist.
   - `request_user_msg = {"role": "user", "content": full_message}` — context-wrapped, what we send this turn.
3. **Seed** the local `messages` list from persisted history plus the request-only wrapper:
   ```python
   conversation = self.get_conversation(user_id)
   messages: List[Dict[str, Any]] = list(conversation)  # copy, don't alias
   messages.append(request_user_msg)
   ```
4. **After the loop**, when writing back to `_conversations` (Step 3), substitute `persisted_user_msg` in place of `request_user_msg` so channel context never enters the store. Concretely: build the persisted list as `list(conversation) + [persisted_user_msg] + messages[len(conversation)+1:]` — i.e. everything the loop appended after the seeded user turn, but with the user turn itself swapped for the clean version.
5. **Drop** the now-unused `max_history` computation. Keep `ADMIN_MAX_CONVERSATION_LENGTH` / `MEMBER_MAX_CONVERSATION_LENGTH` — they still drive count-based trimming.

### Step 3: Persist real `messages` turn-state on success (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Replace** the string-recap writeback at `agent.py:434-453` with the structured writeback described in Step 2.4.
2. **Handle the text-only stop branch** at `agent.py:346-351`: when Claude returns text with no tool calls, `response.content` is never appended to `messages`. Add `messages.append({"role": "assistant", "content": response.content})` on that branch so the assistant's reply is in persisted history.
3. **Skip writeback on exceptions.** In the `except` branches (`agent.py:460-466`), do **not** mutate `_conversations[user_id]`. If the API call failed mid-turn, `messages` may contain an unpaired `tool_use`. Leave the store at its pre-turn state. Implement by writing back only on the success path (after the try body completes normally).
4. **Persist on abort** — the synthetic-tool_result discipline keeps the list paired, so writing it back is safe and lets future turns know the user interrupted the previous operation.

### Step 4: Structure-aware trim with both turn-count and byte budget (`src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Rewrite** `_trim_conversation` (`agent.py:190-195`) to enforce two limits, evicting whole turn-groups from the front:
   - **Turn-count limit:** at most `ADMIN_MAX_CONVERSATION_LENGTH` (20) or `MEMBER_MAX_CONVERSATION_LENGTH` (10) user-text turns.
   - **Byte budget:** total serialized size of the persisted list must stay under a constant `MAX_CONVERSATION_BYTES` (start at **80_000** — well under Anthropic's per-request limit even when added to system prompt + tools schema, and roughly 20K tokens of headroom).
2. **Turn boundaries:** a "turn start" is `{"role": "user", "content": <str>}`. Tool-result user messages have list content, so they're never mistaken for boundaries. Cutting at a turn start guarantees no `tool_use`/`tool_result` pair is ever split.
3. **Algorithm:**
   ```python
   MAX_CONVERSATION_BYTES = 80_000

   def _trim_conversation(self, user_id, is_admin=True):
       conv = _conversations.get(user_id, [])
       max_turns = ADMIN_MAX_CONVERSATION_LENGTH if is_admin else MEMBER_MAX_CONVERSATION_LENGTH
       turn_starts = [
           i for i, m in enumerate(conv)
           if m.get("role") == "user" and isinstance(m.get("content"), str)
       ]
       if not turn_starts:
           return
       # 1) Count cap: keep last N user turns.
       if len(turn_starts) > max_turns:
           cut = turn_starts[-max_turns]
           conv = conv[cut:]
           turn_starts = [i - cut for i in turn_starts[-max_turns:]]
       # 2) Byte cap: drop oldest turn-groups until under budget.
       def _size(lst):
           return len(json.dumps(lst, default=str))
       while _size(conv) > MAX_CONVERSATION_BYTES and len(turn_starts) > 1:
           cut = turn_starts[1]  # drop everything before the second turn
           conv = conv[cut:]
           turn_starts = [i - cut for i in turn_starts[1:]]
       _conversations[user_id] = conv
   ```
   Notes: `json.dumps(..., default=str)` handles Anthropic SDK content blocks (which are pydantic objects) cheaply enough for a per-turn check; this is bounded work, not hot-path. If even one turn alone exceeds the budget, we stop at `len(turn_starts) > 1` and let it through — better to send one oversized turn than to leave an empty/invalid history.
4. **Call** `_trim_conversation` after writeback in Step 3, as the existing flow does.

### Step 5: Verify (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Import check:** `python -c "from src.features.admin_chat.agent import AdminChatAgent"`.
2. **Hand-built shape check:** in a REPL, build a synthetic `_conversations` entry containing `[user-text, assistant(tool_use), user(tool_result), user-text, assistant-text]` plus a fat synthetic `tool_result` (>80 KB) and call `_trim_conversation`. Confirm: (a) trimming never lands mid-pair, (b) the byte cap evicts oldest turn-groups, (c) a single oversize turn is preserved rather than zeroing the list.
3. **Live smoke test (manual, info):** run the bot, send two DMs in sequence where the second references the first ("show me the second result", "use that same channel"). Confirm no Anthropic 400 and that Claude actually uses prior tool output. Then send a third referencing channel context from turn 1 to verify stale channel context is **not** replayed.

## Execution Order
1. Step 1 (audit) — read-only.
2. Step 2 + Step 3 land together — seeding from persisted history and writing it back must be one atomic edit, with the persist/request user-message split in place from the start.
3. Step 4 (structure- and byte-aware trim) — depends on Step 3's new shape.
4. Step 5 (verification).

## Validation Order
1. Python import / syntax check.
2. REPL trim-function check on a synthetic oversized history.
3. Live two-turn DM smoke test (manual, `info`).
