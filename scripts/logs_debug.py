#!/usr/bin/env python3
"""
Debug utility for system logs - test logging, view status, and troubleshoot.

Usage:
    python scripts/logs_debug.py status                  # Show logging status
    python scripts/logs_debug.py errors                  # Show ALL errors (not just 24h)
    python scripts/logs_debug.py errors --hours 6        # Show errors from last 6 hours
    python scripts/logs_debug.py recent                  # Show last 10 logs
    python scripts/logs_debug.py recent -n 50            # Show last 50 logs
    python scripts/logs_debug.py search "rate limit"     # Search logs by message
    python scripts/logs_debug.py search --logger Discord # Search by logger name
    python scripts/logs_debug.py tail                    # Live tail of logs
    python scripts/logs_debug.py test                    # Insert test logs
    python scripts/logs_debug.py clear                   # Clear all logs (with confirmation)
    python scripts/logs_debug.py cleanup                 # Run manual cleanup (48h)
"""

import os
import sys
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

# Color codes
COLORS = {
    'DEBUG': '\033[90m',     # Gray
    'INFO': '\033[32m',      # Green
    'WARNING': '\033[33m',   # Yellow
    'ERROR': '\033[31m',     # Red
    'CRITICAL': '\033[35m',  # Magenta
}
RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'


def get_client():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


def format_log(log, verbose=False):
    """Format a single log entry for display."""
    ts = log['timestamp'][:19].replace('T', ' ')
    level = log['level']
    color = COLORS.get(level, '')
    logger = log.get('logger_name', 'Unknown')
    message = log['message']
    
    # Header line
    output = f"{DIM}{ts}{RESET} {color}{BOLD}{level:8}{RESET} {DIM}[{logger}]{RESET}\n"
    
    # Message (handle multi-line)
    if verbose or level in ('ERROR', 'CRITICAL'):
        output += f"  {message}\n"
    else:
        # Truncate for non-errors in non-verbose mode
        msg_preview = message[:120] + ('...' if len(message) > 120 else '')
        output += f"  {msg_preview}\n"
    
    # Exception traceback for errors
    if log.get('exception') and (verbose or level in ('ERROR', 'CRITICAL')):
        output += f"\n  {color}Exception:{RESET}\n"
        for line in log['exception'].split('\n'):
            output += f"    {DIM}{line}{RESET}\n"
    
    # Extra metadata in verbose mode
    if verbose:
        if log.get('module'):
            output += f"  {DIM}Module: {log['module']}"
            if log.get('function_name'):
                output += f".{log['function_name']}()"
            output += f"{RESET}\n"
        if log.get('hostname'):
            output += f"  {DIM}Host: {log['hostname']}{RESET}\n"
    
    return output


def cmd_status(args):
    """Show logging system status."""
    supabase = get_client()
    
    print(f"\n{BOLD}📊 System Logs Status{RESET}\n")
    print("=" * 60)
    
    # Total count
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count or 0
    print(f"Total logs: {total:,}")
    
    # By level - highlight errors
    print(f"\n{BOLD}By Level:{RESET}")
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        response = supabase.table('system_logs').select('id', count='exact').eq('level', level).execute()
        count = response.count or 0
        pct = (count / total * 100) if total > 0 else 0
        color = COLORS.get(level, '')
        
        # Highlight if there are errors
        if level in ('ERROR', 'CRITICAL') and count > 0:
            print(f"  {color}{BOLD}{level:10} {count:>8,} ({pct:5.1f}%) ⚠️{RESET}")
        else:
            print(f"  {color}{level:10}{RESET} {count:>8,} ({pct:5.1f}%)")
    
    # Recent errors quick check
    print(f"\n{BOLD}Recent Errors:{RESET}")
    for hours, label in [(1, 'Last hour'), (6, 'Last 6h'), (24, 'Last 24h')]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        response = supabase.table('system_logs').select('id', count='exact').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', since).execute()
        count = response.count or 0
        if count > 0:
            print(f"  {COLORS['ERROR']}{label}: {count} errors ⚠️{RESET}")
        else:
            print(f"  {label}: {COLORS['INFO']}✓ No errors{RESET}")
    
    # Time range
    print(f"\n{BOLD}Time Range:{RESET}")
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    if response.data:
        ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"  Most recent: {ts}")
    
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=False).limit(1).execute()
    if response.data:
        ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"  Oldest:      {ts}")
    
    print("=" * 60)


