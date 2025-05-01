# Project Directory Structure

This document provides an overview of the `bndc` code-base, detailing the purpose of each directory and file. Non-source-code elements like logs, databases, temporary files, and environment specifics have been omitted for clarity.

```
.
├── README.md                    # Project overview, setup, and contribution guidelines
├── requirements.txt             # Python dependency lockfile
├── main.py                      # Single entry-point that bootstraps and launches the Discord bot
│
├── scripts/                     # One-off or batch maintenance / migration utilities
│   ├── analyze_channels.py          # Analyse server channels and export stats
│   ├── archive_discord.py           # Bulk archive messages & attachments to local DB / storage
│   ├── backfill_reactions.py        # Populate missing reaction records in DB
│   ├── cleanup_empty_threads.py     # Remove defunct Discord threads
│   ├── cleanup_test_data.py         # Purge development data
│   ├── create_dev_db.py            # Generate a fresh dev SQLite database
│   ├── download_files.py            # Download attachments referenced in the DB
│   ├── download_videos.py           # Fetch remote videos for local storage
│   ├── migrate_add_category_id.py   # Migration adding category_id column
│   ├── migrate_channel_summary.py   # Migration for channel summary table
│   ├── migrate_db.py                # General DB migration helper
│   ├── migrate_summaries.py         # Backfill summaries to new schema
│   └── monthly_equity_shortlist.py  # Monthly analytics batch job
│
├── src/                         # Core application package
│   ├── __init__.py                  # Marks directory as importable module
│   │
│   ├── common/                      # Shared infrastructure, utilities & abstractions
│   │   ├── __init__.py                  # Exposes helper imports
│   │   ├── base_bot.py                  # `BaseDiscordBot` – subclass of `commands.Bot` adding common helpers
│   │   ├── constants.py                 # Global constant values (e.g. max lengths)
│   │   ├── db_handler.py                # Thin wrapper around SQLite queries & schema management
│   │   ├── error_handler.py             # Custom exception & error reporting utilities
│   │   ├── errors.py                    # Domain-specific error classes
│   │   ├── log_handler.py               # Centralised logging setup
│   │   ├── rate_limiter.py              # Simple in-memory rate-limiting helper
│   │   ├── schema.py                    # Pydantic data models mirroring DB tables
│   │   ├── discord_client.py            # (Placeholder) extended Discord client if needed
│   │   └── llm/                         # Language-model client abstractions
│   │       ├── __init__.py                  # Factory returning correct LLM client
│   │       ├── base_client.py               # Interface for all LLM providers
│   │       ├── claude_client.py             # Anthropic Claude implementation
│   │       ├── openai_client.py             # OpenAI GPT implementation
│   │       └── gemini_client.py             # Google Gemini implementation
│   │
│   └── features/                    # Modular bot capabilities (each in its own sub-package)
│       ├── answering/                  # Q&A over archived content
│       │   ├── __init__.py                # Helper re-exports
│       │   └── answerer.py               # Implements retrieval augmented generation to answer queries
│       │
│       ├── admin/                      # Owner / admin only commands
│       │   ├── __init__.py                # N/A
│       │   └── admin_cog.py              # Commands to reload cogs, run diagnostics, etc.
│       │
│       ├── curating/                   # Highlight & curation logic
│       │   ├── __init__.py                # N/A
│       │   ├── curator.py                # Identifies high-quality posts, manages curation DB
│       │   └── curator_cog.py            # Discord commands / listeners exposing curator
│       │
│       ├── logging/                    # Real-time message logging to DB
│       │   ├── logger.py                 # Consumes Discord events and writes to DB
│       │   └── logger_cog.py             # Cog wrapping the above for Discord.py
│       │
│       ├── reacting/                   # Automated reaction-based workflows
│       │   ├── reactor.py                # Core business logic – watches for reactions & performs actions
│       │   └── reactor_cog.py            # Discord event listeners forwarding to `Reactor`
│       │
│       ├── relaying/                   # Webhook relay of messages to external services
│       │   ├── relayer.py                # Handles outbound webhooks respecting auth/signing
│       │   └── relaying_cog.py           # Cog exposing relay commands & background tasks
│       │
│       ├── sharing/                    # Social sharing / cross-posting
│       │   ├── __init__.py               # N/A
│       │   ├── sharer.py                 # Schedules and posts content to Twitter, Zapier, etc.
│       │   ├── sharing_cog.py            # Discord interface to manage sharing jobs
│       │   └── subfeatures/              # Helper modules used by `Sharer`
│       │       ├── __init__.py               # N/A
│       │       ├── content_analyzer.py       # Extract hashtags, categories & media metadata
│       │       ├── notify_user.py            # DM users about successful / failed shares
│       │       └── social_poster.py          # Compose and send posts to specific platforms
│       │
│       └── summarising/                # Daily / on-demand summary generation
│           ├── __init__.py                # N/A
│           ├── summariser.py             # Groups messages by topic and crafts summaries using LLMs
│           ├── summariser_cog.py         # Commands / scheduled tasks for summaries
│           └── subfeatures/              # Specialised summary types
│               ├── __init__.py               # N/A
│               ├── news_summary.py           # Summaries focussed on newsworthy events
│               ├── top_art_sharing.py        # Picks top images/videos to share externally
│               └── top_generations.py        # Highlights most reacted-to AI generations
```

---

### How to Read This Document

• **Directories** are shown with a trailing `/` and indented tree lines.
• This overview should help new contributors quickly locate relevant modules and understand how the bot's functionality is partitioned across the code-base. 