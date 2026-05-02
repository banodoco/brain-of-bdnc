# Implementation Plan: Sync Discord `_resources` Posts into banodoco-website (v2)

## Overview

Promote qualifying Discord forum OPs in the 5 `_resources` channels into `assets` rows on the shared Supabase project (`ujlwuvkrxlvoswwkerdf`), with thread replies as `asset_comments`, reply attachments as `asset_comment_media`, and OP attachments into existing `asset_media`. Media is rehosted into Cloudflare Stream (video) or Supabase Storage (images/other).

**Revision summary vs. v1** — the gate identified 17 concrete defects. The key architectural pivots in this revision:

1. **Promoter is PL/pgSQL, not an edge function.** The existing pg_cron pattern in the repo (`supabase/migrations/20260403000005_schedule_priority_scores_cron.sql`) schedules direct SQL, not HTTP calls. The promoter is pure data-munging over `discord_messages`/`discord_reactions`/`members`, so making it a stored function matches the existing pattern, eliminates the edge-function auth problem, and removes the 30s wall-clock risk.
2. **Discovery no longer cursors on `created_at`.** `discord_messages.reaction_count` is updated by the `sync_message_reactors` trigger *after* message creation (`supabase/migrations/20260319100000_fix_sync_reactors_trigger.sql:1-22`), so `created_at > cursor` permanently misses OPs that cross the 5-reaction threshold late. New approach: anti-join on `assets.discord_thread_id` with a rolling 60-day window — any OP in the last 60 days that is `reaction_count >= 5` and not yet imported gets upserted, regardless of age within the window.
3. **Media importer does not poll Cloudflare.** Reuse the existing `supabase/functions/cloudflare-stream-webhook` (for completion) and `cloudflare-stream-backfill` (for retries). The importer only *initiates* the Stream upload and records the Stream UID; the webhook populates HLS/thumbnail/duration when Cloudflare is done.
4. **Frontend work lands on `banodoco-website/src/pages/ResourceDetail/index.tsx`**, the real live surface (ResourceModal has zero callers per `rg`). The user's explicit markdown-rendering scope note is applied at both `ResourceDetail` (where users actually see it) *and* `ResourceModal` (the path the note names), via a shared `AssetDescription` component using react-markdown + remark-gfm, mirroring `banodoco-website/src/components/posts/PostBodyRenderer.tsx` — no custom `::art[uuid]` tokens. Applies to manual + Discord-imported assets.
5. **Asset typing is `workflow`/`lora`, not `tool`/`lora`.** `SubmitResource/index.tsx:10-13` restricts asset type to `lora | workflow`; `ResourceCard.tsx:151-154`, `useResourceFilters.ts:159-160` branch on those values. Channel mapping: `resources` → `workflow`; `flux_resources`, `wan_resources`, `ltx_resources`, `acestep_resources` → `lora`.
6. **No member fallback.** `members.username` is NOT NULL per `supabase/migrations/20251101000000_create_discord_tables.sql:38-64` and the archive writer always supplies it. Trust the archive pipeline to populate `members` before the message that references the author (if it ever races, log and skip — the next promoter run will pick it up).
7. **`is_hidden` and the new asset columns are threaded through every resource-consuming hook:** `useResources.ts`, `useCommunityResources.ts`, `useCommunityResource.ts`, `useUserProfile.ts` all gain `source`, `discord_channel_id`, `is_hidden` in their SELECT and `.eq('is_hidden', false)` in their filter.
8. **`asset_comment_media` gains `is_deleted BOOLEAN DEFAULT FALSE`** and the promoter reconciles removed-attachment-on-edit by flipping it; the UI filters deleted rows out.
9. **Cron auth pattern**: one first-class vault-bootstrap migration seeds the service-role key (for the importer's HTTP call); the promoter needs no secret because it's SQL.

**Sandbox caveat:** This planning session is restricted to `brain-of-bndc/`; file/line references into `banodoco-website/` and `supabase/` are taken from the gate's critique evidence and the user's scope note. Step 1 re-verifies each one before migrations are written.

---

## Phase 1: Schema Migrations

### Step 1: Confirm current shapes before writing migrations (`supabase/migrations/`, `banodoco-website/src/`)
**Scope:** Small
1. **Re-read** these exact files to lock in the existing schema + UI contracts: `supabase/migrations/20251101000000_create_discord_tables.sql` (members DDL), `supabase/migrations/20260319100000_fix_sync_reactors_trigger.sql` (reaction-count trigger), `supabase/migrations/20260403000005_schedule_priority_scores_cron.sql` (existing cron pattern), `supabase/functions/cloudflare-stream-webhook/index.ts` + `cloudflare-stream-backfill/index.ts` (completion contract), `supabase/config.toml:45-46` (vault status), the most recent migration defining `assets`/`media`/`asset_media` (column list + CHECK constraints), `banodoco-website/src/pages/SubmitResource/index.tsx:10-13` (type enum), `banodoco-website/src/pages/ResourceDetail/index.tsx:166-169` (plain-text description), and `banodoco-website/src/components/posts/PostBodyRenderer.tsx` (markdown pipeline).
2. **Capture** what the existing `media.classification` enum/CHECK contains; decide whether `'discord-comment'` is a CHECK-list addition or an ENUM value.
3. **Capture** the existing `cloudflare-stream-webhook` input contract (what `media` row it expects: which UID column, which status column).

### Step 2: Migration — extend `assets` (`supabase/migrations/<ts>_assets_discord_import_cols.sql`)
**Scope:** Small
1. **Add columns** (all nullable or defaulted so existing rows stay valid):
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
   CREATE INDEX assets_is_hidden_idx ON assets(is_hidden) WHERE is_hidden = TRUE;
   ```

### Step 3: Migration — `asset_comments` + `asset_comment_media` with `is_deleted` (`supabase/migrations/<ts>_asset_comments.sql`)
**Scope:** Medium
1. **Create `asset_comments`** per the brief (FK → `members(member_id)`, UNIQUE on `discord_message_id`, indexes on `(asset_id, discord_created_at)` and `discord_thread_id`).
2. **Create `asset_comment_media`** with an additional column to cover "attachment removed on Discord edit":
   ```sql
   CREATE TABLE asset_comment_media (
     comment_id UUID REFERENCES asset_comments(id) ON DELETE CASCADE,
     media_id UUID REFERENCES media(id) ON DELETE CASCADE,
     sort_order INT NOT NULL DEFAULT 0,
     is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
     PRIMARY KEY (comment_id, media_id)
   );
   ```
   Also add the analogous `asset_media.is_deleted` column (OP attachments can be removed too):
   ```sql
   ALTER TABLE asset_media ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
   ```
3. **RLS**: public-read on both junctions filtered by `is_deleted=false`; no write policies (service-role only). Public-read on `asset_comments` filtered by `is_deleted=false`.

### Step 4: Migration — `media` classification + `media_import_jobs` (`supabase/migrations/<ts>_media_import_jobs.sql`)
**Scope:** Medium
1. **Extend `media.classification`** (CHECK rewrite or `ALTER TYPE … ADD VALUE`, per Step 1 finding) to include `'discord-comment'`. OP attachments use the existing value manual uploads use (confirmed in Step 1 — likely `'post'` or `'primary'`; capture the exact literal).
2. **Create `media_import_jobs`** exactly per brief, plus unique index on `discord_attachment_id WHERE NOT NULL`, and claimable-index on `(status, locked_until) WHERE status IN ('pending','in_progress')`. RLS enabled, no policies (service-role only).

### Step 5: Migration — vault bootstrap for cron HTTP auth (`supabase/migrations/<ts>_vault_service_role_for_cron.sql`)
**Scope:** Small
1. **Enable** `supabase_vault` if not already enabled; store the service-role key so `pg_cron` jobs can authenticate `net.http_post` calls:
   ```sql
   SELECT vault.create_secret(
     current_setting('app.settings.service_role_key_value', true),  -- set once via psql or CLI
     'service_role_key'
   );
   ```
2. **Create a SECURITY DEFINER helper** that cron jobs call to fetch the key without exposing it in `pg_stat_statements`:
   ```sql
   CREATE OR REPLACE FUNCTION internal.get_service_role_key() RETURNS text
     SECURITY DEFINER SET search_path = '' AS $$
       SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'service_role_key' LIMIT 1;
     $$ LANGUAGE sql;
   REVOKE ALL ON FUNCTION internal.get_service_role_key() FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION internal.get_service_role_key() TO postgres;
   ```
3. **Document** in the migration file header: operator must set the secret once out-of-band (`SELECT vault.update_secret(...)`) before cron runs. If `supabase_vault` isn't usable in this project, fall back to a separate `internal.secrets` table owned by `postgres` only — decision deferred to Step 1 findings.

### Step 6: Apply + verify migrations (`supabase/migrations/`)
**Scope:** Small
1. **Run** `supabase db reset` on a scratch branch; confirm all migrations apply cleanly and idempotently.
2. **Verify** that existing website tests still pass (no column renames, only additions).

---

## Phase 2: Promoter as SQL-Native Stored Function

### Step 7: Implement `discord_promote_resources()` PL/pgSQL function (`supabase/migrations/<ts>_discord_promoter_fn.sql`)
**Scope:** Large
1. **Create** the function in a dedicated schema (e.g. `internal` or `discord_sync`) and grant execute only to `postgres`:
   ```sql
   CREATE OR REPLACE FUNCTION internal.discord_promote_resources()
     RETURNS TABLE(channel_id BIGINT, assets_upserted INT, comments_upserted INT, jobs_enqueued INT)
     LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
   DECLARE
     v_channel BIGINT;
     v_assets INT;
     v_comments INT;
     v_jobs INT;
   BEGIN
     FOR v_channel IN SELECT unnest(ARRAY[
       1149372684220768367::bigint, 1275200992136400967::bigint,
       1373291419434877078::bigint, 1457981813120176138::bigint,
       1472633200491626526::bigint
     ])
     LOOP
       -- (discovery + upsert + comments + jobs; see below)
       RETURN NEXT;
     END LOOP;
   END $$;
   ```
2. **Discover-and-upsert OPs** (no cursor — anti-join on `assets.discord_thread_id` closes the late-threshold gap):
   ```sql
   WITH eligible AS (
     SELECT m.message_id, m.channel_id, m.author_id, m.content,
            m.attachments, m.created_at, m.edited_at, m.is_deleted, m.thread_id
       FROM public.discord_messages m
      WHERE m.channel_id = v_channel
        AND m.message_id = m.thread_id
        AND m.reaction_count >= 5
        AND m.is_deleted = FALSE
        AND m.created_at > NOW() - INTERVAL '60 days'
   )
   INSERT INTO public.assets (
     id, name, description, type, member_id, admin_status,
     source, discord_channel_id, discord_thread_id,
     reactions_reached_threshold_at, imported_at, last_synced_at
   )
   SELECT gen_random_uuid(),
          LEFT(split_part(e.content, E'\n', 1), 120),
          e.content,
          CASE e.channel_id
            WHEN 1149372684220768367 THEN 'workflow'
            ELSE 'lora'
          END,
          e.author_id, 'Listed', 'discord_import',
          e.channel_id, e.message_id, NOW(), NOW(), NOW()
     FROM eligible e
   ON CONFLICT (discord_thread_id) DO UPDATE
     SET description       = EXCLUDED.description,
         name              = EXCLUDED.name,
         last_synced_at    = NOW();
   -- reactions_reached_threshold_at + imported_at intentionally NOT updated on conflict
   GET DIAGNOSTICS v_assets = ROW_COUNT;
   ```
   Channel → type mapping: `resources` (1149372684220768367) → `workflow`; all four model-family channels → `lora`.
3. **Upsert replies** for every asset owned by this channel: join `discord_messages` on `thread_id = assets.discord_thread_id AND message_id != thread_id`. Map `edited_at`→`discord_edited_at`, `created_at`→`discord_created_at`, propagate `is_deleted`. Resolve `reply_to_comment_id` in a follow-up `UPDATE` after the insert so self-references work regardless of order.
4. **Enqueue media jobs** from `discord_messages.attachments` JSONB (archive format per `brain-of-bndc/scripts/archive_discord.py:463-469` is `[{url, filename}, …]`). Parse Discord's attachment ID out of the URL path in SQL:
   ```sql
   WITH atts AS (
     SELECT m.message_id,
            m.channel_id,
            (SELECT a_inner.id FROM public.assets a_inner WHERE a_inner.discord_thread_id = m.thread_id) AS op_asset_id,
            (SELECT c.id FROM public.asset_comments c WHERE c.discord_message_id = m.message_id) AS comment_id,
            att ->> 'url' AS url,
            att ->> 'filename' AS filename,
            (regexp_matches(att ->> 'url', '/attachments/\d+/(\d+)/'))[1]::bigint AS att_id
       FROM public.discord_messages m,
            jsonb_array_elements(coalesce(m.attachments,'[]'::jsonb)) att
      WHERE m.thread_id IN (SELECT discord_thread_id FROM public.assets
                             WHERE discord_channel_id = v_channel AND source='discord_import')
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
5. **Reconcile removed attachments**: mark `asset_comment_media.is_deleted = TRUE` (and the same on `asset_media`) for any row whose `media.metadata->>'discord_attachment_id'` no longer appears in the current `discord_messages.attachments` for that `discord_message_id`. Only applies to rows where the parent comment's `is_deleted = FALSE` (so edit-removed ≠ comment-deleted).
6. **Mark comments deleted**: `UPDATE asset_comments SET is_deleted = TRUE FROM discord_messages WHERE asset_comments.discord_message_id = discord_messages.message_id AND discord_messages.is_deleted = TRUE`.
7. **Update `assets.last_synced_at = NOW()`** per channel at end.
8. **No member fallback** — trust archive pipeline. If an FK would fail, the INSERT errors and is logged; next run picks it up when archive has caught up.

### Step 8: Schedule the promoter directly via pg_cron (`supabase/migrations/<ts>_schedule_discord_promoter.sql`)
**Scope:** Small
1. **Follow the existing pattern** in `supabase/migrations/20260403000005_schedule_priority_scores_cron.sql`:
   ```sql
   SELECT cron.schedule(
     'discord-resource-promoter',
     '*/10 * * * *',
     $$ SELECT internal.discord_promote_resources(); $$
   );
   ```
   No HTTP call, no secret, no edge function — matches the repo's established cron pattern.

### Step 9: Verify promoter against live data (`supabase`)
**Scope:** Small
1. **Run** `SELECT * FROM internal.discord_promote_resources();` manually on a staging branch; confirm ~30 assets materialize for 60-day backfill, matching the ~15 OPs/30-day volume noted in the brief.
2. **Re-run** immediately; verify all four counts return 0 (idempotency).
3. **Simulate** a late-threshold OP: set a 45-day-old message's `reaction_count` to `5` manually, rerun; confirm it gets imported (covers FLAG-001).

---

## Phase 3: Media Importer Edge Function (Reuse Existing Cloudflare Infra)

### Step 10: Implement `discord-media-importer` (`supabase/functions/discord-media-importer/index.ts`)
**Scope:** Large
1. **Claim jobs** atomically with `FOR UPDATE SKIP LOCKED` (same pattern as v1), up to N=10 per invocation.
2. **Refresh CDN URL** by calling the existing `refresh-media-urls` edge function. Skip refresh when `original_cdn_url`'s `?ex=` timestamp is still in the future.
3. **Dispatch by content-type**:
   - **Video / animated gif**: initiate a Cloudflare Stream upload via the direct-upload URL API (the same path `cloudflare-stream-backfill/index.ts:35-114` uses), receive a Stream UID, insert a `media` row with the Stream UID and the existing "pending" status the webhook expects. **Do not poll** — the existing `cloudflare-stream-webhook` (`supabase/functions/cloudflare-stream-webhook/`) will populate `playback.hls`, `thumbnail`, and `duration` when Cloudflare finishes; the existing `cloudflare-stream-backfill` job handles retries. This reconciles with the infrastructure called out in FLAG-005 / all_locations-2.
   - **Image**: stream the download from Discord, upload directly to Supabase Storage at `user-uploads/discord-imports/{discord_message_id}/{attachment_id}-{filename}`, insert a `media` row with the public URL populated immediately.
   - **Other** (zip, json, text): upload to the same Storage path, no thumbnail.
4. **Insert junction row** into `asset_media` or `asset_comment_media` with `sort_order` = attachment index in the Discord message.
5. **`media.classification`**: `'discord-comment'` when `target_kind='asset_comment_media'`, else the existing manual-upload classification (captured in Step 1). `media.metadata` JSONB carries `{discord_message_id, discord_channel_id, discord_attachment_id, original_cdn_url, imported_at}`.
6. **Mark done**: `UPDATE media_import_jobs SET status='done', media_id=$id`. The importer's "done" means "handed off"; for video, the Stream webhook completes the media row asynchronously.
7. **On failure**: exponential backoff (`locked_until = NOW() + LEAST(60 * 2^attempts, 3600) * INTERVAL '1 second'`, reset to `pending`, bump `attempts`, write `last_error`). After 5 attempts → `status='failed'`.
8. **No Cloudflare polling in this function** — the wall-clock budget stays well under 400s because each job is bounded by a single download + a single upload.

### Step 11: Confirm webhook contract (`supabase/functions/cloudflare-stream-webhook/index.ts`)
**Scope:** Small
1. **Verify** the webhook already writes `hls_url`/`thumbnail_url`/`duration` to the `media` row addressed by Stream UID. If the `media` row shape is incompatible (e.g. webhook addresses by a different key), we wire a small mapping column or reuse whatever key the webhook already looks for. No changes to the webhook itself unless reconciliation demands it — preference is to add, not fork.

### Step 12: Smoke test one attachment end-to-end (`supabase/functions/discord-media-importer/`)
**Scope:** Small
1. **Insert** a single `media_import_jobs` row targeting one known-good video attachment from a _resources thread.
2. **Invoke** the importer once; confirm `media` row exists with Stream UID; wait a minute for webhook; confirm `hls` URL populates; confirm junction row exists.
3. **Re-invoke**; confirm no new work claimed.

---

## Phase 4: Cron Registration for Importer + Full Backfill

### Step 13: Schedule media importer via pg_cron + vault (`supabase/migrations/<ts>_schedule_discord_media_importer.sql`)
**Scope:** Small
1. **Schedule** the only cron job that needs HTTP auth:
   ```sql
   SELECT cron.schedule(
     'discord-media-importer',
     '*/2 * * * *',
     $$
     SELECT net.http_post(
       url     := 'https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/discord-media-importer',
       headers := jsonb_build_object(
         'Authorization', 'Bearer ' || internal.get_service_role_key(),
         'Content-Type',  'application/json'
       ),
       body    := '{}'::jsonb
     );
     $$
   );
   ```
2. **Document** that the operator must `SELECT vault.update_secret('<uuid>', '<service_role_key>')` once before enabling the schedule; the migration file header has the exact command.

### Step 14: Let the full 60-day backfill run (`supabase`)
**Scope:** Medium
1. **Observe** 1–2 full cron cycles (~20 min). Watch `media_import_jobs` drain; confirm per-channel counts match expectations (~15 OPs/30 days × 2 = ~30 assets).
2. **Sanity-check** one asset per channel: correct `type` (`workflow` for `resources`, `lora` elsewhere), non-empty `asset_media`, playable HLS URL.

---

## Phase 5: Frontend (banodoco-website)

### Step 15: Update resource data hooks to include + filter new columns (`banodoco-website/src/hooks/`)
**Scope:** Medium
1. **Update SELECT columns in all four hooks** (addresses callers-1 / all_locations-1 / scope-2):
   - `useResources.ts:51-74` — add `source, discord_channel_id, is_hidden` to the SELECT list and `.eq('is_hidden', false)` to the filter.
   - `useCommunityResources.ts:118-127` — same.
   - `useCommunityResource.ts:103-113` — same.
   - `useUserProfile.ts:125-133` — same (so profile counts honor the kill switch).
2. **Update TypeScript shapes** for `Asset` / `CommunityResource` / whichever interface each hook returns, adding the three new fields as optional.
3. **Regenerate** Supabase types (`supabase gen types typescript …`) so `asset_comments`/`asset_comment_media`/`media_import_jobs` are present in the generated file.

### Step 16: Shared `AssetDescription` markdown component (`banodoco-website/src/components/resources/AssetDescription.tsx`)
**Scope:** Small
1. **Create** a small component that wraps `react-markdown` + `remark-gfm` with the same plugin set and styling as `banodoco-website/src/components/posts/PostBodyRenderer.tsx`, **but** does NOT register the custom embed tokens (`::art[uuid]` etc.) per the user's explicit scope note. Signature: `<AssetDescription markdown={asset.description} />`.
2. **Sanitize** via the same rehype-sanitize (or whatever `PostBodyRenderer` uses) config to keep the attack surface identical.

### Step 17: Swap plain-text description for markdown in `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Small
1. **Replace** the plain-text block at lines 166-169 with `<AssetDescription markdown={resource.description} />`. Applies to **manual + Discord-imported** assets (per user's scope note).
2. **Do not gate** the markdown change on `source` — universal.

### Step 18: Apply same markdown change in `ResourceModal` (`banodoco-website/src/pages/Resources/ResourceModal.tsx`)
**Scope:** Small
1. **Replace** `<p className="... whitespace-pre-line">{asset.description}</p>` at lines 186-189 with `<AssetDescription markdown={asset.description} />` (satisfies `issue_hints-1` literally; even though the modal is currently uncalled, the user named this file explicitly and a future caller will render correctly).

### Step 19: Add "Made with this" + "Discussion" + "From Discord" badge to `ResourceDetail` (`banodoco-website/src/pages/ResourceDetail/index.tsx`)
**Scope:** Medium
1. **Below the existing gallery**, when `resource.source === 'discord_import'`, render:
   - **"Made with this"** — grid of every `media` row reachable via the asset's `asset_comment_media` rows (newest parent-comment first, `is_deleted=false` only). Clicking a tile opens the existing lightbox anchored to the parent comment.
   - **"Discussion"** — chronological flat list of `asset_comments` (via the new `useAssetComments` hook, Step 20). Each row: author chip (`avatar_url` + `global_name ?? username` from `members`), relative timestamp, "Replying to @name" chip resolved via `reply_to_comment_id` (fallback text when only `reply_to_discord_message_id` is set), `<AssetDescription>` for the `content`, attachment thumbnails, reaction-count chip, and a "View on Discord" link built as `https://discord.com/channels/{GUILD_ID}/{comment.discord_thread_id}/{comment.discord_message_id}` (guild ID hard-coded in a shared constant; all 5 channels share one guild).
2. **"From Discord" badge** — subtle pill rendered when `resource.source === 'discord_import'`, label = friendly channel name (Flux, Wan, LTX, AceStep, Resources).

### Step 20: Add `useAssetComments` hook (`banodoco-website/src/hooks/useAssetComments.ts`)
**Scope:** Small
1. **Single query** joining `asset_comments` → `members` (author) → `asset_comment_media` → `media`, filtered `is_deleted=false` on both `asset_comments` and `asset_comment_media`, ordered by `discord_created_at ASC`. Mirror the existing hook pattern in the repo.

### Step 21: Propagate badge + filter to cards and filter logic (`banodoco-website/src/pages/Resources/ResourceCard.tsx`, `banodoco-website/src/hooks/useResourceFilters.ts`)
**Scope:** Small
1. **`ResourceCard.tsx`**: render the "From Discord" badge; no new branch on `type`.
2. **`useResourceFilters.ts`**: no change unless the existing filter predicate doesn't honor `is_hidden` (the hook's SELECT change in Step 15 already handles this via `.eq('is_hidden', false)`, so filters just work).

---

## Phase 6: Observability + Ops

### Step 22: Health-check SQL (`supabase/sql/discord_sync_health.sql`)
**Scope:** Small
1. **Persist** three reusable queries: (a) jobs stuck `in_progress` past `locked_until`; (b) count of `failed` jobs in last 24h with top `last_error` values; (c) per-asset staleness via `assets.last_synced_at`.

### Step 23: `system_logs` writes on failure (`supabase/functions/discord-media-importer/`, `internal.discord_promote_resources()`)
**Scope:** Small
1. **Importer**: on unhandled exception, insert a `system_logs` row with `component='discord_media_importer'`. (Exact column set confirmed in Step 1.)
2. **Promoter**: wrap the function body in `EXCEPTION WHEN OTHERS THEN INSERT INTO system_logs …` with `component='discord_resource_promoter'`.

### Step 24: Per-phase rollback doc (`supabase/migrations/ROLLBACK_discord_sync.md`)
**Scope:** Small
1. **Document**:
   - Schema: drop tables + new columns in reverse order; drop `'discord-comment'` from the `media.classification` check last.
   - Promoter: `cron.unschedule('discord-resource-promoter'); DROP FUNCTION internal.discord_promote_resources();`. Optional hard rollback: `DELETE FROM assets WHERE source='discord_import'` (CASCADE cleans up comments/junctions).
   - Importer: `cron.unschedule('discord-media-importer')`. Rehosted Cloudflare Stream / Storage objects need manual cleanup.
   - Vault bootstrap: drop the secret + the helper function last.
   - Frontend: revert PR; all changes are additive (hook SELECTs gain columns; UI gains sections + a component).

### Step 25: Risk register (in the same rollback doc)
**Scope:** Small
1. **List**:
   - **Cloudflare Stream quota** — ~30 video attachments in 60-day backfill is negligible; monitor post-cutover.
   - **Discord rate limits** (50 req/s) — the importer's per-job CDN refresh is the only hot path; spaced naturally by the 10-job claim limit per 2-min cycle.
   - **pg_cron edge-function timeout** — non-issue for the promoter (now SQL-native); importer worst case ~350s is well under the 400s edge timeout.
   - **Storage cost** — 60-day backfill of ~30 assets × modest attachment sizes; monitor.
   - **Vault secret bootstrap** — required manual step; if skipped, the media importer cron fails at the `Authorization` header. Health-check (b) surfaces this.

---

## Execution Order

1. **Phase 1** Steps 1–6 (schema + vault bootstrap) — foundation; must merge before any runtime code.
2. **Phase 2** Steps 7–9 (SQL promoter + its cron + live verification) — zero dependency on Cloudflare; proves the data-munging end-to-end.
3. **Phase 3** Steps 10–12 (importer edge function + webhook reconciliation + one-attachment smoke test) — gated on Phase 2 for real jobs to import.
4. **Phase 4** Steps 13–14 (importer cron + full backfill) — flip the switch.
5. **Phase 5** Steps 15–21 (frontend) — can start in parallel with Phase 4 once real data exists.
6. **Phase 6** Steps 22–25 (observability + docs) — land alongside cutover.

## Validation Order

1. `supabase db reset` on scratch branch proves migrations apply cleanly (Phase 1).
2. Manual `SELECT internal.discord_promote_resources()` on staging shows ~30 imports and zero on rerun (Phase 2).
3. Late-threshold simulation (old OP, manually set reaction_count=5) imports correctly (FLAG-001 regression test).
4. Single-job importer smoke test: video → HLS via webhook; image → direct Storage URL (Phase 3).
5. One 10-min cron cycle drains `pending` jobs; no `failed` beyond isolated attachments with logged reasons (Phase 4).
6. ResourceDetail render smoke test: imported asset shows markdown description, "Made with this", "Discussion", "From Discord" badge, working "View on Discord" links (Phase 5).
7. Manual asset render smoke test: description renders as markdown; no new sections appear; behavior otherwise unchanged (Phase 5).
8. `UPDATE assets SET is_hidden=TRUE WHERE id=<one imported>` — confirm it disappears from `useResources`, `useCommunityResources`, `useCommunityResource`, `useUserProfile`-backed surfaces (Phase 5, addresses scope-2).
9. Final idempotency rerun of both promoter and importer — zero net new rows.
