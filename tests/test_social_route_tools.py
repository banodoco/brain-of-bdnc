import pytest

from src.features.admin_chat import agent as admin_agent
from src.features.admin_chat import tools as admin_tools


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.limit_value = None

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append(lambda row, k=key, v=value: row.get(k) == v)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        table = self.supabase.tables.setdefault(self.table_name, [])

        if self.operation == "insert":
            row = dict(self.payload)
            row.setdefault("id", "route-{0}".format(len(table) + 1))
            table.append(row)
            return FakeResult([dict(row)])

        filtered = [row for row in table if all(check(row) for check in self.filters)]

        if self.operation == "update":
            for row in filtered:
                row.update(dict(self.payload))
            return FakeResult([dict(row) for row in filtered])

        if self.operation == "delete":
            deleted = [dict(row) for row in filtered]
            self.supabase.tables[self.table_name] = [row for row in table if row not in filtered]
            return FakeResult(deleted)

        if self.limit_value is not None:
            filtered = filtered[:self.limit_value]
        return FakeResult([dict(row) for row in filtered])


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def table(self, name):
        return FakeQuery(self, name)


class FakePaymentDB:
    def __init__(self):
        self.payment_routes = [
            {
                "id": "pay-default",
                "guild_id": 1,
                "channel_id": None,
                "producer": "grants",
                "enabled": True,
                "route_config": {"use_source_thread": True},
            }
        ]
        self.wallets = [
            {
                "wallet_id": "wallet-1",
                "guild_id": 1,
                "discord_user_id": 42,
                "chain": "solana",
                "wallet_address": "ABCDE12345FGHIJ67890",
                "verified_at": None,
            }
        ]
        self.payments = [
            {
                "payment_id": "pay-1",
                "guild_id": 1,
                "producer": "grants",
                "producer_ref": "thread-1",
                "wallet_id": "wallet-1",
                "recipient_discord_id": 42,
                "recipient_wallet": "ABCDE12345FGHIJ67890",
                "chain": "solana",
                "provider": "solana_native",
                "is_test": False,
                "route_key": "pay-default",
                "confirm_channel_id": 11,
                "notify_channel_id": 11,
                "amount_token": 1.25,
                "amount_usd": 200.0,
                "token_price_usd": 160.0,
                "status": "failed",
                "send_phase": "pre_submit",
                "tx_signature": None,
                "attempt_count": 1,
                "last_error": "rpc reset",
            }
        ]

    def list_payment_routes(self, guild_id, producer=None, channel_id=None, enabled=None, limit=100):
        rows = [row for row in self.payment_routes if row["guild_id"] == guild_id]
        if producer:
            rows = [row for row in rows if row["producer"] == producer]
        if channel_id is not None:
            rows = [row for row in rows if row["channel_id"] == channel_id]
        if enabled is not None:
            rows = [row for row in rows if bool(row["enabled"]) is enabled]
        return rows[:limit]

    def create_payment_route(self, data, guild_id=None):
        row = dict(data)
        row.setdefault("id", f"pay-route-{len(self.payment_routes) + 1}")
        row["guild_id"] = guild_id or row["guild_id"]
        self.payment_routes.append(row)
        return dict(row)

    def update_payment_route(self, route_id, data, guild_id=None):
        for row in self.payment_routes:
            if row["id"] == route_id and row["guild_id"] == guild_id:
                row.update(dict(data))
                return dict(row)
        return None

    def delete_payment_route(self, route_id, guild_id=None):
        for idx, row in enumerate(self.payment_routes):
            if row["id"] == route_id and row["guild_id"] == guild_id:
                return self.payment_routes.pop(idx)
        return None

    def list_wallets(self, guild_id, chain=None, discord_user_id=None, verified=None, limit=100):
        rows = [row for row in self.wallets if row["guild_id"] == guild_id]
        if chain:
            rows = [row for row in rows if row["chain"] == chain]
        if discord_user_id is not None:
            rows = [row for row in rows if row["discord_user_id"] == discord_user_id]
        if verified is True:
            rows = [row for row in rows if row.get("verified_at") is not None]
        if verified is False:
            rows = [row for row in rows if row.get("verified_at") is None]
        return rows[:limit]

    def list_payment_requests(
        self,
        guild_id,
        status=None,
        producer=None,
        recipient_discord_id=None,
        wallet_id=None,
        is_test=None,
        route_key=None,
        limit=100,
    ):
        rows = [row for row in self.payments if row["guild_id"] == guild_id]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if producer:
            rows = [row for row in rows if row["producer"] == producer]
        if recipient_discord_id is not None:
            rows = [row for row in rows if row["recipient_discord_id"] == recipient_discord_id]
        if wallet_id is not None:
            rows = [row for row in rows if row["wallet_id"] == wallet_id]
        if is_test is not None:
            rows = [row for row in rows if row["is_test"] == is_test]
        if route_key is not None:
            rows = [row for row in rows if row["route_key"] == route_key]
        return rows[:limit]

    def get_payment_request(self, payment_id, guild_id=None):
        for row in self.payments:
            if row["payment_id"] == payment_id and (guild_id is None or row["guild_id"] == guild_id):
                return row
        return None

    def requeue_payment(self, payment_id, guild_id=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] != "failed":
            return False
        row["status"] = "queued"
        row["last_error"] = None
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row:
            return False
        row["status"] = "manual_hold"
        row["last_error"] = reason
        return True

    def release_payment_hold(self, payment_id, new_status, guild_id=None, reason=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] != "manual_hold":
            return False
        if new_status not in {"failed", "manual_hold"}:
            return False
        row["status"] = new_status
        if reason is not None:
            row["last_error"] = reason
        return True

    def cancel_payment(self, payment_id, guild_id=None, reason=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] not in {"pending_confirmation", "queued", "failed"}:
            return False
        row["status"] = "cancelled"
        row["last_error"] = reason
        return True


