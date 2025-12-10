#!/usr/bin/env python3
"""
View recent system logs from Supabase.

Usage:
    python scripts/logs_view.py                    # Last 50 logs
    python scripts/logs_view.py -n 100             # Last 100 logs
    python scripts/logs_view.py -l ERROR           # Only ERROR level
    python scripts/logs_view.py -l WARNING,ERROR   # WARNING and ERROR
    python scripts/logs_view.py --since 1h         # Last hour
    python scripts/logs_view.py --since 30m        # Last 30 minutes
    python scripts/logs_view.py --logger DiscordBot # Filter by logger name
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


def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '1h', '30m', '1d' into timedelta."""
    if not duration_str:
        return None
    
    unit = duration_str[-1].lower()
    try:
        value = int(duration_str[:-1])
    except ValueError:
        raise ValueError(f"Invalid duration format: {duration_str}")
    
    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)
    else:
        raise ValueError(f"Unknown time unit: {unit}. Use m (minutes), h (hours), or d (days)")


def format_log(log: dict, verbose: bool = False) -> str:
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
    
    # Truncate message if too long (unless verbose)
    if not verbose and len(message) > 200:
        message = message[:197] + '...'
    
    base = f"{timestamp} {color}{level:8}{reset} [{logger}] {message}"
    
    if verbose:
        if log.get('exception'):
            base += f"\n{'-'*60}\n{log['exception']}"
        if log.get('extra') and log['extra'] != {}:
            base += f"\n  Extra: {log['extra']}"
    
    return base


def main():
    parser = argparse.ArgumentParser(description='View system logs from Supabase')
    parser.add_argument('-n', '--limit', type=int, default=50, help='Number of logs to fetch (default: 50)')
    parser.add_argument('-l', '--level', type=str, help='Filter by log level(s), comma-separated (DEBUG,INFO,WARNING,ERROR,CRITICAL)')
    parser.add_argument('--since', type=str, help='Show logs since duration (e.g., 1h, 30m, 1d)')
    parser.add_argument('--logger', type=str, help='Filter by logger name')
    parser.add_argument('-v', '--verbose', action='store_true', help='Show full messages and exceptions')
    parser.add_argument('--errors-only', action='store_true', help='Shortcut for -l ERROR,CRITICAL')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    args = parser.parse_args()
    
    # Initialize Supabase client
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    
    supabase = create_client(url, key)
    
    # Build query
    query = supabase.table('system_logs').select('*')
    
    # Apply filters
    if args.errors_only:
        query = query.in_('level', ['ERROR', 'CRITICAL'])
    elif args.level:
        levels = [l.strip().upper() for l in args.level.split(',')]
        query = query.in_('level', levels)
    
    if args.since:
        try:
            delta = parse_duration(args.since)
            since_time = (datetime.utcnow() - delta).isoformat()
            query = query.gte('timestamp', since_time)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
    
    if args.logger:
        query = query.ilike('logger_name', f'%{args.logger}%')
    
    # Order and limit
    query = query.order('timestamp', desc=True).limit(args.limit)
    
    try:
        response = query.execute()
    except Exception as e:
        print(f"Error querying logs: {e}")
        sys.exit(1)
    
    logs = response.data
    
    if not logs:
        print("No logs found matching criteria")
        return
    
    if args.json:
        import json
        print(json.dumps(logs, indent=2, default=str))
        return
    
    # Print logs in chronological order (oldest first)
    logs.reverse()
    
    print(f"\n📋 Showing {len(logs)} logs" + (f" (since {args.since} ago)" if args.since else "") + "\n")
    print("-" * 80)
    
    for log in logs:
        print(format_log(log, args.verbose))
    
    print("-" * 80)
    print(f"Total: {len(logs)} logs")


if __name__ == '__main__':
    main()
