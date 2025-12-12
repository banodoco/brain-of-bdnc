"""
Download Videos Script

NOTE: This is a standalone utility script for downloading videos from a LOCAL SQLite 
database backup file (e.g., data/production.db.new). It does NOT use the main Supabase 
database. This is useful for bulk downloading video attachments from an archived backup.

To use:
1. Download/copy a SQLite database backup to data/production.db.new
2. Run this script
"""

import os
import json
import sqlite3
from pathlib import Path
import asyncio
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# Initialize Discord client
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

async def fetch_message(channel_id, message_id):
    """Fetch a message using Discord API."""
    try:
        channel = await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        return message
    except Exception as e:
        print(f"Error fetching message {message_id} in channel {channel_id}: {e}")
        return None

async def download_file(session, url, filepath):
    """Download a file from URL."""
    try:
        async with session.get(url) as response:
            if response.status == 200:
                with open(filepath, 'wb') as f:
                    while True:
                        chunk = await response.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                return True
            else:
                print(f"Failed to download {url}: Status {response.status}")
                return False
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return False

async def process_videos():
    """Main function to process and download videos."""
    # Connect to database
    db_path = Path('data/production.db.new')  # Use the .new file we downloaded
    conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)  # Read-only mode
    conn.execute("PRAGMA query_only = TRUE;")  # Force read-only mode
    conn.execute("PRAGMA trusted_schema = FALSE;")  # More permissive with corruption
    cursor = conn.cursor()

    # Create files directory if it doesn't exist
    files_dir = Path('files')
    files_dir.mkdir(exist_ok=True)

    # Get messages with video attachments and reactions
    query = """
    SELECT 
        m.message_id,
        m.channel_id,
        m.attachments,
        m.author_id,
        mem.username,
        c.channel_name
    FROM messages m
    JOIN channels c ON m.channel_id = c.channel_id
    LEFT JOIN members mem ON m.author_id = mem.member_id
    WHERE (m.attachments LIKE '%.mp4%' 
       OR m.attachments LIKE '%.mov%'
       OR m.attachments LIKE '%.webm%'
       OR m.attachments LIKE '%.avi%'
       OR m.attachments LIKE '%.mkv%')
    AND m.reaction_count >= 1
    AND c.channel_name IN ('wan_gens', 'hunyuanvideo_gens')
    """
    
    cursor.execute(query)
    messages = cursor.fetchall()
    
    print(f"Found {len(messages)} messages with videos to process")
    
    async with aiohttp.ClientSession() as session:
        for msg_id, channel_id, attachments_json, author_id, username, channel_name in messages:
            try:
                # Get fresh message from Discord
                message = await fetch_message(channel_id, msg_id)
                if not message:
                    continue

                # Process each attachment
                attachments = json.loads(attachments_json)
                for i, attachment in enumerate(attachments):
                    if not any(attachment['filename'].lower().endswith(ext) 
                             for ext in ['.mp4', '.mov', '.webm', '.avi', '.mkv']):
                        continue

                    # Find matching attachment in fresh message
                    fresh_attachment = None
                    for att in message.attachments:
                        if att.filename == attachment['filename']:
                            fresh_attachment = att
                            break

                    if not fresh_attachment:
                        print(f"Could not find matching attachment for {attachment['filename']}")
                        continue

                    # Clean username and create filename
                    clean_username = re.sub(r'[^\w\s-]', '', username or 'unknown')
                    ext = Path(attachment['filename']).suffix
                    filename = f"{clean_username}_{channel_name}_{msg_id}_{i}{ext}"
                    filepath = files_dir / filename

                    # Download with fresh URL
                    print(f"Downloading {filename}...")
                    success = await download_file(session, fresh_attachment.url, filepath)
                    if success:
                        print(f"Successfully downloaded {filename}")
                    
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"Error processing message {msg_id}: {e}")
                continue

    conn.close()

@bot.event
async def on_ready():
    """Called when bot is ready."""
    print(f'Logged in as {bot.user}')
    await process_videos()
    await bot.close()

def main():
    """Main entry point."""
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main() 