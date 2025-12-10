#!/usr/bin/env python3
"""
Search system logs from Supabase by message content.

Usage:
    python scripts/logs_search.py "rate limit"     # Search for "rate limit"
    python scripts/logs_search.py "error" -l ERROR # Search errors containing "error"
    python scripts/logs_search.py "api" --since 1d # Search last day for "api"
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
        raise ValueError(f"Unknown time unit: {unit}")


def format_log(log: dict, highlight: str = None) -> str:
    """Format a log entry for display with optional highlighting."""
    timestamp = log.get('timestamp', '')[:19].replace('T', ' ')
    level = log.get('level', 'UNKNOWN')
    logger = log.get('logger_name', '')
    message = log.get('message', '')
    
    # Color codes
    colors = {
        'DEBUG': '\033[90m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    reset = '\033[0m'
    bold = '\033[1m'
    color = colors.get(level, '')
    
    # Highlight search term
    if highlight and highlight.lower() in message.lower():
        idx = message.lower().find(highlight.lower())
        message = (
            message[:idx] + 
            bold + '\033[43m' + message[idx:idx+len(highlight)] + reset + 
            message[idx+len(highlight):]
        )
    
    return f"{timestamp} {color}{level:8}{reset} [{logger}] {message}"


def main():
    parser = argparse.ArgumentParser(description='Search system logs in Supabase')
    parser.add_argument('query', type=str, help='Search term')
    parser.add_argument('-n', '--limit', type=int, default=100, help='Max results (default: 100)')
    parser.add_argument('-l', '--level', type=str, help='Filter by log level')
    parser.add_argument('--since', type=str, help='Search since duration (e.g., 1h, 1d)')
    parser.add_argument('--logger', type=str, help='Filter by logger name')
    parser.add_argument('-v', '--verbose', action='store_true', help='Show exceptions')
    
    args = parser.parse_args()
    
    # Initialize Supabase client
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    
    supabase = create_client(url, key)
    
    # Build query - use ilike for case-insensitive search
    query = supabase.table('system_logs').select('*')
    query = query.ilike('message', f'%{args.query}%')
    
    if args.level:
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
    
    query = query.order('timestamp', desc=True).limit(args.limit)
    
    try:
        response = query.execute()
    except Exception as e:
        print(f"Error searching logs: {e}")
        sys.exit(1)
    
    logs = response.data
    
    if not logs:
        print(f"No logs found matching '{args.query}'")
        return
    
    # Print in chronological order
    logs.reverse()
    
    print(f"\n🔍 Found {len(logs)} logs matching '{args.query}'\n")
    print("-" * 80)
    
    for log in logs:
        print(format_log(log, args.query))
        if args.verbose and log.get('exception'):
            print(f"  Exception: {log['exception'][:300]}...")
    
    print("-" * 80)
    print(f"Total: {len(logs)} matches")


if __name__ == '__main__':
    main()
