from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.features.payments.payment_cog as payment_cog_module
import src.features.sharing.sharing_cog as sharing_cog_module
from src.features.grants.grants_cog import GrantsCog
from src.features.sharing.models import SocialPublishResult


pytestmark = pytest.mark.anyio


class FakeSharer:
    def __init__(self, bot, db_handler, logger_instance):
        self.bot = bot
        self.db_handler = db_handler
        self.logger_instance = logger_instance


class FakeSupabaseResult:
    def __init__(self, data):
        self.data = data


class FakeSupabaseUpdate:
    def __init__(self, recorder, payload):
        self.recorder = recorder
        self.payload = payload
        self.filters = []

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def execute(self):
        self.recorder.append({"payload": self.payload, "filters": self.filters})
        return FakeSupabaseResult([])


class FakeSupabaseTable:
    def __init__(self, recorder):
        self.recorder = recorder

    def update(self, payload):
        return FakeSupabaseUpdate(self.recorder, payload)


class FakeSupabase:
    def __init__(self):
        self.updates = []

    def table(self, _name):
        return FakeSupabaseTable(self.updates)


class FakeDB:
    def __init__(self, claimed=None):
        self.claimed = claimed or []
        self.claim_limits = []
        self.supabase = FakeSupabase()

    def claim_due_social_publications(self, limit):
        self.claim_limits.append(limit)
        return list(self.claimed)


class FakeService:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    async def execute_publication(self, publication_id):
        self.executed.append(publication_id)
        return self.results.pop(0)


class FakeBot:
    def __init__(self, service):
        self.social_publish_service = service
        self.ready_waits = 0
        self._is_ready = False

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready


class FakePaymentChannel:
    def __init__(self):
        self.messages = []

    async def send(self, content, view=None):
        self.messages.append({"content": content, "view": view})
        return SimpleNamespace(content=content, view=view)


class FakePaymentDB:
    def __init__(self, claimed=None):
        self.claimed = claimed or []
        self.claim_limits = []
        self.payments = {}

    def claim_due_payment_requests(self, limit):
        self.claim_limits.append(limit)
        return list(self.claimed)

    def get_payment_request(self, payment_id, guild_id=None):
        payment = self.payments.get(payment_id)
        if payment and guild_id is not None and payment.get("guild_id") != guild_id:
            return None
        return payment

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        payment = self.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return False
        payment["status"] = "manual_hold"
        payment["last_error"] = reason
        return True


class FakePaymentService:
    def __init__(self, pending=None, recovered=None, execute_results=None):
        self.pending = pending or []
        self.recovered = recovered or []
        self.execute_results = list(execute_results or [])
        self.execute_calls = []
        self.recover_calls = []

    def get_pending_confirmation_payments(self, guild_ids=None):
        return list(self.pending)

    async def recover_inflight(self, guild_ids=None):
        self.recover_calls.append(guild_ids)
        return list(self.recovered)

    async def execute_payment(self, payment_id, guild_id=None):
        self.execute_calls.append((payment_id, guild_id))
        return self.execute_results.pop(0)


class FakeProducerCog:
    def __init__(self):
        self.handled = []

    async def handle_payment_result(self, payment):
        self.handled.append(payment["payment_id"])


class FakePaymentBot:
    def __init__(self, payment_service, channel=None, producer_cog=None):
        self.payment_service = payment_service
        self.ready_waits = 0
        self.added_views = []
        self.channel = channel or FakePaymentChannel()
        self.producer_cog = producer_cog
        self.db_handler = None
        self.claude_client = object()
        self.guilds = []
        self._is_ready = False

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))

    def get_cog(self, name):
        if name == "PaymentCog":
            return self.producer_cog
        if name == "GrantsCog":
            return self.producer_cog
        return None

    def get_channel(self, channel_id):
        if channel_id == getattr(self.channel, "id", 999):
            return self.channel
        return None

    async def fetch_channel(self, channel_id):
        if channel_id == getattr(self.channel, "id", 999):
            return self.channel
        raise RuntimeError("unknown channel")


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, ephemeral=False):
        self.deferred = ephemeral


class FakeInteractionFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteractionMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, user_id, guild_id=1):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeInteractionResponse()
        self.followup = FakeInteractionFollowup()
        self.message = FakeInteractionMessage()


