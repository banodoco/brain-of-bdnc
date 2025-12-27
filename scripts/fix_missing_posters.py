#!/usr/bin/env python3
"""
Fix missing video posters in daily_summaries.

Downloads videos from Supabase Storage, extracts poster frames,
uploads them, and updates the full_summary JSON.

Usage:
    python scripts/fix_missing_posters.py              # Dry run
    python scripts/fix_missing_posters.py --apply      # Actually fix
    python scripts/fix_missing_posters.py --date 2025-12-27  # Specific date
"""

import argparse
import io
import json
import os
import sys
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# Check for required dependencies
try:
    from PIL import Image
    import imageio.v3 as iio
    import imageio_ffmpeg
    DEPS_AVAILABLE = True
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("Install with: pip install pillow imageio imageio-ffmpeg")
    DEPS_AVAILABLE = False


def get_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


def extract_poster(video_bytes: bytes, frame_time: float = 1.0) -> bytes:
    """Extract a poster frame from video bytes."""
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
        f.write(video_bytes)
        temp_path = f.name
    
    try:
        # Get video metadata
        meta = iio.immeta(temp_path, plugin="pyav")
        duration = meta.get('duration', 0)
        fps = meta.get('fps', 30)
        
        # Calculate frame index
        actual_time = min(frame_time, duration - 0.1) if duration > frame_time else 0
        frame_index = int(actual_time * fps)
        
        # Read frame
        frame = iio.imread(temp_path, index=frame_index, plugin="pyav")
        
        # Convert to JPEG
        img = Image.fromarray(frame)
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=85)
        buffer.seek(0)
        
        print(f"   ✓ Extracted frame at {actual_time:.1f}s (frame {frame_index})")
        return buffer.getvalue()
    finally:
        os.unlink(temp_path)


def find_videos_without_posters(client, date_filter=None):
    """Find all video entries in summaries that lack poster_url."""
    query = client.table('daily_summaries').select('daily_summary_id, date, channel_id, full_summary')
    
    if date_filter:
        query = query.eq('date', date_filter)
    
    result = query.execute()
    
    missing = []
    for row in result.data:
        if not row.get('full_summary'):
            continue
        
        try:
            items = json.loads(row['full_summary'])
        except:
            continue
        
        for item in items:
            # Check mainMediaUrls
            for media in (item.get('mainMediaUrls') or []):
                if isinstance(media, dict) and media.get('type') == 'video' and not media.get('poster_url'):
                    missing.append({
                        'summary_id': row['daily_summary_id'],
                        'date': row['date'],
                        'channel_id': row['channel_id'],
                        'video_url': media['url'],
                        'location': 'mainMediaUrls',
                        'item_title': item.get('title', 'Unknown')
                    })
            
            # Check subTopics
            for sub in item.get('subTopics', []):
                for media_list in (sub.get('subTopicMediaUrls') or []):
                    if media_list is None:
                        continue
                    for media in media_list:
                        if isinstance(media, dict) and media.get('type') == 'video' and not media.get('poster_url'):
                            missing.append({
                                'summary_id': row['daily_summary_id'],
                                'date': row['date'],
                                'channel_id': row['channel_id'],
                                'video_url': media['url'],
                                'location': 'subTopicMediaUrls',
                                'item_title': item.get('title', 'Unknown')
                            })
    
    return missing


def download_from_storage(client, url: str) -> bytes:
    """Download a file from Supabase Storage URL."""
    import requests
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def upload_poster(client, poster_bytes: bytes, video_url: str) -> str:
    """Upload poster to storage and return public URL."""
    # Generate poster path from video URL
    # e.g., .../2025-12-27/123_0.mp4 -> 2025-12-27/123_0_poster.jpg
    path_part = video_url.split('/summary-media/')[-1].split('?')[0]
    poster_path = path_part.rsplit('.', 1)[0] + '_poster.jpg'
    
    # Upload
    client.storage.from_('summary-media').upload(
        path=poster_path,
        file=poster_bytes,
        file_options={"content-type": "image/jpeg", "upsert": "true"}
    )
    
    # Get public URL
    return client.storage.from_('summary-media').get_public_url(poster_path)


def update_summary_with_poster(client, summary_id: int, video_url: str, poster_url: str):
    """Update the full_summary JSON to add poster_url to the matching video."""
    # Fetch current summary
    result = client.table('daily_summaries').select('full_summary').eq('daily_summary_id', summary_id).execute()
    if not result.data:
        return False
    
    full_summary = result.data[0]['full_summary']
    items = json.loads(full_summary)
    updated = False
    
    for item in items:
        # Check mainMediaUrls
        for media in (item.get('mainMediaUrls') or []):
            if isinstance(media, dict) and media.get('url') == video_url:
                media['poster_url'] = poster_url
                updated = True
        
        # Check subTopics
        for sub in item.get('subTopics', []):
            for media_list in (sub.get('subTopicMediaUrls') or []):
                if media_list is None:
                    continue
                for media in media_list:
                    if isinstance(media, dict) and media.get('url') == video_url:
                        media['poster_url'] = poster_url
                        updated = True
    
    if updated:
        client.table('daily_summaries').update({
            'full_summary': json.dumps(items, indent=2)
        }).eq('daily_summary_id', summary_id).execute()
    
    return updated


def main():
    parser = argparse.ArgumentParser(description="Fix missing video posters in summaries")
    parser.add_argument("--apply", action="store_true", help="Actually apply fixes (default is dry run)")
    parser.add_argument("--date", help="Filter to specific date (YYYY-MM-DD)")
    args = parser.parse_args()
    
    if not DEPS_AVAILABLE:
        sys.exit(1)
    
    client = get_client()
    
    print("\n🔍 Finding videos without posters...\n")
    missing = find_videos_without_posters(client, args.date)
    
    if not missing:
        print("✅ No videos missing posters!")
        return
    
    print(f"Found {len(missing)} videos without posters:\n")
    for i, m in enumerate(missing, 1):
        print(f"  {i}. [{m['date']}] {m['item_title'][:50]}")
        print(f"     URL: {m['video_url'][:80]}...")
        print()
    
    if not args.apply:
        print("🔸 Dry run - use --apply to fix these\n")
        return
    
    print("🔧 Fixing missing posters...\n")
    fixed = 0
    
    for m in missing:
        try:
            print(f"Processing: {m['video_url'].split('/')[-1].split('?')[0]}")
            
            # Download video
            print("   Downloading video...")
            video_bytes = download_from_storage(client, m['video_url'])
            print(f"   Downloaded {len(video_bytes) / 1024 / 1024:.1f} MB")
            
            # Extract poster
            print("   Extracting poster frame...")
            poster_bytes = extract_poster(video_bytes)
            
            # Upload poster
            print("   Uploading poster...")
            poster_url = upload_poster(client, poster_bytes, m['video_url'])
            print(f"   ✓ Uploaded: {poster_url[:60]}...")
            
            # Update database
            print("   Updating database...")
            if update_summary_with_poster(client, m['summary_id'], m['video_url'], poster_url):
                print("   ✓ Database updated")
                fixed += 1
            else:
                print("   ⚠ Could not update database")
            
            print()
            
        except Exception as e:
            print(f"   ❌ Error: {e}\n")
    
    print(f"\n✅ Fixed {fixed}/{len(missing)} videos\n")


if __name__ == "__main__":
    main()

