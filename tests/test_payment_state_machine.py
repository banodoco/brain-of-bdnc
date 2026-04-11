import asyncio
from datetime import datetime, timedelta, timezone

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from src.features.payments.payment_service import PaymentActor, PaymentActorKind, PaymentService
from src.features.payments.provider import SendResult


def _run(coro):
    return asyncio.run(coro)


class FakeStateProvider:
    def __init__(self):
        self.price = 100.0
        self.next_send_result = None
        self.status_map = {}

    async def send(self, recipient, amount_token):
        if self.next_send_result is None:
            raise RuntimeError("send scenario not configured")
        result = self.next_send_result
        self.next_send_result = None
        return result

    async def confirm_tx(self, tx_signature):
        return self.status_map.get(tx_signature, "not_found")

    async def check_status(self, tx_signature):
        return self.status_map.get(tx_signature, "not_found")

    async def get_token_price_usd(self):
        return self.price

    def token_name(self):
        return "SOL"


class FakeStateDB:
    def __init__(self, provider):
        self.provider = provider
        self.rows = {}
        self.wallets = {
            "wallet-1": {
                "wallet_id": "wallet-1",
                "guild_id": 1,
                "wallet_address": "11111111111111111111111111111111",
                "verified_at": None,
            }
        }
        self.history = {}

    def _record_history(self, payment_id):
        row = self.rows[payment_id]
        self.history.setdefault(payment_id, []).append(row["status"])

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test=None):
        rows = [
            dict(row)
            for row in self.rows.values()
            if row.get("guild_id") == guild_id
            and row.get("producer") == producer
            and row.get("producer_ref") == producer_ref
            and (is_test is None or row.get("is_test") == is_test)
        ]
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def create_payment_request(self, record, guild_id=None):
        payment_id = f"pay-{len(self.rows) + 1}"
        now = datetime.now(timezone.utc)
        row = {
            "payment_id": payment_id,
            "created_at": now,
            "updated_at": now,
            **dict(record),
            "guild_id": guild_id or record.get("guild_id"),
        }
        self.rows[payment_id] = row
        self._record_history(payment_id)
        return dict(row)

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        wallet = self.wallets.get(wallet_id)
        if wallet and guild_id is not None and wallet.get("guild_id") != guild_id:
            return None
        return dict(wallet) if wallet else None

    def get_payment_request(self, payment_id, guild_id=None):
        row = self.rows.get(payment_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return dict(row) if row else None

    def mark_payment_confirmed_by_user(self, payment_id, guild_id=None, confirmed_by_user_id=None, confirmed_by="user", scheduled_at=None):
        row = self.rows[payment_id]
        if row["status"] != "pending_confirmation":
            return False
        row.update(
            {
                "status": "queued",
                "confirmed_by": confirmed_by,
                "confirmed_by_user_id": confirmed_by_user_id,
                "scheduled_at": scheduled_at,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._record_history(payment_id)
        return True

    def claim_for_execution(self, payment_id):
        row = self.rows[payment_id]
        if row["status"] != "queued":
            return False
        row["status"] = "processing"
        row["updated_at"] = datetime.now(timezone.utc)
        self._record_history(payment_id)
        return True

    def mark_payment_submitted(self, payment_id, tx_signature, amount_token=None, token_price_usd=None, send_phase="submitted", guild_id=None):
        row = self.rows[payment_id]
        if row["status"] != "processing":
            return False
        now = datetime.now(timezone.utc)
        row.update(
            {
                "status": "submitted",
                "tx_signature": tx_signature,
                "amount_token": amount_token,
                "token_price_usd": token_price_usd,
                "send_phase": send_phase,
                "submitted_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        self.provider.status_map[tx_signature] = "not_found"
        self._record_history(payment_id)
        return True

    def mark_payment_confirmed(self, payment_id, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] != "submitted":
            return False
        now = datetime.now(timezone.utc)
        row.update(
            {
                "status": "confirmed",
                "completed_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        if row.get("tx_signature"):
            self.provider.status_map[row["tx_signature"]] = "confirmed"
        self._record_history(payment_id)
        return True

    def mark_payment_failed(self, payment_id, error, send_phase=None, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] not in {"processing", "submitted"}:
            return False
        now = datetime.now(timezone.utc)
        row.update(
            {
                "status": "failed",
                "completed_at": now,
                "updated_at": now,
                "last_error": error,
                "send_phase": send_phase,
            }
        )
        if row.get("tx_signature"):
            self.provider.status_map[row["tx_signature"]] = "failed"
        self._record_history(payment_id)
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] not in {"pending_confirmation", "queued", "processing", "submitted", "failed"}:
            return False
        row.update(
            {
                "status": "manual_hold",
                "updated_at": datetime.now(timezone.utc),
                "last_error": reason,
            }
        )
        self._record_history(payment_id)
        return True

    def requeue_payment(self, payment_id, retry_after=None, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] != "failed":
            return False
        row.update(
            {
                "status": "queued",
                "updated_at": datetime.now(timezone.utc),
                "retry_after": retry_after,
                "last_error": None,
                "tx_signature": None,
                "send_phase": None,
                "submitted_at": None,
                "completed_at": None,
            }
        )
        self._record_history(payment_id)
        return True

    def release_payment_hold(self, payment_id, new_status, guild_id=None, reason=None):
        row = self.rows[payment_id]
        if row["status"] != "manual_hold" or new_status not in {"failed", "manual_hold"}:
            return False
        row["status"] = new_status
        row["updated_at"] = datetime.now(timezone.utc)
        if reason is not None:
            row["last_error"] = reason
        self._record_history(payment_id)
        return True

    def get_inflight_payments_for_recovery(self, guild_ids=None):
        return [
            dict(row)
            for row in self.rows.values()
            if row.get("status") in {"processing", "submitted"}
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]

    def mark_wallet_verified(self, wallet_id, guild_id=None):
        wallet = self.wallets[wallet_id]
        wallet["verified_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        return sum(
            float(row.get("amount_usd") or 0.0)
            for row in self.rows.values()
            if row.get("guild_id") == guild_id
            and row.get("provider") == provider
            and row.get("status") == "confirmed"
            and row.get("completed_at") is not None
            and row.get("completed_at") >= cutoff
        )

    def force_reconcile_payment_to_confirmed(self, payment_id, *, tx_signature, reason, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] not in {"submitted", "processing", "failed", "manual_hold"}:
            return False
        now = datetime.now(timezone.utc)
        row.update(
            {
                "status": "confirmed",
                "tx_signature": tx_signature,
                "completed_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        self.provider.status_map[tx_signature] = "confirmed"
        self._record_history(payment_id)
        return True

    def force_reconcile_payment_to_failed(self, payment_id, *, tx_signature, reason, guild_id=None):
        row = self.rows[payment_id]
        if row["status"] not in {"submitted", "processing", "failed", "manual_hold"}:
            return False
        now = datetime.now(timezone.utc)
        row.update(
            {
                "status": "failed",
                "tx_signature": tx_signature,
                "completed_at": now,
                "updated_at": now,
                "last_error": reason,
            }
        )
        self.provider.status_map[tx_signature] = "failed"
        self._record_history(payment_id)
        return True


class PaymentStateMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.provider = FakeStateProvider()
        self.db = FakeStateDB(self.provider)
        self.service = PaymentService(
            db_handler=self.db,
            providers={"solana_native": self.provider},
            test_payment_amount=0.002,
            per_payment_usd_cap=500.0,
            daily_usd_cap=1000.0,
            capped_providers={"solana_native"},
        )
        self.sequence = 0
        self.execute_failures = set()

    def _first_payment_with_status(self, *statuses):
        for payment_id, row in sorted(self.db.rows.items()):
            if row.get("status") in statuses:
                return payment_id
        return None

    @rule(
        is_test=st.booleans(),
        producer=st.sampled_from(["grants", "admin_chat"]),
        amount_token=st.sampled_from([0.5, 1.0, 2.0, 3.0]),
        use_amount_usd=st.booleans(),
    )
    def create_payment(self, is_test, producer, amount_token, use_amount_usd):
        self.sequence += 1
        kwargs = {
            "producer": producer,
            "producer_ref": f"{producer}-{self.sequence}",
            "guild_id": 1,
            "recipient_wallet": "11111111111111111111111111111111",
            "chain": "solana",
            "provider": "solana_native",
            "is_test": is_test,
            "confirm_channel_id": 55,
            "notify_channel_id": 55,
            "recipient_discord_id": 42,
            "wallet_id": "wallet-1",
            "metadata": {"sequence": self.sequence},
        }
        if not is_test:
            if use_amount_usd:
                kwargs["amount_usd"] = amount_token * 100.0
            else:
                kwargs["amount_token"] = amount_token

        _run(self.service.request_payment(**kwargs))

    @rule()
    def confirm_payment(self):
        payment_id = self._first_payment_with_status("pending_confirmation")
        if not payment_id:
            return
        payment = self.db.rows[payment_id]
        actor = PaymentActor(PaymentActorKind.AUTO, 42) if payment.get("is_test") else PaymentActor(PaymentActorKind.RECIPIENT_MESSAGE, 42)
        self.service.confirm_payment(payment_id, guild_id=1, actor=actor)

    @rule(
        send_phase=st.sampled_from(["submitted", "failed", "timeout", "ambiguous", "pre_submit"]),
        confirm_status=st.sampled_from(["confirmed", "failed", "timeout", "rpc_unreachable"]),
    )
    def execute_payment(self, send_phase, confirm_status):
        payment_id = self._first_payment_with_status("queued", "processing", "submitted")
        if not payment_id:
            return
        row = self.db.rows[payment_id]
        if row["status"] == "queued":
            self.db.claim_for_execution(payment_id)
        if self.db.rows[payment_id]["status"] == "processing":
            if send_phase == "submitted":
                signature = f"sig-{payment_id}-{self.sequence}"
                self.provider.status_map[signature] = confirm_status
                self.provider.next_send_result = SendResult(signature=signature, phase="submitted", error=None)
            elif send_phase == "failed":
                self.provider.next_send_result = SendResult(signature=None, phase="pre_submit", error="send failed")
            elif send_phase == "timeout":
                signature = f"sig-{payment_id}-{self.sequence}"
                self.provider.status_map[signature] = "timeout"
                self.provider.next_send_result = SendResult(signature=signature, phase="submitted", error=None)
            elif send_phase == "ambiguous":
                self.provider.next_send_result = SendResult(signature=None, phase="ambiguous", error="ambiguous send")
            else:
                self.provider.next_send_result = SendResult(signature=None, phase="pre_submit", error="pre-submit error")
        elif row["status"] == "submitted" and row.get("tx_signature"):
            self.provider.status_map[row["tx_signature"]] = confirm_status

        try:
            _run(self.service.execute_payment(payment_id, guild_id=1))
        except Exception:
            self.execute_failures.add(payment_id)
            self.db.mark_payment_manual_hold(payment_id, reason="worker fallback after execute exception", guild_id=1)

    @rule()
    def recover_inflight(self):
        _run(self.service.recover_inflight(guild_ids=[1]))

    @rule()
    def release_payment_hold(self):
        payment_id = self._first_payment_with_status("manual_hold")
        if not payment_id:
            return
        self.db.release_payment_hold(payment_id, "failed", guild_id=1, reason="released by operator")

    @rule()
    def requeue_payment(self):
        payment_id = self._first_payment_with_status("failed")
        if not payment_id:
            return
        self.db.requeue_payment(payment_id, guild_id=1)

    @rule(status=st.sampled_from(["confirmed", "failed", "not_found", "rpc_unreachable"]))
    def reconcile_with_chain(self, status):
        payment_id = self._first_payment_with_status("submitted", "processing", "failed", "manual_hold")
        if not payment_id:
            return
        row = self.db.rows[payment_id]
        if status == "not_found":
            row["submitted_at"] = datetime.now(timezone.utc) - timedelta(seconds=200)
            row["updated_at"] = row["submitted_at"]
        if row.get("tx_signature"):
            self.provider.status_map[row["tx_signature"]] = status
        _run(self.service.reconcile_with_chain(payment_id, guild_id=1))

    @invariant()
    def confirmed_rows_have_confirmed_signatures(self):
        for row in self.db.rows.values():
            if row.get("status") == "confirmed":
                assert row.get("tx_signature") is not None
                assert _run(self.provider.check_status(row["tx_signature"])) == "confirmed"

    @invariant()
    def no_terminal_row_moves_backwards(self):
        for history in self.db.history.values():
            seen_terminal = False
            for status in history:
                if status in {"confirmed", "cancelled"}:
                    seen_terminal = True
                    continue
                if seen_terminal:
                    assert status not in {"queued", "processing", "submitted"}

    @invariant()
    def capped_confirmed_sum_stays_within_expected_bound(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        confirmed_sum = sum(
            float(row.get("amount_usd") or 0.0)
            for row in self.db.rows.values()
            if row.get("status") == "confirmed"
            and row.get("provider") in self.service.capped_providers
            and row.get("completed_at") is not None
            and row.get("completed_at") >= cutoff
        )
        assert confirmed_sum <= (self.service.daily_usd_cap + self.service.per_payment_usd_cap)

    @invariant()
    def test_rows_never_store_usd_fields(self):
        for row in self.db.rows.values():
            if row.get("is_test"):
                assert row.get("amount_usd") is None
                assert row.get("token_price_usd") is None

    @invariant()
    def execute_failures_fail_closed(self):
        for payment_id in self.execute_failures:
            row = self.db.rows[payment_id]
            assert row.get("status") not in {"processing", "submitted"}


TestPaymentStateMachine = PaymentStateMachine.TestCase
TestPaymentStateMachine.settings = settings(max_examples=40, stateful_step_count=25)
