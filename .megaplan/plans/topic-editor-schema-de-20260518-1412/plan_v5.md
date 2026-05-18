# Implementation Plan: Topic-Editor Schema De-conflation — Unify `post_topic` and Fix Multi-Section Bias

## Overview

The BNDC hourly live-update editor (`src/features/summarising/topic_editor.py`, ~4540 lines) exposes two write tools — `post_simple_topic` (text-only) and `post_sectioned_topic` (anything with media) — that conflate a *rendering* concern ("does media need to attach to a specific block?") with an *editorial* concern ("is this one beat or many beats?"). The system prompt and tool descriptions steer the model toward sectioned output for any media-bearing topic, producing three-section posts ("The Video" / "Audio" / "Community Reaction") for single-beat single-creator stories. A secondary steering surface — the auto-shortlist topic-summary builder at `topic_editor.py:2154-2160` — bakes the legacy tool name into `summary.suggested_actions`, which the model sees in `active_topics` payloads.

The fix introduces a unified `post_topic` tool whose only prose carrier is a `blocks` array — a single intro block IS "simple"; multiple blocks IS "sectioned" — and rewrites the topic-editor system prompt to give explicit brevity guidance and a worked example. Legacy tools stay accepted as deprecated aliases so in-flight model calls and stored topics continue to function. The publisher already renders blocks-only topics via `render_topic_publish_units` (topic_editor.py:4124), so no rendering changes are expected. The auto-shortlist suggested-actions string is updated to name `post_topic` so the model is never steered back to `post_sectioned_topic` from inside an `active_topics` payload.

**Constraints that matter:**
- Stored-topic backward compatibility via `normalize_document_blocks` (topic_editor.py:3957) is mandatory and untouched.
- Legacy tool names (`post_simple_topic`, `post_sectioned_topic`) must remain accepted at the model-call layer AND must keep their current structured rejection behavior on bad inputs (e.g. `post_simple_cannot_attach_media_use_post_sectioned_topic`).
- Legacy alias call STORAGE shape is preserved (body/sections/blocks combo) to avoid compatibility risk for any reader that inspects `summary.body` directly; only NEW `post_topic` calls store blocks-only.
- `post_topic`'s blocks-required contract is non-bypassable: a `post_topic` call must produce a non-empty `blocks` regardless of whether a stray `sections` field happens to be present in the args.
- The live feed is production-public on a cron — prompt language must be conservative.
- Out of scope: candidate-generator prompts (`live_update_prompts.py`), citation visual style, auto-shortlist *threshold* logic (we touch only the suggested_actions string, not the threshold), publisher CTAs, DB migrations.

## Main Phase

### Step 1: Audit the current write-tool surface and capture all touch points (`src/features/summarising/topic_editor.py`, `tests/test_topic_editor_runtime.py`)
**Scope:** Small
1. **Re-confirm** every site that branches on the tool names by grepping `post_simple_topic|post_sectioned_topic` across `src/` and `tests/`. Required hits in `topic_editor.py`:
   - `WRITE_TOOL_NAMES` set (`topic_editor.py:43-51`)
   - `TOPIC_EDITOR_SYSTEM_PROMPT` body (`topic_editor.py:54-143`)
   - `TOPIC_EDITOR_TOOLS` definitions (`topic_editor.py:219-295`)
   - Dispatch / validation branches: legacy-guard block at `1849-1850` (writeback), `1858-1890` (post_simple media + source-count guards), `1893-1905` (sectioned-requires-sections-or-blocks), `1916-1919` (rejected-action map), `2855` (publish), `3631-3647` (`_summary_for_tool`).
   - **Auto-shortlist suggested-actions string** at `topic_editor.py:2154-2160` — builds `summary.suggested_actions` with the literal `post_sectioned_topic` token. This is a model-visible steering surface because shortlisted-topic summaries are appended to `active_topics` and surfaced in the initial user payload to the topic-editor model. Must be updated.
   - **Critical supporting infrastructure:** `build_rejected_transition` allowed-action set at `topic_editor.py:3870` (currently `{rejected_post_simple, rejected_post_sectioned, rejected_watch}` — raises ValueError on anything else).
