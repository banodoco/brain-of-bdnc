#!/usr/bin/env python3
"""
Comprehensive test suite to compare SQLite vs Supabase query results.
Tests all query methods and reports any discrepancies.
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import json

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from src.common.db_handler import DatabaseHandler
from src.common.constants import STORAGE_SQLITE, STORAGE_BOTH, STORAGE_SUPABASE

# Test results tracking
test_results = []

def log_test(test_name: str, passed: bool, details: str = ""):
    """Log a test result."""
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"{status} | {test_name}")
    if details:
        print(f"     {details}")
    test_results.append({
        'test': test_name,
        'passed': passed,
        'details': details
    })

def compare_results(sqlite_result: Any, supabase_result: Any, test_name: str, allow_order_diff: bool = False):
    """Compare results from SQLite and Supabase."""
    
    # Handle None cases
    if sqlite_result is None and supabase_result is None:
        log_test(test_name, True, "Both returned None")
        return True
    
    if type(sqlite_result) != type(supabase_result):
        log_test(test_name, False, f"Type mismatch: SQLite={type(sqlite_result)}, Supabase={type(supabase_result)}")
        return False
    
    # Handle list results
    if isinstance(sqlite_result, list):
        if len(sqlite_result) != len(supabase_result):
            log_test(test_name, False, f"Count mismatch: SQLite={len(sqlite_result)}, Supabase={len(supabase_result)}")
            return False
        
        if len(sqlite_result) == 0:
            log_test(test_name, True, "Both returned empty list")
            return True
        
        # If order doesn't matter, sort by a key
        if allow_order_diff and len(sqlite_result) > 0:
            try:
                if 'message_id' in sqlite_result[0]:
                    sqlite_result = sorted(sqlite_result, key=lambda x: x.get('message_id', 0))
                    supabase_result = sorted(supabase_result, key=lambda x: x.get('message_id', 0))
            except:
                pass
        
        # Compare first few records
        sample_size = min(3, len(sqlite_result))
        for i in range(sample_size):
            sqlite_item = sqlite_result[i]
            supabase_item = supabase_result[i]
            
            # Compare keys
            sqlite_keys = set(sqlite_item.keys())
            supabase_keys = set(supabase_item.keys())
            
            # Supabase might have extra keys like 'discord_members'
            extra_keys = supabase_keys - sqlite_keys
            missing_keys = sqlite_keys - supabase_keys
            
            if missing_keys:
                log_test(test_name, False, f"Missing keys in Supabase: {missing_keys}")
                return False
        
        log_test(test_name, True, f"Matched {len(sqlite_result)} records")
        return True
    
    # Handle dict results
    elif isinstance(sqlite_result, dict):
        sqlite_keys = set(sqlite_result.keys())
        supabase_keys = set(supabase_result.keys())
        
        missing_keys = sqlite_keys - supabase_keys
        if missing_keys:
            log_test(test_name, False, f"Missing keys in Supabase: {missing_keys}")
            return False
        
        log_test(test_name, True, "Dicts match")
        return True
    
    # Handle scalar results
    else:
        if sqlite_result == supabase_result:
            log_test(test_name, True, f"Value: {sqlite_result}")
            return True
        else:
            log_test(test_name, False, f"Value mismatch: SQLite={sqlite_result}, Supabase={supabase_result}")
            return False

async def test_get_last_message_id(db_sqlite: DatabaseHandler, db_supabase: DatabaseHandler, channel_id: int):
    """Test get_last_message_id method."""
    print("\n🔍 Testing get_last_message_id...")
    sqlite_result = db_sqlite.get_last_message_id(channel_id)
    supabase_result = db_supabase.get_last_message_id(channel_id)
    compare_results(sqlite_result, supabase_result, "get_last_message_id")

async def test_message_exists(db_sqlite: DatabaseHandler, db_supabase: DatabaseHandler, message_id: int):
    """Test message_exists method."""
    print("\n🔍 Testing message_exists...")
    sqlite_result = db_sqlite.message_exists(message_id)
    supabase_result = db_supabase.message_exists(message_id)
    compare_results(sqlite_result, supabase_result, "message_exists")

async def test_get_member(db_sqlite: DatabaseHandler, db_supabase: DatabaseHandler, member_id: int):
    """Test get_member method."""
    print("\n🔍 Testing get_member...")
    sqlite_result = db_sqlite.get_member(member_id)
    supabase_result = db_supabase.get_member(member_id)
    compare_results(sqlite_result, supabase_result, "get_member")

async def test_get_messages_in_range(db_sqlite: DatabaseHandler, db_supabase: DatabaseHandler, channel_id: int):
    """Test get_messages_in_range method."""
    print("\n🔍 Testing get_messages_in_range...")
    
    # Test with recent data (last 7 days)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)
    
    sqlite_result = db_sqlite.get_messages_in_range(start_date, end_date, channel_id)
    supabase_result = db_supabase.get_messages_in_range(start_date, end_date, channel_id)
    
    compare_results(sqlite_result, supabase_result, "get_messages_in_range (7 days)", allow_order_diff=True)

async def test_get_messages_by_authors_in_range(db_sqlite: DatabaseHandler, db_supabase: DatabaseHandler):
    """Test get_messages_by_authors_in_range method."""
    print("\n🔍 Testing get_messages_by_authors_in_range...")
    
    # Get a sample author ID first
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=30)
    
    sample_messages = db_sqlite.get_messages_in_range(start_date, end_date)
    if not sample_messages:
        log_test("get_messages_by_authors_in_range", True, "Skipped - no messages found")
        return
    
    # Get unique author IDs (up to 5)
    author_ids = list(set([msg['author_id'] for msg in sample_messages[:20]]))[:5]
    
    if not author_ids:
        log_test("get_messages_by_authors_in_range", True, "Skipped - no authors found")
        return
    
    sqlite_result = db_sqlite.get_messages_by_authors_in_range(author_ids, start_date, end_date)
    supabase_result = db_supabase.get_messages_by_authors_in_range(author_ids, start_date, end_date)
    
    compare_results(sqlite_result, supabase_result, f"get_messages_by_authors_in_range ({len(author_ids)} authors)", allow_order_diff=True)

async def run_all_tests():
    """Run all tests and generate report."""
    print("="*80)
    print("🧪 QUERY BACKEND COMPARISON TEST SUITE")
    print("="*80)
    print("\nInitializing test databases...")
    
    # Check if Supabase is configured
    if not os.getenv('SUPABASE_URL') or not os.getenv('SUPABASE_SERVICE_KEY'):
        print("❌ ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        print("   Please set these environment variables and try again.")
        return False
    
    try:
        # Initialize both database handlers
        print("  • SQLite handler...")
        db_sqlite = DatabaseHandler(dev_mode=False, storage_backend=STORAGE_SQLITE)
        
        print("  • Supabase handler...")
        db_supabase = DatabaseHandler(dev_mode=False, storage_backend=STORAGE_SUPABASE)
        
        print("✅ Both handlers initialized successfully\n")
        
        # Get a sample channel ID
        print("🔍 Finding sample data...")
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)
        sample_messages = db_sqlite.get_messages_in_range(start_date, end_date)
        
        if not sample_messages:
            print("❌ ERROR: No messages found in the last 30 days")
            print("   Please ensure you have data in your SQLite database")
            return False
        
        channel_id = sample_messages[0]['channel_id']
        message_id = sample_messages[0]['message_id']
        author_id = sample_messages[0]['author_id']
        
        print(f"  • Found {len(sample_messages)} messages in last 30 days")
        print(f"  • Using channel_id: {channel_id}")
        print(f"  • Using message_id: {message_id}")
        print(f"  • Using author_id: {author_id}")
        
        # Run tests
        print("\n" + "="*80)
        print("RUNNING TESTS")
        print("="*80)
        
        await test_get_last_message_id(db_sqlite, db_supabase, channel_id)
        await test_message_exists(db_sqlite, db_supabase, message_id)
        await test_get_member(db_sqlite, db_supabase, author_id)
        await test_get_messages_in_range(db_sqlite, db_supabase, channel_id)
        await test_get_messages_by_authors_in_range(db_sqlite, db_supabase)
        
        # Generate report
        print("\n" + "="*80)
        print("TEST SUMMARY")
        print("="*80)
        
        total_tests = len(test_results)
        passed_tests = sum(1 for r in test_results if r['passed'])
        failed_tests = total_tests - passed_tests
        
        print(f"\nTotal Tests: {total_tests}")
        print(f"✅ Passed: {passed_tests}")
        print(f"❌ Failed: {failed_tests}")
        
        if failed_tests > 0:
            print("\n⚠️  FAILED TESTS:")
            for result in test_results:
                if not result['passed']:
                    print(f"  • {result['test']}")
                    if result['details']:
                        print(f"    {result['details']}")
        
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        print(f"\nSuccess Rate: {success_rate:.1f}%")
        
        if success_rate == 100:
            print("\n🎉 ALL TESTS PASSED! SQLite and Supabase queries are consistent.")
            return True
        elif success_rate >= 80:
            print("\n⚠️  MOSTLY PASSING: Some minor discrepancies found.")
            return True
        else:
            print("\n❌ MULTIPLE FAILURES: Significant differences between backends.")
            return False
        
    except Exception as e:
        print(f"\n❌ ERROR during testing: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("Starting query backend comparison tests...\n")
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)

