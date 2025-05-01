# BNDC's Brain

This is the Brain of BNDC, a friendly robot dedicated to helping our around the Banodoco and open source AI art communities. His goal is to streamline the sharing and discovery of knowledge, making it easier for everyone to contribute, learn, and connect. 

## Features

- 📚 **Summarization:** Generates daily or on-demand summaries of activity, grouped by topic (`summarising`).
- ✍️ **Content Synthesis:** Creates long-form articles and reports by synthesizing related discussions (combines `summarising`/`answering`).
- 💾 **Archiving & Logging:** Maintains a searchable archive of all messages, files, and media (`logging`).
- ✨ **Curation:** Automatically identifies high-quality posts and important discussions (`curating`).
- ⚡ **Reaction Workflows:** Triggers automated actions based on message reactions (`reacting`).
- 🔗 **Message Relaying:** Relays messages to external services or platforms via webhooks (`relaying`).
- 📣 **Social Sharing:** Shares curated content or summaries to external platforms like Twitter (`sharing`).
- 🧠 **Question Answering:** Answers questions about past discussions using the community's conversation history (`answering`) (Coming Soon).

## Live Demo

Want to see it in action? Join the [Banodoco Discord server](https://discord.gg/NnFxGvx94b) to see the bot's daily summaries and features live!

## Setup

### Installation

1. Clone the repository:
```bash
git clone https://github.com/peteromallet/bndc-engine.git
cd bndc-engine

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

### Running the Bot

Basic operation:
```bash
python main.py
```

Development mode:
```bash
python main.py --dev
```

Run summary immediately:
```bash
python main.py --run-now
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

### Development Mode

Run the bot in development mode to:
- Use test data instead of live channels
- Test in a development server
- Avoid affecting production data

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