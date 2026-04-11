from datetime import datetime, timedelta, timezone
import logging

import pytest

from src.common.db_handler import DatabaseHandler
from src.common.redaction import redact_wallet
from src.features.payments.payment_service import PaymentService, ReconcileDecision


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakePaymentRequestQuery:
    def __init__(self, rows, mode, payload=None):
        self.rows = rows
        self.mode = mode
        self.payload = payload or {}
        self.filters = []
        self.membership_filters = []
        self.limit_count = None

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.membership_filters.append((key, set(values)))
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        matched = [
            row for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
            and all(row.get(key) in values for key, values in self.membership_filters)
        ]
        if self.limit_count is not None:
            matched = matched[:self.limit_count]
        if self.mode == "select":
            return FakeResult([dict(row) for row in matched])
        for row in matched:
            row.update(dict(self.payload))
        return FakeResult([dict(row) for row in matched])


class FakePaymentRequestTable:
    def __init__(self, store):
        self.store = store

    def select(self, *_args):
        return FakePaymentRequestQuery(list(self.store.values()), "select")

    def update(self, payload):
        return FakePaymentRequestQuery(list(self.store.values()), "update", payload=payload)


class FakeSupabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        assert name == "payment_requests"
        return FakePaymentRequestTable(self.store)


def build_handler(store):
    handler = DatabaseHandler.__new__(DatabaseHandler)
    handler.supabase = FakeSupabase(store)
    handler._gate_check = lambda guild_id: True
    handler._resolve_payment_request_guild_id = lambda payment_id: store[payment_id]["guild_id"]
    handler._serialize_supabase_value = lambda value: value
    return handler


def make_payment(payment_id, status):
    return {
        "payment_id": payment_id,
        "guild_id": 1,
        "status": status,
        "tx_signature": "old-sig",
        "tx_signature_history": [],
        "send_phase": "submitted",
        "submitted_at": datetime.now(timezone.utc),
        "completed_at": None,
        "retry_after": None,
        "last_error": "old error",
    }


@pytest.mark.parametrize("starting_status", ["submitted", "processing", "failed", "manual_hold"])
@pytest.mark.parametrize(
    ("method_name", "target_status", "history_reason", "expected_last_error"),
    [
        ("force_reconcile_payment_to_confirmed", "confirmed", "reconcile_confirmed", None),
        (
            "force_reconcile_payment_to_failed",
            "failed",
            "reconcile_failed",
            "chain said failed",
        ),
    ],
)
def test_force_reconcile_allows_authoritative_chain_corrections(
    caplog,
    starting_status,
    method_name,
    target_status,
    history_reason,
    expected_last_error,
):
    store = {"pay-1": make_payment("pay-1", starting_status)}
    handler = build_handler(store)

    caplog.set_level(logging.WARNING)
    method = getattr(handler, method_name)

    assert method(
        "pay-1",
        tx_signature="sig-new",
        reason="chain said failed",
        guild_id=1,
    ) is True

    row = store["pay-1"]
    history_entry = row["tx_signature_history"][-1]
    assert row["status"] == target_status
    assert row["tx_signature"] == "sig-new"
    assert row["last_error"] == expected_last_error
    assert history_entry["signature"] == "sig-new"
    assert history_entry["status"] == target_status
    assert history_entry["reason"] == history_reason
    assert history_entry["detail"] == "chain said failed"
    assert history_entry["send_phase"] == "submitted"
    assert isinstance(history_entry["timestamp"], datetime)
    assert "Force-reconciled payment pay-1 in guild 1" in caplog.text


@pytest.mark.parametrize("starting_status", ["pending_confirmation", "queued", "cancelled"])
@pytest.mark.parametrize(
    "method_name",
    ["force_reconcile_payment_to_confirmed", "force_reconcile_payment_to_failed"],
)
def test_force_reconcile_rejects_rows_that_never_reached_chain(starting_status, method_name):
    store = {"pay-1": make_payment("pay-1", starting_status)}
    handler = build_handler(store)

    method = getattr(handler, method_name)
    assert method(
        "pay-1",
        tx_signature="sig-new",
        reason="chain truth",
        guild_id=1,
    ) is False

    row = store["pay-1"]
    assert row["status"] == starting_status
    assert row["tx_signature"] == "old-sig"
    assert row["tx_signature_history"] == []


def test_mark_payment_guards_remain_strict():
    store = {
        "confirmed-blocked": make_payment("confirmed-blocked", "failed"),
        "failed-blocked": make_payment("failed-blocked", "manual_hold"),
    }
    handler = build_handler(store)

    assert handler.mark_payment_confirmed("confirmed-blocked", guild_id=1) is False
    assert (
        handler.mark_payment_failed(
            "failed-blocked",
            error="still blocked",
            send_phase="submitted",
            guild_id=1,
        )
        is False
    )


