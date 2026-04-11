"""VERDICT 2026-04-11: `claim_due_payment_requests` uses `FOR UPDATE SKIP LOCKED`
and Postgres read-committed isolation, so the queued -> processing claim step is
structurally safe at the database boundary. The surviving gremlin was post-claim:
`PaymentWorkerCog.cog_load()` started the worker before `recover_inflight()`
finished, which allowed freshly claimed processing/submitted rows to be rescanned
during startup. That overlap is fixed by the 150-second recovery reclaim window in
`PaymentService.recover_inflight()`. This test is a logical-correctness sanity
check, not a proof of DB-level concurrency safety.
"""

from datetime import datetime, timezone

import pytest

from src.features.payments.payment_service import PaymentService
from src.features.payments.provider import SendResult


class HookedProvider:
    def __init__(self, *, hook_phase, hook_coro):
        self.hook_phase = hook_phase
        self.hook_coro = hook_coro

    async def send(self, recipient, amount_token):
        if self.hook_phase == "send":
            await self.hook_coro()
        return SendResult(signature="sig-race", phase="submitted", error=None)

    async def confirm_tx(self, tx_signature):
        if self.hook_phase == "confirm":
            await self.hook_coro()
        return "confirmed"

    async def check_status(self, tx_signature):
        return "confirmed"

    async def get_token_price_usd(self):
        return 100.0

    def token_name(self):
        return "SOL"


class FakeRaceDB:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.row = {
            "payment_id": "pay-race",
            "guild_id": 1,
            "status": "processing",
            "provider": "solana_native",
            "recipient_wallet": "race-wallet",
            "amount_token": 1.0,
            "token_price_usd": 100.0,
            "is_test": False,
            "tx_signature": None,
            "send_phase": None,
            "submitted_at": None,
            "updated_at": now,
            "last_error": None,
        }
        self.transitions = []

    def get_payment_request(self, payment_id, guild_id=None):
        if payment_id != self.row["payment_id"]:
            return None
        if guild_id is not None and guild_id != self.row["guild_id"]:
            return None
        return dict(self.row)

    def get_inflight_payments_for_recovery(self, guild_ids=None):
        if self.row["status"] not in {"processing", "submitted"}:
            return []
        if guild_ids is not None and self.row["guild_id"] not in guild_ids:
            return []
        return [dict(self.row)]

    def mark_payment_submitted(self, payment_id, tx_signature, amount_token=None, token_price_usd=None, send_phase="submitted", guild_id=None):
        if self.row["status"] != "processing":
            return False
        now = datetime.now(timezone.utc)
        self.row.update(
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
        self.transitions.append(("submitted", tx_signature))
        return True

    def mark_payment_confirmed(self, payment_id, guild_id=None):
        if self.row["status"] != "submitted":
            return False
        now = datetime.now(timezone.utc)
        self.row.update(
            {
                "status": "confirmed",
                "updated_at": now,
                "completed_at": now,
                "last_error": None,
            }
        )
        self.transitions.append(("confirmed", self.row["tx_signature"]))
        return True

    def mark_payment_failed(self, payment_id, error, send_phase=None, guild_id=None):
        if self.row["status"] not in {"processing", "submitted"}:
            return False
        now = datetime.now(timezone.utc)
        self.row.update(
            {
                "status": "failed",
                "send_phase": send_phase,
                "updated_at": now,
                "completed_at": now,
                "last_error": error,
            }
        )
        self.transitions.append(("failed", error))
        return True

    def requeue_payment(self, payment_id, retry_after=None, guild_id=None):
        if self.row["status"] != "failed":
            return False
        now = datetime.now(timezone.utc)
        self.row.update(
            {
                "status": "queued",
                "updated_at": now,
                "retry_after": retry_after,
                "last_error": None,
            }
        )
        self.transitions.append(("requeue", retry_after))
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        if self.row["status"] not in {"processing", "submitted", "failed", "queued", "pending_confirmation"}:
            return False
        now = datetime.now(timezone.utc)
        self.row.update(
            {
                "status": "manual_hold",
                "updated_at": now,
                "last_error": reason,
            }
        )
        self.transitions.append(("manual_hold", reason))
        return True


@pytest.mark.anyio
@pytest.mark.parametrize("hook_phase", ["send", "confirm"])
async def test_recover_inflight_and_execute_payment_logical_ordering(hook_phase):
    db_handler = FakeRaceDB()
    service = PaymentService(
        db_handler=db_handler,
        providers={},
        test_payment_amount=0.002,
        logger_instance=None,
    )
    provider = HookedProvider(
        hook_phase=hook_phase,
        hook_coro=lambda: service.recover_inflight(guild_ids=[1]),
    )
    service.providers = {"solana_native": provider}

    result = await service.execute_payment("pay-race", guild_id=1)

    assert result["status"] == "confirmed"
    assert [transition[0] for transition in db_handler.transitions].count("confirmed") == 1
    assert all(transition[0] not in {"failed", "requeue", "manual_hold"} for transition in db_handler.transitions)
