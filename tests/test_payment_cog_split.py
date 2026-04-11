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


async def test_payment_ui_cog_loads_without_admin_chat_cog():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return []

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return []

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    cog = PaymentUICog(bot, LocalDB(), payment_service=bot.payment_service)

    await cog.cog_load()

    assert bot.ready_waits == 0


async def test_orphan_sweep_auto_skip_crash_cancels_payment():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return [
                {
                    "payment_id": "pay-orphan",
                    "guild_id": 1,
                    "producer": "admin_chat",
                    "is_test": False,
                    "status": "pending_confirmation",
                }
            ]

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )
            self.cancel_calls = []

        def find_admin_chat_intent_by_payment_id(self, payment_id):
            return None

        def cancel_payment(self, payment_id, guild_id=None, reason=None):
            self.cancel_calls.append((payment_id, guild_id, reason))
            return True

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return []

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    db = LocalDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)

    await cog._reconcile_admin_chat_orphans()

    assert db.cancel_calls == [
        ("pay-orphan", 1, "admin_chat pending_confirmation payment missing linked intent")
    ]


async def test_orphan_sweep_test_receipt_crash_completes_recovery():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return [
                {
                    "payment_id": "pay-final",
                    "guild_id": 1,
                    "producer": "admin_chat",
                    "is_test": False,
                    "status": "pending_confirmation",
                    "recipient_discord_id": 42,
                    "recipient_wallet": "Wallet1111111111111111111111111111111",
                }
            ]

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )
            self.intent = {
                "intent_id": "intent-1",
                "guild_id": 1,
                "channel_id": 55,
                "recipient_user_id": 42,
                "status": "awaiting_test_receipt_confirmation",
                "final_payment_id": None,
            }
            self.updated = []

        def find_admin_chat_intent_by_payment_id(self, payment_id):
            return dict(self.intent)

        def update_admin_payment_intent(self, intent_id, payload, guild_id):
            self.intent.update(dict(payload))
            self.updated.append((intent_id, dict(payload), guild_id))
            return dict(self.intent)

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return []

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    bot.fetch_user = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1))))
    db = LocalDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)
    cog._send_admin_approval_dm = AsyncMock(return_value=SimpleNamespace(id=1))

    await cog._reconcile_admin_chat_orphans()

    assert db.updated == [
        ("intent-1", {"status": "awaiting_admin_approval", "final_payment_id": "pay-final"}, 1)
    ]
    cog._send_admin_approval_dm.assert_awaited_once()


async def test_initiate_batch_payment_fan_out_crash_leaves_awaiting_admin_init_for_sweep():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return []

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )
            self.intent = {
                "intent_id": "intent-batch",
                "guild_id": 1,
                "channel_id": 55,
                "recipient_user_id": 42,
                "status": "awaiting_admin_init",
                "final_payment_id": None,
            }
            self.updated = []

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return [dict(self.intent)]

        def update_admin_payment_intent(self, intent_id, payload, guild_id):
            self.intent.update(dict(payload))
            self.updated.append((intent_id, dict(payload), guild_id))
            return dict(self.intent)

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    db = LocalDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)

    await cog._reconcile_admin_chat_orphans()

    assert db.updated == [("intent-batch", {"status": "cancelled"}, 1)]


async def test_orphan_sweep_leaves_legacy_awaiting_confirmation_alone():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return [
                {
                    "payment_id": "pay-legacy",
                    "guild_id": 1,
                    "producer": "admin_chat",
                    "is_test": False,
                    "status": "pending_confirmation",
                }
            ]

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )
            self.cancel_calls = []

        def find_admin_chat_intent_by_payment_id(self, payment_id):
            return {
                "intent_id": "intent-legacy",
                "guild_id": 1,
                "channel_id": 55,
                "recipient_user_id": 42,
                "status": "awaiting_confirmation",
                "final_payment_id": "pay-legacy",
            }

        def cancel_payment(self, payment_id, guild_id=None, reason=None):
            self.cancel_calls.append((payment_id, guild_id, reason))
            return True

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return []

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    db = LocalDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)

    await cog._reconcile_admin_chat_orphans()

    assert db.cancel_calls == []


async def test_orphan_sweep_runs_before_view_registration(monkeypatch):
    bot = FakeBot(FakePaymentService())
    db = FakeDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)
    order = []

    async def record_reconcile():
        order.append("reconcile")

    async def record_pending():
        order.append("pending")

    async def record_admin():
        order.append("admin")

    monkeypatch.setattr(cog, "_reconcile_admin_chat_orphans", record_reconcile)
    monkeypatch.setattr(cog, "_register_pending_confirmation_views", record_pending)
    monkeypatch.setattr(cog, "_register_pending_admin_approval_views", record_admin)

    await cog.cog_load()

    assert order[:3] == ["reconcile", "pending", "admin"]


async def test_orphan_sweep_loop_cancels_stale_awaiting_admin_init_during_uptime():
    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            return []

    class LocalDB:
        def __init__(self):
            self.server_config = SimpleNamespace(
                get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
            )
            self.intent = {
                "intent_id": "intent-loop",
                "guild_id": 1,
                "channel_id": 55,
                "recipient_user_id": 42,
                "status": "awaiting_admin_init",
                "final_payment_id": None,
            }

        def list_stale_awaiting_admin_init_intents(self, cutoff_iso):
            return [dict(self.intent)]

        def update_admin_payment_intent(self, intent_id, payload, guild_id):
            self.intent.update(dict(payload))
            return dict(self.intent)

        def list_intents_by_status(self, guild_id, status):
            return []

    bot = FakeBot(LocalPaymentService())
    db = LocalDB()
    cog = PaymentUICog(bot, db, payment_service=bot.payment_service)

    await cog._orphan_sweep_loop.coro(cog)

    assert db.intent["status"] == "cancelled"
