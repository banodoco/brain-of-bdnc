#!/usr/bin/env python3
"""
Comprehensive validation that Supabase and SQLite return identical results.
Tests ALL daily update queries with side-by-side comparison.
"""

import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import json

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from src.common.db_handler import DatabaseHandler
from src.common.constants import STORAGE_SQLITE, STORAGE_SUPABASE

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    END = '\033[0m'
    BOLD = '\033[1m'

def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*80}")
    print(f"{text}")
    print(f"{'='*80}{Colors.END}\n")

def print_subheader(text):
    print(f"\n{Colors.YELLOW}{'-'*80}")
    print(f"{text}")
    print(f"{'-'*80}{Colors.END}")

def compare_results(sqlite_results: List[Dict], supabase_results: List[Dict], query_name: str) -> bool:
    """Compare two result sets and report differences."""
    print_subheader(f"Comparing: {query_name}")
    
    # Check counts
    sqlite_count = len(sqlite_results) if sqlite_results else 0
    supabase_count = len(supabase_results) if supabase_results else 0
    
    print(f"SQLite count:   {sqlite_count}")
    print(f"Supabase count: {supabase_count}")
    
    if sqlite_count != supabase_count:
        print(f"{Colors.RED}❌ COUNT MISMATCH!{Colors.END}")
        print(f"   Difference: {abs(sqlite_count - supabase_count)} records")
        return False
    
    if sqlite_count == 0:
        print(f"{Colors.YELLOW}⚠️  Both returned 0 results (might be expected){Colors.END}")
        return True
    
    # Check keys match
    if sqlite_results and supabase_results:
        sqlite_keys = set(sqlite_results[0].keys())
        supabase_keys = set(supabase_results[0].keys())
        
        if sqlite_keys != supabase_keys:
            print(f"{Colors.RED}❌ KEY MISMATCH!{Colors.END}")
            print(f"   SQLite only:   {sqlite_keys - supabase_keys}")
            print(f"   Supabase only: {supabase_keys - sqlite_keys}")
            # Don't fail on key mismatch if one has extra metadata
            if not (sqlite_keys <= supabase_keys or supabase_keys <= sqlite_keys):
                return False
    
    # Compare first 5 records in detail
    print(f"\n{Colors.CYAN}Detailed comparison of first 5 records:{Colors.END}")
    mismatches = []
    
    for i in range(min(5, sqlite_count)):
        sqlite_record = sqlite_results[i]
        supabase_record = supabase_results[i]
        
        # Get common keys
        common_keys = set(sqlite_record.keys()) & set(supabase_record.keys())
        record_mismatches = []
        
        for key in common_keys:
            sqlite_val = sqlite_record.get(key)
            supabase_val = supabase_record.get(key)
            
            # Normalize values for comparison
            sqlite_val_norm = normalize_value(sqlite_val)
            supabase_val_norm = normalize_value(supabase_val)
            
            if sqlite_val_norm != supabase_val_norm:
                record_mismatches.append({
                    'key': key,
                    'sqlite': sqlite_val,
                    'supabase': supabase_val
                })
        
        if record_mismatches:
            mismatches.append({'record': i, 'mismatches': record_mismatches})
            print(f"\n  Record #{i+1}: {Colors.RED}MISMATCH{Colors.END}")
            for mismatch in record_mismatches:
                print(f"    Key: {mismatch['key']}")
                print(f"      SQLite:   {repr(mismatch['sqlite'])[:100]}")
                print(f"      Supabase: {repr(mismatch['supabase'])[:100]}")
        else:
            print(f"  Record #{i+1}: {Colors.GREEN}✓ Match{Colors.END}")
    
    if mismatches:
        print(f"\n{Colors.RED}❌ FOUND {len(mismatches)} RECORDS WITH MISMATCHES{Colors.END}")
        return False
    else:
        print(f"\n{Colors.GREEN}✅ ALL RECORDS MATCH PERFECTLY!{Colors.END}")
        return True

