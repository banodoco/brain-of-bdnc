# src/common/archive_runner.py

import asyncio
import sys
import os
import logging
from typing import Optional, List

logger = logging.getLogger('DiscordBot')

class ArchiveRunner:
    """Centralized archive script runner to eliminate code duplication."""

    def __init__(self, project_root: Optional[str] = None):
        if project_root is None:
            current_file = os.path.abspath(__file__)
            self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
        else:
            self.project_root = project_root

        self.archive_script_path = os.path.join(self.project_root, 'scripts', 'archive_discord.py')

    async def run_archive(self, days: int, dev_mode: bool = False, in_depth: bool = True,
                          guild_id: Optional[int] = None, channels: Optional[List[str]] = None) -> bool:
        """Run the archive script.

        Args:
            days: Number of days to archive
            dev_mode: Whether to run in development mode
            in_depth: Whether to use in-depth archiving
            guild_id: Explicit guild ID (for multi-server). If None, uses env var.
            channels: Explicit list of channel ID strings. If None, uses env var.

        Returns:
            bool: True if archive completed successfully, False otherwise
        """
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
        except Exception:
            pass

        # Resolve channel list: explicit only. When omitted, archive all accessible
        # channels in the target guild and let per-channel feature flags decide.
        if channels is not None:
            channel_ids = channels
        else:
            channel_ids = []

        # Build command
        cmd = [sys.executable, self.archive_script_path, '--days', str(days)]
        if channel_ids:
            cmd.extend(['--channels', ','.join(channel_ids)])
        if guild_id is not None:
            cmd.extend(['--guild-id', str(guild_id)])
        if in_depth:
            cmd.append('--in-depth')
        if dev_mode:
            cmd.append('--dev')

        if channel_ids:
            logger.info(f"Running archive for {len(channel_ids)} explicit channel(s)"
                        + (f" (guild {guild_id})" if guild_id else ""))
        else:
            logger.info(f"Running archive over all accessible channels"
                        + (f" (guild {guild_id})" if guild_id else ""))

        return await self._run_subprocess(cmd)

    async def _run_subprocess(self, cmd: list) -> bool:
        """Execute a single archive subprocess with log streaming."""
        # Patterns to skip in logs to avoid flooding Supabase
        skip_patterns = [
            'HTTP Request:',
            'Processing Thread',
            'Latest message in DB',
            'Earliest message in DB',
            'Searching for newer',
            'No more messages found',
            'Finished initial fetch',
            'incremental/full archive',
        ]

        try:
            logger.info(f"Running archive command: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.project_root,
                limit=1024 * 1024
            )

            output_lines = []
            if process.stdout:
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError as e:
                        logger.warning(f"Skipping very long log line (>{1024*1024} bytes): {e}")
                        await process.stdout.read(4096)
                        continue
                    if not line:
                        break
                    line_clean = line.decode('utf-8', errors='replace').rstrip()
                    if line_clean:
                        output_lines.append(line_clean)
                        if any(pattern in line_clean for pattern in skip_patterns):
                            continue
                        if len(line_clean) > 2000:
                            line_clean = line_clean[:2000] + "... [truncated]"
                        logger.info(f"[Archive] {line_clean}")

            try:
                await asyncio.wait_for(process.wait(), timeout=3600)
            except asyncio.TimeoutError:
                logger.error("Archive process timed out after 1 hour")
                process.kill()
                await process.wait()
                return False

            if process.returncode == 0:
                logger.info("Archive process completed successfully")
                return True
            else:
                logger.error(f"Archive process failed with return code {process.returncode}")
                if any('429' in line and 'Too Many Requests' in line for line in output_lines):
                    logger.warning("Archive hit Discord 429 rate limit")
                return False

        except Exception as e:
            logger.error(f"Error running archive script: {e}", exc_info=True)
            return False
