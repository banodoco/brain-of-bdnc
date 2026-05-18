# Sprint 4: Hardening, Metrics, Production Rollout, and Selective Runtime Standardisation

Profile intent: `thoughtful//medium @codex +feedback`.

Chain encoding note: this milestone should use first-class chain fields for `vendor: codex`, `depth: medium`, `with_feedback: true`, and `deepseek_provider: direct`.

## Source Plan

Use `docs/agent-loop-standardisation-and-live-update-social-plan.md`, especially:

- `Sprint 4: Hardening, Metrics, Production Rollout, and Selective Runtime Standardisation`
- `Keep agent_loop Narrow`
- `Definition of Done`

## Goal

Make the system operable in production and standardise only the broader agent runtime pieces that clearly reduce real drift.

This sprint is not a platform rewrite. Runtime standardisation should happen only where the previous sprints prove the shared contract is useful.

## Required Scope

Implement:

- Dashboards or status commands for social runs.
- Structured run metrics:
  - decisions by type;
  - media attached vs failed;
  - provider errors;
  - duplicate prevents;
  - human review queue size;
  - cost/tokens.
- Runbook for toggling `draft`, `queue`, and `publish`.
- Backfill/retry tooling for social runs.
- Production rollout checklist.
- Provider-neutral response normalization if it is still duplicated.
- Shared idempotency hooks for write/publish tools.
- Optional narrow migration of `TopicEditor`/`AdminChatAgent` internals only where it removes real drift.
- Preserve loop-specific prompts and policies.

## Media Requirements

Implement:

- Track media attachment success rate.
- Alert or surface cases where media-heavy posts repeatedly fail to attach media.
- Add regression fixtures for representative Discord attachment, external video, image, and embed cases.
- Keep media tools shared across admin chat, topic editor, and social loop.
- Ensure admin chat and social loop use the same underlying media refresh/download/understanding primitives.

## Explicit Non-Goals

- Do not build a god `AgentLoopPolicy`.
- Do not move domain-specific topic, media, or social policy into `src/common/agent_loop`.
- Do not migrate existing loops if it increases complexity.

## Acceptance Criteria

- Production operators can see what happened without reading raw logs.
- Media attachment failures are measurable and debuggable.
- The system can be safely rolled back to draft mode.
- Existing TopicEditor tests pass.
- Existing AdminChat tests pass.
- Existing social loop tests pass.
- Shared tooling reduces drift without making a global service locator or god abstraction.
