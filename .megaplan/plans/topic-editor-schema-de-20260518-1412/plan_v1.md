# Implementation Plan: Topic-Editor Schema De-conflation — Unify `post_topic` and Fix Multi-Section Bias

## Overview

The BNDC hourly live-update editor (`src/features/summarising/topic_editor.py`, ~4540 lines) currently exposes two write tools — `post_simple_topic` (text-only) and `post_sectioned_topic` (anything with media) — that conflate a *rendering* concern ("does media need to attach to a specific block?") with an *editorial* concern ("is this one beat or many beats?"). The system prompt and tool descriptions actively steer the model toward sectioned output for any media-bearing topic, producing three-section posts ("The Video" / "Audio" / "Community Reaction") for single-beat single-creator stories.

The fix is to introduce a unified `post_topic` tool whose only shape is a `blocks` array — a single intro block IS "simple"; multiple blocks IS "sectioned" — and rewrite the topic-editor system prompt to give explicit brevity guidance and a worked example. The legacy tools must stay accepted as deprecated aliases so in-flight model calls and stored topics continue to function. The publisher already renders blocks-only topics via `render_topic_publish_units` (topic_editor.py:4124), so no rendering changes should be required other than verifying the single-intro-block path.

**Constraints that matter:**
- Stored-topic backward compatibility via `normalize_document_blocks` (topic_editor.py:3957) is mandatory and untouched.
- Legacy tool names (`post_simple_topic`, `post_sectioned_topic`) must remain accepted at the model-call layer.
- The live feed is production-public on a cron — prompt language must be conservative.
- Out of scope: candidate-generator prompts (`live_update_prompts.py`), citation visual style, auto-shortlist threshold, publisher CTAs, DB migrations.

## Main Phase

### Step 1: Audit the current write-tool surface and capture all touch points (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Re-confirm** every site that branches on the tool names by re-running a single grep for `post_simple_topic|post_sectioned_topic` across `src/` and `tests/`. Expected hits in `topic_editor.py`:
   - `WRITE_TOOL_NAMES` set (`topic_editor.py:43-51`)
   - `TOPIC_EDITOR_SYSTEM_PROMPT` body (`topic_editor.py:54-143`)
   - `TOPIC_EDITOR_TOOLS` definitions (`topic_editor.py:219-295`)
   - Dispatch / validation branches around `topic_editor.py:1059`, `1849-1928`, `2155-2165`, `2441`, `2855`, `3632-3647`.
2. **List** every test file that currently exercises these tools: `tests/test_topic_editor_runtime.py`, `tests/test_topic_editor_core.py`, `tests/test_topic_editor_media_understanding.py`, `tests/test_backfill_live_update_topics.py`, `tests/test_live_update_editor_*`. Don't modify yet — just enumerate.
3. **Confirm** the publisher path `render_topic_publish_units` (`topic_editor.py:4124-4246`) already handles a one-intro-block topic: a single block produces header + intro text + (optional) media, with no orphan section headers, no missing-section placeholders. Note: in the current code `render_topic` (`topic_editor.py:4087`) is only used as a fallback when `normalize_topic_document` returns no blocks; new `post_topic` calls will always go through `render_topic_publish_units`.

### Step 2: Introduce the unified `post_topic` tool definition and deprecate the legacy two (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Add** a new tool entry `post_topic` in `TOPIC_EDITOR_TOOLS` (insert immediately before `post_simple_topic` at `topic_editor.py:219`). Schema:
   - Required: `proposed_key`, `headline`, `source_message_ids`, `blocks`.
   - **No top-level `body` field.** Prose lives in blocks only.
   - `blocks` items reuse the existing block schema (type=`"intro"`|`"section"`, `title?`, `text` required, per-block `source_message_ids`, per-block `media_refs` with the existing canonical/shorthand shapes).
   - Optional: `parent_topic_id`, `notes`, `override_collisions`.
   - Description must frame the choice as **editorial complexity (one beat vs. many)** with explicit guidance: "One intro block = a single-beat story, even if it has media. Add section blocks only when the topic has distinct contributors, angles, or sub-stories that each independently merit a header."
