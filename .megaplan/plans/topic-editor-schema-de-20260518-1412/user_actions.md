# User Actions

## After Execute

- **U1**: On the next dev-environment live-update cron tick after merge, manually eyeball the BNDC live-update feed: confirm that a single-author single-video topic posts as `title + one paragraph + media attached` (no separate "The Video" / "Audio" / "Community Reaction" sections), and that a genuinely multi-creator topic still posts as a multi-section document. This is success-criterion 19 (info priority) and cannot be verified in CI.
  Rationale: The validation requires inspecting rendered Discord output on the live dev cron — the executor cannot drive a Discord UI render or wait for a cron tick.
