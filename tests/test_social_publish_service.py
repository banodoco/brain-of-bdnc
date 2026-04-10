from datetime import datetime, timezone

import pytest

from src.common.db_handler import WalletUpdateBlockedError
from src.features.payments.payment_service import PaymentActor, PaymentActorKind
from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest
from src.features.sharing.social_publish_service import SocialPublishService
from src.features.payments.payment_service import PaymentService
from src.features.payments.provider import SendResult


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def social_signing_secret(monkeypatch):
    monkeypatch.setenv("SOCIAL_PUBLISH_SIGNING_SECRET", "test-social-signing-secret")


class FakeProvider:
    def __init__(self):
        self.delete_calls = []
        self.publish_calls = []

    async def publish(self, request):
        self.publish_calls.append(request)
        if request.action == "retweet":
            return {
                "provider_ref": request.target_post_ref,
                "provider_url": "https://x.com/example/status/{0}".format(request.target_post_ref),
                "tweet_id": request.target_post_ref,
                "tweet_url": "https://x.com/example/status/{0}".format(request.target_post_ref),
                "delete_supported": False,
            }
        return {
            "provider_ref": "tweet-123",
            "provider_url": "https://x.com/example/status/tweet-123",
            "tweet_id": "tweet-123",
            "tweet_url": "https://x.com/example/status/tweet-123",
            "delete_supported": True,
        }

    async def delete(self, publication):
        self.delete_calls.append(publication["publication_id"])
        return publication.get("delete_supported", False)

    def normalize_target_ref(self, target_ref):
        return target_ref


