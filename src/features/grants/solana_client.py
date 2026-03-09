"""Solana wallet and SOL transfer for micro-grants."""

import asyncio
import logging
import os
import re

import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
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


class SolanaClient:
    """Handles SOL transfers from the bot wallet."""

    def __init__(self):
        private_key = os.getenv('SOLANA_PRIVATE_KEY')
        if not private_key:
            raise ValueError("SOLANA_PRIVATE_KEY env var not set")

        key_bytes = base58.b58decode(private_key)
        self.keypair = Keypair.from_bytes(key_bytes)
        self.rpc_url = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')

    @property
    def public_key(self) -> Pubkey:
        return self.keypair.pubkey()

    async def get_balance_sol(self) -> float:
        """Get bot wallet balance in SOL."""
        async with AsyncClient(self.rpc_url) as client:
            resp = await client.get_balance(self.public_key, commitment=Confirmed)
            lamports = resp.value
            return lamports / 1_000_000_000

    async def send_sol(self, recipient_address: str, amount_sol: float) -> str:
        """Send SOL to a recipient. Returns tx signature after submission (before confirmation).

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

            ix = transfer(TransferParams(
                from_pubkey=self.public_key,
                to_pubkey=recipient,
                lamports=lamports,
            ))

            # Retry up to 3 times — blockhashes can expire under network load
            last_err = None
            for attempt in range(3):
                blockhash_resp = await client.get_latest_blockhash(commitment=Confirmed)
                blockhash = blockhash_resp.value.blockhash

                msg = MessageV0.try_compile(
                    payer=self.public_key,
                    instructions=[ix],
                    address_lookup_table_accounts=[],
                    recent_blockhash=blockhash,
                )
                tx = VersionedTransaction(msg, [self.keypair])

                try:
                    result = await client.send_transaction(tx)
                    signature = str(result.value)
                    logger.info(f"SOL transfer sent: {signature} ({amount_sol:.4f} SOL to {recipient_address})")
                    return signature
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
        """Wait for a transaction to be confirmed. Returns True if confirmed."""
        async with AsyncClient(self.rpc_url) as client:
            await client.confirm_transaction(signature, commitment=Confirmed)
            logger.info(f"SOL transfer confirmed: {signature}")
            return True

    async def check_tx_status(self, signature: str) -> str:
        """Check if a transaction succeeded, failed, or is unknown.

        Returns 'confirmed', 'failed', or 'not_found'.
        """
        async with AsyncClient(self.rpc_url) as client:
            resp = await client.get_signature_statuses([signature])
            statuses = resp.value
            if not statuses or statuses[0] is None:
                return 'not_found'
            status = statuses[0]
            if status.err:
                return 'failed'
            return 'confirmed'
