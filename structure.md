# BNDC Bot: Developer Guide

> **How to Use This Guide**  
> • Skim the Tech Stack & Feature tables to orient yourself.  
> • Use the Directory Tree to find specific files.  
> • When in doubt, the source of truth is always the code – this guide just points you in the right direction.

> **When to Update This Guide**  
> • Add, delete, or rename files/directories.  
> • Add new features or significantly refactor existing ones.  
> • Modify database schema or add migrations.  
> • Change environment variables or deployment config.  
> • Any change that would confuse a new dev skimming this file.

> **Who This Guide Is For**  
> • 🤖 AI assistants + 👨‍💻 Human developers

---

## Table of Contents
- [Tech Stack](#tech-stack)
- [Key Concepts](#key-concepts)
- [Features Overview](#features-overview)
- [Directory Structure](#directory-structure)
- [Supabase Schema](#supabase-schema)

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| **Bot Framework** | Discord.py | Discord bot with cogs architecture |
| **Database** | Supabase (PostgreSQL) | Message archive, member profiles, summaries, logs |
| **LLM Providers** | Claude, OpenAI, Gemini | Summaries, content analysis, dispute resolution |
| **Deployment** | Railway + Docker | Production hosting with Nixpacks builds |
| **Logging** | Python logging → Supabase | Centralized logs with 48h retention |

### Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `DISCORD_BOT_TOKEN` | Bot authentication |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Database connection |
| `REACTION_WATCHLIST` | JSON config for reaction-triggered workflows |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM provider keys |
| `ADMIN_USER_ID` | Discord user ID for admin chat feature (DM-only access) |
| `DEV_MODE` | Enables verbose logging, skips "already summarized" checks |
| `OPENMUSE_FEATURING_CHANNEL_ID` | Channel ID for OpenMuse featuring posts |
| `NO_SHARING_ROLE_ID` | Discord role ID assigned to users who opt out of content sharing |

---

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Cogs** | Discord.py's modular extension system. Each feature has a `_cog.py` that registers commands/listeners with the bot. |
| **Feature Structure** | Features live in `src/features/[name]/` with: core logic (`reactor.py`) + Discord integration (`reactor_cog.py`) + optional `subfeatures/` for complex actions. |
| **Reaction Watchlist** | JSON env var (`REACTION_WATCHLIST`) that configures which emoji reactions trigger which actions. Central routing for all reaction-based workflows. |
| **Archiving** | Messages are archived from Discord → Supabase via `archive_runner.py`. Can run on-demand or scheduled. |
| **Summaries** | LLM-generated daily digests per channel, stored in `daily_summaries` table, posted to dedicated threads. |
| **Member Permissions** | Two boolean flags with TRUE defaults: `include_in_updates` (can be mentioned in summaries/digests) and `allow_content_sharing` (content can be shared externally). When `allow_content_sharing=FALSE`, a Discord role is assigned to make opt-out visible. |

---

## Features Overview

| Feature | Location | Purpose |
|---------|----------|---------|
| **Admin** | `src/features/admin/` | Owner commands: reload cogs, diagnostics, sync management |
| **Admin Chat** | `src/features/admin_chat/` | Claude-powered DM chat for ADMIN_USER_ID with tool use (search messages, share to social, etc.) |
| **Answering** | `src/features/answering/` | RAG-based Q&A over archived messages |
| **Archive** | `src/features/archive/` | Commands to trigger message archiving |
| **Curating** | `src/features/curating/` | Identify & manage high-quality posts for external sharing |
| **Logging** | `src/features/logging/` | Real-time message logging to Supabase |
| **Reacting** | `src/features/reacting/` | Reaction-triggered workflows (tweets, uploads, disputes, etc.) |
| **Relaying** | `src/features/relaying/` | Webhook relay to external services |
| **Sharing** | `src/features/sharing/` | Social media cross-posting (Twitter, etc.) |
| **Summarising** | `src/features/summarising/` | Daily LLM-generated channel summaries |

---

## Directory Structure

```
.
├── main.py                      # Entry point – bootstraps bot, loads cogs
├── requirements.txt             # Python dependencies
├── Procfile / railway.json      # Railway deployment config
├── Dockerfile / nixpacks.toml   # Container build config
│
├── scripts/                     # One-off maintenance utilities
│   ├── archive_discord.py          # Bulk archive messages to Supabase
│   ├── logs.py                      # Unified log monitoring tool (health, summary, errors, tail)
│   └── ...                          # Other utilities (see tree below)
│
├── ../supabase/migrations/       # Workspace-level Supabase repo (separate git root) holds the canonical timestamped SQL migrations
│
└── src/
    ├── common/                      # Shared infrastructure
    │   ├── content_moderator.py         # Image content moderation (WaveSpeed AI API)
    │   ├── db_handler.py                # Database abstraction layer
    │   ├── discord_utils.py             # Discord API helpers (safe_send_message, etc.)
    │   ├── error_handler.py             # @handle_errors decorator
    │   ├── log_handler.py               # Centralized logging setup
    │   ├── schema.py                    # Pydantic models for DB tables
    │   ├── storage_handler.py           # Supabase write operations
    │   ├── openmuse_interactor.py       # OpenMuse media uploads
    │   └── llm/                         # LLM client abstractions
    │       ├── __init__.py                  # Factory (get_llm_client)
    │       ├── claude_client.py
    │       ├── openai_client.py
    │       └── gemini_client.py
    │
    └── features/                    # Bot capabilities (one per subdirectory)
        ├── admin/
        │   └── admin_cog.py
        ├── admin_chat/
        │   ├── admin_chat_cog.py    # Discord DM listener for ADMIN_USER_ID
        │   ├── agent.py              # Claude agent with tool use loop (Arnold pattern)
        │   └── tools.py              # Tool definitions & executors (search, share, refresh_media, etc.)
        ├── answering/
        │   └── answerer.py
        ├── archive/
        │   └── archive_cog.py
        ├── curating/
        │   ├── curator.py
        │   └── curator_cog.py
        ├── logging/
        │   ├── logger.py
        │   └── logger_cog.py
        ├── reacting/
        │   ├── reactor.py               # Watchlist matching & action dispatch
        │   ├── reactor_cog.py
        │   └── subfeatures/
        │       ├── dispute_resolver.py      # LLM-powered dispute resolution
        │       ├── message_linker.py        # Unfurl Discord message links
        │       ├── openmuse_uploader.py     # Upload media to OpenMuse
        │       ├── permission_handler.py    # Curation consent flow
        │       ├── tweet_sharer_bridge.py   # Bridge to sharing feature
        │       └── workflow_uploader.py     # ComfyUI workflow uploads
        ├── relaying/
        │   ├── relayer.py
        │   └── relaying_cog.py
        ├── sharing/
        │   ├── sharer.py
        │   ├── sharing_cog.py
        │   └── subfeatures/
        │       ├── content_analyzer.py      # Extract hashtags, metadata
        │       ├── notify_user.py           # DM users about shares
        │       └── social_poster.py         # Platform-specific posting
        └── summarising/
            ├── summariser.py
            ├── summariser_cog.py
            └── subfeatures/
                ├── news_summary.py
                ├── top_art_sharing.py
                └── top_generations.py
```

### Scripts Reference

| Script | Purpose |
|--------|---------|
| `archive_discord.py` | Bulk archive messages & attachments to Supabase |
| `analyze_channels.py` | Analyse channels with LLM, export stats |
| `backfill_reactions.py` | Populate missing reaction records |
| `logs.py` | Unified log monitoring: `health`, `summary`, `errors`, `recent`, `search`, `tail`, `stats` |

---

## Supabase Schema

### Core Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `discord_messages` | Archived messages | `message_id` (PK), `channel_id`, `author_id`, `content`, `created_at`, `attachments` (JSONB), `reaction_count`, `is_deleted` |
| `discord_members` | Member profiles & permissions | `member_id` (PK), `username`, `global_name`, `twitter_handle`, `reddit_handle`, `include_in_updates` (default TRUE), `allow_content_sharing` (default TRUE) |
| `discord_channels` | Channel metadata | `channel_id` (PK), `channel_name`, `description`, `suitable_posts`, `unsuitable_posts`, `enriched` |
| `daily_summaries` | Generated summaries | `daily_summary_id` (PK), `date`, `channel_id`, `full_summary`, `short_summary`, `included_in_main_summary`, `dev_mode` |
| `channel_summary` | Summary thread mapping | `channel_id` (PK), `summary_thread_id` |
| `system_logs` | Application logs | `id` (PK), `timestamp`, `level`, `logger_name`, `message`, `exception` |
| `sync_status` | Sync state tracking | `table_name`, `last_sync_timestamp`, `sync_status` |

### Views

| View | Purpose |
|------|---------|
| `recent_messages` | Last 7 days of messages with author/channel names |
| `message_stats` | Per-channel message counts and date ranges |
| `recent_errors` | ERROR/CRITICAL logs from last 24 hours |
| `log_stats` | Hourly log counts by level |

### Edge Functions

| Function | Purpose | Secrets Required |
|----------|---------|------------------|
| `refresh-media-urls` | Refresh expired Discord CDN attachment URLs | `DISCORD_BOT_TOKEN` |

**Deployment:**
```bash
# Deploy function
supabase functions deploy refresh-media-urls

# Set secrets
supabase secrets set DISCORD_BOT_TOKEN=your_token_here
```

**Usage:**
```bash
# Refresh URLs for a specific message
curl -X POST 'https://<project>.supabase.co/functions/v1/refresh-media-urls' \
  -H 'Authorization: Bearer <anon_key>' \
  -H 'Content-Type: application/json' \
  -d '{"message_id": "123456789"}'

# Response:
# {
#   "success": true,
#   "message_id": "123456789",
#   "attachments": [{"id": "...", "filename": "image.png", "url": "https://cdn.discordapp.com/...", ...}],
#   "urls_updated": 1
# }
```

### Notes
- All tables have RLS enabled (service-role access only)
- `system_logs` auto-cleaned hourly via `pg_cron` (48h retention)
- Full-text search on `discord_messages.content` and `system_logs.message`