2. **Update** the existing `post_simple_topic` (`topic_editor.py:219-239`) and `post_sectioned_topic` (`topic_editor.py:241-295`) tool descriptions to start with `"[DEPRECATED — use post_topic]"` and to remove the "use this for ANY media" steering on `post_sectioned_topic`. Keep their schemas unchanged so old model calls still validate.
3. **Extend** `WRITE_TOOL_NAMES` (`topic_editor.py:43-51`) to include `"post_topic"`.
4. **Add** a normalization helper near the existing `_summary_for_tool` (`topic_editor.py:3631`):
   ```python
   def _normalize_post_topic_call(call: Dict[str, Any]) -> Dict[str, Any]:
       # Re-key post_simple_topic and post_sectioned_topic into a post_topic
       # call shape (blocks-only). Pure transform; no side effects.
   ```
   - For `post_simple_topic`: synthesize a single `intro` block whose `text` is the legacy `body` and whose `source_message_ids` are the legacy top-level `source_message_ids`. (Legacy `media` array on `post_simple_topic` is empty in practice — the current code rejects media there; assert empty.)
   - For `post_sectioned_topic`: prefer `blocks` if present; otherwise convert `body` + `sections` via the existing `normalize_document_blocks` logic, but inline (do not change `normalize_document_blocks` itself).
   - For `post_topic`: pass through.
5. **Route** every existing dispatch branch through the normalizer so the validation + persistence pipeline only deals with the unified blocks shape:
   - Update the membership set at `topic_editor.py:1059`, `2441`, `2855` to include `"post_topic"`.
   - At `topic_editor.py:1849-1856`, extend `action_by_tool` with `"post_topic": "post_topic"` and update the rejected-action map at `topic_editor.py:1916` similarly. Keep `"post_simple"` / `"post_sectioned"` action strings for legacy aliases so historical telemetry filters continue to work.
   - At `topic_editor.py:1860-1890` (post_simple media/source-count guards): leave intact for the legacy alias path; do NOT extend these guards to `post_topic`.
   - At `topic_editor.py:1893-1905` (post_sectioned requires sections or blocks): generalise the check so `post_topic` calls require non-empty `blocks` (after normalization).
   - At `topic_editor.py:3631-3647` (`_summary_for_tool`): add a `post_topic` branch that emits `{"blocks": args["blocks"]}` (no top-level `body`, no `sections`).

### Step 3: Rewrite `TOPIC_EDITOR_SYSTEM_PROMPT` for editorial brevity (`src/features/summarising/topic_editor.py:54-143`)
**Scope:** Medium
1. **Replace** the constant with a version that:
   - Renames the role from "BNDC live-update topic editor" to **"BNDC live-update writer"** (line 54). The label is itself a bias toward article-style structure.
   - Mentions only `post_topic` in the primary instructions; lists `post_simple_topic` and `post_sectioned_topic` once as **"deprecated — accept if produced, prefer `post_topic`"** so existing learned behavior doesn't break catastrophically.
   - **Removes** the rule "use sectioned for ANY media" (lines 101-105). Replaces it with:
     > *"Use the minimum number of blocks that fits the story. A single creator dropping a single artifact = exactly ONE `intro` block with the media attached to it. Only add `section` blocks when the topic has genuinely distinct contributors, angles, or sub-stories that each independently merit their own header. If you find yourself splitting one creator's one video into 'The Video' / 'Audio' / 'Community Reaction' sections, you are wrong — collapse it to one block."*
   - **Adds** brevity guidance: intro block body = 1-3 sentences, ~30-150 words; section block body = 1-2 sentences; no bullet lists; no filler restatement of the title.
   - **Adds** a concrete worked example: a one-creator one-video drop with two praise replies → exactly one intro block with the video as a `media_ref`. Show the "don't do this" counter-example (the same input split into three sections) and label it explicitly as wrong.
   - **Keeps** the per-block source-attribution rule and the per-block media-attachment rule (those are correctness rails, not the problem).
   - **Keeps** the canonical `media_ref` shape doc and the no-global-Sources-footer rule.
2. **Verify** that no test in `tests/test_topic_editor_*` greps for the old prompt strings; if any do, update them to assert on the new wording in Step 5 rather than blocking here.

### Step 4: Verify the publisher renders a one-block topic cleanly (`src/features/summarising/topic_editor.py:4087-4246`)
**Scope:** Small
1. **Trace** `_publish_topic` (`topic_editor.py:2861`) → `render_topic_publish_units` (`topic_editor.py:4124`). With a unified `post_topic` call producing one intro block:
   - `normalize_topic_document` returns `[{type: "intro", title: None, text, source_message_ids, media_refs}]`.
   - The function emits one text unit (`header + "\n\n" + block_content`) followed by one media unit per `media_ref`. No section header is emitted because the block type is `intro` and `intro` branches skip the `**title**` line.
2. **Confirm** by reading the loop body at `topic_editor.py:4165-4216`: a single intro block produces exactly one text unit and N media units. No regression to investigate.
3. **Do not modify the publisher** unless a test fails in Step 5. The brief explicitly says "if the existing publisher already handles one-block topics, no change needed."

