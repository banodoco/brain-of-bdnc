# src/common/archive_runner.py

import subprocess
import sys
import os
import logging
from typing import Optional

logger = logging.getLogger('DiscordBot')

class ArchiveRunner:
    """Centralized archive script runner to eliminate code duplication."""
    
    def __init__(self, project_root: Optional[str] = None):
        """
        Initialize the ArchiveRunner.
        
        Args:
            project_root: Optional project root path. If None, will auto-detect.
        """
        if project_root is None:
            # Auto-detect project root from this file's location
            current_file = os.path.abspath(__file__)
            self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
        else:
            self.project_root = project_root
            
        self.archive_script_path = os.path.join(self.project_root, 'scripts', 'archive_discord.py')
    
    async def run_archive(self, days: int, dev_mode: bool = False, in_depth: bool = True) -> bool:
        """
        Run the archive script with the specified parameters.
        
        Args:
            days: Number of days to archive
            dev_mode: Whether to run in development mode
            in_depth: Whether to use in-depth archiving
            
        Returns:
            bool: True if archive completed successfully, False otherwise
        """
        # Prefer archiving only configured channels_to_monitor when available
        # so that the scraped data matches what the summarizer looks at.
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
        except Exception:
            pass

        env_prefix = 'DEV_' if dev_mode else ''
        channels_env_key = f"{env_prefix}CHANNELS_TO_MONITOR"
        channels_str = os.getenv(channels_env_key, '')
        channel_ids: list[str] = [c.strip() for c in channels_str.split(',') if c.strip()]

        # Get storage backend from environment to pass to archive script
        storage_backend = os.getenv('STORAGE_BACKEND', 'sqlite')

        # If we have a channel list, run the archiver once per channel; otherwise fall back to full-guild scrape
        commands_to_run: list[list[str]] = []
        if channel_ids:
            for cid in channel_ids:
                cmd = [sys.executable, self.archive_script_path, '--days', str(days), '--channel', cid]
                if in_depth:
                    cmd.append('--in-depth')
                if dev_mode:
                    cmd.append('--dev')
                # Pass storage backend to archive script
                cmd.extend(['--storage-backend', storage_backend])
                commands_to_run.append(cmd)
            logger.info(f"Running archive for {len(channel_ids)} monitored channel(s) from {channels_env_key} with storage backend: {storage_backend}")
        else:
            cmd = [sys.executable, self.archive_script_path, '--days', str(days)]
            if in_depth:
                cmd.append('--in-depth')
            if dev_mode:
                cmd.append('--dev')
            # Pass storage backend to archive script
            cmd.extend(['--storage-backend', storage_backend])
            commands_to_run.append(cmd)
            logger.info(f"{channels_env_key} not set; running archive over all accessible channels with storage backend: {storage_backend}")

        try:
            all_ok = True
            for cmd in commands_to_run:
                logger.info(f"Running archive command: {' '.join(cmd)}")
                
                # Stream output in real-time instead of capturing it
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.project_root,
                    bufsize=1,
                    universal_newlines=True
                )
                
                # Stream output line by line
                if process.stdout:
                    for line in iter(process.stdout.readline, ''):
                        if line:
                            logger.info(f"[Archive] {line.rstrip()}")
                
                process.wait(timeout=3600)
                
                if process.returncode == 0:
                    logger.info("Archive process completed successfully")
                else:
                    all_ok = False
                    logger.error(f"Archive process failed with return code {process.returncode}")
            return all_ok
        except subprocess.TimeoutExpired:
            logger.error("Archive process timed out after 1 hour")
            return False
        except Exception as e:
            logger.error(f"Error running archive script: {e}", exc_info=True)
            return False
