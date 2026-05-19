# Brief: Move hourly archive in-process to eliminate duplicate Discord gateway connections

## Outcome

Eliminate the duplicate Discord gateway connection caused by `scripts/archive_discord.py` running as a subprocess that re-authenticates with the same bot token. The hourly archive runs in-process inside the main bot, sharing its existing `discord.Client` gateway connection. After this change, the production bot stops emitting spurious "Bot restarted" DMs every time the hourly archive runs.

## Why this matters (context)

Investigation summary (already done, do not re-derive):

- The main bot fires a "Bot restarted" DM from `on_ready` in `src/common/base_bot.py:183`. `on_ready` runs on **every fresh Discord gateway session**, not only on process start.
- The Railway production process has been continuously running on deployment `66a26102` since 2026-05-17 19:23 UTC, yet today (2026-05-18) three "Bot is ready" events fired at 10:26, 13:29, and 14:25 UTC — none of which were actual process restarts.
- Each hour, `main.py:339` calls `run_archive_script` (`src/common/archive_runner.py:87`), which spawns `python scripts/archive_discord.py` as a subprocess. That subprocess calls `bot.start(os.getenv('DISCORD_BOT_TOKEN'))` at `scripts/archive_discord.py:1400`, opening a **second** Discord gateway connection with the **same** bot token.
- Discord allows only one live gateway per bot. The dual login causes session churn on the main bot; in addition, the archive subprocess does loop-blocking work that triggers `heartbeat blocked for more than 10 seconds` warnings (seen in `system_logs` at 02:26, 04:26, 10:25, 11:26, 11:27, 13:26 UTC today), making the churn worse.

The fix is structural: stop opening a second gateway. Run the archive logic in the main bot process, using the main bot's existing client.

## Scope (IN)

1. **Extract archive logic from the `discord.Client` subclass into a plain async runner.**
   - Today `DiscordArchiveBot` (in `scripts/archive_discord.py`, ~1455 lines) is a `discord.Client` subclass. Its `on_ready` (`:520`) collects channels/threads, then iterates `archive_channel(item.id)` (`:683`), which dispatches to `_archive_channel_incremental` or `_archive_channel_date_range`.
   - Move the archiving logic into a plain async class (e.g. `ArchiveTask` or `InProcessArchiver`) that takes a `discord.Client` as a constructor argument and uses `bot.get_guild(...)`, `bot.get_channel(...)`, etc. on the **passed-in** client.
   - The state currently held on the Client subclass (db queue, db worker thread, rate limiter, member cache, target channel ids, days_limit / start_date / end_date, in_depth, fetch_reactions, fast_fill, summary thread cache, total_messages_archived) moves onto the new class.

2. **Wire the new runner into the main bot's hourly loop.**
   - In `main.py:328-348`, the `hourly_message_fetch` task currently calls `await run_archive_script(days=1, dev_mode=args.dev, logger=logger, guild_id=gid)` per guild.
   - Replace that with a direct call to the new in-process runner using the main bot's client (e.g. `await ArchiveTask(bot, days=1, guild_id=gid, ...).run()`).

3. **Keep the standalone CLI entry point in `scripts/archive_discord.py` working.**
   - `python scripts/archive_discord.py --days 1 [--start-date ... --end-date ...] [--channels ...] [--guild-id ...] [--in-depth] [--dev]` is still used for ad-hoc backfills.
   - The CLI path constructs a fresh, short-lived `discord.Client`, connects it via gateway (fine for a one-shot CLI run when the bot is NOT also live on Railway against the same token), runs the new runner against that client, then disconnects. The existing argparse surface in `scripts/archive_discord.py` (around `:1340`+) must remain backwards-compatible.

4. **Delete the subprocess plumbing.**
   - Remove (or stub out for one release as a deprecation shim) `src/common/archive_runner.py`'s `ArchiveRunner._run_subprocess` path and its `[Archive] ...` log-prefix capture loop.
   - Remove the `run_archive_script` helper in `main.py:91-105` if no other callers exist, or rewrite it to call the in-process runner directly.

