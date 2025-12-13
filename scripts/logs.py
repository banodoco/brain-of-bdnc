#!/usr/bin/env python3
"""
Unified log debugging and monitoring tool.

Usage:
    python scripts/logs.py health                    # Quick health check - errors, warnings, summary status
    python scripts/logs.py summary                   # Show how today's daily summary went
    python scripts/logs.py summary --date 2024-12-12 # Check summary for specific date
    python scripts/logs.py errors                    # Show ALL errors (most recent first)
    python scripts/logs.py errors --hours 6          # Show errors from last 6 hours
    python scripts/logs.py recent                    # Show last 20 logs
    python scripts/logs.py recent -n 50              # Show last 50 logs
    python scripts/logs.py search "rate limit"       # Search logs by message
    python scripts/logs.py search --logger Discord   # Search by logger name
    python scripts/logs.py tail                      # Live tail of logs
    python scripts/logs.py stats                     # Detailed statistics
    python scripts/logs.py cleanup                   # Clean up old logs (48h)
    
Press Ctrl+C to stop tail.
"""

import os
import sys
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

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
GREEN = '\033[32m'
YELLOW = '\033[33m'
RED = '\033[31m'
CYAN = '\033[36m'


def get_client():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        print(f"{RED}Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set{RESET}")
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
        msg_preview = message[:150] + ('...' if len(message) > 150 else '')
        output += f"  {msg_preview}\n"
    
    # Exception traceback for errors
    if log.get('exception') and (verbose or level in ('ERROR', 'CRITICAL')):
        output += f"\n  {color}Exception:{RESET}\n"
        for line in log['exception'].split('\n')[:15]:  # Limit traceback lines
            output += f"    {DIM}{line}{RESET}\n"
        if log['exception'].count('\n') > 15:
            output += f"    {DIM}... (truncated){RESET}\n"
    
    return output


