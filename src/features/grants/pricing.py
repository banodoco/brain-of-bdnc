"""GPU pricing and SOL price fetching for micro-grants."""

import logging
import time

import aiohttp

logger = logging.getLogger('DiscordBot')

# Approximate cloud GPU rates (USD/hr)
GPU_RATES = {
    'H100_80GB': 2.50,
    'H200': 3.50,
    'B200': 5.00,
}

# 20% buffer for platform fees
FEE_MULTIPLIER = 1.2

# Max grant: 50hrs of H100 (including fee buffer)
MAX_GRANT_USD = 50 * GPU_RATES['H100_80GB'] * FEE_MULTIPLIER  # $150

# Cache SOL price for 60 seconds
_sol_price_cache = {'price': None, 'timestamp': 0}
SOL_CACHE_TTL = 60


def calculate_grant_cost(gpu_type: str, hours: float) -> float:
    """Calculate total grant cost in USD including fee buffer."""
    rate = GPU_RATES.get(gpu_type)
    if not rate:
        raise ValueError(f"Unknown GPU type: {gpu_type}. Valid: {list(GPU_RATES.keys())}")
    return round(hours * rate * FEE_MULTIPLIER, 2)


async def get_sol_price_usd() -> float:
    """Fetch current SOL/USD price from CoinGecko (60s cache)."""
    now = time.time()
    if _sol_price_cache['price'] and (now - _sol_price_cache['timestamp']) < SOL_CACHE_TTL:
        return _sol_price_cache['price']

    url = 'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd'
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            price = data['solana']['usd']

    _sol_price_cache['price'] = price
    _sol_price_cache['timestamp'] = now
    logger.info(f"Fetched SOL price: ${price}")
    return price


def usd_to_sol(usd_amount: float, sol_price: float) -> float:
    """Convert USD to SOL amount."""
    return round(usd_amount / sol_price, 6)