5. **Audit blocking work and offload where needed.**
   - Today `_db_worker` (`scripts/archive_discord.py:203`) already runs DB ops on a background thread — preserve this pattern.
   - Identify any **other** sync calls inside the per-channel and per-message loops (in `archive_channel`, `_archive_channel_incremental`, `_archive_channel_date_range`, and the message-processing inner loop) that could block the asyncio event loop. The existing `heartbeat blocked` warnings prove at least one such site exists today. Wrap any blocking I/O (sync Supabase calls, sync HTTP, sync file I/O) in `asyncio.to_thread`.
   - Add a periodic `await asyncio.sleep(0)` inside the inner per-message loops as cheap insurance against starvation under bursty channels.

6. **Migrate logging.**
   - Today the subprocess's stdout is line-captured by the parent and re-emitted with a `[Archive] ` prefix (`src/common/archive_runner.py:113`). In-process, archive logs go straight to the main bot's existing `DiscordBot` logger. The `[Archive] ` prefix can be dropped — anything currently relying on the prefix to grep `system_logs` will need to use a different filter (the `archive` module name suffices). Note this in the brief's done criteria but don't change downstream consumers in this sprint.

## Anti-scope (OUT — do NOT touch)

- **Do NOT change the `on_ready` notification logic** in `src/common/base_bot.py:183`. That is a separate concern (it would only ever be a band-aid given the underlying gateway churn is the real bug).
- **Do NOT restructure `src/common/log_handler.py`.** The `SupabaseLogHandler` already has a background thread + queue and is non-blocking; just confirm it. No refactor of the logging system.
- **Do NOT touch other cogs**, `admin_chat`, `topic_editor`, `live-update editor`, `summarising`, `gating`, `competition`, `content`, `grants`, etc. They are out of scope.
- **Do NOT add a second bot token.** The fix is to stop opening a second gateway, not to register a second bot account.
- **Do NOT introduce REST-only mode** as a fallback. We're committing to the in-process design; if the audit in #5 reveals blocking issues, fix them in-place rather than backing out to REST-only.
- **Do NOT change the per-guild hourly schedule semantics** (`tasks.loop(hours=1)`, `before_loop` gating on `bot.summary_completed`). The trigger is fine; only the work inside changes.

## Locked decisions

- **LD-1:** Refactor approach is **in-process**, not REST-only-subprocess and not second-token. (Decided after weighing options — see Outcome / Why this matters.)
- **LD-2:** The new runner is a **plain async class that takes the bot as a constructor argument**, not a Cog and not a Client subclass. Composition over inheritance.
- **LD-3:** The standalone CLI in `scripts/archive_discord.py` **stays working** for ad-hoc backfills. CLI mode constructs its own short-lived `discord.Client`; production mode reuses the main bot's client.
- **LD-4:** The existing `_db_worker` background thread + queue pattern **stays**; do not refactor message-write persistence to a different concurrency model in this sprint.
- **LD-5:** Subprocess shim (`src/common/archive_runner.py`) is **deleted in this sprint**, not kept around as a fallback. (Reversibility is via git, not a feature flag.)

## Open questions for the planner to resolve

- **OQ-1:** Which specific sync code paths in the archive currently block the asyncio event loop? The `heartbeat blocked` warnings around `_archive_channel_incremental` + `logger.info` (see stack at `scripts/archive_discord.py:1084` → `logging/__init__.py:1477`) suggest the Supabase log handler may not be as non-blocking as expected under load. Confirm `SupabaseLogHandler.emit` is truly non-blocking, and identify any other blocking sites. Produce a per-call-site verdict before writing fixes.
- **OQ-2:** How should the new runner handle the discord.py member/thread cache warm-up that today happens implicitly via the subprocess's gateway READY? In-process, the main bot's client is already warmed up — but the runner needs to be robust to channels/threads that exist but aren't in cache yet (e.g. forum threads). Decide whether to call `bot.fetch_channel(id)` on cache misses, or `guild.fetch_active_threads()`, etc.
- **OQ-3:** How does the new runner signal completion / surface errors to the hourly loop in `main.py:328`? Today the subprocess returns a bool from `ArchiveRunner.run_archive`. The new runner should preserve that contract (or a richer one — e.g. a small result object with `success: bool`, `messages_archived: int`, `duration_seconds: float`) so observability/log lines stay equivalent.
- **OQ-4:** What's the right behavior if one guild's archive raises mid-run? Today each guild gets its own subprocess, so a crash in one doesn't take down the loop. In-process, an unhandled exception could disrupt the per-guild loop. The new runner needs an explicit try/except around each guild's `await runner.run()` call (consistent with `main.py:341` today).

