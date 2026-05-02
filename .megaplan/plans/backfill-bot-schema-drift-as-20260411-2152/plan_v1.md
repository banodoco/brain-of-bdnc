# Implementation Plan: Backfill bot schema drift as Supabase CLI migrations

## Overview
The bot has accumulated schema definitions as "shadow" scripts under `brain-of-bndc/sql/` (three files: `payments.sql`, `admin_payment_intents.sql`, `social_publications.sql`) that were hand-applied to production but were never promoted into Supabase CLI migrations. Goal: create timestamped, idempotent migration files in the shared workspace-level `supabase/migrations/` directory, delete the shadow folder, and update the repo's docs/workflow notes. Production already matches the desired state, so the new migrations must be pure no-ops on a live DB (idempotent guards on every statement).

Key facts gathered:
- `brain-of-bndc/sql/` contains exactly 3 files (`sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql`). Their current contents are already mostly idempotent (`create ... if not exists`, `add column if not exists`, `create or replace function`, guarded backfill `UPDATE`). A few statements (`REVOKE`, `ENABLE ROW LEVEL SECURITY`, `DROP CONSTRAINT IF EXISTS`/`ADD CONSTRAINT`) are inherently idempotent or already guarded.
- `brain-of-bndc/supabase/` does **not** exist on disk; `git ls-files` shows no `supabase/**` tracked files in the bot repo. The real target is the **sibling** path `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` (outside the session sandbox — see Questions).
- No Python runtime code loads from `sql/...`. Only documentation references the path: `docs/payments.md` (10 line-number references), `structure.md:98-101` (describes a bot-local `supabase/` tree that no longer exists), `.cursorrules:57` (naming convention note).
- FK dependency: `admin_payment_intents` references `wallet_registry(wallet_id)` and `payment_requests(payment_id)`, so its migration must sort after `payments`. `social_publications` is independent.

Constraints:
- File changes only, no live DB access.
- Must stay proportional: this is a cut/paste-with-guards + docs update, not a refactor.
- Executor will need write access to the sibling `supabase/migrations/` path; the planning session's sandbox is `brain-of-bndc`-only.

## Main Phase

### Step 1: Re-confirm shadow SQL and its idempotency shape (`sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql`)
**Scope:** Small
1. **Re-read** each shadow file end to end and verify every statement is idempotent against a production DB that already has the final state:
   - `sql/payments.sql:8` — `create extension if not exists pgcrypto;` ✓
   - `sql/payments.sql:10-18` — `create or replace function public.set_payment_updated_at()` ✓
   - `sql/payments.sql:20-32` — `create table if not exists public.wallet_registry` ✓
   - `sql/payments.sql:34-41` — unique / index `if not exists` ✓
   - `sql/payments.sql:43-47` — `drop trigger if exists ... create trigger` ✓
   - `sql/payments.sql:49-76` — `payment_channel_routes` table + indexes + trigger, all guarded ✓
   - `sql/payments.sql:78-164` — `payment_requests` table + indexes + trigger ✓
   - `sql/payments.sql:166-198` — `create or replace function public.claim_due_payment_requests(...)` ✓
   - `sql/payments.sql:200-207` — `alter table ... enable row level security` + `revoke ...` (inherently idempotent) ✓
   - `sql/payments.sql:209-210` — `alter table ... add column if not exists tx_signature_history` ✓
   - `sql/payments.sql:212-223` — guarded backfill `UPDATE ... where ... and jsonb_array_length(tx_signature_history) = 0` ✓
   - `sql/admin_payment_intents.sql:13-48` — `create table if not exists` ✓
   - `sql/admin_payment_intents.sql:50-60` — `add column if not exists` (×4) ✓
   - `sql/admin_payment_intents.sql:62-79` — `drop constraint if exists ... add constraint ...` ✓
   - `sql/admin_payment_intents.sql:81-95` — indexes + trigger, all guarded ✓
   - `sql/admin_payment_intents.sql:97-99` — `enable row level security` + `revoke ...` ✓
   - `sql/social_publications.sql:1-126` — extension, `create or replace function`, `create table if not exists`, `add column if not exists`, `create index if not exists`, `drop trigger if exists + create`, `create or replace function claim_due_social_publications`, `alter ... enable rls`, `revoke`, unique partial indexes — all idempotent ✓
