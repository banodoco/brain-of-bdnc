# Implementation Plan: Sync Discord `_resources` Posts into banodoco-website

## Overview

Promote qualifying Discord forum OPs in the 5 `_resources` channels (`resources`, `flux_resources`, `wan_resources`, `ltx_resources`, `acestep_resources`) into `assets` rows on the shared Supabase project (`ujlwuvkrxlvoswwkerdf`). Thread replies become `asset_comments`; reply attachments become `asset_comment_media`; OP attachments go to existing `asset_media`. Media is rehosted into Cloudflare Stream / Supabase Storage so rendering doesn't depend on Discord's expiring CDN signatures.

**Repository layout this touches:**
- `brain-of-bndc/` (Python, Railway) — already archives every message + reaction into `discord_messages` and `discord_reactions`; we read from here and do **not** modify the archive pipeline per the brief.
- `supabase/migrations/` (workspace root) — new tables + schema deltas live here.
- `supabase/functions/` — two new Edge Functions (`discord-resource-promoter`, `discord-media-importer`) plus shared helpers in `supabase/functions/_shared/`.
- `banodoco-website/` — ResourceModal gets "Made with this" + "Discussion" sections; new `useAssetComments` hook; imported-asset badge.

**Two important findings from the brain-of-bndc side** (both must be reflected in the plan):

1. **Members table is `members`**, not `discord_members` — `src/common/storage_handler.py:190-209` upserts to `members`. All FK references in new tables should point to `members(member_id)`.
2. **Archive does not persist Discord `attachment.id`**. `scripts/archive_discord.py:463-469` stores only `{url, filename}` per attachment. Dedup must parse the attachment ID out of the CDN URL path (`https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/{filename}?…`) — it's stable across CDN-signature refreshes. Fallback key: `(discord_message_id, filename)`.

**Reusable infrastructure confirmed:**
- `discord_messages.reaction_count` is an already-denormalized total (any emoji counts), so the `>= 5` trigger works directly.
- Granular per-emoji data is in `discord_reactions` if we later need it.
- Existing Supabase Edge Function `refresh-media-urls` (`structure.md:196-226`; logic in `src/common/discord_utils.py:90-160`) is the pattern for Discord CDN URL refresh inside edge-function runtime. The media importer can call it instead of re-implementing the refresh.

**Planning constraint I hit and you should know about:** This session is sandboxed to `brain-of-bndc/`, so I could not open `banodoco-website/src/pages/Resources/ResourceModal.tsx` or `supabase/migrations/` directly. Frontend file paths and the `assets`/`media`/`asset_media` schema are taken from the brief; I've flagged specific unknowns in `questions` that materially affect implementation (column names, classification enum values, existing storage bucket layout).

---

## Phase 1: Schema & Migrations (Foundation)

### Step 1: Audit existing `assets` / `media` / `asset_media` schema (`supabase/migrations/`)
**Scope:** Small
1. **Read** the current `supabase/migrations/` directory to find the most recent migration defining `assets`, `media`, and `asset_media`. Record: `assets` column list + constraints, `media.classification` CHECK enum values, existing RLS policies.
2. **Confirm** `members(member_id)` is the FK target (not `discord_members`). If the website is already referencing a view or different table name, plan to either match the website convention or add a compatibility view — decide before writing migrations.
3. **Capture** the existing Cloudflare Stream / Supabase Storage bucket conventions (`user-uploads/...`) used by manually-uploaded assets so Discord imports use matching paths.

### Step 2: Migration — extend `assets` (`supabase/migrations/<ts>_assets_discord_import_cols.sql`)
**Scope:** Small
1. **Add columns** (all nullable / with defaults so existing rows stay valid):
   ```sql
   ALTER TABLE assets
     ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
       CHECK (source IN ('manual','discord_import')),
     ADD COLUMN discord_thread_id BIGINT,
     ADD COLUMN discord_channel_id BIGINT,
     ADD COLUMN imported_at TIMESTAMPTZ,
     ADD COLUMN last_synced_at TIMESTAMPTZ,
     ADD COLUMN reactions_reached_threshold_at TIMESTAMPTZ,
     ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT FALSE;

   CREATE UNIQUE INDEX assets_discord_thread_id_unique
     ON assets(discord_thread_id)
     WHERE discord_thread_id IS NOT NULL;
   CREATE INDEX assets_source_idx ON assets(source);
   ```
2. **Backfill** nothing — existing rows stay `source='manual'` per the brief.