class FakeGrantsDB:
    def __init__(self):
        self.server_config = SimpleNamespace(
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 10},
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
        )
        self.storage_handler = None
        self.wallet_calls = []
        self.status_updates = []
        self.recorded_payments = []
        self.grant = {
            "thread_id": 1001,
            "guild_id": 1,
            "applicant_id": 222,
            "total_cost_usd": 42.5,
            "gpu_type": "a10",
            "recommended_hours": 5,
            "status": "payment_requested",
        }

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        self.wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        return {"wallet_id": "wallet-1", "wallet_address": address}

    def update_grant_status(self, thread_id, status, guild_id=None, **kwargs):
        self.grant["status"] = status
        self.grant.update(kwargs)
        self.status_updates.append((thread_id, status, guild_id, kwargs))
        return True

    def get_grant_by_thread(self, thread_id, guild_id=None):
        if thread_id == self.grant["thread_id"] and (guild_id is None or guild_id == self.grant["guild_id"]):
            return dict(self.grant)
        return None

    def record_grant_payment(self, thread_id, tx_signature, sol_amount, sol_price_usd, guild_id=None):
        self.recorded_payments.append((thread_id, tx_signature, sol_amount, sol_price_usd, guild_id))
        self.grant["status"] = "paid"
        return True


class FakeGrantThread:
    def __init__(self, thread_id=1001, parent_id=10):
        self.id = thread_id
        self.parent_id = parent_id
        self.guild = SimpleNamespace(id=1)
        self.messages = []
        self.edits = []

    async def send(self, content):
        self.messages.append(content)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeGrantPaymentService:
    def __init__(self):
        self.request_calls = []
        self.confirm_calls = []

    async def request_payment(self, **kwargs):
        self.request_calls.append(kwargs)
        return {
            "payment_id": "pay-test" if kwargs["is_test"] else "pay-final",
            "status": "pending_confirmation",
            **kwargs,
        }

    def confirm_payment(self, payment_id, **kwargs):
        self.confirm_calls.append((payment_id, kwargs))
        return {"payment_id": payment_id, "status": "queued"}


class FakeGrantPaymentCog:
    def __init__(self):
        self.sent = []

    async def send_confirmation_request(self, payment_id):
        self.sent.append(payment_id)
        return SimpleNamespace(id=payment_id)


async def test_scheduler_lifecycle_starts_and_stops_via_cog_load(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    db_handler = FakeDB()
    service = FakeService([])
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)

    calls = []
    monkeypatch.setattr(cog.scheduled_publication_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.scheduled_publication_worker, "start", lambda: calls.append("start"))
    await cog.cog_load()

    monkeypatch.setattr(cog.scheduled_publication_worker, "is_running", lambda: True)
    monkeypatch.setattr(cog.scheduled_publication_worker, "cancel", lambda: calls.append("cancel"))
    cog.cog_unload()

    assert calls == ["start", "cancel"]


async def test_scheduler_claims_executes_and_waits_for_bot_ready(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    publication = {"publication_id": "pub-1", "platform": "twitter", "action": "post", "attempt_count": 1}
    db_handler = FakeDB(claimed=[publication])
    service = FakeService([SocialPublishResult(publication_id="pub-1", success=True)])
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)

    await cog._before_scheduled_publication_worker()
    await cog.scheduled_publication_worker.coro(cog)

    assert bot.ready_waits == 1
    assert db_handler.claim_limits == [cog.claim_batch_size]
    assert service.executed == ["pub-1"]


async def test_scheduler_retries_transient_failures_and_respects_retry_budget(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    db_handler = FakeDB()
    service = FakeService(
        [
            SocialPublishResult(publication_id="pub-2", success=False, error="timeout from provider"),
            SocialPublishResult(publication_id="pub-3", success=False, error="timeout from provider"),
        ]
    )
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)
    cog.max_attempts = 3
    cog.retry_delay_seconds = 60

    await cog._process_claimed_publication(
        {
            "publication_id": "pub-2",
            "guild_id": 1,
            "platform": "twitter",
            "action": "post",
            "attempt_count": 1,
        }
    )
    await cog._process_claimed_publication(
        {
            "publication_id": "pub-3",
            "guild_id": 1,
            "platform": "twitter",
            "action": "post",
            "attempt_count": 3,
        }
    )

    assert service.executed == ["pub-2", "pub-3"]
    assert len(db_handler.supabase.updates) == 1
    update = db_handler.supabase.updates[0]
    assert update["payload"]["status"] == "queued"
    assert update["payload"]["last_error"] == "timeout from provider"
    assert ("publication_id", "pub-2") in update["filters"]
    assert ("guild_id", 1) in update["filters"]


