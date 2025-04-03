import os
import sys
import time
import argparse
# Add project root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging
from typing import List, Set, Dict
from src.common.constants import get_database_path
from src.common.schema import get_schema_tuples

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_table_columns(cursor, table_name: str) -> Dict[str, dict]:
    """Get current columns in the table with their full definitions."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = {}
    for row in cursor.fetchall():
        # row format: (cid, name, type, notnull, dflt_value, pk)
        columns[row[1]] = {
            'type': row[2],
            'notnull': row[3],
            'default': row[4],
            'primary_key': row[5]
        }
    return columns

def get_desired_messages_schema() -> List[tuple]:
    """Define the desired schema structure for the messages table."""
    return [
        ("message_id", "BIGINT PRIMARY KEY"),
        ("channel_id", "BIGINT"),
        ("author_id", "BIGINT"),
        ("content", "TEXT"),
        ("created_at", "TEXT"),
        ("attachments", "TEXT"),
        ("embeds", "TEXT"),
        ("reaction_count", "INTEGER DEFAULT 0"),
        ("reactors", "TEXT"),
        ("reference_id", "BIGINT"),
        ("edited_at", "TEXT"),
        ("is_pinned", "BOOLEAN"),
        ("thread_id", "BIGINT"),
        ("message_type", "TEXT"),
        ("flags", "INTEGER"),
        ("is_deleted", "BOOLEAN DEFAULT FALSE"),
        ("indexed_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    ]

def get_desired_members_schema() -> List[tuple]:
    """Get the desired schema structure for the members table."""
    return [
        ("member_id", "BIGINT PRIMARY KEY"),
        ("username", "TEXT NOT NULL"),
        ("global_name", "TEXT"),
        ("server_nick", "TEXT"),
        ("avatar_url", "TEXT"),
        ("discriminator", "TEXT"),
        ("bot", "BOOLEAN DEFAULT FALSE"),
        ("system", "BOOLEAN DEFAULT FALSE"),
        ("accent_color", "INTEGER"),
        ("banner_url", "TEXT"),
        ("discord_created_at", "TEXT"),
        ("guild_join_date", "TEXT"),
        ("role_ids", "TEXT"),  # JSON array of role IDs
        # New columns for sharing feature:
        ("twitter_handle", "TEXT"),
        ("instagram_handle", "TEXT"),
        ("youtube_handle", "TEXT"),
        ("tiktok_handle", "TEXT"),
        ("website", "TEXT"),
        ("sharing_consent", "BOOLEAN DEFAULT FALSE"),
        ("dm_preference", "BOOLEAN DEFAULT TRUE"),
        # Existing audit columns:
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        # Removed: ("notifications", "TEXT DEFAULT '[]'") - Assuming not needed based on db_handler
    ]

def get_desired_daily_summaries_schema() -> List[tuple]:
    """Get the desired schema structure for the daily_summaries table."""
    return [
        ("daily_summary_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("channel_id", "BIGINT NOT NULL REFERENCES channels(channel_id)"),
        ("full_summary", "TEXT"),
        ("short_summary", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    ]

def create_table_if_not_exists(cursor, table_name: str, schema_func):
    """Generic function to create a table based on a schema function if it doesn't exist."""
    if not table_exists(cursor, table_name):
        logger.info(f"Table '{table_name}' does not exist. Creating table.")
        schema = schema_func()
        columns_def = ", ".join([f"{name} {type_}" for name, type_ in schema])
        # Add constraints if needed, e.g., UNIQUE for daily_summaries
        constraints = ""
        if table_name == "daily_summaries":
            constraints = ", UNIQUE(date, channel_id) ON CONFLICT REPLACE, FOREIGN KEY (channel_id) REFERENCES channels(channel_id)"
        create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_def}{constraints})"
        cursor.execute(create_sql)
        logger.info(f"Table '{table_name}' created successfully.")
    else:
        logger.info(f"Table '{table_name}' already exists.")

def table_exists(cursor, table_name: str) -> bool:
    """Check if a table exists in the database."""
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone() is not None

def backup_table(cursor, table_name: str):
    """Create a backup of the table before migration."""
    backup_name = f"{table_name}_backup_{int(time.time())}"
    cursor.execute(f"""
        CREATE TABLE {backup_name} AS 
        SELECT * FROM {table_name}
    """)
    return backup_name

def validate_migration(cursor, original_count: int):
    """Validate that no data was lost during migration."""
    cursor.execute("SELECT COUNT(*) FROM messages")
    new_count = cursor.fetchone()[0]
    if new_count != original_count:
        raise ValueError(
            f"Data loss detected! Original row count: {original_count}, "
            f"New row count: {new_count}"
        )