### Step 3: Migration — `asset_comments` + `asset_comment_media` (`supabase/migrations/<ts>_asset_comments.sql`)
**Scope:** Medium
1. **Create `asset_comments`** with the columns in the brief. Key points:
   ```sql
   CREATE TABLE asset_comments (
     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     asset_id UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
     discord_message_id BIGINT NOT NULL UNIQUE,
     discord_thread_id BIGINT NOT NULL,
     author_member_id BIGINT REFERENCES members(member_id),
     content TEXT,
     reply_to_comment_id UUID REFERENCES asset_comments(id) ON DELETE SET NULL,
     reply_to_discord_message_id BIGINT,
     reaction_count INT NOT NULL DEFAULT 0,
     discord_created_at TIMESTAMPTZ NOT NULL,
     discord_edited_at TIMESTAMPTZ,
     is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   CREATE INDEX asset_comments_asset_created_idx
     ON asset_comments(asset_id, discord_created_at);
   CREATE INDEX asset_comments_thread_idx ON asset_comments(discord_thread_id);
   ```
2. **Create `asset_comment_media`** with composite PK `(comment_id, media_id)` and `sort_order INT DEFAULT 0`.
3. **RLS**:
   ```sql
   ALTER TABLE asset_comments ENABLE ROW LEVEL SECURITY;
   CREATE POLICY "public read asset_comments" ON asset_comments
     FOR SELECT USING (is_deleted = FALSE);
   ALTER TABLE asset_comment_media ENABLE ROW LEVEL SECURITY;
   CREATE POLICY "public read asset_comment_media" ON asset_comment_media
     FOR SELECT USING (TRUE);
   ```
   Writes are service-role only (no INSERT/UPDATE/DELETE policies).

### Step 4: Migration — `media` classification + `media_import_jobs` + cursors (`supabase/migrations/<ts>_media_import_jobs.sql`)
**Scope:** Medium
1. **Extend `media.classification` CHECK** to include `'discord-comment'`. If the current enum is a CHECK list (not a Postgres enum type), rewrite the CHECK; if it's a Postgres `ENUM`, use `ALTER TYPE … ADD VALUE`. (Verify in Step 1.) Decide convention for OP attachments — see open question Q3; default assumption: OP attachments reuse the existing classification used by manually-uploaded primary/gallery media.
2. **Create `media_import_jobs`** exactly per brief with `status` CHECK `('pending','in_progress','done','failed','skipped')`, `attempts INT DEFAULT 0`, `locked_until TIMESTAMPTZ`, and a unique index on `discord_attachment_id`:
   ```sql
   CREATE UNIQUE INDEX media_import_jobs_attachment_unique
     ON media_import_jobs(discord_attachment_id)
     WHERE discord_attachment_id IS NOT NULL;
   CREATE INDEX media_import_jobs_claimable_idx
     ON media_import_jobs(status, locked_until)
     WHERE status IN ('pending','in_progress');
   ```
   RLS: enable, no policies (service-role only).
3. **Create `discord_resource_sync_cursors`**:
   ```sql
   CREATE TABLE discord_resource_sync_cursors (
     channel_id BIGINT PRIMARY KEY,
     last_processed_created_at TIMESTAMPTZ,
     last_backfill_cursor_at TIMESTAMPTZ,
     backfill_complete BOOLEAN NOT NULL DEFAULT FALSE,
     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   INSERT INTO discord_resource_sync_cursors (channel_id, last_backfill_cursor_at)
     VALUES (1149372684220768367, NOW() - INTERVAL '60 days'),
            (1275200992136400967, NOW() - INTERVAL '60 days'),
            (1373291419434877078, NOW() - INTERVAL '60 days'),
            (1457981813120176138, NOW() - INTERVAL '60 days'),
            (1472633200491626526, NOW() - INTERVAL '60 days')
     ON CONFLICT DO NOTHING;
   ```
   Service-role only.

### Step 5: Sanity-check the migration set locally (`supabase/migrations/`)
**Scope:** Small
1. **Run** `supabase db reset` on a scratch branch (or `supabase db diff`) and confirm the migrations apply cleanly and idempotently.
2. **Verify** existing `assets` / `media` tests in the website repo still pass (no column renames, only additions).

---

## Phase 2: Promoter Edge Function (dry-run first)