@pytest.fixture
def fake_supabase(monkeypatch):
    supabase = FakeSupabase(
        {
            "social_channel_routes": [
                {
                    "id": "route-default",
                    "guild_id": 1,
                    "channel_id": None,
                    "platform": "twitter",
                    "enabled": True,
                    "route_config": {"account": "main"},
                }
            ]
        }
    )
    monkeypatch.setattr(admin_tools, "_get_supabase", lambda: supabase)
    monkeypatch.setattr(admin_tools, "_resolve_guild_id", lambda params=None: 1)
    return supabase


def test_route_tools_are_admin_only():
    admin_tool_names = {tool["name"] for tool in admin_tools.get_tools_for_role(True)}
    member_tool_names = {tool["name"] for tool in admin_tools.get_tools_for_role(False)}

    assert {
        "list_social_routes",
        "create_social_route",
        "update_social_route",
        "delete_social_route",
        "list_payment_routes",
        "create_payment_route",
        "update_payment_route",
        "delete_payment_route",
        "list_wallets",
        "list_payments",
        "get_payment_status",
        "retry_payment",
        "hold_payment",
        "release_payment",
        "cancel_payment",
        "initiate_payment",
    }.issubset(admin_tool_names)
    assert "list_social_routes" not in member_tool_names
    assert "create_social_route" not in member_tool_names
    assert "list_payments" not in member_tool_names
    assert "payment_requests" not in admin_tools.QUERYABLE_TABLES
    assert "payment_channel_routes" not in admin_tools.QUERYABLE_TABLES
    assert "wallet_registry" not in admin_tools.QUERYABLE_TABLES


@pytest.mark.anyio
async def test_social_route_tool_crud_flow(fake_supabase):
    listed = await admin_tools.execute_list_social_routes({"platform": "x"})
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["data"][0]["id"] == "route-default"

    created = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "channel_id": "123",
            "enabled": True,
            "route_config": {"account": "alt"},
        }
    )
    assert created["success"] is True
    assert created["route"]["channel_id"] == 123
    assert created["route"]["route_config"] == {"account": "alt"}

    updated = await admin_tools.execute_update_social_route(
        {
            "route_id": created["route"]["id"],
            "channel_id": "",
            "enabled": False,
            "route_config": {"account": "archive"},
        }
    )
    assert updated["success"] is True
    assert updated["route"]["channel_id"] is None
    assert updated["route"]["enabled"] is False
    assert updated["route"]["route_config"] == {"account": "archive"}

    deleted = await admin_tools.execute_delete_social_route(
        {"route_id": created["route"]["id"]}
    )
    assert deleted["success"] is True
    assert deleted["route"]["id"] == created["route"]["id"]

    remaining_ids = {row["id"] for row in fake_supabase.tables["social_channel_routes"]}
    assert remaining_ids == {"route-default"}


