"""
Shared blockchain / RPC helpers for the ZAO Fractal Discord bot.

Centralises all Optimism JSON-RPC interaction so that cogs do not duplicate
low-level ``eth_call`` logic, contract addresses, or session management.

Public API:
    - ``eth_call``              -- generic read-only JSON-RPC eth_call
    - ``query_erc20_balance``   -- ERC-20 ``balanceOf(address)``
    - ``query_erc1155_balance`` -- ERC-1155 ``balanceOf(address, uint256)``
    - ``get_total_respect``     -- OG + ZOR Respect combined balance
    - ``is_wearer_of_hat``      -- Hats Protocol ``isWearerOfHat`` check
    - ``close_session``         -- tear down the shared ``aiohttp`` session
"""

import logging
import os
import aiohttp

logger = logging.getLogger('bot')

# ── Optimism contract addresses ──────────────────────────────────────────────
# OG Respect is a standard ERC-20 token (18 decimals) from the original fractal contract.
OG_RESPECT_ADDRESS = '0x34cE89baA7E4a4B00E17F7E4C0cb97105C216957'

# ZOR Respect is an ERC-1155 multi-token contract; all Respect lives under token ID 0.
ZOR_RESPECT_ADDRESS = '0x9885CCeEf7E8371Bf8d6f2413723D25917E7445c'
ZOR_TOKEN_ID = 0

# Fallback public RPC endpoint used when the ALCHEMY_OPTIMISM_RPC env var is not set.
DEFAULT_OPTIMISM_RPC = 'https://mainnet.optimism.io'

# Hats Protocol v1 isWearerOfHat selector
_SELECTOR_IS_WEARER = '0x4352409a'
# Hats Protocol v1 canonical contract address (same on every chain)
_HATS_CONTRACT = '0x3bc1A0Ad72417f2d411118085256fC53CBdDd137'

# ── Shared aiohttp session (lazy-initialised) ───────────────────────────────
_session: aiohttp.ClientSession | None = None


def _get_rpc_url(rpc_url: str | None = None) -> str:
    """Return the Optimism RPC URL to use.

    If *rpc_url* is provided it is returned as-is.  Otherwise the
    ``ALCHEMY_OPTIMISM_RPC`` env var is preferred, falling back to the
    public Optimism endpoint.
    """
    if rpc_url:
        return rpc_url
    return os.getenv('ALCHEMY_OPTIMISM_RPC', DEFAULT_OPTIMISM_RPC)


async def _get_session() -> aiohttp.ClientSession:
    """Return the shared ``aiohttp.ClientSession``, creating it on first use."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    """Close the shared ``aiohttp.ClientSession`` if it is open.

    Should be called during bot shutdown to release resources cleanly.
    """
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


# ── Generic RPC helper ───────────────────────────────────────────────────────

async def eth_call(to: str, data: str, rpc_url: str | None = None) -> str:
    """Execute a read-only ``eth_call`` against the Optimism JSON-RPC.

    Args:
        to: The contract address to call (0x-prefixed hex string).
        data: ABI-encoded calldata (function selector + arguments).
        rpc_url: Optional RPC URL override.  Defaults to the Alchemy env
            var or the public Optimism endpoint.

    Returns:
        The hex-encoded return value from the contract, or ``"0x"``
        if the call fails for any reason (network error, RPC error, etc.).
    """
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"]
    }
    try:
        session = await _get_session()
        async with session.post(
            _get_rpc_url(rpc_url), json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            result = await resp.json()
            return result.get("result", "0x")
    except Exception as e:
        logger.error(f"eth_call failed for {to}: {e}")
        return "0x"


# ── Token balance helpers ────────────────────────────────────────────────────

async def query_erc20_balance(contract: str, wallet: str,
                              rpc_url: str | None = None) -> float:
    """Query the ``balanceOf`` function on an ERC-20 contract.

    Constructs ABI-encoded calldata for ``balanceOf(address)``
    (selector ``0x70a08231``) and converts the raw 256-bit result from
    wei to a human-readable float (18 decimal places).

    Returns:
        The token balance as a float, or ``0.0`` on failure.
    """
    addr_padded = wallet[2:].lower().zfill(64)
    data = f"0x70a08231{addr_padded}"
    result = await eth_call(contract, data, rpc_url)
    if result and result != "0x" and len(result) >= 66:
        return int(result, 16) / 1e18
    return 0.0


async def query_erc1155_balance(contract: str, wallet: str, token_id: int,
                                rpc_url: str | None = None) -> float:
    """Query the ``balanceOf`` function on an ERC-1155 contract.

    Constructs ABI-encoded calldata for ``balanceOf(address, uint256)``
    (selector ``0x00fdd58e``).  ERC-1155 balances are whole integers;
    the result is cast to float for consistency with ``query_erc20_balance``.

    Returns:
        The token balance as a float, or ``0.0`` on failure.
    """
    addr_padded = wallet[2:].lower().zfill(64)
    id_padded = hex(token_id)[2:].zfill(64)
    data = f"0x00fdd58e{addr_padded}{id_padded}"
    result = await eth_call(contract, data, rpc_url)
    if result and result != "0x" and len(result) >= 66:
        return float(int(result, 16))
    return 0.0


async def get_total_respect(wallet: str, rpc_url: str | None = None) -> float:
    """Return the combined OG + ZOR Respect balance for a wallet.

    Queries both the OG Respect (ERC-20) and ZOR Respect (ERC-1155)
    contracts on Optimism and sums the results.

    Returns:
        Total Respect as a float, or ``0.0`` on failure.
    """
    og = await query_erc20_balance(OG_RESPECT_ADDRESS, wallet, rpc_url)
    zor = await query_erc1155_balance(ZOR_RESPECT_ADDRESS, wallet, ZOR_TOKEN_ID, rpc_url)
    return og + zor


# ── Hats Protocol helper ────────────────────────────────────────────────────

async def is_wearer_of_hat(wallet: str, hat_id: int,
                           rpc_url: str | None = None) -> bool:
    """Check if an Ethereum address is currently wearing a specific hat onchain.

    Calls ``isWearerOfHat(address, uint256)`` on the canonical Hats
    Protocol v1 contract.

    Args:
        wallet: The 0x-prefixed Ethereum address to check.
        hat_id: The numeric (256-bit) hat ID.
        rpc_url: Optional RPC URL override.

    Returns:
        True if the address is an active wearer of the hat, False otherwise.
    """
    addr_padded = wallet[2:].lower().zfill(64)
    id_padded = hex(hat_id)[2:].zfill(64)
    data = f"{_SELECTOR_IS_WEARER}{addr_padded}{id_padded}"
    result = await eth_call(_HATS_CONTRACT, data, rpc_url)
    if result and len(result) >= 66:
        return int(result, 16) != 0
    return False
