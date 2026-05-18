# Sprint 2: Media Understanding and Durable Queue Mode

Profile intent: `premium/standard/high @codex +feedback`.

Chain encoding note: this milestone should use first-class chain fields for `vendor: codex`, `depth: high`, and `with_feedback: true`.

## Source Plan

Use `docs/agent-loop-standardisation-and-live-update-social-plan.md`, especially:

- `Sprint 2: Media Understanding and Durable Queue Mode`
- `Queued Media Risk`
- `Provider Identity Risk`
- `Duplicate Protection Must Be Durable`
- `Definition of Done`

## Goal

Make media-aware social decisions and enable queue mode only when media attachment is durable across restarts/deploys.

This is the highest-risk sprint. Queued media must not depend on temporary local files that disappear after a restart or deploy.

## Required Scope

Implement:

- Extract/reuse image and video understanding handlers for the social loop.
- Add social-loop read tools:
  - `get_live_update_topic`
  - `get_source_messages`
  - `get_published_update_context`
  - `inspect_message_media`
  - `understand_image`
  - `understand_video`
  - `list_social_routes`
- Add typed tool result envelopes with truncation metadata.
- Run image/video understanding on selected or candidate media.
- Require media understanding for media-heavy posts unless explicitly skipped with a recorded reason.
- Extend `SocialSourceKind` with `live_update_social`.
- Add provider/account metadata for bot-owned live-update social posts.
- Add `enqueue_social_post`.
- Add publication duplicate checks against both `live_update_social_runs` and `social_publications`.
- Add queue-mode status and failure logging.

## Durable Media Requirement

Queue mode is not complete until queued image/video media can attach reliably after process restart or deploy.

Implement one durable strategy:

1. Upload resolved media to durable object storage and queue durable URLs; or
2. Store media ref identities and resolve/download them at publication execution time.

Also implement:

- Validate file size, content type, and provider compatibility before enqueueing.
- Store fallback reason when media cannot be attached.
- Ensure `SocialPublishService` or provider execution does not depend on temporary files for queued media.

## Explicit Non-Goals

- Do not enable immediate publish mode.
- Do not implement thread publishing.
- Do not silently degrade media-heavy posts into text-only posts.
- Do not rely on `duplicate_policy` alone; enforce durable guards.

## Acceptance Criteria

- The social loop can inspect and understand media from source messages.
- A media-heavy live update draft includes selected media refs plus understanding summaries.
- Tool drift tests fail if a social-loop media tool is advertised without a handler/backend dependency.
- In `LIVE_UPDATE_SOCIAL_MODE=queue`, an approved post creates a queued `social_publications` row.
- Queued text posts work.
- Queued media posts work after process restart or deploy.
- Publication rows include topic ID, source message IDs, live-update Discord message IDs, selected media refs, and social run ID.
- Duplicate reruns do not enqueue another post.
