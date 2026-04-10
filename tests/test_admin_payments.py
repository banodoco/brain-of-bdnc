from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.common.db_handler import WalletUpdateBlockedError
from src.features.admin_chat.admin_chat_cog import AdminChatCog
from src.features.admin_chat.tools import execute_initiate_payment
from src.features.grants.grants_cog import GrantsCog
from src.features.payments.payment_cog import PaymentCog
from src.features.payments.payment_service import PaymentService


VALID_SOL_ADDRESS = "11111111111111111111111111111111"


class FakeClassifierResponse:
    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]


class FakeClassifierMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeClassifierResponse(self.text)


class FakeClassifierClient:
    def __init__(self, text):
        self.messages = FakeClassifierMessages(text)


class FakeAuthor:
    def __init__(self, user_id, *, bot=False):
        self.id = user_id
        self.bot = bot
        self.display_name = f"user-{user_id}"

    async def send(self, content, **kwargs):
        if not hasattr(self, "sent_messages"):
            self.sent_messages = []
        message = SimpleNamespace(content=content, kwargs=kwargs)
        self.sent_messages.append(message)
        return message


class FakeMessage:
    def __init__(self, message_id, author, channel, guild, content):
        self.id = message_id
        self.author = author
        self.channel = channel
        self.guild = guild
        self.content = content


class FakeChannel:
    def __init__(self, channel_id=55, guild=None, messages=None, *, parent_id=None):
        self.id = channel_id
        self.guild = guild or SimpleNamespace(id=1)
        self.parent_id = parent_id
        self._messages = list(messages or [])
        self.sent_messages = []

    async def send(self, content, **kwargs):
        message_id = 900 + len(self.sent_messages)
        message = SimpleNamespace(id=message_id, content=content, kwargs=kwargs)
        self.sent_messages.append(message)
        return message

    def history(self, limit=100, after=None, oldest_first=False):
        after_id = getattr(after, "id", None)
        messages = [msg for msg in self._messages if after_id is None or msg.id > after_id]
        messages.sort(key=lambda msg: msg.id, reverse=not oldest_first)
        messages = messages[:limit]

        async def iterator():
            for message in messages:
                yield message

        return iterator()


class FakeAdminPaymentCog:
    def __init__(self):
        self.confirmation_requests = []

    async def send_confirmation_request(self, payment_id):
        self.confirmation_requests.append(payment_id)
        return SimpleNamespace(id=f"confirm-{payment_id}")


class FakeFlowCog:
    def __init__(self):
        self.started = []

    async def _start_admin_payment_flow(self, channel, intent):
        self.started.append((channel, dict(intent)))


class FakeBot:
    def __init__(
        self,
        channel,
        *,
        payment_service=None,
        payment_cog=None,
        admin_chat_cog=None,
        guilds=None,
        db_handler=None,
    ):
        self.payment_service = payment_service
        self._channel = channel
        self._payment_cog = payment_cog
        self._admin_chat_cog = admin_chat_cog
        self.guilds = guilds or [channel.guild]
        self.user = SimpleNamespace(id=999)
        self.ready_waits = 0
        self.fetched_users = {}
        self._is_ready = False
        self.db_handler = db_handler
        self.claude_client = object()

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id):
        if channel_id == self._channel.id:
            return self._channel
        raise RuntimeError("unknown channel")

    async def fetch_user(self, user_id):
        user = self.fetched_users.get(user_id)
        if user is None:
            user = FakeAuthor(user_id)
            user.sent_messages = []
            self.fetched_users[user_id] = user
        return user

    def get_cog(self, name):
        if name == "PaymentCog":
            return self._payment_cog
        if name == "AdminChatCog":
            return self._admin_chat_cog
        return None

    def get_guild(self, guild_id):
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None


