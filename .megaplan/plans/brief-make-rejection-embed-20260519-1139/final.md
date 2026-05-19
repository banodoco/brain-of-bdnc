# Execution Checklist

- [x] **T1:** Wrap per-block citation URLs in angle brackets at topic_editor.py:4312. Change `f"[{idx}] {url}"` to `f"[{idx}] <{url}>"` so Discord suppresses same-channel message-link collapse to channel-pill while keeping the URL clickable. This is a single-line change in `render_topic_publish_units`.
  Executor notes: Changed line 4348 from `f"[{idx}] {url}"` to `f"[{idx}] <{url}>"`. Fallback path `f"[{idx}] {sid}"` unchanged. Verified: 95 tests pass, 2 pre-identified citation format assertions fail (expected — fixed in T5).
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/archive-inprocess/brief.md
    - .megaplan/briefs/rejection-and-citation-linkability/brief.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/.plan.lock
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/final.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_output.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_snapshot.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_v1_raw.txt
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/phase_result.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.meta.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/state.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_finalize_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_plan_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/user_actions.md
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_runtime.py

- [x] **T2:** Add URL validation guard at line 1314 in `_dispatch_understand_media` and write investigation-documentation comment block. Replace `media_url = attachment.get("url") or attachment.get("proxy_url") or ""` with a guard that prefers `proxy_url` when `url` does not start with `http`. Also add a top-of-function comment explaining that archive data may store bare filenames in the `url` field, that the guard mitigates this, and that the new `media_url:` surface line in rejection output (T4) makes the actual URL visible in production.
  Executor notes: Replaced `media_url = attachment.get('url') or attachment.get('proxy_url') or ''` with guard that prefers proxy_url when url doesn't start with 'http'. Falls back to bare url when both are bad. Added investigation-documentation comment block in function docstring. Verified: syntax check passes, no regression in non-citation tests.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/archive-inprocess/brief.md
    - .megaplan/briefs/rejection-and-citation-linkability/brief.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/.plan.lock
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/final.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_output.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_snapshot.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_v1_raw.txt
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/phase_result.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.meta.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/state.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_finalize_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_plan_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/user_actions.md
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_runtime.py

- [x] **T3:** Enrich `_dispatch_understand_media` error-outcome dicts with source metadata (`message_id`, `channel_id`, `guild_id`, `media_url`). Three return sites need enrichment: (a) attachment_index out-of-range at :1303 (add `message_id` from line 1270 if `source` resolved), (b) no-url-field at :1315 (add `message_id`), (c) requests.get failure at :1348 (add `message_id`, `channel_id` from resolved source, `guild_id` from source or context fallback, `media_url`). All fields are optional — only include when available. Format: `message_id` as string, `channel_id` as int-or-None, `guild_id` as int-or-None, `media_url` as string.
  Executor notes: Enriched all four error-return sites: (a) message_id-not-found adds message_id; (b) attachment_index out-of-range adds message_id/channel_id/guild_id from source; (c) no-url-field adds message_id/channel_id/guild_id from source; (d) download-failure adds message_id/channel_id/guild_id/media_url with guild_id context fallback. All fields optional/cast correctly. Verified: syntax check passes.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/archive-inprocess/brief.md
    - .megaplan/briefs/rejection-and-citation-linkability/brief.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/.plan.lock
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/final.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_output.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_snapshot.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/finalize_v1_raw.txt
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/phase_result.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.md
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/plan_v1.meta.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/state.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_finalize_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_plan_v1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/user_actions.md
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_runtime.py

- [x] **T4:** Update `_build_trace_embed` rejection field renderer at :2866-2877. For each rejected outcome, check the outcome dict for `message_id`/`channel_id`/`guild_id`/`media_url`. When `message_id`+`channel_id`+`guild_id` are all present, prepend a clickable jump URL line: `jump: https://discord.com/channels/<g>/<c>/<m>`. When `media_url` is present, add a `media_url:` line. Then emit the error line as today. Fall back to today's single-line `\`tool\`: {error}` format when metadata is missing. Fallback `guild_id` from `updates.get("guild_id")`. Keep 1024-char truncation. Also check `input_by_id` (line 2851) for `message_id` as a secondary fallback.
  Depends on: T3
  Executor notes: T4 was already fully implemented at lines 2902-2943. Verified: (1) jump URL line emitted when message_id+channel_id+guild_id present (line 2931), (2) media_url line emitted when media present (line 2934), (3) fallback to legacy `tool`: {error} when metadata missing (line 2938), (4) 1024-char truncation preserved (lines 2940-2942), (5) updates.get('guild_id') used as guild_id fallback (line 2923), (6) input_by_id secondary fallback for message_id (lines 2916-2919). Full test suite run: 149 passed, 3 failed — all 3 failures are pre-identified citation format assertions from T1 (runtime.py:3123, core.py:358, core.py section test). Zero regressions from T4. No additional code changes needed.
  Files changed:
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution_audit.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution_batch_1.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution_batch_2.json
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/step_receipt_execute_v1.json

