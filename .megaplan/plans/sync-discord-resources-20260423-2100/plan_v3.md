# Implementation Plan: Sync Discord `_resources` Posts into banodoco-website (v3)

## Overview

Promote qualifying Discord forum OPs in the 5 `_resources` channels into `assets` rows on the shared Supabase project (`ujlwuvkrxlvoswwkerdf`), with thread replies as `asset_comments`, reply attachments as `asset_comment_media`, and OP attachments into existing `asset_media`. Media is rehosted into Cloudflare Stream (video) or Supabase Storage (images/other).

**Revision summary v2 → v3** — 13 flags remained after v2. Concrete decisions this revision locks in:

1. **Discovery has no time window.** v2 kept `created_at > NOW() - INTERVAL '60 days'`, which still missed OPs older than 60 days that reach the threshold later. v3 drops the window. The anti-join on `assets.discord_thread_id IS NULL` plus `reaction_count >= 5` is the only filter — cheap (scan bounded by channel_id + threshold), correct (any qualifying OP ever becomes an asset), and closes FLAG-001 / correctness-1 once and for all.
2. **Promoter has row-level FK safety.** The set-based `INSERT … SELECT` used `WHERE EXISTS (SELECT 1 FROM members WHERE member_id = src.author_id)` prefilters, plus a separate `system_logs` write for each filtered row naming the missing `member_id`. One bad author never aborts the whole sync. Addresses FLAG-006 / correctness-2.
3. **Cloudflare duration is dropped from the contract.** `cloudflare-stream-webhook/index.ts:210-240` only writes `cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, and `storage_provider` — no duration. v3 does not promise duration. If it's later wanted, a one-line webhook change is a separate task. Addresses FLAG-005 / all_locations-1.
4. **Vault replaced by a `internal.secrets` table.** The repo has no `vault.create_secret` pattern and `supabase/config.toml:45-46` has `db.vault` commented. v3 ships a self-contained migration that creates `internal.secrets` (postgres-owned, no RLS exposure) + `internal.get_service_role_key()` SECURITY DEFINER accessor. The migration applies cleanly on `supabase db reset` without any pre-existing secret; the operator runs a one-line `INSERT INTO internal.secrets …` once at provisioning time (documented in the migration header). Addresses FLAG-005 / all_locations-2.
5. **No new lightbox in v1.** The existing `ResourceDetail` has only `activeMedia`/`setActiveMedia` and no comment-specific lightbox. v3 downgrades comment attachment tiles and "Made with this" tiles to **anchor tags that open the rehosted media URL in a new tab** (`target="_blank" rel="noopener"`). A follow-up task is flagged for a real lightbox. Addresses FLAG-007 / scope / callers.
6. **PL/pgSQL promoter is a documented design deviation from the brief.** The brief asked for an edge-function promoter + cursor table; v2 pivoted to a SQL-native function with an anti-join replacing the cursor. v3 makes this an explicit settled decision in the Overview with rationale: matches the existing `schedule_priority_scores_cron.sql` pattern, removes the edge-function auth dependency, eliminates 30s-wall-clock risk, and the anti-join covers exactly what the cursor used to do. Addresses `issue_hints`.

**Settled design decisions** (do not re-litigate):

- FKs target `members(member_id)` (table renamed from `discord_members`).
- Discord `attachment.id` parsed from CDN URL `/attachments/{channel}/{id}/{filename}` (stable across signature refresh).
- Promoter is PL/pgSQL; no cursor table; anti-join on `assets.discord_thread_id IS NULL` replaces cursor.
- Media importer reuses `cloudflare-stream-webhook` for Stream completion (HLS URL + thumbnail; **not** duration).
- Asset type mapping: channel `1149372684220768367` → `workflow`; other four → `lora`.
- Universal markdown rendering via shared `AssetDescription` component (react-markdown + remark-gfm, no custom embed tokens).
- `useResources`, `useCommunityResources`, `useCommunityResource`, `useUserProfile` all SELECT `source`, `discord_channel_id`, `is_hidden` and filter `is_hidden=false`.
- `asset_comment_media` and `asset_media` have `is_deleted` columns for attachment-removal-on-edit.
- No member fallback insert; promoter prefilters on member existence.
- Discord vs. web secret delivery uses a migration-seeded `internal.secrets` table, not `supabase_vault`.

**Sandbox caveat:** This planning session is restricted to `brain-of-bndc/`; precise `banodoco-website/` and `supabase/` references are taken from gate evidence. Step 1 re-verifies each before migrations are written.

---

## Phase 1: Schema Migrations

### Step 1: Audit current shapes (`supabase/migrations/`, `banodoco-website/src/`, `supabase/functions/`)
**Scope:** Small
1. **Re-read** these exact files: `supabase/migrations/20251101000000_create_discord_tables.sql` (members DDL), `20260319100000_fix_sync_reactors_trigger.sql` (reaction-count trigger), `20260403000005_schedule_priority_scores_cron.sql` (cron pattern), `supabase/functions/cloudflare-stream-webhook/index.ts:210-240` (webhook columns), `supabase/functions/cloudflare-stream-backfill/index.ts` (direct-upload initiation pattern), most-recent `assets`/`media`/`asset_media` migration (column list + CHECK constraints), `banodoco-website/src/pages/SubmitResource/index.tsx:10-13` (type enum), `ResourceDetail/index.tsx:59-74,166-169,228-245` (state + plain-text description + media-switch render), `components/posts/PostBodyRenderer.tsx` (markdown pipeline).
2. **Capture** the exact literal value used by manual OP/primary media in `media.classification` (for OP imports) and the current enum/CHECK shape.
3. **Capture** whether `cloudflare-stream-webhook` keys `media` rows by a `cloudflare_uid` column or by some other identifier; capture the `media` columns it sets (`cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, `storage_provider`) and explicitly note that `duration` is **not** among them.