def cmd_health(args):
    """Quick health check - shows errors, warnings, and summary status."""
    supabase = get_client()
    
    print(f"\n{BOLD}🏥 System Health Check{RESET}")
    print("=" * 60)
    
    # Time ranges
    now = datetime.utcnow()
    last_1h = (now - timedelta(hours=1)).isoformat()
    last_6h = (now - timedelta(hours=6)).isoformat()
    last_24h = (now - timedelta(hours=24)).isoformat()
    
    # 1. Error counts
    print(f"\n{BOLD}🚨 Errors & Warnings:{RESET}")
    for hours, since, label in [(1, last_1h, 'Last hour'), (6, last_6h, 'Last 6h'), (24, last_24h, 'Last 24h')]:
        # Errors
        err_resp = supabase.table('system_logs').select('id', count='exact').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', since).execute()
        err_count = err_resp.count or 0
        
        # Warnings
        warn_resp = supabase.table('system_logs').select('id', count='exact').eq('level', 'WARNING').gte('timestamp', since).execute()
        warn_count = warn_resp.count or 0
        
        if err_count > 0:
            print(f"  {label}: {RED}{err_count} errors{RESET}, {YELLOW}{warn_count} warnings{RESET}")
        elif warn_count > 0:
            print(f"  {label}: {GREEN}0 errors{RESET}, {YELLOW}{warn_count} warnings{RESET}")
        else:
            print(f"  {label}: {GREEN}✓ No errors or warnings{RESET}")
    
    # 2. Recent errors (show up to 3)
    err_response = supabase.table('system_logs').select('*').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', last_24h).order('timestamp', desc=True).limit(3).execute()
    if err_response.data:
        print(f"\n{BOLD}📋 Recent Errors (last 24h):{RESET}")
        for log in err_response.data:
            ts = log['timestamp'][:16].replace('T', ' ')
            msg = log['message'][:100] + ('...' if len(log['message']) > 100 else '')
            print(f"  {DIM}{ts}{RESET} {msg}")
    
    # 3. Summary status (check for today's summary)
    print(f"\n{BOLD}📰 Daily Summary Status:{RESET}")
    today = now.strftime('%Y-%m-%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Check for summary-related logs today
    summary_logs = supabase.table('system_logs').select('*').ilike('message', '%summary%').gte('timestamp', f'{today}T00:00:00').order('timestamp', desc=True).limit(10).execute()
    
    # Look for key summary events
    summary_started = False
    summary_completed = False
    channels_processed = 0
    summary_errors = []
    
    for log in (summary_logs.data or []):
        msg = log['message'].lower()
        if 'generating requested summary' in msg or 'generate_summary' in msg:
            summary_started = True
        if 'main summary saved' in msg or 'posting top generations' in msg:
            summary_completed = True
        if 'processing channel' in msg:
            channels_processed += 1
        if log['level'] in ('ERROR', 'CRITICAL') and 'summary' in msg:
            summary_errors.append(log['message'][:80])
    
    if summary_started and summary_completed:
        print(f"  Today ({today}): {GREEN}✓ Completed{RESET} ({channels_processed} channels processed)")
    elif summary_started:
        print(f"  Today ({today}): {YELLOW}⏳ In progress or incomplete{RESET}")
    else:
        print(f"  Today ({today}): {DIM}Not yet run{RESET}")
    
    if summary_errors:
        print(f"  {RED}Errors during summary:{RESET}")
        for err in summary_errors[:3]:
            print(f"    - {err}")
    
    # 4. Bot activity indicator
    print(f"\n{BOLD}🤖 Bot Activity:{RESET}")
    recent_logs = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    if recent_logs.data:
        last_log = recent_logs.data[0]['timestamp'][:19].replace('T', ' ')
        last_log_dt = datetime.fromisoformat(recent_logs.data[0]['timestamp'][:19])
        age_mins = (now - last_log_dt).total_seconds() / 60
        
        if age_mins < 5:
            print(f"  Last log: {GREEN}{last_log} ({age_mins:.0f}m ago) ✓ Active{RESET}")
        elif age_mins < 30:
            print(f"  Last log: {YELLOW}{last_log} ({age_mins:.0f}m ago){RESET}")
        else:
            print(f"  Last log: {RED}{last_log} ({age_mins:.0f}m ago) ⚠️ No recent activity{RESET}")
    else:
        print(f"  {RED}No logs found{RESET}")
    
    print("=" * 60)


def cmd_summary(args):
    """Show detailed info about daily summary runs."""
    supabase = get_client()
    
    # Determine date to check
    if args.date:
        try:
            check_date = datetime.strptime(args.date, '%Y-%m-%d')
        except ValueError:
            print(f"{RED}Invalid date format. Use YYYY-MM-DD{RESET}")
            return
    else:
        check_date = datetime.utcnow()
    
    date_str = check_date.strftime('%Y-%m-%d')
    start_ts = f'{date_str}T00:00:00'
    end_ts = f'{date_str}T23:59:59'
    
    print(f"\n{BOLD}📰 Daily Summary Analysis - {date_str}{RESET}")
    print("=" * 60)
    
    # Get all summary-related logs for the date
    query = supabase.table('system_logs').select('*').gte('timestamp', start_ts).lte('timestamp', end_ts).order('timestamp', desc=False)
    
    # Filter for summary-related logs
    response = query.execute()
    all_logs = response.data or []
    
    # Filter locally for summary-related logs
    summary_keywords = ['summary', 'summariser', 'summarizer', 'channel_summaries', 'generate_summary', 
                       'top generation', 'top_gen', 'news_summary', 'topgenerations', 'top art']
    summary_logs = [
        log for log in all_logs 
        if any(kw in log['message'].lower() for kw in summary_keywords) or
           any(kw in log.get('logger_name', '').lower() for kw in summary_keywords)
    ]
    
    if not summary_logs:
        print(f"\n{DIM}No summary-related logs found for {date_str}{RESET}")
        print(f"\nTip: Summary typically runs around 10:00 UTC")
        return
    
    # Timeline
    print(f"\n{BOLD}📅 Timeline:{RESET}")
    
    key_events = {
        'start': None,
        'channels_found': None,
        'channels_processed': [],
        'main_summary': None,
        'top_gens': None,
        'end': None,
        'errors': []
    }
    
    for log in summary_logs:
        ts = log['timestamp'][:19].replace('T', ' ')
        msg = log['message'].lower()
        
        if 'generating requested summary' in msg:
            key_events['start'] = ts
            print(f"  {GREEN}⏱ {ts}{RESET} Summary started")
        
        if 'returned' in msg and 'channels' in msg:
            key_events['channels_found'] = ts
            # Extract channel count
            import re
            match = re.search(r'(\d+)\s*channels?', msg)
            if match:
                print(f"  {CYAN}📊 {ts}{RESET} Found {match.group(1)} active channels")
        
        if 'processing channel' in msg:
            key_events['channels_processed'].append(ts)
        
        if 'news summary generated' in msg:
            print(f"  {CYAN}📝 {ts}{RESET} Channel summary generated")
        
        if 'main summary saved' in msg:
            key_events['main_summary'] = ts
            print(f"  {GREEN}✅ {ts}{RESET} Main summary saved to DB")
        
        if 'posting top generations' in msg.lower() or 'top_x_generations' in msg.lower():
            key_events['top_gens'] = ts
            print(f"  {CYAN}🎨 {ts}{RESET} Top generations posted")
        
        if log['level'] in ('ERROR', 'CRITICAL'):
            key_events['errors'].append((ts, log['message'][:100]))
            print(f"  {RED}❌ {ts}{RESET} {log['message'][:80]}...")
    
    # Summary
    print(f"\n{BOLD}📊 Summary:{RESET}")
    print(f"  Channels processed: {len(key_events['channels_processed'])}")
    
    if key_events['main_summary']:
        print(f"  Main summary: {GREEN}✓ Saved{RESET}")
    elif key_events['start']:
        print(f"  Main summary: {YELLOW}⚠ Not saved (may have failed){RESET}")
    
    if key_events['top_gens']:
        print(f"  Top generations: {GREEN}✓ Posted{RESET}")
    elif key_events['start']:
        print(f"  Top generations: {YELLOW}⚠ Not posted{RESET}")
    
    if key_events['errors']:
        print(f"\n{BOLD}{RED}Errors ({len(key_events['errors'])}):{RESET}")
        for ts, err in key_events['errors'][:5]:
            print(f"  {ts}: {err}")
    
    # Check daily_summaries table
    print(f"\n{BOLD}💾 Database Records:{RESET}")
    try:
        summaries_resp = supabase.table('daily_summaries').select('channel_id, date, created_at').eq('date', date_str).execute()
        if summaries_resp.data:
            print(f"  Found {len(summaries_resp.data)} summary record(s) in daily_summaries table")
            for s in summaries_resp.data[:5]:
                print(f"    - Channel {s['channel_id']} at {s['created_at'][:19]}")
        else:
            print(f"  {DIM}No records in daily_summaries for {date_str}{RESET}")
    except Exception as e:
        print(f"  {RED}Error checking daily_summaries: {e}{RESET}")
    
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
        print(f"\n{GREEN}✅ No errors found ({time_desc}){RESET}")
        return
    
    print(f"\n{RED}{BOLD}🚨 {len(response.data)} errors ({time_desc}):{RESET}\n")
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
        msg = log['message'][:120] + ('...' if len(log['message']) > 120 else '')
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
    level_order = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3, 'CRITICAL': 4}
    min_level = level_order.get(args.level.upper() if args.level else 'DEBUG', 0)
    
    try:
        while True:
            # Query for new logs
            query = supabase.table('system_logs').select('*').gt('timestamp', last_ts)
            response = query.order('timestamp', desc=False).limit(50).execute()
            
            for log in response.data:
                log_id = log.get('id')
                if log_id and log_id not in seen_ids:
                    # Filter by level
                    log_level = level_order.get(log['level'], 1)
                    if log_level < min_level:
                        continue
                    
                    seen_ids.add(log_id)
                    ts = log['timestamp'][:19].replace('T', ' ')
                    level = log['level']
                    color = COLORS.get(level, '')
                    logger = log.get('logger_name', '?')
                    msg = log['message'][:120] + ('...' if len(log['message']) > 120 else '')
                    print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {DIM}[{logger}]{RESET} {msg}")
                    
                    # Update last timestamp
                    if log['timestamp'] > last_ts:
                        last_ts = log['timestamp']
            
            # Cleanup old seen_ids
            if len(seen_ids) > 1000:
                seen_ids = set(list(seen_ids)[-500:])
            
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