- [x] **T5:** Update existing test assertions and add new test coverage. (a) Update `test_topic_editor_runtime.py:3123`: change expected string from `Sources: [1] https://discord.com/channels/1/10/200` to `Sources: [1] <https://discord.com/channels/1/10/200>`. (b) Update `test_topic_editor_core.py:358`: same angle-bracket wrapping change for the `Sources:` assertion. (c) Add `test_rejection_field_renders_jump_url_when_message_id_present` — build an outcome with `outcome: tool_error`, `tool: understand_video`, `message_id: "200"`, `channel_id: 10`, `guild_id: 1`, `media_url: "https://cdn.example.com/video.mp4"`, `error: "failed to download media: 404 ..."`, provide `updates.metadata.tool_calls` with matching input, call `_build_trace_embed`, assert rejection field value contains `jump: https://discord.com/channels/1/10/200` and `media_url: https://cdn.example.com/video.mp4`. (d) Add `test_rejection_field_falls_back_gracefully_without_message_id` — outcome with `tool_error` but no metadata; assert today's format without crash. (e) Add `test_citation_urls_are_angle_bracket_wrapped` — call `render_topic_publish_units` and assert output contains `<https://discord.com/channels/...>`.
  Depends on: T1, T2, T3, T4
  Executor notes: Updated 5 existing citation assertions: runtime.py:3123 ('Sources: [1] <url>'), core.py:358 ('Sources: [1] <url>'), core.py:410, 413, 416 ('[1] <url>'). Added 3 new tests: test_rejection_field_renders_jump_url_when_message_id_present (happy path — jump URL + media_url in rejection field), test_rejection_field_falls_back_gracefully_without_message_id (graceful fallback — legacy format when no metadata), test_citation_urls_are_angle_bracket_wrapped (<URL> wrapping verified, unwrapped form absent). Full suite: 155 passed, 0 failed in 1.92s.
  Files changed:
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution_batch_3.json
    - tests/test_topic_editor_core.py

- [x] **T6:** Run full test suite (`pytest tests/test_topic_editor_runtime.py tests/test_topic_editor_core.py -x -q`) to verify all changes pass. If any test fails, read the error, fix the code, and re-run until all pass. Then write a throwaway smoke script that exercises the rejection field rendering and citation wrapping manually, confirm it works, and delete the script.
  Depends on: T5
  Executor notes: Full test suite: 155 passed, 0 failed, 2 warnings in 1.72s. Smoke script written, run, and deleted. Smoke tests verified: (1) citation URLs wrapped in <https://...> with no bare unwrapped URLs, (2) rejection field enriched with jump URL + media_url lines, (3) graceful fallback to legacy format when no metadata. All assertions passed.
  Files changed:
    - .megaplan/plans/brief-make-rejection-embed-20260519-1139/execution_batch_4.json

## Watch Items

