import asyncio
import logging

import discord
import pytest

from src.common.discord_utils import DeleteCounts, safe_delete_messages


pytestmark = pytest.mark.anyio


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "reason"
        self.headers = {}


class FakeMessage:
    def __init__(self, message_id, deleted):
        self.id = message_id
        self._deleted = deleted

    async def delete(self):
        self._deleted.append(self.id)


class FakeChannel:
    def __init__(self, behaviors):
        self.id = 555
        self.behaviors = dict(behaviors)
        self.attempts = {}
        self.deleted = []

    async def fetch_message(self, message_id):
        attempt = self.attempts.get(message_id, 0)
        self.attempts[message_id] = attempt + 1
        behavior = self.behaviors[message_id]
        if isinstance(behavior, list):
            current = behavior[min(attempt, len(behavior) - 1)]
        else:
            current = behavior
        if isinstance(current, Exception):
            raise current
        if current == "ok":
            return FakeMessage(message_id, self.deleted)
        raise RuntimeError(f"unexpected behavior: {current!r}")


def make_http_exception(status, text):
    exc = discord.HTTPException(FakeResponse(status), text)
    if status == 429:
        exc.retry_after = 0
    return exc


async def test_safe_delete_messages_handles_expected_error_modes(monkeypatch):
    channel = FakeChannel(
        {
            1: "ok",
            2: discord.NotFound(FakeResponse(404), "missing"),
            3: discord.Forbidden(FakeResponse(403), "forbidden"),
            4: [make_http_exception(429, "rate limited"), "ok"],
            5: make_http_exception(500, "server error"),
            6: AttributeError("missing fetch"),
        }
    )
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await safe_delete_messages(
        channel,
        [1, 2, 3, 4, 5, 6],
        logger=logging.getLogger("test.safe_delete_messages"),
    )

    assert result == DeleteCounts(deleted=2, skipped=1, errored=3)
    assert channel.deleted == [1, 4]
    assert sleep_calls == [0]


async def test_safe_delete_messages_handles_none_channel():
    result = await safe_delete_messages(
        None,
        [101, 102],
        logger=logging.getLogger("test.safe_delete_messages"),
    )

    assert result == DeleteCounts(deleted=0, skipped=2, errored=0)