def cmd_errors(args):
    """Show errors - ALL by default, or filtered by hours."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*').in_('level', ['ERROR', 'CRITICAL'])
    
    # Apply time filter if specified
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
        time_desc = f"last {args.hours} hours"
    else:
        time_desc = "all time"
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print(f"\n{COLORS['INFO']}✅ No errors found ({time_desc}){RESET}")
        return
    
    print(f"\n{COLORS['ERROR']}{BOLD}🚨 {len(response.data)} errors ({time_desc}):{RESET}\n")
    print("-" * 60)
    
    for log in response.data:
        print(format_log(log, verbose=args.verbose))
        print("-" * 60)


def cmd_recent(args):
    """Show most recent logs."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*')
    
    # Filter by level if specified
    if args.level:
        query = query.eq('level', args.level.upper())
    
    # Filter by time if specified
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print("No logs found")
        return
    
    print(f"\n{BOLD}📋 Last {len(response.data)} logs:{RESET}\n")
    
    # Show in chronological order
    for log in reversed(response.data):
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = COLORS.get(level, '')
        logger = log.get('logger_name', '?')
        msg = log['message'][:100] + ('...' if len(log['message']) > 100 else '')
        print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {DIM}[{logger}]{RESET} {msg}")


def cmd_search(args):
    """Search logs by message or logger."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*')
    
    # Search by pattern in message
    if args.pattern:
        query = query.ilike('message', f'%{args.pattern}%')
    
    # Filter by logger
    if args.logger:
        query = query.ilike('logger_name', f'%{args.logger}%')
    
    # Filter by level
    if args.level:
        query = query.eq('level', args.level.upper())
    
    # Filter by time
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print("No matching logs found")
        return
    
    print(f"\n{BOLD}🔍 Found {len(response.data)} matching logs:{RESET}\n")
    print("-" * 60)
    
    for log in response.data:
        print(format_log(log, verbose=args.verbose))
        print("-" * 60)


def cmd_tail(args):
    """Live tail of logs (polling)."""
    supabase = get_client()
    
    print(f"{BOLD}📡 Tailing logs (Ctrl+C to stop)...{RESET}\n")
    
    # Get the most recent timestamp to start from
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    last_ts = response.data[0]['timestamp'] if response.data else datetime.utcnow().isoformat()
    
    seen_ids = set()
    
    try:
        while True:
            # Query for new logs
            query = supabase.table('system_logs').select('*').gt('timestamp', last_ts)
            
            if args.level:
                query = query.eq('level', args.level.upper())
            
            response = query.order('timestamp', desc=False).limit(50).execute()
            
            for log in response.data:
                log_id = log.get('id')
                if log_id and log_id not in seen_ids:
                    seen_ids.add(log_id)
                    ts = log['timestamp'][:19].replace('T', ' ')
                    level = log['level']
                    color = COLORS.get(level, '')
                    logger = log.get('logger_name', '?')
                    msg = log['message'][:100] + ('...' if len(log['message']) > 100 else '')
                    print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {DIM}[{logger}]{RESET} {msg}")
                    
                    # Update last timestamp
                    if log['timestamp'] > last_ts:
                        last_ts = log['timestamp']
            
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


def cmd_test(args):
    """Insert test log entries at various levels."""
    supabase = get_client()
    
    print("📝 Inserting test logs...")
    
    test_logs = [
        {'level': 'DEBUG', 'message': 'Debug test message - detailed diagnostic info'},
        {'level': 'INFO', 'message': 'Info test message - normal operation'},
        {'level': 'WARNING', 'message': 'Warning test message - something to watch'},
        {'level': 'ERROR', 'message': 'Error test message - something went wrong'},
        {'level': 'CRITICAL', 'message': 'Critical test message - system failure'},
    ]
    
    for log in test_logs:
        log.update({
            'logger_name': 'DebugScript',
            'module': 'logs_debug',
            'function_name': 'cmd_test',
            'hostname': 'local-debug'
        })
        supabase.table('system_logs').insert(log).execute()
        color = COLORS.get(log['level'], '')
        print(f"  {color}✓ {log['level']}{RESET}: {log['message'][:50]}")
    
    print(f"\n✅ Inserted {len(test_logs)} test logs")


def cmd_clear(args):
    """Clear all logs."""
    supabase = get_client()
    
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count or 0
    
    if total == 0:
        print("No logs to clear")
        return
    
    print(f"{COLORS['ERROR']}⚠️  This will delete ALL {total:,} log entries!{RESET}")
    confirm = input("Type 'DELETE' to confirm: ")
    
    if confirm != 'DELETE':
        print("Cancelled")
        return
    
    # Delete in batches
    deleted = 0
    while True:
        response = supabase.table('system_logs').delete().neq('id', 0).limit(1000).execute()
        batch = len(response.data) if response.data else 0
        if batch == 0:
            break
        deleted += batch
        print(f"  Deleted {deleted:,}...", end='\r')
    
    print(f"\n✅ Cleared {deleted:,} logs")


def cmd_cleanup(args):
    """Run manual 48-hour cleanup."""
    supabase = get_client()
    
    hours = args.hours or 48
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    
    # Count logs to delete
    response = supabase.table('system_logs').select('id', count='exact').lt('timestamp', cutoff).execute()
    to_delete = response.count or 0
    
    if to_delete == 0:
        print(f"✅ No logs older than {hours} hours")
        return
    
    print(f"Found {to_delete:,} logs older than {hours} hours")
    confirm = input("Delete them? [y/N]: ")
    
    if confirm.lower() != 'y':
        print("Cancelled")
        return
    
    # Delete
    deleted = 0
    while True:
        response = supabase.table('system_logs').delete().lt('timestamp', cutoff).limit(1000).execute()
        batch = len(response.data) if response.data else 0
        if batch == 0:
            break
        deleted += batch
        print(f"  Deleted {deleted:,}...", end='\r')
    
    print(f"\n✅ Cleaned up {deleted:,} old logs")


def main():
    parser = argparse.ArgumentParser(
        description='Debug utility for system logs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status                    Show logging status and error counts
  %(prog)s errors                    Show ALL errors (most recent first)
  %(prog)s errors --hours 6          Show errors from last 6 hours
  %(prog)s errors -v                 Show errors with full tracebacks
  %(prog)s recent -n 50              Show last 50 logs
  %(prog)s recent --level ERROR      Show only ERROR level logs
  %(prog)s search "rate limit"       Search for logs containing text
  %(prog)s search --logger Discord   Search by logger name
  %(prog)s tail                      Live tail of new logs
  %(prog)s tail --level ERROR        Tail only errors
        """
    )
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # status command
    subparsers.add_parser('status', help='Show logging status and error counts')
    
    # errors command
    errors_parser = subparsers.add_parser('errors', help='Show errors (all by default)')
    errors_parser.add_argument('--hours', type=int, help='Only show errors from last N hours')
    errors_parser.add_argument('-n', '--limit', type=int, default=20, help='Max number of errors to show')
    errors_parser.add_argument('-v', '--verbose', action='store_true', help='Show full details')
    
    # recent command
    recent_parser = subparsers.add_parser('recent', help='Show recent logs')
    recent_parser.add_argument('-n', '--limit', type=int, default=20, help='Number of logs')
    recent_parser.add_argument('--level', help='Filter by level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    recent_parser.add_argument('--hours', type=int, help='Only show logs from last N hours')
    
    # search command
    search_parser = subparsers.add_parser('search', help='Search logs')
    search_parser.add_argument('pattern', nargs='?', help='Search pattern (in message)')
    search_parser.add_argument('--logger', help='Filter by logger name')
    search_parser.add_argument('--level', help='Filter by level')
    search_parser.add_argument('--hours', type=int, help='Only search last N hours')
    search_parser.add_argument('-n', '--limit', type=int, default=20, help='Max results')
    search_parser.add_argument('-v', '--verbose', action='store_true', help='Show full details')
    
    # tail command
    tail_parser = subparsers.add_parser('tail', help='Live tail of logs')
    tail_parser.add_argument('--level', help='Filter by level')
    tail_parser.add_argument('--interval', type=float, default=2.0, help='Poll interval in seconds')
    
    # test command
    subparsers.add_parser('test', help='Insert test log entries')
    
    # clear command
    subparsers.add_parser('clear', help='Clear all logs')
    
    # cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up old logs')
    cleanup_parser.add_argument('--hours', type=int, default=48, help='Delete logs older than N hours')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    commands = {
        'status': cmd_status,
        'errors': cmd_errors,
        'recent': cmd_recent,
        'search': cmd_search,
        'tail': cmd_tail,
        'test': cmd_test,
        'clear': cmd_clear,
        'cleanup': cmd_cleanup,
    }
    
    commands[args.command](args)


if __name__ == '__main__':
    main()