## Constraints

- **C-1:** No regression in archive throughput / messages-archived-per-hour relative to current production baseline.
- **C-2:** No new `heartbeat blocked for more than N seconds` warnings on the **main bot's** gateway after deploy. (Pre-fix, these warnings appear on the archive subprocess's gateway; post-fix, the only gateway is the main bot's, so the bar moves to "no warnings at all".)
- **C-3:** No new `Bot is ready` log lines except after actual Railway deployments. Verified by checking that `Bot is ready` count over a 24h post-deploy window matches the `Starting deployment` count over the same window.
- **C-4:** `python scripts/archive_discord.py --days 1` from a developer laptop still works for ad-hoc backfills (against the dev guild / dev token).

## Done criteria

- **DC-1 (must):** Over a 6+ hour window after deploy, `system_logs` shows **zero** new `Bot is ready! Logged in as BNDC` lines outside of explicit Railway deploys, AND **zero** new `heartbeat blocked` warnings.
- **DC-2 (must):** Over the same window, the count of new rows in `discord_messages` (filtered to the configured archived guilds) is within ±10% of the pre-refactor hourly baseline. (Establish baseline by querying current state before merge.)
- **DC-3 (must):** `src/common/archive_runner.py` is removed (or reduced to a one-line deprecation re-export); `main.py:91-105` no longer spawns a subprocess for archiving; the hourly loop calls the new in-process runner directly.
- **DC-4 (must):** `python scripts/archive_discord.py --days 1 --guild-id <dev_guild_id>` succeeds locally against a dev token and writes new rows to the dev guild's archive.
- **DC-5 (should):** New runner exposes a small result object (or equivalent log lines) summarizing duration, messages archived, and any per-channel errors, so the hourly loop's logs in `main.py:346` (`Hourly message fetch completed successfully`) get correspondingly richer.
- **DC-6 (info):** A brief one-paragraph note in `docs/` (or as a code comment at the top of the new runner module) explaining why archive lives in-process now and what the failure mode would be if someone re-introduced a separate gateway login.

## Touchpoints

Primary edits:
- `scripts/archive_discord.py` (~1455 LOC, biggest refactor target)
- `src/common/archive_runner.py` (delete or stub)
- `main.py:91-105` (drop `run_archive_script` subprocess helper)
- `main.py:328-360` (rewire `hourly_message_fetch` to call new runner)

Possibly affected (read but likely not edited):
- `src/common/base_bot.py` (verify nothing assumes a separate archive process)
- `src/common/log_handler.py` (confirm `SupabaseLogHandler` is non-blocking; do NOT refactor)

New files (optional, planner's call):
- `src/features/archiving/archive_task.py` (new home for the extracted runner — module name negotiable)

## Reference: production evidence

Today's three spurious "Bot restarted" DMs and their causes:

| Local time (UTC+2) | `Bot is ready` ts (UTC) | Deployment | Preceded by |
|---|---|---|---|
| 12:26 | 10:26:31 | `66a26102` | `[Archive] heartbeat blocked` at 10:25:59; two `Logged in as BNDC#9536` (10:24:07, 10:24:16) |
| 15:29 | 13:29:18 | `66a26102` | `[Archive] heartbeat blocked` at 13:26:02, then `session has been invalidated` at 13:29:08 |
| 16:25 | 14:25:54 | `66a26102` | Hourly fetch started at 14:23:44 |

Last actual `🚀 Starting deployment` was 2026-05-17 19:23:32 (also `66a26102`). The process has been continuous since then.
