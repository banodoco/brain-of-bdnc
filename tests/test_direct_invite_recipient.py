"""Identity-targeted invites: no live Discord or Supabase access."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.direct_invite_approvals import DirectInviteApprovals, InviteRecipient, parse_invite_recipient
from tests.test_three_tier_member_model import _make_cog, _make_tier_roles

USER_ID = 123456789012345678
OTHER_ID = 223456789012345678


class MemoryApprovals:
    def __init__(self):
        self.rows = []
        self.fail_save = False
        self.fail_consume = False

    def save(self, guild_id, recipient, approved_by):
        if self.fail_save:
            raise RuntimeError('storage offline')
        row = dict(id=str(len(self.rows)), guild_id=guild_id, user_id=recipient.user_id,
                   username=recipient.username, recipient_key=recipient.key, approved_by=approved_by)
        self.rows.append(row)
        return deepcopy(row)

    def matching(self, guild_id, user_id, username):
        return deepcopy([r for r in self.rows if r['guild_id'] == guild_id and (
            r['user_id'] == user_id or (r['user_id'] is None and r['username'] == username.lower()))])

    def bind(self, approval, user_id):
        row = next(r for r in self.rows if r['id'] == approval['id'])
        if row['user_id'] not in (None, user_id):
            return False
        row['user_id'] = user_id
        return True

    def consume(self, approval):
        if self.fail_consume:
            raise RuntimeError('storage offline')
        self.rows = [r for r in self.rows if r['id'] != approval['id']]


def setup_invites():
    cog = _make_cog()
    cog.db_handler.supabase = object()
    cog._speaker_invite_uses = {456: 7}
    cog._get_equity_holders_role_id = lambda _: 99
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda _: tier
    cog._get_newbie_role_id = lambda _: tier['newbie'].id
    store = MemoryApprovals()
    cog._approval_store = lambda: store
    caller = SimpleNamespace(id=999, name='owner', roles=[SimpleNamespace(id=99)])
    guild = SimpleNamespace(id=456, members=[], get_member=lambda uid: caller if uid == caller.id else None,
                            fetch_member=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'missing')),
                            query_members=AsyncMock(return_value=[]), get_role=lambda _: tier['newbie'])
    invite = SimpleNamespace(code='targeted', url='https://discord.gg/targeted', uses=0, delete=AsyncMock())
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=invite))
    cog._get_default_invite_channel = lambda _: channel
    cog.server_config = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog._is_speaker_invite_join = AsyncMock(return_value=False)
    interaction = SimpleNamespace(user=caller, guild=guild, guild_id=guild.id, channel_id=123,
                                  response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    return cog, store, guild, interaction, channel, invite, tier


def make_member(guild, tier, *, user_id=USER_ID, name='mostaadtech', roles=None):
    return SimpleNamespace(id=user_id, name=name, display_name='MostAadTech', nick='MostAadTech',
                           mention=f'<@{user_id}>', guild=guild, bot=False,
                           roles=roles if roles is not None else [tier['newbie']],
                           add_roles=AsyncMock(), remove_roles=AsyncMock())


def run_command(cog, interaction, recipient):
    asyncio.run(cog.direct_invite.callback(cog, interaction, recipient=recipient))


@pytest.mark.parametrize('value', [str(USER_ID), f'<@{USER_ID}>', f'<@!{USER_ID}>'])
def test_parse_user_id_or_mention(value):
    assert parse_invite_recipient(value) == InviteRecipient(user_id=USER_ID)


@pytest.mark.parametrize('value', ['MostAadTech', '@MOSTAADTECH', ' @mostaadtech '])
def test_normalize_exact_username(value):
    assert parse_invite_recipient(value) == InviteRecipient(username='mostaadtech')


def test_numeric_usernames_and_id_disambiguation():
    assert parse_invite_recipient('1234') == InviteRecipient(username='1234')
    assert parse_invite_recipient('@' + str(USER_ID)) == InviteRecipient(username=str(USER_ID))


@pytest.mark.parametrize('value', ['', ' ', '@', 'display name', 'x', 'a..b', 'user#1234', '<@abc>',
                                 '99999999999999999999', '-123456789012345678', 'éclair', '1' * 33])
def test_reject_invalid_recipient(value):
    with pytest.raises(ValueError):
        parse_invite_recipient(value)


@pytest.mark.parametrize('recipient', [str(USER_ID), '@MostAadTech'])
def test_existing_member_promoted_immediately_by_id_or_username(recipient):
    cog, store, guild, interaction, channel, _, tier = setup_invites()
    member = make_member(guild, tier)
    guild.members = [member]
    original_get = guild.get_member
    guild.get_member = lambda uid: member if uid == USER_ID else original_get(uid)
    run_command(cog, interaction, recipient)
    member.add_roles.assert_awaited_once_with(tier['speaker'], reason='Joined via Speaker invite')
    member.remove_roles.assert_awaited_once()
    assert cog.db_handler.status == 'speaker'
    assert store.rows == []
    channel.create_invite.assert_not_called()
    assert 'Assigned **Speaker**' in interaction.followup.send.call_args.args[0]


def test_uncached_existing_username_resolves_exact_account_name():
    cog, store, guild, interaction, channel, _, tier = setup_invites()
    member = make_member(guild, tier)
    decoy = make_member(guild, tier, user_id=OTHER_ID, name='anotheraccount')
    guild.query_members.return_value = [decoy, member]
    run_command(cog, interaction, '@MOSTAADTECH')
    member.add_roles.assert_awaited_once()
    decoy.add_roles.assert_not_called()
    channel.create_invite.assert_not_called()


def test_uncached_existing_member_resolved_by_id():
    cog, _, guild, interaction, channel, _, tier = setup_invites()
    member = make_member(guild, tier)
    guild.fetch_member.side_effect = None
    guild.fetch_member.return_value = member
    run_command(cog, interaction, str(USER_ID))
    guild.fetch_member.assert_awaited_once_with(USER_ID)
    member.add_roles.assert_awaited_once()
    channel.create_invite.assert_not_called()


@pytest.mark.parametrize('recipient', [str(USER_ID), '@MOSTAADTECH'])
def test_pending_approval_promotes_on_later_join_and_is_consumed(recipient):
    cog, store, guild, interaction, channel, _, tier = setup_invites()
    run_command(cog, interaction, recipient)
    assert len(store.rows) == 1
    assert channel.create_invite.call_args.kwargs['unique'] is True
    cog.server_config.set_server_field.assert_not_called()
    assert cog._speaker_invite_uses == {456: 7}
    member = make_member(guild, tier)
    asyncio.run(cog.on_member_join(member))
    member.add_roles.assert_awaited_once_with(tier['speaker'], reason='Joined via Speaker invite')
    assert store.rows == []
    cog._is_speaker_invite_join.assert_awaited_once_with(guild)


def test_display_name_and_nickname_do_not_match_target_username():
    cog, store, guild, interaction, _, _, tier = setup_invites()
    decoy = make_member(guild, tier, user_id=OTHER_ID, name='anotheraccount')
    guild.members = [decoy]
    guild.query_members.return_value = [decoy]
    run_command(cog, interaction, '@MostAadTech')
    asyncio.run(cog.on_member_join(decoy))
    decoy.add_roles.assert_not_called()  # already has Newbie; no Speaker grant
    assert store.rows[0]['user_id'] is None
    assert store.rows[0]['username'] == 'mostaadtech'


def test_pending_id_does_not_approve_wrong_account_or_other_guild():
    cog, store, guild, interaction, _, _, tier = setup_invites()
    run_command(cog, interaction, str(USER_ID))
    wrong_account = make_member(guild, tier, user_id=OTHER_ID)
    asyncio.run(cog.on_member_join(wrong_account))
    wrong_account.add_roles.assert_not_called()
    other_guild = SimpleNamespace(id=999, get_role=guild.get_role)
    right_account_wrong_guild = make_member(other_guild, tier)
    asyncio.run(cog.on_member_join(right_account_wrong_guild))
    right_account_wrong_guild.add_roles.assert_not_called()
    assert len(store.rows) == 1


def test_failed_role_assignment_retains_and_binds_username_approval():
    cog, store, guild, interaction, _, _, tier = setup_invites()
    run_command(cog, interaction, '@MostAadTech')
    member = make_member(guild, tier)
    member.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='forbidden'), 'forbidden')
    asyncio.run(cog.on_member_join(member))
    assert store.rows[0]['user_id'] == USER_ID
    assert cog.db_handler.status == 'newbie'
    decoy = make_member(guild, tier, user_id=OTHER_ID)
    asyncio.run(cog.on_member_join(decoy))
    decoy.add_roles.assert_not_called()
    member.name = 'renamedaccount'
    member.add_roles.side_effect = None
    asyncio.run(cog.on_member_join(member))
    assert store.rows == []


@pytest.mark.parametrize('failed_join_first', [True, False])
def test_existing_member_retry_consumes_older_username_approval(failed_join_first):
    cog, store, guild, interaction, _, _, tier = setup_invites()
    run_command(cog, interaction, '@MostAadTech')
    member = make_member(guild, tier)
    if failed_join_first:
        member.add_roles.side_effect = RuntimeError('role assignment failed')
        asyncio.run(cog.on_member_join(member))
        assert store.rows[0]['user_id'] == USER_ID
        member.add_roles.side_effect = None
    guild.members = [member]
    run_command(cog, interaction, '@MostAadTech')
    assert store.rows == []  # Both the old username and new ID approval consumed.
    member.add_roles.reset_mock()
    asyncio.run(cog.on_member_join(member))
    member.add_roles.assert_not_called()


def test_status_write_failure_retains_approval_and_does_not_change_roles():
    cog, store, guild, interaction, _, _, tier = setup_invites()
    run_command(cog, interaction, str(USER_ID))
    cog.db_handler.set_member_status = MagicMock(return_value=False)
    member = make_member(guild, tier)
    asyncio.run(cog.on_member_join(member))
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()
    assert len(store.rows) == 1


def test_failed_persistence_does_not_issue_invite_or_promote():
    cog, store, guild, interaction, channel, invite, tier = setup_invites()
    store.fail_save = True
    run_command(cog, interaction, '@MostAadTech')
    invite.delete.assert_awaited_once()
    assert 'Could not save' in interaction.followup.send.call_args.args[0]
    assert invite.url not in interaction.followup.send.call_args.args[0]
    assert store.rows == []
    cog.server_config.set_server_field.assert_not_called()
    member = make_member(guild, tier)
    guild.members = [member]
    run_command(cog, interaction, '@MostAadTech')
    member.add_roles.assert_not_called()


def test_missing_storage_preserves_legacy_command_but_fails_targeted_approval():
    cog, _, _, interaction, _, invite, _ = setup_invites()
    cog.db_handler.supabase = None
    del cog._approval_store
    run_command(cog, interaction, '@MostAadTech')
    assert 'Could not save' in interaction.followup.send.call_args.args[0]
    run_command(cog, interaction, None)
    assert 'Direct Speaker invite created' in interaction.followup.send.call_args.args[0]
    cog.server_config.set_server_field.assert_called_once_with(456, 'speaker_invite_code', invite.code)


def test_unauthorized_recipient_command_has_no_side_effects():
    cog, store, _, interaction, channel, _, _ = setup_invites()
    interaction.user.roles = []
    run_command(cog, interaction, '@MostAadTech')
    assert 'Only Equity Holders' in interaction.followup.send.call_args.args[0]
    assert store.rows == []
    channel.create_invite.assert_not_called()


def test_recipient_command_does_not_unmute_moderated_member():
    cog, store, guild, interaction, _, _, tier = setup_invites()
    member = make_member(guild, tier, roles=[tier['moderated']])
    guild.members = [member]
    run_command(cog, interaction, '@MostAadTech')
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()
    assert len(store.rows) == 1
    assert 'could not assign Speaker' in interaction.followup.send.call_args.args[0]


def test_storage_round_trip_survives_new_store_and_binds_before_consume():
    """Exercise actual Supabase query contract, not the in-memory cog stand-in."""
    client = MagicMock()
    query = client.table.return_value
    for method in ('upsert', 'select', 'eq', 'or_', 'update', 'is_', 'delete'):
        getattr(query, method).return_value = query
    row = dict(id='approval-id', guild_id=456, username='mostaadtech', user_id=None)
    query.execute.return_value = SimpleNamespace(data=[row])
    store = DirectInviteApprovals(client)
    saved = store.save(456, InviteRecipient(username='mostaadtech'), 999)
    payload = query.upsert.call_args.args[0]
    assert payload['recipient_key'] == 'username:mostaadtech'
    assert payload['approved_by'] == 999
    assert query.upsert.call_args.kwargs['on_conflict'] == 'guild_id,recipient_key'
    recreated = DirectInviteApprovals(client)
    assert recreated.matching(456, USER_ID, 'MostAadTech') == [row]
    assert query.or_.call_args.args[0] == f'user_id.eq.{USER_ID},and(username.eq.mostaadtech,user_id.is.null)'
    assert recreated.bind(saved, USER_ID) is True
    query.update.assert_called_once_with({'user_id': USER_ID})
    query.is_.assert_called_once_with('user_id', 'null')
    recreated.consume(saved)
    query.delete.assert_called_once()
    assert ('id', 'approval-id') in [call.args for call in query.eq.call_args_list]
    client.table.assert_called_with('direct_invite_approvals')


def test_storage_rejects_unconfirmed_write_and_propagates_failure():
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.return_value = SimpleNamespace(data=[])
    with pytest.raises(RuntimeError, match='not saved'):
        DirectInviteApprovals(client).save(456, InviteRecipient(user_id=USER_ID), 999)
    with pytest.raises(RuntimeError, match='unavailable'):
        DirectInviteApprovals(None).save(456, InviteRecipient(user_id=USER_ID), 999)


def test_storage_bind_does_not_take_approval_claimed_by_another_account():
    client = MagicMock()
    query = client.table.return_value
    for method in ('update', 'eq', 'is_', 'select'):
        getattr(query, method).return_value = query
    query.execute.side_effect = [SimpleNamespace(data=[]), SimpleNamespace(data=[{'user_id': OTHER_ID}])]
    approval = dict(id='approval-id', guild_id=456, username='mostaadtech', user_id=None)
    assert DirectInviteApprovals(client).bind(approval, USER_ID) is False