def test_payment_service_rejects_sub_rent_test_payment_amount():
    with pytest.raises(ValueError, match="PAYMENT_TEST_AMOUNT_SOL=0.0001 is 100000 lamports"):
        PaymentService(build_handler({}), providers={}, test_payment_amount=0.0001)


@pytest.mark.parametrize(
    ("wallet", "expected"),
    [
        (None, "unknown"),
        ("short", "short"),
        ("AbCdEfGhIjKlMnOpQrStUvWxYz1234567890", "AbCd...7890"),
    ],
)
def test_redact_wallet(wallet, expected):
    assert redact_wallet(wallet) == expected


class FakeReconcileProvider:
    def __init__(self, status="confirmed", *, raises=None):
        self.status = status
        self.raises = raises
        self.status_calls = []

    async def check_status(self, tx_signature):
        self.status_calls.append(tx_signature)
        if self.raises is not None:
            raise self.raises
        return self.status


class FakeReconcileDB:
    def __init__(self, rows=None):
        self.rows = {row["payment_id"]: dict(row) for row in (rows or [])}
        self.confirmed_reconciles = []
        self.failed_reconciles = []
        self.manual_hold_calls = []
        self.provider_updates = []

    def get_payment_request(self, payment_id, guild_id=None):
        row = self.rows.get(payment_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def force_reconcile_payment_to_confirmed(self, payment_id, *, tx_signature, reason, guild_id=None):
        self.confirmed_reconciles.append((payment_id, tx_signature, reason, guild_id))
        row = self.rows[payment_id]
        row["status"] = "confirmed"
        row["tx_signature"] = tx_signature
        row["last_error"] = None
        return True

    def force_reconcile_payment_to_failed(self, payment_id, *, tx_signature, reason, guild_id=None):
        self.failed_reconciles.append((payment_id, tx_signature, reason, guild_id))
        row = self.rows[payment_id]
        row["status"] = "failed"
        row["tx_signature"] = tx_signature
        row["last_error"] = reason
        return True

    def get_legacy_provider_payment_requests(self, guild_ids=None):
        return [
            row for row in self.rows.values()
            if row.get("provider") == "solana"
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]

    def _update_payment_request_record(self, payment_id, payload, guild_id=None, allowed_statuses=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if row is None:
            return False
        if allowed_statuses and row.get("status") not in allowed_statuses:
            return False
        row.update(dict(payload))
        self.provider_updates.append((payment_id, payload.get("provider"), guild_id))
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if row is None:
            return False
        row["status"] = "manual_hold"
        row["last_error"] = reason
        self.manual_hold_calls.append((payment_id, reason, guild_id))
        return True


def _make_reconcilable_payment(
    payment_id,
    *,
    status="manual_hold",
    tx_signature="sig-123",
    provider="solana_payouts",
    submitted_at=None,
):
    return {
        "payment_id": payment_id,
        "guild_id": 1,
        "status": status,
        "tx_signature": tx_signature,
        "provider": provider,
        "submitted_at": submitted_at or datetime.now(timezone.utc).isoformat(),
        "producer": "admin_chat",
    }


@pytest.mark.anyio
async def test_reconcile_with_chain_not_applicable_for_non_submitted_states():
    db = FakeReconcileDB(
        [_make_reconcilable_payment("pay-queued", status="queued", tx_signature=None)]
    )
    service = PaymentService(db, providers={}, test_payment_amount=0.002)

    decision = await service.reconcile_with_chain("pay-queued", guild_id=1)

    assert decision.decision == "not_applicable"
    assert "does not require chain reconciliation" in decision.reason


@pytest.mark.anyio
async def test_reconcile_with_chain_allows_requeue_without_prior_signature():
    db = FakeReconcileDB(
        [_make_reconcilable_payment("pay-nosig", status="failed", tx_signature=None)]
    )
    service = PaymentService(db, providers={}, test_payment_amount=0.002)

    decision = await service.reconcile_with_chain("pay-nosig", guild_id=1)

    assert decision == ReconcileDecision(
        "allow_requeue",
        "no prior signature",
        None,
    )


@pytest.mark.anyio
async def test_reconcile_with_chain_keeps_hold_when_provider_is_unavailable():
    db = FakeReconcileDB(
        [_make_reconcilable_payment("pay-provider-missing", provider="solana")]
    )
    service = PaymentService(db, providers={}, test_payment_amount=0.002)

    decision = await service.reconcile_with_chain("pay-provider-missing", guild_id=1)

    assert decision.decision == "keep_in_hold"
    assert decision.reason == "provider unavailable"
    assert db.confirmed_reconciles == []
    assert db.failed_reconciles == []


@pytest.mark.anyio
async def test_reconcile_with_chain_reconciles_confirmed_status():
    db = FakeReconcileDB([_make_reconcilable_payment("pay-confirmed")])
    provider = FakeReconcileProvider(status="confirmed")
    service = PaymentService(
        db,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
    )

    decision = await service.reconcile_with_chain("pay-confirmed", guild_id=1)

    assert decision.decision == "reconciled_confirmed"
    assert db.confirmed_reconciles == [
        ("pay-confirmed", "sig-123", "chain reported confirmed during reconcile", 1)
    ]


@pytest.mark.anyio
async def test_reconcile_with_chain_reconciles_failed_status():
    db = FakeReconcileDB([_make_reconcilable_payment("pay-failed")])
    provider = FakeReconcileProvider(status="failed")
    service = PaymentService(
        db,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
    )

    decision = await service.reconcile_with_chain("pay-failed", guild_id=1)

    assert decision.decision == "reconciled_failed"
    assert db.failed_reconciles == [
        ("pay-failed", "sig-123", "chain reported failed during reconcile", 1)
    ]


@pytest.mark.anyio
async def test_reconcile_with_chain_allows_requeue_after_blockhash_window():
    db = FakeReconcileDB(
        [
            _make_reconcilable_payment(
                "pay-old-not-found",
                status="submitted",
                submitted_at=(datetime.now(timezone.utc) - timedelta(seconds=151)).isoformat(),
            )
        ]
    )
    provider = FakeReconcileProvider(status="not_found")
    service = PaymentService(
        db,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
    )

    decision = await service.reconcile_with_chain("pay-old-not-found", guild_id=1)

    assert decision.decision == "allow_requeue"
    assert decision.reason == "beyond 150s blockhash safety window"


@pytest.mark.anyio
async def test_reconcile_with_chain_keeps_hold_when_not_found_is_too_recent():
    db = FakeReconcileDB(
        [
            _make_reconcilable_payment(
                "pay-recent-not-found",
                status="submitted",
                submitted_at=(datetime.now(timezone.utc) - timedelta(seconds=149)).isoformat(),
            )
        ]
    )
    provider = FakeReconcileProvider(status="not_found")
    service = PaymentService(
        db,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
    )

    decision = await service.reconcile_with_chain("pay-recent-not-found", guild_id=1)

    assert decision.decision == "keep_in_hold"
    assert decision.reason == "too recent"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider",
    [
        FakeReconcileProvider(status="rpc_unreachable"),
        FakeReconcileProvider(raises=RuntimeError("rpc down")),
    ],
)
async def test_reconcile_with_chain_keeps_hold_when_rpc_is_unreachable(provider):
    db = FakeReconcileDB([_make_reconcilable_payment("pay-rpc")])
    service = PaymentService(
        db,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
    )

    decision = await service.reconcile_with_chain("pay-rpc", guild_id=1)

    assert decision.decision == "keep_in_hold"
    assert decision.reason == "RPC unreachable during reconcile"


def test_get_legacy_provider_payment_requests_filters_provider_and_guild():
    store = {
        "pay-legacy-a": {
            "payment_id": "pay-legacy-a",
            "guild_id": 1,
            "provider": "solana",
        },
        "pay-legacy-b": {
            "payment_id": "pay-legacy-b",
            "guild_id": 2,
            "provider": "solana",
        },
        "pay-modern": {
            "payment_id": "pay-modern",
            "guild_id": 1,
            "provider": "solana_payouts",
        },
    }
    handler = build_handler(store)
    handler._get_writable_guild_ids = lambda guild_ids=None: guild_ids

    rows = handler.get_legacy_provider_payment_requests(guild_ids=[1])

    assert [row["payment_id"] for row in rows] == ["pay-legacy-a"]


def test_migrate_legacy_provider_rows_rewrites_known_producers_and_holds_unknown(caplog):
    db = FakeReconcileDB(
        [
            {
                "payment_id": "pay-grants",
                "guild_id": 1,
                "provider": "solana",
                "producer": "grants",
                "status": "queued",
            },
            {
                "payment_id": "pay-admin",
                "guild_id": 1,
                "provider": "solana",
                "producer": "admin_chat",
                "status": "queued",
            },
            {
                "payment_id": "pay-unknown",
                "guild_id": 1,
                "provider": "solana",
                "producer": "mystery",
                "status": "queued",
            },
        ]
    )
    service = PaymentService(db, providers={}, test_payment_amount=0.002)

    caplog.set_level(logging.WARNING)
    migrated = service.migrate_legacy_provider_rows(guild_ids=[1])

    assert migrated == 2
    assert db.rows["pay-grants"]["provider"] == "solana_grants"
    assert db.rows["pay-admin"]["provider"] == "solana_payouts"
    assert db.rows["pay-unknown"]["status"] == "manual_hold"
    assert db.rows["pay-unknown"]["last_error"] == (
        "legacy provider could not be mapped: unknown producer=mystery"
    )
    assert "pay-grants" in caplog.text
    assert "pay-admin" in caplog.text
    assert "pay-unknown" in caplog.text
