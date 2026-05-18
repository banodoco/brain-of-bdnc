# Sprint 1: Draft Social Review, Tool Contract, and Admin Media Access

Profile intent: `thoughtful//high @codex +feedback`.

Chain encoding note: this milestone should use first-class chain fields for `vendor: codex`, `depth: high`, `with_feedback: true`, and `deepseek_provider: direct`.

## Source Plan

Use `docs/agent-loop-standardisation-and-live-update-social-plan.md`, especially:

- `Multi-Sprint Rollout Plan`
- `Sprint 1: Draft-Only Social Review, Tool Contract, and Admin Media Access`
- `Design Review Corrections`
- `Definition of Done`

## Goal

Create the post-live-update social review loop after successful Discord live-update posting, keep it non-publishing by default, and give it access to admin-equivalent media/social helpers immediately.

This sprint proves the product path and the minimal standardisation direction without migrating `TopicEditor` or `AdminChatAgent` to a shared runner.

## Required Scope

Implement:

- `live_update_social_runs` persistence.
- Durable duplicate guard for `topic_id + platform + action`.
- `LiveUpdateSocialAgent` in `draft` mode.
- Best-effort social review trigger after live-update publish results with `status in {"sent", "partial"}`.
- Reconstruct `publish_units` from topic summary plus source metadata.
- Minimal `ToolSpec` and `ToolBinding`.
- Social-loop conformance tests so advertised tools have handlers.
- Terminal decision tools:
  - `draft_social_post`
  - `skip_social_post`
  - `request_social_review`
- Trace/status logging for social runs.
- Extract or wrap admin-chat helpers needed by the social loop:
  - inspect a Discord message and fetch fresh attachment/embed media;
  - download a media URL;
  - refresh Discord CDN URLs;
  - list/resolve social routes;
  - call the canonical social publish service.

## Media Requirements

Media is not optional, but this sprint must not publish media yet.

Implement:

- Store selected media refs as stable identities, not CDN URLs.
- Record which media refs were considered, selected, skipped, or unresolved.
- Add `MediaRefIdentity` and `ResolvedMedia`.
- Resolve media refs to fresh URLs or local files on demand for draft/understanding.

Do not:

- Persist Discord CDN URLs as durable identity.
- Queue or publish social posts.
- Treat text-only fallback as success when media was expected.

## Explicit Non-Goals

- Do not migrate `TopicEditor` onto a shared runner.
- Do not migrate `AdminChatAgent` onto a shared runner.
- Do not implement queue or publish mode.
- Do not implement thread, quote, or reply social strategies.
- Do not add a broad generic `agent_runs` schema unless clearly necessary.

## Acceptance Criteria

- A successful live update creates one social review run.
- The run records `skip`, `draft`, or `needs_review`.
- Draft text and selected media ref identities are inspectable.
- The social loop can use admin-equivalent media inspection/download/refresh behavior through shared helpers.
- Duplicate reruns do not create a second social run for the same topic/platform/action.
- No social publication is created in default mode.
- Tests cover the trigger, persistence, duplicate guard, tool conformance, and media-ref recording.