2. **Re-confirm** related tests by listing every test file that exercises these tools, the rejected-action helper, or the auto-shortlist suggested-actions string: `tests/test_topic_editor_runtime.py` (specifically `test_topic_editor_audit_action_vocabulary_excludes_invalid_rejected_actions` at `:1380-1437`, plus the post_simple media-rejection assertions around `:305-337`), `tests/test_topic_editor_core.py`, `tests/test_topic_editor_media_understanding.py`, `tests/test_backfill_live_update_topics.py`, `tests/test_live_update_editor_*`. Also grep tests for the literal string `'post_sectioned_topic'` inside any `suggested_actions`-related assertion. Don't modify yet — enumerate.
3. **Confirm** the publisher path `render_topic_publish_units` (`topic_editor.py:4124-4246`) handles a one-intro-block topic cleanly: a single intro block produces header + intro text + (optional) media units, with no orphan `**title**` section header (the intro branch at `4165-4216` skips the title line for `type == "intro"`).

### Step 2: Introduce the unified `post_topic` tool definition and deprecate the legacy two (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Add** a new tool entry `post_topic` in `TOPIC_EDITOR_TOOLS` (insert immediately before `post_simple_topic` at `topic_editor.py:219` so it advertises first). Schema:
   - Required: `proposed_key`, `headline`, `source_message_ids`, `blocks`.
   - **No top-level `body` field.** Prose lives in blocks only.
   - **No `sections` field.** The unified tool exposes `blocks` only; the legacy `sections` axis is not part of `post_topic`'s contract.
   - `blocks` items reuse the existing block schema (`type="intro"|"section"`, `title?`, `text` required, per-block `source_message_ids`, per-block `media_refs` with the existing canonical/shorthand shapes), and `blocks` itself MUST have `minItems: 1` in its JSON schema entry — this is the first line of defense against an empty-blocks call.
   - Optional: `parent_topic_id`, `notes`, `override_collisions`.
   - Description frames the choice as **editorial complexity (one beat vs. many)**: "One intro block = a single-beat story, even if it has media. Add section blocks only when the topic has distinct contributors, angles, or sub-stories that each independently merit a header."
2. **Update** the existing `post_simple_topic` (`topic_editor.py:219-239`) and `post_sectioned_topic` (`topic_editor.py:241-295`) tool descriptions to start with `"[DEPRECATED — use post_topic]"` and remove the "use this for ANY media" steering on `post_sectioned_topic`. Keep their schemas unchanged so old model calls still validate.
3. **Extend** `WRITE_TOOL_NAMES` (`topic_editor.py:43-51`) to include `"post_topic"`.

### Step 3: Extend dispatch with strict ordering (legacy guards FIRST, then unified shape) (`src/features/summarising/topic_editor.py`)
**Scope:** Medium

**Ordering rule (must be followed exactly):**
1. Tool-name acceptance + raw schema validation (including `blocks: minItems: 1` for `post_topic`).
2. **Legacy structural guards** (post_simple media check at `:1858-1880`, post_simple source-count check at `:1881-1890`) — unchanged; legacy alias calls keep their current structured rejection behavior (`post_simple_cannot_attach_media_use_post_sectioned_topic`, `post_simple_requires_single_author_and_one_or_two_sources`).
3. **Block normalization + default-media attachment** (the existing path that produces `normalized_blocks` and `_attach_default_media_refs_to_blocks`).
4. **Alias re-keying** — at this point, `post_simple_topic` and `post_sectioned_topic` are coerced into the unified shape ONLY in a local variable used for downstream validation/persistence; the original `call["name"]` is preserved for telemetry.
5. **Blocks-required guards — SEPARATE branches by tool name** (see Step 3.3 below).
6. Collision check + rejection.
7. Persistence via `_summary_for_tool`.

**Concrete edits:**
1. **Generalize the writeback at `topic_editor.py:1849-1850`** so it fires for `post_topic` as well as `post_sectioned_topic`:
   ```python
   if call["name"] in ("post_sectioned_topic", "post_topic"):
       args["blocks"] = normalized_blocks
   ```
   This ensures default-media-attached blocks make it into `args` for both the legacy sectioned path AND the new unified path. The legacy `post_simple_topic` alias path is intentionally excluded here because it has no `blocks` in its native schema; its alias normalization happens in step 3 below by synthesizing an intro block from `body` after the legacy guards have already run.
