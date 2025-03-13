import sqlite3
import sys

def check_messages_tables(db_path):
    try:
        # Connect to the database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("Checking messages table...")
        
        # Check messages table structure
        cursor.execute("PRAGMA table_info(messages);")
        columns = cursor.fetchall()
        print("\nMessages table columns:", [col[1] for col in columns])
        
        # Get messages count
        cursor.execute("SELECT COUNT(*) FROM messages;")
        messages_count = cursor.fetchone()[0]
        print(f"Messages count: {messages_count}")
        
        # Check FTS table structure
        print("\nChecking messages_fts table structure...")
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts';")
        fts_structure = cursor.fetchone()
        print(f"FTS table definition: {fts_structure[0] if fts_structure else 'Not found'}")
        
        # Try to get FTS count using direct rowid access
        print("\nChecking FTS entries...")
        try:
            cursor.execute("SELECT COUNT(*) FROM messages_fts;")
            fts_count = cursor.fetchone()[0]
            print(f"FTS entries count: {fts_count}")
            
            # Compare counts
            print(f"\nDifference between messages and FTS: {messages_count - fts_count}")
        except sqlite3.Error as e:
            print(f"Could not count FTS entries: {e}")
        
        # Check for messages with content but missing from FTS
        print("\nChecking for messages with content but missing from FTS...")
        cursor.execute("""
            SELECT COUNT(*) FROM messages m
            WHERE content IS NOT NULL 
            AND content != ''
            AND NOT EXISTS (
                SELECT 1 FROM messages_fts 
                WHERE messages_fts.rowid = m.message_id
            );
        """)
        missing_with_content = cursor.fetchone()[0]
        print(f"Messages with content but missing from FTS: {missing_with_content}")
        
        # Sample some problematic messages if they exist
        if missing_with_content > 0:
            print("\nSampling some messages with content but missing from FTS:")
            cursor.execute("""
                SELECT message_id, content 
                FROM messages 
                WHERE content IS NOT NULL 
                AND content != ''
                AND NOT EXISTS (
                    SELECT 1 FROM messages_fts 
                    WHERE messages_fts.rowid = message_id
                )
                LIMIT 3;
            """)
            samples = cursor.fetchall()
            for sample in samples:
                print(f"\nMessage ID: {sample[0]}")
                print(f"Content: {sample[1][:100]}...")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    db_path = "./production.db"
    check_messages_tables(db_path) 