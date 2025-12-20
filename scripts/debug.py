#!/usr/bin/env python3
"""
Debug utility for investigating bot issues.

This script consolidates common debugging tasks. When you create a useful
one-off debug command, consider adding it here for future use.

Usage:
    python scripts/debug.py env                    # Show relevant env config
    python scripts/debug.py channels --limit 10   # List channels from DB
    python scripts/debug.py messages --channel ID  # Messages from a channel
    python scripts/debug.py members --limit 20    # List members
    python scripts/debug.py logs --hours 1        # Recent logs from Supabase
    python scripts/debug.py railway-logs -n 100   # Railway platform logs
    python scripts/debug.py summaries             # Recent summaries
    python scripts/debug.py channel-info ID       # Details about a specific channel
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def get_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


def format_row(row, max_width=100):
    """Format a row for display, truncating long values."""
    formatted = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) > max_width:
            v = v[:max_width] + "..."
        formatted[k] = v
    return formatted


def query_table(client, table, filters=None, limit=10, order_by=None, order_desc=True):
    """Query a table with optional filters."""
    q = client.table(table).select("*")
    
    if filters:
        for col, val in filters.items():
            q = q.eq(col, val)
    
    if order_by:
        q = q.order(order_by, desc=order_desc)
    
    q = q.limit(limit)
    result = q.execute()
    return result.data


def cmd_env(args):
    """Show environment configuration for debugging channel/ID mismatches."""
    print("\n🔧 Environment Configuration:\n")
    
    # Key channel/ID env vars to check
    env_vars = [
        ("DISCORD_BOT_TOKEN", True),  # (name, is_secret)
        ("SUPABASE_URL", False),
        ("SUPABASE_SERVICE_KEY", True),
        # Production
        ("GUILD_ID", False),
        ("SUMMARY_CHANNEL_ID", False),
        ("TOP_GENS_ID", False),
        ("ART_CHANNEL_ID", False),
        ("CHANNELS_TO_MONITOR", False),
        # Development
        ("DEV_GUILD_ID", False),
        ("DEV_SUMMARY_CHANNEL_ID", False),
        ("DEV_TOP_GENS_ID", False),
        ("DEV_ART_CHANNEL_ID", False),
        ("DEV_CHANNELS_TO_MONITOR", False),
        ("TEST_DATA_CHANNEL", False),
        # Other
        ("DEV_MODE", False),
        ("REACTION_WATCHLIST", False),
    ]
    
    print("  Production:")
    for var, is_secret in env_vars:
        if var.startswith("DEV_") or var in ["DISCORD_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "DEV_MODE", "REACTION_WATCHLIST"]:
            continue
        val = os.getenv(var)
        if val:
            print(f"    {var} = {val}")
        else:
            print(f"    {var} = (not set)")
    
    print("\n  Development:")
    for var, is_secret in env_vars:
        if not var.startswith("DEV_") and var != "TEST_DATA_CHANNEL":
            continue
        val = os.getenv(var)
        if val:
            print(f"    {var} = {val}")
        else:
            print(f"    {var} = (not set)")
    
    print("\n  Other:")
    print(f"    DEV_MODE = {os.getenv('DEV_MODE', '(not set)')}")
    watchlist = os.getenv("REACTION_WATCHLIST")
    if watchlist:
        print(f"    REACTION_WATCHLIST = {watchlist[:80]}..." if len(watchlist) > 80 else f"    REACTION_WATCHLIST = {watchlist}")
    
    # Check for common issues
    print("\n⚠️  Potential Issues:")
    issues = []
    
    summary_id = os.getenv("SUMMARY_CHANNEL_ID")
    top_gens_id = os.getenv("TOP_GENS_ID")
    if summary_id and not top_gens_id:
        issues.append("TOP_GENS_ID not set - will default to SUMMARY_CHANNEL_ID in production")
    if summary_id and top_gens_id and summary_id == top_gens_id:
        issues.append("TOP_GENS_ID equals SUMMARY_CHANNEL_ID - individual top gens will post to summary channel")
    
    dev_summary_id = os.getenv("DEV_SUMMARY_CHANNEL_ID")
    dev_top_gens_id = os.getenv("DEV_TOP_GENS_ID")
    if dev_summary_id and not dev_top_gens_id:
        issues.append("DEV_TOP_GENS_ID not set - will default to DEV_SUMMARY_CHANNEL_ID in dev mode")
    
    if issues:
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("  None detected")
    print()


def cmd_railway_logs(args):
    """Fetch Railway platform logs using the Railway CLI."""
    print("\n🚂 Railway Platform Logs:\n")
    
    lines = args.limit if hasattr(args, 'limit') else 100
    
    try:
        # Check if railway CLI is available
        check_result = subprocess.run(
            ['railway', '--version'],
            capture_output=True,
            text=True
        )
        
        if check_result.returncode != 0:
            print("❌ Railway CLI not found. Install it with:")
            print("   npm i -g @railway/cli")
            return
        
        # Run railway logs command
        result = subprocess.run(
            ['railway', 'logs', '--lines', str(lines)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            error_output = result.stderr.strip()
            
            # Handle common errors
            if "No linked project" in error_output:
                print("❌ No linked Railway project found.")
                print("\nTo link this directory to a Railway project:")
                print("   cd /path/to/bndc")
                print("   railway link")
            elif "not a TTY" in error_output:
                print("⚠️  Railway CLI requires an interactive terminal for some operations.")
                print("\nTry running this command directly in your terminal:")
                print(f"   railway logs --lines {lines}")
            else:
                print(f"❌ Error fetching logs:\n{error_output}")
            return
        
        # Success - print logs
        output = result.stdout.strip()
        if output:
            print(output)
        else:
            print("No logs found.")
            
    except subprocess.TimeoutExpired:
        print("❌ Command timed out after 30 seconds")
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install it with:")
        print("   npm i -g @railway/cli")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


def cmd_railway_status(args):
    """Check Railway service status and health endpoint."""
    print("\n🚂 Railway Service Status:\n")
    
    try:
        # Check if railway CLI is available
        check_result = subprocess.run(
            ['railway', '--version'],
            capture_output=True,
            text=True
        )
        
        if check_result.returncode != 0:
            print("❌ Railway CLI not found. Install it with:")
            print("   npm i -g @railway/cli")
            return
        
        # Get Railway project info
        result = subprocess.run(
            ['railway', 'status', '--json'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_output = result.stderr.strip()
            if "No linked project" in error_output:
                print("❌ No linked Railway project found.")
                print("\nTo link this directory to a Railway project:")
                print("   cd /path/to/bndc")
                print("   railway link")
            else:
                print(f"❌ Error fetching status:\n{error_output}")
            return
        
        # Parse JSON output
        try:
            status_data = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            print("⚠️  Could not parse Railway status JSON")
            status_data = {}
        
        if status_data:
            print(f"📦 Project: {status_data.get('project', {}).get('name', 'Unknown')}")
            print(f"🔧 Service: {status_data.get('service', {}).get('name', 'Unknown')}")
            print(f"🌍 Environment: {status_data.get('environment', {}).get('name', 'Unknown')}")
            print()
        
        # Try to get the service URL
        url_result = subprocess.run(
            ['railway', 'domain'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        service_url = None
        if url_result.returncode == 0 and url_result.stdout.strip():
            # Parse the URL from Railway's output (might include extra text)
            output = url_result.stdout.strip()
            # Look for https:// URL
            if 'https://' in output:
                # Extract just the URL
                import re
                match = re.search(r'https://[^\s]+', output)
                if match:
                    service_url = match.group(0)
            else:
                service_url = output
            
            if service_url:
                print(f"🌐 Service URL: {service_url}")
        
        # Check health endpoints if we have a URL
        if service_url:
            print("\n📊 Health Check Endpoints:\n")
            
            # Import requests here (optional dependency for this command)
            try:
                import requests
                
                endpoints = [
                    ('/health', 'Basic liveness'),
                    ('/ready', 'Readiness check'),
                    ('/status', 'Detailed metrics')
                ]
                
                for path, description in endpoints:
                    url = f"{service_url}{path}"
                    try:
                        response = requests.get(url, timeout=5)
                        
                        if response.status_code == 200:
                            status_emoji = "✅"
                            status_text = "OK"
                        elif response.status_code == 503:
                            status_emoji = "⏳"
                            status_text = "Not Ready"
                        else:
                            status_emoji = "❌"
                            status_text = f"HTTP {response.status_code}"
                        
                        print(f"  {status_emoji} {path:10} - {status_text:12} - {description}")
                        
                        # Show detailed status if available
                        if path == '/status' and response.status_code == 200:
                            try:
                                data = response.json()
                                print(f"\n     Deployment: {data.get('deployment_id', 'unknown')[:12]}...")
                                print(f"     Status: {data.get('status', 'unknown')}")
                                if data.get('uptime_seconds'):
                                    uptime_min = int(data['uptime_seconds'] / 60)
                                    print(f"     Uptime: {uptime_min} minutes")
                                if data.get('metrics'):
                                    metrics = data['metrics']
                                    print(f"     Messages logged: {metrics.get('messages_logged', 0)}")
                                    print(f"     Messages archived: {metrics.get('messages_archived', 0)}")
                                    print(f"     Errors: {metrics.get('errors_logged', 0)}")
                            except:
                                pass
                        
                    except requests.Timeout:
                        print(f"  ⏱️  {path:10} - Timeout      - {description}")
                    except requests.RequestException as e:
                        print(f"  ❌ {path:10} - Error        - {description}")
                        
            except ImportError:
                print("  ℹ️  Install 'requests' to check health endpoints")
                print(f"     pip install requests")
                print(f"\n  You can manually check:")
                for path, desc in endpoints:
                    print(f"     curl {service_url}{path}")
        else:
            print("\n⚠️  No service URL found. Health endpoints not checked.")
            
    except subprocess.TimeoutExpired:
        print("❌ Command timed out")
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install it with:")
        print("   npm i -g @railway/cli")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


def cmd_deployments(args):
    """Analyze Railway deployment history for duplicate deploys, crashes, etc."""
    print("\n🚀 Railway Deployment Analysis:\n")
    
    try:
        # Check if railway CLI is available
        check_result = subprocess.run(
            ['railway', '--version'],
            capture_output=True,
            text=True
        )
        
        if check_result.returncode != 0:
            print("❌ Railway CLI not found. Install it with:")
            print("   npm i -g @railway/cli")
            return
        
        # Fetch deployment list as JSON
        result = subprocess.run(
            ['railway', 'deployment', 'list', '--limit', str(args.limit), '--json'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            error_output = result.stderr.strip()
            if "No linked project" in error_output:
                print("❌ No linked Railway project found.")
                print("\nTo link this directory to a Railway project:")
                print("   cd /path/to/bndc")
                print("   railway link")
            else:
                print(f"❌ Error fetching deployments:\n{error_output}")
            return
        
        # Parse JSON output
        try:
            deployments = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse deployment JSON: {e}")
            return
        
        if not deployments:
            print("No deployments found.")
            return
        
        # Analysis
        print(f"📊 Analyzing {len(deployments)} recent deployments...\n")
        
        # Track issues
        issues = []
        commit_times = {}  # commit_hash -> [timestamps]
        
        for d in deployments:
            commit = d.get('meta', {}).get('commitHash', 'unknown')[:7]
            created_at = d.get('createdAt', '')
            status = d.get('status', 'UNKNOWN')
            
            # Track duplicate commits
            if commit not in commit_times:
                commit_times[commit] = []
            commit_times[commit].append(created_at)
        
        # Find duplicate deployments (same commit within short time window)
        print("🔍 Checking for duplicate deployments...\n")
        duplicates_found = False
        for commit, timestamps in commit_times.items():
            if len(timestamps) > 1:
                # Sort timestamps
                timestamps.sort()
                for i in range(len(timestamps) - 1):
                    t1 = datetime.fromisoformat(timestamps[i].replace('Z', '+00:00'))
                    t2 = datetime.fromisoformat(timestamps[i+1].replace('Z', '+00:00'))
                    diff_seconds = (t2 - t1).total_seconds()
                    
                    # Flag if deployments are within 5 minutes
                    if diff_seconds < 300:
                        duplicates_found = True
                        print(f"⚠️  DUPLICATE: Commit {commit} deployed twice {diff_seconds:.0f}s apart")
                        print(f"   First:  {timestamps[i]}")
                        print(f"   Second: {timestamps[i+1]}")
                        print()
                        issues.append(f"Duplicate deploy of {commit} ({diff_seconds:.0f}s apart)")
        
        if not duplicates_found:
            print("✅ No duplicate deployments detected\n")
        
        # Show recent deployment timeline
        print("📅 Recent Deployment Timeline:\n")
        for d in deployments[:20]:
            ts = d.get('createdAt', '')[:19].replace('T', ' ')
            status = d.get('status', 'UNKNOWN')
            commit = d.get('meta', {}).get('commitHash', 'unknown')[:7]
            msg = d.get('meta', {}).get('commitMessage', '').split('\n')[0][:60]
            
            status_emoji = {
                'SUCCESS': '✅',
                'REMOVED': '🗑️',
                'FAILED': '❌',
                'BUILDING': '🔨',
                'DEPLOYING': '🚀',
                'CRASHED': '💥'
            }.get(status, '❓')
            
            print(f"{status_emoji} {ts} [{status:10}] {commit} {msg}")
        
        # Summary
        print(f"\n📈 Summary:")
        statuses = {}
        for d in deployments:
            status = d.get('status', 'UNKNOWN')
            statuses[status] = statuses.get(status, 0) + 1
        
        for status, count in sorted(statuses.items()):
            print(f"   {status}: {count}")
        
        if issues:
            print(f"\n⚠️  Issues detected: {len(issues)}")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print(f"\n✅ No deployment issues detected")
            
    except subprocess.TimeoutExpired:
        print("❌ Command timed out after 30 seconds")
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install it with:")
        print("   npm i -g @railway/cli")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


def cmd_railway_logs(args):
    """Fetch Railway platform logs using CLI."""
    try:
        # Check if railway CLI is available
        result = subprocess.run(['which', 'railway'], capture_output=True, text=True)
        if result.returncode != 0:
            print("❌ Railway CLI not installed. Install with: npm i -g @railway/cli")
            return
        
        # Fetch logs
        lines = args.limit or 100
        cmd = ['railway', 'logs', '--lines', str(lines)]
        
        if args.json:
            cmd.append('--json')
        
        print(f"\n🚂 Railway Platform Logs (last {lines}):\n")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"❌ Error: {result.stderr.strip()}")
            if 'No linked project' in result.stderr:
                print("\n💡 To fix this:")
                print("   1. Run: railway link (in an interactive terminal)")
                print("   2. Or check Railway dashboard: https://railway.app")
                print("   3. Or use Railway API directly (see Railway docs)")
            elif 'not a TTY' in result.stderr:
                print("\n💡 Railway CLI requires an interactive terminal")
                print("   Run this command directly in your terminal:")
                print(f"   railway logs --lines {lines}")
            return
        
        # Display logs
        print(result.stdout)
                
    except Exception as e:
        print(f"Error fetching Railway logs: {e}")


def cmd_db_stats(args, client):
    """Show database statistics - table sizes and recent activity."""
    print("\n📊 Database Statistics:\n")
    
    tables = {
        'discord_messages': 'Messages',
        'discord_channels': 'Channels',
        'discord_members': 'Members',
        'daily_summaries': 'Daily Summaries',
        'system_logs': 'System Logs',
        'shared_content': 'Shared Content'
    }
    
    print("Table Sizes:")
    for table, name in tables.items():
        try:
            result = client.table(table).select('*', count='exact').limit(1).execute()
            count = result.count if result.count is not None else 0
            print(f"  {name:20} {count:>10,} rows")
        except Exception as e:
            print(f"  {name:20} {'Error':>10}")
    
    print("\nRecent Activity (last 24 hours):")
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    
    # Messages by creation time
    try:
        msgs = client.table('discord_messages').select('message_id', count='exact').gte('created_at', cutoff).execute()
        print(f"  New messages:        {msgs.count:>10,}")
    except:
        print(f"  New messages:        {'Error':>10}")
    
    # Messages by index time (when archived)
    try:
        indexed = client.table('discord_messages').select('message_id', count='exact').gte('indexed_at', cutoff).execute()
        print(f"  Messages archived:   {indexed.count:>10,}")
    except:
        print(f"  Messages archived:   {'Error':>10}")
    
    # Summaries
    try:
        sums = client.table('daily_summaries').select('id', count='exact').gte('created_at', cutoff).execute()
        print(f"  New summaries:       {sums.count:>10,}")
    except:
        print(f"  New summaries:       {'Error':>10}")
    
    # Errors
    try:
        errs = client.table('system_logs').select('id', count='exact').gte('timestamp', cutoff).eq('level', 'ERROR').execute()
        print(f"  Errors logged:       {errs.count:>10,}")
    except:
        print(f"  Errors logged:       {'Error':>10}")


def cmd_archive_status(args, client):
    """Check archive status - messages indexed vs created."""
    print("\n📦 Archive Status:\n")
    
    from datetime import datetime, timedelta
    
    # Check different time windows
    windows = [
        (1, "Last 1 hour"),
        (6, "Last 6 hours"),
        (24, "Last 24 hours")
    ]
    
    print("Messages Created vs Archived:\n")
    for hours, label in windows:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        
        try:
            # Messages created in this window
            created = client.table('discord_messages').select('message_id', count='exact').gte('created_at', cutoff).execute()
            
            # Messages indexed in this window
            indexed = client.table('discord_messages').select('message_id', count='exact').gte('indexed_at', cutoff).execute()
            
            status = "✅" if indexed.count >= created.count else "⚠️"
            print(f"  {status} {label:15} Created: {created.count:>4}  Archived: {indexed.count:>4}")
        except Exception as e:
            print(f"  ❌ {label:15} Error: {e}")
    
    # Most recent archive activity
    print("\n🕐 Recent Archive Activity:\n")
    try:
        recent = client.table('discord_messages').select('indexed_at, channel_id').order('indexed_at', desc=True).limit(5).execute()
        
        if recent.data:
            for msg in recent.data:
                indexed = msg['indexed_at'][:19].replace('T', ' ')
                channel = msg['channel_id']
                print(f"  {indexed} - Channel {channel}")
        else:
            print("  No recent archive activity found")
    except Exception as e:
        print(f"  Error: {e}")
    
    # Messages by channel (top 5)
    print("\n📊 Messages by Channel (last 24h):\n")
    try:
        cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        msgs = client.table('discord_messages').select('channel_id, message_id').gte('created_at', cutoff_24h).execute()
        
        channels = {}
        for msg in msgs.data:
            ch = msg['channel_id']
            channels[ch] = channels.get(ch, 0) + 1
        
        for ch, count in sorted(channels.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"  Channel {ch}: {count:>4} messages")
    except Exception as e:
        print(f"  Error: {e}")


def cmd_bot_status(args):
    """Check bot status via health endpoint."""
    print("\n🤖 Bot Status:\n")
    
    try:
        import requests
        import subprocess
        from datetime import datetime
        
        # Get service URL
        result = subprocess.run(
            ['railway', 'domain'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            print("❌ Could not get Railway domain")
            return
        
        # Parse URL
        output = result.stdout.strip()
        if 'https://' in output:
            import re
            match = re.search(r'https://[^\s]+', output)
            service_url = match.group(0) if match else None
        else:
            service_url = output
        
        if not service_url:
            print("❌ No service URL found")
            return
        
        # Get status
        try:
            response = requests.get(f"{service_url}/status", timeout=5)
            data = response.json()
            
            status_emoji = "✅" if data.get('status') == 'ready' else "⏳"
            print(f"{status_emoji} Status: {data.get('status', 'unknown')}")
            print(f"📦 Deployment: {data.get('deployment_id', 'unknown')[:12]}...")
            
            if data.get('startup_time'):
                startup = datetime.fromisoformat(data['startup_time'].replace('Z', '+00:00'))
                uptime = datetime.utcnow().replace(tzinfo=startup.tzinfo) - startup
                hours = int(uptime.total_seconds() / 3600)
                minutes = int((uptime.total_seconds() % 3600) / 60)
                print(f"⏱️  Uptime: {hours}h {minutes}m")
            
            if data.get('metrics'):
                metrics = data['metrics']
                print(f"\n📈 Activity:")
                print(f"   Messages logged:   {metrics.get('messages_logged', 0):>6,}")
                print(f"   Messages archived: {metrics.get('messages_archived', 0):>6,}")
                print(f"   Errors:            {metrics.get('errors_logged', 0):>6,}")
            
            if data.get('last_heartbeat'):
                heartbeat = datetime.fromisoformat(data['last_heartbeat'].replace('Z', '+00:00'))
                now = datetime.utcnow().replace(tzinfo=heartbeat.tzinfo)
                seconds_ago = (now - heartbeat).total_seconds()
                print(f"\n💓 Last heartbeat: {int(seconds_ago)}s ago")
                
        except requests.Timeout:
            print("⏱️  Request timed out - bot may be starting")
        except requests.RequestException as e:
            print(f"❌ Error connecting to bot: {e}")
    
    except ImportError:
        print("⚠️  Install 'requests' to check bot status:")
        print("   pip install requests")
    except Exception as e:
        print(f"❌ Error: {e}")


def cmd_channel_info(args, client):
    """Get details about a specific channel by ID."""
    if not args.channel_id:
        print("Error: channel-info requires a channel ID")
        sys.exit(1)
    
    channel_id = int(args.channel_id)
    
    # Get channel from DB
    results = query_table(client, "discord_channels", {"channel_id": channel_id}, limit=1)
    
    if results:
        print(f"\n📺 Channel {channel_id}:\n")
        for k, v in results[0].items():
            print(f"  {k}: {v}")
    else:
        print(f"\n❌ Channel {channel_id} not found in database")
    
    # Check if it's referenced in env vars
    print(f"\n🔍 Env var references:")
    env_matches = []
    for key in ["SUMMARY_CHANNEL_ID", "TOP_GENS_ID", "ART_CHANNEL_ID", 
                "DEV_SUMMARY_CHANNEL_ID", "DEV_TOP_GENS_ID", "DEV_ART_CHANNEL_ID"]:
        val = os.getenv(key)
        if val and str(channel_id) in val:
            env_matches.append(key)
    
    if env_matches:
        for match in env_matches:
            print(f"  - {match}")
    else:
        print("  (not referenced in any env vars)")
    print()


def cmd_query(args, client):
    """Query a table."""
    table_map = {
        "messages": "discord_messages",
        "channels": "discord_channels", 
        "members": "discord_members",
        "logs": "discord_logs",
        "summaries": "daily_summaries",
    }
    
    table = table_map.get(args.command, args.command)
    
    filters = {}
    if args.channel:
        filters["channel_id"] = args.channel
    if args.message:
        filters["message_id"] = args.message
        
    # Determine order column
    order_col = None
    if table in ["discord_messages", "discord_logs"]:
        order_col = "created_at"
    elif table == "daily_summaries":
        order_col = "summary_date"
        
    results = query_table(client, table, filters, args.limit, order_col)
    
    # Apply hours filter for logs
    if args.hours and table == "discord_logs":
        cutoff = datetime.utcnow() - timedelta(hours=args.hours)
        results = [r for r in results if r.get("created_at", "") >= cutoff.isoformat()]
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Debug utility for investigating bot issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  env                 Show environment configuration
  bot-status          Check bot status and uptime via health endpoint
  archive-status      Check archive status (messages created vs archived)
  db-stats            Database statistics and recent activity
  channel-info ID     Details about a specific channel
  railway-status      Check Railway service status and health endpoints
  deployments         Analyze Railway deployment history (duplicates, crashes)
  railway-logs        Fetch Railway platform logs (deployments, restarts)
  channels            List channels from database
  messages            List messages (use --channel to filter)
  members             List members
  logs                List logs from Supabase (use --hours to filter)
  summaries           List daily summaries

Tip: For detailed log analysis, use scripts/logs.py
        """
    )
    parser.add_argument("command", help="Command to run")
    parser.add_argument("channel_id", nargs="?", help="Channel ID for channel-info command")
    parser.add_argument("--channel", type=int, help="Filter by channel_id")
    parser.add_argument("--message", type=int, help="Filter by message_id")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Limit results")
    parser.add_argument("--hours", type=int, help="Filter to last N hours")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    args = parser.parse_args()
    
    # Commands that don't need Supabase
    if args.command == "env":
        cmd_env(args)
        return
    
    if args.command == "bot-status":
        cmd_bot_status(args)
        return
    
    if args.command == "railway-status":
        cmd_railway_status(args)
        return
    
    if args.command == "deployments":
        cmd_deployments(args)
        return
    
    if args.command == "railway-logs":
        cmd_railway_logs(args)
        return
    
    # Commands that need Supabase
    client = get_client()
    
    if args.command == "archive-status":
        cmd_archive_status(args, client)
        return
    
    if args.command == "db-stats":
        cmd_db_stats(args, client)
        return
    
    if args.command == "channel-info":
        cmd_channel_info(args, client)
        return
    
    # Table queries
    table_commands = ["messages", "channels", "members", "logs", "summaries"]
    if args.command in table_commands or args.command.startswith("discord_"):
        results = cmd_query(args, client)
    else:
        try:
            results = query_table(client, args.command, limit=args.limit)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Available commands: env, channel-info, {', '.join(table_commands)}")
            sys.exit(1)
    
    # Output
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        if not results:
            print("No results found.")
        else:
            print(f"\n📊 Found {len(results)} results:\n")
            for i, row in enumerate(results, 1):
                print(f"--- {i} ---")
                for k, v in format_row(row).items():
                    print(f"  {k}: {v}")
                print()


if __name__ == "__main__":
    main()
