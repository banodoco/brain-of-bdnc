# Implementation Plan: Rejection Embed Media URLs Clickable + Citation Self-Link Fix

## Overview

Two display fixes in `src/features/summarising/topic_editor.py`:

1. **Rejection field enrichment** (`:2866-2877`): When admin-embed rejections are rendered, also include a clickable Discord jump URL to the source message and surface the actual `media_url` the tool tried to fetch. The data (message_id, channel_id, guild_id, media_url) is added to the error outcome dict inside `_dispatch_understand_media` (`:1348-1354`), then consumed by `_build_trace_embed`.
2. **Citation URL wrapping** (`:4307-4312`): Wrap per-block citation URLs in `<…>` so Discord suppresses same-channel collapse to a `#channel-name` pill while keeping the URL clickable as plain blue text.
3. **URL truncation investigation** (`:1314`, `:1345`): Investigate why the 404 error shows a bare filename (`LTX-23-i2v_00416-audio.mp4`). Add a validation guard and documentation note.

All changes are display-layer only — no impact on archive/persistence/publishing logic.

## Main Phase

### Step 1: Enrich `_dispatch_understand_media` error outcomes with source metadata
**Scope:** Small — one function, two return sites
**File:** `src/features/summarising/topic_editor.py`

1. **At line 1348-1354 (the `requests.get` error handler):** Before returning the error outcome, add `message_id`, `channel_id`, `guild_id`, and `media_url` to the dict. The `message_id` is already available from line 1270. The `channel_id` and `guild_id` come from the resolved `source` dict (line 1279/1292).
   - `message_id` → `str(message_id)` from line 1270
   - `channel_id` → `source.get("channel_id")` 
   - `guild_id` → `source.get("guild_id") or context.get("guild_id")`
   - `media_url` → the `media_url` variable from line 1314

2. **URL validation guard at line 1314:** After `media_url = attachment.get("url") or attachment.get("proxy_url") or ""`, add a check: if `media_url` does not start with `"http"`, prefer `proxy_url` and log a structured warning. If both are bad, still proceed but the error outcome will carry the bad URL for diagnosis.
   ```python
   raw_url = attachment.get("url") or ""
   media_url = (raw_url if raw_url.startswith("http") else "") or attachment.get("proxy_url") or raw_url
   ```
   (If `raw_url` is bare and proxy_url is missing, media_url stays bare — the download will fail, but the rejection output now surfaces the actual URL used.)

3. **Same enrichment for other error sites in `_dispatch_understand_media`:** The `attachment_index` out-of-range error (line 1303) and `no url field` error (line 1315) should also carry `message_id` where possible (source may not be resolved in all paths — only add when available).

### Step 2: Update `_build_trace_embed` rejection field to render jump URL + media_url
**Scope:** Small — ~15 lines in the rejection loop
**File:** `src/features/summarising/topic_editor.py`

1. **At lines 2866-2877**, for each rejected outcome, look up the tool input via the already-built `input_by_id` (line 2851) to get `message_id` as fallback. Then check the outcome itself for `message_id`, `channel_id`, `guild_id`, and `media_url`.

2. **New format per rejection:**
   ```
   `understand_video` · jump: https://discord.com/channels/<g>/<c>/<m>
   media_url: <full url>
   error: failed to download media: 404 ...
   ```
   When fields are missing, fall back to today's single-line format gracefully.

3. **Keep the 1024-char truncation** with `value[:1000] + "\n…"` as today.

### Step 3: Wrap citation URLs in angle brackets
**Scope:** Trivial — 1 line change
**File:** `src/features/summarising/topic_editor.py`

1. **At line 4312**, change:
   ```python
   citation_parts.append(f"[{idx}] {url}")
   ```
   to:
   ```python
   citation_parts.append(f"[{idx}] <{url}>")
   ```
   Discord's `<URL>` syntax suppresses the embed/unfurl preview (including the same-channel message-link collapse to `#channel-name`) while keeping the URL clickable as plain blue text.

### Step 4: Document the URL truncation investigation
**Scope:** Trivial — comment only
**File:** `src/features/summarising/topic_editor.py`

1. **Add a comment block at `_dispatch_understand_media` (around line 1314)** explaining the finding:
   - Discord gateway attachment objects always carry full `https://cdn.discordapp.com/...` URLs.
   - Archived messages in the database may store truncated or bare-filename URLs in the `url` field.
   - The validation guard added in Step 1.2 prefers `proxy_url` when `url` doesn't start with `http`.
   - The new `media_url:` line in rejection output (Step 2) makes the actual URL visible in production for future debugging.

### Step 5: Update and add tests
**Scope:** Medium — update 1 existing assertion, add 2 new test functions
**File:** `tests/test_topic_editor_runtime.py`

1. **Update citation assertion at line 3123:** Change the expected string from:
   ```python
   assert "Sources: [1] https://discord.com/channels/1/10/200" in channel.sent[0]
   ```
   to:
   ```python
   assert "Sources: [1] <https://discord.com/channels/1/10/200>" in channel.sent[0]
   ```

2. **Search for any other assertions on `Sources:` URL format** in the test file. Update any that match the non-wrapped format.

3. **Add test: `test_rejection_field_renders_jump_url_when_message_id_present`**
   - Build an outcome with `outcome: "tool_error"`, `tool: "understand_video"`, `message_id: "200"`, `channel_id: 10`, `guild_id: 1`, `media_url: "https://cdn.example.com/video.mp4"`, `error: "failed to download media: 404 ..."`.
   - Also provide `updates.metadata.tool_calls` with matching input.
   - Call `_build_trace_embed` and assert the rejection field value contains `jump: https://discord.com/channels/1/10/200` and `media_url: https://cdn.example.com/video.mp4`.

4. **Add test: `test_rejection_field_falls_back_gracefully_without_message_id`**
   - Outcome with `tool_error` but no `message_id`/`channel_id`. Assert the rejection line uses today's format without crashing or emitting empty lines.

5. **Add test: `test_citation_urls_are_angle_bracket_wrapped`**
   - Call `render_topic_publish_units` with sample source_metadata and assert the output contains `<https://discord.com/channels/...>`.

## Execution Order
1. Step 3 (citation wrapping) — trivial, low-risk, can land immediately.
2. Step 4 (documentation) — write the comment so investigation is captured.
3. Step 1 (enrich outcomes) — the data plumbing for rejection enrichment.
4. Step 2 (render rejection field) — consume the enriched data.
5. Step 5 (tests) — update assertions and add new coverage.

## Validation Order
1. Run `pytest tests/test_topic_editor_runtime.py -x -q` after Step 3 to confirm citation wrapping doesn't break existing tests.
2. After Steps 1+2, run the same test suite — expect updated assertions to need updating.
3. After Step 5, full test suite must pass.
4. Optional: search for any other test files that assert on citation URL format or rejection line format and update them.
