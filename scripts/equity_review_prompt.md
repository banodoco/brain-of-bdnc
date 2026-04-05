# Monthly Equity Review — Agentic Prompt

You are reviewing community contributions for the Banodoco Discord server for **{{MONTH}}** to identify people who should receive monthly equity allocation.

Your job is to **surface everyone worth considering** — the reviewer (POM) will make the final decisions. It is much better to include a borderline candidate with a "Weak" verdict than to silently drop someone who deserved consideration.

## Your tools

You have access to `discord_tools.py` in the `brain-of-bndc/scripts/` directory. Run commands like:

```bash
cd /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc && python scripts/discord_tools.py <command> [args]
```

Key commands:
- `top --month {{MONTH}} --min-reactions N --limit N --show-reactors` — highest-reacted messages
- `user "username" --month {{MONTH}} --limit 50 --show-reactors` — all messages from a user
- `search "keyword" --month {{MONTH}} --limit 30` — search by content
- `context MESSAGE_ID` — see a message with surrounding context and replies
- `thread MESSAGE_ID` — follow a full reply chain
- `channels --month {{MONTH}}` — active channels
- `summaries --month {{MONTH}}` — daily channel summaries

You also have the current equity holders list at `brain-of-bndc/equity_holders.txt`. Note: this file uses Discord display names which may differ from current usernames.

---

## Phase 1: Build the Shortlist (mechanical — follow every step)

The goal of Phase 1 is to produce a **master tally**: a list of every person surfaced, which passes found them, and how many passes they appeared in. Do NOT skip any step. Do NOT filter people out during this phase — just collect names.

**Exclude from the tally:** pom (the reviewer), BNDC / general-scheming (the bot), and any other bot accounts. Also watch for duplicate names — the same person may appear as "ramonguthrie" and "ramonguthrie (4080 16GB) 🇬🇧". Merge these into one entry.

### Step 1.1 — Channels overview
Run: `channels --month {{MONTH}}`
Record the top 5-10 most active channels. You'll use these later.

### Step 1.2 — Top-reacted messages
Run: `top --month {{MONTH}} --min-reactions 3 --limit 100 --show-reactors`
Write down every unique author and their reaction counts. Tag each person with **"pass: top-reactions"**.

### Step 1.3 — Daily summaries
Run: `summaries --month {{MONTH}}`
Read every summary. Write down every person mentioned by name for a notable contribution. Tag each with **"pass: summaries"**.

### Step 1.4 — Open source / tool sharing
Run each of these searches and record every unique author:
- `search "github.com" --month {{MONTH}} --limit 50`
- `search "huggingface" --month {{MONTH}} --limit 50`
- `search "workflow" --month {{MONTH}} --limit 50`
- `search "node" --month {{MONTH}} --limit 50`
- `search "lora" --month {{MONTH}} --limit 50`
Tag each person with **"pass: open-source"**.

### Step 1.5 — Helpers and troubleshooters
Run:
- `search "fixed" --month {{MONTH}} --limit 30`
- `search "try this" --month {{MONTH}} --limit 30`
- `search "here's how" --month {{MONTH}} --limit 30`
- `search "you need to" --month {{MONTH}} --limit 30`
Record every unique author who appears to be helping others. Tag each with **"pass: helpers"**.

### Step 1.7 — Build the master tally

Now merge all results into a single table. For each person, list:
- Name
- Which passes they appeared in
- Total number of passes (out of 4: top-reactions, summaries, open-source, helpers)
- Highest reaction count seen

**IMPORTANT: Write this tally to a file** at `brain-of-bndc/results/{{MONTH}}_tally.md` before proceeding. This preserves your work in case earlier tool output gets compressed from context. You will read this file back at the start of Phase 2.

Example format:

```
# Master Tally — {{MONTH}}

| Name | Passes | Pass count | Best reactions | Equity holder? |
|------|--------|------------|---------------|----------------|
| Kijai | top-reactions, summaries, open-source, helpers | 4 | 49 | No |
| VRGameDevGirl84 | top-reactions, summaries, open-source | 3 | 37 | No |
| ingi // SYSTMS | top-reactions, artists | 2 | 20 | No |
| brbbbq | top-reactions, summaries | 2 | 19 | No |
| ...  | ... | ... | ... | ... |
```

### Step 1.8 — Read equity holders list
Run: `cat brain-of-bndc/equity_holders.txt`
Cross-reference the master tally against existing holders. Mark any matches.

### Step 1.9 — Select candidates for deep dive

Read back `brain-of-bndc/results/{{MONTH}}_tally.md` to refresh your memory of the full tally.

**Every person with 2+ passes MUST get a `user` lookup in Phase 2.** No exceptions, no silent drops. If after the lookup their contribution turns out to be trivial, write a short evaluation explaining why — but you must look first.

Additionally:
- **1 pass but 10+ reactions** → also gets a deep dive
- **1 pass but prominently featured in summaries** → also gets a deep dive
- **1 pass, low signal** → notable mention only (skip Phase 2, include in final report)
- **Existing equity holders** who appeared in any pass → flag for the report (brief note on continued activity)

