#!/usr/bin/env python3
"""
Simple Supabase query utility for debugging.

Usage:
    python scripts/query_supabase.py "SELECT * FROM discord_messages LIMIT 5"
    python scripts/query_supabase.py channels              # List all channels
    python scripts/query_supabase.py messages --channel 123 --limit 10
    python scripts/query_supabase.py members --limit 20
    python scripts/query_supabase.py logs --hours 1
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

def raw_query(client, sql):
    """Execute raw SQL via RPC (requires a function in Supabase)."""
    # For now, we'll parse simple SELECT queries
    print(f"Note: Raw SQL not directly supported. Use table commands instead.")
    print(f"Query: {sql}")
    return []

def main():
    parser = argparse.ArgumentParser(description="Query Supabase for debugging")
    parser.add_argument("command", help="Table name or 'sql' for raw query")
    parser.add_argument("query", nargs="?", help="SQL query if command is 'sql'")
    parser.add_argument("--channel", type=int, help="Filter by channel_id")
    parser.add_argument("--message", type=int, help="Filter by message_id")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Limit results")
    parser.add_argument("--hours", type=int, help="Filter to last N hours")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    args = parser.parse_args()
    client = get_client()
    
    # Map shorthand commands to tables
    table_map = {
        "messages": "discord_messages",
        "channels": "discord_channels", 
        "members": "discord_members",
        "logs": "discord_logs",
        "summaries": "daily_summaries",
    }
    
    if args.command == "sql":
        results = raw_query(client, args.query)
    elif args.command in table_map or args.command.startswith("discord_"):
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
    else:
        # Try as a table name directly
        try:
            results = query_table(client, args.command, limit=args.limit)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Available commands: {', '.join(table_map.keys())}")
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
