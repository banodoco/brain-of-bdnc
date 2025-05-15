import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = 'data/production.db' # Adjust if your script is run from a different relative location
USER_IDS = [855002018253897749, 256155058620727306]
HOURS_AGO = 6

def run_manual_query():
    print(f"Connecting to database: {DB_PATH}")
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row # To access columns by name
        cursor = conn.cursor()

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=HOURS_AGO)

        start_time_iso = start_time.isoformat()
        end_time_iso = end_time.isoformat()

        print(f"Fetching messages for authors {USER_IDS} between {start_time_iso} and {end_time_iso}")

        placeholders = ','.join('?' * len(USER_IDS))
        sql_query = f"""
            SELECT message_id, author_id, content, created_at
            FROM messages
            WHERE author_id IN ({placeholders})
            AND created_at >= ? AND created_at <= ?
            ORDER BY created_at ASC
        """
        
        params = list(USER_IDS) + [start_time_iso, end_time_iso]

        cursor.execute(sql_query, tuple(params))
        results = [dict(row) for row in cursor.fetchall()]

        if results:
            print(f"Found {len(results)} messages:")
            for i, msg in enumerate(results):
                print(f"  --- Message {i+1} ---")
                print(f"    Message ID: {msg['message_id']}")
                print(f"    Author ID:  {msg['author_id']}")
                print(f"    Created At: {msg['created_at']}")
                print(f"    Content:    \"{msg['content'][:200]}{'...' if len(msg['content']) > 200 else ''}\"")
        else:
            print("No messages found matching the criteria.")

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if conn:
            conn.close()
            print("Database connection closed.")

if __name__ == '__main__':
    run_manual_query() 