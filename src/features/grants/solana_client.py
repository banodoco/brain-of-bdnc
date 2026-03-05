"""Solana wallet and SOL transfer for micro-grants."""

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
        """Send SOL to a recipient. Returns tx signature string.

        Raises RuntimeError on failure.
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

            # Build versioned transaction
            ix = transfer(TransferParams(
                from_pubkey=self.public_key,
                to_pubkey=recipient,
                lamports=lamports,
            ))

            blockhash_resp = await client.get_latest_blockhash(commitment=Confirmed)
            blockhash = blockhash_resp.value.blockhash

            msg = MessageV0.try_compile(
                payer=self.public_key,
                instructions=[ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self.keypair])

            # Send and confirm
            result = await client.send_transaction(tx)
            signature = str(result.value)
            logger.info(f"SOL transfer sent: {signature} ({amount_sol:.4f} SOL to {recipient_address})")

            # Wait for confirmation
            await client.confirm_transaction(signature, commitment=Confirmed)
            logger.info(f"SOL transfer confirmed: {signature}")

            return signature