class FakeAdminPaymentService:
    def __init__(self, *, request_result=None, confirm_result=None):
        self.request_calls = []
        self.confirm_calls = []
        self.request_result = request_result
        self.confirm_result = confirm_result or {"payment_id": "pay-1", "status": "queued"}

    async def request_payment(self, **kwargs):
        self.request_calls.append(dict(kwargs))
        if self.request_result is None:
            return {
                "payment_id": "payment-final",
                "status": "pending_confirmation",
                **kwargs,
            }
        return self.request_result

    def confirm_payment(self, payment_id, **kwargs):
        self.confirm_calls.append((payment_id, dict(kwargs)))
        return self.confirm_result


class FakeIntentDB:
    TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

    def __init__(self):
        self.wallets = {}
        self.intents = {}
        self.active_by_key = {}
        self.payments = {}
        self.storage_handler = None
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.created_intents = []
        self.updated_intents = []
        self.upsert_wallet_calls = []
        self.cancel_payment_calls = []
        self.server_config = SimpleNamespace(
            get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "enabled": True, "write_enabled": True}],
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
            is_guild_enabled=lambda guild_id: True,
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 55},
        )

    def _sync_active_index(self, intent):
        key = (int(intent["guild_id"]), int(intent["channel_id"]), int(intent["recipient_user_id"]))
        if intent.get("status") in self.TERMINAL_STATUSES:
            self.active_by_key.pop(key, None)
        else:
            self.active_by_key[key] = intent

    def get_wallet(self, guild_id, discord_user_id, chain):
        wallet = self.wallets.get((guild_id, discord_user_id, chain))
        return dict(wallet) if wallet else None

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        for wallet in self.wallets.values():
            if wallet["wallet_id"] == wallet_id and (guild_id is None or wallet["guild_id"] == guild_id):
                return dict(wallet)
        return None

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def create_admin_payment_intent(self, record, guild_id):
        intent_id = f"intent-{len(self.intents) + 1}"
        intent = {"intent_id": intent_id, **dict(record), "guild_id": guild_id}
        self.intents[intent_id] = intent
        self.created_intents.append(dict(intent))
        self._sync_active_index(intent)
        return dict(intent)

    def get_active_intent_for_recipient(self, guild_id, channel_id, recipient_user_id):
        intent = self.active_by_key.get((guild_id, channel_id, recipient_user_id))
        return dict(intent) if intent else None

    def update_admin_payment_intent(self, intent_id, payload, guild_id):
        intent = self.intents.get(intent_id)
        if not intent or intent["guild_id"] != guild_id:
            return None
        intent.update(dict(payload))
        self.updated_intents.append((intent_id, dict(payload), guild_id))
        self._sync_active_index(intent)
        return dict(intent)

    def get_admin_payment_intent(self, intent_id, guild_id):
        intent = self.intents.get(intent_id)
        if not intent or intent["guild_id"] != guild_id:
            return None
        return dict(intent)

    def list_active_intents(self, guild_id):
        return [
            dict(intent)
            for intent in self.intents.values()
            if intent["guild_id"] == guild_id and intent.get("status") not in self.TERMINAL_STATUSES
        ]

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallets.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-{len(self.upsert_wallet_calls) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "now",
            "metadata": metadata,
        }
        self.upsert_wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        self.wallets[key] = wallet
        return dict(wallet)

    def get_payment_request(self, payment_id, guild_id=None):
        payment = self.payments.get(payment_id)
        if payment and guild_id is not None and payment.get("guild_id") != guild_id:
            return None
        return dict(payment) if payment else None

    def cancel_payment(self, payment_id, guild_id=None, reason=None):
        self.cancel_payment_calls.append((payment_id, guild_id, reason))
        payment = self.payments.get(payment_id)
        if payment:
            payment["status"] = "cancelled"
            payment["last_error"] = reason
        return True


class FakeProvider:
    def __init__(self):
        self.price_calls = 0

    async def get_token_price_usd(self):
        self.price_calls += 1
        return 200.0

    def token_name(self):
        return "SOL"


