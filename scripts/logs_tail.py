#!/usr/bin/env python3
"""
Tail system logs from Supabase in real-time (polling).

Usage:
    python scripts/logs_tail.py                    # Tail all logs
    python scripts/logs_tail.py -l WARNING         # Only WARNING and above
    python scripts/logs_tail.py --interval 2       # Poll every 2 seconds
    
Press Ctrl+C to stop.
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client


def format_log(log: dict) -> str:
    """Format a log entry for display."""
    timestamp = log.get('timestamp', '')[:19].replace('T', ' ')
    level = log.get('level', 'UNKNOWN')
    logger = log.get('logger_name', '')
    message = log.get('message', '')
    
    # Color codes
    colors = {
        'DEBUG': '\033[90m',     # Gray
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    reset = '\033[0m'
    color = colors.get(level, '')
    
    return f"{timestamp} {color}{level:8}{reset} [{logger}] {message}"


def main():
    parser = argparse.ArgumentParser(description='Tail system logs from Supabase')
    parser.add_argument('-l', '--level', type=str, help='Minimum log level (DEBUG,INFO,WARNING,ERROR,CRITICAL)')
    parser.add_argument('--interval', type=float, default=3.0, help='Poll interval in seconds (default: 3)')
    parser.add_argument('--logger', type=str, help='Filter by logger name')
    
    args = parser.parse_args()
    
    # Initialize Supabase client
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    
    supabase = create_client(url, key)
    
    # Level hierarchy for filtering
    level_order = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3, 'CRITICAL': 4}
    min_level_num = level_order.get(args.level.upper() if args.level else 'DEBUG', 0)
    
    # Start from now
    last_timestamp = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
    seen_ids = set()
    
    print(f"🔄 Tailing logs (polling every {args.interval}s). Press Ctrl+C to stop.\n")
    print("-" * 80)
    
    try:
        while True:
            # Build query
            query = supabase.table('system_logs').select('*')
            query = query.gt('timestamp', last_timestamp)
            
            if args.logger:
                query = query.ilike('logger_name', f'%{args.logger}%')
            
            query = query.order('timestamp', desc=False).limit(100)
            
            try:
                response = query.execute()
                logs = response.data
                
                for log in logs:
                    log_id = log.get('id')
                    
                    # Skip if already seen
                    if log_id in seen_ids:
                        continue
                    
                    seen_ids.add(log_id)
                    
                    # Filter by level
                    log_level = log.get('level', 'INFO')
                    if level_order.get(log_level, 1) < min_level_num:
                        continue
                    
                    print(format_log(log))
                    
                    # Show exception if present
                    if log.get('exception'):
                        print(f"  {log['exception'][:200]}...")
                    
                    # Update last timestamp
                    log_ts = log.get('timestamp', '')
                    if log_ts > last_timestamp:
                        last_timestamp = log_ts
                
                # Cleanup old seen_ids to prevent memory growth
                if len(seen_ids) > 1000:
                    seen_ids = set(list(seen_ids)[-500:])
                    
            except Exception as e:
                print(f"\033[31mError polling logs: {e}\033[0m")
            
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print("\n\nStopped tailing logs.")


if __name__ == '__main__':
    main()
