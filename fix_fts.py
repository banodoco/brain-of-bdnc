import sqlite3
import sys

def fix_fts_table(db_path):
    try:
        # Connect to the database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("Starting FTS table fix...")
        
        # Drop the existing FTS table
        print("Dropping existing FTS table...")
        cursor.execute("DROP TABLE IF EXISTS messages_fts;")
        
        # Create the FTS table with correct content_rowid
        print("Creating new FTS table with correct structure...")
        cursor.execute("""
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='message_id'
            );
        """)
        
        # Populate the FTS table
        print("Populating FTS table with message content...")
        cursor.execute("""
            INSERT INTO messages_fts(rowid, content)
            SELECT message_id, content
            FROM messages
            WHERE content IS NOT NULL AND content != '';
        """)
        
        # Get counts for verification
        cursor.execute("SELECT COUNT(*) FROM messages WHERE content IS NOT NULL AND content != '';")
        messages_with_content = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM messages_fts;")
        fts_count = cursor.fetchone()[0]
        
        print(f"\nVerification:")
        print(f"Messages with content: {messages_with_content}")
        print(f"FTS entries: {fts_count}")
        
        # Commit changes
        conn.commit()
        print("\nFTS table fix completed successfully!")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        print(f"Error: {e}")
        if conn:
            conn.rollback()

if __name__ == "__main__":
    db_path = "./production.db"
    fix_fts_table(db_path) 