def cmd_stats(args):
    """Show detailed log statistics."""
    supabase = get_client()
    
    print(f"\n{BOLD}📊 Log Statistics{RESET}")
    print("=" * 60)
    
    # Total logs
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count or 0
    print(f"Total logs: {total:,}")
    
    # By level
    print(f"\n{BOLD}By Level:{RESET}")
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        response = supabase.table('system_logs').select('id', count='exact').eq('level', level).execute()
        count = response.count or 0
        pct = (count / total * 100) if total > 0 else 0
        color = COLORS.get(level, '')
        bar = '█' * int(pct / 2) if pct > 0 else ''
        print(f"  {color}{level:10}{RESET} {count:>8,} ({pct:5.1f}%) {bar}")
    
    # Last 24 hours
    yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
    response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', yesterday).execute()
    last_24h = response.count or 0
    print(f"\nLast 24 hours: {last_24h:,}")
    
    # Errors last 24h
    response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', yesterday).in_('level', ['ERROR', 'CRITICAL']).execute()
    errors_24h = response.count or 0
    if errors_24h > 0:
        print(f"Errors (24h): {RED}{errors_24h:,}{RESET}")
    else:
        print(f"Errors (24h): {GREEN}0{RESET}")
    
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
    
    # Top loggers
    print(f"\n{BOLD}Top Loggers:{RESET}")
    response = supabase.table('system_logs').select('logger_name').limit(5000).execute()
    if response.data:
        logger_counts = Counter(log['logger_name'] for log in response.data)
        for logger, count in logger_counts.most_common(8):
            print(f"  {logger:35} {count:>6,}")
    
    print("=" * 60)


