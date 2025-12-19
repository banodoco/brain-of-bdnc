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
    python scripts/debug.py logs --hours 1        # Recent logs
    python scripts/debug.py summaries             # Recent summaries
    python scripts/debug.py channel-info ID       # Details about a specific channel
"""

import argparse
import json
import os
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
  channel-info ID     Details about a specific channel
  channels            List channels from database
  messages            List messages (use --channel to filter)
  members             List members
  logs                List logs (use --hours to filter)
  summaries           List daily summaries
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
    
    # Commands that need Supabase
    client = get_client()
    
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
