import sqlite3
from datetime import datetime, timedelta
import json

def test_art_query(db_path, art_channel_id):
    try:
        # Connect to the database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get yesterday's date
        yesterday = datetime.utcnow() - timedelta(hours=24)
        
        print(f"Testing art query for channel {art_channel_id} since {yesterday.isoformat()}")
        
        # First check if the channel exists
        cursor.execute("SELECT * FROM channels WHERE channel_id = ?", (art_channel_id,))
        channel = cursor.fetchone()
        if not channel:
            print(f"\nWarning: Channel {art_channel_id} not found in channels table")
        else:
            print(f"\nChannel found: {dict(channel)}")
        
        # Check how many messages exist for this channel in the last 24h
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM messages
            WHERE channel_id = ?
            AND created_at > ?
        """, (art_channel_id, yesterday.isoformat()))
        message_count = cursor.fetchone()['count']
        print(f"\nTotal messages in last 24h: {message_count}")
        
        # Check how many of those have attachments
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM messages
            WHERE channel_id = ?
            AND created_at > ?
            AND json_valid(attachments)
            AND attachments != '[]'
        """, (art_channel_id, yesterday.isoformat()))
        attachment_count = cursor.fetchone()['count']
        print(f"Messages with attachments: {attachment_count}")
        
        # Now run the actual query
        query = """
            SELECT 
                m.message_id,
                m.content,
                m.attachments,
                m.jump_url,
                m.author_id,
                m.reactors,
                m.embeds,
                CASE 
                    WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                    ELSE json_array_length(m.reactors)
                END as unique_reactor_count
            FROM messages m
            WHERE m.channel_id = ?
            AND m.created_at > ?
            AND json_valid(m.attachments)
            AND m.attachments != '[]'
            ORDER BY unique_reactor_count DESC
            LIMIT 5
        """
        
        print("\nExecuting main query...")
        cursor.execute(query, (art_channel_id, yesterday.isoformat()))
        results = cursor.fetchall()
        
        if not results:
            print("No results found!")
        else:
            print(f"\nFound {len(results)} results. Top posts:")
            for idx, row in enumerate(results, 1):
                row_dict = dict(row)
                attachments = json.loads(row_dict['attachments'])
                reactors = json.loads(row_dict['reactors']) if row_dict['reactors'] else []
                
                print(f"\n{idx}. Message ID: {row_dict['message_id']}")
                print(f"Content: {row_dict['content'][:100]}..." if row_dict['content'] else "No content")
                print(f"Attachment count: {len(attachments)}")
                print(f"Reactor count: {len(reactors)}")
                print(f"Jump URL: {row_dict['jump_url']}")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        print(traceback.format_exc())

if __name__ == "__main__":
    db_path = "./production_fixed.db"
    art_channel_id = 1138865343314530324  # From the logs
    test_art_query(db_path, art_channel_id) 