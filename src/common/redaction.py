from typing import Optional


def redact_wallet(wallet: Optional[str]) -> str:
    if not wallet:
        return 'unknown'
    wallet = str(wallet)
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:4]}...{wallet[-4:]}"
