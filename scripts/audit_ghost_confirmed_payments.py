"""Read-only audit: find Solana payments marked 'confirmed' that didn't actually land.

Run from project root:
    python scripts/audit_ghost_confirmed_payments.py

Will not mutate anything. Queries Supabase via psycopg2 with
SUPABASE_CONNECTION_STRING, then calls Solana mainnet RPC
(`getSignatureStatuses` with `searchTransactionHistory: true`) for every
confirmed signature and flags:

    1. Payments whose on-chain status has a non-null ``err``.
    2. Payments whose signature returns ``null`` (never landed).
    3. Verified wallets whose verifying test payment amount was below
       0.001 SOL — those are below the rent-exempt floor and were
       guaranteed to be ghost-confirmed prior to the Bug A/Bug B fixes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """Tiny .env loader so the script is self-contained."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()


# Use the public Solana RPC endpoint for this audit rather than the Helius
# endpoint in .env so we don't burn the production quota on a read-only audit.
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
BATCH_SIZE = 256  # getSignatureStatuses accepts up to 256 sigs per call.


def rpc_call(method: str, params: list[Any]) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        SOLANA_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
                if "error" in body:
                    raise RuntimeError(f"RPC error: {body['error']}")
                return body["result"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise


def get_statuses(signatures: list[str]) -> list[dict | None]:
    out: list[dict | None] = []
    for i in range(0, len(signatures), BATCH_SIZE):
        chunk = signatures[i : i + BATCH_SIZE]
        result = rpc_call(
            "getSignatureStatuses",
            [chunk, {"searchTransactionHistory": True}],
        )
        value = result.get("value", [])
        out.extend(value)
        time.sleep(0.2)  # gentle rate limit
    return out


def _fetch_via_psycopg2() -> tuple[list[dict], list[dict]] | None:
    """Try direct postgres first; return (payments, wallets) or None on failure."""
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
    except Exception as e:
        print(f"psycopg2 connect failed ({e}); falling back to supabase-py REST.")
        return None
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT payment_id, guild_id, recipient_wallet, tx_signature,
                       amount_token, is_test, confirmed_at, status
                FROM payment_requests
                WHERE status = 'confirmed'
                  AND tx_signature IS NOT NULL
                  AND chain = 'solana'
                ORDER BY confirmed_at DESC NULLS LAST
                """
            )
            payments = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT wallet_id, guild_id, discord_user_id, wallet_address,
                       verified_at
                FROM wallet_registry
                WHERE chain = 'solana'
                  AND verified_at IS NOT NULL
                """
            )
            wallets = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return payments, wallets