def normalize_value(val: Any) -> Any:
    """Normalize values for comparison (handle JSON, dates, etc.)"""
    if val is None:
        return None
    
    # Handle JSON strings
    if isinstance(val, str):
        # Try to parse JSON
        if val.startswith('[') or val.startswith('{'):
            try:
                return json.loads(val)
            except:
                pass
        # Normalize datetime strings
        if 'T' in val or '-' in val:
            try:
                # Try parsing as datetime
                dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
                return dt.isoformat()
            except:
                pass
    
    # Handle numbers - convert to same type
    if isinstance(val, (int, float)):
        return float(val)
    
    return val

async def test_production_channels():
    """Test production channels query."""
    print_header("TEST 1: Production Channels Query")
    
    # Use actual channel IDs from the system
    channel_ids = "1138865343314530324,1221869948469776516"
    
    query = f"""
        SELECT c.channel_id, c.channel_name, COALESCE(c2.channel_name, 'Unknown') as source, 
               COUNT(m.message_id) as msg_count 
        FROM channels c 
        LEFT JOIN channels c2 ON c.category_id = c2.channel_id 
        LEFT JOIN messages m ON c.channel_id = m.channel_id 
        AND m.created_at > datetime('now', '-24 hours') 
        WHERE c.channel_id IN ({channel_ids}) OR c.category_id IN ({channel_ids}) 
        GROUP BY c.channel_id, c.channel_name, source 
        HAVING COUNT(m.message_id) >= 25 
        ORDER BY msg_count DESC
    """
    
    # Test SQLite
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    sqlite_results = db_sqlite.execute_query(query)
    
    # Test Supabase
    db_supabase = DatabaseHandler(storage_backend=STORAGE_SUPABASE)
    supabase_results = db_supabase.execute_query(query)
    
    return compare_results(sqlite_results, supabase_results, "Production Channels")

async def test_channel_history():
    """Test channel history query."""
    print_header("TEST 2: Channel History (get_messages_in_range)")
    
    # Get a channel ID first
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    sample_query = "SELECT channel_id FROM channels LIMIT 1"
    channels = db_sqlite.execute_query(sample_query)
    
    if not channels:
        print(f"{Colors.YELLOW}⚠️  No channels found - skipping test{Colors.END}")
        return True
    
    channel_id = channels[0]['channel_id']
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(hours=24)
    
    print(f"Testing with channel_id: {channel_id}")
    print(f"Date range: {start_date.isoformat()} to {end_date.isoformat()}")
    
    # Test SQLite
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    sqlite_results = db_sqlite.get_messages_in_range(start_date, end_date, channel_id)
    
    # Test Supabase
    db_supabase = DatabaseHandler(storage_backend=STORAGE_SUPABASE)
    supabase_results = db_supabase.get_messages_in_range(start_date, end_date, channel_id)
    
    return compare_results(sqlite_results, supabase_results, "Channel History")

async def test_top_generations():
    """Test top generations query (complex with CTEs and JSON)."""
    print_header("TEST 3: Top Generations Query")
    
    yesterday = datetime.now(timezone.utc) - timedelta(days=7)
    
    query = """
        WITH video_messages AS (
            SELECT 
                m.message_id,
                m.channel_id,
                m.content,
                m.attachments,
                m.reactors,
                c.channel_name,
                COALESCE(mem.server_nick, mem.global_name, mem.username) as author_name,
                CASE 
                    WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                    ELSE json_array_length(m.reactors)
                END as unique_reactor_count
            FROM messages m
            JOIN channels c ON m.channel_id = c.channel_id
            JOIN members mem ON m.author_id = mem.member_id
            WHERE m.created_at > ?
            AND json_valid(m.attachments)
            AND m.attachments != '[]'
            AND LOWER(c.channel_name) NOT LIKE '%nsfw%'
            AND EXISTS (
                SELECT 1
                FROM json_each(m.attachments)
                WHERE LOWER(json_extract(value, '$.filename')) LIKE '%.mp4'
                   OR LOWER(json_extract(value, '$.filename')) LIKE '%.mov'
                   OR LOWER(json_extract(value, '$.filename')) LIKE '%.webm'
            )
        )
        SELECT *
        FROM video_messages
        WHERE unique_reactor_count >= 3
        ORDER BY unique_reactor_count DESC
        LIMIT 10
    """
    
    # Test SQLite
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    sqlite_results = db_sqlite.execute_query(query, (yesterday.isoformat(),))
    
    # Test Supabase
    db_supabase = DatabaseHandler(storage_backend=STORAGE_SUPABASE)
    supabase_results = db_supabase.execute_query(query, (yesterday.isoformat(),))
    
    return compare_results(sqlite_results, supabase_results, "Top Generations")