class FailingProvider(FakeProvider):
    async def publish(self, request):
        self.publish_calls.append(request)
        raise RuntimeError("provider boom")


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.shared_posts = []
        self.deleted_shared_posts = []
        self.supabase = None
        self.server_config = None

    def create_social_publication(self, data, guild_id=None):
        publication_id = "pub-{0}".format(len(self.rows) + 1)
        row = dict(data)
        row["publication_id"] = publication_id
        row["guild_id"] = guild_id or row.get("guild_id")
        self.rows[publication_id] = row
        return row

    def get_social_publication_by_id(self, publication_id, guild_id=None):
        row = self.rows.get(publication_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def mark_social_publication_processing(self, publication_id, guild_id=None, attempt_count=None, retry_after=None):
        row = self.rows[publication_id]
        row["status"] = "processing"
        if attempt_count is not None:
            row["attempt_count"] = attempt_count
        if retry_after is not None:
            row["retry_after"] = retry_after
        return True

    def mark_social_publication_succeeded(self, publication_id, guild_id=None, provider_ref=None, provider_url=None, delete_supported=None):
        row = self.rows[publication_id]
        row["status"] = "succeeded"
        row["provider_ref"] = provider_ref
        row["provider_url"] = provider_url
        row["delete_supported"] = delete_supported
        return True

    def mark_social_publication_failed(self, publication_id, last_error, guild_id=None, retry_after=None):
        row = self.rows[publication_id]
        row["status"] = "failed"
        row["last_error"] = last_error
        row["retry_after"] = retry_after
        return True

    def mark_social_publication_cancelled(self, publication_id, guild_id=None, last_error=None):
        row = self.rows[publication_id]
        row["status"] = "cancelled"
        row["last_error"] = last_error
        return True

    def record_shared_post(self, **kwargs):
        self.shared_posts.append(kwargs)
        return True

    def mark_shared_post_deleted(self, discord_message_id, platform, guild_id=None):
        self.deleted_shared_posts.append((discord_message_id, platform, guild_id))
        return True


def build_request(action="post", scheduled_at=None, target_post_ref=None):
    return SocialPublishRequest(
        message_id=1,
        channel_id=2,
        guild_id=3,
        user_id=4,
        platform="twitter",
        action=action,
        scheduled_at=scheduled_at,
        target_post_ref=target_post_ref,
        text="hello world" if action != "retweet" else None,
        source_kind="admin_chat",
        source_context=PublicationSourceContext(
            source_kind="admin_chat",
            metadata={"user_details": {"username": "poster"}, "original_content": "hello"},
        ),
    )


class FakeServerConfig:
    def __init__(self, enabled=True, route=None):
        self.enabled = enabled
        self.route = route
        self.resolve_calls = []

    def is_feature_enabled(self, guild_id, channel_id, feature):
        assert feature == "sharing"
        return self.enabled

    def resolve_social_route(self, guild_id, channel_id, platform):
        self.resolve_calls.append((guild_id, channel_id, platform))
        if not self.enabled:
            return None
        return self.route


async def test_publish_now_enqueue_execute_and_delete_branching():
    db_handler = FakeDB()
    provider = FakeProvider()
    service = SocialPublishService(db_handler, providers={"twitter": provider}, logger_instance=None)

    post_result = await service.publish_now(build_request())
    assert post_result.success is True
    assert post_result.publication_id == "pub-1"
    assert post_result.delete_supported is True
    assert len(db_handler.shared_posts) == 1

    reply_result = await service.publish_now(build_request(action="reply", target_post_ref="12345"))
    assert reply_result.success is True
    assert reply_result.delete_supported is True
    assert len(db_handler.shared_posts) == 1

    retweet_result = await service.publish_now(build_request(action="retweet", target_post_ref="777"))
    assert retweet_result.success is True
    assert retweet_result.delete_supported is False
    assert len(db_handler.shared_posts) == 1

    scheduled_at = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    queued_result = await service.enqueue(build_request(scheduled_at=scheduled_at))
    assert queued_result.success is True
    assert db_handler.rows[queued_result.publication_id]["status"] == "queued"
    assert db_handler.rows[queued_result.publication_id]["scheduled_at"] == scheduled_at

    db_handler.rows["pub-exec"] = {
        "publication_id": "pub-exec",
        "guild_id": 3,
        "message_id": 99,
        "channel_id": 2,
        "user_id": 4,
        "platform": "twitter",
        "action": "post",
        "source_kind": "admin_chat",
        "request_payload": {
            "message_id": 99,
            "channel_id": 2,
            "guild_id": 3,
            "user_id": 4,
            "platform": "twitter",
            "action": "post",
            "text": "scheduled",
            "source_kind": "admin_chat",
            "source_context": {"source_kind": "admin_chat", "metadata": {"user_details": {"username": "poster"}}},
        },
        "attempt_count": 1,
        "status": "queued",
    }
    db_handler.rows["pub-exec"]["integrity_version"] = service.SIGNATURE_VERSION
    db_handler.rows["pub-exec"]["integrity_signature"] = service._sign_publication_payload(db_handler.rows["pub-exec"])
    execute_result = await service.execute_publication("pub-exec")
    assert execute_result.success is True
    assert db_handler.rows["pub-exec"]["status"] == "succeeded"

    assert await service.delete_publication(post_result.publication_id) is True
    assert provider.delete_calls == ["pub-1"]
    assert db_handler.rows["pub-1"]["status"] == "cancelled"
    assert db_handler.deleted_shared_posts == [(1, "twitter", 3)]

    assert await service.delete_publication(retweet_result.publication_id) is False
    assert provider.delete_calls == ["pub-1"]


async def test_publish_and_enqueue_resolve_and_persist_route_selection():
    db_handler = FakeDB()
    db_handler.server_config = FakeServerConfig(
        route={
            "id": "route-default",
            "channel_id": None,
            "platform": "twitter",
            "route_config": {"account": "main"},
        }
    )
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    immediate_result = await service.publish_now(build_request())
    assert immediate_result.success is True
    assert db_handler.rows[immediate_result.publication_id]["route_key"] == "route-default"
    assert db_handler.rows[immediate_result.publication_id]["request_payload"]["route_override"] == {
        "id": "route-default",
        "channel_id": None,
        "platform": "twitter",
        "route_config": {"account": "main"},
        "route_key": "route-default",
    }

    scheduled_at = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    queued_request = build_request(scheduled_at=scheduled_at)
    queued_request.route_override = "manual-route"
    queued_result = await service.enqueue(queued_request)
    assert queued_result.success is True
    assert db_handler.rows[queued_result.publication_id]["route_key"] == "manual-route"
    assert db_handler.rows[queued_result.publication_id]["request_payload"]["route_override"] == {
        "route_key": "manual-route"
    }
    assert db_handler.server_config.resolve_calls == [(3, 2, "twitter")]


async def test_publish_now_rejects_disabled_or_unrouted_channels():
    disabled_db_handler = FakeDB()
    disabled_db_handler.server_config = FakeServerConfig(enabled=False)
    disabled_service = SocialPublishService(
        disabled_db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    disabled_result = await disabled_service.publish_now(build_request())
    assert disabled_result.success is False
    assert disabled_result.error == "Sharing is not enabled for this channel."
    assert disabled_db_handler.rows == {}

    unrouted_db_handler = FakeDB()
    unrouted_db_handler.server_config = FakeServerConfig(enabled=True, route=None)
    unrouted_service = SocialPublishService(
        unrouted_db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    unrouted_result = await unrouted_service.enqueue(build_request())
    assert unrouted_result.success is False
    assert unrouted_result.error == "No social route is configured for this channel and platform."
    assert unrouted_db_handler.rows == {}


async def test_publish_failures_mark_canonical_rows_failed():
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FailingProvider()},
        logger_instance=None,
    )

    result = await service.publish_now(build_request())
    assert result.success is False
    assert result.publication_id == "pub-1"
    assert db_handler.rows["pub-1"]["status"] == "failed"
    assert db_handler.rows["pub-1"]["last_error"] == "provider boom"


async def test_execute_publication_rejects_tampered_row():
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    publication = service._build_publication_record(
        build_request(scheduled_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)),
        status="queued",
    )
    publication["publication_id"] = "pub-bad"
    publication["request_payload"]["text"] = "tampered after signing"
    db_handler.rows["pub-bad"] = publication

    result = await service.execute_publication("pub-bad")

    assert result.success is False
    assert result.error == "Invalid publication signature"
    assert db_handler.rows["pub-bad"]["status"] == "failed"
    assert db_handler.rows["pub-bad"]["last_error"] == "Invalid publication signature"


