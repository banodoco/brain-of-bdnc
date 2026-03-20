"""
ServerConfig — per-server settings sourced from the database.

Loads from server_config / channel_effective_config tables. Runtime write access
and feature enablement are driven by DB state, not env flags.
"""

import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger('DiscordBot')


def _int_or_none(val) -> Optional[int]:
    """Safely cast to int, return None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class ServerConfig:
    """Per-server configuration with DB-backed enablement and write gating."""

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client
        self._bndc_guild_id = _int_or_none(os.getenv('GUILD_ID'))

        # Caches (populated by refresh())
        self._servers: Dict[int, dict] = {}          # guild_id -> server_config row
        self._channel_config: Dict[int, dict] = {}   # channel_id -> effective config row
        self._last_refresh_monotonic = 0.0
        self._auto_refresh_seconds = max(int(os.getenv('SERVER_CONFIG_REFRESH_SECONDS', '60')), 0)

        # Try initial load
        self.refresh()

    # ------------------------------------------------------------------
    # Safety gate
    # ------------------------------------------------------------------

    def is_guild_enabled(self, guild_id: Optional[int], require_write: bool = False) -> bool:
        """Check if a guild is enabled in server_config.

        When require_write is True, write_enabled must also be set.
        """
        if guild_id is None:
            return False
        self._maybe_refresh()
        server = self._servers.get(guild_id)
        if not server or not server.get('enabled'):
            return False
        if require_write and not server.get('write_enabled'):
            return False
        return True

    def is_write_allowed(self, guild_id: Optional[int]) -> bool:
        """Check if writes are allowed for this guild via DB config."""
        return self.is_guild_enabled(guild_id, require_write=True)

    @property
    def bndc_guild_id(self) -> Optional[int]:
        return self._bndc_guild_id

    def get_server(self, guild_id: int) -> Optional[dict]:
        """Return the server_config row for a guild, or None."""
        self._maybe_refresh()
        return self._servers.get(guild_id)

    def get_enabled_servers(self, require_write: bool = False) -> List[dict]:
        """Return enabled servers, optionally limited to writable ones."""
        self._maybe_refresh()
        return [
            s for s in self._servers.values()
            if self.is_guild_enabled(s.get('guild_id'), require_write=require_write)
        ]

    def get_first_server_with_field(self, field: str, require_write: bool = False) -> Optional[dict]:
        """Return the first enabled server that has a non-null field value."""
        for server in self.get_enabled_servers(require_write=require_write):
            if server.get(field) is not None:
                return server
        return None

    def get_default_guild_id(self, *, require_write: bool = False, field: Optional[str] = None) -> Optional[int]:
        """Return a default guild id from DB config.

        If field is provided, prefer the first enabled server with that field populated.
        Otherwise return the first enabled server.
        """
        server = self.get_first_server_with_field(field, require_write=require_write) if field else None
        if server is None:
            enabled = self.get_enabled_servers(require_write=require_write)
            server = enabled[0] if enabled else None
        return int(server['guild_id']) if server else None

    def resolve_guild_id(self, explicit: Optional[int] = None, *,
                         require_write: bool = False) -> Optional[int]:
        """Canonical guild_id resolution: explicit > DB default > BNDC env fallback."""
        if explicit:
            return int(explicit)
        return self.get_default_guild_id(require_write=require_write) or self._bndc_guild_id

    def get_guilds_to_archive(self) -> List[dict]:
        """Return list of writable guilds with archiving enabled."""
        return [
            s for s in self.get_enabled_servers(require_write=True)
            if s.get('default_archiving')
        ]

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    def is_feature_enabled(self, guild_id: Optional[int], channel_id: Optional[int], feature: str) -> bool:
        """Check if a feature is enabled for a guild+channel combination.

        Resolution: channel config > parent channel config > server default > False.
        Requires guild to be enabled and write_enabled.
        """
        if guild_id is None:
            return False

        self._maybe_refresh()

        feature_key = f"{feature}_enabled"  # e.g. 'logging_enabled'

        server = self._servers.get(guild_id)
        if not server or not server.get('enabled') or not server.get('write_enabled'):
            return False

        # 1. Check channel-level config (from channel_effective_config view)
        if channel_id and channel_id in self._channel_config:
            cfg = self._channel_config[channel_id]
            val = cfg.get(feature_key)
            if val is not None:
                return bool(val)

        # 2. Thread-parent fallback: resolve parent, check its config
        if channel_id and channel_id not in self._channel_config:
            parent_id = self.resolve_parent_channel(channel_id)
            if parent_id and parent_id != channel_id and parent_id in self._channel_config:
                cfg = self._channel_config[parent_id]
                val = cfg.get(feature_key)
                if val is not None:
                    return bool(val)

        # 3. Server-level default
        default_key = f"default_{feature}"  # e.g. 'default_logging'
        val = server.get(default_key)
        if val is not None:
            return bool(val)
        return False

    def resolve_parent_channel(self, channel_id: int) -> Optional[int]:
        """Return parent_id for a channel. Checks cache first, then queries DB."""
        self._maybe_refresh()
        # Check in-memory cache
        cfg = self._channel_config.get(channel_id)
        if cfg:
            return cfg.get('parent_id')

        # Query DB for channels not in config (first-seen threads)
        if self._supabase:
            try:
                result = (
                    self._supabase.table('discord_channels')
                    .select('parent_id')
                    .eq('channel_id', channel_id)
                    .limit(1)
                    .execute()
                )
                if result.data and result.data[0].get('parent_id'):
                    return result.data[0]['parent_id']
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    def get_content(self, guild_id: int, content_key: str) -> Optional[str]:
        """Fetch content from server_content table."""
        if not self._supabase:
            return None
        try:
            result = (
                self._supabase.table('server_content')
                .select('content')
                .eq('guild_id', guild_id)
                .eq('content_key', content_key)
                .execute()
            )
            if result.data:
                return result.data[0].get('content')
        except Exception as e:
            logger.debug(f"ServerConfig.get_content({guild_id}, {content_key}): {e}")
        return None

    # ------------------------------------------------------------------
    # Server config field access
    # ------------------------------------------------------------------

    def get_server_field(self, guild_id: int, field: str, *, cast: type = str) -> Optional:
        """Get a field from server_config for an enabled guild.

        Uses None-aware checks so False/0/[] are valid DB values.
        """
        self._maybe_refresh()
        server = self._servers.get(guild_id)
        if not server or not server.get('enabled'):
            return None
        val = server.get(field)
        if val is not None:
            try:
                return cast(val)
            except (ValueError, TypeError):
                pass
        return None

    # ------------------------------------------------------------------
    # Refresh from DB
    # ------------------------------------------------------------------

    def refresh(self):
        """Reload server_config and channel_effective_config from DB."""
        if not self._supabase:
            logger.debug("ServerConfig.refresh(): no supabase client, skipping")
            return

        try:
            # Load server configs
            result = self._supabase.table('server_config').select('*').execute()
            self._servers = {row['guild_id']: row for row in (result.data or [])}
            logger.debug(f"ServerConfig: loaded {len(self._servers)} server configs")
        except Exception as e:
            logger.warning(f"ServerConfig: failed to load server_config: {e}")

        try:
            # Load channel effective configs
            result = self._supabase.table('channel_effective_config').select('*').execute()
            self._channel_config = {row['channel_id']: row for row in (result.data or [])}
            logger.debug(f"ServerConfig: loaded {len(self._channel_config)} channel configs")
        except Exception as e:
            # View may not exist yet (pre-migration)
            logger.debug(f"ServerConfig: failed to load channel_effective_config: {e}")

        self._last_refresh_monotonic = time.monotonic()

    def _maybe_refresh(self):
        """Refresh cached config periodically so DB changes apply without restart."""
        if not self._supabase or self._auto_refresh_seconds <= 0:
            return
        if (time.monotonic() - self._last_refresh_monotonic) >= self._auto_refresh_seconds:
            self.refresh()
