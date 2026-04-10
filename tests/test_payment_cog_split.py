from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import load_module_from_repo
from src.features.payments.payment_ui_cog import PaymentUICog
from src.features.payments.payment_worker_cog import PaymentWorkerCog


pytestmark = pytest.mark.anyio


class FakePaymentService:
    def __init__(self, pending=None):
        self.pending = list(pending or [])

    def get_pending_confirmation_payments(self, guild_ids=None):
        if guild_ids is None:
            return list(self.pending)
        return [row for row in self.pending if row.get("guild_id") in guild_ids]

    async def recover_inflight(self, guild_ids=None):
        return []


class FakeDB:
    def __init__(self):
        self.server_config = SimpleNamespace(
            get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
        )


class FakeBot:
    def __init__(self, payment_service):
        self.payment_service = payment_service
        self.added_views = []
        self._cogs = {}
        self.ready_waits = 0
        self._is_ready = False

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))

    def get_cog(self, name):
        return self._cogs.get(name)

    def is_ready(self):
        return self._is_ready

    async def wait_until_ready(self):
        self.ready_waits += 1


async def test_split_cogs_preserve_view_registration_and_cap_breach_routing(monkeypatch):
    payment_service = FakePaymentService(
        pending=[{"payment_id": "pay-pending", "guild_id": 1, "status": "pending_confirmation"}]
    )
    bot = FakeBot(payment_service)
    db_handler = FakeDB()

    worker_cog = PaymentWorkerCog(bot, db_handler, payment_service=payment_service)
    worker_cog._dm_admin_payment_failure = AsyncMock()
    ui_cog = PaymentUICog(bot, db_handler, payment_service=payment_service)
    bot._cogs["PaymentWorkerCog"] = worker_cog
    bot._cogs["PaymentUICog"] = ui_cog

    await ui_cog.cog_load()

    assert len(bot.added_views) == 1
    assert bot.added_views[0][0].payment_id == "pay-pending"

    main_module = load_module_from_repo("main.py", "tests_main_payment_cog_split")
    notify = main_module._bind_cap_breach_dm(bot, main_module.logging.getLogger("test.payment_cog_split"))
    payment = {"payment_id": "pay-cap"}

    await notify(payment)

    worker_cog._dm_admin_payment_failure.assert_awaited_once_with(payment)