class FakePaymentProvider:
    def __init__(
        self,
        send_result=None,
        confirm_result="confirmed",
        check_status_result="confirmed",
        price=150.0,
        price_error=None,
    ):
        self.send_result = send_result or SendResult(signature="sig-1", phase="submitted", error=None)
        self.confirm_result = confirm_result
        self.check_status_result = check_status_result
        self.price = price
        self.price_error = price_error
        self.send_calls = []
        self.confirm_calls = []
        self.status_calls = []
        self.price_calls = 0

    async def send(self, recipient, amount_token):
        self.send_calls.append((recipient, amount_token))
        return self.send_result

    async def confirm_tx(self, tx_signature):
        self.confirm_calls.append(tx_signature)
        return self.confirm_result

    async def check_status(self, tx_signature):
        self.status_calls.append(tx_signature)
        return self.check_status_result

    async def get_token_price_usd(self):
        self.price_calls += 1
        if self.price_error is not None:
            raise self.price_error
        return self.price

    def token_name(self):
        return "SOL"


class FakePaymentDB:
    def __init__(self):
        self.rows = {}
        self.wallets = {}
        self.wallet_registry = {}
        self.transitions = []
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.rolling_24h_calls = []

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test=None):
        rows = [
            row for row in self.rows.values()
            if row.get("guild_id") == guild_id
            and row.get("producer") == producer
            and row.get("producer_ref") == producer_ref
            and (is_test is None or row.get("is_test") == is_test)
        ]
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def create_payment_request(self, record, guild_id=None):
        payment_id = "pay-{0}".format(len(self.rows) + 1)
        row = dict(record)
        row["payment_id"] = payment_id
        row["guild_id"] = guild_id or row.get("guild_id")
        self.rows[payment_id] = row
        return row

    def get_payment_request(self, payment_id, guild_id=None):
        row = self.rows.get(payment_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def mark_payment_confirmed_by_user(self, payment_id, guild_id=None, confirmed_by_user_id=None, confirmed_by="user", scheduled_at=None):
        row = self.rows[payment_id]
        row.update(
            {
                "status": "queued",
                "confirmed_by": confirmed_by,
                "confirmed_by_user_id": confirmed_by_user_id,
                "scheduled_at": scheduled_at,
            }
        )
        self.transitions.append(("confirmed_by_user", payment_id))
        return True

    def mark_payment_submitted(self, payment_id, tx_signature, amount_token=None, token_price_usd=None, send_phase="submitted", guild_id=None):
        row = self.rows[payment_id]
        row.update(
            {
                "status": "submitted",
                "tx_signature": tx_signature,
                "amount_token": amount_token,
                "token_price_usd": token_price_usd,
                "send_phase": send_phase,
            }
        )
        self.transitions.append(("submitted", payment_id, tx_signature))
        return True

    def mark_payment_confirmed(self, payment_id, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "confirmed"
        self.transitions.append(("confirmed", payment_id))
        return True

    def mark_payment_failed(self, payment_id, error, send_phase=None, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "failed"
        row["last_error"] = error
        row["send_phase"] = send_phase
        self.transitions.append(("failed", payment_id, send_phase))
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "manual_hold"
        row["last_error"] = reason
        self.transitions.append(("manual_hold", payment_id, reason))
        return True

    def requeue_payment(self, payment_id, retry_after=None, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "queued"
        row["retry_after"] = retry_after
        self.transitions.append(("requeue", payment_id))
        return True

    def get_inflight_payments_for_recovery(self, guild_ids=None):
        rows = [
            row for row in self.rows.values()
            if row.get("status") in {"processing", "submitted"}
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]
        return list(rows)

    def get_pending_confirmation_payments(self, guild_ids=None):
        return [
            row for row in self.rows.values()
            if row.get("status") == "pending_confirmation"
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        wallet = self.wallets.get(wallet_id)
        if wallet and guild_id is not None and wallet.get("guild_id") != guild_id:
            return None
        return wallet

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        key = (int(guild_id), str(provider).strip().lower())
        self.rolling_24h_calls.append(key)
        return float(self.rolling_24h_usd.get(key, 0.0))

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
            "wallet_id": existing["wallet_id"] if existing else "wallet-user-{0}".format(len(self.wallet_registry) + 1),
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "verified",
            "metadata": metadata,
        }
        self.wallet_registry[key] = wallet
        self.wallets[wallet["wallet_id"]] = wallet
        return wallet

    def mark_wallet_verified(self, wallet_id, guild_id=None):
        wallet = self.wallets[wallet_id]
        wallet["verified_at"] = "verified"
        self.transitions.append(("wallet_verified", wallet_id))
        return True


async def test_payment_service_request_is_idempotent_and_test_amount_is_fixed():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    provider = FakePaymentProvider(price=200.0)
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    created = await service.request_payment(
        producer="grants",
        producer_ref="thread-1",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_native",
        is_test=True,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )
    assert created["status"] == "pending_confirmation"
    assert created["amount_token"] == 0.000001
    assert created["amount_usd"] is None
    assert created["token_price_usd"] is None

    duplicate = await service.request_payment(
        producer="grants",
        producer_ref="thread-1",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_native",
        is_test=True,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
    )
    assert duplicate["payment_id"] == created["payment_id"]
    assert len(db_handler.rows) == 1

    confirmed = service.confirm_payment(
        created["payment_id"],
        actor=PaymentActor(PaymentActorKind.AUTO, 99),
    )
    assert confirmed["status"] == "queued"
    assert confirmed["confirmed_by"] == "auto"


async def test_payment_service_confirm_payment_requires_expected_recipient():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-secure"] = {
        "payment_id": "pay-secure",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-secure",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-secure",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 999),
    )
    accepted = service.confirm_payment(
        "pay-secure",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert rejected is None
    assert accepted["status"] == "queued"


async def test_confirm_rejects_null_recipient_discord_id():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-null"] = {
        "payment_id": "pay-null",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-null",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": None,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-null",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert rejected is None
    assert db_handler.rows["pay-null"]["status"] == "pending_confirmation"


async def test_confirm_rejects_auto_actor_for_non_test_payment():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-auto"] = {
        "payment_id": "pay-auto",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-auto",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-auto",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.AUTO, 123),
    )

    assert rejected is None
    assert db_handler.rows["pay-auto"]["status"] == "pending_confirmation"


async def test_confirm_rejects_mismatched_user():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-mismatch"] = {
        "payment_id": "pay-mismatch",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-mismatch",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-mismatch",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 999),
    )

    assert rejected is None
    assert db_handler.rows["pay-mismatch"]["status"] == "pending_confirmation"


