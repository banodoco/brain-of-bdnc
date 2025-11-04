#!/usr/bin/env python3
"""
Automated migration script from SQLite to Supabase.
Handles all steps: verification, sync, and validation.
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime
import time

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SupabaseMigration:
    """Handles the complete migration from SQLite to Supabase."""
    
    def __init__(self, check_only=False, batch_size=1000):
        self.check_only = check_only
        self.batch_size = batch_size
        self.errors = []
        self.warnings = []
        
    def print_header(self, text):
        """Print a formatted header."""
        logger.info("=" * 80)
        logger.info(f"  {text}")
        logger.info("=" * 80)
    
    def print_step(self, step_num, total_steps, description):
        """Print a step header."""
        logger.info(f"\n{'='*80}")
        logger.info(f"STEP {step_num}/{total_steps}: {description}")
        logger.info(f"{'='*80}\n")
    
    def check_environment(self):
        """Check that all required environment variables are set."""
        self.print_step(1, 6, "Checking Environment Variables")
        
        required_vars = {
            'SUPABASE_URL': os.getenv('SUPABASE_URL'),
            'SUPABASE_SERVICE_KEY': os.getenv('SUPABASE_SERVICE_KEY'),
            'DISCORD_BOT_TOKEN': os.getenv('DISCORD_BOT_TOKEN'),
            'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY'),
        }
        
        optional_vars = {
            'SUPABASE_DB_PASSWORD': os.getenv('SUPABASE_DB_PASSWORD'),
        }
        
        all_good = True
        
        # Check required
        for var_name, var_value in required_vars.items():
            if var_value:
                logger.info(f"✅ {var_name}: Set ({len(var_value)} characters)")
            else:
                logger.error(f"❌ {var_name}: NOT SET")
                self.errors.append(f"Missing required environment variable: {var_name}")
                all_good = False
        
        # Check optional
        for var_name, var_value in optional_vars.items():
            if var_value:
                logger.info(f"✅ {var_name}: Set (optional)")
            else:
                logger.warning(f"⚠️  {var_name}: Not set (optional, but recommended)")
                self.warnings.append(f"Optional variable not set: {var_name}")
        
        # Check storage backend
        storage_backend = os.getenv('STORAGE_BACKEND', 'sqlite')
        logger.info(f"\n📦 Current STORAGE_BACKEND: {storage_backend}")
        
        if storage_backend == 'sqlite':
            logger.warning("⚠️  STORAGE_BACKEND is set to 'sqlite'")
            logger.warning("   For migration, set to 'both' to write to SQLite and Supabase")
            self.warnings.append("STORAGE_BACKEND should be 'both' for migration")
        elif storage_backend == 'both':
            logger.info("✅ STORAGE_BACKEND is 'both' - perfect for migration!")
        elif storage_backend == 'supabase':
            logger.info("✅ STORAGE_BACKEND is 'supabase' - already migrated!")
        
        return all_good
    
    def check_supabase_connection(self):
        """Test connection to Supabase."""
        self.print_step(2, 6, "Testing Supabase Connection")
        
        try:
            from supabase import create_client
            
            url = os.getenv('SUPABASE_URL')
            key = os.getenv('SUPABASE_SERVICE_KEY')
            
            logger.info(f"Connecting to: {url}")
            client = create_client(url, key)
            
            # Test query
            result = client.table('sync_status').select('*').limit(1).execute()
            logger.info("✅ Successfully connected to Supabase!")
            logger.info(f"✅ Tables are accessible")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Supabase: {e}")
            self.errors.append(f"Supabase connection failed: {e}")
            return False
    
    def check_supabase_tables(self):
        """Verify that all required tables exist in Supabase."""
        self.print_step(3, 6, "Verifying Supabase Tables")
        
        try:
            from supabase import create_client
            
            url = os.getenv('SUPABASE_URL')
            key = os.getenv('SUPABASE_SERVICE_KEY')
            client = create_client(url, key)
            
            required_tables = [
                'discord_messages',
                'discord_members',
                'discord_channels',
                'sync_status'
            ]
            
            all_exist = True
            for table_name in required_tables:
                try:
                    result = client.table(table_name).select('*').limit(1).execute()
                    logger.info(f"✅ Table '{table_name}' exists")
                except Exception as e:
                    logger.error(f"❌ Table '{table_name}' not found or inaccessible")
                    self.errors.append(f"Table '{table_name}' missing or inaccessible")
                    all_exist = False
            
            if all_exist:
                logger.info("\n✅ All required tables exist!")
            else:
                logger.error("\n❌ Some tables are missing")
                logger.error("   Run the SQL script: scripts/create_supabase_schema.sql")
            
            return all_exist
            
        except Exception as e:
            logger.error(f"❌ Error checking tables: {e}")
            self.errors.append(f"Error checking tables: {e}")
            return False
    
    def check_sqlite_data(self):
        """Check SQLite database and count records."""
        self.print_step(4, 6, "Analyzing SQLite Database")
        
        try:
            from src.common.db_handler import DatabaseHandler
            
            db_handler = DatabaseHandler(dev_mode=False, storage_backend='sqlite')
            
            # Count records in each table
            def count_table(table_name):
                def _count(conn):
                    cursor = conn.cursor()
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                    count = cursor.fetchone()[0]
                    cursor.close()
                    return count
                return db_handler._execute_with_retry(_count)
            
            messages_count = count_table('messages')
            members_count = count_table('members')
            channels_count = count_table('channels')
            
            logger.info(f"📊 SQLite Database Statistics:")
            logger.info(f"   Messages: {messages_count:,}")
            logger.info(f"   Members:  {members_count:,}")
            logger.info(f"   Channels: {channels_count:,}")
            
            total = messages_count + members_count + channels_count
            logger.info(f"   Total:    {total:,} records")
            
            if total == 0:
                logger.warning("⚠️  SQLite database is empty - nothing to migrate")
                self.warnings.append("SQLite database is empty")
            else:
                logger.info(f"\n✅ Found {total:,} records to migrate")
            
            return {
                'messages': messages_count,
                'members': members_count,
                'channels': channels_count,
                'total': total
            }
            
        except Exception as e:
            logger.error(f"❌ Error analyzing SQLite database: {e}")
            self.errors.append(f"SQLite analysis failed: {e}")
            return None
    
    async def sync_data(self, counts):
        """Sync all data from SQLite to Supabase."""
        self.print_step(5, 6, "Syncing Data to Supabase")
        
        if not counts or counts['total'] == 0:
            logger.info("No data to sync - skipping")
            return True
        
        try:
            from src.common.db_handler import DatabaseHandler
            from src.common.storage_handler import StorageHandler
            
            # Initialize handlers
            db_handler = DatabaseHandler(dev_mode=False, storage_backend='sqlite')
            storage_handler = StorageHandler('supabase')
            
            total_synced = 0
            total_records = counts['total']
            start_time = time.time()
            
            # Sync messages
            if counts['messages'] > 0:
                logger.info(f"\n📨 Syncing {counts['messages']:,} messages...")
                offset = 0
                
                while offset < counts['messages']:
                    def fetch_batch(conn):
                        import sqlite3
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT * FROM messages 
                            ORDER BY message_id 
                            LIMIT ? OFFSET ?
                        """, (self.batch_size, offset))
                        results = [dict(row) for row in cursor.fetchall()]
                        cursor.close()
                        return results
                    
                    batch = db_handler._execute_with_retry(fetch_batch)
                    if not batch:
                        break
                    
                    try:
                        stored = await storage_handler.store_messages_to_supabase(batch)
                        total_synced += stored
                        progress = (total_synced / total_records) * 100
                        logger.info(f"   Progress: {total_synced:,}/{total_records:,} ({progress:.1f}%)")
                    except Exception as e:
                        logger.error(f"   ❌ Error syncing batch: {e}")
                    
                    offset += self.batch_size
            
            # Sync members
            if counts['members'] > 0:
                logger.info(f"\n👥 Syncing {counts['members']:,} members...")
                
                def fetch_members(conn):
                    import sqlite3
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM members")
                    results = [dict(row) for row in cursor.fetchall()]
                    cursor.close()
                    return results
                
                members = db_handler._execute_with_retry(fetch_members)
                
                try:
                    stored = await storage_handler.store_members_to_supabase(members)
                    total_synced += stored
                    logger.info(f"   ✅ Synced {stored} members")
                except Exception as e:
                    logger.error(f"   ❌ Error syncing members: {e}")
            
            # Sync channels
            if counts['channels'] > 0:
                logger.info(f"\n📺 Syncing {counts['channels']:,} channels...")
                
                def fetch_channels(conn):
                    import sqlite3
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM channels")
                    results = [dict(row) for row in cursor.fetchall()]
                    cursor.close()
                    return results
                
                channels = db_handler._execute_with_retry(fetch_channels)
                
                try:
                    stored = await storage_handler.store_channels_to_supabase(channels)
                    total_synced += stored
                    logger.info(f"   ✅ Synced {stored} channels")
                except Exception as e:
                    logger.error(f"   ❌ Error syncing channels: {e}")
            
            elapsed = time.time() - start_time
            logger.info(f"\n✅ Sync complete!")
            logger.info(f"   Total synced: {total_synced:,}/{total_records:,} records")
            logger.info(f"   Time elapsed: {elapsed:.1f} seconds")
            logger.info(f"   Rate: {total_synced/elapsed:.0f} records/second")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Sync failed: {e}")
            self.errors.append(f"Sync failed: {e}")
            return False
    
    def verify_migration(self, original_counts):
        """Verify that data was migrated correctly."""
        self.print_step(6, 6, "Verifying Migration")
        
        try:
            from supabase import create_client
            
            url = os.getenv('SUPABASE_URL')
            key = os.getenv('SUPABASE_SERVICE_KEY')
            client = create_client(url, key)
            
            # Count records in Supabase
            logger.info("Counting records in Supabase...")
            
            messages_result = client.table('discord_messages').select('*', count='exact').limit(1).execute()
            members_result = client.table('discord_members').select('*', count='exact').limit(1).execute()
            channels_result = client.table('discord_channels').select('*', count='exact').limit(1).execute()
            
            supabase_counts = {
                'messages': messages_result.count,
                'members': members_result.count,
                'channels': channels_result.count,
            }
            
            logger.info(f"\n📊 Comparison:")
            logger.info(f"   {'Table':<15} {'SQLite':<15} {'Supabase':<15} {'Status'}")
            logger.info(f"   {'-'*60}")
            
            all_match = True
            for table in ['messages', 'members', 'channels']:
                sqlite_count = original_counts.get(table, 0)
                supabase_count = supabase_counts.get(table, 0)
                
                if sqlite_count == supabase_count:
                    status = "✅ Match"
                elif supabase_count >= sqlite_count:
                    status = "✅ OK (more in Supabase)"
                else:
                    status = f"⚠️ Mismatch"
                    all_match = False
                
                logger.info(f"   {table:<15} {sqlite_count:<15,} {supabase_count:<15,} {status}")
            
            if all_match:
                logger.info(f"\n✅ Verification passed! All data migrated successfully.")
            else:
                logger.warning(f"\n⚠️  Some counts don't match - review the sync log")
            
            return all_match
            
        except Exception as e:
            logger.error(f"❌ Verification failed: {e}")
            self.errors.append(f"Verification failed: {e}")
            return False
    
    def print_summary(self):
        """Print migration summary."""
        self.print_header("MIGRATION SUMMARY")
        
        if self.errors:
            logger.error(f"\n❌ {len(self.errors)} ERRORS:")
            for i, error in enumerate(self.errors, 1):
                logger.error(f"   {i}. {error}")
        
        if self.warnings:
            logger.warning(f"\n⚠️  {len(self.warnings)} WARNINGS:")
            for i, warning in enumerate(self.warnings, 1):
                logger.warning(f"   {i}. {warning}")
        
        if not self.errors and not self.warnings:
            logger.info("\n✅ Perfect! No errors or warnings.")
        
        logger.info("\n" + "=" * 80)
    
    def print_next_steps(self):
        """Print next steps based on migration status."""
        self.print_header("NEXT STEPS")
        
        if self.errors:
            logger.info("\n⚠️  Migration encountered errors. Please:")
            logger.info("   1. Review the errors above")
            logger.info("   2. Fix any configuration issues")
            logger.info("   3. Run this script again")
        elif self.check_only:
            logger.info("\n✅ Pre-flight check passed! You're ready to migrate.")
            logger.info("\nTo start the migration, run:")
            logger.info("   python scripts/migrate_to_supabase.py")
        else:
            logger.info("\n✅ Migration complete! Final steps:")
            logger.info("\n1. Update STORAGE_BACKEND to 'supabase':")
            logger.info("   - In .env file: STORAGE_BACKEND=supabase")
            logger.info("   - Or Railway Variables: STORAGE_BACKEND=supabase")
            logger.info("\n2. Restart your bot:")
            logger.info("   - Local: python main.py")
            logger.info("   - Railway: Will auto-restart after variable change")
            logger.info("\n3. Verify bot functionality:")
            logger.info("   - Check bot is online in Discord")
            logger.info("   - Send test messages")
            logger.info("   - Monitor logs for errors")
            logger.info("\n4. Monitor for 24-48 hours before removing SQLite")
        
        logger.info("\n" + "=" * 80 + "\n")
    
    async def run(self):
        """Run the complete migration process."""
        self.print_header("SUPABASE MIGRATION TOOL")
        
        if self.check_only:
            logger.info("Running in CHECK-ONLY mode (no data will be migrated)\n")
        else:
            logger.info("Running FULL MIGRATION (will sync all data)\n")
        
        # Step 1: Check environment
        if not self.check_environment():
            logger.error("\n❌ Environment check failed - cannot proceed")
            self.print_summary()
            return False
        
        # Step 2: Test Supabase connection
        if not self.check_supabase_connection():
            logger.error("\n❌ Cannot connect to Supabase - cannot proceed")
            self.print_summary()
            return False
        
        # Step 3: Verify tables
        if not self.check_supabase_tables():
            logger.error("\n❌ Supabase tables not set up - cannot proceed")
            self.print_summary()
            return False
        
        # Step 4: Analyze SQLite
        counts = self.check_sqlite_data()
        if counts is None:
            logger.error("\n❌ Cannot access SQLite database - cannot proceed")
            self.print_summary()
            return False
        
        if self.check_only:
            logger.info("\n✅ Pre-flight check complete!")
            self.print_summary()
            self.print_next_steps()
            return True
        
        # Step 5: Sync data
        if not await self.sync_data(counts):
            logger.error("\n❌ Data sync failed")
            self.print_summary()
            return False
        
        # Step 6: Verify
        self.verify_migration(counts)
        
        # Done!
        self.print_summary()
        self.print_next_steps()
        
        return len(self.errors) == 0


async def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Migrate Discord bot from SQLite to Supabase',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/migrate_to_supabase.py --check-only    # Pre-flight check only
  python scripts/migrate_to_supabase.py                 # Full migration
  python scripts/migrate_to_supabase.py --batch-size 500  # Custom batch size
        """
    )
    
    parser.add_argument(
        '--check-only',
        action='store_true',
        help='Only check configuration, don\'t migrate data'
    )
    
    parser.add_argument(
        '--batch-size',
        type=int,
        default=1000,
        help='Number of records to sync per batch (default: 1000)'
    )
    
    args = parser.parse_args()
    
    migration = SupabaseMigration(
        check_only=args.check_only,
        batch_size=args.batch_size
    )
    
    success = await migration.run()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())

