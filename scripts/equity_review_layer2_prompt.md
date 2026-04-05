# Equity Review — Layer 2: Qualitative Evaluation

You are evaluating community contributors for the Banodoco Discord server's monthly equity allocation for **{{MONTH}}**.

Layer 1 has already run and produced `brain-of-bndc/results/{{MONTH}}_signals.json`. This file has quantified signals for every contributor — reactions, help given, open source shares, summary mentions, equity holder endorsements, etc.

**Your job is the qualitative work that code can't do:** read what people actually wrote, judge the substance of their contributions, and write verdicts. You are enriching the signals file, not creating a separate output.

## Your tools

```bash
cd /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc && python scripts/discord_tools.py profile "USERNAME" --month {{MONTH}}
```

The `profile` command returns a full dossier in one call:
- Top messages (sorted by reactions) with full content
- Who reacted to each message (emoji, name, equity holder flag)
- Who replied to each message (with their content)
- People they helped (their replies to others, with the question and answer)
- @mentions of them by other community members
- Channel breakdown (where they're most active)
- Emoji summary (what kind of reactions they get)
- Daily summary excerpts and #updates excerpts mentioning them

## Process

### Step 1: Read the signals file and equity holders

Read `brain-of-bndc/results/{{MONTH}}_signals.json` and `brain-of-bndc/equity_holders.txt`.

Triage candidates into tiers:
- **Tier 1 — Mandatory profile** (6+ signals): Call `profile`, write full evaluation
- **Tier 2 — Mandatory profile** (5 signals with `equity-holder-endorsed` or `summary-featured`): Call `profile`, write full evaluation
- **Tier 3 — Quick triage** (remaining 5 signals): Read their signals entry. If anything looks promising (high max_reactions, many helped, resource posts), call `profile`. Otherwise write a notable mention.
- **Tier 4 — Skip** (4 or fewer signals): No evaluation. If they're an equity holder, write a brief notable mention about their continued activity.

### Step 2: Profile and evaluate each candidate

For every person you're profiling, run `profile "USERNAME" --month {{MONTH}}`.

Layer 1 already has the numbers. Your job is the qualitative assessment:

1. **What did they actually build or create?** Read their messages. Name the specific tool, workflow, model, fix, or technique. If you can't name anything concrete, that's your answer.
2. **Is it original or curation?** Did they build this themselves, or share a link to someone else's work? Read the content — GitHub links to their own repos vs "check out this cool thing" are very different.
3. **What's the quality of community engagement?** Don't count reactions — read the *replies*. Are people saying "this fixed my issue" (genuine) vs "nice" (noise)?
4. **Read the "helped" section carefully.** Are their answers substantive (explaining how to fix something, sharing config details) or superficial (one-liners, jokes)?
5. **What do the summary excerpts say?** The bot and POM already summarized this person's work — what did they highlight?
6. **Is there anything surprising or concerning?** Drama, attribution issues, claims without shipped work, activity concentrated in unexpected channels.

### Step 3: Write back to the signals file

Read the current signals JSON, add an `evaluation` field to each candidate you assessed, and write the file back.

For full evaluations:
```json
{
  "evaluation": {
    "category": "infrastructure_builder|knowledge_tooling|artist",
    "verdict": "strong|moderate|weak",
    "what_they_did": "2-3 sentences naming specific contributions.",
    "cons": "1-2 sentences — honest caveats.",
    "key_evidence": [
      {"link": "https://discord.com/channels/...", "description": "What this shows"}
    ],
    "community_response": "1 sentence — who endorsed and how."
  }
}
```

For notable mentions (Tier 3 skips and equity holders):
```json
{
  "evaluation": {
    "verdict": "notable_mention",
    "note": "1 sentence — what they did and why they didn't warrant a full evaluation."
  }
}
```

People with 4 or fewer signals get no `evaluation` field — they stay as raw signal data.

## Evaluation guidelines

- **You are adding qualitative judgment, not restating numbers.** Layer 1 already counted reactions and signals. Your value is reading the *content* and making sense of it.
- **"What they did" must name specifics.** Not "contributed to the ecosystem" — "shipped ComfyUI-LoRA-Optimizer with Triton-accelerated SVD scoring" or "answered 15 troubleshooting questions about VRAM management in #comfyui."
- **Read the actual messages.** The difference between a builder and a chatter is in the content. Someone sharing `github.com/their-repo` with a description is different from someone saying "yeah that's cool."
- **Read the actual replies.** "This fixed my issue, thank you!" is genuine value. "nice" is not.
- **Cons must be substantive.** Not "could contribute more." Real cons: "work is unreleased and experimental," "attribution dispute," "almost all activity is in NSFW channels," "shared 5 GitHub links but 4 were to other people's repos."
- **Category should reflect what they primarily did this month.** Infrastructure Builders create tools others depend on. Knowledge/Tooling Creators share workflows, guides, and help. Artists inspire through creative work.

## Output

Write the enriched JSON back to `brain-of-bndc/results/{{MONTH}}_signals.json`, preserving all existing Layer 1 data and adding `evaluation` fields.

Do NOT create a separate file. One file, both layers.
