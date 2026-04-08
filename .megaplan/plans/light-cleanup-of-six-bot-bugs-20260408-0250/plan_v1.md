# Implementation Plan: Light Cleanup of Six Bot Bugs

## Overview
The idea references "six bot bugs — see notes," but no notes artifact is attached to the plan directory (`.megaplan/plans/light-cleanup-of-six-bot-bugs-20260408-0250/` contains only `state.json`), and no clarification file exists. The repository is a Python Discord bot (`brain-of-bndc`) with feature modules under `src/features/` (admin, answering, archive, competition, content, curating, gating, grants, health, logging, reacting, relaying, sharing, summarising). Without the bug list, a concrete per-bug plan cannot be written. This plan is a scaffold: once the six bugs are pasted in, each becomes one step below. Until then, the plan captures the intended shape, the cheap validation path, and the assumptions being made so work can start the moment the notes arrive.

Key constraints:
- Robustness is `light` (per `state.json`), so each fix should be the minimal direct change — no refactors, no defensive wrappers.
- Working tree already has unrelated modifications in `.megaplan/schemas/*.json` and `src/features/summarising/summariser.py`; fixes must stay isolated from those.
- Fixes should be independent commits/steps so any one can be reverted without affecting the others.

## Main Phase

### Step 1: Retrieve the bug notes and lock scope
**Scope:** Small
1. **Obtain** the six-bug list from the user (paste into this plan, or drop a `notes.md` into `.megaplan/plans/light-cleanup-of-six-bot-bugs-20260408-0250/`).
2. **Classify** each bug: file path, one-line symptom, one-line proposed fix, risk (trivial / needs-investigation).
3. **Stop** and re-plan if any bug turns out to be non-trivial — "light cleanup" implies each should be a few lines.

### Step 2: Reproduce or statically confirm each bug (`src/features/...`)
**Scope:** Small
1. **Read** the exact lines named in the notes for each of the six bugs before touching anything.
2. **Confirm** the bug is real by tracing the code path; if a bug can't be confirmed from the code alone, flag it and ask the user rather than guessing.

### Step 3: Apply Bug Fix 1 (file TBD from notes)
**Scope:** Small
1. **Implement** the minimal fix at the exact line identified in Step 2.
2. **Leave** surrounding code untouched (no drive-by cleanups).

### Step 4: Apply Bug Fix 2 (file TBD from notes)
**Scope:** Small
1. Same pattern as Step 3.

### Step 5: Apply Bug Fix 3 (file TBD from notes)
**Scope:** Small
1. Same pattern as Step 3.

### Step 6: Apply Bug Fix 4 (file TBD from notes)
**Scope:** Small
1. Same pattern as Step 3.

### Step 7: Apply Bug Fix 5 (file TBD from notes)
**Scope:** Small
1. Same pattern as Step 3.

### Step 8: Apply Bug Fix 6 (file TBD from notes)
**Scope:** Small
1. Same pattern as Step 3.

### Step 9: Validate (`python -m py_compile`, targeted imports, any existing tests)
**Scope:** Small
1. **Syntax check** each touched file: `python -m py_compile <file>`.
2. **Import check** each touched module from the repo root to catch import-time errors.
3. **Run** any existing tests covering the touched modules (look under `scripts/test_*.py` and project test dirs — current repo shows `scripts/test_social_picks.py`; check for more once bugs are known).
4. **Manual smoke** in dev Discord bot only if a bug's nature requires it (flag which ones to the user).

## Execution Order
1. Step 1 (get notes) must complete first — everything else is blocked on it.
2. Step 2 (confirm) before any edits.
3. Steps 3–8 can be done in any order, but do them sequentially in separate commits so each is independently revertable.
4. Step 9 last.

## Validation Order
1. `py_compile` per file immediately after each fix (cheapest).
2. Module import sanity once all fixes are in.
3. Targeted existing tests (if any).
4. Full test suite / manual smoke only if prior steps reveal concerns.