@pytest.mark.anyio
async def test_create_social_route_validates_route_config(fake_supabase):
    result = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "route_config": "not-a-dict",
        }
    )

    assert result["success"] is False
    assert "route_config must be a JSON object" in result["error"]


@pytest.mark.anyio
async def test_create_social_route_requires_account_for_twitter(fake_supabase):
    result = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "route_config": {},
        }
    )

    assert result["success"] is False
    assert "route_config.account" in result["error"]
    assert "create_payment_route" in result["error"]


@pytest.mark.anyio
async def test_update_social_route_requires_account_for_twitter(fake_supabase):
    result = await admin_tools.execute_update_social_route(
        {
            "route_id": "route-default",
            "route_config": {},
        }
    )

    assert result["success"] is False
    assert "route_config.account" in result["error"]


def test_admin_prompt_steers_payment_requests_to_payment_tools():
    assert "Payment routing, payout confirmations, test payments, wallet collection" in admin_agent.SYSTEM_PROMPT


@pytest.mark.anyio
async def test_payment_route_wallet_and_control_tools_are_redacted():
    db_handler = FakePaymentDB()

    listed_routes = await admin_tools.execute_list_payment_routes(db_handler, {"producer": "grants"})
    assert listed_routes["success"] is True
    assert listed_routes["data"][0]["id"] == "pay-default"

    created_route = await admin_tools.execute_create_payment_route(
        db_handler,
        {
            "producer": "grants",
            "channel_id": "123",
            "route_config": {"confirm_channel_id": 500},
        },
    )
    assert created_route["success"] is True
    assert created_route["route"]["channel_id"] == 123

    wallets = await admin_tools.execute_list_wallets(
        db_handler,
        {"chain": "solana", "verified": False},
    )
    assert wallets["success"] is True
    assert wallets["data"][0]["wallet_address"] == "ABCD...7890"

    payments = await admin_tools.execute_list_payments(
        db_handler,
        {"status": "failed", "producer": "grants"},
    )
    assert payments["success"] is True
    assert payments["data"][0]["recipient_wallet"] == "ABCD...7890"

    status = await admin_tools.execute_get_payment_status(db_handler, {"payment_id": "pay-1"})
    assert status["success"] is True
    assert status["payment"]["recipient_wallet"] == "ABCD...7890"

    retried = await admin_tools.execute_retry_payment(db_handler, {"payment_id": "pay-1"})
    assert retried["success"] is True
    assert retried["payment"]["status"] == "queued"

    held = await admin_tools.execute_hold_payment(
        db_handler,
        {"payment_id": "pay-1", "reason": "needs review"},
    )
    assert held["success"] is True
    assert held["payment"]["status"] == "manual_hold"

    released = await admin_tools.execute_release_payment(
        db_handler,
        {"payment_id": "pay-1", "new_status": "failed", "reason": "chain rejected"},
    )
    assert released["success"] is True
    assert released["payment"]["status"] == "failed"

    disallowed_release = await admin_tools.execute_release_payment(
        db_handler,
        {"payment_id": "pay-1", "new_status": "confirmed"},
    )
    assert disallowed_release["success"] is False

    cancelled = await admin_tools.execute_cancel_payment(
        db_handler,
        {"payment_id": "pay-1", "reason": "operator cancelled"},
    )
    assert cancelled["success"] is True
    assert cancelled["payment"]["status"] == "cancelled"


def test_agent_prompt_mentions_payment_tools():
    assert "initiate_payment" in admin_agent.SYSTEM_PROMPT
    assert "list_payment_routes" in admin_agent.SYSTEM_PROMPT
    assert "list_payments" in admin_agent.SYSTEM_PROMPT
    assert "release_payment" in admin_agent.SYSTEM_PROMPT