def create_temp_table_and_migrate_data(cursor, desired_schema: List[tuple], existing_columns: Dict[str, dict]):
    """Create a temporary table with the desired schema and migrate the data."""
    # Get original row count
    cursor.execute("SELECT COUNT(*) FROM messages")
    original_count = cursor.fetchone()[0]
    
    # Create backup
    backup_name = backup_table(cursor, "messages")
    logger.info(f"Created backup table: {backup_name}")
    
    try:
        # Create column definitions string
        columns_def = ", ".join([f"{name} {type_}" for name, type_ in desired_schema])
        
        # Create new table and copy data
        cursor.execute(f"""
            CREATE TABLE messages_new (
                {columns_def}
            )
        """)
        
        # Copy data, using message_id since we've already migrated from id
        cursor.execute("""
            INSERT INTO messages_new 
            SELECT message_id, channel_id, author_id,
                   content, created_at, attachments, embeds, reaction_count,
                   reactors, reference_id, edited_at, is_pinned, thread_id,
                   message_type, flags, 0, indexed_at
            FROM messages
        """)
        
        # Validate before dropping old table
        cursor.execute("SELECT COUNT(*) FROM messages_new")
        if cursor.fetchone()[0] != original_count:
            raise ValueError("Row count mismatch before table swap")
        
        # Drop old table and rename new one
        cursor.execute("DROP TABLE messages")
        cursor.execute("ALTER TABLE messages_new RENAME TO messages")
        
        # Final validation
        validate_migration(cursor, original_count)
        
    except Exception as e:
        # If anything goes wrong, we can restore from backup
        logger.error(f"Migration failed: {e}")
        cursor.execute("DROP TABLE IF EXISTS messages")
        cursor.execute(f"ALTER TABLE {backup_name} RENAME TO messages")
        raise

def _needs_migration(existing_columns: Dict[str, dict], desired_schema: List[tuple]) -> bool:
    """Checks if a table schema needs migration (missing/extra columns, type changes, PK change)."""
    desired_column_names = {name for name, _ in desired_schema}
    existing_column_names = set(existing_columns.keys())

    missing_columns = desired_column_names - existing_column_names
    extra_columns = existing_column_names - desired_column_names

    # Check for type changes (simple comparison, might need refinement for complex types)
    type_changes = False
    for name, type_ in desired_schema:
        if name in existing_columns and existing_columns[name]['type'].upper() != type_.split()[0].upper():
             # Basic type check (e.g., 'BIGINT PRIMARY KEY' vs 'BIGINT')
             logger.debug(f"Type mismatch for {name}: DB has {existing_columns[name]['type']}, desired {type_}")
             type_changes = True
             break

    # Check if the primary key is correct (assuming single PK column defined in schema)
    desired_pk = [name for name, type_ in desired_schema if "PRIMARY KEY" in type_.upper()]
    actual_pk = [name for name, col in existing_columns.items() if col['primary_key'] == 1]
    primary_key_wrong = (desired_pk and actual_pk != desired_pk)

    if missing_columns:
        logger.info(f"Migration needed: Missing columns {missing_columns}")
        return True
    if extra_columns:
        logger.info(f"Migration needed: Extra columns {extra_columns}")
        return True
    if type_changes:
        logger.info("Migration needed: Column type changes detected")
        return True
    if primary_key_wrong:
        logger.info(f"Migration needed: Primary key mismatch (expected {desired_pk}, got {actual_pk})")
        return True

    return False

