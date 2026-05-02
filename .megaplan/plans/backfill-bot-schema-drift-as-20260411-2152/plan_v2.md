# Implementation Plan: Backfill bot schema drift as Supabase CLI migrations

## Overview
The bot has accumulated schema definitions as "shadow" scripts under `brain-of-bndc/sql/` (three files: `payments.sql`, `admin_payment_intents.sql`, `social_publications.sql`) that were hand-applied to production but were never promoted into Supabase CLI migrations. Goal: create timestamped, idempotent migration files in the workspace-level `supabase/migrations/` directory, delete the shadow folder in the bot repo, and document the migration workflow in the bot's `README.md`. Production already matches the desired state, so the new migrations must be pure no-ops on a live DB (idempotent guards on every statement).

**Critical cross-repo context (FLAG-001).** This task spans **two separate git repositories**:
- **Bot repo:** `/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/` — where `sql/` lives and where the deletions + docs edits land.
- **Workspace repo:** `/Users/peteromalley/Documents/banodoco-workspace/` — a *different* git root (confirmed via `git rev-parse --show-toplevel` from each location) whose `supabase/migrations/` is the target for the new SQL files.

The current planning/execution sandbox is rooted at `brain-of-bndc/` only; writes into the workspace repo's `supabase/migrations/` require either (a) expanding the executor's writable roots to include `/Users/peteromalley/Documents/banodoco-workspace/supabase/`, or (b) staging the files inside `brain-of-bndc/` and having the user move+commit them in the workspace repo as a separate manual step. This plan defines **both** paths explicitly and treats the sibling writes as a first-class handoff, not an afterthought.

Key facts gathered:
- `brain-of-bndc/sql/` contains exactly 3 files (`sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql`). Their contents are already mostly idempotent (`create ... if not exists`, `add column if not exists`, `create or replace function`, guarded backfill `UPDATE`). `REVOKE`, `ENABLE ROW LEVEL SECURITY`, and `DROP CONSTRAINT IF EXISTS`/`ADD CONSTRAINT` are inherently idempotent or already guarded.
- `brain-of-bndc/supabase/` does **not** exist on disk; `git ls-files` shows no `supabase/**` tracked files in the bot repo. The target `/Users/peteromalley/Documents/banodoco-workspace/supabase/` exists as a directory (verified via `test -d`) but is in a different git repo.
- No Python runtime code loads from `sql/...` (grep for `"sql/` and `'sql/` returned zero runtime hits). Only documentation references the path: `docs/payments.md` (10 line-number references), `structure.md:98-101` (describes a bot-local `supabase/` tree that does not exist), `.cursorrules:57` (naming convention note).
- FK dependency: `admin_payment_intents` references `wallet_registry(wallet_id)` and `payment_requests(payment_id)`, so its migration must sort after `payments` by filename timestamp. `social_publications` is independent.

Constraints:
- File changes only, no live DB access.
- Must stay proportional: cut/paste-with-guards + docs update, not a refactor.
- Cross-repo commits: the bot-repo deletions and the workspace-repo migration additions are **two separate commits in two separate repos** and cannot be bundled.
- Deletions in the bot repo must not land until the workspace-repo commits are confirmed, to avoid destroying the source of truth.

## Phase 1: Prepare migration content (bot-repo read-only audit + staging)

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
2. **Note** any not-idempotent lines discovered (expected: none based on re-reading). If any `CREATE INDEX` without `IF NOT EXISTS` or `CREATE TRIGGER` without prior `DROP IF EXISTS` is found, wrap it before copying into the migration.
3. **Preserve** `sql/payments.sql:1-7` (the "RLS posture" comment) verbatim in the payments migration so the RLS rationale travels with the schema into the workspace repo.

### Step 2: Stage the three migration files inside the bot repo at `.migrations_staging/` (`brain-of-bndc/.migrations_staging/`)
**Scope:** Medium
**Why stage, not write directly to sibling:** the planning/execution sandbox is rooted at `brain-of-bndc/`. Staging inside the bot repo guarantees the executor can always produce the artifacts and that `git status` inside `brain-of-bndc/` can confirm they exist (validation gap from FLAG-001). The staging directory is added to `.gitignore` so it is never accidentally committed to the bot repo.

1. **Create** `brain-of-bndc/.migrations_staging/` (new directory, excluded from bot-repo commits by gitignore — see Step 2.5).
2. **Create** `brain-of-bndc/.migrations_staging/20260411220000_backfill_payments.sql`:
   - Copy the full body of `sql/payments.sql:1-223` verbatim, preserving the RLS-posture header comment.
   - Prepend a migration header comment explaining purpose ("Backfill of hand-applied bot schema for wallet_registry, payment_channel_routes, payment_requests, and claim_due_payment_requests"), idempotency guarantee ("safe to replay against production — every statement is guarded"), and the FK relationship note ("admin_payment_intents migration depends on this one").
   - Do not edit SQL semantics. Literal copy plus header.
