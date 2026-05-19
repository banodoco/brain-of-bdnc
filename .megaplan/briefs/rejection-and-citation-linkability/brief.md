# Brief: Make rejection embed media URLs clickable + fix resources self-link collapse

## Outcome

Two display fixes in `src/features/summarising/topic_editor.py`:

1. **Rejection field links to the source post and surfaces the real media URL.** When the bot logs an admin-embed rejection (`tool_error` outcome from `understand_video` etc.), the rejection line currently shows only the raw exception text (e.g. `understand_video: failed to download media: 404 Client Error: Not Found for url: LTX-23-i2v_00416-audio.mp4`). After the fix, the rejection line additionally renders a clickable Discord jump URL to the source message the tool was acting on, plus the actual `media_url` the tool tried to fetch (so admins can verify what failed).
2. **Resources-channel citation URLs stay clickable to the specific message** rather than collapsing to a generic `#channel-name` pill. When a topic posted in `#ltx_resources` cites messages from that same channel, Discord auto-collapses the `https://discord.com/channels/G/C/M` URL to a flat channel mention. The fix wraps citation URLs in `<…>` so Discord suppresses the unfurl/collapse but the URL stays clickable as plain text.

Also: investigate the apparent URL truncation in the rejection 404 — the server's "Not Found for url: LTX-23-i2v_00416-audio.mp4" suggests `media_url` may be degenerating to a bare filename somewhere upstream of `requests.get`. If a real bug is found, fix at the source; if it's just how the upstream server reports the path, document the finding and rely on the new "surface real media_url" path to make this visible.

## Why this matters (context)

User reported two things from the BNDC bot's admin output:

- Rejection line: `understand_video: failed to download media: 404 Client Error: Not Found for url: LTX-23-i2v_00416-audio.mp4` — no way to navigate to the offending source post, and the URL fragment looks suspiciously like a bare filename rather than a real cdn.discordapp.com URL.
- Resources post Sources line: `Sources: [1] ⁠ltx_resources⁠ [2] ⁠ltx_resources⁠ [3] ⁠ltx_resources⁠` — looks like a non-clickable channel pill, in contrast to chatter posts where citations stay as clickable message links. The cause is Discord's renderer collapsing same-channel message URLs to a channel mention; the post is in `#ltx_resources` and the sources are also in `#ltx_resources`.

Both are display-layer-only and have no impact on archive / persistence / publishing logic.

## Scope (IN)

1. **`src/features/summarising/topic_editor.py:2866-2877` — rejection field renderer.**
   - Today: `rejection_lines.append(f"\`{tool}\`: {error}")` where `error` is the raw exception text from the outcome.
   - After: for each rejection, additionally compute a clickable Discord jump URL to the source message the tool was operating on (the tool input typically carries `message_id`; combine with the topic's `guild_id` and the source row's `channel_id`). If the tool input contains a `media_url`, surface it on its own line so Discord auto-links the full URL. Format roughly:
     ```
     `understand_video` · jump: https://discord.com/channels/<g>/<c>/<m>
     media_url: <full url>
     error: failed to download media: 404 ...
     ```
   - Keep the 1024-char embed-field truncation.
   - If the tool input doesn't expose `message_id` / `media_url`, fall back gracefully to today's behavior — don't crash, don't emit empty lines.

2. **`src/features/summarising/topic_editor.py:4307-4314` — per-block citation URL renderer.**
   - Today emits `[N] https://discord.com/channels/G/C/M` (auto-collapses to channel pill when self-referential).
   - After: wrap the URL in `<…>` → `[N] <https://discord.com/channels/G/C/M>` so Discord suppresses the unfurl/collapse but the URL remains clickable as plain blue link text.
   - Preserve the existing fallback `[N] {sid}` path for missing metadata.

3. **`src/features/summarising/topic_editor.py:1343-1354` — investigate URL truncation.**
   - Read the call sites that build `media_url` and pass it into `requests.get` at `:1345`. The reported 404 says "Not Found for url: LTX-23-i2v_00416-audio.mp4" (a bare filename) — figure out whether that filename is actually what we sent, or whether the destination server just truncates its 404 message that way (some CDNs do).
   - If it's a real bug — e.g. attachment-rel URL never got prefixed with `https://cdn.discordapp.com/attachments/...` — fix at the construction site and add the URL we tried to a structured log so a future repeat is debuggable.
   - If it's just the upstream server's quirk, write a one-paragraph note in `docs/` (or a top-of-function comment) so the next person doesn't chase it. The new "surface real media_url" line in fix #1 will make this visible in production.

4. **Test updates.**
   - `tests/test_topic_editor_runtime.py`: any test that asserts on the exact rejection-line format or citation-line format needs to be updated to match.
   - Add at least one test that:
     - Asserts the rejection field renders a jump URL when `message_id` is present.
     - Asserts citation URLs are emitted wrapped in `<…>`.

