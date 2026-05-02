# Implementation Plan: Sync Discord `_resources` Posts into banodoco-website (v4)

## Overview

Promote qualifying Discord forum OPs in the 5 `_resources` channels into `assets` rows on the shared Supabase project (`ujlwuvkrxlvoswwkerdf`), with thread replies as `asset_comments`, reply attachments as `asset_comment_media`, and OP attachments into existing `asset_media`. Media is rehosted into Cloudflare Stream (video) or Supabase Storage (images/other).

**Revision summary v3 → v4** — 9 flags remained; all four critique pressure points are narrow mechanical or design-surface corrections:

1. **True anti-join on assets INSERT + separate UPDATE path.** v3 used `ON CONFLICT DO UPDATE` which kept `GET DIAGNOSTICS ROW_COUNT > 0` on reruns, violating the idempotency success criterion. v4 splits into two statements: `INSERT … WHERE NOT EXISTS (SELECT 1 FROM assets a2 WHERE a2.discord_thread_id = m.message_id) … ON CONFLICT DO NOTHING` (counts only true inserts) and a conditional `UPDATE … WHERE description IS DISTINCT FROM …` (counts only actual drift). Same pattern for `asset_comments`. Fixes FLAG-009 / correctness-1.
2. **`system_logs` uses real column names `logger_name`/`level`/`message`/`extra`.** Per `supabase/migrations/20251210000000_create_system_logs.sql:5-18`. Every insert in the promoter body and in the importer error path now matches the actual schema. Fixes FLAG-008 / correctness-2.
3. **Partial composite index on `discord_messages` for the all-history scan.** v3 claimed the scan is "cheap" but the repo only has single-column indexes. v4 adds `CREATE INDEX … ON discord_messages (channel_id, reaction_count DESC) WHERE message_id = thread_id AND is_deleted = FALSE` in Phase 1. All predicates are IMMUTABLE so the partial index is legal, and it shrinks the index to forum OPs only (a tiny fraction of all messages). Fixes FLAG-010 / all_locations.
4. **Promoter gains a `dry_run` parameter; first-working-version milestone is the dry-run invocation.** `internal.discord_promote_resources(dry_run BOOLEAN DEFAULT FALSE)` — when `TRUE`, the function computes intended-insert counts via SELECT, writes a summary row to `system_logs` with `level='info'` and an `extra->>'dry_run'='true'` marker, emits `RAISE NOTICE` per channel, and returns without mutating any data. Cron continues to call it with no args (defaults to FALSE). Execution Order's first live step is `SELECT internal.discord_promote_resources(dry_run := TRUE);`. Fixes issue_hints-2.
5. **Settled design decisions explicitly called out once, then not revisited.** The gate has endorsed DEC-001..DEC-011 across prior iterations; the Overview cites them with a pointer and `issue_hints-1` / `issue_hints` stay closed. The PL/pgSQL promoter + no-cursor-table pivot is a *gate-endorsed deliberate deviation* from the brief's stated shape: anti-join on `assets.discord_thread_id` fully replaces the cursor's role (it tracks "already imported" directly), SQL-native execution removes the 30s edge-function wall-clock risk and the auth dependency, and the pattern matches the repo's existing `supabase/migrations/20260403000005_schedule_priority_scores_cron.sql`. This is the correct call and v4 does not re-litigate it.

**Settled design decisions** (do not re-litigate; all gate-endorsed across prior iterations):

- FKs target `members(member_id)` (renamed from `discord_members`).
- Discord `attachment.id` parsed from CDN URL `/attachments/{channel}/{id}/{filename}` (stable across signature refresh); `regexp_match` (singular) returns NULL on miss.
- Promoter is PL/pgSQL invoked by pg_cron directly; no cursor table; anti-join on `assets.discord_thread_id IS NULL` plus `reaction_count >= 5` replaces cursor. Deliberate gate-endorsed deviation from the brief's two-edge-function + cursor-table shape.
- Media importer reuses `cloudflare-stream-webhook` for Stream completion (writes `cloudflare_playback_hls_url` + `cloudflare_thumbnail_url` + `storage_provider`; **not** duration).
- Channel → type map: `1149372684220768367` → `workflow`; others → `lora`.
- Universal markdown via shared `AssetDescription` component (react-markdown + remark-gfm, no custom embed tokens).
- `useResources`/`useCommunityResources`/`useCommunityResource`/`useUserProfile` all SELECT `source, discord_channel_id, is_hidden` and filter `is_hidden=false`.
- `asset_comment_media` + `asset_media` gain `is_deleted`.
- No member fallback; promoter prefilters on `EXISTS (SELECT 1 FROM members …)`.
- Secret delivery via `internal.secrets` + `internal.get_service_role_key()` SECURITY DEFINER, not `supabase_vault`.
- Comment/"Made with this" tiles are `<a target="_blank">` in v1; real lightbox is a tracked follow-up.

