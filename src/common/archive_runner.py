# src/common/archive_runner.py

import asyncio
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
        
        Uses asyncio subprocess to avoid blocking the event loop, allowing other
        async tasks (like the daily summary) to run concurrently.
        
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

        # If we have a channel list, run the archiver once per channel; otherwise fall back to full-guild scrape
        commands_to_run: list[list[str]] = []
        if channel_ids:
            for cid in channel_ids:
                cmd = [sys.executable, self.archive_script_path, '--days', str(days), '--channel', cid]
                if in_depth:
                    cmd.append('--in-depth')
                if dev_mode:
                    cmd.append('--dev')
                commands_to_run.append(cmd)
            logger.info(f"Running archive for {len(channel_ids)} monitored channel(s) from {channels_env_key}")
        else:
            cmd = [sys.executable, self.archive_script_path, '--days', str(days)]
            if in_depth:
                cmd.append('--in-depth')
            if dev_mode:
                cmd.append('--dev')
            commands_to_run.append(cmd)
            logger.info(f"{channels_env_key} not set; running archive over all accessible channels")

        # Patterns to skip in logs to avoid flooding Supabase
        skip_patterns = [
            'HTTP Request:',           # API call spam
            'Processing Thread',       # Per-thread progress (500+ per channel)
            'Latest message in DB',    # Per-thread diagnostic
            'Earliest message in DB',  # Per-thread diagnostic
            'Searching for newer',     # Per-thread diagnostic
            'No more messages found',  # Per-thread diagnostic
            'Finished initial fetch',  # Per-thread diagnostic
            'incremental/full archive',# Per-thread diagnostic
        ]

        try:
            all_ok = True
            for cmd in commands_to_run:
                logger.info(f"Running archive command: {' '.join(cmd)}")
                
                # Use asyncio subprocess to avoid blocking the event loop
                # Increase buffer limit to 1MB (default is 64KB) to handle very long log lines
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.project_root,
                    limit=1024 * 1024  # 1MB buffer limit
                )
                
                # Stream output line by line asynchronously
                if process.stdout:
                    while True:
                        try:
                            line = await process.stdout.readline()
                        except ValueError as e:
                            # Handle case where line exceeds even our increased limit
                            logger.warning(f"Skipping very long log line (>{1024*1024} bytes): {e}")
                            # Read and discard the rest of the oversized chunk
                            await process.stdout.read(4096)
                            continue
                        if not line:
                            break
                        line_clean = line.decode('utf-8', errors='replace').rstrip()
                        if line_clean:
                            # Skip verbose logs
                            if any(pattern in line_clean for pattern in skip_patterns):
                                continue
                            # Truncate very long lines to prevent log flooding
                            if len(line_clean) > 2000:
                                line_clean = line_clean[:2000] + "... [truncated]"
                            # Log important archive events
                            logger.info(f"[Archive] {line_clean}")
                
                # Wait for process to complete with timeout
                try:
                    await asyncio.wait_for(process.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    logger.error("Archive process timed out after 1 hour")
                    process.kill()
                    await process.wait()
                    return False
                
                if process.returncode == 0:
                    logger.info("Archive process completed successfully")
                else:
                    all_ok = False
                    logger.error(f"Archive process failed with return code {process.returncode}")
            return all_ok
        except Exception as e:
            logger.error(f"Error running archive script: {e}", exc_info=True)
            return False
