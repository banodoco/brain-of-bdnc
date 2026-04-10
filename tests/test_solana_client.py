"""Tests for SolanaClient.confirm_tx status.err checking (Bug A)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# A throwaway 64-byte base58 keypair for instantiating SolanaClient in tests.
# Not a real wallet — just valid-shaped bytes so Keypair.from_bytes() accepts it.
_DUMMY_SECRET_B58 = (
    "4h4q8HoeFGAqwQY1pBQ4X9Fb4TVWYR5Hv1fQha6GWwhey7gm3eLMeCzPK1mt6wtMoypYwRkEDKzGvGpEe77K33zC"
)

os.environ.setdefault("SOLANA_PRIVATE_KEY", _DUMMY_SECRET_B58)

from src.features.grants.solana_client import SolanaClient  # noqa: E402


TEST_SIG = "3N97kukwU7Jxbuw2vi285SXerNWpu5NsVB4mmhQDVJsBPQQggji43a1ZxrrqrkqRyG2cAXJP5wonbBNQCtmM2aw2"


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


@pytest.mark.anyio
async def test_confirm_tx_raises_on_status_err():
    """A finalized-but-errored tx must cause confirm_tx to raise."""
    errored_status = SimpleNamespace(
        err={"InsufficientFundsForRent": {"account_index": 1}}
    )
    fake = _FakeAsyncClient(status_value=[errored_status])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx(TEST_SIG)

    msg = str(excinfo.value)
    assert TEST_SIG in msg
    assert "InsufficientFundsForRent" in msg
    fake.confirm_transaction.assert_awaited_once()
    fake.get_signature_statuses.assert_awaited_once()


@pytest.mark.anyio
async def test_confirm_tx_raises_when_not_found():
    """If the signature status is missing, we treat it as a hard failure too."""
    fake = _FakeAsyncClient(status_value=[None])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await client.confirm_tx(TEST_SIG)

    assert "not found" in str(excinfo.value)


@pytest.mark.anyio
async def test_confirm_tx_returns_true_on_success():
    """A clean status (err is None) should return True."""
    ok_status = SimpleNamespace(err=None)
    fake = _FakeAsyncClient(status_value=[ok_status])

    client = _make_client()

    with patch(
        "src.features.grants.solana_client.AsyncClient",
        return_value=fake,
    ):
        result = await client.confirm_tx(TEST_SIG)

    assert result is True