**Sandbox caveat:** this planning session is restricted to `brain-of-bndc/`; precise `banodoco-website/` and `supabase/` file/line references are taken from gate evidence across iterations.

---

## Phase 1: Schema Migrations

### Step 1: Audit current shapes (`supabase/migrations/`, `banodoco-website/src/`, `supabase/functions/`)
**Scope:** Small
1. **Re-read** these exact files before writing any migration SQL:
   - `supabase/migrations/20251101000000_create_discord_tables.sql` (members DDL + existing discord_messages single-column indexes).
   - `supabase/migrations/20260319100000_fix_sync_reactors_trigger.sql` (reaction-count trigger).
   - `supabase/migrations/20260403000005_schedule_priority_scores_cron.sql` (cron pattern).
   - **`supabase/migrations/20251210000000_create_system_logs.sql:5-18`** — transcribe the exact column list (confirmed via critique: `logger_name`, `level`, `message`, `extra`). Copy it verbatim into the plan's working notes before writing any INSERT.
   - `supabase/functions/cloudflare-stream-webhook/index.ts:210-240` (webhook columns: `cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, `storage_provider`; duration is **not** persisted).
   - `supabase/functions/cloudflare-stream-backfill/index.ts:35-114` (direct-upload initiation pattern).
   - Most-recent `assets`/`media`/`asset_media` migration — capture `media.classification` enum/CHECK shape and the existing manual-upload literal for OP imports.
   - `banodoco-website/src/pages/SubmitResource/index.tsx:10-13` (type enum).
   - `banodoco-website/src/pages/ResourceDetail/index.tsx:59-74, 166-169, 228-245` (state + plain-text description + media-switch render).
   - `banodoco-website/src/components/posts/PostBodyRenderer.tsx` (markdown pipeline + sanitization).
2. **Block** any migration or function body that references `system_logs(component, …)` — every insert must use `logger_name`, `level`, `message`, `extra` exactly.

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
3. **RLS**: public-read on `asset_comments` / `asset_comment_media` filtered `is_deleted=false`; service-role only for writes.

### Step 4: Migration — `media` classification + `media_import_jobs` (`supabase/migrations/<ts>_media_import_jobs.sql`)
**Scope:** Medium
1. **Extend `media.classification`** (CHECK rewrite or `ALTER TYPE ADD VALUE` per Step 1 finding) to include `'discord-comment'`. OP attachments reuse the existing manual-upload literal (captured in Step 1).
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
   REVOKE ALL ON internal.secrets FROM PUBLIC, anon, authenticated;

   CREATE OR REPLACE FUNCTION internal.get_service_role_key()
     RETURNS TEXT LANGUAGE sql SECURITY DEFINER SET search_path = ''
     AS $$ SELECT value FROM internal.secrets WHERE name = 'service_role_key' LIMIT 1; $$;
   REVOKE ALL ON FUNCTION internal.get_service_role_key() FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION internal.get_service_role_key() TO postgres;
   ```
2. **Migration header documents** the one-time operator action:
   ```sql
   -- After first deploy, run ONCE out-of-band (psql / Supabase SQL editor):
   --   INSERT INTO internal.secrets (name, value)
   --   VALUES ('service_role_key', '<paste service role key>')
   --   ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
   ```
3. **Rationale**: applies cleanly on `supabase db reset` without any pre-existing secret; `internal.get_service_role_key()` returns NULL until seeded; the importer cron will emit a 401 visibly when unseeded.

### Step 6: Migration — partial composite index on `discord_messages` (`supabase/migrations/<ts>_discord_messages_resource_op_idx.sql`)
**Scope:** Small
1. **Add** the supporting index for the promoter's every-10-min all-history scan:
   ```sql
   CREATE INDEX IF NOT EXISTS discord_messages_resource_op_idx
     ON public.discord_messages (channel_id, reaction_count DESC)
     WHERE message_id = thread_id AND is_deleted = FALSE;
   ```
   Predicates `message_id = thread_id` and `is_deleted = FALSE` are IMMUTABLE so partial indexing is legal. This keeps the working set to forum OPs only — a tiny subset of `discord_messages` — and makes the `reaction_count >= 5` scan an index range.
2. **Bonus** (optional, only if Step 9 shows planner preferring it): add `author_id` to the INCLUDE list to speed the `EXISTS (SELECT 1 FROM members …)` prefilter via index-only lookups.

### Step 7: Apply + verify (`supabase/migrations/`)
**Scope:** Small
1. **Run** `supabase db reset` on a scratch branch; confirm every migration applies cleanly.
2. **Verify** existing website tests still pass (additive only).

---

## Phase 2: Promoter as SQL-Native Stored Function (with dry-run)

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
       -- (see steps 2-6 below; branches on dry_run)
       RETURN NEXT;
     END LOOP;
   END $$;
   ```
2. **Log missing members** (writes-only; skip in dry-run path if cleaner, but safe to log either way):
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
3. **Discover + INSERT new assets** (true anti-join; no ON CONFLICT UPDATE; guarantees `v_ai = 0` on reruns):
   ```sql
   IF NOT dry_run THEN
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
4. **Update drifted descriptions** (only when content actually changed; guarantees `v_au = 0` on clean reruns):
   ```sql
   IF NOT dry_run THEN
     UPDATE public.assets a
        SET description    = m.content,
            name           = LEFT(split_part(m.content, E'\n', 1), 120),
            last_synced_at = NOW()
       FROM public.discord_messages m
      WHERE a.discord_thread_id = m.message_id
        AND a.source = 'discord_import'
        AND m.channel_id = v_ch
        AND (a.description IS DISTINCT FROM m.content
             OR a.name IS DISTINCT FROM LEFT(split_part(m.content, E'\n', 1), 120));
     GET DIAGNOSTICS v_au = ROW_COUNT;
   ELSE
     SELECT COUNT(*) INTO v_au FROM public.assets a
       JOIN public.discord_messages m
         ON m.message_id = a.discord_thread_id
      WHERE a.source = 'discord_import' AND m.channel_id = v_ch
        AND (a.description IS DISTINCT FROM m.content
             OR a.name IS DISTINCT FROM LEFT(split_part(m.content, E'\n', 1), 120));
   END IF;
   ```
5. **Insert + update comments** with the same split-statement pattern:
   - `INSERT … WHERE NOT EXISTS (SELECT 1 FROM asset_comments c WHERE c.discord_message_id = m.message_id) AND EXISTS (… members …) ON CONFLICT (discord_message_id) DO NOTHING;` → `v_ci`.
   - `UPDATE asset_comments c SET content = m.content, discord_edited_at = m.edited_at WHERE c.content IS DISTINCT FROM m.content OR c.discord_edited_at IS DISTINCT FROM m.edited_at;` → `v_cu`.
   - Follow-up `UPDATE asset_comments SET reply_to_comment_id = ac2.id FROM asset_comments ac2 WHERE asset_comments.reply_to_discord_message_id = ac2.discord_message_id AND asset_comments.reply_to_comment_id IS NULL;` to resolve self-references regardless of order.
6. **Enqueue media jobs** with CTE exactly as in v3 Step 7.4, using `regexp_match` (singular) on the CDN URL; `ON CONFLICT (discord_attachment_id) DO NOTHING` → `v_j`. In dry-run path, replace with a `SELECT COUNT(*)` from the CTE.
7. **Reconcile removed attachments** (`v_cmd`): mark `asset_comment_media.is_deleted=TRUE` (and `asset_media.is_deleted=TRUE`) for rows whose `media.metadata->>'discord_attachment_id'` no longer appears in `discord_messages.attachments` for the source message. Scoped to `is_deleted=FALSE` parent rows. Dry-run returns the candidate count.
8. **Propagate comment deletions** (`v_cd`): `UPDATE asset_comments SET is_deleted = TRUE FROM discord_messages WHERE asset_comments.discord_message_id = discord_messages.message_id AND discord_messages.is_deleted = TRUE AND asset_comments.is_deleted = FALSE;`. Dry-run: count only.
9. **Per-channel summary**:
   ```sql
   IF dry_run THEN
     RAISE NOTICE '[dry-run] channel %: would insert % assets, update %, insert % comments, update %, enqueue % jobs, % members missing',
       v_ch, v_ai, v_au, v_ci, v_cu, v_j, v_mm;
     INSERT INTO public.system_logs (logger_name, level, message, extra) VALUES (
       'discord_resource_promoter', 'info', 'dry-run summary',
       jsonb_build_object('dry_run', true, 'channel_id', v_ch,
         'assets_inserted', v_ai, 'assets_updated', v_au,
         'comments_inserted', v_ci, 'comments_updated', v_cu,
         'jobs_enqueued', v_j, 'members_missing', v_mm));
   ELSE
     UPDATE public.assets SET last_synced_at = NOW()
      WHERE discord_channel_id = v_ch AND source = 'discord_import';
   END IF;
   channel_id := v_ch;
   assets_inserted := v_ai; assets_updated := v_au;
   comments_inserted := v_ci; comments_updated := v_cu;
   jobs_enqueued := v_j; members_missing := v_mm;
   comment_media_marked_deleted := v_cmd; comments_marked_deleted := v_cd;
   dry_run := dry_run;
   ```
10. **Wrap** the top-level body in `EXCEPTION WHEN OTHERS THEN INSERT INTO public.system_logs (logger_name, level, message, extra) VALUES ('discord_resource_promoter', 'error', SQLERRM, jsonb_build_object('sqlstate', SQLSTATE))` so cron never crashes silently.

### Step 9: Schedule the promoter (`supabase/migrations/<ts>_schedule_discord_promoter.sql`)
**Scope:** Small
1. **Cron** — calls with no args, so `dry_run` defaults to FALSE:
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
2. **Verify** the returned table shows non-zero `assets_inserted`/`comments_inserted`/`jobs_enqueued` matching the ~15 OPs/30-day expectation, **with zero data written** (verify via `SELECT COUNT(*) FROM assets WHERE source='discord_import'` remains unchanged).
3. **Verify** `system_logs` has exactly one row per channel tagged `extra->>'dry_run' = 'true'`.
4. **Check query plan**: `EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM internal.discord_promote_resources(dry_run := TRUE);` — confirm the planner uses `discord_messages_resource_op_idx`.

### Step 11: Live promoter verification (staging)
**Scope:** Small
1. **Run** `SELECT * FROM internal.discord_promote_resources();` (live).
2. **Rerun** immediately; confirm `assets_inserted=0, assets_updated=0, comments_inserted=0, comments_updated=0, jobs_enqueued=0` (real idempotency, now that the INSERT uses a proper anti-join).
3. **Late-threshold regression**: set an old OP's `reaction_count = 5`, rerun; confirm it imports.
4. **Missing-member regression**: delete a referenced `members` row, rerun; confirm the sync completes, the skipped OP appears in `system_logs` (`logger_name='discord_resource_promoter', level='warning'`), and other OPs import.

---

## Phase 3: Media Importer Edge Function (Reuse Existing Cloudflare Infra)

### Step 12: Implement `discord-media-importer` (`supabase/functions/discord-media-importer/index.ts`)
**Scope:** Large
1. **Claim jobs** atomically with `FOR UPDATE SKIP LOCKED`, limit 10 per invocation.
2. **Refresh CDN URL** via the existing `refresh-media-urls` edge function; skip when the URL's `?ex=` timestamp is still in the future.
3. **Dispatch by content-type**:
   - **Video / animated gif**: initiate Cloudflare Stream direct upload (mirror `cloudflare-stream-backfill/index.ts:35-114`), POST Discord bytes, capture Stream UID. Insert `media` with the Stream UID populated in whichever column the webhook looks up (captured in Step 1). **No polling** — `cloudflare-stream-webhook` fills `cloudflare_playback_hls_url` + `cloudflare_thumbnail_url` on ready. Duration is **not** promised.
   - **Image**: stream Discord → Supabase Storage at `user-uploads/discord-imports/{discord_message_id}/{attachment_id}-{filename}`; insert `media` with public URL.
   - **Other** (zip/json/text): Storage upload, no thumbnail.
4. **Insert junction row** (`asset_media` or `asset_comment_media`) with `sort_order` = attachment index.
5. **`media.classification`**: `'discord-comment'` when `target_kind='asset_comment_media'`; else the existing manual-upload literal. `media.metadata` JSONB: `{discord_message_id, discord_channel_id, discord_attachment_id, original_cdn_url, imported_at}`.
6. **Mark done**: `UPDATE media_import_jobs SET status='done', media_id=$id`.
7. **On failure**: exponential backoff (`locked_until = NOW() + LEAST(60 * 2^attempts, 3600) * INTERVAL '1 second'`; bump `attempts`; write `last_error`; status→`pending`); after 5 attempts → `status='failed'`.
8. **Error logging**: on unhandled exception, `INSERT INTO public.system_logs (logger_name, level, message, extra) VALUES ('discord_media_importer', 'error', <err>, <payload>)` — using the real columns.

### Step 13: Confirm webhook contract (`supabase/functions/cloudflare-stream-webhook/index.ts`)
**Scope:** Small
1. **Verify** the webhook looks up `media` by the column our importer populates (Stream UID) and writes only the three fields (`cloudflare_playback_hls_url`, `cloudflare_thumbnail_url`, `storage_provider`). **Do not change the webhook**; importer adapts to it.

### Step 14: Smoke test one attachment (`supabase/functions/discord-media-importer/`)
**Scope:** Small
1. **Insert** one `media_import_jobs` row for a known-good video attachment from a `_resources` thread.
2. **Invoke** importer; confirm `media` row with Stream UID; wait ~1 min; confirm webhook populates HLS URL + thumbnail; confirm junction row.
3. **Re-invoke**; confirm no new claims.

---

## Phase 4: Importer Cron + Backfill

### Step 15: Schedule media importer with `internal.secrets` auth (`supabase/migrations/<ts>_schedule_discord_media_importer.sql`)
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
2. **Runbook note**: if `internal.secrets` isn't seeded, cron POSTs with empty Bearer and the edge function returns 401 — visible in Supabase function logs.

### Step 16: Full backfill (`supabase`)
**Scope:** Medium
1. **Observe** 1–2 cycles (~20 min); watch `media_import_jobs` drain.
2. **Sanity-check** one asset per channel: correct `type`, non-empty junction, playable HLS after webhook.

---

## Phase 5: Frontend (banodoco-website)

### Step 17: Update resource data hooks to include + filter new columns (`banodoco-website/src/hooks/`)
**Scope:** Medium
1. **Update SELECT + filter** in `useResources.ts:51-74`, `useCommunityResources.ts:118-127`, `useCommunityResource.ts:103-113`, `useUserProfile.ts:125-133` — add `source, discord_channel_id, is_hidden` and `.eq('is_hidden', false)`.
2. **Update TypeScript shapes** (`Asset`/`CommunityResource`) — three new fields optional.
3. **Regenerate** Supabase types.

### Step 18: Shared `AssetDescription` markdown component (`banodoco-website/src/components/resources/AssetDescription.tsx`)
**Scope:** Small
1. **Wrap** react-markdown + remark-gfm mirroring `PostBodyRenderer.tsx`'s plugin + sanitization config — minus custom embed tokens.

### Step 19: Swap plain-text description for markdown in `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Small
1. **Replace** the plain-text block at lines 166-169 with `<AssetDescription markdown={resource.description} />`. Universal — manual + Discord-imported.

### Step 20: Apply same markdown change in `ResourceModal` (`banodoco-website/src/pages/Resources/ResourceModal.tsx`)
**Scope:** Small
1. **Replace** the `whitespace-pre-line` `<p>` at lines 186-189 with `<AssetDescription markdown={asset.description} />`.

### Step 21: Add "Made with this" + "Discussion" + "From Discord" badge to `ResourceDetail`
**Scope:** Medium
1. **Below the existing gallery**, when `resource.source === 'discord_import'`:
   - **"Made with this"** grid — tiles are `<a href={media.url} target="_blank" rel="noopener noreferrer">` wrappers over the thumbnail. No lightbox in v1.
   - **"Discussion"** — flat chronological list via `useAssetComments`. Each row: author chip (`avatar_url` + `global_name ?? username` from `members`), relative timestamp, "Replying to @name" chip from `reply_to_comment_id`, `<AssetDescription>` for content, attachment tiles (same anchor pattern), reaction-count chip, "View on Discord" link built from a hard-coded `GUILD_ID` constant + `discord_thread_id` + `discord_message_id`.
2. **"From Discord" badge** — subtle pill when `resource.source === 'discord_import'`, labeled with the channel friendly name.

### Step 22: Add `useAssetComments` hook (`banodoco-website/src/hooks/useAssetComments.ts`)
**Scope:** Small
1. **Single query** joining `asset_comments` → `members` → `asset_comment_media` → `media`, filtered `is_deleted=false` on both `asset_comments` and `asset_comment_media`, ordered `discord_created_at ASC`.

### Step 23: Badge on resource card (`banodoco-website/src/pages/Resources/ResourceCard.tsx`)
**Scope:** Small
1. **Render** the "From Discord" badge when `resource.source === 'discord_import'`.

---

## Phase 6: Observability + Ops

### Step 24: Health-check SQL (`supabase/sql/discord_sync_health.sql`)
**Scope:** Small
1. **Persist** reusable queries: jobs stuck `in_progress` past `locked_until`; `failed` in last 24h grouped by `last_error`; per-asset `last_synced_at` staleness; recent `system_logs` rows where `logger_name IN ('discord_resource_promoter','discord_media_importer')`.

### Step 25: Rollback + risk register + follow-ups (`supabase/migrations/ROLLBACK_discord_sync.md`)
**Scope:** Small
1. **Rollback**: drop tables + columns in reverse order; `cron.unschedule(…)` both schedules; `DROP FUNCTION internal.discord_promote_resources(BOOLEAN)`; `DROP INDEX discord_messages_resource_op_idx`; drop helper function then `internal.secrets` table; revert frontend PR.
2. **Risks**: Cloudflare Stream quota (negligible); Discord rate limits (paced by 10-job claim × 2 min); edge-function timeout (comfortable); Storage cost (monitor); unseeded `internal.secrets` → 401s.
3. **Follow-ups explicitly tracked**: comment lightbox, Cloudflare duration webhook extension, `supabase_vault` migration from `internal.secrets`.

---

## Execution Order

1. **Phase 1** Steps 1–7 (audit + schema + `internal.secrets` + partial index) — foundation.
2. **Phase 2** Steps 8–10 (promoter function + cron + **dry-run first-working-version milestone**).
3. **Phase 2** Step 11 (live promoter + late-threshold + missing-member regression tests).
4. **Phase 3** Steps 12–14 (importer + webhook reconciliation + single-attachment smoke test).
5. **Phase 4** Steps 15–16 (importer cron + full backfill).
6. **Phase 5** Steps 17–23 (frontend) — can start in parallel with Phase 4.
7. **Phase 6** Steps 24–25 (observability + rollback doc).

## Validation Order

1. `supabase db reset` applies every Phase 1 migration cleanly.
2. `EXPLAIN ANALYZE` on the promoter's discovery query confirms `discord_messages_resource_op_idx` is used.
3. **Dry-run** `SELECT * FROM internal.discord_promote_resources(dry_run := TRUE);` writes zero data, logs a dry-run summary to `system_logs`, and reports expected counts.
4. **Live** `SELECT internal.discord_promote_resources();` imports every qualifying OP.
5. **Rerun live**: `assets_inserted=0, assets_updated=0, comments_inserted=0, comments_updated=0, jobs_enqueued=0` (idempotency holds because of the true anti-join).
6. **Late-threshold regression**: old OP flipped to `reaction_count=5` imports on the next tick.
7. **Missing-member regression**: deleted `members` row → promoter completes, skipped OP logged with `logger_name='discord_resource_promoter'`, others import.
8. **Attachment-URL regex**: `SELECT regexp_match('https://cdn.discordapp.com/attachments/123/456/foo.png?ex=a&is=b&hm=c', '/attachments/\d+/(\d+)/')` returns `{456}`.
9. **Attachment-removal-on-edit**: simulated; corresponding junction row gets `is_deleted=TRUE`.
10. **Importer smoke test**: one video → Stream UID + HLS via webhook; one image → Storage URL.
11. **ResourceDetail render**: imported asset shows markdown description, Made-with-this tiles (`<a target="_blank">`), Discussion list, badge, working View-on-Discord links.
12. **Manual asset render**: description renders markdown; no Discord sections appear; unchanged otherwise.
13. **is_hidden kill-switch**: flipping `true` makes the asset disappear from all four hook-backed surfaces and from `ResourceDetail` by URL.
14. **Final idempotency**: rerun promoter and importer; zero net new rows.
