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
    - ``submit_breakout``       -- sign and broadcast a ``submitBreakout`` tx
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


# ── Onchain submitBreakout ─────────────────────────────────────────────────
#
# ABI encoding for:  submitBreakout(uint256 groupNum, address[] rankedAddresses)
#
# The function selector is the first 4 bytes of keccak256 of the canonical
# signature.  We lazily compute it on first use via eth_hash (bundled with
# eth-account).  The selector for this signature is 0xa2be0d05.
#
# If the contract uses a different signature (e.g. with additional params),
# update the selector and encoding logic below.

_SUBMIT_BREAKOUT_SELECTOR = None  # Lazily computed on first use


def _keccak256(data: bytes) -> bytes:
    """Compute Keccak-256 hash using eth_hash (bundled with eth-account)."""
    from eth_hash.auto import keccak
    return keccak(data)


def _get_submit_breakout_selector() -> str:
    """Return the 4-byte function selector for ``submitBreakout(uint256,address[])``."""
    global _SUBMIT_BREAKOUT_SELECTOR
    if _SUBMIT_BREAKOUT_SELECTOR is None:
        digest = _keccak256(b"submitBreakout(uint256,address[])")
        _SUBMIT_BREAKOUT_SELECTOR = "0x" + digest[:4].hex()
    return _SUBMIT_BREAKOUT_SELECTOR


def _encode_submit_breakout(group_num: int, addresses: list[str]) -> str:
    """ABI-encode calldata for ``submitBreakout(uint256, address[])``.

    Manual ABI encoding following the Solidity ABI spec:
        - Slot 0: uint256 groupNum (static)
        - Slot 1: offset to address[] data (always 0x40 = 64 for two head slots)
        - Slot 2: length of address[]
        - Slots 3+: each address padded to 32 bytes

    Returns:
        The full 0x-prefixed calldata hex string.
    """
    selector = _get_submit_breakout_selector()

    # Slot 0: groupNum as uint256
    group_hex = hex(group_num)[2:].zfill(64)

    # Slot 1: offset to the dynamic address[] (2 * 32 = 64 = 0x40)
    offset_hex = hex(64)[2:].zfill(64)

    # Slot 2: array length
    length_hex = hex(len(addresses))[2:].zfill(64)

    # Slots 3+: each address left-padded to 32 bytes
    addr_slots = ""
    for addr in addresses:
        # Strip 0x prefix and left-pad to 64 hex chars (32 bytes)
        addr_slots += addr[2:].lower().zfill(64)

    return f"{selector}{group_hex}{offset_hex}{length_hex}{addr_slots}"


async def _rpc_call(method: str, params: list, rpc_url: str | None = None) -> dict:
    """Make a raw JSON-RPC call and return the full response body.

    Unlike ``eth_call`` which is specialised for read-only calls, this is a
    general-purpose RPC helper used by the transaction submission flow.

    Raises:
        RuntimeError: If the RPC returns an error object or the HTTP request fails.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    session = await _get_session()
    async with session.post(
        _get_rpc_url(rpc_url), json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        body = await resp.json()
        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body


async def submit_breakout(
    ranked_addresses: list[str],
    group_num: int,
) -> str | None:
    """Sign and broadcast a ``submitBreakout`` transaction on Optimism.

    Uses the bot's hot wallet (``BOT_PRIVATE_KEY``) to sign and send the
    transaction via raw JSON-RPC calls.  This avoids a ``web3.py`` dependency
    and matches the existing ``aiohttp``-based RPC pattern in this module.

    Args:
        ranked_addresses: Ordered list of 0x-prefixed Ethereum addresses,
            from highest rank (level 6) to lowest (level 1).
        group_num: The breakout group number (1-indexed).

    Returns:
        The 0x-prefixed transaction hash string on success, or ``None`` if
        auto-submit is not configured or the transaction fails.  Errors are
        logged but never raised -- callers should fall back to URL generation.
    """
    from config.config import BOT_PRIVATE_KEY, ORDAO_CONTRACT_ADDRESS, OPTIMISM_RPC_URL

    if not BOT_PRIVATE_KEY:
        return None

    try:
        # Import eth_account for signing (lightweight, no web3.py needed)
        from eth_account import Account

        account = Account.from_key(BOT_PRIVATE_KEY)
        bot_address = account.address
        rpc_url = OPTIMISM_RPC_URL

        logger.info(
            f"Submitting breakout onchain: group={group_num}, "
            f"addresses={[a[:8] + '...' for a in ranked_addresses]}, "
            f"from={bot_address[:8]}..."
        )

        # 1. Get the nonce for the bot wallet
        nonce_resp = await _rpc_call(
            "eth_getTransactionCount", [bot_address, "latest"], rpc_url
        )
        nonce = int(nonce_resp["result"], 16)

        # 2. Get current gas prices from the network
        gas_price_resp = await _rpc_call("eth_gasPrice", [], rpc_url)
        base_gas_price = int(gas_price_resp["result"], 16)

        # 3. Encode the calldata
        calldata = _encode_submit_breakout(group_num, ranked_addresses)

        # 4. Estimate gas (with a safety margin)
        estimate_tx = {
            "from": bot_address,
            "to": ORDAO_CONTRACT_ADDRESS,
            "data": calldata,
        }
        try:
            gas_resp = await _rpc_call(
                "eth_estimateGas", [estimate_tx], rpc_url
            )
            gas_limit = int(int(gas_resp["result"], 16) * 1.3)  # 30% safety margin
        except RuntimeError as e:
            logger.warning(f"Gas estimation failed, using default 300000: {e}")
            gas_limit = 300_000

        # 5. Build an EIP-1559 transaction (Type 2) for Optimism
        # Optimism L2 gas is very cheap; we use modest priority fees.
        max_priority_fee = 1_000_000  # 0.001 gwei -- minimal tip on Optimism
        max_fee = base_gas_price * 2 + max_priority_fee  # 2x base + priority

        tx = {
            "type": 2,              # EIP-1559
            "chainId": 10,          # Optimism mainnet
            "nonce": nonce,
            "to": ORDAO_CONTRACT_ADDRESS,
            "value": 0,
            "data": bytes.fromhex(calldata[2:]) if calldata.startswith("0x") else bytes.fromhex(calldata),
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
        }

        # 6. Sign the transaction
        signed = account.sign_transaction(tx)

        # 7. Broadcast via eth_sendRawTransaction
        raw_hex = "0x" + signed.raw_transaction.hex()
        send_resp = await _rpc_call(
            "eth_sendRawTransaction", [raw_hex], rpc_url
        )

        tx_hash = send_resp.get("result")
        if tx_hash:
            logger.info(f"Breakout submitted onchain! tx_hash={tx_hash}")
            return tx_hash
        else:
            logger.error(f"sendRawTransaction returned no hash: {send_resp}")
            return None

    except ImportError:
        logger.error(
            "eth-account package not installed. "
            "Run: pip install eth-account  -- falling back to URL generation."
        )
        return None
    except Exception as e:
        logger.error(f"Failed to submit breakout onchain: {e}", exc_info=True)
        return None