3. **Create** `brain-of-bndc/.migrations_staging/20260411220100_backfill_admin_payment_intents.sql`:
   - Copy the full body of `sql/admin_payment_intents.sql:1-99` verbatim.
   - Prepend a header comment documenting the FK dependency on `payment_requests` / `wallet_registry` (the prior migration in the ordering) and the idempotency guarantee.
   - Keep the duplicate `create extension if not exists pgcrypto;` and `create or replace function public.set_payment_updated_at()` block — they are idempotent no-ops and removing them would make the migration non-self-contained on a per-file basis.
4. **Create** `brain-of-bndc/.migrations_staging/20260411220200_backfill_social_publications.sql`:
   - Copy the full body of `sql/social_publications.sql:1-126`.
   - **Reorder** the sole semantic bug: move the `revoke execute on function public.claim_due_social_publications(integer, bigint[]) from anon, authenticated;` statement (`sql/social_publications.sql:77`) to appear **after** the `create or replace function public.claim_due_social_publications(...)` definition (`sql/social_publications.sql:93-126`). On production this statement succeeds because the function already exists, but a fresh `supabase db reset` would fail with "function does not exist" at line 77 otherwise.
   - Prepend a header comment noting independence from the payments migrations and calling out this one reorder.
5. **Create** `brain-of-bndc/.migrations_staging/README.md` explaining the handoff:
   - "These files must be moved to `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` and committed in the workspace repo (a different git root from `brain-of-bndc`)."
   - Exact move command: `mv brain-of-bndc/.migrations_staging/*.sql /Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`.
   - Reminder to commit in the workspace repo with `cd /Users/peteromalley/Documents/banodoco-workspace && git add supabase/migrations/2026041122* && git commit`.
   - Warning: **do not** run Step 5 (delete shadow) in the bot repo until the workspace-repo commits are confirmed.
6. **Add** `/.migrations_staging/` to `brain-of-bndc/.gitignore` so the staged files never leak into bot-repo commits.

### Step 2b (alternative): Write directly to the sibling path if sandbox permits (`/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`)
**Scope:** Small
**Condition:** Executor has writable-root access to `/Users/peteromalley/Documents/banodoco-workspace/supabase/` (either granted up-front or granted on-request during execution).
1. **Audit** the target directory first by listing `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` for any existing files that define `wallet_registry`, `payment_channel_routes`, `payment_requests`, `admin_payment_intents`, `social_publications`, `social_channel_routes`, `claim_due_payment_requests`, or `claim_due_social_publications`. If any conflict exists, halt and raise — do not add duplicate-schema migrations.
2. **Write** the three files directly to `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` with the same content as Step 2.2–2.4.
3. **Skip** the staging directory creation (Step 2.1), README (Step 2.5), and gitignore addition (Step 2.6). Still do Step 5 only after the workspace-repo commits land.
4. **Run** `git -C /Users/peteromalley/Documents/banodoco-workspace status supabase/migrations/` to confirm the new files are visible to the workspace repo's git.

## Phase 2: Bot-repo edits (deletions + docs)

### Step 3: Update documentation in the bot repo (`README.md`, `structure.md`, `docs/payments.md`, `.cursorrules`)
**Scope:** Medium
1. **`README.md`** — Add a new "Database migrations" subsection under "Database Storage" (after `README.md:72`). Content (~15 lines):
   - Canonical path: `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` (a **separate git repo** — not under `brain-of-bndc/`). Explain the two-repo topology in one sentence.
   - Naming: `YYYYMMDDHHMMSS_short_description.sql`.
   - Idempotency requirement: every statement must be safe to replay (`IF NOT EXISTS`, `CREATE OR REPLACE`, guarded backfills). Any new schema must also be replay-safe against production.
   - Apply command: `supabase db push` (production) / `supabase db reset` (local).
   - Warning: **do not** drop SQL into a `sql/` folder inside this repo — that shadow-folder pattern was retired when this plan backfilled the historical drift.
2. **`structure.md:98-103`** — Remove the bot-local `supabase/` tree (it does not exist and never did as a committed artifact). Replace with a one-line note: `# Supabase migrations live in the workspace-level repo at ../supabase/migrations/ (separate git root).`
3. **`docs/payments.md`** — Rewrite the ten `sql/payments.sql:NNN` references (lines `20`, `80`, `92`, `126`, `129`, `166`, `177`, `267`, `287`, `301`). New anchor strategy:
   - Filename: `../supabase/migrations/20260411220000_backfill_payments.sql` (relative path from `brain-of-bndc/docs/` is fine; adjust if another convention is used).
   - Because line numbers inside the new migration file shift by the prepended header comment (likely +5–10 lines), prefer **section-name anchors** (e.g., `uq_payment_requests_active_producer_ref`, `claim_due_payment_requests`, `tx_signature_history backfill`) over exact line numbers to make the references resilient to future header edits.
