"""Rollback the stored tier status when a Speaker role change fails."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_three_tier_member_model import _make_cog, _make_tier_roles


def _member(tier, roles):
    member = SimpleNamespace(
        id=123,
        name='testuser',
        guild=SimpleNamespace(id=456),
        roles=roles,
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def test_failed_assignment_restores_stored_speaker_status_without_speaker_role():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    cog.db_handler.status = 'speaker'
    cog.db_handler.get_guild_member = lambda member_id, guild_id: {'member_status': 'speaker'}
    member = _member(tier, [tier['newbie']])
    member.add_roles.side_effect = RuntimeError('role assignment failed')

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is False
    assert cog.db_handler.status == 'speaker'
    assert ('set_member_status', 123, 'speaker', None, False) in cog.db_handler.calls
    assert ('set_member_status', 123, 'speaker', None, False) == cog.db_handler.calls[-1]
    member.remove_roles.assert_not_awaited()


def test_failed_assignment_restores_newbie_status():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    cog.db_handler.status = 'newbie'
    cog.db_handler.get_guild_member = lambda member_id, guild_id: {'member_status': 'newbie'}
    member = _member(tier, [tier['newbie']])
    member.add_roles.side_effect = RuntimeError('role assignment failed')

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is False
    assert cog.db_handler.status == 'newbie'
    assert cog.db_handler.calls[-1] == ('set_member_status', 123, 'newbie', None, False)


def test_invalid_stored_status_falls_back_to_existing_role():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    cog.db_handler.status = 'unexpected'
    cog.db_handler.get_guild_member = lambda member_id, guild_id: {'member_status': 'unexpected'}
    member = _member(tier, [tier['speaker'], tier['newbie']])
    member.remove_roles.side_effect = RuntimeError('role removal failed')

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is False
    assert cog.db_handler.status == 'speaker'
    assert cog.db_handler.calls[-1] == ('set_member_status', 123, 'speaker', None, False)


def test_missing_stored_status_falls_back_to_newbie_role():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    # get_member_status defaults to Speaker when no row exists. The raw row
    # check must still fall back to the member's current role.
    cog.db_handler.status = 'speaker'
    cog.db_handler.get_guild_member = lambda member_id, guild_id: None
    member = _member(tier, [tier['newbie']])
    member.add_roles.side_effect = RuntimeError('role assignment failed')

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is False
    assert cog.db_handler.status == 'newbie'


def test_successful_assignment_keeps_speaker_status():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    cog.db_handler.status = 'newbie'
    member = _member(tier, [tier['newbie']])

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is True
    assert cog.db_handler.status == 'speaker'
    member.add_roles.assert_awaited_once_with(tier['speaker'], reason='Joined via Speaker invite')
    member.remove_roles.assert_awaited_once_with(tier['newbie'], reason='Speaker invite join')


def test_moderated_status_still_prevents_speaker_promotion():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    cog.db_handler.status = 'moderated'
    member = _member(tier, [tier['newbie']])

    result = asyncio.run(cog._assign_speaker_on_join(member))

    assert result is False
    assert cog.db_handler.status == 'moderated'
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
