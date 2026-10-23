"""Recipient Speaker approvals must use the guild's existing write gate."""
import asyncio
from unittest.mock import MagicMock

import pytest

from src.common.direct_invite_approvals import DirectInviteApprovals, InviteRecipient
from tests.test_direct_invite_recipient import USER_ID, make_member, run_command, setup_invites


def denied_store(client=None):
    return DirectInviteApprovals(client or MagicMock(), lambda guild_id: False)


def test_read_only_store_rejects_every_mutation_before_database_access():
    client = MagicMock()
    store = denied_store(client)
    approval = {'id': 'approval-id', 'guild_id': 456, 'user_id': None}

    with pytest.raises(PermissionError, match='Writes are not enabled'):
        store.save(456, InviteRecipient(user_id=USER_ID), approved_by=999)
    with pytest.raises(PermissionError, match='Writes are not enabled'):
        store.bind(approval, USER_ID)
    with pytest.raises(PermissionError, match='Writes are not enabled'):
        store.consume(approval)

    client.table.assert_not_called()


def test_read_only_targeted_command_creates_no_invite_or_approval():
    cog, store, _, interaction, channel, _, _ = setup_invites()
    cog.db_handler._gate_check = lambda guild_id: False

    run_command(cog, interaction, '@MostAadTech')

    assert 'writes are not enabled' in interaction.followup.send.call_args.args[0]
    channel.create_invite.assert_not_called()
    assert store.rows == []


def test_read_only_join_keeps_pending_approval_without_role_changes():
    cog, store, guild, _, _, _, tier = setup_invites()
    cog.db_handler._gate_check = lambda guild_id: False
    store.rows.append({
        'id': 'approval-id', 'guild_id': guild.id, 'user_id': USER_ID,
        'username': None, 'recipient_key': f'id:{USER_ID}', 'approved_by': 999,
    })
    member = make_member(guild, tier)

    asyncio.run(cog.on_member_join(member))

    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()
    assert len(store.rows) == 1