def migrate_generic_table(cursor, table_name: str, schema_func):
    """Migrates a table using the standard backup, create new, copy, swap method."""
    logger.info(f"Starting migration check for table '{table_name}'...")
    
    if not table_exists(cursor, table_name):
         logger.warning(f"Table '{table_name}' does not exist. Cannot migrate. Creating instead.")
         create_table_if_not_exists(cursor, table_name, schema_func)
         return

    existing_columns = get_table_columns(cursor, table_name)
    desired_schema = schema_func()

    if not _needs_migration(existing_columns, desired_schema):
        logger.info(f"Table '{table_name}' schema is up to date. No migration needed.")
        return

    logger.info(f"Migration required for table '{table_name}'.")
    backup_name = backup_table(cursor, table_name)
    logger.info(f"Created backup table: {backup_name}")

    try:
        # Get original row count for validation
        cursor.execute(f"SELECT COUNT(*) FROM \"{backup_name}\"") # Count from backup
        original_count = cursor.fetchone()[0]

        # Create new table with the desired schema
        new_table_name = f"{table_name}_new"
        create_table_if_not_exists(cursor, new_table_name, schema_func)

        # Prepare column lists for INSERT INTO SELECT
        desired_cols_ordered = [f'\"{name}\"' for name, _ in desired_schema]
        # Select only columns that exist in the *old* table (the backup)
        # And provide defaults for new columns
        select_cols_ordered = []
        backup_columns = get_table_columns(cursor, backup_name) # Get columns from backup
        
        for name, type_def in desired_schema:
            if name in backup_columns:
                select_cols_ordered.append(f'\"{name}\"') # Select existing column
            else:
                # Provide default value for new column based on type/definition
                default_value = "NULL" # Default NULL
                if "DEFAULT" in type_def.upper():
                    parts = type_def.upper().split("DEFAULT")
                    default_value = parts[1].strip()
                    # Handle specific types if needed (e.g., boolean 0/1)
                    if "BOOLEAN" in parts[0] and default_value == "FALSE":
                        default_value = "0"
                    elif "BOOLEAN" in parts[0] and default_value == "TRUE":
                        default_value = "1"
                elif "BOOLEAN" in type_def.upper(): # Default for boolean if not specified
                     default_value = "0" # Default to False

                select_cols_ordered.append(default_value)
                logger.debug(f"Providing default '{default_value}' for new column '{name}'")

        # Copy data from backup to new table
        insert_sql = f"""
            INSERT INTO \"{new_table_name}\" ({', '.join(desired_cols_ordered)})
            SELECT {', '.join(select_cols_ordered)}
            FROM \"{backup_name}\"
        """
        logger.debug(f"Running INSERT SQL: {insert_sql}")
        cursor.execute(insert_sql)

        # Validate row count before dropping old table
        cursor.execute(f"SELECT COUNT(*) FROM \"{new_table_name}\"")
        new_count = cursor.fetchone()[0]
        if new_count != original_count:
            raise ValueError(f"Row count mismatch after copy ({new_count} vs {original_count}). Migration aborted.")

        # Drop the original table
        cursor.execute(f"DROP TABLE \"{table_name}\"")
        # Rename the new table to the original name
        cursor.execute(f"ALTER TABLE \"{new_table_name}\" RENAME TO \"{table_name}\"")

        # Recreate indexes if necessary (example for messages)
        if table_name == "messages":
            logger.info("Recreating indexes for messages table...")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_id ON messages(channel_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON messages(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_author_id ON messages(author_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_reference_id ON messages(reference_id)")
        elif table_name == "members":
             logger.info("Recreating indexes for members table...")
             cursor.execute("CREATE INDEX IF NOT EXISTS idx_members_username ON members(username)")
        elif table_name == "daily_summaries":
            logger.info("Recreating indexes for daily_summaries table...")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_channel ON daily_summaries(channel_id)")

        logger.info(f"Table '{table_name}' migrated successfully.")

    except Exception as e:
        logger.error(f"Migration failed for table '{table_name}': {e}", exc_info=True)
        # Attempt to restore from backup
        logger.warning(f"Attempting to restore '{table_name}' from backup '{backup_name}'...")
        try:
            cursor.execute(f"DROP TABLE IF EXISTS \"{table_name}\"")
            cursor.execute(f"DROP TABLE IF EXISTS \"{table_name}_new\"") # Drop temp table if exists
            cursor.execute(f"ALTER TABLE \"{backup_name}\" RENAME TO \"{table_name}\"")
            logger.info(f"Successfully restored '{table_name}' from {backup_name}.")
        except Exception as restore_e:
            logger.critical(f"Failed to restore table '{table_name}' from backup! DB might be in inconsistent state. Backup table: {backup_name}. Error: {restore_e}", exc_info=True)
        raise # Re-raise original exception

def migrate_messages_table(cursor):
    migrate_generic_table(cursor, "messages", get_desired_messages_schema)

def migrate_members_table(cursor):
    migrate_generic_table(cursor, "members", get_desired_members_schema)

def migrate_daily_summaries_table(cursor):
    migrate_generic_table(cursor, "daily_summaries", get_desired_daily_summaries_schema)

def migrate_remove_raw_messages(cursor):
    """Remove raw_messages column from daily_summaries table."""
    logger.info("Starting raw_messages removal migration")
    
    try:
        # First check if raw_messages column exists
        cursor.execute("PRAGMA table_info(daily_summaries)")
        columns = {col[1]: col for col in cursor.fetchall()}
        
        if 'raw_messages' not in columns:
            logger.info("raw_messages column already removed")
            return
            
        # Get original row count
        cursor.execute("SELECT COUNT(*) FROM daily_summaries")
        original_count = cursor.fetchone()[0]
        
        # Create backup
        backup_name = backup_table(cursor, "daily_summaries")
        logger.info(f"Created backup table: {backup_name}")
        
        try:
            # Create new table without raw_messages
            cursor.execute("""
                CREATE TABLE daily_summaries_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    full_summary TEXT,
                    short_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, channel_id) ON CONFLICT REPLACE,
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
                )
            """)
            
            # Copy data to new table
            cursor.execute("""
                INSERT INTO daily_summaries_new 
                (id, date, channel_id, 
                 full_summary, short_summary, created_at)
                SELECT 
                    id, date, channel_id,
                    full_summary, short_summary, created_at
                FROM daily_summaries
            """)
            
            # Validate before dropping old table
            cursor.execute("SELECT COUNT(*) FROM daily_summaries_new")
            if cursor.fetchone()[0] != original_count:
                raise ValueError("Row count mismatch before table swap")
            
            # Drop old table and rename new one
            cursor.execute("DROP TABLE daily_summaries")
            cursor.execute("ALTER TABLE daily_summaries_new RENAME TO daily_summaries")
            
            logger.info("Successfully removed raw_messages column")
            
        except Exception as e:
            # If anything goes wrong, we can restore from backup
            logger.error(f"Migration failed: {e}")
            cursor.execute("DROP TABLE IF EXISTS daily_summaries")
            cursor.execute(f"ALTER TABLE {backup_name} RENAME TO daily_summaries")
            raise
            
    except Exception as e:
        logger.error(f"Error during raw_messages removal migration: {e}")
        raise

def cleanup_backup_tables(cursor):
    """Clean up any backup tables from previous migrations."""
    try:
        # Get list of all tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_backup_%'")
        backup_tables = cursor.fetchall()
        
        for (table_name,) in backup_tables:
            logger.info(f"Dropping backup table: {table_name}")
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
            
    except Exception as e:
        logger.error(f"Error cleaning up backup tables: {e}")
        raise

def migrate_database(dev_mode: bool = False):
    conn = None
    try:
        # Get appropriate database path
        db_path = get_database_path(dev_mode)
        logger.info(f"Using database at: {db_path}")
        
        # Connect to the database
        conn = sqlite3.connect(db_path)
        conn.execute("BEGIN TRANSACTION")  # Explicit transaction
        cursor = conn.cursor()

        # Ensure tables exist before attempting migrations that depend on them (like FKs)
        create_table_if_not_exists(cursor, "channels", lambda: [("channel_id", "BIGINT PRIMARY KEY"), ("channel_name", "TEXT NOT NULL")]) # Minimal channels schema if needed

        # Run migrations using the generic handler
        migrate_members_table(cursor)
        migrate_messages_table(cursor)
        migrate_daily_summaries_table(cursor)
        migrate_remove_raw_messages(cursor)

        # Add any other specific migration steps if necessary
        # Example: Add FTS table creation/migration if needed
        logger.info("Checking/Creating FTS table for messages...")
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='message_id',
                tokenize = 'porter unicode61'
            )
        """)
        # Optional: Rebuild FTS index if schema changed significantly
        # cursor.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        logger.info("FTS table check/creation complete.")

        # Clean up backup tables
        cleanup_backup_tables(cursor)
        
        # Commit all changes
        conn.commit()
        logger.info("Migration completed successfully")
        
    except Exception as e:
        logger.error(f"Unexpected error during migration: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

def main():
    parser = argparse.ArgumentParser(description='Migrate database schema')
    parser.add_argument('--dev', action='store_true', help='Use development database')
    args = parser.parse_args()

    try:
        # Process development database
        if args.dev:
            logger.info("Starting database migration")
            logger.info(f"Processing development database at: {get_database_path(True)}")
            migrate_database(dev_mode=True)
            logger.info("Migration completed successfully for development database")
        else:
            # Process both databases
            logger.info("Starting database migration")
            
            # Process development database
            logger.info(f"Processing development database at: {get_database_path(True)}")
            migrate_database(dev_mode=True)
            logger.info("Migration completed successfully for development database")
            
            # Process production database
            logger.info(f"Processing production database at: {get_database_path(False)}")
            migrate_database(dev_mode=False)
            logger.info("Migration completed successfully for production database")
            
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise

if __name__ == "__main__":
    main() 
    main() 