async def test_payment_scheduler_lifecycle_registers_views_and_starts_worker():
    payment_service = FakePaymentService(
        pending=[
            {
                "payment_id": "pay-pending",
                "guild_id": 1,
                "status": "pending_confirmation",
            }
        ],
        recovered=[],
    )
    db_handler = FakePaymentDB()
    db_handler.server_config = None
    bot = FakePaymentBot(payment_service)
    cog = payment_cog_module.PaymentCog(bot, db_handler, payment_service=payment_service)

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cog.payment_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.payment_worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(cog.payment_worker, "change_interval", lambda **kwargs: calls.append(kwargs["seconds"]))

    await cog.cog_load()
    assert len(bot.added_views) == 0
    await cog.on_ready()

    monkeypatch.setattr(cog.payment_worker, "is_running", lambda: True)
    monkeypatch.setattr(cog.payment_worker, "cancel", lambda: calls.append("cancel"))
    cog.cog_unload()
    monkeypatch.undo()

    assert calls == [cog.worker_interval_seconds, "start", "cancel"]
    assert len(bot.added_views) == 1
    view, message_id = bot.added_views[0]
    assert isinstance(view, payment_cog_module.PaymentConfirmView)
    assert view.payment_id == "pay-pending"
    assert message_id is None


async def test_payment_scheduler_claims_executes_notifies_and_hands_off():
    claimed = {"payment_id": "pay-1", "guild_id": 1}
    terminal_payment = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "thread-1",
        "recipient_wallet": "ABCDE12345FGHIJ67890",
        "chain": "solana",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.25,
        "status": "confirmed",
        "notify_channel_id": 999,
        "notify_thread_id": None,
        "tx_signature": "sig-123",
        "last_error": None,
    }
    db_handler = FakePaymentDB(claimed=[claimed])
    payment_service = FakePaymentService(execute_results=[terminal_payment])
    producer_cog = FakeProducerCog()
    bot = FakePaymentBot(payment_service, producer_cog=producer_cog)
    cog = payment_cog_module.PaymentCog(bot, db_handler, payment_service=payment_service)

    await cog.payment_worker.coro(cog)

    assert db_handler.claim_limits == [cog.claim_batch_size]
    assert payment_service.execute_calls == [("pay-1", 1)]
    assert len(bot.channel.messages) == 1
    assert "Payment Confirmed" in bot.channel.messages[0]["content"]
    assert producer_cog.handled == ["pay-1"]


async def test_payment_scheduler_replays_pending_terminal_handoff_on_ready():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    bot = FakePaymentBot(payment_service, producer_cog=None)
    cog = payment_cog_module.PaymentCog(bot, db_handler, payment_service=payment_service)
    payment = {
        "payment_id": "pay-recovered",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "thread-1",
        "status": "confirmed",
    }

    await cog._handoff_terminal_result(payment)
    assert "pay-recovered" in cog._pending_terminal_handoffs

    producer_cog = FakeProducerCog()
    bot.producer_cog = producer_cog
    await cog.on_ready()

    assert producer_cog.handled == ["pay-recovered"]
    assert cog._pending_terminal_handoffs == {}


async def test_payment_cog_load_does_not_block_on_wait_until_ready():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    bot = FakePaymentBot(payment_service)
    cog = payment_cog_module.PaymentCog(bot, db_handler, payment_service=payment_service)

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cog.payment_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.payment_worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(cog.payment_worker, "change_interval", lambda **kwargs: calls.append(kwargs["seconds"]))

    await cog.cog_load()
    monkeypatch.undo()

    assert bot.ready_waits == 0
    assert calls == [cog.worker_interval_seconds, "start"]