### Step 6: Scaffold shared helpers (`supabase/functions/_shared/`)
**Scope:** Small
1. **Add** `_shared/discord.ts` — Discord API client wrapper (`GET /channels/{channel_id}/messages/{message_id}`, `GET /channels/{thread_id}` for forum OP refresh), pulling `DISCORD_BOT_TOKEN` from env. Mirror the refresh pattern in `brain-of-bndc/src/common/discord_utils.py:90-160` — same ForumChannel → fetch thread by OP ID fallback.
2. **Add** `_shared/discordAttachments.ts` with a single helper:
   ```ts
   export function parseDiscordAttachmentId(cdnUrl: string): bigint | null {
     // Matches https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/{filename}?...
     const m = cdnUrl.match(/\/attachments\/\d+\/(\d+)\//);
     return m ? BigInt(m[1]) : null;
   }
   ```
3. **Add** `_shared/supabase.ts` — service-role Supabase client factory used by both functions.
4. **Add** `_shared/channelConfig.ts`:
   ```ts
   export const RESOURCE_CHANNELS = {
     1149372684220768367n: { key: 'resources',       assetType: 'tool' },
     1275200992136400967n: { key: 'flux_resources',  assetType: 'lora' },
     1373291419434877078n: { key: 'wan_resources',   assetType: 'lora' },
     1457981813120176138n: { key: 'ltx_resources',   assetType: 'lora' },
     1472633200491626526n: { key: 'acestep_resources', assetType: 'lora' },
   } as const;
   export const REACTION_THRESHOLD = 5;
   ```

### Step 7: Implement `discord-resource-promoter` (`supabase/functions/discord-resource-promoter/index.ts`)
**Scope:** Large
1. **Entrypoint** reads `DRY_RUN` env; on `true`, logs intended writes and returns — no DB mutations. Wall-clock budget ~25s; work is paginated per channel.
2. **For each channel** in `RESOURCE_CHANNELS`:
   - Load cursor (`last_processed_created_at`, `last_backfill_cursor_at`, `backfill_complete`).
   - **Query qualifying OPs**:
     ```sql
     SELECT message_id, thread_id, channel_id, author_id, content,
            attachments, reaction_count, created_at, edited_at, is_deleted
       FROM discord_messages
      WHERE channel_id = $1
        AND message_id = thread_id
        AND reaction_count >= 5
        AND created_at > $2
        AND is_deleted = FALSE
      ORDER BY created_at ASC
      LIMIT 50;
     ```
     Use `last_backfill_cursor_at` while `backfill_complete=FALSE`, else `last_processed_created_at`.
3. **Upsert `assets`** keyed on `discord_thread_id`:
   - `name` = first non-empty line of `content`, truncated to 120 chars.
   - `description` = full `content`.
   - `type` = `RESOURCE_CHANNELS[channel_id].assetType`.
   - `member_id` = `author_id`.
   - `admin_status='Listed'`, `source='discord_import'`, `discord_channel_id`, `discord_thread_id`, `reactions_reached_threshold_at = NOW()` **only on INSERT** (use `ON CONFLICT (discord_thread_id) DO UPDATE SET ... EXCLUDING reactions_reached_threshold_at`).
   - `imported_at` set only on INSERT; `last_synced_at = NOW()` always.
4. **Upsert replies** (`discord_messages WHERE thread_id = X AND message_id != thread_id`). Only process replies where the asset row now exists (avoids races).
   - Upsert `asset_comments` on `discord_message_id`.
   - Resolve `reply_to_comment_id` via `SELECT id FROM asset_comments WHERE discord_message_id = $reference_id` (nullable — may not be imported yet; store `reply_to_discord_message_id` unconditionally).
   - Propagate `is_deleted` from `discord_messages.is_deleted` so deletions hide comments.
   - Copy `edited_at` → `discord_edited_at`, `reaction_count`, `created_at` → `discord_created_at`.
5. **Enqueue media jobs** for every attachment on OP and replies:
   - Parse `attachment_id` from URL via `parseDiscordAttachmentId`. If null, log and skip (rare; will be monitored).
   - `INSERT … ON CONFLICT (discord_attachment_id) DO NOTHING` into `media_import_jobs` with `target_kind` (`'asset_media'` for OP, `'asset_comment_media'` for replies), `target_id` (asset_id or comment_id), `original_cdn_url` = attachment URL, `filename`.