4. **`.cursorrules:57`** — Update the line "Migrations in `supabase/migrations/` with timestamp prefixes" to: "Migrations in `../supabase/migrations/` (workspace-level repo, separate git root from `brain-of-bndc/`), with `YYYYMMDDHHMMSS_` timestamp prefixes."
5. **CLAUDE.md** — Out of scope unless explicitly requested (see Questions). `README.md` carries the workflow docs.

### Step 4: Remove the shadow `sql/` folder (`brain-of-bndc/sql/`)
**Scope:** Small
**Precondition:** Either Step 2b completed successfully and the workspace-repo commits are confirmed, **or** the user has confirmed they have moved the files from `.migrations_staging/` and committed them in the workspace repo. If neither is true, **halt** and ask — do not delete the source of truth.
1. **Run** `git -C brain-of-bndc status sql/` to confirm only the three tracked files exist (no stray untracked contents to lose).
2. **Run** `git rm brain-of-bndc/sql/payments.sql brain-of-bndc/sql/admin_payment_intents.sql brain-of-bndc/sql/social_publications.sql`.
3. **Confirm** the `sql/` directory is removed (it should be empty and Git will not track the empty dir — a `test ! -d brain-of-bndc/sql` check is sufficient, or `ls` should report no such directory).

## Phase 3: Validation (no live DB)

### Step 5: Validate bot-repo edits (`brain-of-bndc/`)
**Scope:** Small
1. **Grep** `brain-of-bndc/` (excluding `.megaplan/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.migrations_staging/`) for `sql/payments.sql`, `sql/admin_payment_intents.sql`, `sql/social_publications.sql`, and bare `sql/` path references. Zero matches outside the new docs is the pass condition.
2. **Run** `git -C brain-of-bndc status` and confirm:
   - 3 tracked deletions under `sql/`.
   - Edits to `README.md`, `structure.md`, `docs/payments.md`, `.cursorrules`, `.gitignore` (only if Step 2 staging path was used).
   - (Staging path only) untracked `.migrations_staging/` that is gitignored, so `git status --ignored` shows it but `git status` does not.
3. **Run** `pytest -q tests/test_social_publications.py tests/test_caller_paths.py tests/test_notification_delete.py` to confirm no Python regressions from the deletions or doc edits. These tests do not exercise SQL, but they will catch any `sql/...` string references embedded in code.

### Step 6: Validate workspace-repo migrations (`/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`)
**Scope:** Small
**Two variants depending on Step 2 vs Step 2b path:**
- **Direct-write path (Step 2b used):**
  1. Run `test -f /Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/20260411220000_backfill_payments.sql` (and the other two filenames) to confirm presence.
  2. Run `git -C /Users/peteromalley/Documents/banodoco-workspace status supabase/migrations/` to confirm the workspace repo sees three new untracked files under `supabase/migrations/`.
  3. Static syntax scan of each file: open and look for unclosed `$$` blocks, missing semicolons, mismatched parens, stray identifiers. If `pg_format` or `sqlfluff` is available, run it as a read-only lint; do not block on their absence.
  4. Separately commit the new files in the workspace repo (`cd /Users/peteromalley/Documents/banodoco-workspace && git add supabase/migrations/2026041122* && git commit -m "backfill bot schema drift as supabase migrations"`). The commit message should mention that production is already in this state.
- **Staging path (Step 2 used):**
  1. Run `test -f brain-of-bndc/.migrations_staging/20260411220000_backfill_payments.sql` (and the other two, plus the `README.md`) to confirm the three files and the handoff README exist inside the bot repo.
  2. Report to the user exactly which files to move and which commands to run in the workspace repo. Do **not** proceed to Step 4 (deletion) until the user confirms the workspace-repo commits exist. Flag this as a manual checkpoint.

## Execution Order
1. **Step 1** (audit) first — cheap, read-only, deterministic.
2. **Decide** between Step 2 (staging) and Step 2b (direct-write) based on the executor's writable roots. Prefer Step 2b; fall back to Step 2.
3. **Step 3** (docs edits) after the migration files exist on disk so `docs/payments.md` section anchors can be verified against the real file content.
4. **Step 4** (deletion) **only** after Step 6 confirms the workspace-repo migrations are committed (direct-write path) or the user confirms they have moved+committed them (staging path). Never delete before the replacement is durable.
5. **Step 5** (bot-repo validation) and **Step 6** (workspace-repo validation) last.

## Validation Order
1. **Cheapest first:** grep for stale `sql/...` references inside `brain-of-bndc/` (Step 5.1).
2. **Then:** `git status` inside `brain-of-bndc/` and (direct-write path) inside the workspace repo (Step 5.2, Step 6.1–6.2).
3. **Then:** static syntax scan of new migration files (Step 6.3).
4. **Then:** targeted pytest subset against the bot repo (Step 5.3).
5. **Finally:** manual-checkpoint confirmation that the workspace-repo commits exist before Step 4's deletions land.
