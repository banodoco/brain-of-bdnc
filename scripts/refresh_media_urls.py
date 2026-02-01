#!/usr/bin/env python3
"""
Refresh expired Discord CDN URLs for top posts.

Discord attachment URLs expire after some time. This script:
1. Finds top N posts per month (by reaction count) that have media attachments
2. Fetches fresh URLs from Discord API
3. Updates the database with the new URLs

Usage:
    # Dry run (default) - shows what would be updated
    python scripts/refresh_media_urls.py --months 3 --per-month 30
    
    # Actually update the database
    python scripts/refresh_media_urls.py --months 3 --per-month 30 --execute
    
    # Specific month
    python scripts/refresh_media_urls.py --month 2026-01 --per-month 30
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from collections import defaultdict
import calendar

# Setup path for imports
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, '.env'))

import discord
from discord.ext import commands

from src.common.db_handler import DatabaseHandler
from src.common.discord_utils import refresh_media_url

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [RefreshURLs] - %(message)s'
)
logger = logging.getLogger(__name__)


def get_month_date_range(year_month: str) -> tuple[datetime, datetime]:
    """Get start and end datetime for a YYYY-MM string."""
    year, month = map(int, year_month.split('-'))
    _, last_day = calendar.monthrange(year, month)
    start_date = datetime(year, month, 1, 0, 0, 0)
    end_date = datetime(year, month, last_day, 23, 59, 59, 999999)
    return start_date, end_date


def get_months_to_process(months_back: int, specific_month: Optional[str] = None) -> List[str]:
    """Get list of YYYY-MM strings to process."""
    if specific_month:
        return [specific_month]
    
    months = []
    today = datetime.today()
    current_year = today.year
    current_month = today.month
    
    for i in range(months_back):
        # Calculate month by subtracting i months
        month = current_month - i
        year = current_year
        
        # Handle year rollover
        while month <= 0:
            month += 12
            year -= 1
        
        month_str = f"{year:04d}-{month:02d}"
        months.append(month_str)
    
    return sorted(months)


async def get_top_posts_with_media(
    db_handler: DatabaseHandler,
    start_date: datetime,
    end_date: datetime,
    limit: int
) -> List[Dict]:
    """
    Get top posts by reaction count that have media attachments.
    
    Returns messages sorted by reaction_count descending.
    """
    # Fetch all messages in the date range
    messages = db_handler.get_messages_in_range(start_date, end_date)
    
    if not messages:
        return []
    
    # Filter to messages with attachments, tracking seen message IDs to avoid duplicates
    with_attachments = []
    seen_message_ids = set()
    
    for msg in messages:
        message_id = msg.get('message_id')
        
        # Skip duplicates
        if message_id in seen_message_ids:
            continue
        seen_message_ids.add(message_id)
        
        attachments = msg.get('attachments')
        if not attachments:
            continue
        
        # Parse attachments if it's a string
        if isinstance(attachments, str):
            try:
                attachments = json.loads(attachments)
            except json.JSONDecodeError:
                continue
        
        if not attachments or not isinstance(attachments, list) or len(attachments) == 0:
            continue
        
        # Check if any attachment has a URL (valid media)
        has_media = any(
            att.get('url') for att in attachments 
            if isinstance(att, dict)
        )
        
        if has_media:
            msg['_parsed_attachments'] = attachments
            with_attachments.append(msg)
    
    # Sort by reaction_count descending
    sorted_msgs = sorted(
        with_attachments,
        key=lambda x: x.get('reaction_count', 0),
        reverse=True
    )
    
    return sorted_msgs[:limit]


def compare_attachments(old: List[Dict], new: List[Dict]) -> Dict[str, Any]:
    """
    Compare old and new attachment lists, return comparison info.
    """
    changes = []
    
    # Create lookup by filename or id
    old_by_filename = {att.get('filename', att.get('id', i)): att for i, att in enumerate(old)}
    new_by_filename = {att.get('filename', att.get('id', i)): att for i, att in enumerate(new)}
    
    for key, old_att in old_by_filename.items():
        new_att = new_by_filename.get(key)
        if new_att:
            old_url = old_att.get('url', '')
            new_url = new_att.get('url', '')
            
            # Check if URL changed
            if old_url != new_url:
                changes.append({
                    'filename': key,
                    'old_url_preview': old_url[:80] + '...' if len(old_url) > 80 else old_url,
                    'new_url_preview': new_url[:80] + '...' if len(new_url) > 80 else new_url,
                    'url_changed': True
                })
            else:
                changes.append({
                    'filename': key,
                    'url_changed': False
                })
    
    return {
        'total_attachments': len(old),
        'changes': changes,
        'urls_changed': sum(1 for c in changes if c.get('url_changed', False))
    }


async def refresh_message_urls(
    bot: commands.Bot,
    db_handler: DatabaseHandler,
    message: Dict,
    dry_run: bool = True
) -> Dict[str, Any]:
    """
    Refresh URLs for a single message.
    
    Returns a result dict with status and comparison info.
    """
    message_id = message.get('message_id')
    channel_id = message.get('channel_id')
    thread_id = message.get('thread_id')  # For forum posts
    old_attachments = message.get('_parsed_attachments', [])
    reaction_count = message.get('reaction_count', 0)
    
    result = {
        'message_id': message_id,
        'channel_id': channel_id,
        'reaction_count': reaction_count,
        'status': 'pending',
        'dry_run': dry_run
    }
    
    try:
        # Fetch fresh URLs from Discord
        # Try the stored channel_id first
        refresh_result = await refresh_media_url(bot, channel_id, message_id, logger)
        
        # If that fails and we have a thread_id, try using that as the channel
        # (useful for forum posts where messages are in thread channels)
        if not refresh_result and thread_id:
            logger.debug(f"Retrying with thread_id {thread_id} for message {message_id}")
            refresh_result = await refresh_media_url(bot, thread_id, message_id, logger)
        
        if not refresh_result or not refresh_result.get('success'):
            result['status'] = 'failed'
            result['error'] = 'Could not fetch message from Discord'
            return result
        
        new_attachments = refresh_result.get('attachments', [])
        
        if not new_attachments:
            result['status'] = 'no_attachments'
            result['note'] = 'Message no longer has attachments on Discord'
            return result
        
        # Compare old and new
        comparison = compare_attachments(old_attachments, new_attachments)
        result['comparison'] = comparison
        
        if comparison['urls_changed'] == 0:
            result['status'] = 'unchanged'
            result['note'] = 'All URLs are already up to date'
            return result
        
        # Would update (or actually update if not dry run)
        if dry_run:
            result['status'] = 'would_update'
            result['note'] = f"Would update {comparison['urls_changed']} URL(s)"
        else:
            # Actually perform the update - pass full message data with only attachments changed
            # Copy the original message and update only the attachments field
            message_data = {
                'message_id': message_id,
                'channel_id': channel_id,
                'author_id': message.get('author_id'),
                'content': message.get('content'),
                'created_at': message.get('created_at'),
                'attachments': new_attachments,  # Updated attachments
                'embeds': message.get('embeds', []),
                'reaction_count': message.get('reaction_count', 0),
                'reactors': message.get('reactors', []),
                'reference_id': message.get('reference_id'),
                'edited_at': message.get('edited_at'),
                'is_pinned': message.get('is_pinned', False),
                'thread_id': message.get('thread_id'),
                'message_type': message.get('message_type'),
                'flags': message.get('flags'),
                'is_deleted': message.get('is_deleted', False),
            }
            success = db_handler.update_message(message_data)
            
            if success:
                result['status'] = 'updated'
                result['note'] = f"Updated {comparison['urls_changed']} URL(s)"
            else:
                result['status'] = 'update_failed'
                result['error'] = 'Database update failed'
        
        return result
        
    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)
        logger.error(f"Error refreshing message {message_id}: {e}", exc_info=True)
        return result


async def run_refresh(
    months: List[str],
    per_month: int,
    dry_run: bool,
    dev_mode: bool
):
    """Main refresh logic."""
    
    # Initialize database
    db_handler = DatabaseHandler(dev_mode=dev_mode)
    logger.info(f"Connected to {'dev' if dev_mode else 'production'} database")
    
    # Initialize Discord bot (minimal, just for API access)
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='!', intents=intents)
    
    # Get token
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        logger.error("DISCORD_BOT_TOKEN not set")
        return
    
    # Results tracking
    all_results = defaultdict(list)
    summary = {
        'months_processed': 0,
        'messages_checked': 0,
        'urls_refreshed': 0,
        'urls_unchanged': 0,
        'errors': 0
    }
    
    @bot.event
    async def on_ready():
        logger.info(f"Bot connected as {bot.user}")
        
        try:
            for month in months:
                logger.info(f"\n{'='*60}")
                logger.info(f"Processing month: {month}")
                logger.info(f"{'='*60}")
                
                start_date, end_date = get_month_date_range(month)
                
                # Get top posts with media
                top_posts = await get_top_posts_with_media(
                    db_handler, start_date, end_date, per_month
                )
                
                logger.info(f"Found {len(top_posts)} posts with media in {month}")
                
                if not top_posts:
                    continue
                
                summary['months_processed'] += 1
                
                for i, msg in enumerate(top_posts, 1):
                    message_id = msg.get('message_id')
                    reaction_count = msg.get('reaction_count', 0)
                    num_attachments = len(msg.get('_parsed_attachments', []))
                    
                    logger.info(f"  [{i}/{len(top_posts)}] Message {message_id} ({reaction_count} reactions, {num_attachments} attachments)")
                    
                    result = await refresh_message_urls(bot, db_handler, msg, dry_run)
                    all_results[month].append(result)
                    
                    summary['messages_checked'] += 1
                    
                    if result['status'] == 'updated' or result['status'] == 'would_update':
                        summary['urls_refreshed'] += result.get('comparison', {}).get('urls_changed', 0)
                        logger.info(f"    -> {result['status']}: {result.get('note', '')}")
                    elif result['status'] == 'unchanged':
                        summary['urls_unchanged'] += 1
                        logger.info(f"    -> URLs already current")
                    elif result['status'] in ('failed', 'error', 'update_failed'):
                        summary['errors'] += 1
                        logger.warning(f"    -> {result['status']}: {result.get('error', 'Unknown error')}")
                    else:
                        logger.info(f"    -> {result['status']}: {result.get('note', '')}")
                    
                    # Small delay to avoid rate limits
                    await asyncio.sleep(0.5)
            
            # Print summary
            logger.info(f"\n{'='*60}")
            logger.info("SUMMARY")
            logger.info(f"{'='*60}")
            logger.info(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
            logger.info(f"Months processed: {summary['months_processed']}")
            logger.info(f"Messages checked: {summary['messages_checked']}")
            logger.info(f"URLs {'would be refreshed' if dry_run else 'refreshed'}: {summary['urls_refreshed']}")
            logger.info(f"URLs already current: {summary['urls_unchanged']}")
            logger.info(f"Errors: {summary['errors']}")
            
            if dry_run and summary['urls_refreshed'] > 0:
                logger.info(f"\nTo actually update the database, run with --execute flag")
            
            # Print detailed results for dry run
            if dry_run:
                logger.info(f"\n{'='*60}")
                logger.info("DETAILED CHANGES")
                logger.info(f"{'='*60}")
                
                for month, results in all_results.items():
                    changes = [r for r in results if r['status'] == 'would_update']
                    if changes:
                        logger.info(f"\n{month}:")
                        for r in changes:
                            logger.info(f"  Message {r['message_id']} ({r['reaction_count']} reactions):")
                            for change in r.get('comparison', {}).get('changes', []):
                                if change.get('url_changed'):
                                    logger.info(f"    {change['filename']}:")
                                    logger.info(f"      OLD: {change['old_url_preview']}")
                                    logger.info(f"      NEW: {change['new_url_preview']}")
        
        finally:
            await bot.close()
    
    # Run the bot
    await bot.start(token)


def main():
    parser = argparse.ArgumentParser(
        description="Refresh expired Discord CDN URLs for top posts with media."
    )
    
    parser.add_argument(
        '--months', type=int, default=3,
        help='Number of months to look back (default: 3)'
    )
    parser.add_argument(
        '--month', type=str,
        help='Specific month to process (YYYY-MM format, overrides --months)'
    )
    parser.add_argument(
        '--per-month', type=int, default=30,
        help='Number of top posts per month to refresh (default: 30)'
    )
    parser.add_argument(
        '--execute', action='store_true',
        help='Actually update the database (default is dry-run)'
    )
    parser.add_argument(
        '--dev', action='store_true',
        help='Use development database'
    )
    
    args = parser.parse_args()
    
    # Determine months to process
    months = get_months_to_process(args.months, args.month)
    dry_run = not args.execute
    
    logger.info(f"{'='*60}")
    logger.info("Discord Media URL Refresher")
    logger.info(f"{'='*60}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE (will update database)'}")
    logger.info(f"Months to process: {months}")
    logger.info(f"Top posts per month: {args.per_month}")
    logger.info(f"Database: {'dev' if args.dev else 'production'}")
    logger.info(f"{'='*60}")
    
    if not dry_run:
        confirm = input("\nThis will UPDATE the database. Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            logger.info("Aborted.")
            return
    
    asyncio.run(run_refresh(
        months=months,
        per_month=args.per_month,
        dry_run=dry_run,
        dev_mode=args.dev
    ))


if __name__ == '__main__':
    main()