async def test_payment_confirm_view_rejects_non_recipient():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    db_handler.payments["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "status": "pending_confirmation",
        "recipient_discord_id": 123,
    }
    bot = FakePaymentBot(payment_service)
    cog = payment_cog_module.PaymentCog(bot, db_handler, payment_service=payment_service)
    view = payment_cog_module.PaymentConfirmView(cog, "pay-1")
    interaction = FakeInteraction(user_id=999)

    await view._confirm_button_pressed(interaction)

    assert interaction.followup.messages == [("Only the intended recipient can confirm this payment.", True)]
    assert payment_service.execute_calls == []


async def test_grants_wallet_submission_and_test_confirmation_queue_final_payment():
    payment_service = FakeGrantPaymentService()
    payment_cog = FakeGrantPaymentCog()
    db_handler = FakeGrantsDB()
    thread = FakeGrantThread()

    bot = FakePaymentBot(payment_service, channel=thread, producer_cog=payment_cog)
    bot.db_handler = db_handler
    bot.payment_service = payment_service
    bot.get_cog = lambda name: payment_cog if name == "PaymentCog" else None

    cog = GrantsCog(bot)
    cog._tags["in progress"] = object()
    async def noop_apply_tag(_thread, _tag_name):
        return None
    cog._apply_tag = noop_apply_tag
    async def fake_fetch_grant_thread(_thread_id):
        return thread
    cog._fetch_grant_thread = fake_fetch_grant_thread

    await cog._start_payment_flow(thread, dict(db_handler.grant), "Wallet111111111111111111111111111111111")

    assert db_handler.wallet_calls[0][:4] == (1, 222, "solana", "Wallet111111111111111111111111111111111")
    assert payment_service.request_calls[0]["is_test"] is True
    assert payment_service.confirm_calls == [("pay-test", {"guild_id": 1, "confirmed_by": "auto", "confirmed_by_user_id": 222})]
    assert db_handler.status_updates[-1][1] == "payment_requested"

    test_payment = {
        "payment_id": "pay-test",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "1001",
        "recipient_wallet": "Wallet111111111111111111111111111111111",
        "wallet_id": "wallet-1",
        "chain": "solana",
        "provider": "solana",
        "is_test": True,
        "status": "confirmed",
        "confirm_channel_id": 10,
        "confirm_thread_id": 1001,
        "notify_channel_id": 10,
        "notify_thread_id": 1001,
        "route_key": None,
    }
    bot.get_channel = lambda channel_id: thread if channel_id == 1001 else None
    async def fetch_thread(_channel_id):
        return thread
    bot.fetch_channel = fetch_thread

    await cog.handle_payment_result(test_payment)

    assert len(payment_service.request_calls) == 2
    final_request = payment_service.request_calls[1]
    assert final_request["is_test"] is False
    assert final_request["amount_usd"] == 42.5
    assert payment_cog.sent == ["pay-final"]
    assert "Test payment confirmed." in thread.messages[-1]


async def test_grants_final_payment_confirmation_marks_grant_paid():
    payment_service = FakeGrantPaymentService()
    db_handler = FakeGrantsDB()
    thread = FakeGrantThread()
    bot = FakePaymentBot(payment_service, channel=thread)
    bot.db_handler = db_handler
    bot.payment_service = payment_service
    bot.get_cog = lambda name: None
    bot.get_channel = lambda channel_id: thread if channel_id == 1001 else None
    async def fetch_thread(_channel_id):
        return thread
    bot.fetch_channel = fetch_thread

    cog = GrantsCog(bot)
    async def fake_fetch_grant_thread(_thread_id):
        return thread
    cog._fetch_grant_thread = fake_fetch_grant_thread

    await cog.handle_payment_result(
        {
            "payment_id": "pay-final",
            "guild_id": 1,
            "producer": "grants",
            "producer_ref": "1001",
            "recipient_wallet": "Wallet111111111111111111111111111111111",
            "chain": "solana",
            "provider": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.5,
            "token_price_usd": 150.0,
            "tx_signature": "sig-123",
        }
    )

    assert db_handler.recorded_payments == [(1001, "sig-123", 1.5, 150.0, 1)]
    assert thread.edits == [{"archived": True}]
    assert "Payment sent!" in thread.messages[-1]