async def test_request_payment_per_payment_cap_manual_holds():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    provider = FakePaymentProvider(price=200.0)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-cap-usd",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=600.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "per-payment cap exceeded: $600.00 > $500.00"
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_path_cap_breach():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-cap-token",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=4.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "per-payment cap exceeded: $600.00 > $500.00"
    assert created["amount_usd"] == 600.0
    assert created["token_price_usd"] == 150.0
    assert created["request_payload"]["amount_usd"] == 600.0
    assert created["request_payload"]["token_price_usd"] == 150.0
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_path_stamps_amount_usd():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-stamp",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "pending_confirmation"
    assert created["amount_usd"] == 225.0
    assert created["token_price_usd"] == 150.0
    assert created["request_payload"]["amount_usd"] == 225.0
    assert created["request_payload"]["token_price_usd"] == 150.0
    assert provider.price_calls == 1


async def test_request_payment_amount_token_path_missing_price_holds():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=None)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-missing-price",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "cap check unavailable: token price missing"
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_uncapped_provider_preserves_none():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-uncapped",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["amount_usd"] is None
    assert created["token_price_usd"] is None
    assert provider.price_calls == 0


async def test_request_payment_rolling_daily_cap_manual_holds():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    db_handler.rolling_24h_usd[(3, "solana_payouts")] = 1900.0
    provider = FakePaymentProvider(price=200.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-daily-usd",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "rolling daily cap exceeded: $2050.00 > $2000.00"


async def test_request_payment_rolling_daily_cap_sees_derived_usd():
    db_handler = FakePaymentDB()
    db_handler.rolling_24h_usd[(3, "solana_payouts")] = 1900.0
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-daily-token",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "rolling daily cap exceeded: $2050.00 > $2000.00"
    assert created["amount_usd"] == 150.0
    assert created["token_price_usd"] == 150.0


async def test_slot_reuse_collision_detected_after_failure():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-old"] = {
        "payment_id": "pay-old",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-collision",
        "recipient_wallet": "old-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "failed",
    }
    notifications = []

    async def on_cap_breach(payment):
        notifications.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-collision",
        guild_id=3,
        recipient_wallet="new-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "idempotency collision: prior wallet differs"
    assert len(db_handler.rows) == 2
    assert notifications == [created["payment_id"]]