6. **Member fallback**: before inserting comments/assets, `INSERT … ON CONFLICT DO NOTHING` into `members` using only `member_id`, `username=NULL`. The archive pipeline typically already has the row; this is a safety net (the `members` table has no NOT NULL columns beyond the primary key per the brain-of-bndc schema).
7. **Advance cursor** after each channel, even on partial progress. If backfill page returned `<50` rows and the newest `created_at` within the page is older than the OP's actual created_at, mark `backfill_complete=TRUE` once we've caught up to `NOW() - INTERVAL '10 min'`.
8. **Time budget**: if `performance.now()` exceeds ~25s, break out of the channel loop and return progress stats. pg_cron will re-invoke in 10 min; stuck channels naturally fairness-rotate by sorting on cursor-age.
9. **Structured log** JSON with `{channel_id, oPs_seen, assets_inserted, assets_updated, comments_inserted, media_jobs_enqueued, elapsed_ms, dry_run}` — caller can aggregate from Supabase logs.

### Step 8: Local dry-run verification (`supabase/functions/discord-resource-promoter/`)
**Scope:** Small
1. **Deploy** to staging project with `DRY_RUN=true`.
2. **Invoke** once via `curl` and confirm log output shows expected OP count matching the ~15 threads in the last 30 days.
3. **Deploy** with `DRY_RUN=false`, **manually** invoke, then verify `assets`, `asset_comments`, and `media_import_jobs` rows exist — but **no `media` rows yet** (importer not deployed).
4. **Rerun** the promoter twice and confirm zero new rows (idempotency).

---

## Phase 3: Media Importer Edge Function

### Step 9: Implement `discord-media-importer` (`supabase/functions/discord-media-importer/index.ts`)
**Scope:** Large
1. **Claim jobs** atomically (single UPDATE ... RETURNING):
   ```sql
   UPDATE media_import_jobs
      SET status='in_progress',
          locked_until = NOW() + INTERVAL '10 minutes',
          attempts = attempts + 1,
          updated_at = NOW()
    WHERE id IN (
      SELECT id FROM media_import_jobs
       WHERE status = 'pending'
          OR (status = 'in_progress' AND locked_until < NOW())
       ORDER BY created_at ASC
       LIMIT 10
       FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
   ```
2. **Refresh CDN URL** by calling the existing `refresh-media-urls` function (or inlining the logic in `_shared/discord.ts`) — pass `message_id`, receive fresh signed URL. If the original URL from the archive is still within its `?ex=` expiry, we can skip the refresh for speed.
3. **Classify by content-type** (HEAD request after refresh):
   - `video/*`, `image/gif` (animated): **stream to Cloudflare Stream** via tus. Poll `/stream/{uid}` until `readyToStream`; capture `playback.hls`, `thumbnail`, `duration`.
   - `image/*`: **stream to Supabase Storage** at `user-uploads/discord-imports/{discord_message_id}/{attachment_id}-{filename}`. Thumbnail via Supabase's image transform (if enabled) or inline resize.
   - Anything else (`application/zip`, `application/json`, `text/*`): upload to same Storage path, no thumbnail.
4. **Insert `media` row**:
   - `classification = 'discord-comment'` when `target_kind='asset_comment_media'`; for `target_kind='asset_media'`, use the website's existing OP/post classification (see Q3).
   - `member_id` from the source Discord message's author_id.
   - URL columns populated with Cloudflare HLS / Storage public URL.
   - `metadata` JSONB:
     ```json
     {
       "discord_message_id": "…",
       "discord_channel_id": "…",
       "discord_attachment_id": "…",
       "original_cdn_url": "…",
       "imported_at": "…"
     }
     ```
5. **Insert junction row** — `asset_media` or `asset_comment_media` (with `sort_order` = attachment position in Discord message).
6. **Mark job done**: `UPDATE media_import_jobs SET status='done', media_id = $id, updated_at=NOW() WHERE id = $job_id`.
7. **On failure**: catch, log, `UPDATE … SET status='pending', last_error=$err, locked_until = NOW() + INTERVAL '<exp_backoff>'`. Exponential backoff = `LEAST(60 * 2^attempts, 3600)` seconds. After `attempts >= 5`, set `status='failed'` (stays in table for dashboard visibility).
8. **Time budget** ~350s; claim loop stops when budget exhausted. Unfinished `in_progress` jobs are naturally re-picked next run after `locked_until` expires.