2. **Note** any not-idempotent lines found (expected: none based on current reading). If any `CREATE INDEX` without `IF NOT EXISTS` or `CREATE TRIGGER` without prior `DROP IF EXISTS` is discovered, wrap it before copying into the migration.
3. **Note** that `sql/payments.sql:200-201` ("RLS is already enabled below") is a header comment; preserve verbatim in the payments migration so the RLS posture is documented in the migration too.

### Step 2: Create `supabase/migrations/20260411220000_backfill_payments.sql`
**Scope:** Medium
**Target path:** `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/20260411220000_backfill_payments.sql` (sibling repo — see Questions on sandbox).
1. **Copy** the full body of `sql/payments.sql:1-223` into the new migration file, preserving the existing RLS-posture header comment (`sql/payments.sql:1-7`).
2. **Prepend** a short migration header comment: `-- Backfill of hand-applied bot schema for wallet_registry, payment_channel_routes, payment_requests, and claim_due_payment_requests.` plus a one-line note that production already has the final state and this migration is idempotent.
3. **Do not** edit SQL semantics. No refactors, no added columns, no renamed constraints — literal copy plus header.
4. **Reason for ordering this first:** `admin_payment_intents` has FKs into `wallet_registry` and `payment_requests`, so their CREATE must land in a prior migration (strict filename-timestamp ordering under Supabase CLI).

### Step 3: Create `supabase/migrations/20260411220100_backfill_admin_payment_intents.sql`
**Scope:** Small
**Target path:** `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/20260411220100_backfill_admin_payment_intents.sql`.
1. **Copy** the full body of `sql/admin_payment_intents.sql:1-99` verbatim.
2. **Remove** the duplicate `create extension if not exists pgcrypto;` and `create or replace function public.set_payment_updated_at()` block (`sql/admin_payment_intents.sql:1-11`) — these are already guaranteed present by Step 2's migration and being idempotent they are safe to keep, but dropping them keeps each migration scoped to its own concern. If ambiguous, err on the side of keeping them (they are no-ops).
3. **Prepend** header comment documenting the FK dependency on `payment_requests` / `wallet_registry` and the idempotency guarantee.

### Step 4: Create `supabase/migrations/20260411220200_backfill_social_publications.sql`
**Scope:** Small
**Target path:** `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/20260411220200_backfill_social_publications.sql`.
1. **Copy** the full body of `sql/social_publications.sql:1-126` verbatim.
2. **Note**: `sql/social_publications.sql:77` revokes execute on `public.claim_due_social_publications(integer, bigint[])` before that function is defined at line 93. That ordering works on live systems because `REVOKE` on a nonexistent function errors; since production already has the function, the statement is safe on live but will fail on a brand-new DB if the migration is replayed from zero. **Fix**: reorder the file so the function definition (`sql/social_publications.sql:93-126`) precedes the `REVOKE EXECUTE` statement, matching what a fresh `supabase db reset` would need. This is the only semantic correction; flag it in the migration header comment.
3. **Prepend** header comment noting the independence from the payments migrations.

### Step 5: Delete the shadow `sql/` folder (`sql/`)
**Scope:** Small
1. **Remove** `sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql` via `git rm`.
2. **Remove** the now-empty `sql/` directory (implicit after `git rm` of all contents under git; confirm with a directory listing).
3. Do **not** `rm -rf sql/` with untracked files still inside — verify via `git status` first that only the three tracked files exist.