def cmd_cleanup(args):
    """Clean up old logs."""
    supabase = get_client()
    
    hours = args.hours or 48
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    
    # Count logs to delete
    response = supabase.table('system_logs').select('id', count='exact').lt('timestamp', cutoff).execute()
    to_delete = response.count or 0
    
    if to_delete == 0:
        print(f"{GREEN}✅ No logs older than {hours} hours{RESET}")
        return
    
    print(f"Found {to_delete:,} logs older than {hours} hours")
    confirm = input("Delete them? [y/N]: ")
    
    if confirm.lower() != 'y':
        print("Cancelled")
        return
    
    # Delete in batches
    deleted = 0
    while True:
        response = supabase.table('system_logs').delete().lt('timestamp', cutoff).limit(1000).execute()
        batch = len(response.data) if response.data else 0
        if batch == 0:
            break
        deleted += batch
        print(f"  Deleted {deleted:,}...", end='\r')
    
    print(f"\n{GREEN}✅ Cleaned up {deleted:,} old logs{RESET}")


def cmd_trace(args):
    """Trace a specific feature/operation by keyword."""
    supabase = get_client()
    
    # Predefined feature keywords
    FEATURES = {
        'summary': ['summary', 'summariser', 'summarizer', 'news_summary', 'generate_summary', 
                   'channel_summaries', 'top generation', 'top_gen', 'topgenerations'],
        'archive': ['[Archive]', 'archive_discord', 'archiving'],
        'share': ['sharer', 'sharing', 'twitter', 'social_poster', 'tweet'],
        'react': ['reactor', 'reaction', 'watchlist'],
        'llm': ['claude', 'anthropic', 'openai', 'gemini', 'llm', 'rate limit'],
    }
    
    feature = args.feature.lower()
    if feature in FEATURES:
        keywords = FEATURES[feature]
        print(f"\n{BOLD}🔍 Tracing '{feature}' feature{RESET}")
        print(f"Keywords: {', '.join(keywords)}")
    else:
        keywords = [args.feature]
        print(f"\n{BOLD}🔍 Tracing custom keyword: '{args.feature}'{RESET}")
    
    # Time filter
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
    else:
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    
    print(f"Time range: since {since[:19]}")
    print("=" * 60)
    
    # Search for each keyword
    all_logs = []
    seen_ids = set()
    
    for keyword in keywords[:3]:  # Limit to avoid timeout
        try:
            response = supabase.table('system_logs').select('*').ilike('message', f'%{keyword}%').gte('timestamp', since).order('timestamp', desc=False).limit(100).execute()
            for log in response.data:
                if log['id'] not in seen_ids:
                    seen_ids.add(log['id'])
                    all_logs.append(log)
        except Exception as e:
            print(f"{RED}Search for '{keyword}' timed out{RESET}")
    
    # Sort by timestamp
    all_logs.sort(key=lambda x: x['timestamp'])
    
    if not all_logs:
        print(f"\n{DIM}No logs found for '{feature}'{RESET}")
        print(f"\nNote: Logs may have been cleaned up (48h retention) or feature may not be logging to Supabase.")
        return
    
    print(f"\n{BOLD}Found {len(all_logs)} related logs:{RESET}\n")
    
    # Group by time gaps (identify slow operations)
    prev_time = None
    for log in all_logs:
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = COLORS.get(level, '')
        msg = log['message'][:140] + ('...' if len(log['message']) > 140 else '')
        
        # Show time gap if > 1 minute
        if prev_time:
            current = datetime.fromisoformat(log['timestamp'][:19])
            gap = (current - prev_time).total_seconds() / 60
            if gap > 1:
                print(f"{YELLOW}  ⏱ {gap:.1f} minute gap{RESET}")
        
        print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {msg}")
        prev_time = datetime.fromisoformat(log['timestamp'][:19])
    
    print("=" * 60)
    
    # Summary
    error_count = sum(1 for log in all_logs if log['level'] in ('ERROR', 'CRITICAL'))
    if error_count:
        print(f"\n{RED}⚠ {error_count} errors found{RESET}")


