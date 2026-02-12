# src/features/health/health_check_cog.py

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

logger = logging.getLogger('DiscordBot')


class HealthCheckCog(commands.Cog):
    """Periodic health checks that DM the admin when something looks wrong."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)
        self.health_check_loop.start()

    def cog_unload(self):
        self.health_check_loop.cancel()

    # ------------------------------------------------------------------
    # Scheduled loop – runs every 6 hours
    # ------------------------------------------------------------------
    @tasks.loop(hours=6)
    async def health_check_loop(self):
        alerts: list[str] = []

        try:
            alerts.extend(self._check_recent_messages())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking recent messages: {e}", exc_info=True)

        try:
            alerts.extend(self._check_reactions_recorded())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking reactions: {e}", exc_info=True)

        try:
            alerts.extend(self._check_daily_summary())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking daily summary: {e}", exc_info=True)

        if alerts:
            await self._notify_admin(alerts)
        else:
            logger.info("[HealthCheck] All checks passed")

    @health_check_loop.before_loop
    async def before_health_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    def _get_supabase(self):
        """Return the Supabase client, or None."""
        if self.db and self.db.storage_handler and self.db.storage_handler.supabase_client:
            return self.db.storage_handler.supabase_client
        return None

    def _check_recent_messages(self) -> list[str]:
        """Alert if no messages were indexed in the last 6 hours."""
        sb = self._get_supabase()
        if not sb:
            return ["Supabase client unavailable – cannot check recent messages"]

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('indexed_at', cutoff)
            .limit(1)
            .execute()
        )
        count = result.count if result.count is not None else len(result.data)
        if count == 0:
            return ["No messages indexed in the last 6 hours"]
        return []

    def _check_reactions_recorded(self) -> list[str]:
        """Alert if there are recent messages but none have reactions."""
        sb = self._get_supabase()
        if not sb:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        # Check if any recent messages exist at all
        msg_result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('created_at', cutoff)
            .limit(1)
            .execute()
        )
        msg_count = msg_result.count if msg_result.count is not None else len(msg_result.data)
        if msg_count == 0:
            return []  # No messages at all – nothing to check

        # Check if any of those have reactions
        react_result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('created_at', cutoff)
            .gt('reaction_count', 0)
            .limit(1)
            .execute()
        )
        react_count = react_result.count if react_result.count is not None else len(react_result.data)
        if react_count == 0:
            return ["No messages with reaction_count > 0 in the last 24 hours (reaction updates may be broken)"]
        return []

    def _check_daily_summary(self) -> list[str]:
        """After 8:00 UTC, alert if today's daily summary is missing."""
        now = datetime.now(timezone.utc)
        if now.hour < 8:
            return []  # Too early – summary hasn't been scheduled yet

        sb = self._get_supabase()
        if not sb:
            return []

        today = now.strftime('%Y-%m-%d')
        result = (
            sb.table('daily_summaries')
            .select('*', count='exact')
            .eq('date', today)
            .limit(1)
            .execute()
        )
        count = result.count if result.count is not None else len(result.data)
        if count == 0:
            return [f"No daily summary found for {today} (checked after 08:00 UTC)"]
        return []

    # ------------------------------------------------------------------
    # Admin notification
    # ------------------------------------------------------------------
    async def _notify_admin(self, alerts: list[str]):
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if not admin_id_str:
            logger.error("[HealthCheck] ADMIN_USER_ID not set – cannot send alerts")
            return

        try:
            admin_id = int(admin_id_str)
        except ValueError:
            logger.error("[HealthCheck] ADMIN_USER_ID is not a valid integer")
            return

        body = "\n".join(f"- {a}" for a in alerts)
        message = f"**Health Check Alert**\n{body}"
        if len(message) > 1900:
            message = message[:1900] + "..."

        try:
            admin_user = await self.bot.fetch_user(admin_id)
            await admin_user.send(message)
            logger.info(f"[HealthCheck] Sent {len(alerts)} alert(s) to admin")
        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning("[HealthCheck] Rate limited sending admin DM – skipping")
            else:
                logger.error(f"[HealthCheck] Failed to DM admin: {e}")
        except Exception as e:
            logger.error(f"[HealthCheck] Failed to DM admin: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------
    @commands.command(name="healthcheck")
    async def healthcheck_command(self, ctx: commands.Context):
        """Manually trigger a health check."""
        alerts: list[str] = []
        alerts.extend(self._check_recent_messages())
        alerts.extend(self._check_reactions_recorded())
        alerts.extend(self._check_daily_summary())

        if alerts:
            body = "\n".join(f"- {a}" for a in alerts)
            await ctx.send(f"**Health Check Issues:**\n{body}")
        else:
            await ctx.send("All health checks passed.")
