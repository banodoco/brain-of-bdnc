import sqlite3
import json
from datetime import datetime
import sys
import os

def dump_good_data(src_db_path, dest_db_path):
    try:
        # Delete destination database if it exists
        if os.path.exists(dest_db_path):
            print(f"Removing existing database at {dest_db_path}")
            os.remove(dest_db_path)
        
        # Connect to source database
        src_conn = sqlite3.connect(src_db_path)
        src_conn.row_factory = sqlite3.Row
        
        # Create new database
        dest_conn = sqlite3.connect(dest_db_path)
        
        print("Starting database dump...")
        
        # Copy schema
        print("\nCopying schema...")
        schema_cursor = src_conn.cursor()
        schema_cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        schemas = schema_cursor.fetchall()
        
        dest_cursor = dest_conn.cursor()
        for name, sql in schemas:
            if sql and not name.startswith('messages_fts'):  # Skip FTS tables for now
                print(f"Creating table: {name}")
                dest_cursor.execute(sql)
        
        # Copy channels data
        print("\nCopying channels...")
        try:
            channels = src_conn.execute("SELECT * FROM channels").fetchall()
            for channel in channels:
                dest_cursor.execute("""
                    INSERT INTO channels 
                    (channel_id, channel_name, description, suitable_posts, 
                     unsuitable_posts, rules, setup_complete, nsfw, enriched, category_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, tuple(channel))
            print(f"Copied {len(channels)} channels")
        except Exception as e:
            print(f"Error copying channels: {e}")
        
        # Copy members data
        print("\nCopying members...")
        try:
            members = src_conn.execute("SELECT * FROM members").fetchall()
            for member in members:
                dest_cursor.execute("""
                    INSERT INTO members 
                    (member_id, username, global_name, server_nick, avatar_url,
                     discriminator, bot, system, accent_color, banner_url,
                     discord_created_at, guild_join_date, role_ids, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, tuple(member))
            print(f"Copied {len(members)} members")
        except Exception as e:
            print(f"Error copying members: {e}")
        
        # Copy messages data in chunks to avoid memory issues
        print("\nCopying messages...")
        offset = 0
        chunk_size = 1000
        total_copied = 0
        skipped_messages = []
        
        while True:
            try:
                messages = src_conn.execute(f"SELECT * FROM messages LIMIT {chunk_size} OFFSET {offset}").fetchall()
                if not messages:
                    break
                    
                for message in messages:
                    try:
                        # Validate JSON fields
                        attachments = message['attachments']
                        if attachments:
                            json.loads(attachments)  # Test if valid JSON
                            
                        reactors = message['reactors']
                        if reactors:
                            json.loads(reactors)  # Test if valid JSON
                            
                        embeds = message['embeds']
                        if embeds:
                            json.loads(embeds)  # Test if valid JSON
                        
                        # Validate date
                        created_at = message['created_at']
                        if created_at:
                            datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            
                        edited_at = message['edited_at']
                        if edited_at:
                            datetime.fromisoformat(edited_at.replace('Z', '+00:00'))
                        
                        # Insert valid message
                        dest_cursor.execute("""
                            INSERT INTO messages 
                            (message_id, channel_id, author_id, content, created_at,
                             attachments, embeds, reaction_count, reactors, reference_id,
                             edited_at, is_pinned, thread_id, message_type, flags,
                             jump_url, is_deleted, indexed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, tuple(message))
                        total_copied += 1
                        
                    except (json.JSONDecodeError, ValueError) as e:
                        skipped_messages.append((message['message_id'], str(e)))
                        continue
                
                offset += chunk_size
                if offset % 10000 == 0:
                    print(f"Processed {offset} messages...")
                dest_conn.commit()
                
            except Exception as e:
                print(f"Error in chunk starting at offset {offset}: {e}")
                offset += chunk_size  # Skip problematic chunk
                continue
        
        print(f"\nSuccessfully copied {total_copied} messages")
        if skipped_messages:
            print(f"Skipped {len(skipped_messages)} corrupt messages:")
            for msg_id, error in skipped_messages[:10]:  # Show first 10 errors
                print(f"Message {msg_id}: {error}")
            if len(skipped_messages) > 10:
                print(f"... and {len(skipped_messages) - 10} more")
        
        # Create FTS table
        print("\nCreating FTS table...")
        dest_cursor.execute("""
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='message_id'
            );
        """)
        
        # Populate FTS table
        print("Populating FTS table...")
        dest_cursor.execute("""
            INSERT INTO messages_fts(rowid, content)
            SELECT message_id, content
            FROM messages
            WHERE content IS NOT NULL AND content != '';
        """)
        
        # Create indexes
        print("\nRecreating indexes...")
        index_cursor = src_conn.cursor()
        index_cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        indexes = index_cursor.fetchall()
        
        for index in indexes:
            try:
                if index[0]:  # Skip any NULL entries
                    print(f"Creating index: {index[0][:50]}...")
                    dest_cursor.execute(index[0])
            except Exception as e:
                print(f"Error creating index: {e}")
        
        # Commit and close
        dest_conn.commit()
        src_conn.close()
        dest_conn.close()
        
        print("\nDatabase dump completed!")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        print(traceback.format_exc())

if __name__ == "__main__":
    src_db_path = "./production.db"
    dest_db_path = "./production_fixed.db"
    dump_good_data(src_db_path, dest_db_path) 