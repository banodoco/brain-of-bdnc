from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from conftest import load_module_from_repo


check_payment_invariants = load_module_from_repo(
    "scripts/check_payment_invariants.py",
    "tests_check_payment_invariants",
)


@pytest.fixture
def synthetic_invariant_data():
    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    payments = [
        {
            "payment_id": "pay-chain-error",
            "guild_id": 1,
            "recipient_wallet": "wallet-good",
            "tx_signature": "sig-chain-error",
            "amount_token": 1.0,
            "amount_usd": 120.0,
            "is_test": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "confirmed_at": now.isoformat(),
            "completed_at": now.isoformat(),
            "status": "confirmed",
            "provider": "solana_payouts",
            "chain": "solana",
        },
        {
            "payment_id": "pay-stale",
            "guild_id": 1,
            "recipient_wallet": "wallet-stale",
            "tx_signature": None,
            "amount_token": 0.0,
            "amount_usd": None,
            "is_test": False,
            "created_at": (now - timedelta(hours=25)).isoformat(),
            "updated_at": (now - timedelta(hours=25)).isoformat(),
            "confirmed_at": None,
            "completed_at": None,
            "status": "processing",
            "provider": "solana_payouts",
            "chain": "solana",
        },
        {
            "payment_id": "pay-small-test",
            "guild_id": 1,
            "recipient_wallet": "wallet-ghost",
            "tx_signature": "sig-small-test",
            "amount_token": 0.0005,
            "amount_usd": None,
            "is_test": True,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "confirmed_at": now.isoformat(),
            "completed_at": now.isoformat(),
            "status": "confirmed",
            "provider": "solana_grants",
            "chain": "solana",
        },
        {
            "payment_id": "pay-cap",
            "guild_id": 1,
            "recipient_wallet": "wallet-cap",
            "tx_signature": "sig-cap",
            "amount_token": 0.5,
            "amount_usd": 95.0,
            "is_test": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "confirmed_at": now.isoformat(),
            "completed_at": now.isoformat(),
            "status": "confirmed",
            "provider": "solana_payouts",
            "chain": "solana",
        },
    ]
    wallets = [
        {
            "wallet_id": "wallet-ghost-1",
            "guild_id": 1,
            "discord_user_id": 42,
            "wallet_address": "wallet-ghost",
            "verified_at": now.isoformat(),
            "chain": "solana",
        },
        {
            "wallet_id": "wallet-reuse-1",
            "guild_id": 1,
            "discord_user_id": 10,
            "wallet_address": "wallet-reuse",
            "verified_at": None,
            "chain": "solana",
        },
        {
            "wallet_id": "wallet-reuse-2",
            "guild_id": 1,
            "discord_user_id": 11,
            "wallet_address": "wallet-reuse",
            "verified_at": None,
            "chain": "solana",
        },
    ]
    status_map = {
        "sig-chain-error": {"err": {"InstructionError": [0, "boom"]}},
        "sig-small-test": {"err": None},
        "sig-cap": {"err": None},
    }
    return now, payments, wallets, status_map


def test_evaluate_invariants_reports_each_violation(synthetic_invariant_data):
    now, payments, wallets, status_map = synthetic_invariant_data

    report = check_payment_invariants.evaluate_invariants(
        payments,
        wallets,
        status_map,
        now=now,
        admin_payout_daily_usd_cap=100.0,
    )

    assert report["summary"]["error_count"] >= 5
    assert report["checks"]["admin_payout_daily_cap"]["level"] == "error"
    assert report["checks"]["confirmed_rows_match_chain"]["violations"] == [
        {
            "payment_id": "pay-chain-error",
            "tx_signature": "sig-chain-error",
            "reason": "signature_has_chain_error",
            "chain_error": {"InstructionError": [0, "boom"]},
        }
    ]
    assert report["checks"]["stale_pending_or_processing"]["violations"][0]["payment_id"] == "pay-stale"
    assert report["checks"]["verified_wallets_have_test_payment"]["violations"][0]["wallet_id"] == "wallet-ghost-1"
    assert report["checks"]["wallet_address_reuse"]["violations"][0]["wallet_address"] == "wallet-reuse"
    assert report["checks"]["admin_payout_daily_cap"]["violations"][0]["reason"] == "cap_exceeded"


def test_main_prints_structured_report_and_exits_nonzero(
    monkeypatch,
    synthetic_invariant_data,
):
    now, payments, wallets, status_map = synthetic_invariant_data
    stdout = io.StringIO()
    monkeypatch.setenv("ADMIN_PAYOUT_DAILY_USD_CAP", "100")

    exit_code = check_payment_invariants.main(
        fetcher=lambda: (payments, wallets),
        status_fetcher=lambda signatures: {signature: status_map[signature] for signature in signatures},
        now=now,
        stdout=stdout,
    )

    output = stdout.getvalue()
    assert exit_code == 1
    assert '"confirmed_rows_match_chain"' in output
    assert '"stale_pending_or_processing"' in output
    assert '"verified_wallets_have_test_payment"' in output
    assert '"wallet_address_reuse"' in output
    assert '"admin_payout_daily_cap"' in output
