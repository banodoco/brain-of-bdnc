#!/usr/bin/env python3
"""
Debug utility for system logs - test logging, view status, and troubleshoot.

Usage:
    python scripts/logs_debug.py test        # Insert test logs
    python scripts/logs_debug.py status      # Show logging status
    python scripts/logs_debug.py recent      # Show last 10 logs
    python scripts/logs_debug.py errors      # Show recent errors
    python scripts/logs_debug.py clear       # Clear all logs (with confirmation)
    python scripts/logs_debug.py cleanup     # Run manual cleanup (24h)
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


def get_client():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


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
        print(f"  ✓ {log['level']}: {log['message'][:50]}")
    
    print(f"\n✅ Inserted {len(test_logs)} test logs")


def cmd_status(args):
    """Show logging system status."""
    supabase = get_client()
    
    print("\n📊 System Logs Status\n")
    print("=" * 50)
    
    # Total count
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count
    print(f"Total logs: {total:,}")
    
    # By level
    print("\nBy Level:")
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        response = supabase.table('system_logs').select('id', count='exact').eq('level', level).execute()
        count = response.count
        pct = (count / total * 100) if total > 0 else 0
        print(f"  {level:10} {count:>6,} ({pct:5.1f}%)")
    
    # Age distribution
    print("\nAge Distribution:")
    now = datetime.utcnow()
    
    for label, hours in [('< 1 hour', 1), ('1-6 hours', 6), ('6-12 hours', 12), ('12-24 hours', 24)]:
        if hours == 1:
            since = (now - timedelta(hours=1)).isoformat()
            response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', since).execute()
        else:
            prev_hours = {'1-6 hours': 1, '6-12 hours': 6, '12-24 hours': 12}[label]
            since = (now - timedelta(hours=hours)).isoformat()
            until = (now - timedelta(hours=prev_hours)).isoformat()
            response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', since).lt('timestamp', until).execute()
        count = response.count
        print(f"  {label:15} {count:>6,}")
    
    # Most recent
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    if response.data:
        ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"\nMost recent: {ts}")
    
    # Oldest
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=False).limit(1).execute()
    if response.data:
        ts = response.data[0]['timestamp'][:19].replace('T', ' ')
        print(f"Oldest:      {ts}")
    
    print("=" * 50)


def cmd_recent(args):
    """Show most recent logs."""
    supabase = get_client()
    
    limit = args.limit or 10
    response = supabase.table('system_logs').select('*').order('timestamp', desc=True).limit(limit).execute()
    
    if not response.data:
        print("No logs found")
        return
    
    print(f"\n📋 Last {len(response.data)} logs:\n")
    
    # Color codes
    colors = {
        'DEBUG': '\033[90m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    reset = '\033[0m'
    
    for log in reversed(response.data):
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = colors.get(level, '')
        msg = log['message'][:80] + ('...' if len(log['message']) > 80 else '')
        print(f"{ts} {color}{level:8}{reset} {msg}")


def cmd_errors(args):
    """Show recent errors."""
    supabase = get_client()
    
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    response = supabase.table('system_logs').select('*').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', since).order('timestamp', desc=True).limit(20).execute()
    
    if not response.data:
        print("✅ No errors in the last 24 hours!")
        return
    
    print(f"\n🚨 {len(response.data)} errors in last 24 hours:\n")
    
    for log in response.data:
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = '\033[31m' if level == 'ERROR' else '\033[35m'
        reset = '\033[0m'
        
        print(f"{ts} {color}{level}{reset}")
        print(f"  Logger: {log['logger_name']}")
        print(f"  Message: {log['message'][:100]}")
        if log.get('exception'):
            print(f"  Exception: {log['exception'][:150]}...")
        print()


def cmd_clear(args):
    """Clear all logs."""
    supabase = get_client()
    
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count
    
    if total == 0:
        print("No logs to clear")
        return
    
    print(f"⚠️  This will delete ALL {total:,} log entries!")
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
    
    cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    
    # Count logs to delete
    response = supabase.table('system_logs').select('id', count='exact').lt('timestamp', cutoff).execute()
    to_delete = response.count
    
    if to_delete == 0:
        print("✅ No logs older than 48 hours")
        return
    
    print(f"Found {to_delete:,} logs older than 48 hours")
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
    parser = argparse.ArgumentParser(description='Debug utility for system logs')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # test command
    subparsers.add_parser('test', help='Insert test log entries')
    
    # status command
    subparsers.add_parser('status', help='Show logging status')
    
    # recent command
    recent_parser = subparsers.add_parser('recent', help='Show recent logs')
    recent_parser.add_argument('-n', '--limit', type=int, default=10, help='Number of logs')
    
    # errors command
    subparsers.add_parser('errors', help='Show recent errors')
    
    # clear command
    subparsers.add_parser('clear', help='Clear all logs')
    
    # cleanup command
    subparsers.add_parser('cleanup', help='Run 24h cleanup')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    commands = {
        'test': cmd_test,
        'status': cmd_status,
        'recent': cmd_recent,
        'errors': cmd_errors,
        'clear': cmd_clear,
        'cleanup': cmd_cleanup,
    }
    
    commands[args.command](args)


if __name__ == '__main__':
    main()
