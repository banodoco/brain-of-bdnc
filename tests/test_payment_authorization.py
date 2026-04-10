import logging

import src.features.payments.producer_flows as producer_flows
from src.features.payments.payment_service import PaymentActor, PaymentActorKind, PaymentService


class FakePaymentDB:
    def __init__(self):
        self.rows = {}

    def get_payment_request(self, payment_id, guild_id=None):
        row = self.rows.get(payment_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def mark_payment_confirmed_by_user(
        self,
        payment_id,
        guild_id=None,
        confirmed_by_user_id=None,
        confirmed_by="user",
        scheduled_at=None,
    ):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if row is None or row.get("status") != "pending_confirmation":
            return False
        row.update(
            {
                "status": "queued",
                "confirmed_by": confirmed_by,
                "confirmed_by_user_id": confirmed_by_user_id,
                "scheduled_at": scheduled_at,
            }
        )
        return True


def make_payment(
    payment_id,
    *,
    producer="grants",
    guild_id=1,
    recipient_discord_id=123,
    is_test=False,
    status="pending_confirmation",
):
    return {
        "payment_id": payment_id,
        "guild_id": guild_id,
        "producer": producer,
        "producer_ref": f"{producer}-{payment_id}",
        "recipient_wallet": "Wallet11111111111111111111111111111111",
        "recipient_discord_id": recipient_discord_id,
        "provider": "solana_native",
        "chain": "solana",
        "amount_token": 1.0,
        "is_test": is_test,
        "status": status,
    }


def make_service(db):
    return PaymentService(
        db_handler=db,
        providers={},
        test_payment_amount=0.001,
        logger_instance=logging.getLogger("test.payment.authorization"),
    )


def test_recipient_click_authorization_paths():
    db = FakePaymentDB()
    db.rows["pay-click"] = make_payment("pay-click", producer="grants", recipient_discord_id=123)
    service = make_service(db)

    rejected = service.confirm_payment(
        "pay-click",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 999),
    )
    accepted = service.confirm_payment(
        "pay-click",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert rejected is None
    assert accepted["status"] == "queued"
    assert accepted["confirmed_by"] == "recipient_click"
    assert accepted["confirmed_by_user_id"] == 123


def test_recipient_message_set_membership_for_grants_and_admin_chat():
    db = FakePaymentDB()
    db.rows["pay-grants-message"] = make_payment("pay-grants-message", producer="grants", recipient_discord_id=123)
    db.rows["pay-admin-message"] = make_payment("pay-admin-message", producer="admin_chat", recipient_discord_id=123)
    db.rows["pay-admin-click"] = make_payment("pay-admin-click", producer="admin_chat", recipient_discord_id=123)
    service = make_service(db)

    grants_rejected = service.confirm_payment(
        "pay-grants-message",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_MESSAGE, 123),
    )
    admin_message_confirmed = service.confirm_payment(
        "pay-admin-message",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_MESSAGE, 123),
    )
    admin_click_confirmed = service.confirm_payment(
        "pay-admin-click",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert grants_rejected is None
    assert admin_message_confirmed["status"] == "queued"
    assert admin_message_confirmed["confirmed_by"] == "recipient_message"
    assert admin_click_confirmed["status"] == "queued"
    assert admin_click_confirmed["confirmed_by"] == "recipient_click"


def test_null_recipient_and_auto_paths():
    db = FakePaymentDB()
    db.rows["pay-null"] = make_payment("pay-null", producer="grants", recipient_discord_id=None)
    db.rows["pay-auto-real"] = make_payment("pay-auto-real", producer="grants", recipient_discord_id=123, is_test=False)
    db.rows["pay-auto-test"] = make_payment("pay-auto-test", producer="grants", recipient_discord_id=123, is_test=True)
    service = make_service(db)

    null_recipient_rejected = service.confirm_payment(
        "pay-null",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )
    auto_real_rejected = service.confirm_payment(
        "pay-auto-real",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.AUTO, 123),
    )
    auto_test_confirmed = service.confirm_payment(
        "pay-auto-test",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.AUTO, 123),
    )

    assert null_recipient_rejected is None
    assert auto_real_rejected is None
    assert auto_test_confirmed["status"] == "queued"
    assert auto_test_confirmed["confirmed_by"] == "auto"


def test_admin_dm_and_unknown_producer(monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setitem(
        producer_flows.PRODUCER_FLOWS,
        "admin_dm_test",
        producer_flows.ProducerFlow(
            test_confirmed_by=frozenset({PaymentActorKind.ADMIN_DM}),
            real_confirmed_by=frozenset({PaymentActorKind.ADMIN_DM}),
        ),
    )

    db = FakePaymentDB()
    db.rows["pay-admin-wrong"] = make_payment("pay-admin-wrong", producer="admin_dm_test", recipient_discord_id=123)
    db.rows["pay-admin-right"] = make_payment("pay-admin-right", producer="admin_dm_test", recipient_discord_id=123)
    db.rows["pay-unknown"] = make_payment("pay-unknown", producer="unknown_producer", recipient_discord_id=123)
    service = make_service(db)

    wrong_admin = service.confirm_payment(
        "pay-admin-wrong",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.ADMIN_DM, 111),
    )
    right_admin = service.confirm_payment(
        "pay-admin-right",
        guild_id=1,
        actor=PaymentActor(PaymentActorKind.ADMIN_DM, 999),
    )
    with caplog.at_level(logging.WARNING):
        unknown = service.confirm_payment(
            "pay-unknown",
            guild_id=1,
            actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
        )

    assert wrong_admin is None
    assert right_admin["status"] == "queued"
    assert right_admin["confirmed_by"] == "admin_dm"
    assert unknown is None
    assert "unknown producer for confirmation authorization" in caplog.text