### Step 10: Verify importer end-to-end on one attachment (`supabase/functions/discord-media-importer/`)
**Scope:** Small
1. **Manually insert** a single `media_import_jobs` row pointing at a known Discord attachment from one of the _resources channels.
2. **Invoke** importer; confirm `media` row, junction row, and job `status='done'` exist.
3. **Verify** the rehosted URL loads (HLS manifest for video, direct Storage URL for image).
4. **Re-run** importer to confirm no additional work is done (claim query returns 0 rows).

---

## Phase 4: pg_cron wiring + Full Backfill

### Step 11: Register cron schedules (`supabase/migrations/<ts>_schedule_discord_sync_crons.sql`)
**Scope:** Small
1. **Enable** `pg_cron` + `pg_net` if not already enabled.
2. **Add** both schedules:
   ```sql
   SELECT cron.schedule(
     'discord-resource-promoter',
     '*/10 * * * *',
     $$
     SELECT net.http_post(
       url     := 'https://<project>.supabase.co/functions/v1/discord-resource-promoter',
       headers := jsonb_build_object(
         'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
         'Content-Type',  'application/json'
       ),
       body    := '{}'::jsonb
     );
     $$
   );
   SELECT cron.schedule(
     'discord-media-importer',
     '*/2 * * * *',
     $$
     SELECT net.http_post(
       url     := 'https://<project>.supabase.co/functions/v1/discord-media-importer',
       headers := jsonb_build_object(
         'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
         'Content-Type',  'application/json'
       ),
       body    := '{}'::jsonb
     );
     $$
   );
   ```
3. **Do not** commit the service-role key literal — rely on the `vault`-backed `app.settings.service_role_key` pattern already used in the project (confirm in Q5).

### Step 12: Run the 60-day backfill end-to-end
**Scope:** Medium
1. **Let** cron run for one full cycle (~1 hour). Watch `media_import_jobs` counts by status and `assets` / `asset_comments` growth.
2. **Sanity-check** a handful of asset rows in each of the 5 channels: correct `type`, correct `name`, correct `member_id`, non-empty `asset_media` once importer catches up.
3. **Compare** expected volume — ~15 OPs in last 30 days × 2 = ~30 assets over 60 days, plus replies × attachments.

---

## Phase 5: Frontend (banodoco-website)

### Step 13: Add `useAssetComments` hook (`banodoco-website/src/hooks/useAssetComments.ts`)
**Scope:** Small
1. **Mirror** the existing asset-fetching hook pattern (e.g. `useAsset` / `useAssets`). Pull from Supabase with a single query:
   ```ts
   const { data } = await supabase
     .from('asset_comments')
     .select(`
       id, content, discord_message_id, discord_thread_id, reaction_count,
       discord_created_at, discord_edited_at, is_deleted,
       reply_to_comment_id, reply_to_discord_message_id,
       author:members!author_member_id(member_id, global_name, avatar_url, username),
       media:asset_comment_media(
         sort_order,
         media:media(*)
       )
     `)
     .eq('asset_id', assetId)
     .eq('is_deleted', false)
     .order('discord_created_at', { ascending: true });
   ```
2. **Return** `{ comments, isLoading, error }` with minimal client-side shape normalization.

### Step 14: Update `ResourceModal` (`banodoco-website/src/pages/Resources/ResourceModal.tsx`)
**Scope:** Medium
1. **Keep** the existing gallery (primary media + `asset_media`) unchanged.
2. **Add "Made with this" section** below gallery — when `asset.source === 'discord_import'`, render a grid of every `media` row reachable via `asset_comment_media` for this asset (newest first). Data source: flatten `comments[*].media[*].media` and sort by parent comment's `discord_created_at` desc. Clicking a tile opens the existing lightbox anchored to the parent comment in the discussion below.
3. **Add "Discussion" section** — flat chronological list of comments. Each row:
   - Author chip: `avatar_url` + `global_name ?? username` from `members`.
   - Timestamp: `discord_created_at` (relative).
   - "Replying to @name" chip with a 1-line excerpt when `reply_to_comment_id` resolves, plain "In reply to a Discord message" fallback when only `reply_to_discord_message_id` is set.
   - `content` rendered via the app's existing markdown renderer (confirm Q6).
   - Inline thumbnail grid from `comment.media`, click → lightbox.
   - `reaction_count` chip.
   - "View on Discord" link: `https://discord.com/channels/{guild_id}/{discord_channel_id}/{discord_message_id}` (use `asset.discord_channel_id` + `comment.discord_message_id`; guild_id is a constant — see Q7).