class FakePaymentRequestDB:
    def __init__(self):
        self.created = []
        self.wallets = {"wallet-1": {"wallet_id": "wallet-1", "wallet_address": VALID_SOL_ADDRESS, "guild_id": 1}}
        self.wallet_registry = {}
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test):
        return [
            dict(row)
            for row in self.created
            if row["guild_id"] == guild_id
            and row["producer"] == producer
            and row["producer_ref"] == producer_ref
            and row["is_test"] == is_test
        ]

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        wallet = self.wallets.get(wallet_id)
        if wallet and guild_id is not None and wallet["guild_id"] != guild_id:
            return None
        return dict(wallet) if wallet else None

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallet_registry.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-user-{len(self.wallet_registry) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "now",
            "metadata": metadata,
        }
        self.wallet_registry[key] = wallet
        self.wallets[wallet["wallet_id"]] = wallet
        return dict(wallet)

    def create_payment_request(self, record, guild_id=None):
        row = {"payment_id": f"payment-{len(self.created) + 1}", **dict(record), "guild_id": guild_id or record["guild_id"]}
        self.created.append(row)
        return dict(row)


class FakeGrantDB:
    def __init__(self):
        self.wallets = {}
        self.upsert_wallet_calls = []
        self.status_updates = []
        self.storage_handler = None
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.server_config = SimpleNamespace(
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 55},
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
        )

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallets.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-{len(self.upsert_wallet_calls) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "now",
            "metadata": metadata,
        }
        self.upsert_wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        self.wallets[key] = wallet
        return dict(wallet)

    def update_grant_status(self, thread_id, status, guild_id=None, **kwargs):
        self.status_updates.append((thread_id, status, guild_id, kwargs))
        return True


@pytest.fixture(autouse=True)
def admin_chat_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.mark.anyio
async def test_initiate_payment_wallet_on_file():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    flow_cog = FakeFlowCog()
    bot = FakeBot(channel, payment_service=object(), admin_chat_cog=flow_cog)

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1.5, "reason": "thanks", "admin_user_id": 999},
    )

    assert result["success"] is True
    assert result["wallet_on_file"] is True
    assert db.created_intents[0]["status"] == "awaiting_test"
    assert db.created_intents[0]["wallet_id"] == "wallet-verified"
    assert db.created_intents[0]["admin_user_id"] == 999
    assert len(flow_cog.started) == 1
    assert flow_cog.started[0][1]["producer_ref"].startswith("1_42_")


@pytest.mark.anyio
async def test_initiate_payment_no_wallet():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 2.0, "admin_user_id": 999},
    )

    assert result["success"] is True
    assert result["wallet_on_file"] is False
    assert db.created_intents[0]["status"] == "awaiting_wallet"
    assert db.created_intents[0]["admin_user_id"] == 999
    assert db.updated_intents[-1][1]["prompt_message_id"] == 900
    assert "<@42>" in channel.sent_messages[0].content


@pytest.mark.anyio
async def test_initiate_payment_validation():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    cases = [
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "nope", "amount_sol": 1},
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 0},
        {"source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1},
        {"guild_id": 1, "recipient_user_id": "42", "amount_sol": 1},
    ]

    for params in cases:
        result = await execute_initiate_payment(bot, db, params)
        assert result["success"] is False


@pytest.mark.anyio
async def test_initiate_payment_tool_rejects_wallet_address_arg():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "recipient_user_id": "42",
            "amount_sol": 1.0,
            "wallet_address": "Injected111111111111111111111111111111",
            "recipient_wallet": "Injected222222222222222222222222222222",
        },
    )

    assert result["success"] is True
    assert result["wallet_on_file"] is False
    assert db.created_intents[0]["status"] == "awaiting_wallet"
    assert db.created_intents[0]["wallet_id"] is None
    assert db.wallets == {}


