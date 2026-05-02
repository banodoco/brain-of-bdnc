# Implementation Plan: Sync Discord `_resources` Posts into banodoco-website (v5)

## Overview

Promote qualifying Discord forum OPs in the 5 `_resources` channels into `assets` rows on the shared Supabase project (`ujlwuvkrxlvoswwkerdf`), with thread replies as `asset_comments`, reply attachments as `asset_comment_media`, and OP attachments into existing `asset_media`. Media is rehosted into Cloudflare Stream (video) or Supabase Storage (images/other).

**Revision summary v4 → v5** — two narrow fixes, no architectural changes:

1. **Guild ID flows through data, not a frontend constant.** The repo already stores `guild_id` on `discord_messages` and `discord_channels` and indexes it per `supabase/migrations/20260320000000_multi_server.sql:140-145`; `brain-of-bndc/src/common/urls.py:7-12` already constructs jump URLs from a passed `guild_id`. v4's hardcoded frontend `GUILD_ID` would break silently if the `_resources` channels ever moved guilds and ignores the existing caller contract. v5 denormalizes `discord_guild_id BIGINT` onto `assets` and `asset_comments` (parallel to the already-denormalized `discord_channel_id`/`discord_thread_id`), populated by the promoter from `discord_messages.guild_id`. The Discussion "View on Discord" link reads `comment.discord_guild_id` directly. Fixes callers / all_locations / FLAG-011.
2. **Markdown component reuses `MarkdownRenderer`, not a nonexistent sanitization layer.** v4 said `AssetDescription` should mirror `PostBodyRenderer.tsx`'s "plugin + sanitization config", but `PostBodyRenderer.tsx:22-50` just delegates to `MarkdownTextSegment`, and the real pipeline lives at `banodoco-website/src/components/posts/MarkdownRenderer.tsx:122-126` — `react-markdown` with `remarkGfm` only, no sanitize/rehype layer. v5 redirects Step 18 to reuse `MarkdownRenderer` directly (importing and wrapping, not reimplementing). No custom embed tokens are registered in the website's markdown path today, so "avoid custom embed tokens" is an already-satisfied constraint. Fixes issue_hints.

**issue_hints-1** (PL/pgSQL pivot + cursor-table removal as a brief-deviation) is gate-settled DEC-003 across iterations 1, 2, 3 — already marked `status=addressed` in the latest gate signals. v5 keeps the one-line Overview acknowledgment and does not re-open it.

**Settled design decisions** (all gate-endorsed, do not re-litigate): FKs target `members(member_id)`; attachment ID parsed from CDN URL path with `regexp_match`; promoter is PL/pgSQL invoked by pg_cron directly; media importer reuses `cloudflare-stream-webhook` (writes HLS + thumbnail + storage_provider, **not** duration); channel→type map `workflow`/`lora`; universal markdown via `AssetDescription`; `useResources`/`useCommunityResources`/`useCommunityResource`/`useUserProfile` filter `is_hidden=false`; `asset_comment_media` + `asset_media` have `is_deleted`; no member fallback, EXISTS prefilter instead; secrets via `internal.secrets` + SECURITY DEFINER accessor; `<a target="_blank">` tiles in v1, real lightbox deferred; promoter has `dry_run BOOLEAN DEFAULT FALSE` with a dedicated first-working-version milestone; main INSERTs use `WHERE NOT EXISTS` + `ON CONFLICT DO NOTHING` and drift-UPDATE is a separate conditional statement; partial composite index on `discord_messages (channel_id, reaction_count DESC) WHERE message_id = thread_id AND is_deleted = FALSE`; all `system_logs` INSERTs use `logger_name, level, message, extra`.

**New in v5:** `assets.discord_guild_id BIGINT` + `asset_comments.discord_guild_id BIGINT NOT NULL`, populated by the promoter from `discord_messages.guild_id`, read by the frontend for jump-link construction.

**Sandbox caveat:** this planning session is restricted to `brain-of-bndc/`; precise `banodoco-website/` and `supabase/` file/line references are taken from gate evidence across iterations.

---

## Phase 1: Schema Migrations

