# Topic-Editor Schema De-conflation — Fix Multi-Section Bias in Hourly Summary Posts

## Goal

Stop the BNDC hourly summary live-update flow from producing long, multi-section editorial posts for single-beat stories. A single-creator single-video post (e.g. "NebSH drops 'The Last Party'") should render as one paragraph alongside the video — not as a three-section document with separate "The Video" / "Audio" / "Community Reaction" headers.

## Root cause (analysis is settled — do not re-litigate)

`src/features/summarising/topic_editor.py` conflates two orthogonal axes into a single tool choice:

1. **Has media or not** — a *rendering* concern (Discord needs media attached to a specific message so the publisher can lay it out).
2. **Has one editorial angle or many** — an *editorial* concern (one paragraph vs. a structured document).

`post_simple_topic` vs `post_sectioned_topic` is meant to be the editorial switch. But the rule "media must attach to a block" turned it into the rendering switch: any media → sectioned. Once sectioned, the schema shape (`blocks: [{type: "intro" | "section", title, text, source_message_ids, media_refs}]`) and the tool name itself prompt the model to fill in sections.

Concrete reinforcers in the current code:

- `post_simple_topic` (topic_editor.py:219-224) is gated to text-only — its tool description and the system prompt at lines 101-105 both forbid media on it, forcing media topics into `post_sectioned_topic`.
- `post_sectioned_topic` (topic_editor.py:241-295) requires BOTH a top-level `body` string AND a `blocks` array — structural duplication that invites an intro paragraph plus sectioned content (look at `required` at line 293).
- The system-prompt rule "every factual block gets its own `source_message_ids`" (lines 108-113) pressures the model to *create* extra blocks just to house multiple citations cleanly.
- The role label "You are the BNDC live-update topic editor" (line 54) primes article-style sectioned output.
- The topic-editor system prompt has **zero brevity guidance**. (Compare `live_update_prompts.py:382-383` which says "1-2 sentences, 35-220 words, compact prose, no bullet lists" — that lives in the candidate generator, not the topic editor.)
- The publisher emits a "Share with your followers!" CTA per rendered block — a system-level incentive to split (not in scope to remove, just context for why this matters).

## In scope

1. **Collapse `post_simple_topic` + `post_sectioned_topic` into one unified `post_topic` tool.** The unified tool takes a `blocks` array. A one-block topic is "simple"; a many-block topic is "sectioned". One semantic, one entry point.

2. **Keep `post_simple_topic` and `post_sectioned_topic` as deprecated aliases** that normalize into the unified shape so existing model calls don't error. Mark them deprecated in their tool descriptions. Drop them from `TOPIC_EDITOR_DECISION_TOOLS` advertising preference if there is one; the model should be steered to `post_topic`.

3. **Drop the redundant top-level `body` field on the unified tool.** Prose lives in blocks only. Existing stored topics that have a top-level `body` continue to load via `normalize_document_blocks` (topic_editor.py:3957) — that path already converts `body` → intro block. Don't break it.

4. **Rewrite the topic-editor system prompt** (`TOPIC_EDITOR_SYSTEM_PROMPT` constant, lines 54-143):
   - Remove the "use sectioned for ANY media" rule. Replace with editorial guidance: *"Use the minimum number of blocks that fits the story. A single creator with a single artifact = exactly ONE intro block with the media attached. Only add `section` blocks when the topic has genuinely distinct contributors, angles, or sub-stories that each independently merit their own header."*
   - Add explicit brevity guidance: intro block body = 1-3 sentences, ~30-150 words; section block bodies = 1-2 sentences each. No bullet lists. No padding.
   - Add a concrete minimal example showing a single-creator single-video post as exactly one intro block with the video attached. The example should illustrate what NOT to do as well (don't split a single-beat story into Video/Audio/Reaction sections).
   - Replace the "editor" role framing with "writer" or "live-update author" — the label is itself a bias.
   - Keep the per-block source attribution and per-block media attachment rules (those are valuable for citation/render correctness and are not the problem).

5. **Update the tool descriptions** in `TOPIC_EDITOR_TOOLS` to remove the "post_sectioned_topic for ANY media" steering. The unified `post_topic` description should frame the choice as editorial complexity (one beat vs. many), not media presence.

6. **Verify the publisher renders a single-block topic cleanly.** Grep for the publisher path (likely in `src/features/sharing/` or wherever topic rendering happens). A single-intro-block topic with media must render as: title + one paragraph + media attached, with no orphan "section" UI, no missing-section placeholders, no duplicated "Share with your followers!" CTAs. If the existing publisher already handles one-block topics, no change needed; otherwise add the rendering branch.

7. **Tests** in the existing topic-editor test file (find it under `tests/`):
   - Unified `post_topic` accepts a single-intro-block topic with media → normalizes correctly.
   - Unified `post_topic` accepts a multi-block topic → normalizes correctly.
   - Legacy `post_simple_topic` still works (deprecated alias path).
   - Legacy `post_sectioned_topic` with `body` + `blocks` still works (deprecated alias path).
   - `normalize_document_blocks` still converts old `body + sections` shape to blocks (legacy stored-topic compat).
   - End-to-end smoke: a fixture mimicking the NebSH scenario (one author, one video drop, one reply, two praise comments) produces a topic with exactly one block when run through the topic editor's normalization path. (If running the full LLM pipeline in tests is impractical, test the schema acceptance + prompt-content assertions instead.)

## Out of scope

- The candidate-generator path (`live_update_prompts.py`) — different feature, already has good brevity guidance.
- Visual rendering of citations (inline brackets vs footer) — leave as-is.
- The auto-shortlist media reaction-threshold logic — unrelated.
- The "Share with your followers!" per-section CTA in the publisher — separate concern; if it ends up looking weird with one-block posts, file a follow-up ticket, don't fix in this run.
- Database schema migrations for stored topic records — the legacy normalization path already handles old shapes; we are not migrating data, only changing what NEW topics look like.

## Constraints

- **Backward compatibility for stored topics is mandatory.** Existing rows must keep loading. `normalize_document_blocks` (topic_editor.py:3957) is the contract — don't change its accepted inputs.
- **Legacy tool aliases must keep working at the model-call layer.** If a model call comes in with `post_simple_topic` or `post_sectioned_topic`, accept and normalize. Don't reject.
- **The live-update feed runs on a cron in production.** Botched prompt changes show up as ugly posts publicly. Prompt language must be conservative and well-tested. Prefer adding rules over removing the safety rails on source/media attachment.

## Validation

After the change, the NebSH "Last Party" scenario should generate a one-block post — title + one intro paragraph with the video attached, no separate "The Video" / "Audio" / "Community Reaction" sections. Multi-creator/multi-angle stories (e.g. a tool release with several creators showing different demos) should still produce multi-block output.

## Files (starting points — investigate the full call graph)

- `src/features/summarising/topic_editor.py` — primary: schema, prompts, tools, `normalize_document_blocks`.
- `src/features/summarising/live_update_editor.py` — related editor logic.
- `src/features/summarising/summariser_cog.py` — cog wiring.
- Publisher path: grep for `post_simple_topic` / `post_sectioned_topic` references and "Share with your followers" string to locate.
- Tests: existing topic-editor test file under `tests/`.
