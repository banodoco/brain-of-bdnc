import sqlite3
import json
from datetime import datetime

def compare_databases(original_db_path, fixed_db_path):
    print("Comparing original and fixed databases...")
    
    # Connect to both databases
    orig_conn = sqlite3.connect(original_db_path)
    fixed_conn = sqlite3.connect(fixed_db_path)
    orig_conn.row_factory = sqlite3.Row
    fixed_conn.row_factory = sqlite3.Row
    
    # Get detailed counts
    print("\nDetailed message counts:")
    
    # Count by channel
    print("\nMessages by channel:")
    for conn, db_name in [(orig_conn, "Original"), (fixed_conn, "Fixed")]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.channel_name, COUNT(*) as count
            FROM messages m
            LEFT JOIN channels c ON m.channel_id = c.channel_id
            GROUP BY m.channel_id, c.channel_name
            ORDER BY count DESC
        """)
        print(f"\n{db_name} database:")
        for row in cursor.fetchall():
            print(f"  {row[0] or 'Unknown channel'}: {row[1]} messages")
    
    # Count by date ranges
    print("\nMessages by date range:")
    for conn, db_name in [(orig_conn, "Original"), (fixed_conn, "Fixed")]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                MIN(created_at) as earliest,
                MAX(created_at) as latest,
                COUNT(*) as count
            FROM messages
        """)
        row = cursor.fetchone()
        print(f"\n{db_name} database:")
        print(f"  Earliest message: {row[0]}")
        print(f"  Latest message: {row[1]}")
        print(f"  Total messages: {row[2]}")
    
    # Check for duplicate message IDs
    print("\nChecking for duplicate message IDs:")
    for conn, db_name in [(orig_conn, "Original"), (fixed_conn, "Fixed")]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT message_id, COUNT(*) as count
            FROM messages
            GROUP BY message_id
            HAVING count > 1
            ORDER BY count DESC
        """)
        duplicates = cursor.fetchall()
        print(f"\n{db_name} database:")
        if duplicates:
            print(f"  Found {len(duplicates)} message IDs with duplicates:")
            for row in duplicates[:5]:  # Show first 5
                print(f"  Message ID {row[0]}: {row[1]} copies")
            if len(duplicates) > 5:
                print(f"  ... and {len(duplicates) - 5} more")
        else:
            print("  No duplicate message IDs found")
    
    # Close connections
    orig_conn.close()
    fixed_conn.close()

if __name__ == "__main__":
    compare_databases("./production.db", "./production_fixed.db") 