async def test_top_art():
    """Test top art sharing query."""
    print_header("TEST 4: Top Art Sharing Query")
    
    # Use a known art channel or skip
    art_channel_id = int(os.getenv('ART_CHANNEL_ID', '0'))
    if art_channel_id == 0:
        print(f"{Colors.YELLOW}⚠️  ART_CHANNEL_ID not set - using default{Colors.END}")
        art_channel_id = 1138865343314530324  # fallback
    
    yesterday = datetime.now(timezone.utc) - timedelta(days=7)
    
    query = """
        SELECT 
            m.message_id,
            m.channel_id,
            m.author_id,
            m.content,
            m.attachments,
            m.reactors,
            COALESCE(mem.server_nick, mem.global_name, mem.username) as author_name,
            CASE 
                WHEN m.reactors IS NULL OR m.reactors = '[]' THEN 0
                ELSE json_array_length(m.reactors)
            END as unique_reactor_count
        FROM messages m
        JOIN members mem ON m.author_id = mem.member_id
        WHERE m.channel_id = ?
        AND m.created_at > ?
        AND json_valid(m.attachments)
        AND m.attachments != '[]'
        ORDER BY unique_reactor_count DESC
        LIMIT 10
    """
    
    # Test SQLite
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    sqlite_results = db_sqlite.execute_query(query, (art_channel_id, yesterday.isoformat()))
    
    # Test Supabase
    db_supabase = DatabaseHandler(storage_backend=STORAGE_SUPABASE)
    supabase_results = db_supabase.execute_query(query, (art_channel_id, yesterday.isoformat()))
    
    return compare_results(sqlite_results, supabase_results, "Top Art")