def cmd_test(args):
    """Insert test log entries."""
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
            'logger_name': 'LogsScript',
            'module': 'logs',
            'function_name': 'cmd_test',
            'hostname': 'local-debug'
        })
        supabase.table('system_logs').insert(log).execute()
        color = COLORS.get(log['level'], '')
        print(f"  {color}✓ {log['level']}{RESET}: {log['message'][:50]}")
    
    print(f"\n{GREEN}✅ Inserted {len(test_logs)} test logs{RESET}")


def main():
    parser = argparse.ArgumentParser(
        description='Unified log debugging and monitoring tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  health    Quick health check - errors, warnings, summary status
  summary   Detailed info about daily summary runs
  errors    Show errors (all by default, or last N hours)
  recent    Show recent logs
  search    Search logs by message or logger
  tail      Live tail of new logs
  stats     Detailed log statistics
  trace     Trace a feature: summary, archive, share, react, llm
  cleanup   Clean up old logs

Examples:
  %(prog)s health                    Quick health check
  %(prog)s summary                   Today's summary status
  %(prog)s summary --date 2024-12-12 Specific date summary
  %(prog)s errors --hours 6          Errors from last 6 hours
  %(prog)s trace summary             Trace summary feature logs
  %(prog)s trace llm --hours 6       Trace LLM calls in last 6h
  %(prog)s search "rate limit"       Search for text
  %(prog)s tail --level WARNING      Tail warnings and above
        """
    )
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # health command
    subparsers.add_parser('health', help='Quick health check')
    
    # summary command
    summary_parser = subparsers.add_parser('summary', help='Daily summary analysis')
    summary_parser.add_argument('--date', type=str, help='Date to check (YYYY-MM-DD, default: today)')
    
    # errors command
    errors_parser = subparsers.add_parser('errors', help='Show errors')
    errors_parser.add_argument('--hours', type=int, help='Only show errors from last N hours')
    errors_parser.add_argument('-n', '--limit', type=int, default=20, help='Max number of errors')
    errors_parser.add_argument('-v', '--verbose', action='store_true', help='Show full details')
    
    # recent command
    recent_parser = subparsers.add_parser('recent', help='Show recent logs')
    recent_parser.add_argument('-n', '--limit', type=int, default=20, help='Number of logs')
    recent_parser.add_argument('--level', help='Filter by level')
    recent_parser.add_argument('--hours', type=int, help='Only logs from last N hours')
    
    # search command
    search_parser = subparsers.add_parser('search', help='Search logs')
    search_parser.add_argument('pattern', nargs='?', help='Search pattern')
    search_parser.add_argument('--logger', help='Filter by logger name')
    search_parser.add_argument('--level', help='Filter by level')
    search_parser.add_argument('--hours', type=int, help='Only search last N hours')
    search_parser.add_argument('-n', '--limit', type=int, default=30, help='Max results')
    search_parser.add_argument('-v', '--verbose', action='store_true', help='Show full details')
    
    # tail command
    tail_parser = subparsers.add_parser('tail', help='Live tail of logs')
    tail_parser.add_argument('--level', help='Minimum level to show')
    tail_parser.add_argument('--interval', type=float, default=2.0, help='Poll interval in seconds')
    
    # stats command
    subparsers.add_parser('stats', help='Detailed log statistics')
    
    # cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up old logs')
    cleanup_parser.add_argument('--hours', type=int, default=48, help='Delete logs older than N hours')
    
    # trace command
    trace_parser = subparsers.add_parser('trace', help='Trace a feature (summary, archive, share, react, llm)')
    trace_parser.add_argument('feature', help='Feature to trace: summary, archive, share, react, llm, or custom keyword')
    trace_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    
    # test command
    subparsers.add_parser('test', help='Insert test logs')
    
    args = parser.parse_args()
    
    if not args.command:
        # Default to health check
        args.command = 'health'
    
    commands = {
        'health': cmd_health,
        'summary': cmd_summary,
        'errors': cmd_errors,
        'recent': cmd_recent,
        'search': cmd_search,
        'tail': cmd_tail,
        'stats': cmd_stats,
        'trace': cmd_trace,
        'cleanup': cmd_cleanup,
        'test': cmd_test,
    }
    
    commands[args.command](args)


if __name__ == '__main__':
    main()
