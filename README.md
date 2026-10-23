# BNDC's Brain <img src="bndc.png" align="right" width="150px">

This is the Brain of BNDC, a friendly robot dedicated to helping our around the Banodoco and open source AI art communities. His goal is to streamline the sharing and discovery of knowledge, making it easier for everyone to contribute, learn, and connect.

## Features

- 📚 **Live Updates:** Runs hourly topic-editor passes that publish structured community updates (`summarising`).
- ✍️ **Content Synthesis:** Creates long-form articles and reports by synthesizing related discussions (combines `summarising`/`answering`).
- 💾 **Archiving & Logging:** Maintains a searchable archive of all messages, files, and media (`logging`).
- ✨ **Curation:** Automatically identifies high-quality posts and important discussions (`curating`).
- ⚡ **Reaction Workflows:** Triggers automated actions based on message reactions (`reacting`).
- 🔗 **Message Relaying:** Relays messages to external services or platforms via webhooks (`relaying`).
- 📣 **Social Sharing:** Shares curated content or summaries to external platforms like Twitter (`sharing`).
- 🧠 **Question Answering:** Answers questions about past discussions using the community's conversation history (`answering`) (Coming Soon).

## Code Structure

For a detailed LLM-friendly overview of the code structure, see [structure.md](structure.md). This file provides a comprehensive breakdown of each directory and module that can be used to help understand or extend the codebase.

## Live Demo

Want to see it in action? Join the [Banodoco Discord server](https://discord.gg/NnFxGvx94b)!

## Setup

### Deployment Options

You can run this bot either:
- **Locally** - On your own machine (see Local Installation below)
- **Railway** - Deploy to the cloud with one click (see [RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md))

### Local Installation

1. Clone the repository:
```bash
git clone https://github.com/banodoco/brain-of-bndc.git
cd brain-of-bndc

```

2. Install required dependencies:
```bash
pip install -r requirements.txt
```

3. Copy the example environment file `.env.example` to a new file named `.env`:
```bash
cp .env.example .env
```
Then, open the `.env` file and fill in the required values. Refer to the comments in `.env.example` for guidance on each variable.

### Railway Deployment

For deploying to Railway (recommended for production):

1. See the complete guide: [RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md)
2. Quick start: [RAILWAY_QUICK_START.md](RAILWAY_QUICK_START.md)
3. Railway offers:
   - ✅ Automatic deployments from GitHub
   - ✅ Free tier available
   - ✅ Built-in logging and monitoring
   - ✅ Easy environment variable management
   - ✅ Persistent storage options

### Database Storage

The bot uses **Supabase** (Cloud PostgreSQL) for data storage:
- See [SUPABASE_MIGRATION.md](SUPABASE_MIGRATION.md) for setup guide
- Free tier: 500MB storage, perfect for most Discord servers
- Automatic backups and high scalability

Ensure you have `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` environment variables set.

#### Database migrations

Canonical Supabase CLI migrations live in `../supabase/migrations/` (workspace-level, a separate git repo sitting next to `brain-of-bndc/`).

- Name every migration `YYYYMMDDHHMMSS_description.sql`.
- Keep every migration idempotent because production may already contain the schema drift being backfilled: use `IF NOT EXISTS`, `CREATE OR REPLACE`, and guarded backfills that only touch rows still needing updates.
- Apply migrations with `supabase db push` and validate fresh state with `supabase db reset`.
- Do not drop ad hoc SQL into a bot-repo `sql/` folder; the workspace-level Supabase repo is the canonical migration source of truth.

### Running the Bot

#### Direct Speaker invites

Equity Holders can use `/direct_invite recipient:@username` or
`/direct_invite recipient:123456789012345678` (a user mention also works).
An existing member receives Speaker immediately. For someone who has not joined,
the bot saves an approval and returns an invite; that account receives Speaker
when joining through **any** link. The approval is consumed after successful
promotion. Failed assignments retain the approval so the command can be retried.
This does not unmute a Moderated member.

Use the exact Discord **account username**, not a display name or server nickname.
An optional leading `@` and case differences are accepted. Prefix a long numeric
username with `@` to distinguish it from an ID. An unresolved username is matched
against the account's current username on join, then bound to its user ID; it
cannot follow a username change made before that first match. Use a user ID when
available. Existing members' usernames are resolved to IDs immediately.

Recipient-specific links do not grant Speaker to other accounts and do not
replace the guild's generic Speaker invite. Omitting `recipient` preserves the
existing invite-use tracking behavior (`max_uses` defaults to 10, capped at 10).
Invite use limits apply to the link, not the saved identity approval.

Before deploying this feature, apply the paired workspace migration
[`20261023120000_direct_invite_approvals.sql`](https://github.com/banodoco/banodoco-workspace/pull/1)
from `banodoco/banodoco-workspace`'s `supabase/migrations/`. The approval table is
accessible to the service role only. If storage is unavailable, targeted commands
report failure instead of issuing an unrecorded approval; generic invites still
work. Restart the bot after deployment so its command sync registers `recipient`.

Basic operation:
```bash
python main.py
```

Development mode - using test data and channels:
```bash
python main.py --dev
```

Run summary immediately:
```bash
python main.py --run-now
```

Archive historical messages (e.g., from the past 30 days):
```bash
python scripts/archive_discord.py --days 30
```

### Bot Permissions

The bot requires the following Discord permissions:
- Read Messages/View Channels
- Send Messages
- Create Public Threads
- Send Messages in Threads
- Manage Messages (for pinning)
- Read Message History
- Attach Files
- Add Reactions
- View Channel
- Manage Threads

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

Archive management commands:
```bash
# Archive specific channels or date ranges
python scripts/archive_discord.py --channel-id <channel_id> --start-date YYYY-MM-DD

# Clean up test or temporary data
python scripts/cleanup_test_data.py

# Migrate database schema
python scripts/migrate_db.py
```
