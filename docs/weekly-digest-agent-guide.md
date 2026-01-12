# Weekly Digest Agent Guide

This guide explains how to create a weekly digest document for a Discord category, featuring the most interesting discussions and media from the past week.

## Overview

You will:
1. Explore messages from a Discord category using utility commands
2. Identify the most engaging content (high reactions, interesting discussions)
3. **Build the document incrementally** - add items as you find them
4. Refresh media URLs as you go
5. **Edit down** from ~15-20 draft items to the best 10-12
6. **Get user feedback** - share draft and wait for approval
7. **Study the writing style** from the updates channel
8. **Rewrite to match** the established tone and format
9. Final polish for quality and consistency

**Key principle:** Write as you explore, then edit, get feedback, then style-match.

## Target Category

**Category ID:** `1457980621929451595`

**Channels:**
- `ltx_training` (1457981700817817620) - Training discussions and resources
- `ltx_resources` (1457981813120176138) - Shared resources, workflows, models
- `ltx_chatter` (1309520535012638740) - General discussion
- `ltx_gens` (1458032975982755861) - Generations and outputs

---

## Step 1: Discover Top Content

Start by getting an overview of the most engaging content across all channels:

```bash
# Get top 30 messages by reactions across the category (last 7 days)
python scripts/weekly_digest.py category-top 1457980621929451595 --days 7 --limit 30
```

This shows messages sorted by reaction count. Note:
- **Message IDs** - You'll need these to get more context and refresh media
- **Reaction counts** - Higher = more community interest
- **Channel names** - Helps understand the topic area
- **Attachments** - Media that could be included in the digest

### Per-Channel Deep Dive

If you want to explore specific channels more thoroughly:

```bash
# Get all messages from a specific channel
python scripts/weekly_digest.py messages 1309520535012638740 --days 7 --limit 100

# Get top messages from a specific channel
python scripts/weekly_digest.py top 1309520535012638740 --days 7 --min-reactions 2
```

---

## Step 2: Explore Message Context

For promising messages, get the full context to understand the discussion:

```bash
# Get a message with its replies and surrounding conversation
python scripts/weekly_digest.py context MESSAGE_ID --surrounding 10

# Follow the entire reply chain (finds root message + all nested replies)
python scripts/weekly_digest.py thread MESSAGE_ID
```

**Context** returns:
- **BEFORE**: Messages posted just before the target
- **TARGET MESSAGE**: The message itself with full details
- **AFTER**: Messages posted just after
- **REPLIES**: All messages that directly replied to it (i.e., messages where `reference_id == MESSAGE_ID`)

**Thread** returns:
- The entire conversation tree from root to all leaves
- Shows which messages replied to which

### Explore by User

If you notice a particular user has interesting content:

```bash
# Get all messages from a user (optionally filtered to the category)
python scripts/weekly_digest.py user Kijai --category 1457980621929451595 --days 7
python scripts/weekly_digest.py user harelcain --limit 20
```

### Search by Keyword

Find discussions about specific topics:

```bash
# Search for messages containing keywords
python scripts/weekly_digest.py search "workflow" --category 1457980621929451595
python scripts/weekly_digest.py search "LoRA" --days 14
python scripts/weekly_digest.py search "training" --limit 50
```

### Find Media-Only Posts

Focus on posts with attachments (images/videos):

```bash
# Get messages with media, sorted by reactions
python scripts/weekly_digest.py media --category 1457980621929451595 --min-reactions 3
```

Use these to:
- Understand what sparked the conversation
- Find related insights in replies
- Identify if there are multiple interesting perspectives
- Deep-dive into a prolific contributor's work
- Find discussions about specific features or tools

---

## Step 3: Refresh Media URLs

Discord CDN URLs expire. Before including any media in the digest, refresh the URLs:

```bash
# Preview the refresh (dry run)
python scripts/weekly_digest.py refresh MESSAGE_ID --dry-run

# Actually refresh and update the database
python scripts/weekly_digest.py refresh MESSAGE_ID

# Batch refresh multiple messages at once
python scripts/weekly_digest.py batch-refresh "1458793441789083669,1458788193691373569,1458518180619223235"
```

The command will:
1. Fetch the message from Discord API
2. Get fresh attachment URLs with new expiry tokens
3. Update the database with valid URLs

**Important:** Always refresh media URLs for any message you'll include in the digest.

**Tip:** Collect all the message IDs you plan to use, then batch-refresh them all at once to save time.

