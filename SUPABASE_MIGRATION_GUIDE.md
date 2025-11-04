# Supabase Migration Guide - Summary Tables

This guide helps you migrate the summary-related tables to Supabase so the bot can run in `--supabase` only mode.

## Problem

Previously, two tables were only available in SQLite:
- `daily_summaries` - stores generated channel summaries
- `channel_summary` - stores thread IDs for summary threads

This caused the bot to fail when running with `--storage-backend supabase` only.

## Solution

We've added support for these tables in Supabase and updated the code to write/read from both backends.

## Migration Steps

### Step 1: Run the SQL Migration in Supabase

1. Open your Supabase project dashboard
2. Go to the SQL Editor
3. Open the migration file: `scripts/migrate_summary_tables_to_supabase.sql`
4. Copy and paste the SQL into the Supabase SQL editor
5. Click "Run" to execute the migration

This will create:
- `daily_summaries` table
- `channel_summary` table
- Appropriate indexes and policies
- Row Level Security (RLS) policies

### Step 2: (Optional) Migrate Existing Data

If you have existing summaries in SQLite that you want to migrate to Supabase, create a migration script:

```python
import sqlite3
import os
from datetime import datetime
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
supabase = create_client(supabase_url, supabase_key)

# Path to your SQLite database
DB_PATH = os.getenv('DEV_DATABASE_PATH', 'data/dev.db')

def migrate_summaries():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Migrate daily summaries
    cursor.execute("SELECT * FROM daily_summaries")
    summaries = [dict(row) for row in cursor.fetchall()]
    
    if summaries:
        print(f"Migrating {len(summaries)} daily summaries...")
        supabase.table('daily_summaries').upsert(summaries).execute()
        print("✅ Daily summaries migrated")
    
    # Migrate channel summary thread IDs
    cursor.execute("SELECT * FROM channel_summary")
    channel_summaries = [dict(row) for row in cursor.fetchall()]
    
    if channel_summaries:
        print(f"Migrating {len(channel_summaries)} channel summary records...")
        supabase.table('channel_summary').upsert(channel_summaries).execute()
        print("✅ Channel summaries migrated")
    
    conn.close()
    print("🎉 Migration complete!")

if __name__ == '__main__':
    migrate_summaries()
```

### Step 3: Test with Supabase-Only Mode

Now you can run the bot with Supabase-only storage:

```bash
# Test with dev mode
python main.py --dev --storage-backend supabase --summary-now

# Or set it as default in .env
STORAGE_BACKEND=supabase
```

## What Changed in the Code

1. **`storage_handler.py`**: Added methods:
   - `store_daily_summary_to_supabase()`
   - `update_summary_thread_to_supabase()`

2. **`db_handler.py`**: Updated methods to use Supabase:
   - `store_daily_summary()` - now writes to Supabase
   - `get_summary_thread_id()` - now reads from Supabase
   - `update_summary_thread()` - now updates Supabase

3. **Behavior by Storage Backend**:
   - `--storage-backend sqlite`: Only uses SQLite (local)
   - `--storage-backend supabase`: Only uses Supabase (cloud)
   - `--storage-backend both`: Uses both SQLite and Supabase (default, most reliable)

## Verification

After migration, check that tables exist in Supabase:

1. Go to Supabase Dashboard → Table Editor
2. Verify these tables exist:
   - `discord_messages` ✓
   - `discord_members` ✓
   - `discord_channels` ✓
   - `daily_summaries` ✓ (NEW)
   - `channel_summary` ✓ (NEW)

## Logs to Watch For

When running with Supabase:
- ✅ `"Storing summary to Supabase for channel X"`
- ✅ `"Fetching summary thread ID from Supabase for channel X"`
- ✅ `"Updating summary thread ID in Supabase for channel X"`

If you see errors, check:
1. That the migration SQL ran successfully
2. Your `SUPABASE_SERVICE_KEY` has write permissions
3. RLS policies are correctly configured

## Rollback

If you need to rollback to SQLite-only mode:

```bash
# In .env or command line
STORAGE_BACKEND=sqlite

# Or
python main.py --dev --storage-backend sqlite --summary-now
```

## Questions?

The code now fully supports Supabase-only mode for all operations including summaries. If you encounter any issues, check the logs for detailed error messages with the 🔄, ✅, and ❌ emoji prefixes.

