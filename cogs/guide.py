"""
Guide and Leaderboard cog for the ZAO Fractal Discord Bot.

Provides two slash commands:
  /guide        - Posts an educational embed explaining how ZAO Fractal
                  voting works, including the ranking flow and Respect
                  point distribution.
  /leaderboard  - Queries on-chain Respect token balances (both the legacy
                  OG ERC-20 and the newer ZOR ERC-1155 contracts on
                  Optimism) and displays the top 10 holders in Discord.

On-chain data is fetched via raw ``eth_call`` JSON-RPC requests so the bot
has no dependency on heavyweight web3 libraries.  Leaderboard results are
cached in memory for 5 minutes to avoid excessive RPC calls.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import logging
import time
import aiohttp
from cogs.base import BaseCog
from config.config import RESPECT_POINTS

# Resolve the absolute path to the shared data directory (project root / data)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

# JSON file mapping human-readable member names to their Ethereum wallet addresses
NAMES_FILE = os.path.join(DATA_DIR, 'names_to_wallets.json')

# ── Optimism contract addresses ──────────────────────────────────────────────
# OG Respect is a standard ERC-20 token from the original fractal contract.
OG_RESPECT_ADDRESS = '0x34cE89baA7E4a4B00E17F7E4C0cb97105C216957'

# ZOR Respect is an ERC-1155 multi-token contract; all Respect lives under
# token ID 0.
ZOR_RESPECT_ADDRESS = '0x9885CCeEf7E8371Bf8d6f2413723D25917E7445c'
ZOR_TOKEN_ID = 0

# Fallback public RPC endpoint used when the ALCHEMY_OPTIMISM_RPC env var is
# not set.  The Alchemy endpoint is preferred for reliability and rate limits.
DEFAULT_OPTIMISM_RPC = 'https://mainnet.optimism.io'


class GuideCog(BaseCog):
    """Discord cog providing informational and leaderboard slash commands.

    Inherits from ``BaseCog`` to get a per-cog logger and any shared
    initialisation logic.

    Attributes:
        _lb_cache: Optional dict holding cached leaderboard data and a
            timestamp.  Structure: ``{'data': [<top-10 dicts>], 'timestamp': float}``.
            Set to ``None`` until the first successful fetch.
        _lb_cache_ttl: Time-to-live in seconds for the leaderboard cache.
            Defaults to 300 (5 minutes).
    """

    def __init__(self, bot):
        """Initialise the GuideCog and register it with the bot.

        Args:
            bot: The ``commands.Bot`` instance that owns this cog.
        """
        super().__init__(bot)
        # In-memory cache to avoid hammering the Optimism RPC on every call
        self._lb_cache = None  # {'data': [...], 'timestamp': float}
        self._lb_cache_ttl = 300  # 5 minutes

    @app_commands.command(
        name="guide",
        description="Learn how ZAO Fractal voting works"
    )
    async def guide(self, interaction: discord.Interaction):
        """Post an overview of ZAO Fractal with a link to the full guide.

        Builds a rich embed containing:
        - A short description of what ZAO Fractal is.
        - A numbered quick-flow walkthrough of a typical session.
        - A table mapping rank positions to Respect point rewards.
        - A link to the full external guide with visuals.

        Args:
            interaction: The Discord interaction triggered by ``/guide``.
        """
        # Green-themed embed matching the ZAO brand colour (0x57F287)
        embed = discord.Embed(
            title="\U0001f4da How ZAO Fractal Works",
            description=(
                "**ZAO Fractal** is a fractal democracy system where small groups "
                "reach consensus on contribution rankings and earn onchain Respect tokens."
            ),
            color=0x57F287
        )

        # Step-by-step walkthrough of a typical fractal session
        embed.add_field(
            name="\u26a1 Quick Flow",
            value=(
                "1\ufe0f\u20e3 **Group up** \u2014 2-6 people join a voice channel\n"
                "2\ufe0f\u20e3 **Start** \u2014 Facilitator runs `/zaofractal`\n"
                "3\ufe0f\u20e3 **Vote** \u2014 Rank contributions Level 6 \u2192 1\n"
                "4\ufe0f\u20e3 **Results** \u2014 Bot posts rankings + onchain submit link\n"
                "5\ufe0f\u20e3 **Earn Respect** \u2014 Confirm results onchain at zao.frapps.xyz"
            ),
            inline=False
        )

        # Build a human-readable table pairing each rank/level with its
        # Respect point reward from the config constant.
        ranks = ["\U0001f947 1st", "\U0001f948 2nd", "\U0001f949 3rd", "4th", "5th", "6th"]
        levels = [6, 5, 4, 3, 2, 1]
        table_lines = []
        for i in range(len(RESPECT_POINTS)):
            table_lines.append(f"{ranks[i]} (Lvl {levels[i]}) \u2192 **{RESPECT_POINTS[i]} Respect**")

        # Respect points field -- shows the doubled Fibonacci reward table
        embed.add_field(
            name="\U0001f3c6 Respect Points (2x Fibonacci)",
            value="\n".join(table_lines),
            inline=False
        )

        # External link to the full illustrated guide hosted on Vercel
        embed.add_field(
            name="\U0001f4d6 Full Guide",
            value="**[View the complete guide with visuals \u2192](https://zao-fractal.vercel.app/guide)**",
            inline=False
        )

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="leaderboard",
        description="View the ZAO Respect leaderboard"
    )
    async def leaderboard(self, interaction: discord.Interaction):
        """Fetch on-chain Respect balances and display the top 10 in Discord.

        The response is deferred because on-chain queries can take several
        seconds.  Results are cached for ``_lb_cache_ttl`` seconds so
        repeated invocations are nearly instant.

        Args:
            interaction: The Discord interaction triggered by ``/leaderboard``.
        """
        # Defer so Discord doesn't time out while we query the blockchain
        await interaction.response.defer()

        # Attempt to fetch (or return cached) leaderboard data.
        # Broad exception catch ensures any RPC / parsing failure is surfaced
        # gracefully to the user rather than crashing the command.
        try:
            top_10 = await self._fetch_leaderboard()
        except Exception as e:
            self.logger.error(f"Leaderboard fetch failed: {e}")
            await interaction.followup.send(
                "Failed to fetch onchain data. Try again later.",
                ephemeral=True
            )
            return

        # Guard against an empty wallet file or all-zero balances
        if not top_10:
            await interaction.followup.send("No leaderboard data available.", ephemeral=True)
            return

        # Build the leaderboard embed with the same green brand colour
        embed = discord.Embed(
            title="\U0001f3c6 ZAO Respect Leaderboard",
            description="Live onchain Respect rankings (OG + ZOR) on Optimism",
            color=0x57F287
        )

        # Format each entry with a medal emoji for the top 3 and a plain
        # number for ranks 4-10.
        lines = []
        for entry in top_10:
            rank = entry['rank']
            if rank == 1:
                medal = "\U0001f947"
            elif rank == 2:
                medal = "\U0001f948"
            elif rank == 3:
                medal = "\U0001f949"
            else:
                medal = f"`{rank}.`"

            total = entry['total']
            # Format OG and ZOR balances individually for the breakdown.
            # OG uses :.0f (float from ERC-20 division); ZOR uses int (whole number).
            og_str = f"{entry['og']:.0f}" if entry['og'] > 0 else "0"
            zor_str = f"{int(entry['zor'])}" if entry['zor'] > 0 else "0"

            # Each line: medal/rank  Name -- Total Respect (OG + ZOR breakdown)
            lines.append(
                f"{medal} **{entry['name']}** \u2014 **{total:.0f}** Respect "
                f"({og_str} OG + {zor_str} ZOR)"
            )

        # Leaderboard rankings field -- all 10 entries joined into one block
        embed.add_field(
            name="Top 10",
            value="\n".join(lines),
            inline=False
        )

        # External link to the full leaderboard page (shows all members,
        # not just the top 10)
        embed.add_field(
            name="\U0001f310 Full Leaderboard",
            value="**[View all members \u2192](https://www.thezao.com/zao-leaderboard)**",
            inline=False
        )

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
        await interaction.followup.send(embed=embed)

    async def _fetch_leaderboard(self) -> list[dict]:
        """Fetch on-chain Respect balances for every known member wallet.

        Reads the ``names_to_wallets.json`` mapping, queries both the OG
        (ERC-20) and ZOR (ERC-1155) Respect contracts on Optimism for each
        wallet, sums the balances, ranks members by total, and returns the
        top 10.

        Returns:
            A list of up to 10 dicts, each containing keys:
            ``name``, ``wallet``, ``og``, ``zor``, ``total``, ``rank``.
            Returns an empty list when no wallet data is available.
        """
        # Return cached results if they are still fresh (within TTL window).
        # This avoids redundant RPC calls when multiple users invoke
        # /leaderboard in quick succession.
        if self._lb_cache and time.time() - self._lb_cache['timestamp'] < self._lb_cache_ttl:
            return self._lb_cache['data']

        # Load the name-to-wallet mapping from disk.  If the file doesn't
        # exist yet (first run / no wallets registered), return empty.
        if not os.path.exists(NAMES_FILE):
            return []

        with open(NAMES_FILE, 'r') as f:
            names_map = json.load(f)  # dict[str, str] -- {display_name: wallet_address}

        # Filter out members who haven't linked a wallet yet (empty string)
        entries = [(name, wallet) for name, wallet in names_map.items() if wallet]
        if not entries:
            return []

        # Prefer the Alchemy RPC if configured; fall back to the public endpoint
        rpc_url = os.getenv('ALCHEMY_OPTIMISM_RPC', DEFAULT_OPTIMISM_RPC)
        results = []

        # Query each member's OG (ERC-20) + ZOR (ERC-1155) balance
        # sequentially to stay within RPC rate limits.
        async with aiohttp.ClientSession() as session:
            for name, wallet in entries:
                og = await self._query_erc20(session, rpc_url, wallet, OG_RESPECT_ADDRESS)
                zor = await self._query_erc1155(session, rpc_url, wallet, ZOR_RESPECT_ADDRESS, ZOR_TOKEN_ID)
                total = og + zor
                # Only include members who have earned at least some Respect
                if total > 0:
                    results.append({
                        'name': name,
                        'wallet': wallet,
                        'og': og,
                        'zor': zor,
                        'total': total,
                    })

        # Sort descending by total Respect so highest earners appear first
        results.sort(key=lambda x: -x['total'])

        # Assign 1-based rank numbers after sorting.  Note: ties are not
        # handled specially -- members with equal totals get sequential ranks.
        for i, entry in enumerate(results):
            entry['rank'] = i + 1

        # Only the top 10 are displayed in Discord; slice before caching
        top_10 = results[:10]

        # Store the result and a timestamp so subsequent calls within the
        # TTL window can skip the on-chain queries entirely.
        self._lb_cache = {'data': top_10, 'timestamp': time.time()}
        return top_10

    async def _query_erc20(self, session: aiohttp.ClientSession, rpc_url: str,
                           wallet: str, contract: str) -> float:
        """Query the ``balanceOf`` function on an ERC-20 contract.

        Constructs the ABI-encoded calldata for ``balanceOf(address)``
        (selector ``0x70a08231``) and converts the raw 256-bit result from
        wei to a human-readable float (18 decimal places).

        Args:
            session: Reusable aiohttp session for connection pooling.
            rpc_url: Optimism JSON-RPC endpoint URL.
            wallet: The holder's Ethereum address (``0x``-prefixed).
            contract: The ERC-20 contract address.

        Returns:
            The token balance as a float, or ``0.0`` on failure.
        """
        # Strip the 0x prefix and left-pad to 32 bytes (64 hex chars)
        addr_padded = wallet[2:].lower().zfill(64)
        # 0x70a08231 is the 4-byte function selector for balanceOf(address)
        data = f"0x70a08231{addr_padded}"
        result = await self._eth_call(session, rpc_url, contract, data)
        # A valid ERC-20 balanceOf response is a 32-byte (64 hex char) uint256,
        # prefixed with "0x" for a total length of at least 66 characters.
        # Empty or error responses ("0x") are treated as zero balance.
        if result and result != "0x" and len(result) >= 66:
            # Convert from 18-decimal fixed-point to a float
            return int(result, 16) / 1e18
        return 0.0

    async def _query_erc1155(self, session: aiohttp.ClientSession, rpc_url: str,
                             wallet: str, contract: str, token_id: int) -> float:
        """Query the ``balanceOf`` function on an ERC-1155 contract.

        Constructs the ABI-encoded calldata for
        ``balanceOf(address, uint256)`` (selector ``0x00fdd58e``).
        Unlike ERC-20, ERC-1155 balances are whole integers (no decimals),
        so the raw value is returned as-is (cast to float for consistency).

        Args:
            session: Reusable aiohttp session for connection pooling.
            rpc_url: Optimism JSON-RPC endpoint URL.
            wallet: The holder's Ethereum address (``0x``-prefixed).
            contract: The ERC-1155 contract address.
            token_id: The specific token ID to query (0 for ZOR Respect).

        Returns:
            The token balance as a float, or ``0.0`` on failure.
        """
        # ABI-encode the two arguments: address (32 bytes) and token ID (32 bytes)
        addr_padded = wallet[2:].lower().zfill(64)
        id_padded = hex(token_id)[2:].zfill(64)
        # 0x00fdd58e is the 4-byte selector for balanceOf(address, uint256)
        data = f"0x00fdd58e{addr_padded}{id_padded}"
        result = await self._eth_call(session, rpc_url, contract, data)
        # Same 66-char minimum check as ERC-20 (32-byte uint256 + "0x" prefix)
        if result and result != "0x" and len(result) >= 66:
            # ERC-1155 balances have no decimals; cast to float for uniformity
            # with the ERC-20 return type so callers can simply add them.
            return float(int(result, 16))
        return 0.0

    async def _eth_call(self, session: aiohttp.ClientSession, rpc_url: str,
                        to: str, data: str) -> str:
        """Execute a read-only ``eth_call`` against the Optimism JSON-RPC.

        This is the low-level helper used by both ``_query_erc20`` and
        ``_query_erc1155``.  It sends a single JSON-RPC request with a
        10-second timeout and returns the hex-encoded result string.

        Args:
            session: Reusable aiohttp session for connection pooling.
            rpc_url: The JSON-RPC endpoint URL.
            to: The contract address to call.
            data: ABI-encoded calldata (function selector + arguments).

        Returns:
            The hex-encoded return value from the contract, or ``"0x"``
            if the call fails for any reason (network error, RPC error, etc.).
        """
        # Build a standard JSON-RPC 2.0 payload.  "latest" reads the most
        # recent confirmed block on Optimism.
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]
        }
        try:
            # 10-second timeout guards against slow or unresponsive RPCs
            async with session.post(rpc_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                # The "result" key holds the hex-encoded return data on success.
                # On RPC-level errors the key may be missing; default to "0x".
                return result.get("result", "0x")
        except Exception as e:
            # Log but don't raise -- callers treat "0x" as a zero balance
            self.logger.error(f"eth_call failed for {to}: {e}")
            return "0x"


async def setup(bot):
    """Entry point called by ``bot.load_extension('cogs.guide')``."""
    await bot.add_cog(GuideCog(bot))