**Tip (do this right at the end too):** Re-run refresh (or batch-refresh) **right before finalizing/sending** the doc, so the media links are fresh even if you drafted earlier.

---

## Step 4: Build the Digest Document Incrementally

**Important:** Don't wait until you've explored everything to start writing. Create the markdown file early and **add to it as you go**:

1. Create the file: `results/2025-01_ltx_weekly_digest.md`
2. As you find interesting content, immediately add a draft entry
3. Include the message ID, author, and your initial notes
4. Refresh media URLs as you add each item
5. Keep exploring and adding until you have 15-20 draft items

This approach ensures you don't lose track of good content and makes the final editing phase much easier.

### Document Structure

```markdown
# LTX Video Weekly Digest - [Date Range]

A summary of the most interesting developments, discussions, and creations from the LTX Video community this week.

---

## 1. [Topic Title]

**By [Username]** | [Channel Name] | [X reactions]

[Description of what happened, what was shared, or what was discovered. 2-3 sentences explaining why this is interesting.]

[If there's media, include it:]

# If it's an image:
![Description](REFRESHED_IMAGE_URL)

# If it's a video:
[Video link](REFRESHED_VIDEO_URL)
# (Optionally also paste the URL on its own line for easy clicking)

[Link to original message]

---

## 2. [Next Topic]
...
```

### Content Guidelines

Based on `src/features/summarising/subfeatures/news_summary.py`, prioritize:

1. **Original creations** - Custom nodes, workflows, tools, techniques created by community members
2. **Notable achievements** - Impressive demonstrations or results
3. **High engagement content** - Things that got lots of reactions/discussion
4. **New features/announcements** - Updates people are excited about
5. **Shared resources** - Workflows, models, scripts that help others
6. **Discoveries** - New techniques or interesting findings

### Writing Style

- **Attribution**: Always credit creators with bold usernames: "**Username** shared..."
- **Evidence-based**: Only report what's clearly demonstrated or stated
- **Not hyperbolic**: Describe accurately without excessive enthusiasm
- **Contextual**: Explain why something matters to the community
- **Concise**: Keep each item to 2-4 sentences plus media

---

## Step 5: Edit Down to Best Items

Now that you have 15-20 draft items, **edit down to the best 10-12**:

1. **Rank items** by quality, uniqueness, and community engagement
2. **Remove duplicates** or items covering similar ground
3. **Cut weaker items** - better to have 10 excellent items than 15 mediocre ones
4. **Expand the keepers** - add more context, better descriptions
5. **Verify all media** - ensure URLs work and media is relevant

---

## Step 6: Get User Feedback

**STOP and ask the user for feedback before proceeding.**

Share the current draft with the user and ask:
- Are these the right topics to cover?
- Any items to cut or add?
- Any specific style/tone preferences?
- Anything missing that should be included?

Wait for their input before moving to the style-matching step.

---

## Step 7: Match the Writing Style (After Feedback)

Once you have user approval on the content, study the writing style from the main updates channel to ensure consistency.

### Fetch Reference Examples

```bash
# Get recent posts from the updates channel to study the style
python scripts/weekly_digest.py messages 1138790534987661363 --days 30 --limit 20

# Or check announcements for more formal style reference
python scripts/weekly_digest.py messages 1246615722164224141 --days 60 --limit 10
```

Available reference channels:
- `updates` (1138790534987661363) - Regular community updates
- `announcements` (1246615722164224141) - More formal announcements
- `dev_updates` (1138787208904581160) - Developer-focused updates

### Analyze the Style

Read through 5-10 recent update posts and note:
- **Tone**: Is it casual, professional, enthusiastic?
- **Length**: How long are typical item descriptions?
- **Structure**: How are titles formatted? How is media introduced?
- **Attribution**: How are creators credited?
- **Vocabulary**: What terms are commonly used? ("Banodocians", specific tool names, etc.)

### Apply the Style

Now rewrite your digest entries to match:
- Use the same tone and voice
- Match the typical length and structure
- Use consistent terminology
- Format titles and media references the same way

**Example transformation:**

*Before (generic):*
> Kijai released a new node for audio generation with LTX-2.

*After (matching community style):*
> **Kijai** dropped a new audio guide node for LTX-2, letting you sync generated video to audio cues. The results are impressively cohesive:

---

## Step 8: Final Polish

Review your final 10-12 items and ensure:

1. **Variety**: Mix of channels, authors, and topic types
2. **Media quality**: Each media item actually loads and is relevant
3. **Accuracy**: All claims are supported by the actual messages
4. **Flow**: Topics are ordered logically (could be chronological or by theme)
5. **Completeness**: Each item has proper attribution and context

