# Run Supabase Migration - Summary Tables

## ✅ Migration File Created
`supabase/migrations/20251104154121_add_summary_tables.sql`

This migration adds:
- `channel_summary` table
- `daily_summaries` table
- Indexes
- RLS policies

## 🚀 How to Run the Migration

You have **3 options**:

### Option 1: Push to Remote Supabase (Recommended)

```bash
# Step 1: Link to your Supabase project (if not already linked)
supabase link --project-ref <your-project-ref>

# Step 2: Push the migration
supabase db push

# Alternative: Push directly to remote
supabase db push --db-url "$DATABASE_URL"
```

**How to get your project ref:**
- Go to your Supabase dashboard
- Look at the URL: `https://app.supabase.com/project/<your-project-ref>`
- Or find it in Project Settings → General → Reference ID

### Option 2: Use Environment Variable

```bash
# If you have DATABASE_URL set
supabase db push --db-url "$DATABASE_URL"
```

### Option 3: Run SQL Directly in Dashboard

If the CLI doesn't work, just copy/paste the migration file:

1. Open [Supabase Dashboard](https://app.supabase.com)
2. Go to **SQL Editor**
3. Click **New Query**
4. Copy/paste: `supabase/migrations/20251104154121_add_summary_tables.sql`
5. Click **Run**

## 🧪 Verify Migration Worked

After running, test with:

```bash
python main.py --dev --storage-backend supabase --summary-now
```

Look for these logs:
```
✅ Storing summary to Supabase for channel X
✅ Fetching summary thread ID from Supabase
✅ Updated summary thread ID in Supabase
```

## 📋 Migration File Location

Created at: `supabase/migrations/20251104154121_add_summary_tables.sql`

This follows Supabase's naming convention: `{timestamp}_{description}.sql`