@pytest.mark.anyio
async def test_initiate_payment_duplicate_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    existing = {
        "intent_id": "intent-existing",
        "guild_id": 1,
        "channel_id": 55,
        "recipient_user_id": 42,
        "status": "awaiting_wallet",
    }
    db.intents[existing["intent_id"]] = dict(existing)
    db.active_by_key[(1, 55, 42)] = db.intents[existing["intent_id"]]
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1},
    )

    assert result["success"] is True
    assert result["duplicate"] is True
    assert db.created_intents == []


@pytest.mark.anyio
async def test_wallet_reply_valid():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()
    message = FakeMessage(1001, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS)

    handled = await cog._check_pending_payment_reply(message)

    assert handled is True
    assert db.upsert_wallet_calls[0][3] == VALID_SOL_ADDRESS
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_test"
    assert db.intents[intent["intent_id"]]["resolved_by_message_id"] == 1001
    cog._start_admin_payment_flow.assert_awaited_once()


@pytest.mark.anyio
async def test_upsert_wallet_raises_during_active_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.active_payment_or_intent_users.add((1, 42))
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object(), db_handler=db)
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()

    await cog._handle_wallet_received(
        FakeMessage(1007, FakeAuthor(42), channel, guild, "Wallet22222222222222222222222222222222"),
        intent,
        "Wallet22222222222222222222222222222222",
    )

    assert "active payment is already in flight" in channel.sent_messages[-1].content
    assert db.intents[intent["intent_id"]]["status"] == "failed"
    cog._start_admin_payment_flow.assert_not_awaited()

    grant_thread = FakeChannel(channel_id=1001, guild=guild, parent_id=10)
    grant_db = FakeGrantDB()
    grant_db.wallets[(1, 222, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 222,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    grant_db.active_payment_or_intent_users.add((1, 222))
    payment_service = FakeAdminPaymentService()
    grant_bot = FakeBot(
        grant_thread,
        payment_service=payment_service,
        guilds=[guild],
        db_handler=grant_db,
    )
    grant_cog = GrantsCog(grant_bot)

    await grant_cog._start_payment_flow(
        grant_thread,
        {"applicant_id": 222, "thread_id": 1001, "total_cost_usd": 42.5},
        "Wallet33333333333333333333333333333333",
    )

    assert "active payment in flight" in grant_thread.sent_messages[-1].content
    assert payment_service.request_calls == []


@pytest.mark.anyio
async def test_upsert_wallet_unblocked_after_terminal():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.active_payment_or_intent_users.add((1, 42))
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object(), db_handler=db)
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()

    await cog._handle_wallet_received(
        FakeMessage(1008, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS),
        intent,
        VALID_SOL_ADDRESS,
    )

    assert db.intents[intent["intent_id"]]["status"] == "awaiting_test"
    cog._start_admin_payment_flow.assert_awaited_once()

    grant_thread = FakeChannel(channel_id=1001, guild=guild, parent_id=10)
    grant_db = FakeGrantDB()
    grant_db.wallets[(1, 222, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 222,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    payment_service = FakeAdminPaymentService(
        request_result={"payment_id": "pay-test", "status": "pending_confirmation"},
        confirm_result={"payment_id": "pay-test", "status": "queued"},
    )
    grant_bot = FakeBot(
        grant_thread,
        payment_service=payment_service,
        guilds=[guild],
        db_handler=grant_db,
    )
    grant_cog = GrantsCog(grant_bot)
    grant_cog._apply_tag = AsyncMock()

    await grant_cog._start_payment_flow(
        grant_thread,
        {"applicant_id": 222, "thread_id": 1001, "total_cost_usd": 42.5},
        "Wallet44444444444444444444444444444444",
    )

    assert grant_db.upsert_wallet_calls[0][3] == "Wallet44444444444444444444444444444444"
    assert payment_service.request_calls[0]["wallet_id"] == "wallet-existing"
    assert payment_service.confirm_calls == [
        ("pay-test", {"guild_id": 1, "confirmed_by": "auto", "confirmed_by_user_id": 222})
    ]
    assert grant_db.status_updates[-1][1] == "payment_requested"


@pytest.mark.anyio
async def test_wallet_reply_invalid_then_classified():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    cog._classifier_client = FakeClassifierClient('{"category":"ambiguous","extracted_address":null}')
    message = FakeMessage(1002, FakeAuthor(42), channel, guild, "not a wallet")

    handled = await cog._check_pending_payment_reply(message)

    assert handled is True
    assert db.intents[intent["intent_id"]]["status"] == "failed"
    assert "review needed" in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_wallet_reply_no_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    message = FakeMessage(1003, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS)

    handled = await cog._check_pending_payment_reply(message)

    assert handled is False
    assert db.updated_intents == []


@pytest.mark.anyio
async def test_handle_payment_result_test_confirmed():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    payment_cog = FakeAdminPaymentCog()
    bot = FakeBot(channel, payment_service=payment_service, payment_cog=payment_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog.handle_payment_result(
        {
            "payment_id": "payment-test",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": True,
            "status": "confirmed",
            "recipient_wallet": VALID_SOL_ADDRESS,
            "chain": "solana",
            "provider": "solana",
            "confirm_channel_id": 55,
            "confirm_thread_id": None,
            "notify_channel_id": 55,
            "notify_thread_id": None,
            "route_key": "admin_chat",
            "wallet_id": "wallet-1",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert payment_service.request_calls[0]["amount_token"] == 1.75
    assert payment_service.request_calls[0]["amount_usd"] is None if "amount_usd" in payment_service.request_calls[0] else True
    assert payment_cog.confirmation_requests == ["payment-final"]
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_confirmation"
    assert db.intents[intent["intent_id"]]["final_payment_id"] == "payment-final"
    assert db.intents[intent["intent_id"]]["prompt_message_id"] == 900
    admin_user = bot.fetched_users[999]
    assert "Test payment confirmed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_handle_payment_result_test_failed():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    bot = FakeBot(channel, payment_service=payment_service, payment_cog=FakeAdminPaymentCog())
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog.handle_payment_result(
        {
            "payment_id": "payment-test",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": True,
            "status": "failed",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert payment_service.request_calls == []
    assert db.intents[intent["intent_id"]]["status"] == "failed"
    assert "review needed" in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_confirmation_reply():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "awaiting_confirmation",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    cog._classifier_client = FakeClassifierClient('{"category":"positive_confirmation","extracted_address":null}')
    message = FakeMessage(1004, FakeAuthor(42), channel, guild, "yes, send it")

    handled = await cog._check_pending_payment_reply(message)

    assert handled is True
    assert payment_service.confirm_calls == [
        ("payment-final", {"guild_id": 1, "confirmed_by": "free_text", "confirmed_by_user_id": 42})
    ]
    assert db.intents[intent["intent_id"]]["status"] == "confirmed"


@pytest.mark.anyio
async def test_handle_payment_result_final_confirmed_notifies_admin():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "confirmed",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog.handle_payment_result(
        {
            "payment_id": "payment-final",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.75,
            "tx_signature": "sig-123",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert db.intents[intent["intent_id"]]["status"] == "completed"
    admin_user = bot.fetched_users[999]
    assert "Final payment confirmed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_concurrent_intents_same_channel():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent_one = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.0,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    intent_two = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 43,
            "requested_amount_sol": 2.0,
            "producer_ref": "1_43_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    matched = []

    async def record_wallet(message, intent, wallet_address):
        matched.append((message.author.id, intent["intent_id"], wallet_address))

    cog._handle_wallet_received = record_wallet

    await cog._check_pending_payment_reply(FakeMessage(1005, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS))
    await cog._check_pending_payment_reply(FakeMessage(1006, FakeAuthor(43), channel, guild, VALID_SOL_ADDRESS))

    assert matched == [
        (42, intent_one["intent_id"], VALID_SOL_ADDRESS),
        (43, intent_two["intent_id"], VALID_SOL_ADDRESS),
    ]


@pytest.mark.anyio
async def test_request_payment_amount_token():
    provider = FakeProvider()
    db = FakePaymentRequestDB()
    service = PaymentService(db_handler=db, providers={"solana": provider}, test_payment_amount=0.01)

    result = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-1",
        guild_id=1,
        recipient_wallet=VALID_SOL_ADDRESS,
        chain="solana",
        provider="solana",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=55,
        notify_channel_id=55,
        recipient_discord_id=42,
        wallet_id="wallet-1",
        metadata={"intent_id": "intent-1"},
    )

    assert result is not None
    assert result["amount_token"] == 1.5
    assert result["amount_usd"] is None
    assert result["token_price_usd"] is None
    assert provider.price_calls == 0


@pytest.mark.anyio
async def test_admin_success_dm_over_threshold(monkeypatch, clear_payment_policy_env):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setenv("ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD", "100")
    monkeypatch.setenv("ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS", "solana_payouts")
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = PaymentCog(bot, FakePaymentRequestDB(), payment_service=payment_service)
    cog._notify_payment_result = AsyncMock()
    cog._handoff_terminal_result = AsyncMock()
    cog._dm_admin_payment_success = AsyncMock()
    cog._dm_admin_payment_failure = AsyncMock()

    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-success",
            "producer": "admin_chat",
            "producer_ref": "intent-1",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.0,
            "amount_usd": 150.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
        }
    )
    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-under",
            "producer": "admin_chat",
            "producer_ref": "intent-2",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.0,
            "amount_usd": 50.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
        }
    )
    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-failure",
            "producer": "admin_chat",
            "producer_ref": "intent-3",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "failed",
            "amount_token": 1.0,
            "amount_usd": 150.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
            "last_error": "manual review",
        }
    )

    assert cog._dm_admin_payment_success.await_count == 1
    assert cog._dm_admin_payment_failure.await_count == 1


@pytest.mark.anyio
async def test_admin_success_dm_sees_derived_usd(monkeypatch, clear_payment_policy_env):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = PaymentCog(bot, FakePaymentRequestDB(), payment_service=payment_service)

    await cog._dm_admin_payment_success(
        {
            "payment_id": "pay-success",
            "producer": "admin_chat",
            "producer_ref": "intent-1",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.5,
            "amount_usd": 321.09,
            "recipient_wallet": VALID_SOL_ADDRESS,
            "tx_signature": "sig-123",
        }
    )

    message = bot.fetched_users[999].sent_messages[-1].content
    assert "✅ **Payment Completed**" in message
    assert "- USD: $321.09" in message
    assert "- Wallet: `1111...1111`" in message
    assert "requires manual review" not in message


@pytest.mark.anyio
async def test_startup_reconciliation():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(channel_id=55, guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "prompt_message_id": 1000,
            "status": "awaiting_confirmation",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    cog._classifier_client = FakeClassifierClient('{"category":"positive_confirmation","extracted_address":null}')
    message = FakeMessage(1001, FakeAuthor(42), channel, guild, "yes")
    channel._messages = [message]

    await cog.cog_load()
    assert bot.ready_waits == 0
    await cog.on_ready()

    assert db.intents[intent["intent_id"]]["status"] == "confirmed"
    assert db.intents[intent["intent_id"]]["last_scanned_message_id"] == 1001
    assert payment_service.confirm_calls == [
        ("payment-final", {"guild_id": 1, "confirmed_by": "free_text", "confirmed_by_user_id": 42})
    ]


@pytest.mark.anyio
async def test_admin_chat_cog_load_does_not_block_on_wait_until_ready():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(channel_id=55, guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog.cog_load()

    assert bot.ready_waits == 0