### Step 2: Migration — extend `assets` (`supabase/migrations/<ts>_assets_discord_import_cols.sql`)
**Scope:** Small
1. **Add columns** (additive; existing rows stay valid):
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
     ON assets(discord_thread_id) WHERE discord_thread_id IS NOT NULL;
   CREATE INDEX assets_source_idx ON assets(source);
   CREATE INDEX assets_is_hidden_idx ON assets(is_hidden) WHERE is_hidden = TRUE;
   ```

### Step 3: Migration — `asset_comments` + `asset_comment_media` + `asset_media.is_deleted` (`supabase/migrations/<ts>_asset_comments.sql`)
**Scope:** Medium
1. **Create `asset_comments`** per brief (FK → `members(member_id)`, UNIQUE on `discord_message_id`, indexes on `(asset_id, discord_created_at)` and `discord_thread_id`, `is_deleted BOOLEAN DEFAULT FALSE`).
2. **Create `asset_comment_media`** with `is_deleted`:
   ```sql
   CREATE TABLE asset_comment_media (
     comment_id UUID REFERENCES asset_comments(id) ON DELETE CASCADE,
     media_id   UUID REFERENCES media(id)          ON DELETE CASCADE,
     sort_order INT  NOT NULL DEFAULT 0,
     is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
     PRIMARY KEY (comment_id, media_id)
   );
   ALTER TABLE asset_media ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
   ```
3. **RLS**: public-read on `asset_comments` / `asset_comment_media` filtered `is_deleted=false`; service-role-only for writes.

### Step 4: Migration — `media` classification + `media_import_jobs` (`supabase/migrations/<ts>_media_import_jobs.sql`)
**Scope:** Medium
1. **Extend `media.classification`** (CHECK rewrite or `ALTER TYPE ADD VALUE`, per Step 1 finding) to include `'discord-comment'`. OP attachments use the existing manual-upload literal (captured in Step 1).
2. **Create `media_import_jobs`** per brief with unique index on `discord_attachment_id WHERE NOT NULL` and claimable-index on `(status, locked_until) WHERE status IN ('pending','in_progress')`. RLS enabled, no policies.

### Step 5: Migration — `internal.secrets` table + SECURITY DEFINER accessor (`supabase/migrations/<ts>_internal_secrets_bootstrap.sql`)
**Scope:** Small
1. **Create** a postgres-owned secrets table and accessor that is entirely self-contained:
   ```sql
   CREATE SCHEMA IF NOT EXISTS internal AUTHORIZATION postgres;

   CREATE TABLE IF NOT EXISTS internal.secrets (
     name       TEXT PRIMARY KEY,
     value      TEXT NOT NULL,
     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   ALTER TABLE internal.secrets ENABLE ROW LEVEL SECURITY;
   -- No policies: only postgres and service_role (bypasses RLS) can read.
   REVOKE ALL ON internal.secrets FROM PUBLIC, anon, authenticated;

   CREATE OR REPLACE FUNCTION internal.get_service_role_key()
     RETURNS TEXT LANGUAGE sql SECURITY DEFINER SET search_path = ''
     AS $$ SELECT value FROM internal.secrets WHERE name = 'service_role_key' LIMIT 1; $$;
   REVOKE ALL ON FUNCTION internal.get_service_role_key() FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION internal.get_service_role_key() TO postgres;
   ```
2. **Migration header documents** the one-time operator action (run from psql after first deploy, not committed to the repo):
   ```sql
   -- After first deploy, run ONCE out-of-band:
   --   INSERT INTO internal.secrets (name, value)
   --   VALUES ('service_role_key', '<paste service role key>')
   --   ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
   ```
3. **Rationale**: the migration applies cleanly on `supabase db reset` with no pre-existing secret; `internal.get_service_role_key()` returns NULL until seeded, which will make the importer cron fail loudly and visibly via `system_logs`. This is deliberately discoverable instead of silently broken.

### Step 6: Apply + verify (`supabase/migrations/`)
**Scope:** Small
1. **Run** `supabase db reset` on a scratch branch; confirm all migrations apply cleanly.
2. **Verify** that existing website tests still pass (additive changes only).

---

## Phase 2: Promoter as SQL-Native Stored Function

### Step 7: Implement `internal.discord_promote_resources()` (`supabase/migrations/<ts>_discord_promoter_fn.sql`)
**Scope:** Large
1. **Create** the function in the `internal` schema (EXECUTE only to postgres):
   ```sql
   CREATE OR REPLACE FUNCTION internal.discord_promote_resources()
     RETURNS TABLE(
       channel_id BIGINT,
       assets_upserted INT,
       comments_upserted INT,
       jobs_enqueued INT,
       members_missing INT
     ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
   DECLARE
     v_channels BIGINT[] := ARRAY[
       1149372684220768367::bigint, 1275200992136400967::bigint,
       1373291419434877078::bigint, 1457981813120176138::bigint,
       1472633200491626526::bigint
     ];
     v_ch BIGINT; v_a INT; v_c INT; v_j INT; v_miss INT;
   BEGIN
     FOREACH v_ch IN ARRAY v_channels LOOP
       -- (1) log missing members then (2) discover assets (with prefilter)
       -- (3) upsert comments (with prefilter) (4) enqueue media jobs
       -- (5) reconcile removed attachments (6) propagate is_deleted
       RETURN NEXT;
     END LOOP;
   END $$;
   ```
2. **Discover-and-upsert OPs** — no time window; anti-join + member prefilter:
   ```sql
   -- First, log missing members so we know what was skipped.
   INSERT INTO public.system_logs (component, level, message, payload)
   SELECT 'discord_resource_promoter', 'warning',
          'skipping OP with missing member',
          jsonb_build_object('channel_id', v_ch, 'message_id', m.message_id,
                             'author_id', m.author_id)
     FROM public.discord_messages m
     LEFT JOIN public.members mem ON mem.member_id = m.author_id
    WHERE m.channel_id = v_ch
      AND m.message_id = m.thread_id
      AND m.reaction_count >= 5
      AND m.is_deleted = FALSE
      AND NOT EXISTS (SELECT 1 FROM public.assets a WHERE a.discord_thread_id = m.message_id)
      AND mem.member_id IS NULL;
   GET DIAGNOSTICS v_miss = ROW_COUNT;

   -- Then upsert the valid ones.
   INSERT INTO public.assets (
     id, name, description, type, member_id, admin_status, source,
     discord_channel_id, discord_thread_id,
     reactions_reached_threshold_at, imported_at, last_synced_at
   )
   SELECT gen_random_uuid(),
          LEFT(split_part(m.content, E'\n', 1), 120),
          m.content,
          CASE m.channel_id WHEN 1149372684220768367 THEN 'workflow'
                             ELSE 'lora' END,
          m.author_id, 'Listed', 'discord_import',
          m.channel_id, m.message_id, NOW(), NOW(), NOW()
     FROM public.discord_messages m
    WHERE m.channel_id = v_ch
      AND m.message_id = m.thread_id
      AND m.reaction_count >= 5
      AND m.is_deleted = FALSE
      AND EXISTS (SELECT 1 FROM public.members mem WHERE mem.member_id = m.author_id)
   ON CONFLICT (discord_thread_id) DO UPDATE
      SET description    = EXCLUDED.description,
          name           = EXCLUDED.name,
          last_synced_at = NOW();
   GET DIAGNOSTICS v_a = ROW_COUNT;
   ```
   No `created_at` filter anywhere — scan is bounded by the anti-join (via `ON CONFLICT DO UPDATE` on `discord_thread_id`) and the reaction threshold. Channel-level scan size is trivially small (`~15 OPs / 30 days × 5 channels`).
3. **Upsert replies** using the same member-prefilter pattern; log missing members the same way. Map `edited_at`→`discord_edited_at`, `created_at`→`discord_created_at`. Run a follow-up `UPDATE asset_comments SET reply_to_comment_id = ac2.id FROM asset_comments ac2 WHERE asset_comments.reply_to_discord_message_id = ac2.discord_message_id` so self-references resolve regardless of insert order.
4. **Enqueue media jobs** from `discord_messages.attachments` JSONB:
   ```sql
   WITH atts AS (
     SELECT m.message_id, m.channel_id, m.thread_id,
            (SELECT a2.id FROM public.assets a2
              WHERE a2.discord_thread_id = m.thread_id) AS op_asset_id,
            (SELECT c.id FROM public.asset_comments c
              WHERE c.discord_message_id = m.message_id) AS comment_id,
            att ->> 'url' AS url,
            att ->> 'filename' AS filename,
            (regexp_match(att ->> 'url', '/attachments/\d+/(\d+)/'))[1]::bigint AS att_id
       FROM public.discord_messages m,
            jsonb_array_elements(COALESCE(m.attachments,'[]'::jsonb)) att
      WHERE m.thread_id IN (
              SELECT discord_thread_id FROM public.assets
               WHERE discord_channel_id = v_ch AND source='discord_import'
            )
        AND m.is_deleted = FALSE
   )
   INSERT INTO public.media_import_jobs (
     id, discord_attachment_id, discord_message_id, target_kind, target_id,
     original_cdn_url, filename, status, created_at, updated_at
   )
   SELECT gen_random_uuid(), a.att_id, a.message_id,
          CASE WHEN a.comment_id IS NULL THEN 'asset_media' ELSE 'asset_comment_media' END,
          COALESCE(a.comment_id, a.op_asset_id),
          a.url, a.filename, 'pending', NOW(), NOW()
     FROM atts a
    WHERE a.att_id IS NOT NULL
   ON CONFLICT (discord_attachment_id) DO NOTHING;
   ```
   `regexp_match` (singular, vs. `regexp_matches`) returns NULL instead of raising when no match, keeping the query clean.
5. **Reconcile removed attachments**: for each comment/asset, mark `is_deleted=TRUE` on any `asset_comment_media`/`asset_media` row whose `media.metadata->>'discord_attachment_id'` no longer appears in `discord_messages.attachments` for the source message. Scope to `is_deleted=FALSE` parent rows.
6. **Propagate comment deletions**: `UPDATE asset_comments SET is_deleted = TRUE FROM discord_messages WHERE asset_comments.discord_message_id = discord_messages.message_id AND discord_messages.is_deleted = TRUE AND asset_comments.is_deleted = FALSE`.
7. **Update `assets.last_synced_at`** per channel before returning.
8. **Wrap the body** in `BEGIN … EXCEPTION WHEN OTHERS THEN INSERT INTO system_logs … END` so uncaught errors are logged instead of crashing cron.

### Step 8: Schedule the promoter (`supabase/migrations/<ts>_schedule_discord_promoter.sql`)
**Scope:** Small
1. **Cron**:
   ```sql
   SELECT cron.schedule(
     'discord-resource-promoter',
     '*/10 * * * *',
     $$ SELECT internal.discord_promote_resources(); $$
   );
   ```
   No HTTP, no secret — identical pattern to `20260403000005_schedule_priority_scores_cron.sql`.

### Step 9: Verify promoter against live data (staging)
**Scope:** Small
1. **Run** `SELECT * FROM internal.discord_promote_resources();`; confirm assets materialize for all qualifying OPs across all 5 channels.
2. **Re-run**; expect all four counts to be 0 (idempotency).
3. **Simulate late-threshold**: pick an OP older than 60 days with `reaction_count < 5`, manually set `reaction_count = 5`, rerun; confirm it imports. Closes the FLAG-001 regression gap.
4. **Simulate missing member**: temporarily delete a referenced member, rerun; confirm (a) the promoter completes without aborting, (b) the skipped OP is logged to `system_logs`, (c) other OPs import normally.

---

## Phase 3: Media Importer Edge Function (Reuse Existing Cloudflare Infra)

### Step 10: Implement `discord-media-importer` (`supabase/functions/discord-media-importer/index.ts`)
**Scope:** Large
1. **Claim jobs** atomically with `FOR UPDATE SKIP LOCKED`, limit 10 per invocation.
2. **Refresh CDN URL** by calling the existing `refresh-media-urls` edge function; skip refresh when the URL's `?ex=` timestamp is still in the future.
3. **Dispatch by content-type**:
   - **Video / animated gif**: request a Cloudflare Stream direct-upload URL (mirror `cloudflare-stream-backfill/index.ts:35-114`), POST the Discord attachment bytes, capture the Stream UID. Insert a `media` row with the Stream UID and whatever "pending" shape the existing webhook expects (captured in Step 1). **Do not poll** — `cloudflare-stream-webhook` will populate `cloudflare_playback_hls_url` + `cloudflare_thumbnail_url` when Cloudflare signals ready; duration is **not** part of this contract.
   - **Image**: stream from Discord → Supabase Storage at `user-uploads/discord-imports/{discord_message_id}/{attachment_id}-{filename}`; insert the `media` row with the public URL populated immediately.
   - **Other** (zip, json, text): upload to the same Storage path; no thumbnail.
4. **Insert junction row** into `asset_media` or `asset_comment_media` with `sort_order` = attachment index in the source Discord message.
5. **`media.classification`**: `'discord-comment'` for `target_kind='asset_comment_media'`; existing manual-upload literal otherwise. `media.metadata` JSONB: `{discord_message_id, discord_channel_id, discord_attachment_id, original_cdn_url, imported_at}`.
6. **Mark done**: `UPDATE media_import_jobs SET status='done', media_id=$id`. For video, "done" means "handed off"; the webhook asynchronously completes the media row.
7. **On failure**: exponential backoff (`locked_until = NOW() + LEAST(60 * 2^attempts, 3600) * INTERVAL '1 second'`, bump `attempts`, write `last_error`, status→`pending`). After 5 attempts → `status='failed'`.
8. **No Cloudflare polling.** Budget is a single download + upload per job; comfortably under the 400s edge timeout.

### Step 11: Confirm webhook contract (`supabase/functions/cloudflare-stream-webhook/index.ts`)
**Scope:** Small
1. **Verify** the webhook looks up the target `media` row by the column our importer populates (Stream UID) and writes only `cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, `storage_provider`. **Do not change the webhook.** If an incompatibility surfaces, the importer adapts to the existing contract, not the other way around.

### Step 12: Smoke test one attachment end-to-end (`supabase/functions/discord-media-importer/`)
**Scope:** Small
1. **Insert** one `media_import_jobs` row pointing at a known-good video attachment in a `_resources` thread.
2. **Invoke** the importer; confirm `media` row with Stream UID; wait ~1 min; confirm webhook populates HLS URL + thumbnail; confirm junction row exists.
3. **Re-invoke**; confirm no new claims.

---

## Phase 4: Importer Cron + Backfill

### Step 13: Schedule media importer with `internal.secrets` auth (`supabase/migrations/<ts>_schedule_discord_media_importer.sql`)
**Scope:** Small
1. **Cron**:
   ```sql
   SELECT cron.schedule(
     'discord-media-importer',
     '*/2 * * * *',
     $$
     SELECT net.http_post(
       url     := 'https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/discord-media-importer',
       headers := jsonb_build_object(
         'Authorization', 'Bearer ' || COALESCE(internal.get_service_role_key(), ''),
         'Content-Type',  'application/json'
       ),
       body    := '{}'::jsonb
     );
     $$
   );
   ```
2. **Runbook note** in the migration header: if `internal.secrets` hasn't been seeded, the cron job will POST with an empty Bearer and the edge function will return 401 — visible in `system_logs` via the edge function's error path. Operator seeds the key via the one-liner documented in Step 5.

### Step 14: Full backfill (`supabase`)
**Scope:** Medium
1. **Observe** 1–2 full cron cycles (~20 min). Watch `media_import_jobs` drain.
2. **Sanity-check** one asset per channel: correct `type`, non-empty junction rows, playable HLS URL after webhook completes.

---

## Phase 5: Frontend (banodoco-website)

### Step 15: Update resource data hooks to include + filter new columns (`banodoco-website/src/hooks/`)
**Scope:** Medium
1. **Update** SELECT + filter in `useResources.ts:51-74`, `useCommunityResources.ts:118-127`, `useCommunityResource.ts:103-113`, `useUserProfile.ts:125-133` — add `source, discord_channel_id, is_hidden` and append `.eq('is_hidden', false)`.
2. **Update TypeScript shapes** (`Asset`/`CommunityResource`) — three new fields optional.
3. **Regenerate** Supabase types so `asset_comments`/`asset_comment_media`/`media_import_jobs` are present.

### Step 16: Shared `AssetDescription` markdown component (`banodoco-website/src/components/resources/AssetDescription.tsx`)
**Scope:** Small
1. **Wrap** react-markdown + remark-gfm, matching the plugin + sanitization config of `PostBodyRenderer.tsx` **minus** custom embed tokens. Signature: `<AssetDescription markdown={asset.description} />`.

### Step 17: Swap plain-text description for markdown (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Small
1. **Replace** the plain-text block at lines 166-169 with `<AssetDescription markdown={resource.description} />`. Universal — manual + Discord-imported.

### Step 18: Apply same markdown change in `ResourceModal` (`banodoco-website/src/pages/Resources/ResourceModal.tsx`)
**Scope:** Small
1. **Replace** `<p className="... whitespace-pre-line">{asset.description}</p>` at lines 186-189 with `<AssetDescription markdown={asset.description} />`.

### Step 19: Add "Made with this" + "Discussion" + "From Discord" badge to `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Medium
1. **Below the existing gallery**, when `resource.source === 'discord_import'`, render:
   - **"Made with this"** — grid of every `media` row reachable via the asset's `asset_comment_media` (newest parent-comment first, `is_deleted=false` only). Tiles are **`<a href={media.url} target="_blank" rel="noopener noreferrer">`** wrapping the thumbnail — no lightbox in v1. Follow-up task flagged in the rollback/risk doc for a real lightbox.
   - **"Discussion"** — chronological list via `useAssetComments`. Each row: author chip (avatar + `global_name ?? username` from `members`), relative timestamp, "Replying to @name" chip from `reply_to_comment_id` (fallback text when only `reply_to_discord_message_id` is set), `<AssetDescription>` for the content, attachment tiles (same `<a target="_blank">` pattern), reaction-count chip, and "View on Discord" link built from a hard-coded `GUILD_ID` constant + `comment.discord_thread_id` + `comment.discord_message_id`.
2. **"From Discord" badge** — subtle pill when `resource.source === 'discord_import'`, labeled with the channel's friendly name (Flux / Wan / LTX / AceStep / Resources).

### Step 20: Add `useAssetComments` hook (`banodoco-website/src/hooks/useAssetComments.ts`)
**Scope:** Small
1. **Single query** joining `asset_comments` → `members` (author) → `asset_comment_media` → `media`, filtered `is_deleted=false` on both `asset_comments` and `asset_comment_media`, ordered `discord_created_at ASC`.

### Step 21: Badge on resource card (`banodoco-website/src/pages/Resources/ResourceCard.tsx`)
**Scope:** Small
1. **Render** the "From Discord" badge when `resource.source === 'discord_import'`. No changes to type-based branches.

---

## Phase 6: Observability + Ops

### Step 22: Health-check SQL (`supabase/sql/discord_sync_health.sql`)
**Scope:** Small
1. **Persist** three reusable queries: jobs stuck `in_progress` past `locked_until`; `failed` in last 24h with top `last_error` groupings; per-asset `last_synced_at` staleness.

### Step 23: `system_logs` writes on failure (importer + promoter)
**Scope:** Small
1. **Importer**: on unhandled exception, insert a `system_logs` row with `component='discord_media_importer'`.
2. **Promoter**: already logs missing-member skips (Step 7) and wraps uncaught errors in an EXCEPTION handler writing `component='discord_resource_promoter'`.

### Step 24: Rollback + risk register (`supabase/migrations/ROLLBACK_discord_sync.md`)
**Scope:** Small
1. **Rollback**:
   - Schema: drop new tables + columns in reverse order; drop `'discord-comment'` from `media.classification` last.
   - Promoter: `cron.unschedule('discord-resource-promoter'); DROP FUNCTION internal.discord_promote_resources();`. Optional hard rollback: `DELETE FROM assets WHERE source='discord_import'` (CASCADE cleans comments/junctions).
   - Importer: `cron.unschedule('discord-media-importer')`. Rehosted Cloudflare Stream / Storage objects require manual cleanup.
   - `internal.secrets`: drop helper function, then table.
   - Frontend: revert PR; all changes are additive.
2. **Risk register**:
   - Cloudflare Stream quota — ~30 videos / 60-day backfill; negligible.
   - Discord rate limits (50 req/s) — only the importer's per-job CDN refresh hits Discord API; paced naturally by the 10-job claim limit per 2-min cycle.
   - Importer edge-function timeout (400s) — well above worst-case per-job download + upload.
   - Storage cost — monitor post-cutover.
   - `internal.secrets` seed step — if skipped, importer cron returns 401 visibly in logs. Health-check surfaces it.
3. **Follow-up tasks** (tracked explicitly, not in v1 scope):
   - **Comment lightbox** — replace `<a target="_blank">` tiles with a real in-app lightbox modal anchored to the parent comment.
   - **Cloudflare duration** — one-line webhook change to persist `duration` if the UI later needs it.
   - **Supabase Vault migration** — if the project adopts `supabase_vault` globally, migrate `internal.secrets` to it.

---

## Execution Order

1. **Phase 1** Steps 1–6 (schema + `internal.secrets` bootstrap) — foundation.
2. **Phase 2** Steps 7–9 (SQL promoter + cron + live verification including regression tests for late-threshold and missing-member).
3. **Phase 3** Steps 10–12 (importer edge function + webhook contract confirmation + smoke test).
4. **Phase 4** Steps 13–14 (importer cron + backfill).
5. **Phase 5** Steps 15–21 (frontend) — can start in parallel with Phase 4 once real data exists.
6. **Phase 6** Steps 22–24 (observability + docs).

## Validation Order

1. `supabase db reset` applies all Phase 1 migrations cleanly (including `internal.secrets` with no pre-existing value).
2. Manual `SELECT internal.discord_promote_resources()` imports every qualifying OP across all 5 channels with correct `type`; rerun yields zero new rows.
3. **Late-threshold regression**: manually flip an OP older than 60 days to `reaction_count=5`, rerun promoter, confirm it imports.
4. **Missing-member regression**: delete a referenced `members` row, rerun promoter, confirm the sync completes, the skipped OP appears in `system_logs` with `component='discord_resource_promoter'`, and other OPs import.
5. **Attachment-URL regex**: confirm `regexp_match('https://cdn.discordapp.com/attachments/123/456/foo.png?ex=abc&is=def&hm=xyz', '/attachments/\d+/(\d+)/')` returns `{"456"}` in psql; add a CHECK-style assertion inline in the verification SQL.
6. **Attachment-removal-on-edit**: simulate a Discord edit removing an attachment; confirm the corresponding `asset_comment_media`/`asset_media` row has `is_deleted=TRUE` on the next promoter tick.
7. **Importer smoke test**: one video → Stream UID + HLS after webhook; one image → direct Storage URL.
8. **ResourceDetail render**: imported asset shows markdown description, "Made with this" tiles (clickable `<a target="_blank">`), "Discussion" list, badge, working "View on Discord" links.
9. **Manual asset render**: description renders as markdown; no Discord sections appear; otherwise unchanged.
10. **is_hidden kill-switch**: flip `is_hidden=true` on one imported asset; confirm it disappears from all four hook-backed surfaces and from ResourceDetail by URL.
11. **Final idempotency**: rerun both promoter and importer; expect zero net new rows.