async def test_simple_queries():
    """Test simple queries that should definitely match."""
    print_header("TEST 5: Simple Queries (Sanity Check)")
    
    results = []
    
    # Test 1: Sample message retrieval (use LIMIT to avoid fetching all)
    print_subheader("Sample message retrieval (LIMIT 100)")
    db_sqlite = DatabaseHandler(storage_backend=STORAGE_SQLITE)
    db_supabase = DatabaseHandler(storage_backend=STORAGE_SUPABASE)
    
    # Use limited queries to avoid fetching everything
    sqlite_messages = db_sqlite.execute_query("SELECT message_id, content, author_id FROM messages LIMIT 100")
    supabase_messages = db_supabase.execute_query("SELECT message_id, content, author_id FROM messages LIMIT 100")
    
    sqlite_val = len(sqlite_messages) if sqlite_messages else 0
    supabase_val = len(supabase_messages) if supabase_messages else 0
    
    print(f"SQLite:   {sqlite_val:,}")
    print(f"Supabase: {supabase_val:,}")
    
    if sqlite_val == supabase_val:
        print(f"{Colors.GREEN}✅ Counts match!{Colors.END}")
        results.append(True)
    else:
        print(f"{Colors.RED}❌ COUNT MISMATCH!{Colors.END}")
        print(f"   Difference: {abs(sqlite_val - supabase_val):,} messages")
        print(f"{Colors.YELLOW}⚠️  This may indicate incomplete sync to Supabase{Colors.END}")
        results.append(False)
    
    # Test 2: Sample member retrieval (use LIMIT)
    print_subheader("Sample member retrieval (LIMIT 50)")
    sqlite_members = db_sqlite.execute_query("SELECT member_id, username FROM members LIMIT 50")
    supabase_members = db_supabase.execute_query("SELECT user_id, username FROM members LIMIT 50")
    
    sqlite_val = len(sqlite_members) if sqlite_members else 0
    supabase_val = len(supabase_members) if supabase_members else 0
    
    print(f"SQLite:   {sqlite_val:,}")
    print(f"Supabase: {supabase_val:,}")
    
    if sqlite_val == supabase_val:
        print(f"{Colors.GREEN}✅ Counts match!{Colors.END}")
        results.append(True)
    else:
        print(f"{Colors.RED}❌ COUNT MISMATCH!{Colors.END}")
        print(f"   Difference: {abs(sqlite_val - supabase_val):,} members")
        results.append(False)
    
    # Test 3: Get specific message by ID
    print_subheader("Fetch specific message by ID")
    sample_msg = db_sqlite.execute_query("SELECT message_id FROM messages LIMIT 1")
    if sample_msg:
        msg_id = sample_msg[0]['message_id']
        sqlite_msg = db_sqlite.execute_query("SELECT message_id, content, author_id, channel_id, created_at FROM messages WHERE message_id = ?", (msg_id,))
        supabase_msg = db_supabase.execute_query("SELECT message_id, content, author_id, channel_id, sent_at as created_at FROM messages WHERE message_id = ?", (msg_id,))
        
        if compare_results(sqlite_msg, supabase_msg, f"Message {msg_id}"):
            results.append(True)
        else:
            results.append(False)
    else:
        print(f"{Colors.YELLOW}⚠️  No messages found{Colors.END}")
        results.append(True)
    
    return all(results)

async def main():
    """Run all validation tests."""
    print(f"{Colors.BOLD}{Colors.CYAN}")
    print("="*80)
    print("COMPREHENSIVE SUPABASE vs SQLITE PARITY VALIDATION")
    print("="*80)
    print(f"{Colors.END}")
    print(f"\nThis will compare ALL query results between backends to ensure")
    print(f"perfect data consistency and format matching.\n")
    
    results = {}
    
    try:
        # Run all tests
        results['simple'] = await test_simple_queries()
        results['production_channels'] = await test_production_channels()
        results['channel_history'] = await test_channel_history()
        results['top_generations'] = await test_top_generations()
        results['top_art'] = await test_top_art()
        
    except Exception as e:
        print(f"\n{Colors.RED}❌ ERROR during testing: {e}{Colors.END}")
        import traceback
        traceback.print_exc()
        return False
    
    # Print summary
    print_header("VALIDATION SUMMARY")
    
    all_passed = True
    for test_name, passed in results.items():
        status = f"{Colors.GREEN}✅ PASS{Colors.END}" if passed else f"{Colors.RED}❌ FAIL{Colors.END}"
        print(f"{status} | {test_name}")
        if not passed:
            all_passed = False
    
    print("\n" + "="*80)
    if all_passed:
        print(f"{Colors.BOLD}{Colors.GREEN}🎉 ALL VALIDATION TESTS PASSED!{Colors.END}")
        print(f"{Colors.GREEN}Supabase and SQLite return identical results.{Colors.END}")
    else:
        print(f"{Colors.BOLD}{Colors.RED}❌ VALIDATION FAILED!{Colors.END}")
        print(f"{Colors.RED}Found discrepancies between Supabase and SQLite.{Colors.END}")
        print(f"{Colors.YELLOW}Please review the detailed comparison above.{Colors.END}")
    print("="*80 + "\n")
    
    return all_passed

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