## Anti-scope (OUT)

- **Do NOT** restructure the admin-embed layout. Field order, field names, field count all stay.
- **Do NOT** touch the publishing pipeline, the curator, the archive runner, or anything outside `topic_editor.py` + its tests.
- **Do NOT** change `_render_source_suffix` (legacy simple-topic path). That path isn't producing user-visible artifacts in the reported issue; structured blocks are.
- **Do NOT** add a feature flag — the change is reversible via git.
- **Do NOT** broaden the truncation investigation into a refactor of the media-resolution pipeline. If the root cause is multi-hop, document it and fix only the immediate truncation.

## Locked decisions

- **LD-1:** Rejection field is enriched in-line — no separate embed, no thread-mention chain. Keep it dense.
- **LD-2:** Citation URLs use `<URL>` wrapping (suppress-unfurl, keep clickable) rather than markdown link syntax `[label](url)` — that syntax doesn't render in regular Discord messages, only in embed descriptions.
- **LD-3:** Truncation investigation happens once, in this sprint. If inconclusive, the new "media_url:" line is the durable mitigation; we don't open a follow-up sprint for it.
- **LD-4:** No new dependencies. Use existing helpers (e.g. the same `guild_id` / `channel_id` resolution path `render_topic_publish_units` already uses via `meta_by_id`).

## Open questions (planner should resolve)

- **OQ-1:** What's the right way to get `channel_id` for the rejection's source message inside `_build_admin_embed`? The outcomes don't carry channel_id directly; we'd need to look at `metadata.get("tool_calls")` for the matching call's input, or hydrate from `self.db.get_topic_editor_source_messages` like `render_topic_publish_units` already does.
- **OQ-2:** Does the citation `<URL>` wrap actually defeat Discord's same-channel collapse? (It's the standard way to suppress embeds, but the collapse-to-channel-pill is a separate rendering quirk.) If `<URL>` doesn't fix it, the fallback is to render each citation as a quoted single-line block with the bare ID + a separate per-citation jump URL on its own line.
- **OQ-3:** Does the rejection field have access to the tool *input* for the failing call? `_build_admin_embed` already builds `input_by_id = {call.get("id"): call.get("input") or {} for call in (metadata.get("tool_calls") or [])}` at `:2851` for the "tool calls" field — we should reuse the same lookup for rejections.

## Constraints

- **C-1:** No regression in the existing admin-embed field layout. Same field order, same field names, same conditional rendering.
- **C-2:** No new exceptions raised when tool input is missing `message_id` or `media_url` — graceful fallback to today's line.
- **C-3:** Embed-field value still fits in Discord's 1024-char limit. With multiple rejections, truncate as today (`value[:1000] + "\n…"`).
- **C-4:** Test suite passes (`pytest tests/test_topic_editor_runtime.py`).

## Done criteria

- **DC-1 (must):** Rejection lines in the admin embed include a clickable Discord jump URL to the source message when `message_id` is available, and include the raw `media_url` on its own line when the tool input has one.
- **DC-2 (must):** Per-block citation URLs in `render_topic_publish_units` are wrapped in `<…>` so Discord doesn't collapse same-channel self-references to a flat channel pill.
- **DC-3 (must):** All existing tests still pass; new tests cover both behaviors.
- **DC-4 (should):** One-paragraph note (top of `_handle_understand_video` or a sibling location) capturing the truncation investigation's conclusion.

## Touchpoints

Primary edits:
- `src/features/summarising/topic_editor.py` (around `:1340-1360`, `:2851`, `:2866-2877`, `:4307-4314`)
- `tests/test_topic_editor_runtime.py` (rejection-line + citation-line assertions)

Possibly affected (read but unlikely to edit):
- `src/features/admin_chat/tools.py` (only if the rejection field's source needs additional metadata that admin_chat already exposes)

## Reference: user-reported evidence

Rejection example (verbatim from admin chat):
```
rejections (1)
understand_video: failed to download media: 404 Client Error: Not Found for url: LTX-23-i2v_00416-audio.mp4
```

Publishing line (UUID is a topic_id; the user wants it linkable too — that's a separate, lower-priority ask, NOT in scope for this sprint):
```
publishing (1)
1dc7e5ff-0bf9-4503-aa4c-229f1b3efd06 · failed · media_sent 0 · source_media 0 · messages 3
```

Resources Sources line (verbatim, where ⁠…⁠ markers are Discord's channel-mention bidi wrap):
```
Sources: [1] ⁠ltx_resources⁠ [2] ⁠ltx_resources⁠ [3] ⁠ltx_resources⁠
```
