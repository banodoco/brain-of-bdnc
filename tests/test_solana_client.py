"""Tests for SolanaClient.confirm_tx status.err checking (Bug A)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from solders.compute_budget import set_compute_unit_price as real_set_compute_unit_price
from solders.hash import Hash

# A throwaway 64-byte base58 keypair for instantiating SolanaClient in tests.
# Not a real wallet — just valid-shaped bytes so Keypair.from_bytes() accepts it.
_DUMMY_SECRET_B58 = (
    "4h4q8HoeFGAqwQY1pBQ4X9Fb4TVWYR5Hv1fQha6GWwhey7gm3eLMeCzPK1mt6wtMoypYwRkEDKzGvGpEe77K33zC"
)

os.environ.setdefault("SOLANA_PRIVATE_KEY", _DUMMY_SECRET_B58)

from src.features.grants.solana_client import SendResult, SolanaClient  # noqa: E402


TEST_SIG = "3N97kukwU7Jxbuw2vi285SXerNWpu5NsVB4mmhQDVJsBPQQggji43a1ZxrrqrkqRyG2cAXJP5wonbBNQCtmM2aw2"
VALID_SOL_ADDRESS = "11111111111111111111111111111111"


class _FakeAsyncClient:
    """Minimal stand-in for solana.rpc.async_api.AsyncClient used in confirm_tx."""

    def __init__(self, *, status_value):
        self._status_value = status_value
        self.confirm_transaction = AsyncMock(return_value=None)
        self.get_signature_statuses = AsyncMock(
            return_value=SimpleNamespace(value=status_value)
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_client() -> SolanaClient:
    return SolanaClient(private_key=_DUMMY_SECRET_B58)


class _FakeSendAsyncClient:
    def __init__(self):
        self.get_balance = AsyncMock(return_value=SimpleNamespace(value=2_000_000_000))
        self.get_latest_blockhash = AsyncMock(
            return_value=SimpleNamespace(
                value=SimpleNamespace(
                    blockhash=Hash.default(),
                    last_valid_block_height=1_000_000,
                )
            )
        )
        self.send_transaction = AsyncMock(return_value=SimpleNamespace(value=TEST_SIG))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _extract_tx_confirm_decision(mock_logger, expected_decision):
    for call in mock_logger.info.call_args_list:
        if call.args and call.args[0] == "tx_confirm_decision":
            extra = call.kwargs.get("extra") or {}
            if extra.get("decision") == expected_decision:
                return extra
    return None


@pytest.mark.anyio
async def test_dynamic_priority_fee_uses_75th_percentile_from_sdk():
    # Values chosen above the 500_000 floor so the 75th percentile isn't clamped
    # and we actually exercise the percentile calculation.
    client = _make_client()
    fake_rpc = SimpleNamespace(
        get_recent_prioritization_fees=AsyncMock(
            return_value=SimpleNamespace(
                value=[
                    SimpleNamespace(prioritization_fee=0),
                    SimpleNamespace(prioritization_fee=1_500_000),
                    SimpleNamespace(prioritization_fee=2_000_000),
                    SimpleNamespace(prioritization_fee=3_000_000),
                    SimpleNamespace(prioritization_fee=4_000_000),
                ]
            )
        )
    )

    fee = await client._get_dynamic_priority_fee(fake_rpc)

    assert fee == 3_000_000


@pytest.mark.anyio
async def test_dynamic_priority_fee_clamps_to_floor():
    client = _make_client()
    fake_rpc = SimpleNamespace(
        get_recent_prioritization_fees=AsyncMock(
            return_value=SimpleNamespace(
                value=[
                    SimpleNamespace(prioritization_fee=1),
                    SimpleNamespace(prioritization_fee=2),
                    SimpleNamespace(prioritization_fee=3),
                ]
            )
        )
    )

    fee = await client._get_dynamic_priority_fee(fake_rpc)

    assert fee == client.priority_fee_micro_lamports


@pytest.mark.anyio
async def test_dynamic_priority_fee_clamps_to_ceiling(monkeypatch):
    # Override both floor and ceiling for this test so we have a window where the
    # percentile (750_000) exceeds the ceiling (500_000) and gets clamped down.
    monkeypatch.setenv("SOLANA_PRIORITY_FEE_MICRO_LAMPORTS", "50000")
    monkeypatch.setenv("SOLANA_PRIORITY_FEE_CEILING_MICRO_LAMPORTS", "500000")
    client = _make_client()
    fake_rpc = SimpleNamespace(
        get_recent_prioritization_fees=AsyncMock(
            return_value=SimpleNamespace(
                value=[
                    SimpleNamespace(prioritization_fee=100_000),
                    SimpleNamespace(prioritization_fee=200_000),
                    SimpleNamespace(prioritization_fee=500_000),
                    SimpleNamespace(prioritization_fee=800_000),
                ]
            )
        )
    )

    fee = await client._get_dynamic_priority_fee(fake_rpc)

    assert fee == 500_000


@pytest.mark.anyio
async def test_dynamic_priority_fee_falls_back_to_static_floor_on_error(caplog):
    client = _make_client()
    fake_rpc = SimpleNamespace(
        get_recent_prioritization_fees=AsyncMock(side_effect=RuntimeError("rpc down"))
    )

    fee = await client._get_dynamic_priority_fee(fake_rpc)

    assert fee == client.priority_fee_micro_lamports
    assert "Falling back to static Solana priority fee floor" in caplog.text


@pytest.mark.anyio
async def test_send_sol_uses_dynamic_priority_fee():
    client = _make_client()
    fake_rpc = _FakeSendAsyncClient()
    client._get_dynamic_priority_fee = AsyncMock(return_value=54_321)

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake_rpc,
    ), patch(
        "src.features.grants.solana_client.set_compute_unit_price",
        side_effect=real_set_compute_unit_price,
    ) as mock_set_compute_unit_price:
        # send_sol now returns a SendResult rather than a bare signature string,
        # because the rebroadcast-during-confirmation fix needs the signed tx
        # and the blockhash's last_valid_block_height to drive the loop. The
        # signature field on the returned dataclass is the thing older tests
        # used to assert against directly.
        result = await client.send_sol(VALID_SOL_ADDRESS, 0.1)

    assert isinstance(result, SendResult)
    assert result.signature == TEST_SIG
    assert result.last_valid_block_height == 1_000_000
    client._get_dynamic_priority_fee.assert_awaited_once_with(fake_rpc)
    mock_set_compute_unit_price.assert_called_once_with(54_321)


@pytest.mark.anyio
async def test_confirm_tx_raises_on_status_err():
    """A finalized-but-errored tx must cause confirm_tx to raise."""
    errored_status = SimpleNamespace(
        err={"InsufficientFundsForRent": {"account_index": 1}},
        slot=123,
        confirmation_status="finalized",
    )
    fake = _FakeAsyncClient(status_value=[errored_status])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ), patch("src.features.grants.solana_client.logger") as mock_logger:
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx(TEST_SIG)

    msg = str(excinfo.value)
    assert TEST_SIG in msg
    assert "InsufficientFundsForRent" in msg
    fake.confirm_transaction.assert_awaited_once()
    fake.get_signature_statuses.assert_awaited_once()
    extra = _extract_tx_confirm_decision(mock_logger, "errored")
    assert extra == {
        "event": "tx_confirm_decision",
        "signature": TEST_SIG,
        "err": "{'InsufficientFundsForRent': {'account_index': 1}}",
        "slot": 123,
        "confirmation_status": "finalized",
        "decision": "errored",
    }


@pytest.mark.anyio
async def test_confirm_tx_raises_when_not_found():
    """If the signature status is missing, we treat it as a hard failure too."""
    fake = _FakeAsyncClient(status_value=[None])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ), patch("src.features.grants.solana_client.logger") as mock_logger:
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx(TEST_SIG)

    assert "not found" in str(excinfo.value)
    extra = _extract_tx_confirm_decision(mock_logger, "not_found")
    assert extra == {
        "event": "tx_confirm_decision",
        "signature": TEST_SIG,
        "err": None,
        "slot": None,
        "confirmation_status": None,
        "decision": "not_found",
    }


@pytest.mark.anyio
async def test_confirm_tx_returns_true_on_success():
    """A clean status (err is None) should return True."""
    ok_status = SimpleNamespace(err=None, slot=456, confirmation_status="finalized")
    fake = _FakeAsyncClient(status_value=[ok_status])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ), patch("src.features.grants.solana_client.logger") as mock_logger:
        result = await client.confirm_tx(TEST_SIG)

    assert result is True
    extra = _extract_tx_confirm_decision(mock_logger, "confirmed")
    assert extra == {
        "event": "tx_confirm_decision",
        "signature": TEST_SIG,
        "err": None,
        "slot": 456,
        "confirmation_status": "finalized",
        "decision": "confirmed",
    }


# -------------------------------------------------------------------------
# confirm_tx_with_rebroadcast tests
#
# These cover the production-incident fix: a 0.002 SOL payment was accepted
# by Helius but never landed on chain, because the RPC did not gossip the tx
# to a leader in time. The fix is to rebroadcast the same signed tx every
# few seconds during the confirmation window so transient single-broadcast
# drops self-heal. All four tests below lock that behavior in.
# -------------------------------------------------------------------------


class _RebroadcastFakeAsyncClient:
    """Stand-in for AsyncClient exposing the surface confirm_tx_with_rebroadcast uses."""

    def __init__(
        self,
        *,
        status_sequence,
        block_height_sequence=None,
        send_side_effect=None,
    ):
        self._status_sequence = list(status_sequence)
        self._block_height_sequence = list(block_height_sequence or [])
        self.send_transaction = AsyncMock(
            return_value=SimpleNamespace(value=TEST_SIG),
            side_effect=send_side_effect,
        )
        self.get_signature_statuses = AsyncMock(side_effect=self._next_status)
        self.get_block_height = AsyncMock(side_effect=self._next_block_height)

    async def _next_status(self, *_args, **_kwargs):
        if self._status_sequence:
            value = self._status_sequence.pop(0)
        else:
            value = [None]
        return SimpleNamespace(value=value)

    async def _next_block_height(self, *_args, **_kwargs):
        if self._block_height_sequence:
            value = self._block_height_sequence.pop(0)
        else:
            value = 0
        return SimpleNamespace(value=value)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_send_result() -> SendResult:
    # ``signed_tx`` is only ever passed straight through to send_transaction,
    # which is mocked — so any sentinel object is fine here.
    return SendResult(
        signature=TEST_SIG,
        signed_tx=object(),  # type: ignore[arg-type]
        last_valid_block_height=1_000_000,
    )


@pytest.mark.anyio
async def test_confirm_tx_with_rebroadcast_happy_path(monkeypatch):
    """Confirms on the very first poll → one rebroadcast, returns True."""
    confirmed_status = SimpleNamespace(err=None, confirmation_status="confirmed")
    fake = _RebroadcastFakeAsyncClient(status_sequence=[[confirmed_status]])

    # No real sleeps — keep the test fast.
    monkeypatch.setattr(
        "src.features.grants.solana_client.asyncio.sleep",
        AsyncMock(return_value=None),
    )

    client = _make_client()
    send_result = _make_send_result()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        result = await client.confirm_tx_with_rebroadcast(
            send_result,
            rebroadcast_interval=0.0,
            max_wait_seconds=5.0,
        )

    assert result is True
    assert fake.send_transaction.await_count == 1
    assert fake.get_signature_statuses.await_count == 1


@pytest.mark.anyio
async def test_confirm_tx_with_rebroadcast_recovers_dropped_first_send(monkeypatch):
    """Simulates the production incident: first sends don't land, later rebroadcasts do."""
    confirmed_status = SimpleNamespace(err=None, confirmation_status="confirmed")
    # Three "not found" polls, then the signature lands.
    status_sequence = [
        [None],
        [None],
        [None],
        [confirmed_status],
    ]
    fake = _RebroadcastFakeAsyncClient(status_sequence=status_sequence)

    monkeypatch.setattr(
        "src.features.grants.solana_client.asyncio.sleep",
        AsyncMock(return_value=None),
    )

    client = _make_client()
    send_result = _make_send_result()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        result = await client.confirm_tx_with_rebroadcast(
            send_result,
            rebroadcast_interval=0.0,
            max_wait_seconds=30.0,
        )

    assert result is True
    # One rebroadcast per loop iteration; we needed 4 iterations to see confirmed.
    assert fake.send_transaction.await_count >= 2
    assert fake.send_transaction.await_count == 4
    assert fake.get_signature_statuses.await_count == 4


