"""Solana wallet and SOL transfer for micro-grants."""

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass

import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.rpc.requests import GetRecentPrioritizationFees
from solders.rpc.responses import GetRecentPrioritizationFeesResp
from solders.system_program import transfer, TransferParams
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.message import MessageV0

logger = logging.getLogger('DiscordBot')

# Base58-encoded Solana address: 32-44 chars
SOLANA_ADDRESS_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def is_valid_solana_address(address: str) -> bool:
    """Check if a string looks like a valid Solana address."""
    if not SOLANA_ADDRESS_RE.match(address):
        return False
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False


@dataclass
class SendResult:
    """Everything a caller needs to rebroadcast and confirm a signed transaction.

    ``signed_tx`` is the fully-signed ``VersionedTransaction`` object so the
    confirmation loop can re-submit the same signed payload to the RPC as many
    times as needed during the blockhash validity window. Re-sending an already-
    signed tx is a no-op on chain once it has been included, so this is the
    standard Solana production pattern for surviving transient single-broadcast
    drops (Helius accept/gossip races, skipped leader slots, packet loss).
    """

    signature: str
    signed_tx: VersionedTransaction
    last_valid_block_height: int


class SolanaClient:
    """Handles SOL transfers from the bot wallet."""

    def __init__(self, private_key: str | None = None):
        private_key = private_key or os.getenv('SOLANA_PRIVATE_KEY')
        if not private_key:
            raise ValueError("No Solana private key provided and SOLANA_PRIVATE_KEY env var not set")

        key_bytes = base58.b58decode(private_key)
        self.keypair = Keypair.from_bytes(key_bytes)
        self.rpc_url = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
        # Static priority-fee floor. 10_000 micro-lamports/CU is a reasonable mainnet
        # floor: cheap (simple transfers burn ~200 CU → ~2_000_000 micro-lamports =
        # 0.000002 SOL extra fee) but high enough to be included under normal congestion.
        self.priority_fee_micro_lamports = int(
            os.getenv('SOLANA_PRIORITY_FEE_MICRO_LAMPORTS', '10000')
        )
        self.priority_fee_ceiling_micro_lamports = int(
            os.getenv('SOLANA_PRIORITY_FEE_CEILING_MICRO_LAMPORTS', '1000000')
        )

    @property
    def public_key(self) -> Pubkey:
        return self.keypair.pubkey()

    async def get_balance_sol(self) -> float:
        """Get bot wallet balance in SOL."""
        async with AsyncClient(self.rpc_url) as client:
            resp = await client.get_balance(self.public_key, commitment=Confirmed)
            lamports = resp.value
            return lamports / 1_000_000_000

    async def _get_dynamic_priority_fee(self, client) -> int:
        """Return a congestion-aware priority fee, clamped to configured bounds."""
        try:
            if hasattr(client, 'get_recent_prioritization_fees'):
                response = await client.get_recent_prioritization_fees()
            else:
                response = await client._provider.make_request(
                    GetRecentPrioritizationFees(),
                    GetRecentPrioritizationFeesResp,
                )

            entries = getattr(response, 'value', None) or []
            non_zero_fees = sorted(
                int(getattr(entry, 'prioritization_fee', 0) or 0)
                for entry in entries
                if int(getattr(entry, 'prioritization_fee', 0) or 0) > 0
            )
            if not non_zero_fees:
                raise ValueError("no non-zero prioritization fees returned")

            index = max(math.ceil(len(non_zero_fees) * 0.75) - 1, 0)
            percentile_fee = non_zero_fees[index]
            return max(
                self.priority_fee_micro_lamports,
                min(percentile_fee, self.priority_fee_ceiling_micro_lamports),
            )
        except Exception as exc:
            logger.warning(
                "Falling back to static Solana priority fee floor %s: %s",
                self.priority_fee_micro_lamports,
                exc,
            )
            return self.priority_fee_micro_lamports

    def _log_tx_confirm_decision(self, signature: str, *, decision: str, err, status) -> None:
        logger.info(
            'tx_confirm_decision',
            extra={
                'event': 'tx_confirm_decision',
                'signature': signature,
                'err': repr(err) if err is not None else None,
                'slot': getattr(status, 'slot', None) if status is not None else None,
                'confirmation_status': getattr(status, 'confirmation_status', None) if status is not None else None,
                'decision': decision,
            },
        )

    async def send_sol(self, recipient_address: str, amount_sol: float) -> SendResult:
        """Send SOL to a recipient. Returns a SendResult with the signed tx.

        The signed ``VersionedTransaction`` and the blockhash's
        ``last_valid_block_height`` are returned so the caller can rebroadcast
        the exact same signed payload during the confirmation window. This is
        the standard Solana production pattern for surviving transient
        single-broadcast drops — re-sending an already-included signature is a
        harmless no-op on chain.

        Raises RuntimeError on failure. Retries blockhash errors up to 3 times.
        """
        recipient = Pubkey.from_string(recipient_address)
        lamports = int(amount_sol * 1_000_000_000)

        async with AsyncClient(self.rpc_url) as client:
            # Check balance
            balance_resp = await client.get_balance(self.public_key, commitment=Confirmed)
            balance_lamports = balance_resp.value
            if balance_lamports < lamports + 10_000:  # 10k lamports for fees
                raise RuntimeError(
                    f"Insufficient balance: {balance_lamports / 1e9:.4f} SOL, "
                    f"need {amount_sol:.4f} SOL + fees"
                )

            transfer_ix = transfer(TransferParams(
                from_pubkey=self.public_key,
                to_pubkey=recipient,
                lamports=lamports,
            ))

            # Prepend ComputeBudget instructions so the tx isn't dropped under congestion.
            # Budget must cover: set_compute_unit_limit (~150 CU) + set_compute_unit_price
            # (~150 CU) + system-program transfer (~150 CU) ≈ 450 CU minimum. 1_000 CU
            # gives comfortable headroom without materially affecting fees (fee scales
            # with actually-consumed CU × price, not the requested limit).
            compute_unit_limit = 1_000
            compute_unit_price = await self._get_dynamic_priority_fee(client)
            set_cu_limit_ix = set_compute_unit_limit(compute_unit_limit)
            set_cu_price_ix = set_compute_unit_price(compute_unit_price)
            instructions = [set_cu_limit_ix, set_cu_price_ix, transfer_ix]
            logger.info(
                f"SOL transfer priority fee: cu_limit={compute_unit_limit} "
                f"cu_price={compute_unit_price} micro-lamports/CU "
                f"(floor={self.priority_fee_micro_lamports}, ceiling={self.priority_fee_ceiling_micro_lamports})"
            )

            # Retry up to 3 times — blockhashes can expire under network load
            last_err = None
            for attempt in range(3):
                blockhash_resp = await client.get_latest_blockhash(commitment=Confirmed)
                blockhash = blockhash_resp.value.blockhash
                last_valid_block_height = int(
                    getattr(blockhash_resp.value, 'last_valid_block_height', 0) or 0
                )

                msg = MessageV0.try_compile(
                    payer=self.public_key,
                    instructions=instructions,
                    address_lookup_table_accounts=[],
                    recent_blockhash=blockhash,
                )
                tx = VersionedTransaction(msg, [self.keypair])

                try:
                    # Skip preflight — the simulation can fail with stale blockhash
                    # on congested public RPCs even when the actual send would succeed
                    opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                    result = await client.send_transaction(tx, opts=opts)
                    signature = str(result.value)
                    logger.info(f"SOL transfer sent: {signature} ({amount_sol:.4f} SOL to {recipient_address})")
                    return SendResult(
                        signature=signature,
                        signed_tx=tx,
                        last_valid_block_height=last_valid_block_height,
                    )
                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    retryable = 'Blockhash not found' in err_str or '429' in err_str
                    if retryable and attempt < 2:
                        delay = 2 ** attempt  # 1s, 2s
                        logger.warning(f"Send failed (attempt {attempt + 1}/3): {err_str[:100]}, retrying in {delay}s...")
                        await asyncio.sleep(delay)
                        continue
                    raise

            raise RuntimeError(f"Transaction failed after 3 attempts: {last_err}")

    async def confirm_tx(self, signature: str) -> bool:
        """Wait for a transaction to be confirmed and verify it did not error on chain.

        ``AsyncClient.confirm_transaction`` only waits for the signature to reach
        the requested commitment level — it does NOT inspect ``status.err``. A
        finalized-but-errored transaction (e.g. ``InsufficientFundsForRent``)
        trivially satisfies that call, so we must follow up with
        ``get_signature_statuses`` and raise on a non-null ``err``.

        Returns True if the transaction was confirmed and had no error.
        Raises RuntimeError if the transaction errored on chain or the status
        lookup came back empty/not-found.
        """
        async with AsyncClient(self.rpc_url) as client:
            sig = Signature.from_string(signature)
            await client.confirm_transaction(sig, commitment=Confirmed)

            # Re-fetch the signature status and inspect ``err`` explicitly.
            # This mirrors ``check_tx_status`` below but inlined so the
            # confirm path is a single round-trip after the wait.
            resp = await client.get_signature_statuses(
                [sig], search_transaction_history=True
            )
            statuses = resp.value
            if not statuses or statuses[0] is None:
                self._log_tx_confirm_decision(
                    signature,
                    decision='not_found',
                    err=None,
                    status=None,
                )
                raise RuntimeError(
                    f"SOL transfer {signature} not found after confirmation wait "
                    "(signature did not land on chain)"
                )
            status = statuses[0]
            if status.err is not None:
                self._log_tx_confirm_decision(
                    signature,
                    decision='errored',
                    err=status.err,
                    status=status,
                )
                raise RuntimeError(
                    f"SOL transfer {signature} failed on chain: err={status.err!r}"
                )

            self._log_tx_confirm_decision(
                signature,
                decision='confirmed',
                err=None,
                status=status,
            )
            logger.info(f"SOL transfer confirmed: {signature}")
            return True

    async def confirm_tx_with_rebroadcast(
        self,
        send_result: "SendResult",
        *,
        rebroadcast_interval: float = 2.0,
        max_wait_seconds: float = 60.0,
    ) -> bool:
        """Confirm a signed tx by repeatedly rebroadcasting + polling status.

        This is the standard Solana production pattern. Each iteration:

        1. Re-sends the exact signed ``VersionedTransaction`` via
           ``send_transaction(skip_preflight=True)``. Once the tx has been
           included on chain the node no-ops on duplicate sends, so this is
           safe to repeat as many times as the confirmation window allows.
        2. Polls ``get_signature_statuses([sig], search_transaction_history=True)``:

           - If ``status.err`` is non-null → raises ``RuntimeError`` (P0 fix,
             must never regress: a finalized-but-errored tx must surface as an
             error, not a silent success).
           - If ``confirmationStatus`` in ``('confirmed', 'finalized')`` →
             returns True.
        3. Sleeps ``rebroadcast_interval`` seconds, then loops.

        Raises ``RuntimeError`` when the wall-clock elapsed exceeds
        ``max_wait_seconds`` or the current block height has passed
        ``last_valid_block_height`` (blockhash expired → the tx can no longer
        land, retry is pointless).
        """
        signature = send_result.signature
        signed_tx = send_result.signed_tx
        last_valid_block_height = send_result.last_valid_block_height
        sig = Signature.from_string(signature)

        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
        start = time.monotonic()
        broadcast_count = 0

        async with AsyncClient(self.rpc_url) as client:
            while True:
                # (1) Rebroadcast the signed tx. Failures here are non-fatal:
                # the next poll may still reveal the tx landed from a previous
                # send. Only truly catastrophic conditions (expired blockhash,
                # timeout) end the loop.
                try:
                    await client.send_transaction(signed_tx, opts=opts)
                    broadcast_count += 1
                except Exception as exc:
                    logger.debug(
                        f"SOL rebroadcast attempt failed for {signature}: {exc}"
                    )

                # (2) Poll signature status.
                resp = await client.get_signature_statuses(
                    [sig], search_transaction_history=True
                )
                statuses = getattr(resp, 'value', None) or []
                status = statuses[0] if statuses else None

                if status is not None:
                    if status.err is not None:
                        # P0: finalized-but-errored must raise, not swallow.
                        self._log_tx_confirm_decision(
                            signature,
                            decision='errored',
                            err=status.err,
                            status=status,
                        )
                        raise RuntimeError(
                            f"SOL transfer {signature} failed on chain: "
                            f"err={status.err!r}"
                        )
                    confirmation_status = getattr(
                        status, 'confirmation_status', None
                    )
                    # ``confirmation_status`` may be a string or an enum with a
                    # ``.name`` attribute depending on the SDK version.
                    cs_name = (
                        confirmation_status
                        if isinstance(confirmation_status, str)
                        else getattr(confirmation_status, 'name', '')
                    )
                    cs_lower = (cs_name or '').lower()
                    if cs_lower in ('confirmed', 'finalized'):
                        self._log_tx_confirm_decision(
                            signature,
                            decision='confirmed',
                            err=None,
                            status=status,
                        )
                        logger.info(
                            f"SOL transfer confirmed: {signature} "
                            f"(rebroadcasts={broadcast_count}, "
                            f"status={cs_lower})"
                        )
                        return True

                # (3) Check blockhash expiry. If the blockhash is no longer
                # valid the tx can never land regardless of how many times we
                # rebroadcast — bail out now.
                if last_valid_block_height > 0:
                    try:
                        bh_resp = await client.get_block_height(
                            commitment=Confirmed
                        )
                        current_block_height = int(
                            getattr(bh_resp, 'value', 0) or 0
                        )
                    except Exception as exc:
                        logger.debug(
                            f"get_block_height failed during confirm loop: {exc}"
                        )
                        current_block_height = 0
                    if (
                        current_block_height
                        and current_block_height > last_valid_block_height
                    ):
                        raise RuntimeError(
                            f"SOL transfer {signature} confirmation timed out: "
                            f"blockhash expired "
                            f"(current_block_height={current_block_height}, "
                            f"last_valid_block_height={last_valid_block_height})"
                        )

                # (4) Check wall-clock timeout.
                elapsed = time.monotonic() - start
                if elapsed >= max_wait_seconds:
                    raise RuntimeError(
                        f"SOL transfer {signature} confirmation timed out after "
                        f"{elapsed:.1f}s (rebroadcasts={broadcast_count})"
                    )

                await asyncio.sleep(rebroadcast_interval)

    async def check_tx_status(self, signature: str) -> str:
        """Check if a transaction succeeded, failed, or is unknown.

        Returns 'confirmed', 'failed', or 'not_found'.
        """
        async with AsyncClient(self.rpc_url) as client:
            sig = Signature.from_string(signature)
            resp = await client.get_signature_statuses([sig], search_transaction_history=True)
            statuses = resp.value
            if not statuses or statuses[0] is None:
                return 'not_found'
            status = statuses[0]
            if status.err:
                return 'failed'
            return 'confirmed'
