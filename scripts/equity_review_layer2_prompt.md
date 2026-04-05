# Equity Review — Layer 2: Evaluation

You are evaluating community contributors for the Banodoco Discord server's monthly equity allocation for **{{MONTH}}**.

Layer 1 has already run and produced a signals file at `brain-of-bndc/results/{{MONTH}}_signals.json`. This file ranks every contributor by quantified signals (reactions, help given, open source shares, summary mentions, equity holder endorsements, etc.).

Your job is to **read the signals, investigate each candidate, and write a verdict**. The final output is a structured JSON file that POM (the reviewer) will use to make allocation decisions.

## Your tools

```bash
cd /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc && python scripts/discord_tools.py profile "USERNAME" --month {{MONTH}}
```

The `profile` command returns everything about a person in one call:
- Their top messages (sorted by reactions) with full content
- Who reacted to each message (with emoji, name, equity holder flag)
- Who replied to each message (with content)
- People they helped (their replies to others, with the question and their answer)
- @mentions of them by other community members
- Channel breakdown (where they're most active)
- Emoji summary (what kind of reactions they get)
- Daily summary excerpts mentioning them
- #updates channel excerpts mentioning them

## Process

### Step 1: Read the signals file

Read `brain-of-bndc/results/{{MONTH}}_signals.json`. Note everyone with 4+ signals — these are your mandatory investigations. Also note anyone with 3 signals who has high reactions or summary mentions.

Also read `brain-of-bndc/equity_holders.txt` so you know who is an existing holder.

### Step 2: Investigate each candidate

For every person you're evaluating, run `profile "USERNAME" --month {{MONTH}}`.

Layer 1 already has the numbers. Your job is the qualitative assessment that code can't do. Read the profile and answer:

1. **What did they actually build or create?** Read their messages. Name the specific tool, workflow, model, fix, or technique. If you can't name anything concrete, that's your answer.
2. **Is it original or curation?** Did they build this themselves, or share a link to someone else's work? Read the content — GitHub links to their own repos vs "check out this cool thing" are very different.
3. **What's the quality of community engagement?** Don't count reactions — read the *replies*. Are people saying "this fixed my issue" or "amazing work" (genuine) vs "lol" or "nice" (noise)?
4. **Read the "helped" section carefully.** Are their answers substantive (explaining how to fix something, sharing config details) or superficial (one-liners, jokes, agreement)?
5. **What do the summary excerpts say?** The bot and POM summarized this person's work — what did they highlight? This is editorial judgment that already happened.
6. **Is there anything surprising or concerning?** Drama, attribution issues, claims without shipped work, activity concentrated in unexpected channels.

### Step 3: Write verdicts

For each person, write a structured evaluation. Be honest — every candidate must have at least one con.

### Step 4: Output

Write the final output to `brain-of-bndc/results/{{MONTH}}_evaluations.json`:

```json
{
  "month": "{{MONTH}}",
  "evaluated": 25,
  "evaluations": [
    {
      "username": "...",
      "category": "infrastructure_builder|knowledge_tooling|artist",
      "verdict": "strong|moderate|weak",
      "is_equity_holder": false,
      "signal_count": 8,
      "what_they_did": "2-3 sentences describing their actual contributions with specifics.",
      "cons": "1-2 sentences — honest caveats, tradeoffs, or concerns.",
      "key_evidence": [
        {
          "message_link": "https://discord.com/channels/...",
          "description": "What this message shows (N reactions, N replies)"
        }
      ],
      "community_response": "1 sentence — who endorsed their work and how."
    }
  ],
  "notable_mentions": [
    {
      "username": "...",
      "signal_count": 3,
      "is_equity_holder": true,
      "note": "1 sentence — what they did and why they didn't get a full evaluation."
    }
  ]
}
```

## Evaluation guidelines

- **You are adding qualitative judgment, not restating numbers.** Layer 1 already counted reactions, signals, and mentions. Don't repeat that. Your value is reading the *content* and making sense of it.
- **"What they did" must name specifics.** Not "contributed to the ecosystem" — "shipped ComfyUI-LoRA-Optimizer with Triton-accelerated SVD scoring" or "answered 15 troubleshooting questions about VRAM management in #comfyui." If you can't name anything after reading their profile, they're Weak.
- **Read the actual messages.** The difference between a builder and a chatter is in the content, not the metrics. Someone sharing `github.com/their-repo` with a description of what it does is different from someone saying "yeah that's cool."
- **Read the actual replies.** "This fixed my issue, thank you!" is genuine value. "nice" is not. The profile shows you both — distinguish them.
- **Cons must be substantive.** Not "could contribute more" — that's filler. Real cons: "work is unreleased and still experimental," "attribution dispute with another contributor," "almost all activity is in NSFW channels," "shared 5 GitHub links but 4 were to other people's repos."
- **Category should reflect what they primarily did this month.** Someone who built a tool AND shared art is categorized by their primary impact.

## Coverage

- **Every person with 4+ signals must be profiled and evaluated.** No exceptions.
- **People with 3 signals and 10+ max reactions** should also be profiled.
- **Existing equity holders** who appear in the signals file should be in notable_mentions with a note on their March activity.
- **People whose profile reveals trivial activity** after investigation get a Weak verdict or go in notable_mentions — but they must appear somewhere. No silent drops.