@pytest.mark.anyio
async def test_confirm_tx_with_rebroadcast_preserves_status_err_check(monkeypatch):
    """P0 regression guard: a non-null status.err must raise, never swallow."""
    errored_status = SimpleNamespace(
        err={"InsufficientFundsForRent": {"account_index": 1}},
        slot=789,
        confirmation_status="finalized",
    )
    fake = _RebroadcastFakeAsyncClient(status_sequence=[[errored_status]])

    monkeypatch.setattr(
        "src.features.grants.solana_client.asyncio.sleep",
        AsyncMock(return_value=None),
    )

    client = _make_client()
    send_result = _make_send_result()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx_with_rebroadcast(
                send_result,
                rebroadcast_interval=0.0,
                max_wait_seconds=5.0,
            )

    msg = str(excinfo.value)
    assert TEST_SIG in msg
    assert "InsufficientFundsForRent" in msg
    assert "failed on chain" in msg


@pytest.mark.anyio
async def test_confirm_tx_with_rebroadcast_blockhash_expired(monkeypatch):
    """When current block height passes last_valid_block_height → timeout RuntimeError."""
    # Signature never lands; current block height jumps past the expiry.
    status_sequence = [[None]]
    # First poll's block-height lookup returns a height past expiry → should bail.
    block_height_sequence = [1_000_001]
    fake = _RebroadcastFakeAsyncClient(
        status_sequence=status_sequence,
        block_height_sequence=block_height_sequence,
    )

    monkeypatch.setattr(
        "src.features.grants.solana_client.asyncio.sleep",
        AsyncMock(return_value=None),
    )

    client = _make_client()
    send_result = _make_send_result()  # last_valid_block_height = 1_000_000

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx_with_rebroadcast(
                send_result,
                rebroadcast_interval=0.0,
                max_wait_seconds=30.0,
            )

    msg = str(excinfo.value)
    assert TEST_SIG in msg
    assert "blockhash expired" in msg
    assert "1000001" in msg
    assert "1000000" in msg
