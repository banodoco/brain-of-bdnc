#!/usr/bin/env python3
"""
Show log statistics and optionally clean up old logs.

Usage:
    python scripts/logs_stats.py              # Show statistics
    python scripts/logs_stats.py --cleanup    # Clean logs older than 7 days
    python scripts/logs_stats.py --cleanup 3  # Clean logs older than 3 days
"""

import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client


def main():
    parser = argparse.ArgumentParser(description='Log statistics and cleanup')
    parser.add_argument('--cleanup', nargs='?', const=7, type=int, metavar='DAYS',
                        help='Clean up logs older than N days (default: 7)')
    
    args = parser.parse_args()
    
    # Initialize Supabase client
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    
    supabase = create_client(url, key)
    
    print("\n📊 Log Statistics\n")
    print("=" * 60)
    
    # Total logs
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count
    print(f"Total logs: {total:,}")
    
    # Logs by level
    print("\nBy Level:")
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        response = supabase.table('system_logs').select('id', count='exact').eq('level', level).execute()
        count = response.count
        pct = (count / total * 100) if total > 0 else 0
        bar = '█' * int(pct / 2) if pct > 0 else ''
        print(f"  {level:10} {count:>8,} ({pct:5.1f}%) {bar}")
    
    # Logs in last 24h
    yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
    response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', yesterday).execute()
    last_24h = response.count
    print(f"\nLast 24 hours: {last_24h:,}")
    
    # Errors in last 24h
    response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', yesterday).in_('level', ['ERROR', 'CRITICAL']).execute()
    errors_24h = response.count
    print(f"Errors (24h): {errors_24h:,}")
    
    # Most recent log
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    if response.data:
        last_ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"\nMost recent: {last_ts}")
    
    # Oldest log
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=False).limit(1).execute()
    if response.data:
        oldest_ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"Oldest: {oldest_ts}")
    
    # Top loggers
    print("\nTop Loggers (by message count):")
    response = supabase.table('system_logs').select('logger_name').execute()
    if response.data:
        from collections import Counter
        logger_counts = Counter(log['logger_name'] for log in response.data)
        for logger, count in logger_counts.most_common(5):
            print(f"  {logger:30} {count:>8,}")
    
    print("=" * 60)
    
    # Cleanup if requested
    if args.cleanup:
        days = args.cleanup
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        
        # Count logs to delete
        response = supabase.table('system_logs').select('id', count='exact').lt('timestamp', cutoff).execute()
        to_delete = response.count
        
        if to_delete == 0:
            print(f"\n✅ No logs older than {days} days to clean up")
            return
        
        print(f"\n⚠️  Found {to_delete:,} logs older than {days} days")
        confirm = input("Delete these logs? [y/N]: ")
        
        if confirm.lower() == 'y':
            # Delete in batches
            deleted = 0
            while True:
                response = supabase.table('system_logs').delete().lt('timestamp', cutoff).limit(1000).execute()
                batch_deleted = len(response.data) if response.data else 0
                if batch_deleted == 0:
                    break
                deleted += batch_deleted
                print(f"  Deleted {deleted:,} logs...", end='\r')
            
            print(f"\n✅ Deleted {deleted:,} old logs")
        else:
            print("Cleanup cancelled")


if __name__ == '__main__':
    main()
