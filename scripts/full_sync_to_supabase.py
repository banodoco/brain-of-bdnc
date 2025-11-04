#!/usr/bin/env python3
"""
Full historical sync from SQLite to Supabase.
Syncs ALL messages regardless of timestamps. Use for initial data migration.
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from src.common.db_handler import DatabaseHandler
from src.common.storage_handler import StorageHandler
from src.common.constants import STORAGE_BOTH
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def full_sync_messages(batch_size=1000):
    """Sync all messages from SQLite to Supabase."""
    logger.info("Starting full historical sync...")
    
    # Initialize handlers
    db_handler = DatabaseHandler(dev_mode=False, storage_backend='sqlite')  # Read from SQLite only
    storage_handler = StorageHandler('supabase')  # Write to Supabase
    
    # Get total count
    logger.info("Counting messages in SQLite...")
    def count_messages(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        count = cursor.fetchone()[0]
        cursor.close()
        return count
    
    total_messages = db_handler._execute_with_retry(count_messages)
    logger.info(f"Found {total_messages} total messages to sync")
    
    # Sync in batches
    synced_count = 0
    offset = 0
    
    while offset < total_messages:
        logger.info(f"Syncing batch {offset//batch_size + 1} (offset={offset}, batch_size={batch_size})")
        
        # Fetch batch from SQLite
        def fetch_batch(conn):
            import sqlite3
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM messages 
                ORDER BY message_id 
                LIMIT ? OFFSET ?
            """, (batch_size, offset))
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        
        batch = db_handler._execute_with_retry(fetch_batch)
        
        if not batch:
            break
        
        # Store to Supabase
        try:
            stored = await storage_handler.store_messages_to_supabase(batch)
            synced_count += stored
            logger.info(f"✅ Synced {stored}/{len(batch)} messages (total: {synced_count}/{total_messages})")
        except Exception as e:
            logger.error(f"❌ Error syncing batch: {e}")
            # Continue with next batch
        
        offset += batch_size
    
    logger.info(f"🎉 Full sync complete! Synced {synced_count}/{total_messages} messages")
    return synced_count

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Full historical sync to Supabase')
    parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for syncing')
    args = parser.parse_args()
    
    synced = asyncio.run(full_sync_messages(batch_size=args.batch_size))
    print(f"\n✅ Successfully synced {synced} messages to Supabase")

