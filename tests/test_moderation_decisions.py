import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from conftest import load_module_from_repo


pytestmark = pytest.mark.anyio


reactor_module = load_module_from_repo(
    "src/features/reacting/reactor_cog.py",
    "src.features.reacting.tests_reactor_cog_moderation",
)
db_handler_module = load_module_from_repo(
    "src/common/db_handler.py",
    "src.common.tests_db_handler_moderation",
)

ReactorCog = reactor_module.ReactorCog
DatabaseHandler = db_handler_module.DatabaseHandler


class FakeDMChannel:
    def __init__(self):
        self.send = AsyncMock()


class FakeUser:
    def __init__(self, user_id, name, *, bot=False):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = bot
        self.mention = f"<@{user_id}>"
        self._dm = FakeDMChannel()

    async def create_dm(self):
        return self._dm


class FakeChannel:
    def __init__(self, channel_id, message):
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.type = discord.ChannelType.text
        self._message = message
        self.fetch_message = AsyncMock(return_value=message)


class FakeMessage:
    def __init__(self, message_id, guild_id, channel_id, author, content):
        self.id = message_id
        self.guild = SimpleNamespace(id=guild_id)
        self.author = author
        self.content = content
        self.channel = None
        self.jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        self.remove_reaction = AsyncMock()


class FakeLoggerCog:
    def __init__(self):
        self.log_reaction_add = AsyncMock()
        self.log_reaction_remove = AsyncMock()


class FakeReactorInstance:
    def __init__(self):
        self.execute_reaction_action = AsyncMock()
        self.execute_message_action = AsyncMock()

    def check_reaction(self, reaction, user):
        return None

    def check_message(self, message):
        return None