### Final Checklist

- [ ] Started with 15-20 draft items, edited down to best 10-12
- [ ] All media URLs refreshed and working
- [ ] Each creator properly credited with **bold username**
- [ ] Jump URLs included for original messages
- [ ] No duplicate or overlapping topics
- [ ] Balanced coverage across channels (if applicable)
- [ ] Studied writing style from updates channel
- [ ] Rewrote entries to match established tone and format
- [ ] Writing is polished and consistent throughout

---

## Utility Reference

### Available Commands

```bash
# === DISCOVERY ===

# List channels in a category
python scripts/weekly_digest.py channels CATEGORY_ID

# Get messages from a channel (last N days)
python scripts/weekly_digest.py messages CHANNEL_ID --days 7 --limit 50

# Get top messages by reactions (single channel)
python scripts/weekly_digest.py top CHANNEL_ID --days 7 --min-reactions 3 --limit 20

# Get top messages across entire category
python scripts/weekly_digest.py category-top CATEGORY_ID --days 7 --min-reactions 3 --limit 30

# Get messages with attachments only
python scripts/weekly_digest.py media --category CATEGORY_ID --days 7 --min-reactions 2 --limit 30
python scripts/weekly_digest.py media --channel CHANNEL_ID --days 7

# Search messages by content
python scripts/weekly_digest.py search "workflow" --category CATEGORY_ID --days 7 --limit 30
python scripts/weekly_digest.py search "LoRA" --days 14


# === USER & CONVERSATION EXPLORATION ===

# Get all messages from a specific user
python scripts/weekly_digest.py user USERNAME --days 7 --limit 30
python scripts/weekly_digest.py user Kijai --category CATEGORY_ID  # Filter to category

# Get message context (replies + surrounding messages)
python scripts/weekly_digest.py context MESSAGE_ID --surrounding 10

# Follow a reply chain (find root and all replies)
python scripts/weekly_digest.py thread MESSAGE_ID


# === MEDIA REFRESH ===

# Refresh expired media URLs (single message)
python scripts/weekly_digest.py refresh MESSAGE_ID
python scripts/weekly_digest.py refresh MESSAGE_ID --dry-run  # Preview only

# Batch refresh multiple messages
python scripts/weekly_digest.py batch-refresh "MSG_ID1,MSG_ID2,MSG_ID3"
python scripts/weekly_digest.py batch-refresh "MSG_ID1,MSG_ID2" --dry-run
```

### Discord Jump URLs

To link to original messages, use this format:
```
https://discord.com/channels/GUILD_ID/CHANNEL_ID/MESSAGE_ID
```

Guild ID for Banodoco: `1076117621407223829`

### Getting Fresh Media URLs

After refreshing a message's media, you can get the URLs from the database:

```python
# In a Python script or REPL
from dotenv import load_dotenv
import os, json
from supabase import create_client

load_dotenv()
client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))

result = client.table('discord_messages').select('attachments').eq('message_id', MESSAGE_ID).execute()
attachments = result.data[0]['attachments']
if isinstance(attachments, str):
    attachments = json.loads(attachments)

for att in attachments:
    print(f"Filename: {att['filename']}")
    print(f"URL: {att['url']}")
```

---

## Example Output

Here's what a completed digest entry should look like:

```markdown
## 3. LTX 2.0 Produces Stunning Jensen Duels

**By harelcain** | ltx_chatter | 34 reactions

Harelcain demonstrated LTX 2.0's capability with an impressive AI-generated video featuring Jensen Huang, showcasing the model's ability to handle complex human motion and lighting. The generation sparked a playful "Jensen duel" trend in the community.

![Jensen Duel Video](https://cdn.discordapp.com/attachments/.../ltx-1767872.mp4?ex=...&is=...&hm=...)

[View original →](https://discord.com/channels/1076117621407223829/1309520535012638740/1458788193691373569)
```

---

## Troubleshooting

### "Message not found" when refreshing
The message may have been deleted from Discord. Skip it and find an alternative.

### Media URL still expired after refresh
Run the refresh command again - sometimes there's a timing issue. If it persists, the attachment may have been removed.

### Low reaction counts
Lower the minimum threshold: `--min-reactions 1` or browse all messages to find qualitatively interesting content that didn't get many reactions.

### Missing context
Increase the surrounding message count: `--surrounding 15` to get more conversation history.