4. **Gate** both new sections on `asset.source === 'discord_import'`. Manual assets render as before.

### Step 15: Add "From Discord" badge on resource card (`banodoco-website/src/pages/Resources/<card component>.tsx`)
**Scope:** Small
1. **Show** a subtle badge when `asset.source === 'discord_import'` — label: channel friendly name (`flux_resources` → "Flux"). Map via `RESOURCE_CHANNELS`.

### Step 16: Type updates (`banodoco-website/src/lib/database.types.ts` or equivalent)
**Scope:** Small
1. **Regenerate** Supabase types (`supabase gen types typescript ...`) so new columns + tables are present in the generated file.
2. **Update** any hand-rolled `Asset` / `Media` interfaces touched by the new fields.

---

## Phase 6: Observability + Ops

### Step 17: Health-check SQL snippets (`supabase/sql/health_checks.sql` or docs)
**Scope:** Small
1. **Persist** reusable queries for the ops dashboard (file can live in website repo under a docs/sql folder, or in `supabase/` — verify Q8):
   ```sql
   -- Jobs stuck in_progress past lock
   SELECT * FROM media_import_jobs
    WHERE status='in_progress' AND locked_until < NOW();
   -- Failures in last 24h
   SELECT COUNT(*), MIN(created_at) FROM media_import_jobs
    WHERE status='failed' AND updated_at > NOW() - INTERVAL '24h';
   -- Oldest pending
   SELECT channel_id, NOW() - updated_at AS staleness
     FROM discord_resource_sync_cursors;
   ```

### Step 18: Write logs to `system_logs` on failures
**Scope:** Small
1. **Both edge functions** emit a `system_logs` row on any unhandled failure with component `'discord_resource_promoter'` / `'discord_media_importer'`, level `'error'`, payload = job/cursor + error text. (Confirm `system_logs` insert shape — Q9.)

### Step 19: Rollback documentation (`supabase/migrations/ROLLBACK.md` or PR description)
**Scope:** Small
1. **Document** per-phase rollback:
   - Phase 1 (schema): new tables and columns are additive — safe to `DROP TABLE asset_comments CASCADE; DROP TABLE asset_comment_media CASCADE; DROP TABLE media_import_jobs CASCADE; DROP TABLE discord_resource_sync_cursors;` and `ALTER TABLE assets DROP COLUMN ...`. Drop `'discord-comment'` classification last (check for references).
   - Phase 2 (promoter): `cron.unschedule('discord-resource-promoter')`; optionally `UPDATE assets SET is_hidden=TRUE WHERE source='discord_import'` as a soft kill, or `DELETE FROM assets WHERE source='discord_import'` for a hard rollback.
   - Phase 3 (importer): `cron.unschedule('discord-media-importer')`. Rehosted Cloudflare Stream / Storage objects need manual deletion if rolling back hard.
   - Phase 5 (frontend): feature-flag behind a constant or revert PR; the badge and sections are additive.

---

## Execution Order

1. Phase 1 Steps 1–5 (schema) — foundation, must merge before any runtime code can write.
2. Phase 2 Steps 6–8 (promoter + dry-run) — validates DB mappings and query shape before any CDN traffic.
3. Phase 3 Steps 9–10 (importer) — one-attachment smoke test before full cron.
4. Phase 4 Steps 11–12 (cron + backfill) — turn it on for real.
5. Phase 5 Steps 13–16 (frontend) — can start in parallel with Phase 4 once Phase 3 is green (real data to render).
6. Phase 6 Steps 17–19 (observability + docs) — land alongside the cron cutover.

## Validation Order

1. `supabase db reset` on a scratch branch after Phase 1 to prove migrations apply cleanly.
2. Promoter **dry-run** on staging after Phase 2 — confirm log matches ~15-OP expectation with zero DB writes.
3. Promoter **live single-invocation** after Phase 2 — assets + comments present, `media_import_jobs` enqueued, zero `media` rows (importer not deployed).
4. Importer **single-job smoke test** in Phase 3 — rehosted URL loads, job marks done, rerun is a no-op.
5. **Cron cycle** observation in Phase 4 — watch `media_import_jobs` drain; no `failed` status beyond isolated attachments (log each reason).
6. **Frontend render** smoke test in Phase 5 — open one imported asset in the modal, confirm "Made with this" + "Discussion" + badge + View-on-Discord links resolve.
7. **Idempotency re-run** after Phase 5 — run promoter twice and importer twice, expect zero net new rows.
