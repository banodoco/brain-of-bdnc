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
│   │   ├── constants.py                 # Global constant values (e.g. max lengths). Developers should define new global, non-configurable constants here to avoid magic numbers/strings.
│   │   ├── db_handler.py                # Thin wrapper around SQLite queries & schema management. All database interactions should go through this handler. Avoid raw SQL queries directly in feature code.
│   │   ├── discord_utils.py             # Utilities for common Discord API interactions. Developers should prefer using helpers like 'safe_send_message' from this module for sending messages to ensure consistency, rate limiting, and error handling.
│   │   ├── error_handler.py             # Custom exception & error reporting utilities. Utilize provided utilities (e.g., `@handle_errors` decorator) for consistent error handling and reporting.
│   │   ├── errors.py                    # Domain-specific error classes. Define and use these for domain-specific exceptions to provide more context than generic errors.
│   │   ├── log_handler.py               # Centralised logging setup. Ensure loggers are named appropriately (e.g., logging.getLogger(__name__)) to benefit from the centralized configuration provided by this handler.
│   │   ├── rate_limiter.py              # Simple in-memory rate-limiting helper. Use for external API calls or frequent Discord actions, often via bot.rate_limiter or integrated utilities.
│   │   ├── schema.py                    # Pydantic data models mirroring DB tables. Use these for data validation, defining structured data, and for serialization/deserialization with the database or APIs.
│   │   ├── discord_client.py            # (Placeholder) extended Discord client if needed
│   │   └── llm/                         # Language-model client abstractions
│   │       ├── __init__.py                  # Factory returning correct LLM client. Interact with LLMs via the factory/helper functions provided here to abstract specific client implementations and centralize configuration.
│   │       ├── base_client.py               # Interface for all LLM providers. New LLM client implementations must adhere to this interface.
│   │       ├── claude_client.py             # Anthropic Claude implementation
│   │       ├── openai_client.py             # OpenAI GPT implementation
│   │       └── gemini_client.py             # Google Gemini implementation
│   │
│   └── features/                    # Modular bot capabilities (each in its own sub-package). Major new features should follow this modular structure, often with a core logic file and a _cog.py for Discord integration. Complex actions can be further modularized into a subfeatures/ directory within the feature's package. WE should try to fit new features into existing capabilities where possible.
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
│       ├── reacting/                   # Automated reaction-based workflows. All workflows triggered by message content, reactions, or attachments should be routed through this feature via the WATCHLIST_JSON configuration.
│       │   ├── reactor.py                # Core business logic – watches for reactions & performs actions
│       │   └── reactor_cog.py            # Discord event listeners forwarding to `Reactor`
│       │   └── subfeatures/              # Helper modules for specific reaction-triggered actions
│       │       ├── __init__.py               # Marks directory as a Python package
│       │       ├── permission_handler.py     # Handles curation permission requests and view logic
│       │       ├── dispute_resolver.py       # Manages dispute resolution process using LLMs
│       │       ├── openmuse_uploader.py      # Handles direct uploads to OpenMuse triggered by reactions
│       │       └── tweet_sharer_bridge.py    # Bridges reaction events to the Sharer for social media posting
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