### Step 1: Audit current shapes (`supabase/migrations/`, `banodoco-website/src/`, `supabase/functions/`)
**Scope:** Small
1. **Re-read** these exact files before writing any migration SQL:
   - `supabase/migrations/20251101000000_create_discord_tables.sql` (members DDL + existing `discord_messages` indexes).
   - `supabase/migrations/20260319100000_fix_sync_reactors_trigger.sql` (reaction-count trigger).
   - `supabase/migrations/20260320000000_multi_server.sql:140-145` (confirm `discord_messages.guild_id` and `discord_channels.guild_id` columns + indexes).
   - `supabase/migrations/20260403000005_schedule_priority_scores_cron.sql` (cron pattern).
   - **`supabase/migrations/20251210000000_create_system_logs.sql:5-18`** — transcribe the exact column list (`logger_name`, `level`, `message`, `extra`, plus `id`/`created_at`/etc.) before writing any INSERT.
   - `supabase/functions/cloudflare-stream-webhook/index.ts:210-240` — capture the real lookup column (Stream UID → which `media` column) and confirm it writes only `cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, `storage_provider`.
   - `supabase/functions/cloudflare-stream-backfill/index.ts:35-114` (direct-upload initiation pattern).
   - Most-recent `assets`/`media`/`asset_media` migration — capture `media.classification` enum/CHECK shape and the existing manual-upload literal for OP imports.
   - `banodoco-website/src/pages/SubmitResource/index.tsx:10-13` (type enum).
   - `banodoco-website/src/pages/ResourceDetail/index.tsx:59-74, 166-169, 228-245` (state + plain-text description + media-switch render).
   - **`banodoco-website/src/components/posts/MarkdownRenderer.tsx:122-126`** — this is the real markdown pipeline (`react-markdown` + `remarkGfm`, no sanitize/rehype). `AssetDescription` reuses or wraps this.
2. **Block** any SQL that writes to `system_logs(component, …)`; every INSERT must use `logger_name, level, message, extra`.

### Step 2: Migration — extend `assets` with Discord columns including `discord_guild_id` (`supabase/migrations/<ts>_assets_discord_import_cols.sql`)
**Scope:** Small
1. **Add columns** (additive):
   ```sql
   ALTER TABLE assets
     ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
       CHECK (source IN ('manual','discord_import')),
     ADD COLUMN discord_guild_id BIGINT,
     ADD COLUMN discord_channel_id BIGINT,
     ADD COLUMN discord_thread_id BIGINT,
     ADD COLUMN imported_at TIMESTAMPTZ,
     ADD COLUMN last_synced_at TIMESTAMPTZ,
     ADD COLUMN reactions_reached_threshold_at TIMESTAMPTZ,
     ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT FALSE;

   CREATE UNIQUE INDEX assets_discord_thread_id_unique
     ON assets(discord_thread_id) WHERE discord_thread_id IS NOT NULL;
   CREATE INDEX assets_source_idx ON assets(source);
   CREATE INDEX assets_is_hidden_idx ON assets(is_hidden) WHERE is_hidden = TRUE;
   ```
   `discord_guild_id` is denormalized (parallel to `discord_channel_id` / `discord_thread_id`) so the frontend can build jump URLs from the asset row without joining `discord_channels`.

### Step 3: Migration — `asset_comments` (with `discord_guild_id`) + `asset_comment_media` + `asset_media.is_deleted` (`supabase/migrations/<ts>_asset_comments.sql`)
**Scope:** Medium
1. **Create `asset_comments`** (FK → `members(member_id)`, UNIQUE on `discord_message_id`, indexes on `(asset_id, discord_created_at)` + `discord_thread_id`):
   ```sql
   CREATE TABLE asset_comments (
     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     asset_id UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
     discord_message_id BIGINT NOT NULL UNIQUE,
     discord_thread_id BIGINT NOT NULL,
     discord_guild_id BIGINT NOT NULL,
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
2. **Create `asset_comment_media`** with `is_deleted`; add `asset_media.is_deleted`:
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
3. **RLS**: public-read on `asset_comments` / `asset_comment_media` filtered `is_deleted=false`; service-role only for writes.

### Step 4: Migration — `media` classification + `media_import_jobs` (`supabase/migrations/<ts>_media_import_jobs.sql`)
**Scope:** Medium
1. **Extend `media.classification`** (CHECK rewrite or `ALTER TYPE ADD VALUE` per Step 1 finding) to include `'discord-comment'`. OP attachments reuse the existing manual-upload literal (captured in Step 1).
2. **Create `media_import_jobs`** per brief with unique index on `discord_attachment_id WHERE NOT NULL` and claimable-index on `(status, locked_until) WHERE status IN ('pending','in_progress')`. RLS enabled, no policies.

### Step 5: Migration — `internal.secrets` table + SECURITY DEFINER accessor (`supabase/migrations/<ts>_internal_secrets_bootstrap.sql`)
**Scope:** Small
1. **Create** postgres-owned secrets table + accessor:
   ```sql
   CREATE SCHEMA IF NOT EXISTS internal AUTHORIZATION postgres;

   CREATE TABLE IF NOT EXISTS internal.secrets (
     name       TEXT PRIMARY KEY,
     value      TEXT NOT NULL,
     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   ALTER TABLE internal.secrets ENABLE ROW LEVEL SECURITY;
   REVOKE ALL ON internal.secrets FROM PUBLIC, anon, authenticated;

   CREATE OR REPLACE FUNCTION internal.get_service_role_key()
     RETURNS TEXT LANGUAGE sql SECURITY DEFINER SET search_path = ''
     AS $$ SELECT value FROM internal.secrets WHERE name = 'service_role_key' LIMIT 1; $$;
   REVOKE ALL ON FUNCTION internal.get_service_role_key() FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION internal.get_service_role_key() TO postgres;
   ```
2. **Migration header** documents the one-time operator seed.

### Step 6: Migration — partial composite index on `discord_messages` (`supabase/migrations/<ts>_discord_messages_resource_op_idx.sql`)
**Scope:** Small
1. **Add** the supporting index for the all-history scan:
   ```sql
   CREATE INDEX IF NOT EXISTS discord_messages_resource_op_idx
     ON public.discord_messages (channel_id, reaction_count DESC)
     WHERE message_id = thread_id AND is_deleted = FALSE;
   ```

### Step 7: Apply + verify migrations (`supabase/migrations/`)
**Scope:** Small
1. **Run** `supabase db reset` on a scratch branch; confirm every migration applies cleanly.
2. **Verify** existing website tests still pass.

---

## Phase 2: Promoter as SQL-Native Stored Function (with dry-run, populating guild_id)

### Step 8: Implement `internal.discord_promote_resources(dry_run BOOLEAN DEFAULT FALSE)` (`supabase/migrations/<ts>_discord_promoter_fn.sql`)
**Scope:** Large
1. **Declare** the function with a dry-run parameter:
   ```sql
   CREATE OR REPLACE FUNCTION internal.discord_promote_resources(
     dry_run BOOLEAN DEFAULT FALSE
   ) RETURNS TABLE(
     channel_id BIGINT,
     assets_inserted INT, assets_updated INT,
     comments_inserted INT, comments_updated INT,
     jobs_enqueued INT, members_missing INT,
     comment_media_marked_deleted INT, comments_marked_deleted INT,
     dry_run BOOLEAN
   ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
   DECLARE
     v_channels BIGINT[] := ARRAY[1149372684220768367::bigint,
       1275200992136400967::bigint, 1373291419434877078::bigint,
       1457981813120176138::bigint, 1472633200491626526::bigint];
     v_ch BIGINT;
     v_ai INT; v_au INT; v_ci INT; v_cu INT; v_j INT; v_mm INT; v_cmd INT; v_cd INT;
   BEGIN
     FOREACH v_ch IN ARRAY v_channels LOOP
       -- ... see steps 2-8 below
       RETURN NEXT;
     END LOOP;
   END $$;
   ```
2. **Log missing members** (writes-only path; dry-run counts via SELECT):
   ```sql
   IF NOT dry_run THEN
     INSERT INTO public.system_logs (logger_name, level, message, extra)
     SELECT 'discord_resource_promoter', 'warning',
            'skipping OP: missing member',
            jsonb_build_object('channel_id', v_ch,
                               'message_id', m.message_id,
                               'author_id', m.author_id)
       FROM public.discord_messages m
       LEFT JOIN public.members mem ON mem.member_id = m.author_id
      WHERE m.channel_id = v_ch
        AND m.message_id = m.thread_id
        AND m.reaction_count >= 5
        AND m.is_deleted = FALSE
        AND NOT EXISTS (SELECT 1 FROM public.assets a
                         WHERE a.discord_thread_id = m.message_id)
        AND mem.member_id IS NULL;
     GET DIAGNOSTICS v_mm = ROW_COUNT;
   ELSE
     SELECT COUNT(*) INTO v_mm
       FROM public.discord_messages m
       LEFT JOIN public.members mem ON mem.member_id = m.author_id
      WHERE m.channel_id = v_ch AND m.message_id = m.thread_id
        AND m.reaction_count >= 5 AND m.is_deleted = FALSE
        AND NOT EXISTS (SELECT 1 FROM public.assets a
                         WHERE a.discord_thread_id = m.message_id)
        AND mem.member_id IS NULL;
   END IF;
   ```
3. **INSERT new assets** — anti-join, populate `discord_guild_id` from `m.guild_id`:
   ```sql
   IF NOT dry_run THEN
     INSERT INTO public.assets (
       id, name, description, type, member_id, admin_status, source,
       discord_guild_id, discord_channel_id, discord_thread_id,
       reactions_reached_threshold_at, imported_at, last_synced_at
     )
     SELECT gen_random_uuid(),
            LEFT(split_part(m.content, E'\n', 1), 120),
            m.content,
            CASE m.channel_id WHEN 1149372684220768367 THEN 'workflow'
                               ELSE 'lora' END,
            m.author_id, 'Listed', 'discord_import',
            m.guild_id, m.channel_id, m.message_id,
            NOW(), NOW(), NOW()
       FROM public.discord_messages m
      WHERE m.channel_id = v_ch
        AND m.message_id = m.thread_id
        AND m.reaction_count >= 5
        AND m.is_deleted = FALSE
        AND NOT EXISTS (SELECT 1 FROM public.assets a
                         WHERE a.discord_thread_id = m.message_id)
        AND EXISTS (SELECT 1 FROM public.members mem
                     WHERE mem.member_id = m.author_id)
     ON CONFLICT (discord_thread_id) DO NOTHING;
     GET DIAGNOSTICS v_ai = ROW_COUNT;
   ELSE
     SELECT COUNT(*) INTO v_ai FROM public.discord_messages m
      WHERE m.channel_id = v_ch AND m.message_id = m.thread_id
        AND m.reaction_count >= 5 AND m.is_deleted = FALSE
        AND NOT EXISTS (SELECT 1 FROM public.assets a
                         WHERE a.discord_thread_id = m.message_id)
        AND EXISTS (SELECT 1 FROM public.members mem
                     WHERE mem.member_id = m.author_id);
   END IF;
   ```
4. **UPDATE drifted descriptions** (conditional on `IS DISTINCT FROM`; zero ROW_COUNT on idempotent reruns). Same pattern as v4.
5. **INSERT new comments** with anti-join + EXISTS member prefilter, populating `discord_guild_id` from `m.guild_id`:
   ```sql
   INSERT INTO public.asset_comments (
     id, asset_id, discord_message_id, discord_thread_id,
     discord_guild_id, author_member_id, content,
     reply_to_discord_message_id, reaction_count,
     discord_created_at, discord_edited_at, is_deleted
   )
   SELECT gen_random_uuid(),
          (SELECT a.id FROM public.assets a
            WHERE a.discord_thread_id = m.thread_id),
          m.message_id, m.thread_id, m.guild_id,
          m.author_id, m.content,
          m.reference_id, m.reaction_count,
          m.created_at, m.edited_at, FALSE
     FROM public.discord_messages m
    WHERE m.thread_id IN (SELECT a.discord_thread_id FROM public.assets a
                           WHERE a.discord_channel_id = v_ch
                             AND a.source = 'discord_import')
      AND m.message_id <> m.thread_id
      AND m.is_deleted = FALSE
      AND NOT EXISTS (SELECT 1 FROM public.asset_comments c
                       WHERE c.discord_message_id = m.message_id)
      AND EXISTS (SELECT 1 FROM public.members mem
                   WHERE mem.member_id = m.author_id)
   ON CONFLICT (discord_message_id) DO NOTHING;
   GET DIAGNOSTICS v_ci = ROW_COUNT;
   ```
   Plus the conditional UPDATE for drifted comment content (`v_cu`) and the follow-up `reply_to_comment_id` resolver. Dry-run path replaces INSERT/UPDATE with `SELECT COUNT(*) …` on the same predicates.
6. **Enqueue media jobs** via the CTE from v4 Step 8.4 (using `regexp_match` on the CDN URL); `ON CONFLICT (discord_attachment_id) DO NOTHING` → `v_j`.
7. **Reconcile removed attachments** (`v_cmd`): flip `is_deleted=TRUE` on `asset_comment_media` / `asset_media` when the attachment no longer appears in `discord_messages.attachments`. Scoped to `is_deleted=FALSE` parent rows.
8. **Propagate comment deletions** (`v_cd`): `UPDATE asset_comments SET is_deleted = TRUE` when the source `discord_messages.is_deleted = TRUE` and the comment is still live.
9. **Per-channel summary** — in dry-run path, `INSERT INTO public.system_logs (logger_name, level, message, extra) VALUES ('discord_resource_promoter', 'info', 'dry-run summary', jsonb_build_object('dry_run', true, 'channel_id', v_ch, 'assets_inserted', v_ai, …))` and `RAISE NOTICE …`. In live path, `UPDATE public.assets SET last_synced_at = NOW() WHERE discord_channel_id = v_ch AND source = 'discord_import'`.
10. **Wrap** the body in `EXCEPTION WHEN OTHERS THEN INSERT INTO public.system_logs (logger_name, level, message, extra) VALUES ('discord_resource_promoter', 'error', SQLERRM, jsonb_build_object('sqlstate', SQLSTATE))`.

### Step 9: Schedule the promoter (`supabase/migrations/<ts>_schedule_discord_promoter.sql`)
**Scope:** Small
1. **Cron**:
   ```sql
   SELECT cron.schedule(
     'discord-resource-promoter',
     '*/10 * * * *',
     $$ SELECT internal.discord_promote_resources(); $$
   );
   ```

### Step 10: First-working-version milestone — promoter dry-run (staging)
**Scope:** Small
1. **Run** `SELECT * FROM internal.discord_promote_resources(dry_run := TRUE);` on staging.
2. **Verify** zero writes to `assets`/`asset_comments`/`media_import_jobs` and one `system_logs` row per channel with `logger_name='discord_resource_promoter'`, `level='info'`, `extra->>'dry_run'='true'`.
3. **EXPLAIN (ANALYZE, BUFFERS)** the discovery query; confirm `discord_messages_resource_op_idx` is used.

### Step 11: Live promoter verification (staging)
**Scope:** Small
1. **Run** `SELECT internal.discord_promote_resources();`.
2. **Rerun** immediately; confirm all insert/update counts are zero (idempotency holds via the anti-join + drift-conditional UPDATE).
3. **Confirm** `discord_guild_id` is populated on every new `assets` row and every new `asset_comments` row (non-NULL BIGINT).
4. **Late-threshold regression**: old OP flipped to `reaction_count=5` imports on next tick.
5. **Missing-member regression**: deleted `members` row → skipped OP logged, others import.

---

## Phase 3: Media Importer Edge Function (Reuse Existing Cloudflare Infra)

### Step 12: Implement `discord-media-importer` (`supabase/functions/discord-media-importer/index.ts`)
**Scope:** Large
1. **Claim jobs** atomically (`FOR UPDATE SKIP LOCKED`, limit 10).
2. **Refresh CDN URL** via the existing `refresh-media-urls` edge function; skip when `?ex=` is still in the future.
3. **Dispatch by content-type**:
   - **Video / animated gif**: initiate Cloudflare Stream direct upload, capture Stream UID, insert `media` with the UID in the lookup column captured in Step 1. **No polling**; webhook completes `cloudflare_playback_hls_url` + `cloudflare_thumbnail_url`.
   - **Image**: stream to Supabase Storage at `user-uploads/discord-imports/{discord_message_id}/{attachment_id}-{filename}`; insert `media` with public URL.
   - **Other** (zip/json/text): Storage upload, no thumbnail.
4. **Insert junction row** (`asset_media` or `asset_comment_media`) with `sort_order` = attachment index.
5. **`media.classification`**: `'discord-comment'` for `target_kind='asset_comment_media'`; else the existing manual-upload literal. `media.metadata` JSONB: `{discord_message_id, discord_channel_id, discord_attachment_id, original_cdn_url, imported_at}`.
6. **Mark done**: `UPDATE media_import_jobs SET status='done', media_id=$id`.
7. **On failure**: exponential backoff; after 5 attempts → `status='failed'`.
8. **Error logging** uses `logger_name, level, message, extra` columns.

### Step 13: Confirm webhook contract (`supabase/functions/cloudflare-stream-webhook/index.ts`)
**Scope:** Small
1. **Verify** the webhook's lookup column; importer inserts `media` with that column populated. No webhook changes.

### Step 14: Smoke test one attachment (`supabase/functions/discord-media-importer/`)
**Scope:** Small
1. **Insert** one `media_import_jobs` row for a known video attachment; invoke importer; wait for webhook; confirm HLS URL populates; confirm junction row; re-invoke, no new claims.

---

## Phase 4: Importer Cron + Backfill

### Step 15: Schedule media importer (`supabase/migrations/<ts>_schedule_discord_media_importer.sql`)
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

### Step 16: Full backfill (`supabase`)
**Scope:** Medium
1. **Observe** 1–2 cron cycles; watch `media_import_jobs` drain; sanity-check one asset per channel.

---

## Phase 5: Frontend (banodoco-website)

### Step 17: Update resource data hooks (`banodoco-website/src/hooks/`)
**Scope:** Medium
1. **Update SELECT + filter** in `useResources.ts:51-74`, `useCommunityResources.ts:118-127`, `useCommunityResource.ts:103-113`, `useUserProfile.ts:125-133` — add `source, discord_guild_id, discord_channel_id, discord_thread_id, is_hidden` to the SELECT and `.eq('is_hidden', false)` to the filter.
2. **Update TypeScript shapes** — four new fields optional.
3. **Regenerate** Supabase types.

### Step 18: Shared `AssetDescription` markdown component (`banodoco-website/src/components/resources/AssetDescription.tsx`)
**Scope:** Small
1. **Reuse** the existing markdown pipeline at `banodoco-website/src/components/posts/MarkdownRenderer.tsx:122-126` — which already uses `react-markdown` + `remarkGfm` only (no sanitize/rehype layer, no custom embed tokens in that path). Preferred implementation: `AssetDescription` imports `MarkdownRenderer` and wraps/forwards a single prop; if reuse is awkward, it inlines the same minimal `<ReactMarkdown remarkPlugins={[remarkGfm]}>` configuration.
2. **Do not** add a new sanitization layer, rehype plugins, or custom embed tokens — the existing pipeline's posture is the contract.
3. **Signature**: `<AssetDescription markdown={asset.description} />`.

### Step 19: Swap plain-text description for markdown in `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Small
1. **Replace** the plain-text block at lines 166-169 with `<AssetDescription markdown={resource.description} />`. Universal — manual + Discord-imported.

### Step 20: Apply same markdown change in `ResourceModal` (`banodoco-website/src/pages/Resources/ResourceModal.tsx`)
**Scope:** Small
1. **Replace** the `whitespace-pre-line` `<p>` at lines 186-189 with `<AssetDescription markdown={asset.description} />`.

### Step 21: Add "Made with this" + "Discussion" + "From Discord" badge to `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Medium
1. **When `resource.source === 'discord_import'`**, render below the existing gallery:
   - **"Made with this"** grid — tiles are `<a href={media.url} target="_blank" rel="noopener noreferrer">` over the thumbnail. No lightbox in v1.
   - **"Discussion"** — flat chronological list via `useAssetComments`. Each row: author chip (`avatar_url` + `global_name ?? username` from `members`), relative timestamp, "Replying to @name" chip from `reply_to_comment_id`, `<AssetDescription>` for content, attachment tiles (`<a target="_blank">`), reaction-count chip, **"View on Discord"** link built from the comment row's own `discord_guild_id` / `discord_thread_id` / `discord_message_id`:
     ```tsx
     const jumpUrl =
       `https://discord.com/channels/${comment.discord_guild_id}` +
       `/${comment.discord_thread_id}/${comment.discord_message_id}`;
     ```
     **No hard-coded `GUILD_ID` constant anywhere.** The asset-level OP jump link (if shown near the badge) similarly reads `resource.discord_guild_id` / `resource.discord_channel_id` / `resource.discord_thread_id`.
2. **"From Discord" badge** — subtle pill when `resource.source === 'discord_import'`, labeled with the channel friendly name.

### Step 22: Add `useAssetComments` hook (`banodoco-website/src/hooks/useAssetComments.ts`)
**Scope:** Small
1. **Single query** joining `asset_comments` → `members` → `asset_comment_media` → `media`, filtered `is_deleted=false` on both layers, ordered `discord_created_at ASC`. SELECT must include `discord_guild_id`, `discord_thread_id`, `discord_message_id` on every comment row so the frontend can build jump links without extra joins.

### Step 23: Badge on resource card (`banodoco-website/src/pages/Resources/ResourceCard.tsx`)
**Scope:** Small
1. **Render** the "From Discord" badge when `resource.source === 'discord_import'`.

---

## Phase 6: Observability + Ops

### Step 24: Health-check SQL (`supabase/sql/discord_sync_health.sql`)
**Scope:** Small
1. **Persist** reusable queries: stuck `in_progress` jobs; `failed` in last 24h grouped by `last_error`; per-asset `last_synced_at` staleness; recent `system_logs` rows where `logger_name IN ('discord_resource_promoter','discord_media_importer')`.

### Step 25: Rollback + risk register + follow-ups (`supabase/migrations/ROLLBACK_discord_sync.md`)
**Scope:** Small
1. **Rollback**: drop tables + columns (including `discord_guild_id` on both `assets` and `asset_comments`) in reverse order; `cron.unschedule(…)` both schedules; `DROP FUNCTION internal.discord_promote_resources(BOOLEAN)`; `DROP INDEX discord_messages_resource_op_idx`; drop helper function then `internal.secrets` table; revert frontend PR.
2. **Risks**: Cloudflare Stream quota (negligible); Discord rate limits (paced by 10-job claim); edge-function timeout (comfortable); Storage cost (monitor); unseeded `internal.secrets` → 401s.
3. **Follow-ups**: comment lightbox, Cloudflare duration webhook extension, `supabase_vault` migration from `internal.secrets`.

---

## Execution Order

1. **Phase 1** Steps 1–7 (audit + schema incl. `discord_guild_id` + `internal.secrets` + partial index).
2. **Phase 2** Steps 8–10 (promoter function populating `discord_guild_id` + cron + **dry-run milestone**).
3. **Phase 2** Step 11 (live promoter + regression tests including guild-id population check).
4. **Phase 3** Steps 12–14 (importer + webhook reconciliation + smoke test).
5. **Phase 4** Steps 15–16 (importer cron + backfill).
6. **Phase 5** Steps 17–23 (frontend; hooks select `discord_guild_id`; UI builds jump URLs from row data, no constants).
7. **Phase 6** Steps 24–25.

## Validation Order

1. `supabase db reset` applies all Phase 1 migrations cleanly.
2. `EXPLAIN ANALYZE` confirms the promoter uses `discord_messages_resource_op_idx`.
3. Dry-run writes zero data, logs one `system_logs` row per channel with `extra->>'dry_run'='true'`.
4. Live promoter imports every qualifying OP; **every new `assets` row has a non-NULL `discord_guild_id` matching `discord_messages.guild_id` for the source message; every new `asset_comments` row has a non-NULL `discord_guild_id`.**
5. Rerun live: zero inserts and zero updates.
6. Late-threshold regression: old OP flipped to `reaction_count=5` imports.
7. Missing-member regression: deleted `members` row → skipped OP logged.
8. **`regexp_match`** extracts the attachment ID correctly from URLs with `?ex=…&is=…&hm=…`.
9. Attachment-removal-on-edit marks the junction row `is_deleted=TRUE`.
10. Edit/delete propagation: `asset_comments.content` + `discord_edited_at` update; deletions set `is_deleted`.
11. Importer smoke test: video → Stream UID + HLS via webhook; image → Storage URL.
12. Importer failure path: 5-attempt escalation to `status='failed'`.
13. **Frontend render**: imported asset shows markdown description (via `AssetDescription` wrapping `MarkdownRenderer`), Made-with-this tiles, Discussion list, "From Discord" badge, and **"View on Discord" links built from `comment.discord_guild_id`/`discord_thread_id`/`discord_message_id` — NOT a hard-coded constant.**
14. Manual asset render: description renders markdown; no Discord sections appear.
15. `is_hidden=true` kill switch: asset disappears from all four hook surfaces and from `ResourceDetail` by URL.
16. Final idempotency: rerun promoter and importer; zero net new rows.
