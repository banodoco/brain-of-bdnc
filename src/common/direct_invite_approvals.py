"""Durable, guild-scoped approvals for direct Speaker invites."""

from dataclasses import dataclass
import re
from uuid import uuid4


@dataclass(frozen=True)
class InviteRecipient:
    user_id: int | None = None
    username: str | None = None

    @property
    def key(self) -> str:
        return f"id:{self.user_id}" if self.user_id else f"username:{self.username}"


def parse_invite_recipient(value: str) -> InviteRecipient:
    """Accept an ID/mention or a current account username, never a display name."""
    value = value.strip()
    mention = re.fullmatch(r"<@!?(\d+)>", value)
    if mention or (value.isdecimal() and len(value) >= 17):
        digits = mention.group(1) if mention else value
        if not re.fullmatch(r"[0-9]{17,20}", digits) or not 0 < int(digits) < 2**63:
            raise ValueError("Enter a valid Discord user ID (17–20 digits), or prefix a numeric username with @.")
        return InviteRecipient(user_id=int(digits))
    username = value.removeprefix('@').lower()
    if not re.fullmatch(r"[a-z0-9_.]{2,32}", username) or '..' in username:
        raise ValueError("Enter a Discord user ID or exact account username (not a display name or nickname).")
    return InviteRecipient(username=username)


class DirectInviteApprovals:
    """No local cache: approvals survive restarts and are shared by bot instances.

    Database errors intentionally propagate. A missing table must never produce
    a successful approval response or silently discard an approval.
    """

    def __init__(self, supabase, write_allowed):
        self.supabase = supabase
        if not callable(write_allowed):
            raise TypeError("write_allowed must be the DatabaseHandler guild write gate")
        self.write_allowed = write_allowed

    def _require_write(self, guild_id: int) -> None:
        if not self.write_allowed(guild_id):
            raise PermissionError(f"Writes are not enabled for guild {guild_id}")

    def _table(self):
        if self.supabase is None:
            raise RuntimeError("Direct invite approval storage is unavailable")
        return self.supabase.table('direct_invite_approvals')

    def save(self, guild_id: int, recipient: InviteRecipient, approved_by: int) -> dict:
        self._require_write(guild_id)
        row = {
            'id': str(uuid4()),
            'guild_id': guild_id,
            'recipient_key': recipient.key,
            'user_id': recipient.user_id,
            'username': recipient.username,
            'approved_by': approved_by,
        }
        result = self._table().upsert(row, on_conflict='guild_id,recipient_key').execute()
        if not result.data:
            raise RuntimeError("Direct invite approval was not saved")
        return result.data[0]

    def matching(self, guild_id: int, user_id: int, username: str) -> list[dict]:
        # Usernames are validated before interpolation into the PostgREST filter.
        try:
            parsed = parse_invite_recipient('@' + username)
        except ValueError:
            parsed = InviteRecipient()
        condition = f'user_id.eq.{user_id}'
        if parsed.username:
            condition += f',and(username.eq.{parsed.username},user_id.is.null)'
        return self._table().select('*').eq('guild_id', guild_id).or_(condition).execute().data or []

    def bind(self, approval: dict, user_id: int) -> bool:
        """Bind a username approval once so a retry cannot approve another account."""
        self._require_write(approval['guild_id'])
        if approval.get('user_id') is not None:
            return int(approval['user_id']) == user_id
        result = (self._table().update({'user_id': user_id})
                  .eq('guild_id', approval['guild_id']).eq('id', approval['id'])
                  .is_('user_id', 'null').execute())
        if result.data:
            return True
        # Another bot instance may have bound the same approval already.
        rows = (self._table().select('user_id').eq('guild_id', approval['guild_id'])
                .eq('id', approval['id']).execute().data or [])
        return bool(rows and rows[0].get('user_id') == user_id)

    def consume(self, approval: dict) -> None:
        # Delete the specific version, never a newer approval for the same user.
        self._require_write(approval['guild_id'])
        self._table().delete().eq('guild_id', approval['guild_id']).eq('id', approval['id']).execute()