class FakeBot:
    def __init__(self, db_handler, logger_cog):
        self.db_handler = db_handler
        self.reactor_instance = FakeReactorInstance()
        self._cogs = {"LoggerCog": logger_cog}
        self._users = {}
        self._channels = {}
        self.fetch_user = AsyncMock(side_effect=self._fetch_user)
        self.fetch_channel = AsyncMock(side_effect=self._fetch_channel)

    def get_cog(self, name):
        return self._cogs.get(name)

    def get_user(self, user_id):
        return self._users.get(user_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def _fetch_user(self, user_id):
        return self._users[user_id]

    async def _fetch_channel(self, channel_id):
        return self._channels[channel_id]


def _make_db_handler():
    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = SimpleNamespace(supabase_client=SimpleNamespace())
    db.server_config = SimpleNamespace(
        is_write_allowed=lambda guild_id: True,
        is_feature_enabled=lambda guild_id, channel_id, feature: True,
        get_enabled_servers=lambda require_write=False: [],
        get_server_field=lambda guild_id, field, cast=int: None,
    )
    db._record_calls = []
    db._active_reactors = []
    db._message_snapshot = None

    def record_moderation_decision(**kwargs):
        db._record_calls.append(kwargs)
        return True

    def get_active_reactors(message_id, emoji=None):
        rows = [dict(row) for row in db._active_reactors]
        if emoji is not None:
            rows = [row for row in rows if row["emoji"] == emoji]
        return rows

    def get_message_snapshot(message_id):
        return dict(db._message_snapshot) if db._message_snapshot is not None else None

    db.record_moderation_decision = record_moderation_decision
    db.get_active_reactors = get_active_reactors
    db.get_message_snapshot = get_message_snapshot
    return db


@pytest.fixture
def reactor_env(monkeypatch):
    db_handler = _make_db_handler()
    logger_cog = FakeLoggerCog()
    bot = FakeBot(db_handler, logger_cog)
    cog = ReactorCog(bot, logging.getLogger("test.moderation_decisions"), dev_mode=False)
    monkeypatch.setattr(cog, "_is_feature_enabled", lambda guild_id, channel_id, feature: True)

    created_tasks = []
    original_create_task = asyncio.create_task

    def track_task(coro):
        task = original_create_task(coro)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(reactor_module.asyncio, "create_task", track_task)

    async def drain_tasks():
        if created_tasks:
            pending = list(created_tasks)
            created_tasks.clear()
            await asyncio.gather(*pending)

    return SimpleNamespace(
        bot=bot,
        cog=cog,
        db_handler=db_handler,
        logger_cog=logger_cog,
        drain_tasks=drain_tasks,
    )


def prepare_message_env(reactor_env, *, emoji, content="hello world", message_id=1001, channel_id=2002, guild_id=3003):
    user = FakeUser(42, "Reactor")
    author = FakeUser(84, "Author")
    message = FakeMessage(message_id, guild_id, channel_id, author, content)
    channel = FakeChannel(channel_id, message)
    message.channel = channel
    reactor_env.bot._users[user.id] = user
    reactor_env.bot._channels[channel.id] = channel
    payload = SimpleNamespace(
        user_id=user.id,
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
        emoji=emoji,
    )
    return SimpleNamespace(user=user, author=author, message=message, channel=channel, payload=payload)


@pytest.mark.parametrize(
    ("emoji", "category"),
    [
        ("🇺🇸", "flag"),
        ("🍉", "political"),
        ("☪", "religious"),
    ],
)
async def test_restricted_reaction_add_logs_once_and_gateway_remove_is_noop(reactor_env, emoji, category):
    ctx = prepare_message_env(reactor_env, emoji=emoji)

    await reactor_env.cog.on_raw_reaction_add(ctx.payload)

    assert len(reactor_env.db_handler._record_calls) == 1
    assert reactor_env.db_handler._record_calls[0]["classification"] == "bot_auto_restricted"
    assert reactor_env.db_handler._record_calls[0]["reason"] == category
    assert reactor_env.cog._bot_initiated_removals[(ctx.message.id, ctx.user.id, emoji)][0] == "bot_auto_restricted"

    await reactor_env.cog.on_raw_reaction_remove(ctx.payload)
    await reactor_env.drain_tasks()

    assert len(reactor_env.db_handler._record_calls) == 1


async def test_curator_marker_consumes_remove_without_user_self_removal(reactor_env):
    ctx = prepare_message_env(reactor_env, emoji="❌")
    reactor_env.cog.register_bot_removal(
        ctx.message.id,
        ctx.user.id,
        "❌",
        "bot_curator_reject",
        reason="curator_reject",
    )

    await reactor_env.cog.on_raw_reaction_remove(ctx.payload)
    await reactor_env.drain_tasks()

    assert reactor_env.db_handler._record_calls == [
        {
            "message_id": ctx.message.id,
            "channel_id": ctx.channel.id,
            "guild_id": ctx.message.guild.id,
            "reactor_user_id": ctx.user.id,
            "reactor_name": ctx.user.display_name,
            "emoji": "❌",
            "message_author_id": ctx.author.id,
            "message_author_name": ctx.author.display_name,
            "message_content_snippet": "hello world",
            "classification": "bot_curator_reject",
            "reason": "curator_reject",
            "is_suspicious": False,
        }
    ]


async def test_plain_self_removal_does_not_dm_admin(reactor_env):
    ctx = prepare_message_env(reactor_env, emoji="👍")

    await reactor_env.cog.on_raw_reaction_remove(ctx.payload)
    await reactor_env.drain_tasks()

    assert reactor_env.db_handler._record_calls[0]["classification"] == "user_self_removal"
    assert reactor_env.db_handler._record_calls[0]["is_suspicious"] is False
    reactor_env.bot.fetch_user.assert_not_awaited()


@pytest.mark.parametrize("emoji", ["🤮", "👎", "😭"])
async def test_suspicious_self_removal_dms_admin_with_context(reactor_env, monkeypatch, emoji):
    content = "x" * 250
    ctx = prepare_message_env(reactor_env, emoji=emoji, content=content)
    admin_user = FakeUser(999, "Admin")
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    reactor_env.bot.fetch_user = AsyncMock(return_value=admin_user)

    await reactor_env.cog.on_raw_reaction_remove(ctx.payload)
    await reactor_env.drain_tasks()

    assert reactor_env.db_handler._record_calls[0]["classification"] == "user_self_removal"
    assert reactor_env.db_handler._record_calls[0]["is_suspicious"] is True
    reactor_env.bot.fetch_user.assert_awaited_once_with(999)
    admin_user._dm.send.assert_awaited_once()
    sent_content = admin_user._dm.send.await_args.args[0]
    assert ctx.user.mention in sent_content
    assert emoji in sent_content
    assert ctx.message.jump_url in sent_content
    assert content[:200] in sent_content
    assert content[:201] not in sent_content


async def test_reaction_clear_writes_one_row_per_active_reactor(reactor_env):
    reactor_env.db_handler._message_snapshot = {
        "author_id": 84,
        "author_name": "Author",
        "content": "clear all content",
        "channel_id": 2002,
        "guild_id": 3003,
    }
    reactor_env.db_handler._active_reactors = [
        {"user_id": 1, "emoji": "😀", "guild_id": 3003},
        {"user_id": 2, "emoji": "😎", "guild_id": 3003},
        {"user_id": 3, "emoji": "🔥", "guild_id": 3003},
    ]

    await reactor_env.cog.on_raw_reaction_clear(
        SimpleNamespace(message_id=1001, channel_id=2002, guild_id=3003)
    )

    assert len(reactor_env.db_handler._record_calls) == 3
    assert {row["classification"] for row in reactor_env.db_handler._record_calls} == {"moderator_cleared_all"}
    assert {row["emoji"] for row in reactor_env.db_handler._record_calls} == {"😀", "😎", "🔥"}


async def test_reaction_clear_emoji_filters_to_target_emoji(reactor_env):
    reactor_env.db_handler._message_snapshot = {
        "author_id": 84,
        "author_name": "Author",
        "content": "clear one emoji",
        "channel_id": 2002,
        "guild_id": 3003,
    }
    reactor_env.db_handler._active_reactors = [
        {"user_id": 1, "emoji": "🔥", "guild_id": 3003},
        {"user_id": 2, "emoji": "🔥", "guild_id": 3003},
        {"user_id": 3, "emoji": "😀", "guild_id": 3003},
    ]

    await reactor_env.cog.on_raw_reaction_clear_emoji(
        SimpleNamespace(message_id=1001, channel_id=2002, guild_id=3003, emoji="🔥")
    )

    assert len(reactor_env.db_handler._record_calls) == 2
    assert {row["classification"] for row in reactor_env.db_handler._record_calls} == {"moderator_cleared_emoji"}
    assert {row["emoji"] for row in reactor_env.db_handler._record_calls} == {"🔥"}


async def test_message_delete_cascade_uses_snapshot_fields(reactor_env):
    reactor_env.db_handler._message_snapshot = {
        "author_id": 84,
        "author_name": "Author",
        "content": "message deleted body",
        "channel_id": 2002,
        "guild_id": 3003,
    }
    reactor_env.db_handler._active_reactors = [
        {"user_id": 1, "emoji": "🔥", "guild_id": 3003},
        {"user_id": 2, "emoji": "😀", "guild_id": 3003},
    ]

    await reactor_env.cog.on_raw_message_delete(
        SimpleNamespace(message_id=1001, channel_id=2002, guild_id=3003)
    )

    assert len(reactor_env.db_handler._record_calls) == 2
    assert {row["classification"] for row in reactor_env.db_handler._record_calls} == {"message_deleted_cascade"}
    assert all(row["message_author_id"] == 84 for row in reactor_env.db_handler._record_calls)
    assert all(row["message_author_name"] == "Author" for row in reactor_env.db_handler._record_calls)
    assert all(row["message_content_snippet"] == "message deleted body" for row in reactor_env.db_handler._record_calls)


@pytest.mark.parametrize(
    ("emoji", "expected"),
    [
        ("🇺🇸", "flag"),
        ("🏴\U000e0067\U000e0062\U000e007f", "flag"),
        ("🍉", "political"),
        ("🔻", "political"),
        ("✝", "religious"),
        ("☪", "religious"),
        ("🕉", "religious"),
        ("👍", None),
    ],
)
def test_classify_restricted_emoji(emoji, expected):
    assert ReactorCog._classify_restricted_emoji(emoji) == expected


def test_register_bot_removal_prunes_entries_older_than_sixty_seconds(reactor_env, monkeypatch):
    current = {"value": 0}
    monkeypatch.setattr(reactor_module.time, "monotonic", lambda: current["value"])

    reactor_env.cog.register_bot_removal(1, 2, "😀", "bot_auto_restricted", reason="flag")
    assert (1, 2, "😀") in reactor_env.cog._bot_initiated_removals

    current["value"] = 61
    reactor_env.cog.register_bot_removal(3, 4, "🔥", "bot_curator_reject", reason="curator_reject")

    assert (1, 2, "😀") not in reactor_env.cog._bot_initiated_removals
    assert (3, 4, "🔥") in reactor_env.cog._bot_initiated_removals
