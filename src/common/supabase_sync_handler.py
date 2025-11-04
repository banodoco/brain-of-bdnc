"""
Supabase Sync Handler - Integrates automatic syncing into the main bot workflow.
This module provides a background task that periodically syncs data to Supabase.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path
import sys

# Add the project root to the path so we can import our modules
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from scripts.sync_to_supabase import SupabaseSync
from src.common.constants import get_storage_backend, STORAGE_SUPABASE, STORAGE_BOTH


class SupabaseSyncHandler:
    """Handles background syncing to Supabase for the Discord bot."""
    
    def __init__(self, db_handler, logger: logging.Logger, sync_interval: int = 300):
        """
        Initialize the Supabase sync handler.
        
        Args:
            db_handler: The database handler instance from the bot
            logger: Logger instance
            sync_interval: Sync interval in seconds (default: 5 minutes)
        """
        self.db_handler = db_handler
        self.logger = logger
        self.sync_interval = sync_interval
        self.sync_client: Optional[SupabaseSync] = None
        self.sync_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.last_sync_time: Optional[datetime] = None
        
        # Check if direct writes are enabled - if so, skip background sync
        storage_backend = get_storage_backend()
        if storage_backend in [STORAGE_SUPABASE, STORAGE_BOTH]:
            self.logger.info(f"Direct Supabase writes enabled (STORAGE_BACKEND={storage_backend}). Background sync is disabled.")
            self.direct_writes_enabled = True
            return
        
        self.direct_writes_enabled = False
        
        # Initialize Supabase sync client
        self._init_sync_client()
    
    def _init_sync_client(self) -> None:
        """Initialize the Supabase sync client."""
        try:
            supabase_url = os.getenv('SUPABASE_URL')
            supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
            
            if not supabase_url or not supabase_key:
                self.logger.warning("Supabase credentials not found. Auto-sync to Supabase will be disabled.")
                return
            
            # Use the database path from the db_handler
            db_path = self.db_handler.db_path
            
            self.sync_client = SupabaseSync(db_path, supabase_url, supabase_key, self.logger)
            self.logger.info("Supabase sync client initialized successfully.")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Supabase sync client: {e}", exc_info=True)
    
    async def start_background_sync(self) -> bool:
        """Start the background sync task."""
        if self.direct_writes_enabled:
            self.logger.info("Skipping background sync - direct Supabase writes are enabled via STORAGE_BACKEND")
            return False
        
        if not self.sync_client:
            self.logger.warning("Supabase sync client not available. Cannot start background sync.")
            return False
        
        if self.is_running:
            self.logger.warning("Background sync is already running.")
            return True
        
        try:
            self.logger.info(f"Starting background sync to Supabase (interval: {self.sync_interval}s)...")
            self.sync_task = asyncio.create_task(self._background_sync_loop())
            self.is_running = True
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start background sync: {e}", exc_info=True)
            return False
    
    async def stop_background_sync(self) -> None:
        """Stop the background sync task."""
        if not self.is_running or not self.sync_task:
            return
        
        self.logger.info("Stopping background sync to Supabase...")
        self.is_running = False
        
        try:
            self.sync_task.cancel()
            await self.sync_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Error stopping background sync: {e}", exc_info=True)
        
        self.sync_task = None
        self.logger.info("Background sync stopped.")
    
    async def _background_sync_loop(self) -> None:
        """Main background sync loop."""
        self.logger.info("Background sync loop started.")
        
        # Initial sync check
        await self._perform_sync()
        
        while self.is_running:
            try:
                await asyncio.sleep(self.sync_interval)
                if self.is_running:  # Check again after sleep
                    await self._perform_sync()
                    
            except asyncio.CancelledError:
                self.logger.info("Background sync loop cancelled.")
                break
            except Exception as e:
                self.logger.error(f"Error in background sync loop: {e}", exc_info=True)
                # Continue the loop even if there's an error
                await asyncio.sleep(min(self.sync_interval, 60))  # Wait at least 1 minute on error
    
    async def _perform_sync(self) -> Dict[str, int]:
        """Perform the actual sync operation."""
        if not self.sync_client:
            return {'messages': 0, 'members': 0, 'channels': 0}
        
        try:
            self.logger.debug("Starting periodic sync to Supabase...")
            
            # Perform incremental sync (only new/updated records)
            results = await self.sync_client.full_sync()
            
            # Update last sync time
            self.last_sync_time = datetime.utcnow()
            
            # Log results only if something was synced
            total_synced = sum(results.values())
            if total_synced > 0:
                self.logger.info(f"Sync completed: {results} (total: {total_synced} records)")
            else:
                self.logger.debug("Sync completed: No new records to sync.")
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error during periodic sync: {e}", exc_info=True)
            return {'messages': 0, 'members': 0, 'channels': 0}
    
    async def manual_sync(self, sync_type: str = 'all', limit: Optional[int] = None) -> Dict[str, int]:
        """
        Perform a manual sync operation.
        
        Args:
            sync_type: Type of sync ('all', 'messages', 'members', 'channels')
            limit: Optional limit on number of records to sync
            
        Returns:
            Dictionary with sync results
        """
        if not self.sync_client:
            self.logger.error("Supabase sync client not available.")
            return {'messages': 0, 'members': 0, 'channels': 0}
        
        try:
            self.logger.info(f"Starting manual sync (type: {sync_type}, limit: {limit})...")
            
            results = {'messages': 0, 'members': 0, 'channels': 0}
            
            if sync_type == 'all':
                results = await self.sync_client.full_sync(limit)
            elif sync_type == 'messages':
                results['messages'] = await self.sync_client.sync_messages(limit)
            elif sync_type == 'members':
                results['members'] = await self.sync_client.sync_members(limit)
            elif sync_type == 'channels':
                results['channels'] = await self.sync_client.sync_channels(limit)
            else:
                self.logger.error(f"Invalid sync type: {sync_type}")
                return results
            
            # Update last sync time
            self.last_sync_time = datetime.utcnow()
            
            total_synced = sum(results.values())
            self.logger.info(f"Manual sync completed: {results} (total: {total_synced} records)")
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error during manual sync: {e}", exc_info=True)
            return {'messages': 0, 'members': 0, 'channels': 0}
    
    def get_sync_status(self) -> Dict[str, Any]:
        """Get the current sync status."""
        return {
            'is_running': self.is_running,
            'direct_writes_enabled': self.direct_writes_enabled,
            'last_sync_time': self.last_sync_time.isoformat() if self.last_sync_time else None,
            'sync_interval': self.sync_interval,
            'supabase_available': self.sync_client is not None,
            'next_sync_in': None if not self.is_running or not self.last_sync_time 
                          else max(0, self.sync_interval - (datetime.utcnow() - self.last_sync_time).total_seconds())
        }
    
    async def test_connection(self) -> bool:
        """Test the connection to Supabase."""
        if not self.sync_client:
            return False
        
        try:
            # Try to check if tables exist
            return await self.sync_client.create_tables_if_not_exist()
        except Exception as e:
            self.logger.error(f"Supabase connection test failed: {e}", exc_info=True)
            return False