def _fetch_via_supabase_rest() -> tuple[list[dict], list[dict]]:
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set for REST fallback"
        )
    client = create_client(url, key)

    payments: list[dict] = []
    page = 0
    page_size = 1000
    while True:
        resp = (
            client.table("payment_requests")
            .select(
                "payment_id,guild_id,recipient_wallet,tx_signature,"
                "amount_token,is_test,confirmed_at,status,chain"
            )
            .eq("status", "confirmed")
            .eq("chain", "solana")
            .not_.is_("tx_signature", "null")
            .order("confirmed_at", desc=True)
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = resp.data or []
        payments.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    wallets: list[dict] = []
    page = 0
    while True:
        resp = (
            client.table("wallet_registry")
            .select("wallet_id,guild_id,discord_user_id,wallet_address,verified_at,chain")
            .eq("chain", "solana")
            .not_.is_("verified_at", "null")
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = resp.data or []
        wallets.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    return payments, wallets


def _fetch_ghost_wallets_rest(wallets: list[dict]) -> list[dict]:
    """REST-only version of the ghost-wallet join using client-side join."""
    from supabase import create_client

    if not wallets:
        return []

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    client = create_client(url, key)

    # Pull every test payment in small chunks and filter client-side.
    tests: list[dict] = []
    page = 0
    page_size = 1000
    while True:
        resp = (
            client.table("payment_requests")
            .select(
                "payment_id,guild_id,recipient_wallet,amount_token,"
                "tx_signature,confirmed_at,chain,is_test"
            )
            .eq("is_test", True)
            .eq("chain", "solana")
            .lt("amount_token", 0.001)
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = resp.data or []
        tests.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    # Key tests by (guild_id, recipient_wallet), keep the latest.
    tests.sort(key=lambda r: (r.get("confirmed_at") or ""), reverse=True)
    test_map: dict[tuple, dict] = {}
    for t in tests:
        k = (t["guild_id"], t["recipient_wallet"])
        test_map.setdefault(k, t)

    ghost: list[dict] = []
    for w in wallets:
        k = (w["guild_id"], w["wallet_address"])
        t = test_map.get(k)
        if t is not None:
            ghost.append(
                {
                    "wallet_id": w["wallet_id"],
                    "guild_id": w["guild_id"],
                    "discord_user_id": w["discord_user_id"],
                    "wallet_address": w["wallet_address"],
                    "verified_at": w["verified_at"],
                    "test_amount_token": t["amount_token"],
                    "test_tx_signature": t["tx_signature"],
                    "test_payment_id": t["payment_id"],
                }
            )
    return ghost


def main() -> int:
    print("Connecting to Supabase (read-only)...")
    used_rest = False
    fetched = _fetch_via_psycopg2()
    if fetched is None:
        print("Using supabase-py REST (service role) as fallback...")
        fetched = _fetch_via_supabase_rest()
        used_rest = True
    payments, wallets = fetched

    print(f"Found {len(payments)} confirmed Solana payments with a signature.")
    print(f"Found {len(wallets)} verified Solana wallets.")

    if not payments:
        print("No confirmed Solana payments — nothing to check against chain.")
    else:
        signatures = [p["tx_signature"] for p in payments]
        print(f"Calling getSignatureStatuses in batches of {BATCH_SIZE}...")
        statuses = get_statuses(signatures)

        erred: list[tuple[dict, dict]] = []
        missing: list[dict] = []
        for payment, status in zip(payments, statuses):
            if status is None:
                missing.append(payment)
                continue
            err = status.get("err")
            if err is not None:
                erred.append((payment, err))

        print()
        print("=" * 72)
        print("PAYMENT AUDIT RESULTS")
        print("=" * 72)
        print(f"Total confirmed Solana payments checked : {len(payments)}")
        print(f"Ghost-confirmed (err non-null)          : {len(erred)}")
        print(f"Ghost-confirmed (not found on chain)    : {len(missing)}")
        print()

        if erred:
            print("--- Payments with on-chain err ---")
            for p, err in erred:
                print(
                    f"  payment_id={p['payment_id']} "
                    f"guild={p['guild_id']} "
                    f"wallet={p['recipient_wallet']} "
                    f"amount={p['amount_token']} SOL "
                    f"is_test={p['is_test']} "
                    f"confirmed_at={p['confirmed_at']} "
                    f"sig={p['tx_signature']} "
                    f"err={err}"
                )

        if missing:
            print("--- Payments with signature NOT FOUND on chain ---")
            for p in missing:
                print(
                    f"  payment_id={p['payment_id']} "
                    f"guild={p['guild_id']} "
                    f"wallet={p['recipient_wallet']} "
                    f"amount={p['amount_token']} SOL "
                    f"is_test={p['is_test']} "
                    f"confirmed_at={p['confirmed_at']} "
                    f"sig={p['tx_signature']}"
                )

    # ---- Wallet rent-floor flag ----
    # Any wallet whose verifying test payment was below 0.001 SOL was a ghost
    # verification. Cross-reference with payment_requests where is_test = true
    # AND recipient_wallet matches AND amount_token < 0.001.
    ghost_wallets: list[dict] = []
    if wallets:
        if used_rest:
            ghost_wallets = _fetch_ghost_wallets_rest(wallets)
        else:
            import psycopg2
            import psycopg2.extras
            conn_str = os.environ.get("SUPABASE_CONNECTION_STRING")
            conn = psycopg2.connect(conn_str)
            conn.set_session(readonly=True, autocommit=True)
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT ON (w.wallet_id)
                            w.wallet_id, w.guild_id, w.discord_user_id,
                            w.wallet_address, w.verified_at,
                            pr.amount_token AS test_amount_token,
                            pr.tx_signature AS test_tx_signature,
                            pr.payment_id AS test_payment_id
                        FROM wallet_registry w
                        JOIN payment_requests pr
                          ON pr.recipient_wallet = w.wallet_address
                         AND pr.guild_id = w.guild_id
                         AND pr.chain = 'solana'
                         AND pr.is_test = true
                        WHERE w.chain = 'solana'
                          AND w.verified_at IS NOT NULL
                          AND pr.amount_token < 0.001
                        ORDER BY w.wallet_id, pr.confirmed_at DESC NULLS LAST
                        """
                    )
                    ghost_wallets = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

        print()
        print("=" * 72)
        print("WALLET VERIFICATION AUDIT RESULTS")
        print("=" * 72)
        print(f"Total verified Solana wallets               : {len(wallets)}")
        print(f"Ghost-verified (test amount < 0.001 SOL)    : {len(ghost_wallets)}")
        print()
        if ghost_wallets:
            print("--- Ghost-verified wallets ---")
            for w in ghost_wallets:
                print(
                    f"  wallet_id={w['wallet_id']} "
                    f"guild={w['guild_id']} "
                    f"user={w['discord_user_id']} "
                    f"addr={w['wallet_address']} "
                    f"verified_at={w['verified_at']} "
                    f"test_amount={w['test_amount_token']} "
                    f"test_sig={w['test_tx_signature']}"
                )

    print()
    print("Audit complete. No database changes were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
