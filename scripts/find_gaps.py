#!/usr/bin/env python3
"""
Analyze message data for gaps on an hourly basis.
Identifies periods with suspiciously low message counts that may indicate
incomplete archiving (e.g., from scraper restarts).

Usage:
    python scripts/find_gaps.py              # Normal mode
    python scripts/find_gaps.py --paranoid   # Aggressive detection (recommended for 100% completeness)
"""

import os
import sys
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client
from datetime import datetime, timedelta
from collections import defaultdict
import statistics

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--paranoid', action='store_true', help='Aggressive gap detection for 100% completeness')
args = parser.parse_args()

PARANOID_MODE = args.paranoid

def fetch_all_messages():
    """Fetch all message timestamps with pagination."""
    supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
    
    print("Fetching all message timestamps (this may take a few minutes)...")
    all_messages = []
    offset = 0
    batch_size = 1000
    
    while True:
        batch = supabase.table('discord_messages').select('created_at').order('created_at').range(offset, offset + batch_size - 1).execute()
        if not batch.data:
            break
        all_messages.extend(batch.data)
        offset += batch_size
        if offset % 100000 == 0:
            print(f"  Fetched {len(all_messages):,} messages...")
        if len(batch.data) < batch_size:
            break
    
    print(f"Total messages: {len(all_messages):,}\n")
    return all_messages

def analyze_hourly(messages):
    """Group messages by hour and analyze for gaps."""
    
    if PARANOID_MODE:
        print("🔴 PARANOID MODE: Aggressive gap detection enabled")
        print("   - Threshold: 30% of median (vs 10% in normal mode)")
        print("   - Consecutive hours: 2+ (vs 3+ in normal mode)")
        print("   - Includes hours 5am+ (vs 7am+ in normal mode)")
        print()
    
    # Group by hour
    hourly_counts = defaultdict(int)
    for msg in messages:
        dt = datetime.fromisoformat(msg['created_at'].replace('Z', '+00:00'))
        hour_key = dt.strftime('%Y-%m-%d %H:00')
        hourly_counts[hour_key] += 1
    
    # Get all hours in range
    sorted_hours = sorted(hourly_counts.keys())
    if not sorted_hours:
        print("No messages found!")
        return []
    
    start = datetime.strptime(sorted_hours[0], '%Y-%m-%d %H:00')
    end = datetime.strptime(sorted_hours[-1], '%Y-%m-%d %H:00')
    
    # Fill in missing hours with 0
    current = start
    all_hours = {}
    while current <= end:
        hour_key = current.strftime('%Y-%m-%d %H:00')
        all_hours[hour_key] = hourly_counts.get(hour_key, 0)
        current += timedelta(hours=1)
    
    # Calculate statistics by hour of day (to account for daily patterns)
    hour_of_day_stats = defaultdict(list)
    for hour_key, count in all_hours.items():
        hour_of_day = int(hour_key.split(' ')[1].split(':')[0])
        # Skip first month (server startup) for statistics
        if hour_key >= '2023-09-01':
            hour_of_day_stats[hour_of_day].append(count)
    
    # Calculate median for each hour of day
    hour_medians = {}
    for hour, counts in hour_of_day_stats.items():
        if len(counts) > 10:
            hour_medians[hour] = statistics.median(counts)
        else:
            hour_medians[hour] = 10  # default
    
    # Settings based on mode
    if PARANOID_MODE:
        threshold_pct = 0.30  # 30% of median
        min_consecutive = 2   # 2+ hours
        min_hour = 5          # Include 5am+
    else:
        threshold_pct = 0.10  # 10% of median
        min_consecutive = 3   # 3+ hours
        min_hour = 7          # 7am+
    
    # Find suspicious hours
    suspicious = []
    sorted_all_hours = sorted(all_hours.items())
    
    # Skip first month (Aug 2023 - server startup)
    # Skip known holidays (only in normal mode)
    holidays = ['2023-12-25', '2023-12-31', '2024-01-01', '2024-12-25', '2024-12-31', 
                '2025-01-01', '2025-12-25', '2025-12-31', '2026-01-01']
    
    i = 0
    while i < len(sorted_all_hours):
        hour_key, count = sorted_all_hours[i]
        date_str = hour_key.split(' ')[0]
        hour_of_day = int(hour_key.split(' ')[1].split(':')[0])
        
        # Skip early data (server startup)
        if hour_key < '2023-09-01':
            i += 1
            continue
        
        # Skip holidays (only in normal mode - paranoid checks everything)
        if not PARANOID_MODE and date_str in holidays:
            i += 1
            continue
            
        # Skip overnight hours
        if hour_of_day < min_hour:
            i += 1
            continue
        
        median = hour_medians.get(hour_of_day, 10)
        
        # Check if this starts a suspicious period
        if count < median * threshold_pct:
            # Look ahead to see how long the low period lasts
            low_period_start = i
            low_period_end = i
            
            for j in range(i + 1, min(len(sorted_all_hours), i + 24)):  # Look up to 24 hours ahead
                next_key, next_count = sorted_all_hours[j]
                next_hour = int(next_key.split(' ')[1].split(':')[0])
                next_median = hour_medians.get(next_hour, 10)
                
                if next_count < next_median * threshold_pct and next_hour >= min_hour:
                    low_period_end = j
                else:
                    break
            
            # Flag based on consecutive hour requirement
            if low_period_end - low_period_start >= (min_consecutive - 1):
                for k in range(low_period_start, low_period_end + 1):
                    h_key, h_count = sorted_all_hours[k]
                    h_hour = int(h_key.split(' ')[1].split(':')[0])
                    suspicious.append({
                        'hour': h_key,
                        'count': h_count,
                        'threshold': hour_medians.get(h_hour, 10) * threshold_pct,
                        'surrounding_avg': hour_medians.get(h_hour, 10)
                    })
                i = low_period_end + 1
                continue
        
        i += 1
    
    return suspicious, all_hours, hour_medians