- W1: `<URL>` wrapping safely suppresses Discord same-channel message-link collapse to channel-pill in practice. The plan assumes `<URL>` syntax works for this case (it's the standard way to suppress unfurling), but the behavior can only be confirmed with a live Discord test after deployment. If it doesn't work, the fallback is to render each citation as `[N] <message_id>` without a clickable URL.
- W2: The `guild_id` available in `updates` at `_build_trace_embed` (from `_resolve_guild_id()`) is sufficient for constructing jump URLs. Channel-level specificity comes from the enriched outcome's `channel_id`. If `channel_id` is missing from the outcome, the jump URL cannot be constructed — graceful fallback is required.
- W3: The 1024-char embed-field limit must still hold with the enriched rejection format. With jump URL (~55 chars) + media_url (~100-200 chars) + error line, a single rejection fits easily. Multiple rejections are truncated at 1000 chars + "\n…" as today.
- W4: The bare filename `LTX-23-i2v_00416-audio.mp4` in the 404 error message may be an artifact of the upstream CDN's error response format rather than a real URL truncation bug. The validation guard (T2) + `media_url:` surface line (T4) together provide durable mitigation so future incidents are visible.
- W5: No regression in admin-embed field layout — same field order (editorial reasoning, summary, model & cost, input context, tool calls, rejections, overrides, publishing), same field names, same conditional rendering. The rejection field value changes format but the field name `rejections (N)` stays identical.
- W6: Other tool-call outcomes that carry `tool_error` but originate from handlers other than `_dispatch_understand_media` (e.g., `search_topics`, `get_reply_chain`) will NOT have the enriched fields. `_build_trace_embed` must handle missing fields gracefully for those cases — fall back to today's single-line format.

## Sense Checks

- **SC1** (T1): Does line 4312 now emit `[N] <url>` instead of `[N] url`? Is the fallback path `[N] {sid}` for missing metadata preserved unchanged?
  Executor note: (not provided)

- **SC2** (T2): Does the URL validation guard correctly prefer `proxy_url` when `url` does not start with `http`? Does it fall back to the bare URL when both are bad (preserving the existing behavior of letting the download fail with a visible URL)? Is the documentation comment placed before the function body explaining the truncation finding?
  Executor note: (not provided)

- **SC3** (T3): Are all three error-return sites enriched? (out-of-range at :1303, no-url at :1315, download-failure at :1348). Does each only add fields when the source data is available? Is `message_id` cast to string and `channel_id`/`guild_id` kept as int-or-None?
  Executor note: (not provided)

- **SC4** (T4): Does the rejection field now emit `jump: https://discord.com/channels/G/C/M` and `media_url: <url>` lines when metadata is present, and fall back to today's `\`tool\`: {error}` when it's missing? Is the 1024-char truncation preserved? Is `updates.get("guild_id")` used as a guild_id fallback?
  Executor note: All SC4 criteria verified against lines 2902-2943: (a) jump URL emitted as `jump: https://discord.com/channels/{g_id}/{ch_id}/{msg_id}` when message_id+channel_id+guild_id all present (line 2931); (b) media_url line emitted as `media_url: {media}` when media present (line 2934); (c) falls back to legacy `` `tool`: {error} `` single-line format when neither has_jump nor media (line 2938); (d) 1024-char truncation preserved (lines 2940-2942: `value[:1000] + '…'`); (e) `updates.get('guild_id')` used as guild_id fallback when outcome lacks guild_id (line 2923); (f) `input_by_id` secondary fallback for message_id when outcome lacks it (lines 2916-2919). All criteria satisfied. Test suite confirms no regressions from T4.

- **SC5** (T5): Do both existing citation assertions (runtime.py:3123 and core.py:358) now expect `<url>` wrapping? Do the three new tests exercise the happy path (jump URL + media_url present), the graceful-fallback path (no metadata), and citation wrapping respectively?
  Executor note: (not provided)

- **SC6** (T6): Does `pytest tests/test_topic_editor_runtime.py tests/test_topic_editor_core.py -x -q` pass with zero failures? Was a throwaway smoke script written, run, and deleted?
  Executor note: (not provided)

## Meta

EXECUTION GUIDANCE:

1. All edits are in `src/features/summarising/topic_editor.py` and two test files. No other files are touched.

2. Execute T1 first (single-line change, zero risk), then T2+T3 (same function `_dispatch_understand_media`, but T2 is the guard+comment and T3 enriches error dicts — they can be done in one editing pass but are separate tasks for review clarity), then T4 (depends on T3 because it consumes the enriched fields). T5 updates tests and T6 runs them.

3. For T3 enrichment: the three error return sites are all dict literals. Add `message_id`, `channel_id`, `guild_id`, `media_url` as optional keys at each site, only when the data is available. At the attachment_index out-of-range site (:1303), `source` may not be resolved yet (it's resolved before this check at :1277), so `source.get("channel_id")` and `source.get("guild_id")` ARE available. At the no-url-field site (:1315), same — source is resolved. At the requests.get failure site (:1348), all data is available.

4. For T4: The rejection loop is at :2868. For each outcome, check `outcome.get("message_id")`, `outcome.get("channel_id")`, `outcome.get("guild_id")`, `outcome.get("media_url")`. Build the rejection line piece by piece: jump URL line first if all IDs present, then media_url line if URL present, then the error line. Use `updates.get("guild_id")` as fallback for `guild_id`. Keep the existing `input_by_id` lookup (:2851) as a secondary fallback for `message_id`.

5. When editing test assertions, grep for ALL occurrences of `Sources: [` in test files to ensure none are missed. The two known hits are at runtime.py:3123 and core.py:358.

6. The throwaway smoke script in T6 should: (a) import `render_topic_publish_units` from topic_editor, call it with a sample topic and source_metadata, assert `<https://` is in the output; (b) create a TopicEditor instance, call `_build_trace_embed` with enriched outcomes, assert "jump:" and "media_url:" appear in the embed fields. Delete the script after confirming.

7. GOTCHA: The `_build_trace_embed` function accesses `updates.get("guild_id")`. The `updates` dict is built in `_build_run_update_payload` which includes `"guild_id": self._resolve_guild_id()`. This is the guild-level ID, not channel-specific — the channel_id MUST come from the enriched outcome.

8. GOTCHA: `discord.Embed.add_field` takes a `value` parameter that Discord truncates at 1024 chars. The existing truncation is `value[:1000] + "\n…"`. The enriched rejection format adds ~60-260 chars per rejection, so with 1-2 rejections the field still fits. Always preserve the truncation guard.