### Step 6: Update documentation to point at new migration paths and describe the workflow (`README.md`, `structure.md`, `docs/payments.md`, `.cursorrules`)
**Scope:** Medium
1. **`README.md`** — Add a new "Database migrations" subsection under "Database Storage" (`README.md:65-72`). Content (~15 lines):
   - Canonical path is `<workspace>/supabase/migrations/` (note: sibling to the bot repo, not under `brain-of-bndc/`).
   - Naming: `YYYYMMDDHHMMSS_short_description.sql`.
   - Idempotency requirement: every statement must be safe to replay (use `IF NOT EXISTS`, `CREATE OR REPLACE`, guarded backfills).
   - Apply via Supabase CLI: `supabase db push` (production) / `supabase db reset` (local).
   - Explicitly warn: **do not** drop SQL into a `sql/` folder inside this repo — that pattern was retired when the shadow folder was backfilled.
2. **`structure.md:98-103`** — The current tree falsely shows a `supabase/` subdirectory under `brain-of-bndc/`. Either (a) remove that block entirely and replace with a one-line note pointing at the workspace-level `supabase/`, or (b) leave a stub pointing at the sibling path. Prefer (a) for accuracy.
3. **`docs/payments.md`** — Rewrite the ten `sql/payments.sql:NNN` references (lines `20`, `80`, `92`, `126`, `129`, `166`, `177`, `267`, `287`, `301`) to point at the new migration file, keeping the relative anchors. Because line numbers inside the new migration file shift by the header comment (likely +3–5 lines), recompute anchors after Step 2 and update each reference to the new filename + recomputed line range. If a precise re-anchor is impractical, degrade to filename + section name (e.g., `supabase/migrations/20260411220000_backfill_payments.sql` — `uq_payment_requests_active_producer_ref`).
4. **`.cursorrules:57`** — already says "Migrations in `supabase/migrations/` with timestamp prefixes"; add a one-line clarification that this path is the **workspace-level** supabase dir, not a bot-local folder.
5. **CLAUDE.md** — See Questions. If the user wants a new `brain-of-bndc/CLAUDE.md`, create it with a 10-line "Database migration workflow" section that mirrors the README addition. Otherwise skip.

### Step 7: Validate (no live DB)
**Scope:** Small
1. **Grep** the whole repo for lingering `sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql`, and bare `sql/` path references to confirm every reference now points at the new migration files (exclude `.megaplan/`, `.venv/`, `__pycache__/`).
2. **Syntax-check** each new migration by piping through `psql --set ON_ERROR_STOP=1 -f <file> --dry-run`-equivalent via `pg_format` if available, or at minimum open each in an editor and scan for unclosed `$$` blocks, missing semicolons, or stray identifiers. (The `supabase db lint --local` path is documented as unavailable in this sandbox by prior plans — do not block on it.)
3. **Run** `pytest -q tests/test_social_publications.py tests/test_caller_paths.py tests/test_notification_delete.py` (the three new test files listed in git status that are most likely to import code touching these tables) to confirm nothing Python-side regressed on the deletion of `sql/`. These tests do not exercise SQL directly but will catch any `sql/` string references embedded in code.
4. **Verify** `git status` after all edits shows: 3 deletions (`sql/*.sql`), 3 new files under `supabase/migrations/` (reported as untracked since they are outside the session sandbox — see Questions), and edits to `README.md`, `structure.md`, `docs/payments.md`, `.cursorrules`.

## Execution Order
1. Step 1 (audit) before anything else — cheap and deterministic.
2. Step 2 → Step 3 → Step 4 in strict order: payments migration must precede admin_payment_intents migration in filename timestamp because of FK dependencies; social_publications is independent and can land last.
3. Step 5 (delete shadow) only after Steps 2–4 have produced valid migration files on disk — never delete the source of truth before the replacement is committed.
4. Step 6 (docs) after Steps 2–4 so line-number refs in `docs/payments.md` can be recomputed against the final migration file contents.
5. Step 7 (validation) last.

## Validation Order
1. **Cheapest first:** grep for stale `sql/...` references (Step 7.1).
2. **Then:** static syntax scan of new migration files (Step 7.2).
3. **Then:** targeted pytest subset (Step 7.3).
4. **Finally:** `git status` / `git diff --stat` sanity check (Step 7.4).
