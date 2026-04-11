"""Daily read-only audit for Solana payment invariants.

Run from project root:
    python scripts/check_payment_invariants.py

The script prints a structured JSON report and exits non-zero when any hard
invariant fails. It does not mutate the database.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
RPC_BATCH_SIZE = 256
STALE_PAYMENT_WINDOW = timedelta(hours=24)
MIN_TEST_PAYMENT_SOL = 0.001
CAP_WARNING_THRESHOLD = 0.9
CAP_ERROR_THRESHOLD = 1.0


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_aware_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _is_solana_row(row: dict[str, Any]) -> bool:
    chain = str(row.get("chain") or "").strip().lower()
    provider = str(row.get("provider") or "").strip().lower()
    return chain == "solana" or provider.startswith("solana")


def rpc_call(method: str, params: list[Any], *, rpc_url: str = SOLANA_RPC_URL) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
                if "error" in body:
                    raise RuntimeError(f"RPC error: {body['error']}")
                return body["result"]
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503) and attempt < 4:
                time.sleep(2**attempt)
                continue
            raise
        except Exception:
            if attempt < 4:
                time.sleep(2**attempt)
                continue
            raise


def get_signature_status_map(
    signatures: list[str],
    *,
    rpc_url: str = SOLANA_RPC_URL,
) -> dict[str, dict[str, Any] | None]:
    status_map: dict[str, dict[str, Any] | None] = {}
    for index in range(0, len(signatures), RPC_BATCH_SIZE):
        chunk = signatures[index:index + RPC_BATCH_SIZE]
        result = rpc_call(
            "getSignatureStatuses",
            [chunk, {"searchTransactionHistory": True}],
            rpc_url=rpc_url,
        )
        values = result.get("value", [])
        for signature, status in zip(chunk, values):
            status_map[signature] = status
        time.sleep(0.2)
    return status_map


def _fetch_via_psycopg2() -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        return None

    conn_str = os.environ.get("SUPABASE_CONNECTION_STRING")
    if not conn_str:
        return None

    try:
        conn = psycopg2.connect(conn_str, connect_timeout=10)
    except Exception:
        return None

    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT payment_id, guild_id, recipient_wallet, tx_signature,
                       amount_token, amount_usd, is_test, created_at, updated_at,
                       confirmed_at, completed_at, status, provider, chain
                FROM payment_requests
                """
            )
            payments = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT wallet_id, guild_id, discord_user_id, wallet_address,
                       verified_at, chain
                FROM wallet_registry
                """
            )
            wallets = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return payments, wallets


def _fetch_via_supabase_rest() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set when psycopg2 is unavailable"
        )

    client = create_client(url, key)
    page_size = 1000

    payments: list[dict[str, Any]] = []
    page = 0
    while True:
        response = (
            client.table("payment_requests")
            .select(
                "payment_id,guild_id,recipient_wallet,tx_signature,amount_token,"
                "amount_usd,is_test,created_at,updated_at,confirmed_at,"
                "completed_at,status,provider,chain"
            )
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = response.data or []
        payments.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    wallets: list[dict[str, Any]] = []
    page = 0
    while True:
        response = (
            client.table("wallet_registry")
            .select("wallet_id,guild_id,discord_user_id,wallet_address,verified_at,chain")
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = response.data or []
        wallets.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    return payments, wallets


def fetch_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = _fetch_via_psycopg2()
    if result is not None:
        return result
    return _fetch_via_supabase_rest()


def evaluate_invariants(
    payments: list[dict[str, Any]],
    wallets: list[dict[str, Any]],
    status_map: dict[str, dict[str, Any] | None],
    *,
    now: Optional[datetime] = None,
    admin_payout_daily_usd_cap: Optional[float] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - STALE_PAYMENT_WINDOW
    report: dict[str, Any] = {
        "generated_at": now,
        "checks": {
            "confirmed_rows_match_chain": {"violations": []},
            "stale_pending_or_processing": {"violations": []},
            "verified_wallets_have_test_payment": {"violations": []},
            "wallet_address_reuse": {"violations": []},
            "admin_payout_daily_cap": {
                "level": "ok",
                "violations": [],
                "warnings": [],
            },
        },
        "summary": {"error_count": 0, "warning_count": 0},
    }

    def add_error(check_name: str, violation: dict[str, Any]) -> None:
        report["checks"][check_name]["violations"].append(violation)
        report["summary"]["error_count"] += 1

    def add_warning(check_name: str, warning: dict[str, Any]) -> None:
        report["checks"][check_name]["warnings"].append(warning)
        report["summary"]["warning_count"] += 1

    for payment in payments:
        if payment.get("status") != "confirmed" or not _is_solana_row(payment):
            continue
        signature = payment.get("tx_signature")
        if not signature:
            add_error(
                "confirmed_rows_match_chain",
                {
                    "payment_id": payment.get("payment_id"),
                    "reason": "missing_tx_signature",
                },
            )
            continue
        status = status_map.get(str(signature))
        if status is None:
            add_error(
                "confirmed_rows_match_chain",
                {
                    "payment_id": payment.get("payment_id"),
                    "tx_signature": signature,
                    "reason": "signature_not_found_on_chain",
                },
            )
            continue
        if status.get("err") is not None:
            add_error(
                "confirmed_rows_match_chain",
                {
                    "payment_id": payment.get("payment_id"),
                    "tx_signature": signature,
                    "reason": "signature_has_chain_error",
                    "chain_error": status.get("err"),
                },
            )

    for payment in payments:
        if payment.get("status") not in {"pending_confirmation", "processing"}:
            continue
        updated_at = _to_aware_utc(payment.get("updated_at")) or _to_aware_utc(payment.get("created_at"))
        if updated_at and updated_at < stale_cutoff:
            add_error(
                "stale_pending_or_processing",
                {
                    "payment_id": payment.get("payment_id"),
                    "status": payment.get("status"),
                    "updated_at": updated_at,
                },
            )

    qualifying_test_payments = {
        (
            int(payment["guild_id"]),
            str(payment.get("recipient_wallet") or "").strip().lower(),
        )
        for payment in payments
        if payment.get("status") == "confirmed"
        and bool(payment.get("is_test"))
        and _is_solana_row(payment)
        and float(payment.get("amount_token") or 0.0) >= MIN_TEST_PAYMENT_SOL
    }
    for wallet in wallets:
        if str(wallet.get("chain") or "").strip().lower() != "solana":
            continue
        if not wallet.get("verified_at"):
            continue
        wallet_key = (
            int(wallet["guild_id"]),
            str(wallet.get("wallet_address") or "").strip().lower(),
        )
        if wallet_key not in qualifying_test_payments:
            add_error(
                "verified_wallets_have_test_payment",
                {
                    "wallet_id": wallet.get("wallet_id"),
                    "guild_id": wallet.get("guild_id"),
                    "discord_user_id": wallet.get("discord_user_id"),
                    "wallet_address": wallet.get("wallet_address"),
                },
            )

    wallet_index: dict[str, list[dict[str, Any]]] = {}
    for wallet in wallets:
        if str(wallet.get("chain") or "").strip().lower() != "solana":
            continue
        address = str(wallet.get("wallet_address") or "").strip().lower()
        if not address:
            continue
        wallet_index.setdefault(address, []).append(wallet)
    for address, rows in wallet_index.items():
        distinct_users = sorted(
            {int(row["discord_user_id"]) for row in rows if row.get("discord_user_id") is not None}
        )
        if len(distinct_users) > 1:
            add_error(
                "wallet_address_reuse",
                {
                    "wallet_address": address,
                    "discord_user_ids": distinct_users,
                    "wallet_ids": [row.get("wallet_id") for row in rows],
                },
            )

    cap_check = report["checks"]["admin_payout_daily_cap"]
    if admin_payout_daily_usd_cap and admin_payout_daily_usd_cap > 0:
        rolling_total = 0.0
        for payment in payments:
            if payment.get("status") != "confirmed":
                continue
            if str(payment.get("provider") or "").strip().lower() != "solana_payouts":
                continue
            completed_at = (
                _to_aware_utc(payment.get("completed_at"))
                or _to_aware_utc(payment.get("confirmed_at"))
                or _to_aware_utc(payment.get("updated_at"))
                or _to_aware_utc(payment.get("created_at"))
            )
            if completed_at is None or completed_at < stale_cutoff:
                continue
            rolling_total += float(payment.get("amount_usd") or 0.0)

        usage_ratio = rolling_total / admin_payout_daily_usd_cap
        cap_check["rolling_total_usd"] = rolling_total
        cap_check["cap_usd"] = admin_payout_daily_usd_cap
        cap_check["usage_ratio"] = usage_ratio
        if usage_ratio >= CAP_ERROR_THRESHOLD:
            cap_check["level"] = "error"
            add_error(
                "admin_payout_daily_cap",
                {
                    "reason": "cap_exceeded",
                    "rolling_total_usd": rolling_total,
                    "cap_usd": admin_payout_daily_usd_cap,
                },
            )
        elif usage_ratio >= CAP_WARNING_THRESHOLD:
            cap_check["level"] = "warning"
            add_warning(
                "admin_payout_daily_cap",
                {
                    "reason": "cap_near_limit",
                    "rolling_total_usd": rolling_total,
                    "cap_usd": admin_payout_daily_usd_cap,
                },
            )
    else:
        cap_check["level"] = "warning"
        add_warning(
            "admin_payout_daily_cap",
            {"reason": "ADMIN_PAYOUT_DAILY_USD_CAP not configured"},
        )

    return report


def main(
    *,
    fetcher: Optional[Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]]] = None,
    status_fetcher: Optional[Callable[[list[str]], dict[str, dict[str, Any] | None]]] = None,
    now: Optional[datetime] = None,
    stdout=None,
) -> int:
    out = stdout or sys.stdout
    try:
        payments, wallets = (fetcher or fetch_rows)()
        signatures = [
            str(payment["tx_signature"])
            for payment in payments
            if payment.get("status") == "confirmed"
            and _is_solana_row(payment)
            and payment.get("tx_signature")
        ]
        status_map = (status_fetcher or get_signature_status_map)(signatures)
        cap_env = os.getenv("ADMIN_PAYOUT_DAILY_USD_CAP")
        cap_value = float(cap_env) if cap_env else None
        report = evaluate_invariants(
            payments,
            wallets,
            status_map,
            now=now,
            admin_payout_daily_usd_cap=cap_value,
        )
        print(json.dumps(report, indent=2, default=_json_default, sort_keys=True), file=out)
        return 1 if report["summary"]["error_count"] else 0
    except Exception as exc:
        error_report = {
            "error": str(exc),
            "generated_at": (now or datetime.now(timezone.utc)),
        }
        print(json.dumps(error_report, indent=2, default=_json_default, sort_keys=True), file=out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