Count how many people qualify. If it's more than 30, that's fine — do them all. Thoroughness matters more than brevity.

---

## Phase 2: Deep Dive (for each candidate from Phase 1)

Start by reading `brain-of-bndc/results/{{MONTH}}_tally.md` to reload the full master tally.

For each person selected for deep dive, follow this checklist:

- [ ] Run `user "username" --month {{MONTH}} --limit 50 --show-reactors`
- [ ] Read their messages — what did they actually contribute?
- [ ] For their top 2-3 messages, run `context MESSAGE_ID` or `thread MESSAGE_ID`
- [ ] Note: who replied? What did they say? Who reacted?
- [ ] Check: is this original work or sharing someone else's?
- [ ] Check: one-off or sustained pattern across the month?
- [ ] Check: are they in `equity_holders.txt`?

After completing the checklist, classify and draft the evaluation. For strong candidates, write a full evaluation (all fields). For people who turn out to be trivial after the lookup, a 3-4 line "Investigated — not enough" entry is fine.

---

## Phase 3: Categorize & Evaluate

Classify each candidate into one of these categories:

**Infrastructure Builders** — People who build core tools, nodes, models, or integrations that many others use. These are the highest-impact contributions: new ComfyUI nodes, model releases, significant PRs to major repos, etc.

**Knowledge/Tooling Creators** — People who create workflows, guides, tutorials, troubleshooting help, or smaller tools. They make the ecosystem more accessible and productive for others.

**Artists** — People whose creative work inspires the community, demonstrates new techniques, or pushes the boundaries of what's possible. (Note: artists are primarily identified separately via POM's Art Sharing Wednesday curation in the #updates channel. Any artists who surface through these scans should still be included.)

**Core** — Team/leadership contributions (you likely won't identify these — they'll be added manually).

---

## Phase 4: Write the Report

For each candidate, write an evaluation with this structure:

```
## username — [Category]

**Status:** [New candidate / Existing equity holder]

**Appeared in:** [which scan passes surfaced this person — e.g., "top-reactions (19), summaries, open-source"]

**Pros:**
- Specific contribution 1, with evidence (reaction count, replies, etc.)
- Specific contribution 2
- Pattern of behavior (e.g., "consistently helps users troubleshoot")

**Cons:**
- Any counterarguments (one-off contribution, sharing others' work, already compensated, etc.)
- Every candidate must have at least one con or honest caveat — even the strongest contributors have tradeoffs worth noting

**Key evidence:**
- [Discord link] — description (N reactions, N replies)
- [Discord link] — description

**Community response:**
- Who engaged with their work (especially other known contributors)
- Quality of engagement (grateful replies, people building on their work, etc.)

**Verdict:** [Strong candidate / Moderate candidate / Weak candidate] — one sentence summary
```

## Coverage requirements

- **Every 2+ pass person must be accounted for.** Either a full evaluation or an explicit "investigated, here's why they didn't make the cut" entry. No one from the 2+ pass list should be silently absent from the report.
- **Include a "Notable Mentions" section** at the end for 1-pass people and anyone whose deep dive revealed trivial contributions. A sentence each explaining what they did.
- **Both Infrastructure Builder and Knowledge/Tooling categories must be represented.** Artists are primarily sourced separately. If any artists surface through your scans, include them.
- **Flag existing equity holders** who are still actively contributing — include them in the notable mentions section with a note on what they did this month.

## Important guidelines

- **Follow Phase 1 mechanically.** Run every query. Record every name. Build the tally. Do not skip steps or take shortcuts.
- **Evidence over vibes.** Every pro and con should reference specific messages, reaction counts, or reply threads.
- **Distinguish original work from curation.** Sharing a link to someone else's model release is not the same as building a model. Both have value, but they're different categories and different levels.
- **Consider the reactors.** A message with 10 reactions from known contributors/equity holders signals different impact than 10 reactions from general members. Both matter, but note the difference.
- **Note patterns over individual posts.** Someone who helps 20 people troubleshoot across the month is potentially more valuable than someone with one viral post.
- **The quiet helpers matter.** People who consistently answer questions, debug issues, and explain concepts are extremely valuable even if they never get a single high-reaction post. Look for them in thread replies and channel activity.
- **Be honest about cons.** The goal is to give the reviewer the full picture so they can make an informed decision. Don't advocate — present evidence. Every candidate has tradeoffs; find them.

## Output

Write your full report to `brain-of-bndc/results/{{MONTH}}_equity_review.md`.

At the top of the file, include the master tally, then the summary table, then detailed evaluations:

```markdown
# Equity Review — {{MONTH}}

## Master Tally

[The full merged list from Phase 1 Step 1.7 — every person, which passes found them, pass count]

## Summary

| Candidate | Category | Status | Verdict |
|-----------|----------|--------|---------|
| username1 | Infrastructure Builder | New | Strong |
| username2 | Knowledge/Tooling | Existing holder | Moderate |
| ... | ... | ... | ... |

## Detailed Evaluations

[... full evaluations below ...]

## Notable Mentions

[... brief mentions of borderline candidates and active equity holders ...]
```
