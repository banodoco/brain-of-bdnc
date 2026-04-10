from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.features.payments.payment_worker_cog as payment_worker_cog_module


pytestmark = pytest.mark.anyio


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == getattr(self.channel, "id", None) else None

    async def fetch_channel(self, channel_id):
        if channel_id == getattr(self.channel, "id", None):
            return self.channel
        raise RuntimeError("unknown channel")

    def get_cog(self, _name):
        return None


async def test_terminal_cleanup_wiring_passes_cleanup_ids_and_empty_lists(monkeypatch):
    cleanup_calls = []

    async def fake_safe_delete_messages(channel, message_ids, *, logger):
        cleanup_calls.append((channel, list(message_ids)))
        return SimpleNamespace(deleted=0, skipped=0, errored=0)

    monkeypatch.setattr(payment_worker_cog_module, "safe_delete_messages", fake_safe_delete_messages)

    channel = SimpleNamespace(id=777)
    bot = FakeBot(channel)
    cog = payment_worker_cog_module.PaymentWorkerCog(
        bot,
        db_handler=SimpleNamespace(server_config=None),
        payment_service=SimpleNamespace(),
    )
    cog._notify_payment_result = AsyncMock()
    cog._handoff_terminal_result = AsyncMock()
    cog._dm_admin_payment_success = AsyncMock()
    cog._dm_admin_payment_failure = AsyncMock()

    base_payment = {
        "payment_id": "pay-cleanup",
        "status": "confirmed",
        "is_test": True,
        "notify_channel_id": 777,
        "notify_thread_id": None,
        "provider": "solana_native",
        "amount_usd": 0,
    }

    await cog._handle_terminal_payment(
        {
            **base_payment,
            "metadata": {"cleanup_message_ids": [101, 102]},
        }
    )
    await cog._handle_terminal_payment(
        {
            **base_payment,
            "payment_id": "pay-empty",
            "metadata": {"cleanup_message_ids": []},
        }
    )
    await cog._handle_terminal_payment(
        {
            **base_payment,
            "payment_id": "pay-missing",
            "metadata": {},
        }
    )

    assert cleanup_calls == [
        (channel, [101, 102]),
        (channel, []),
        (channel, []),
    ]
