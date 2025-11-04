#!/usr/bin/env python3
"""
CLI script to migrate summary tables (daily_summaries and channel_summary) to Supabase.
This script reads the SQL migration file and executes it via the Supabase client.

Usage:
    python scripts/migrate_summary_tables.py

Requirements:
    - SUPABASE_URL environment variable
    - SUPABASE_SERVICE_KEY environment variable
    - migrate_summary_tables_to_supabase.sql file in scripts/ directory
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the project root to the path so we can import our modules
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from src.common.log_handler import setup_logging


async def migrate_summary_tables():
    """Create the summary tables in Supabase."""
    logger = setup_logging(dev_mode=False)
    logger.info("🚀 Migrating summary tables to Supabase...")
    
    # Get Supabase credentials
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not supabase_url or not supabase_key:
        logger.error("❌ SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables must be set.")
        return False
    
    try:
        # Initialize Supabase client
        options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
        supabase = create_client(supabase_url, supabase_key, options=options)
        logger.info("✅ Supabase client initialized successfully.")
        
        # Read the SQL migration file
        migration_file = Path(__file__).parent / "migrate_summary_tables_to_supabase.sql"
        if not migration_file.exists():
            logger.error(f"❌ Migration file not found: {migration_file}")
            return False
        
        with open(migration_file, 'r') as f:
            sql_content = f.read()
        
        logger.info(f"📄 Read migration file: {migration_file.name}")
        
        # Split the SQL into individual statements
        statements = [stmt.strip() for stmt in sql_content.split(';') if stmt.strip()]
        
        logger.info(f"📋 Found {len(statements)} SQL statements to execute...")
        
        # Execute each statement directly using the supabase client
        for i, statement in enumerate(statements, 1):
            if not statement:
                continue
            
            # Skip comments
            if statement.strip().startswith('--'):
                continue
                
            try:
                logger.info(f"⏳ Executing statement {i}/{len(statements)}...")
                logger.debug(f"SQL: {statement[:100]}...")
                
                # Use PostgREST to execute raw SQL via rpc
                # Note: This requires having an rpc function set up in Supabase
                # For direct SQL execution, we need to use the Supabase dashboard or psql
                # As a workaround, we'll try to execute via the REST API where possible
                
                # For now, we'll attempt to execute via the supabase.rpc if available
                try:
                    result = supabase.rpc('exec_sql', {'query': statement}).execute()
                    logger.debug(f"✅ Statement {i} executed successfully.")
                except Exception as rpc_error:
                    # If rpc doesn't exist, we need to guide user to dashboard
                    if "function public.exec_sql" in str(rpc_error).lower():
                        logger.warning("⚠️  RPC function 'exec_sql' not found in Supabase.")
                        logger.warning("📝 Please execute the SQL manually in Supabase Dashboard:")
                        logger.warning(f"   File: {migration_file}")
                        logger.warning("   Location: SQL Editor in Supabase Dashboard")
                        return False
                    else:
                        raise rpc_error
                
            except Exception as e:
                # Some statements might fail if tables already exist, which is okay
                error_str = str(e).lower()
                if "already exists" in error_str or "duplicate" in error_str:
                    logger.info(f"✓ Statement {i} skipped (already exists)")
                else:
                    logger.warning(f"⚠️  Statement {i} failed: {str(e)[:200]}...")
                    logger.warning("Continuing with remaining statements...")
        
        logger.info("✅ Summary table migration completed!")
        
        # Verify tables were created
        logger.info("🔍 Verifying tables...")
        try:
            # Try to query the tables to verify they exist
            cs_result = supabase.table('channel_summary').select('*').limit(1).execute()
            logger.info("✅ channel_summary table verified")
            
            ds_result = supabase.table('daily_summaries').select('*').limit(1).execute()
            logger.info("✅ daily_summaries table verified")
            
        except Exception as e:
            logger.error(f"❌ Table verification failed: {e}")
            logger.warning("Tables may not have been created successfully.")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to migrate summary tables: {e}", exc_info=True)
        return False


def print_manual_instructions():
    """Print manual migration instructions."""
    print("\n" + "="*80)
    print("📋 MANUAL MIGRATION REQUIRED")
    print("="*80)
    print("\nThe automatic migration requires an RPC function in Supabase.")
    print("Since this isn't set up, please follow these steps:\n")
    print("1. Open your Supabase Dashboard: https://app.supabase.com")
    print("2. Navigate to: SQL Editor")
    print("3. Click: New Query")
    print("4. Copy the contents of: scripts/migrate_summary_tables_to_supabase.sql")
    print("5. Paste and Run the SQL")
    print("\nAlternatively, run this command if you have psql installed:")
    print(f"   psql $DATABASE_URL < scripts/migrate_summary_tables_to_supabase.sql")
    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    print("\n🚀 Summary Tables Migration Script")
    print("="*80)
    
    success = asyncio.run(migrate_summary_tables())
    
    if success:
        print("\n🎉 Summary tables migrated successfully!")
        print("\n✅ Next steps:")
        print("   1. Test with: python main.py --dev --storage-backend supabase --summary-now")
        print("   2. Watch for logs: 'Storing summary to Supabase for channel X'\n")
    else:
        print("\n❌ Migration failed or requires manual steps.")
        print_manual_instructions()
        sys.exit(1)

