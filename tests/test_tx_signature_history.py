from datetime import datetime, timezone

from src.common.db_handler import DatabaseHandler


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakePaymentRequestQuery:
    def __init__(self, rows, mode, payload=None):
        self.rows = rows
        self.mode = mode
        self.payload = payload or {}
        self.filters = []
        self.limit_count = None

    def eq(self, key, value):
        self.filters.append((key, value))
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


def make_payment(payment_id):
    return {
        "payment_id": payment_id,
        "guild_id": 1,
        "status": "processing",
        "tx_signature": None,
        "tx_signature_history": [],
        "send_phase": None,
        "submitted_at": None,
        "completed_at": None,
        "retry_after": None,
        "last_error": None,
    }


def test_mark_payment_submitted_and_confirmed_append_history():
    store = {"pay-history": make_payment("pay-history")}
    handler = build_handler(store)

    assert handler.mark_payment_submitted("pay-history", tx_signature="sig-1", guild_id=1) is True
    assert handler.mark_payment_confirmed("pay-history", guild_id=1) is True

    history = store["pay-history"]["tx_signature_history"]
    assert [entry["reason"] for entry in history] == ["submit", "confirm"]
    assert history[0]["signature"] == "sig-1"
    assert history[0]["status"] == "submitted"
    assert history[0]["send_phase"] == "submitted"
    assert history[1]["signature"] == "sig-1"
    assert history[1]["status"] == "confirmed"
    assert history[1]["send_phase"] == "submitted"
    assert isinstance(history[0]["timestamp"], datetime)
    assert history[0]["timestamp"].tzinfo == timezone.utc


def test_requeue_preserves_prior_signatures_across_two_cycles():
    store = {"pay-requeue": make_payment("pay-requeue")}
    handler = build_handler(store)

    assert handler.mark_payment_submitted("pay-requeue", tx_signature="sig-1", guild_id=1) is True
    assert handler.mark_payment_failed("pay-requeue", error="boom-1", send_phase="submitted", guild_id=1) is True
    assert handler.requeue_payment("pay-requeue", guild_id=1) is True

    store["pay-requeue"]["status"] = "processing"

    assert handler.mark_payment_submitted("pay-requeue", tx_signature="sig-2", guild_id=1) is True
    assert handler.mark_payment_failed("pay-requeue", error="boom-2", send_phase="submitted", guild_id=1) is True
    assert handler.requeue_payment("pay-requeue", guild_id=1) is True

    row = store["pay-requeue"]
    history = row["tx_signature_history"]
    requeue_entries = [entry for entry in history if entry["reason"] == "requeue"]

    assert row["status"] == "queued"
    assert row["tx_signature"] is None
    assert [entry["signature"] for entry in requeue_entries] == ["sig-1", "sig-2"]
    assert all(entry["status"] == "failed" for entry in requeue_entries)
    assert all(entry["send_phase"] == "submitted" for entry in requeue_entries)
    assert len(history) == 4
