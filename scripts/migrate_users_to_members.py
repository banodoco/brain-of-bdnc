import sqlite3
import os

# --- Configuration ---
OLD_DB_PATH = 'old_users.db'
NEW_DB_PATH = 'data/production.db'
USERS_TABLE = 'users'
MEMBERS_TABLE = 'members'
# -------------------

def migrate_users():
    """
    Migrates user data from the 'users' table in the old database
    to the 'members' table in the new database.
    Uses INSERT OR IGNORE to avoid overwriting existing members.
    """
    print("Starting user migration...")

    if not os.path.exists(OLD_DB_PATH):
        print(f"Error: Old database file not found at '{OLD_DB_PATH}'")
        return

    if not os.path.exists(NEW_DB_PATH):
        print(f"Error: New database file not found at '{NEW_DB_PATH}'")
        return

    old_conn = None
    new_conn = None
    migrated_count = 0
    skipped_count = 0
    error_count = 0

    try:
        # Connect to both databases
        old_conn = sqlite3.connect(OLD_DB_PATH)
        old_conn.row_factory = sqlite3.Row # Access columns by name
        old_cursor = old_conn.cursor()

        new_conn = sqlite3.connect(NEW_DB_PATH)
        new_cursor = new_conn.cursor()

        print(f"Connected to '{OLD_DB_PATH}' and '{NEW_DB_PATH}'.")

        # Fetch all users from the old database
        old_cursor.execute(f"SELECT * FROM {USERS_TABLE}")
        old_users = old_cursor.fetchall()
        total_users = len(old_users)
        print(f"Found {total_users} users in '{OLD_DB_PATH}'. Processing...")

        # Prepare the INSERT OR IGNORE statement for the new database
        # Only include columns we are migrating data for
        insert_sql = f"""
        INSERT INTO {MEMBERS_TABLE} (
            member_id,
            username,
            global_name, -- Using old 'name' as initial global_name
            youtube_handle,
            twitter_handle,
            instagram_handle,
            tiktok_handle,
            website,
            sharing_consent,
            dm_preference,
            created_at,
            updated_at
            -- Other columns will get default values or NULL
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(member_id) DO UPDATE SET
            username = excluded.username,
            global_name = excluded.global_name,
            youtube_handle = excluded.youtube_handle,
            twitter_handle = excluded.twitter_handle,
            instagram_handle = excluded.instagram_handle,
            tiktok_handle = excluded.tiktok_handle,
            website = excluded.website,
            sharing_consent = excluded.sharing_consent,
            dm_preference = excluded.dm_preference,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """

        # Iterate and insert/ignore
        for i, user in enumerate(old_users):
            try:
                # Map data
                member_id = user['id']
                username = user['name']
                global_name = user['name'] # Use old name as initial global_name
                youtube_handle = user['youtube']
                twitter_handle = user['twitter']
                instagram_handle = user['instagram']
                tiktok_handle = user['tiktok']
                website = user['website']
                # Ensure boolean conversion for featured -> sharing_consent
                sharing_consent = bool(user['featured']) if user['featured'] is not None else False
                 # Ensure boolean conversion for dm_notifications -> dm_preference (1=True, 0/NULL=False)
                dm_preference = bool(user['dm_notifications'] == 1) if user['dm_notifications'] is not None else True # Default to True if old value is NULL
                created_at = user['created_at']
                updated_at = user['updated_at']

                # Prepare data tuple for insertion
                data_tuple = (
                    member_id,
                    username,
                    global_name,
                    youtube_handle,
                    twitter_handle,
                    instagram_handle,
                    tiktok_handle,
                    website,
                    sharing_consent,
                    dm_preference,
                    created_at,
                    updated_at
                )

                # Execute the INSERT OR IGNORE statement
                new_cursor.execute(insert_sql, data_tuple)

                # Check if a row was actually inserted (or ignored)
                if new_cursor.rowcount > 0:
                    migrated_count += 1
                else:
                    skipped_count += 1 # User likely already existed

                # Print progress periodically
                if (i + 1) % 100 == 0 or (i + 1) == total_users:
                    print(f"Processed {i + 1}/{total_users} users...")

            except sqlite3.Error as e:
                print(f"Error processing user ID {user.get('id', 'N/A')}: {e}")
                error_count += 1
            except Exception as e:
                 print(f"Unexpected error processing user ID {user.get('id', 'N/A')}: {e}")
                 error_count += 1


        # Commit the changes to the new database if no errors occurred during commit phase
        if error_count == 0:
            print("Committing changes to the new database...")
            new_conn.commit()
            print("Commit successful.")
        else:
             print(f"Skipping commit due to {error_count} processing errors.")
             # Optionally rollback, though INSERT OR IGNORE makes rollback less critical
             # new_conn.rollback()


    except sqlite3.Error as e:
        print(f"Database error during migration: {e}")
        if new_conn:
            new_conn.rollback() # Rollback on connection or setup errors
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        if new_conn:
            new_conn.rollback()
    finally:
        # Close connections
        if old_conn:
            old_conn.close()
            print(f"Closed connection to '{OLD_DB_PATH}'.")
        if new_conn:
            new_conn.close()
            print(f"Closed connection to '{NEW_DB_PATH}'.")

    print("\n--- Migration Summary ---")
    print(f"Total users processed: {total_users}")
    print(f"Users newly migrated: {migrated_count}")
    print(f"Users skipped (already existed): {skipped_count}")
    print(f"Errors encountered: {error_count}")
    print("------------------------")
    print("Migration script finished.")

if __name__ == "__main__":
    print("IMPORTANT: It is strongly recommended to back up 'data/production.db' before running this script.")
    # Add a small delay or user confirmation if desired
    # input("Press Enter to continue or Ctrl+C to cancel...")
    migrate_users() 