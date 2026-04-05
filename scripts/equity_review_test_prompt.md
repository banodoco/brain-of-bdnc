# Equity Review — Small Test Run (Broader Scan)

You are reviewing community contributions for the Banodoco Discord server for **the first week of March 2026 (March 1-7)** to identify people who should receive monthly equity allocation.

## Your tools

Run commands from the `brain-of-bndc` directory:

```bash
cd /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc && python scripts/discord_tools.py <command> [args]
```

Key commands:
- `top --month 2026-03 --min-reactions N --limit N --show-reactors` — highest-reacted messages
- `user "username" --month 2026-03 --limit 20 --show-reactors` — all messages from a user
- `search "keyword" --month 2026-03 --limit 15` — search by content
- `context MESSAGE_ID` — see a message with surrounding context and replies
- `thread MESSAGE_ID` — follow a full reply chain
- `channels --month 2026-03` — active channels
- `summaries --month 2026-03` — daily channel summaries

You also have the current equity holders list at `brain-of-bndc/equity_holders.txt`. Note: this file uses Discord display names which may differ from current usernames.

## Scope — KEEP THIS SMALL

This is a test run. Focus on March 1-7 only (ignore messages outside that window). Limit yourself to:
- Phase 1: Run all scan passes but with smaller limits (10-15 per query)
- Pick the **5 most promising candidates** from the combined results
- Deep dive on those 5 only
- Write a short report

## Process

### Phase 1: Multi-Pass Broad Scan

Build a candidate list from multiple angles:

**Pass 1 — High-impact posts:**
`top --month 2026-03 --min-reactions 3 --limit 15 --show-reactors`
Note who authored these (focus on March 1-7 timestamps).

**Pass 2 — Daily summaries:**
`summaries --month 2026-03`
Read summaries for March 1-7 only. Note every person called out for a notable contribution.

**Pass 3 — Open source work:**
Run a few targeted searches:
- `search "github.com" --month 2026-03 --limit 15`
- `search "huggingface" --month 2026-03 --limit 15`
- `search "workflow" --month 2026-03 --limit 15`

Note who is sharing links to their own repos, models, or workflows (March 1-7 only).

**Pass 4 — Helpers:**
- `search "help" --month 2026-03 --limit 15`
Look for people answering questions and troubleshooting (March 1-7 only).

**Build the candidate list:**
Compile everyone surfaced across all passes. Prioritize people who appear in **multiple passes**. Pick the top 5.

### Phase 2: Deep Dive (5 people only)

For each candidate:
1. `user "username" --month 2026-03 --limit 20 --show-reactors` to see their activity
2. For their top 1-2 messages, run `context MESSAGE_ID` to see who replied and reacted
3. Check if they're in `equity_holders.txt`
4. Assess: original work or sharing others'? One-off or pattern?

### Phase 3: Report

For each of the 5 candidates:

```
## username — [Category]

**Category:** Infrastructure Builder / Knowledge & Tooling / Artist
**Status:** New candidate / Existing equity holder

**Appeared in:** [which scan passes surfaced them — e.g., "top reactions, summaries, github search"]

**Pros:**
- Evidence-based bullet points

**Cons:**
- Any counterarguments, or "None identified"

**Key evidence:**
- [Discord link] — description (N reactions)

**Community response:**
- Who engaged, quality of engagement

**Verdict:** Strong / Moderate / Weak — one sentence
```

Categories:
- **Infrastructure Builders** — core tools, nodes, models, integrations
- **Knowledge/Tooling Creators** — workflows, guides, troubleshooting, smaller tools
- **Artists** — creative work that inspires the community

## Output

Write the report to `brain-of-bndc/results/test_equity_review_v2.md`
