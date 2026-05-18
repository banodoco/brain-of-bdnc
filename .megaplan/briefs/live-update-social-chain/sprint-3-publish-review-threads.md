# Sprint 3: Publish Mode, Review Controls, Threads, and Richer Social Strategy

Profile intent: `thoughtful//high @codex +feedback`.

Chain encoding note: this milestone should use first-class chain fields for `vendor: codex`, `depth: high`, `with_feedback: true`, and `deepseek_provider: direct`.

## Source Plan

Use `docs/agent-loop-standardisation-and-live-update-social-plan.md`, especially:

- `Sprint 3: Publish Mode, Review Controls, Threads, and Richer Social Strategy`
- `Thread Publishing Should Wait`
- `Social Publishing Contract`
- `Definition of Done`

## Goal

Move from queue-only safety to controlled publishing, then add richer social strategy once single-post media is reliable.

Only start this sprint after Sprint 2 has proven durable queued media attachment.

## Required Scope

Implement:

- `publish_social_post`, gated by `LIVE_UPDATE_SOCIAL_MODE=publish`.
- Human-review surface for `needs_review` and draft decisions.
- Admin/status tools to inspect recent social runs and publication outcomes.
- Failure classification:
  - media resolution failed;
  - provider rejected media;
  - route missing;
  - duplicate prevented;
  - model skipped;
  - human review required.
- Social route/account validation before publish.
- Safe retry behavior for failed social runs.
- Thread draft decisions.
- Immediate thread publishing by reply-chaining through `SocialPublishService`.
- Quote/reply decisions for cases with an existing relevant social post.
- `find_existing_social_posts`.
- Content-level duplicate similarity checks.
- A decision on whether queued threads need a native grouped/thread publication model.

## Media Requirements

Implement:

- Confirm attached media appears on the provider result, not just in the queued request.
- Record final media attachment outcome per publication.
- Add explicit text-only fallback only when media was genuinely unavailable or intentionally skipped.
- Support attaching media to the right post in a thread.
- Ensure replies default to text-only only when that is the intended strategy, not because media was lost.
- Add trace output showing which thread item owns which media refs.

## Explicit Non-Goals

- Do not add queued thread support unless the design is settled and safe.
- Do not let quote/reply actions reattach duplicate media accidentally.
- Do not use a separate Twitter/X transport outside `SocialPublishService`.

## Acceptance Criteria

- Publish mode can post a single live-update social post with media attached.
- Operators can inspect exactly what was posted, which media attached, and why.
- Failed media attachment is visible and does not silently degrade into an unexplained text-only post.
- A multi-section live update can become either one post or a short thread.
- Thread media associations are explicit and test-covered.
- Quote/reply actions do not accidentally reattach duplicate media.