async def test_slot_reuse_collision_blocked_when_prior_active():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-active"] = {
        "payment_id": "pay-active",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-active-collision",
        "recipient_wallet": "old-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    notifications = []

    async def on_cap_breach(payment):
        notifications.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-active-collision",
        guild_id=3,
        recipient_wallet="new-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created is None
    assert len(db_handler.rows) == 1
    assert notifications == ["pay-active"]


async def test_slot_reuse_same_wallet_creates_fresh_row_after_failure():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-old"] = {
        "payment_id": "pay-old",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-same-wallet",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "failed",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-same-wallet",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["payment_id"] != "pay-old"
    assert created["status"] == "pending_confirmation"
    assert len(db_handler.rows) == 2


async def test_idempotent_return_for_nonterminal():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-existing"] = {
        "payment_id": "pay-existing",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-existing",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    duplicate = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-existing",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert duplicate["payment_id"] == "pay-existing"
    assert len(db_handler.rows) == 1


async def test_payment_service_execute_persists_submission_and_confirms_test_wallet():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
        "verified_at": None,
    }
    provider = FakePaymentProvider(
        send_result=SendResult(signature="sig-123", phase="submitted", error=None),
        confirm_result="confirmed",
    )
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
    )
    db_handler.rows["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-1",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "wallet_id": "wallet-1",
        "is_test": True,
        "amount_token": 0.000001,
        "token_price_usd": None,
        "status": "processing",
    }

    result = await service.execute_payment("pay-1")

    assert result["status"] == "confirmed"
    assert db_handler.rows["pay-1"]["tx_signature"] == "sig-123"
    assert provider.confirm_calls == ["sig-123"]
    assert db_handler.transitions[:2] == [("submitted", "pay-1", "sig-123"), ("confirmed", "pay-1")]
    assert ("wallet_verified", "wallet-1") in db_handler.transitions