### Step 5: Tests (`tests/test_topic_editor_runtime.py`, `tests/test_topic_editor_core.py`)
**Scope:** Medium
1. **Add** to `tests/test_topic_editor_runtime.py`:
   - `test_post_topic_single_intro_block_with_media_accepted`: drives a `post_topic` call with one `intro` block containing one `media_ref`; asserts the persisted topic has `summary.blocks` of length 1, no top-level `summary.body`, no `summary.sections`, and that `render_topic_publish_units` produces `[text_unit, media_unit]`.
   - `test_post_topic_multi_block_accepted`: drives a `post_topic` call with one intro + two section blocks; asserts blocks length 3, per-block sources preserved, render produces alternating text/media units.
   - `test_post_topic_rejects_empty_blocks`: empty `blocks` array → `post_topic_requires_blocks` rejection.
   - `test_legacy_post_simple_topic_still_normalizes_via_alias`: existing `post_simple_topic` call shape (body + source_message_ids, no media) is accepted and produces a single intro block in storage.
   - `test_legacy_post_sectioned_topic_with_body_and_blocks_still_works`: existing `post_sectioned_topic` call with `body` + `blocks` is accepted; the `body` is dropped from the stored summary (because Step 2.4 path emits blocks-only) OR kept depending on the alias normalization decision — assert whichever the implementation picks and document it in the assumption list. (Recommended: drop `body` to keep one source of truth in storage going forward; legacy stored records are unaffected.)
2. **Add** to `tests/test_topic_editor_core.py`:
   - `test_normalize_document_blocks_still_handles_legacy_body_plus_sections`: explicit regression confirming `normalize_document_blocks` is unchanged. (One assertion is enough — the existing test class already covers this; just add one named-after-the-spec test that pins it.)
3. **Add** an end-to-end-style normalization test mimicking the NebSH "Last Party" scenario: a fixture with one author, one video-bearing source message, one reply, two praise comments, and a model call that emits one intro block. Assert (a) the call is accepted, (b) `summary.blocks` length is 1, (c) the publisher emits exactly one text unit + one media unit, (d) no "section" UI markers (`**...**` lines) appear in the rendered text. Drive this against the deterministic tool-dispatch entry point — do NOT call the real Anthropic API.
4. **Update** any existing test that asserts on the prompt's "use sectioned for ANY media" wording (search `tests/` for `sectioned` and `ANY media`). Replace those assertions with assertions on the new brevity guidance string, or delete the assertion if it is now obsolete.

### Step 6: Run targeted then broad tests (repo root)
**Scope:** Small
1. **Run** the topic-editor test files in isolation first:
   ```bash
   pytest tests/test_topic_editor_core.py tests/test_topic_editor_runtime.py -x -q
   ```
2. **Run** the live-update and backfill suites that depend on stored-topic shape:
   ```bash
   pytest tests/test_live_update_editor_publishing.py tests/test_live_update_editor_lifecycle.py tests/test_backfill_live_update_topics.py tests/test_topic_editor_media_understanding.py -x -q
   ```
3. **Run** the full suite once green:
   ```bash
   pytest -x -q
   ```

## Execution Order
1. Step 1 (audit) — confirm touch points without writing code.
2. Step 2 (tool surface + dispatch) — code change that everything else builds on.
3. Step 3 (system prompt rewrite) — independent of Step 2's mechanics; can be done after Step 2 to keep diffs reviewable.
4. Step 4 (publisher verification) — read-only confirmation. If it surfaces a real gap, fix inline before Step 5.
5. Step 5 (tests) — write new tests for the unified path and the deprecated alias path; update tests broken by Steps 2 & 3.
6. Step 6 (validation) — targeted suites first, then full repo.

## Validation Order
1. `tests/test_topic_editor_core.py` — pure unit tests on `normalize_document_blocks` and helpers. Cheapest; pins backward compat.
2. `tests/test_topic_editor_runtime.py` — dispatch + validation pipeline. Confirms the unified tool and the deprecated aliases.
3. `tests/test_live_update_editor_*` and `tests/test_backfill_live_update_topics.py` — downstream consumers of stored topic summaries. Confirms publisher and lifecycle untouched.
4. Full `pytest` run — catches any unrelated grep-based assertions on the old prompt string.
5. Manual smoke (info-only, cannot run in CI): on the next scheduled live-update cron tick in dev, eyeball that a single-author single-video topic posts as one paragraph + media, not three sections.