def find_gap_ranges(suspicious_hours):
    """Group consecutive suspicious hours into date ranges."""
    if not suspicious_hours:
        return []
    
    ranges = []
    current_start = None
    current_end = None
    
    for item in sorted(suspicious_hours, key=lambda x: x['hour']):
        hour = datetime.strptime(item['hour'], '%Y-%m-%d %H:00')
        
        if current_start is None:
            current_start = hour
            current_end = hour
        elif hour - current_end <= timedelta(hours=2):
            # Extend current range
            current_end = hour
        else:
            # Save current range and start new one
            ranges.append((current_start, current_end))
            current_start = hour
            current_end = hour
    
    # Don't forget the last range
    if current_start:
        ranges.append((current_start, current_end))
    
    return ranges

def main():
    messages = fetch_all_messages()
    suspicious, all_hours, thresholds = analyze_hourly(messages)
    
    # Summary stats
    counts = list(all_hours.values())
    print("=== HOURLY STATISTICS ===")
    print(f"Total hours in range: {len(all_hours):,}")
    print(f"Average: {sum(counts)/len(counts):.1f} messages/hour")
    print(f"Median: {statistics.median(counts):.1f} messages/hour")
    print(f"Min: {min(counts)} | Max: {max(counts)}")
    print()
    
    # Show thresholds by hour of day
    print("=== THRESHOLDS BY HOUR OF DAY ===")
    print("(20% of median for that hour - accounts for quiet overnight periods)")
    for hour in range(24):
        if hour in thresholds:
            print(f"  {hour:02d}:00 - threshold: {thresholds[hour]:.1f}")
    print()
    
    if not suspicious:
        print("=== NO SUSPICIOUS GAPS FOUND ===")
        print("All hours have reasonable message counts relative to surrounding periods!")
        return
    
    print(f"=== SUSPICIOUS HOURS ({len(suspicious)} found) ===")
    for item in suspicious[:30]:  # Show first 30
        print(f"  {item['hour']}: {item['count']} msgs (threshold: {item['threshold']:.1f}, surrounding avg: {item['surrounding_avg']:.1f})")
    
    if len(suspicious) > 30:
        print(f"  ... and {len(suspicious) - 30} more")
    print()
    
    # Group into ranges
    ranges = find_gap_ranges(suspicious)
    
    print(f"=== GAP RANGES TO RE-ARCHIVE ({len(ranges)} found) ===")
    for start, end in ranges:
        # Expand range by 1 day on each side for safety
        safe_start = (start - timedelta(days=1)).strftime('%Y-%m-%d')
        safe_end = (end + timedelta(days=1)).strftime('%Y-%m-%d')
        duration = end - start
        print(f"\n  Gap: {start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%Y-%m-%d %H:%M')} ({duration})")
        print(f"  Command to fix:")
        print(f"    python scripts/archive_discord.py --start-date {safe_start} --end-date {safe_end}")
    
    # Consolidate ranges by month for easier re-archiving
    if PARANOID_MODE and ranges:
        print()
        print("=== CONSOLIDATED BY MONTH (for bulk re-archive) ===")
        months_with_gaps = set()
        for start, end in ranges:
            # Add all months covered by this gap
            current = start.replace(day=1)
            end_month = end.replace(day=1)
            while current <= end_month:
                months_with_gaps.add(current.strftime('%Y-%m'))
                # Move to next month
                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1)
                else:
                    current = current.replace(month=current.month + 1)
        
        # Group consecutive months
        sorted_months = sorted(months_with_gaps)
        consolidated = []
        if sorted_months:
            range_start = sorted_months[0]
            range_end = sorted_months[0]
            
            for month in sorted_months[1:]:
                # Check if consecutive
                prev_year, prev_month = int(range_end[:4]), int(range_end[5:7])
                curr_year, curr_month = int(month[:4]), int(month[5:7])
                
                expected_next = f"{prev_year}-{prev_month+1:02d}" if prev_month < 12 else f"{prev_year+1}-01"
                
                if month == expected_next:
                    range_end = month
                else:
                    consolidated.append((range_start, range_end))
                    range_start = month
                    range_end = month
            
            consolidated.append((range_start, range_end))
        
        print(f"\nRun these {len(consolidated)} commands to ensure 100% completeness:\n")
        for start_month, end_month in consolidated:
            # Calculate date range
            start_date = f"{start_month}-01"
            # End of month
            end_year, end_m = int(end_month[:4]), int(end_month[5:7])
            if end_m == 12:
                end_date = f"{end_year + 1}-01-01"
            else:
                end_date = f"{end_year}-{end_m + 1:02d}-01"
            
            print(f"python scripts/archive_discord.py --start-date {start_date} --end-date {end_date}")
    
    print()
    print("=== RECOMMENDED NEXT STEPS ===")
    print("1. Wait for current archive to finish")
    print("2. Run this script again to get final gap analysis")
    if PARANOID_MODE:
        print("3. Run the CONSOLIDATED commands above (much faster than individual gaps)")
    else:
        print("3. Run the suggested commands above to fill any gaps")
    print("4. Re-run this script to verify completeness")

if __name__ == "__main__":
    main()