async def test_execute_payment_uses_stored_wallet():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "registry-wallet",
        "verified_at": "verified",
    }
    provider = FakePaymentProvider(
        send_result=SendResult(signature="sig-frozen", phase="submitted", error=None),
        confirm_result="confirmed",
    )
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
    )
    db_handler.rows["pay-frozen"] = {
        "payment_id": "pay-frozen",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-frozen",
        "recipient_wallet": "frozen-wallet",
        "provider": "solana_native",
        "wallet_id": "wallet-1",
        "is_test": False,
        "amount_token": 0.75,
        "token_price_usd": 100.0,
        "status": "processing",
    }

    result = await service.execute_payment("pay-frozen")

    assert result["status"] == "confirmed"
    assert provider.send_calls == [("frozen-wallet", 0.75)]


async def test_payment_service_execute_fail_closed_on_ambiguous_and_timeout():
    ambiguous_db = FakePaymentDB()
    ambiguous_db.rows["pay-ambiguous"] = {
        "payment_id": "pay-ambiguous",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-2",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.25,
        "token_price_usd": 100.0,
        "status": "processing",
    }
    ambiguous_service = PaymentService(
        ambiguous_db,
        providers={
            "solana_native": FakePaymentProvider(
                send_result=SendResult(signature=None, phase="ambiguous", error="rpc timeout")
            )
        },
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    ambiguous_result = await ambiguous_service.execute_payment("pay-ambiguous")
    assert ambiguous_result["status"] == "manual_hold"
    assert "Ambiguous send error" in ambiguous_result["last_error"]

    timeout_db = FakePaymentDB()
    timeout_db.rows["pay-timeout"] = {
        "payment_id": "pay-timeout",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-3",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 2.0,
        "token_price_usd": 100.0,
        "status": "processing",
    }
    timeout_service = PaymentService(
        timeout_db,
        providers={
            "solana_native": FakePaymentProvider(
                send_result=SendResult(signature="sig-timeout", phase="submitted", error=None),
                confirm_result="timeout",
            )
        },
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    timeout_result = await timeout_service.execute_payment("pay-timeout")
    assert timeout_result["status"] == "manual_hold"
    assert timeout_db.rows["pay-timeout"]["tx_signature"] == "sig-timeout"
    assert timeout_result["last_error"] == "Confirmation timed out after submission"


async def test_payment_service_recover_inflight_requeues_or_holds_safely():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
        "verified_at": None,
    }
    db_handler.rows["pay-processing"] = {
        "payment_id": "pay-processing",
        "guild_id": 3,
        "status": "processing",
        "provider": "solana_native",
        "is_test": False,
    }
    db_handler.rows["pay-submitted"] = {
        "payment_id": "pay-submitted",
        "guild_id": 3,
        "status": "submitted",
        "provider": "solana_native",
        "tx_signature": "sig-recovery",
        "wallet_id": "wallet-1",
        "is_test": True,
    }
    db_handler.rows["pay-unknown"] = {
        "payment_id": "pay-unknown",
        "guild_id": 3,
        "status": "submitted",
        "provider": "unknown",
        "tx_signature": "sig-unknown",
        "is_test": False,
    }
    provider = FakePaymentProvider(check_status_result="confirmed")
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.000001,
        logger_instance=None,
    )

    recovered = await service.recover_inflight(guild_ids=[3])

    by_id = {row["payment_id"]: row for row in recovered}
    assert by_id["pay-processing"]["status"] == "queued"
    assert by_id["pay-submitted"]["status"] == "confirmed"
    assert by_id["pay-unknown"]["status"] == "manual_hold"
    assert provider.status_calls == ["sig-recovery"]
