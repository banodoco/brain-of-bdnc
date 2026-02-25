"""Shared helpers for Speaker role permission enforcement."""
import logging
from typing import Tuple

import discord

logger = logging.getLogger('DiscordBot')

# The four permission attributes we manage on every channel.
SEND_PERMS = [
    'send_messages',
    'send_messages_in_threads',
    'create_public_threads',
    'create_private_threads',
]


def _expected_values(mode: str, target: str) -> dict:
    """Return the expected permission values for a given mode and target.

    Args:
        mode: 'normal', 'readonly', or 'exempt'
        target: 'everyone' or 'speaker'

    Returns:
        Dict mapping each SEND_PERMS attr to True/False.
    """
    if mode == 'readonly':
        # Both @everyone and Speaker are denied
        return {p: False for p in SEND_PERMS}
    # 'normal' (and fallback)
    if target == 'everyone':
        return {p: False for p in SEND_PERMS}
    else:  # speaker
        return {p: True for p in SEND_PERMS}


def check_overwrite_matches(overwrite: discord.PermissionOverwrite, expected: dict) -> bool:
    """Check if existing cached overwrite already matches expected values.

    Avoids unnecessary Discord API calls when perms are already correct.
    """
    for attr, value in expected.items():
        current = getattr(overwrite, attr)
        if current != value:
            return False
    return True


async def apply_perms_to_channel(
    channel: discord.abc.GuildChannel,
    role: discord.Role,
    mode: str,
) -> Tuple[bool, int]:
    """Check and correct permissions for a single channel.

    Args:
        channel: The Discord channel to enforce.
        role: The Speaker role.
        mode: 'normal', 'readonly', or 'exempt'.

    Returns:
        (changed, api_calls) — whether anything was fixed, and how many API calls made.
    """
    if mode == 'exempt':
        return (False, 0)

    everyone = channel.guild.default_role
    changed = False
    api_calls = 0

    # --- @everyone overwrite ---
    everyone_expected = _expected_values(mode, 'everyone')
    everyone_ow = channel.overwrites_for(everyone)
    if not check_overwrite_matches(everyone_ow, everyone_expected):
        for attr, value in everyone_expected.items():
            setattr(everyone_ow, attr, value)
        await channel.set_permissions(
            everyone, overwrite=everyone_ow,
            reason=f"Speaker perm enforcement — {mode} @everyone",
        )
        api_calls += 1
        changed = True

    # --- Speaker role overwrite ---
    speaker_expected = _expected_values(mode, 'speaker')
    speaker_ow = channel.overwrites_for(role)
    if not check_overwrite_matches(speaker_ow, speaker_expected):
        for attr, value in speaker_expected.items():
            setattr(speaker_ow, attr, value)
        await channel.set_permissions(
            role, overwrite=speaker_ow,
            reason=f"Speaker perm enforcement — {mode} Speaker",
        )
        api_calls += 1
        changed = True

    return (changed, api_calls)