2. **Extend `action_by_tool`** (`topic_editor.py:1852-1856`) with `"post_topic": "post_topic"`. Legacy aliases keep their existing action labels (`post_simple`/`post_sectioned`) for telemetry continuity.
3. **Split the blocks-required guard into two separate branches** (replacing the prior plan's combined `or`). The `post_topic` branch checks `has_blocks` UNCONDITIONALLY — it does NOT consult `has_sections`, since the unified tool's contract has no `sections` field and a stray `sections` value in args must not satisfy a `blocks: []` call. At `topic_editor.py:1893-1905`:
   ```python
   if call["name"] == "post_topic" and not has_blocks:
       return self._reject_create_tool(
           call, context,
           action="rejected_post_topic",
           reason="post_topic_requires_blocks",
           ...,
       )
   if call["name"] == "post_sectioned_topic" and not has_sections and not has_blocks:
       return self._reject_create_tool(
           call, context,
           action="rejected_post_sectioned",
           reason="post_sectioned_requires_sections_or_blocks",
           ...,
       )
   ```
   This means: a `post_topic` call with `blocks: []` plus a stray legacy `sections` array is REJECTED with `post_topic_requires_blocks` (FLAG-004 closed). The schema-level `minItems: 1` from Step 2.1 is the first line; this dispatch-level guard is the redundant second line that also catches the case where schema validation is permissive (e.g. extra-properties leakage).
4. **Extend the rejected-action map** at `topic_editor.py:1915-1919` to include `"post_topic": "rejected_post_topic"`.
5. **Critical supporting-infrastructure change:** extend the allowed-action set in `build_rejected_transition` at `topic_editor.py:3870` to include `"rejected_post_topic"`:
   ```python
   if action not in {"rejected_post_simple", "rejected_post_sectioned", "rejected_post_topic", "rejected_watch"}:
   ```
   Without this, any `post_topic` rejection (collision, empty-blocks) raises ValueError instead of returning `tool_error`.

### Step 4: Add alias-to-unified-shape normalization for `_summary_for_tool` persistence (`src/features/summarising/topic_editor.py:3631-3647`)
**Scope:** Small

The handoff convention is explicit: `_summary_for_tool(tool_name, args)` is called with the ORIGINAL tool name (preserving telemetry-relevant identity) and the post-normalization `args` dict. The function selects the storage shape based on the original name:
1. **`post_topic`** (new): store `{"blocks": args["blocks"]}` only. No top-level `body`. No `sections`. Because Step 3.3 guarantees `args["blocks"]` is non-empty by the time this branch runs, `_summary_for_tool` does not need its own emptiness check.
2. **`post_sectioned_topic`** (legacy alias): keep the existing storage shape `{"body": args.get("body"), "sections": args.get("sections") or [], "blocks": args["blocks"] if args.get("blocks") else <omitted>}`. **Do not drop `body`** — preserve the current behavior at lines 3632-3641 to avoid changing how legacy alias rows look to downstream readers. `normalize_document_blocks` handles both shapes on read; this minimizes compatibility risk.
3. **`post_simple_topic`** (legacy alias): keep the existing `{"body": args.get("body"), "media": args.get("media") or []}` shape at line 3647. Unchanged.
4. **`watch_topic`**: unchanged.

The new branch ordering at `_summary_for_tool`:
```python
if tool_name == "post_topic":
    return {"blocks": args.get("blocks") or []}
if tool_name == "post_sectioned_topic":
    # unchanged from current behavior
    ...
```

### Step 5: Update the auto-shortlist suggested-actions string (`src/features/summarising/topic_editor.py:2154-2160`)
**Scope:** Small
1. **Locate** the auto-shortlist topic-summary builder at `topic_editor.py:2154-2160`. The current `summary.suggested_actions` string includes the literal `post_sectioned_topic` (the model-visible token telling the agent how to act on this shortlisted topic) plus `watch_topic`/update sources and `discard_topic`.
2. **Replace** `post_sectioned_topic` with `post_topic` in that string. The resulting string should read e.g. `"post_topic, watch_topic/update sources, or discard_topic"`. No mention of `post_sectioned_topic` here — the active-topics payload is a hot model-steering surface and we do not want the model nudged back toward the legacy tool from inside it.
3. **Do not** change the auto-shortlist threshold logic, the upstream `active_topics` payload assembly, or any other behavior in this area. The edit is a one-token string replacement plus any directly adjacent grammar.
4. **Grep** `tests/` for any test that asserts on the literal `'post_sectioned_topic'` inside a `suggested_actions` or auto-shortlist context and update those assertions to the new wording. Add a small regression test in Step 7 that pins the new string.

### Step 6: Rewrite `TOPIC_EDITOR_SYSTEM_PROMPT` for editorial brevity (`src/features/summarising/topic_editor.py:54-143`)
**Scope:** Medium
1. **Replace** the constant with a version that:
   - Renames the role from "BNDC live-update topic editor" to **"BNDC live-update writer"** (line 54).
   - Mentions only `post_topic` in the primary instructions; lists `post_simple_topic` and `post_sectioned_topic` once as **"deprecated — accepted for backward compatibility; prefer `post_topic`"**.
   - **Removes** the rule "use sectioned for ANY media" (lines 101-105). Replaces it with:
     > *"Use the minimum number of blocks that fits the story. A single creator dropping a single artifact = exactly ONE `intro` block with the media attached to it. Only add `section` blocks when the topic has genuinely distinct contributors, angles, or sub-stories that each independently merit their own header. If you find yourself splitting one creator's one video into 'The Video' / 'Audio' / 'Community Reaction' sections, you are wrong — collapse it to one block."*
   - **Adds** brevity guidance: intro block body = 1-3 sentences, ~30-150 words; section block body = 1-2 sentences; no bullet lists; no filler restatement of the title.
   - **Adds** a concrete worked example: a one-creator one-video drop with two praise replies → exactly one intro block with the video as a `media_ref`. Show the "don't do this" counter-example (the same input split into three sections) labeled as wrong.
   - **Keeps** the per-block source-attribution rule and the per-block media-attachment rule.
   - **Keeps** the canonical `media_ref` shape doc and the no-global-Sources-footer rule.
2. **Search** `tests/` for any test that greps for old prompt strings (e.g. `"use sectioned for ANY media"`, `"BNDC live-update topic editor"`) and update those assertions to match the new wording or delete them as obsolete.

### Step 7: Verify the publisher renders a one-block topic cleanly (`src/features/summarising/topic_editor.py:4087-4246`)
**Scope:** Small
1. **Trace** `_publish_topic` (`topic_editor.py:2861`) → `render_topic_publish_units` (`topic_editor.py:4124`). With a unified `post_topic` call producing one intro block:
   - `normalize_topic_document` returns `[{type: "intro", title: None, text, source_message_ids, media_refs}]`.
   - The function emits one text unit (`header + "\n\n" + block_content`) followed by one media unit per `media_ref`.
2. **Confirm** by reading the loop body at `topic_editor.py:4165-4216`.
3. **Do not modify the publisher** unless a test in Step 8 fails.

### Step 8: Tests (`tests/test_topic_editor_runtime.py`, `tests/test_topic_editor_core.py`)
**Scope:** Medium
1. **Add** to `tests/test_topic_editor_runtime.py`:
   - `test_post_topic_single_intro_block_with_media_accepted`: drives a `post_topic` call with one `intro` block containing one `media_ref`; asserts the persisted topic has `summary.blocks` of length 1, no `summary.body`, no `summary.sections`, and `render_topic_publish_units` produces `[text_unit, media_unit]`.
   - `test_post_topic_multi_block_accepted`: drives a `post_topic` call with one intro + two section blocks; asserts blocks length 3, per-block sources preserved.
   - `test_post_topic_rejects_empty_blocks_returns_tool_error`: empty `blocks` → returns a `tool_error` transition with action `rejected_post_topic` and reason `post_topic_requires_blocks`. **Verifies `build_rejected_transition` accepts `rejected_post_topic`** (would raise ValueError without Step 3.5).
   - **`test_post_topic_rejects_empty_blocks_even_with_stray_sections`**: drives a `post_topic` call with `blocks: []` AND a non-empty stray `sections` array in args. Asserts the call is rejected with action `rejected_post_topic`, reason `post_topic_requires_blocks` — i.e. the stray `sections` does NOT satisfy the contract (FLAG-004 regression pin). Test the dispatch-level guard directly; if schema-level `minItems` filters it first that is also fine, but the test asserts the final rejection shape either way.
   - `test_post_topic_collision_returns_tool_error`: a `post_topic` call that collides with an existing topic returns a `tool_error` transition with action `rejected_post_topic` (not a raised exception).
   - `test_legacy_post_simple_topic_still_normalizes_via_alias_and_keeps_existing_storage`: legacy `post_simple_topic` call (body + source_message_ids, no media) is accepted and stored with the existing `{body, media}` shape (no behavioral change for legacy alias storage).
   - `test_legacy_post_simple_topic_media_rejection_unchanged`: existing structured rejection `post_simple_cannot_attach_media_use_post_sectioned_topic` still fires for legacy media-bearing `post_simple_topic` calls (the legacy guard at `:1858-1880` runs BEFORE any normalization). This pins the test at `:305-337`.
   - `test_legacy_post_sectioned_topic_with_body_and_blocks_preserves_storage_shape`: existing `post_sectioned_topic` call with `body` + `blocks` is accepted; stored summary keeps `body`, `sections`, and `blocks` exactly as it does today (no behavioral change for legacy alias storage).
   - **`test_auto_shortlist_suggested_actions_uses_post_topic_not_post_sectioned_topic`**: drives the auto-shortlist topic-summary builder for a shortlisted media topic and asserts `summary.suggested_actions` contains the substring `"post_topic"` and does NOT contain `"post_sectioned_topic"` (FLAG-005 regression pin).
2. **Update** the existing action-coverage test `test_topic_editor_audit_action_vocabulary_excludes_invalid_rejected_actions` at `tests/test_topic_editor_runtime.py:1380-1437` to:
   - Add `"post_topic"` to both `allowed_actions` and `configured_actions`.
   - Add `"rejected_post_topic"` to `allowed_actions`.
   - Update the configured-actions branch to add `"post_topic"` when `tool["name"] == "post_topic"`.
   - Add an assertion that `build_rejected_transition(... action="rejected_post_topic" ...)` returns a payload with `action == "rejected_post_topic"` (mirroring the existing `rejected_watch` happy path).
3. **Add** to `tests/test_topic_editor_core.py`:
   - `test_normalize_document_blocks_still_handles_legacy_body_plus_sections`: explicit regression confirming `normalize_document_blocks` is unchanged.
4. **Add** an end-to-end-style normalization test mimicking the NebSH "Last Party" scenario: fixture with one author, one video-bearing source message, one reply, two praise comments, model call emits one intro block. Assert (a) accepted, (b) `summary.blocks` length 1, (c) publisher emits exactly one text unit + one media unit, (d) no `**...**` section header markup in the rendered text. Drive against the deterministic dispatch entry point — do NOT call the real Anthropic API.

### Step 9: Run targeted then broad tests (repo root)
**Scope:** Small
1. **Run** targeted suites first:
   ```bash
   pytest tests/test_topic_editor_core.py tests/test_topic_editor_runtime.py -x -q
   ```
2. **Run** the live-update and backfill suites:
   ```bash
   pytest tests/test_live_update_editor_publishing.py tests/test_live_update_editor_lifecycle.py tests/test_backfill_live_update_topics.py tests/test_topic_editor_media_understanding.py -x -q
   ```
3. **Run** the full suite once green:
   ```bash
   pytest -x -q
   ```

## Execution Order
1. Step 1 (audit) — confirm touch points without writing code; includes the new `:2154-2160` and `:3870` supporting-infrastructure entries.
2. Step 2 (tool surface) — declarative additions only, including `blocks: minItems: 1` on `post_topic`.
3. Step 3 (dispatch + supporting infrastructure) — code change including the split blocks-required guard (FLAG-004), the `build_rejected_transition` allowed-action extension, and the writeback generalization. Ordering rule (legacy guards FIRST, then normalization, then alias re-keying, then split blocks-required guards) is mandatory.
4. Step 4 (`_summary_for_tool` branch) — small persistence-shape addition.
5. Step 5 (auto-shortlist suggested-actions string) — one-token replacement at `:2154-2160` (FLAG-005).
6. Step 6 (system prompt rewrite) — independent of dispatch mechanics.
7. Step 7 (publisher verification) — read-only confirmation.
8. Step 8 (tests) — new tests + targeted update to the action-coverage test, including the two new regression pins for FLAG-004 and FLAG-005.
9. Step 9 (validation) — targeted suites first, then full repo.

## Validation Order
1. `tests/test_topic_editor_core.py` — pure unit tests on `normalize_document_blocks`. Cheapest; pins backward compat.
2. `tests/test_topic_editor_runtime.py` — dispatch + validation pipeline. Confirms unified tool, deprecated aliases, the legacy-guard ordering, the split blocks-required guards (no bypass via stray `sections`), the action-coverage update, that `build_rejected_transition` accepts `rejected_post_topic`, and the auto-shortlist suggested-actions string.
3. `tests/test_live_update_editor_*` and `tests/test_backfill_live_update_topics.py` — downstream consumers of stored topic summaries. Confirms publisher and lifecycle untouched.
4. Full `pytest -x -q` — catches any unrelated grep-based assertions on the old prompt string or the old suggested-actions string.
5. Manual smoke (info-only, cannot run in CI): on the next scheduled live-update cron tick in dev, eyeball that a single-author single-video topic posts as one paragraph + media, not three sections.
