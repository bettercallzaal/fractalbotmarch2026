"""
Hats Protocol integration cog for the ZAO Fractal Discord bot.

Reads the ZAO organisation's hat tree directly from the Hats Protocol smart
contract on Optimism via raw ``eth_call`` RPC requests (no web3 library
required). Provides slash commands for browsing the tree, inspecting
individual hats, checking which hats a member wears, and claiming hats.

Additionally implements automatic Discord role synchronisation: an admin can
link an onchain hat ID to a Discord role, and a background loop (every 10
minutes) grants or revokes that role based on whether each member's
registered wallet currently wears the hat.

Key components:
    - Low-level helpers (_eth_call, _view_hat, _is_wearer_of_hat, etc.):
      ABI-encode calls, send them to the Optimism RPC, and decode responses.
    - _fetch_ipfs_details / _ipfs_to_http: Resolve IPFS URIs to retrieve hat
      metadata (name, description, image).
    - HatsRoleMapping: Supabase-backed store of hat-ID-to-Discord-role mappings.
    - HatsCog: Discord cog exposing /hats, /hat, /myhats, /claimhat, and
      admin commands for managing role sync.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import logging
import time
import os
import aiohttp
from cogs.base import BaseCog
from utils.blockchain import (
    eth_call as _shared_eth_call,
    is_wearer_of_hat as _shared_is_wearer_of_hat,
)
from utils.supabase_client import get_supabase

# ── Hats Protocol constants ──────────────────────────────────────────────────

# Canonical Hats Protocol v1 contract address (same deployment on every chain)
HATS_CONTRACT = '0x3bc1A0Ad72417f2d411118085256fC53CBdDd137'

# The ZAO's tree ID within the Hats Protocol registry
ZAO_TREE_ID = 226

# ── Solidity function selectors (first 4 bytes of keccak256 hash) ─────────

SELECTOR_VIEW_HAT = '0xd395acf8'         # viewHat(uint256)

# ── Cache TTLs ────────────────────────────────────────────────────────────────

TREE_CACHE_TTL = 600   # Seconds to cache the full hat tree (10 minutes)
WEARER_CACHE_TTL = 300  # Seconds to cache individual wearer checks (5 minutes)

def _top_hat_id(tree_id: int) -> int:
    """Compute the top hat ID for a given tree.

    In the Hats Protocol, the top hat occupies the most-significant 32 bits
    of the 256-bit hat ID (i.e., tree_id is left-shifted by 224 bits).
    """
    return tree_id << 224


def _hat_id_hex(hat_id: int) -> str:
    """Format a hat ID as a 0x-prefixed 64-char hex string.

    Args:
        hat_id: The numeric (256-bit) hat identifier.

    Returns:
        A ``0x``-prefixed hex string zero-padded to 64 characters (32 bytes).
    """
    return '0x' + hex(hat_id)[2:].zfill(64)


def _pad_uint256(val: int) -> str:
    """Pad an integer to a 32-byte hex string for ABI encoding.

    Args:
        val: The unsigned integer value to encode.

    Returns:
        A 64-character hex string (no ``0x`` prefix), left-zero-padded.
    """
    return hex(val)[2:].zfill(64)


def _pad_address(addr: str) -> str:
    """Pad an Ethereum address to a 32-byte hex string for ABI encoding.

    Strips the ``0x`` prefix, lowercases, and left-zero-pads to 64 chars.

    Args:
        addr: A ``0x``-prefixed Ethereum address (20 bytes / 40 hex chars).

    Returns:
        A 64-character lowercase hex string (no ``0x`` prefix).
    """
    return addr[2:].lower().zfill(64)


async def _eth_call(to: str, data: str) -> str:
    """Execute a read-only ``eth_call`` against the Optimism RPC.

    Delegates to the shared ``utils.blockchain.eth_call`` helper so that
    session management and RPC URL resolution are centralised.

    Args:
        to: The contract address to call (hex string with 0x prefix).
        data: ABI-encoded calldata (hex string with 0x prefix).

    Returns:
        The hex-encoded return data from the contract, or ``"0x"`` on failure.
    """
    return await _shared_eth_call(to, data)


async def _view_hat(hat_id: int) -> dict | None:
    """Call ``viewHat(uint256)`` on the Hats contract and decode the ABI response.

    Returns a dict with keys: details, max_supply, supply, eligibility, toggle,
    image_uri, last_hat_id, mutable, active.  Returns None if the hat does not
    exist or the response cannot be parsed.
    """
    data = SELECTOR_VIEW_HAT + _pad_uint256(hat_id)
    result = await _eth_call(HATS_CONTRACT, data)

    # Guard: empty or too-short responses indicate the hat does not exist
    if not result or result == '0x' or len(result) < 66:
        return None

    # viewHat returns a tuple of 9 ABI-encoded values:
    #   (string details, uint32 maxSupply, uint32 supply,
    #    address eligibility, address toggle, string imageURI,
    #    uint16 lastHatId, bool mutable_, bool active)
    # Strings are dynamic types, so their slots contain byte-offsets to the
    # actual data at the end of the encoding.  Each ABI "word" is 64 hex
    # chars (32 bytes).
    try:
        raw = result[2:]  # strip the leading "0x"

        # ── Fixed-position words (words 0-8) ──────────────────────────────
        # Word 0: byte-offset to the "details" dynamic string
        # Word 1: maxSupply (uint32, right-aligned in 32 bytes)
        max_supply = int(raw[64:128], 16)
        # Word 2: current supply (uint32)
        supply = int(raw[128:192], 16)
        # Word 3: eligibility module address (last 20 bytes of 32-byte word)
        eligibility = '0x' + raw[192+24:256]
        # Word 4: toggle module address (last 20 bytes)
        toggle = '0x' + raw[256+24:320]
        # Word 5: byte-offset to the "imageURI" dynamic string
        # Word 6: lastHatId -- number of child hats created under this hat
        last_hat_id = int(raw[384:448], 16)
        # Word 7: mutable_ flag (1 = hat config can be changed)
        mutable = int(raw[448:512], 16) != 0
        # Word 8: active flag (1 = hat is currently active)
        active = int(raw[512:576], 16) != 0

        # ── Decode "details" dynamic string ───────────────────────────────
        # The offset (in bytes) is stored in word 0; multiply by 2 for hex chars
        details_offset = int(raw[0:64], 16) * 2
        # First word at the offset is the string length (in bytes)
        details_len = int(raw[details_offset:details_offset+64], 16)
        # Read exactly that many bytes of hex-encoded UTF-8 string data
        details_hex = raw[details_offset+64:details_offset+64+details_len*2]
        details = bytes.fromhex(details_hex).decode('utf-8', errors='replace') if details_hex else ''

        # ── Decode "imageURI" dynamic string ──────────────────────────────
        # The offset is stored in word 5
        image_offset = int(raw[320:384], 16) * 2
        image_len = int(raw[image_offset:image_offset+64], 16)
        image_hex = raw[image_offset+64:image_offset+64+image_len*2]
        image_uri = bytes.fromhex(image_hex).decode('utf-8', errors='replace') if image_hex else ''

        return {
            'details': details,
            'max_supply': max_supply,
            'supply': supply,
            'eligibility': eligibility,
            'toggle': toggle,
            'image_uri': image_uri,
            'last_hat_id': last_hat_id,
            'mutable': mutable,
            'active': active,
        }
    except Exception as e:
        logging.getLogger('bot').error(f"Failed to parse viewHat result for {_hat_id_hex(hat_id)}: {e}")
        return None


async def _is_wearer_of_hat(address: str, hat_id: int) -> bool:
    """Check if an Ethereum address is currently wearing a specific hat onchain.

    Results are cached for ``WEARER_CACHE_TTL`` seconds (5 minutes) in the
    module-level ``_wearer_cache`` dict to reduce redundant RPC calls during
    the role-sync loop and ``/myhats`` command.

    Args:
        address: The 0x-prefixed Ethereum address to check.
        hat_id: The numeric hat ID to check against.

    Returns:
        True if the address is an active wearer of the hat, False otherwise.
    """
    cache_key = (address.lower(), hat_id)
    cached = _wearer_cache.get(cache_key)
    if cached and time.time() - cached['timestamp'] < WEARER_CACHE_TTL:
        return cached['result']

    result = await _shared_is_wearer_of_hat(address, hat_id)
    _wearer_cache[cache_key] = {'result': result, 'timestamp': time.time()}
    return result


# Module-level wearer cache shared across all callers of _is_wearer_of_hat.
# Keyed by (wallet_lower, hat_id) -> {result: bool, timestamp: float}.
_wearer_cache: dict = {}


async def _fetch_ipfs_details(ipfs_uri: str) -> dict:
    """Fetch and parse hat metadata from an IPFS or HTTP URI.

    Hat details are typically stored as JSON on IPFS containing fields like
    ``name``, ``description``, etc.  If the content is not valid JSON, it is
    treated as a plain-text name (truncated to 100 chars).

    Args:
        ipfs_uri: An ``ipfs://`` or ``http(s)://`` URI pointing to the metadata.

    Returns:
        A dict of parsed metadata, or an empty dict on failure.
    """
    if not ipfs_uri:
        return {}

    # Convert ipfs:// protocol to a public HTTP gateway URL
    if ipfs_uri.startswith('ipfs://'):
        url = 'https://ipfs.io/ipfs/' + ipfs_uri[7:]
    elif ipfs_uri.startswith('http'):
        url = ipfs_uri
    else:
        return {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        # Content is plain text rather than JSON; use it as name
                        return {'name': text[:100]}
    except Exception:
        pass
    return {}


def _ipfs_to_http(uri: str) -> str | None:
    """Convert an ``ipfs://`` URI to an HTTP gateway URL.

    Passes through ``http(s)://`` URIs unchanged. Returns None for empty
    or unrecognised URI schemes.
    """
    if not uri:
        return None
    if uri.startswith('ipfs://'):
        return 'https://ipfs.io/ipfs/' + uri[7:]
    if uri.startswith('http'):
        return uri
    return None


class HatsRoleMapping:
    """Supabase-backed store mapping onchain hat IDs to Discord role IDs.

    Admins configure these mappings via /admin_link_hat.  The background
    sync loop in HatsCog reads them to decide which Discord roles to
    grant or revoke based on hat ownership.
    """

    def __init__(self):
        """Initialise the Supabase client for hat-role mappings."""
        self._supabase = get_supabase()

    def set(self, hat_id_hex: str, role_id: int, hat_name: str):
        """Create or update a hat-to-role mapping in Supabase."""
        self._supabase.table("discord_hats_role_mappings").upsert(
            {"hat_id_hex": hat_id_hex, "role_id": role_id, "hat_name": hat_name},
            on_conflict="hat_id_hex",
        ).execute()

    def remove(self, hat_id_hex: str):
        """Remove a hat-to-role mapping if it exists."""
        self._supabase.table("discord_hats_role_mappings").delete().eq(
            "hat_id_hex", hat_id_hex
        ).execute()

    def get_all(self) -> dict:
        """Return all mappings as a dict keyed by hat_id_hex.

        Returns the same format the rest of the code expects:
            { "<hat_id_hex>": {"role_id": <int>, "hat_name": "<str>"}, ... }
        """
        result = self._supabase.table("discord_hats_role_mappings").select("*").execute()
        return {
            row["hat_id_hex"]: {"role_id": row["role_id"], "hat_name": row["hat_name"]}
            for row in (result.data or [])
        }

    def get_role_id(self, hat_id_hex: str) -> int | None:
        """Look up the Discord role ID for a given hat, or None if unmapped."""
        result = self._supabase.table("discord_hats_role_mappings").select(
            "role_id"
        ).eq("hat_id_hex", hat_id_hex).execute()
        if result.data:
            return result.data[0]["role_id"]
        return None


class HatsCog(BaseCog):
    """Discord cog for Hats Protocol integration on Optimism.

    Provides commands for browsing the ZAO hat tree, inspecting individual
    hats, checking which hats a member wears, and claiming hats. Also runs
    a background task that synchronises Discord roles with onchain hat
    ownership every 10 minutes.

    Slash commands:
        /hats              -- View the full ZAO hat tree.
        /hat <name>        -- View details of a specific hat by name search.
        /myhats [user]     -- Check which hats a member wears onchain.
        /claimhat          -- Get a link to claim hats via the Hats Protocol app.
        /admin_link_hat    -- (Admin) Link a hat to a Discord role for auto-sync.
        /admin_unlink_hat  -- (Admin) Remove a hat-to-role mapping.
        /admin_hat_roles   -- (Admin) List all configured hat-to-role mappings.
        /admin_sync_hats   -- (Admin) Manually trigger an immediate role sync.
    """

    def __init__(self, bot):
        """Initialise the cog with role mappings and empty caches.

        Args:
            bot: The Discord bot instance this cog is attached to.
        """
        super().__init__(bot)
        self.role_mapping = HatsRoleMapping()
        self._tree_cache = None       # Cached list of tree node dicts
        self._tree_cache_time = 0     # Epoch timestamp of last tree fetch

    async def cog_load(self):
        """Called by discord.py when the cog is loaded; starts the periodic role sync."""
        self.sync_roles_loop.start()

    async def cog_unload(self):
        """Called by discord.py when the cog is unloaded; cancels the sync loop."""
        self.sync_roles_loop.cancel()

    # ── Tree fetching ──

    async def _build_tree(self, hat_id: int, depth: int = 0, max_depth: int = 3) -> list[dict]:
        """Recursively build the hat tree from onchain data.

        Starting from ``hat_id``, fetches hat metadata via ``_view_hat``,
        resolves a human-readable name from IPFS if available, then recurses
        into children up to ``max_depth`` levels deep.

        Args:
            hat_id: The numeric hat ID to start from.
            depth: Current recursion depth (0 = root).
            max_depth: Maximum depth to recurse into children.

        Returns:
            A list containing a single node dict (with nested 'children'),
            or an empty list if the hat does not exist.
        """
        hat_data = await _view_hat(hat_id)
        if not hat_data:
            return []

        # Try to get a readable name from IPFS metadata
        name = None
        details_meta = await _fetch_ipfs_details(hat_data['details'])
        if isinstance(details_meta, dict):
            name = details_meta.get('name') or details_meta.get('title')
        if not name and hat_data['details']:
            # If details aren't an IPFS URI, treat the raw string as the name
            name = hat_data['details'][:80] if not hat_data['details'].startswith('ipfs://') else None

        # Build the node dict representing this hat in the tree
        node = {
            'id': hat_id,
            'id_hex': _hat_id_hex(hat_id),
            'name': name or f'Hat {_hat_id_hex(hat_id)[:18]}...',
            'supply': hat_data['supply'],
            'max_supply': hat_data['max_supply'],
            'active': hat_data['active'],
            'image_uri': hat_data['image_uri'],
            'children': [],
            'depth': depth,
        }

        # Stop recursion at max depth to avoid excessive RPC calls
        if depth >= max_depth:
            return [node]

        # Enumerate child hats using last_hat_id (count of children)
        if hat_data['last_hat_id'] > 0:
            for i in range(1, hat_data['last_hat_id'] + 1):
                # Child hat ID: shift parent's level and add child index
                child_id = self._compute_child_id(hat_id, i, depth)
                if child_id:
                    child_nodes = await self._build_tree(child_id, depth + 1, max_depth)
                    node['children'].extend(child_nodes)

        return [node]

    def _compute_child_id(self, parent_id: int, child_index: int, parent_depth: int) -> int | None:
        """Compute a child hat ID given parent and index.

        Hat IDs use a hierarchical encoding:
        - Top hat (level 0): first 4 bytes = tree ID
        - Level 1: next 2 bytes
        - Level 2+: subsequent 2 bytes each

        Args:
            parent_id: The numeric hat ID of the parent hat.
            child_index: The 1-based index of the child within the parent.
            parent_depth: The tree depth of the parent (0 = top hat).

        Returns:
            The numeric child hat ID, or None if the depth exceeds the
            maximum supported by the 256-bit hat ID encoding.
        """
        # Level 0 (top hat): children use bytes 4-5 (bits 208-223)
        # Level 1: children use bytes 6-7 (bits 192-207)
        # Level N: children use bytes (4 + N*2) to (5 + N*2)

        if parent_depth == 0:
            # Top hat -> level 1 child
            shift = 224 - 16  # bits 208
        else:
            # Level N -> level N+1
            shift = 224 - 16 * (parent_depth + 1)

        if shift < 0:
            return None

        return parent_id | (child_index << shift)

    async def _get_cached_tree(self) -> list[dict]:
        """Return the ZAO hat tree, using a cached version if still fresh.

        The tree is rebuilt from onchain data only when the cache has expired
        (after TREE_CACHE_TTL seconds). This avoids hammering the RPC on
        repeated command invocations.
        """
        # Return cached tree if it exists and hasn't expired
        if self._tree_cache and time.time() - self._tree_cache_time < TREE_CACHE_TTL:
            return self._tree_cache

        top_hat = _top_hat_id(ZAO_TREE_ID)
        tree = await self._build_tree(top_hat, depth=0, max_depth=2)
        self._tree_cache = tree
        self._tree_cache_time = time.time()
        return tree

    # ── Role sync ──

    @tasks.loop(minutes=10)
    async def sync_roles_loop(self):
        """Background task: sync Discord roles with onchain hat ownership.

        For every configured hat-to-role mapping, iterates over all guild
        members, checks whether their registered wallet wears the hat
        onchain, and grants or revokes the corresponding Discord role
        accordingly. Members without a registered wallet have the role
        removed if they hold it.
        """
        mappings = self.role_mapping.get_all()
        if not mappings:
            return

        # Need the wallet registry to map Discord members to Ethereum addresses
        registry = getattr(self.bot, 'wallet_registry', None)
        if not registry:
            return

        for guild in self.bot.guilds:
            for hat_id_hex, mapping in mappings.items():
                role_id = mapping['role_id']
                role = guild.get_role(role_id)
                if not role:
                    continue

                # Convert hex hat ID string to integer for onchain lookup
                hat_id = int(hat_id_hex, 16)

                for member in guild.members:
                    if member.bot:
                        continue

                    wallet = registry.lookup(member)
                    if not wallet:
                        # No registered wallet means they can't wear a hat; revoke role
                        if role in member.roles:
                            try:
                                await member.remove_roles(role, reason="Hats Protocol sync - no wallet")
                            except discord.Forbidden:
                                pass
                        continue

                    # Check onchain whether this wallet currently wears the hat
                    is_wearer = await _is_wearer_of_hat(wallet, hat_id)

                    # Grant the role if wearing, revoke if not
                    if is_wearer and role not in member.roles:
                        try:
                            await member.add_roles(role, reason="Hats Protocol sync")
                            self.logger.info(f"Added role {role.name} to {member.display_name} (hat wearer)")
                        except discord.Forbidden:
                            self.logger.warning(f"Cannot add role {role.name} - missing permissions")
                    elif not is_wearer and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Hats Protocol sync - no longer wearing hat")
                            self.logger.info(f"Removed role {role.name} from {member.display_name}")
                        except discord.Forbidden:
                            pass

    @sync_roles_loop.before_loop
    async def before_sync(self):
        """Wait until the bot is fully connected before starting the sync loop."""
        await self.bot.wait_until_ready()

    # ── Commands ──

    @app_commands.command(
        name="hats",
        description="View the ZAO Hats Protocol tree structure"
    )
    async def hats(self, interaction: discord.Interaction):
        """Display the full ZAO hat tree as a formatted embed.

        Shows each hat with its active/inactive status, current and max supply,
        indented by depth. Also appends any configured hat-to-role mappings.
        """
        await interaction.response.defer()

        tree = await self._get_cached_tree()
        if not tree:
            await interaction.followup.send("Could not fetch the ZAO hats tree.", ephemeral=True)
            return

        embed = discord.Embed(
            title="\U0001f3a9 ZAO Hats Tree",
            description="Onchain org structure on Optimism via [Hats Protocol](https://app.hatsprotocol.xyz/trees/10/226)",
            color=0x57F287
        )

        # Render the tree as indented text lines (capped at 25 to fit embed limits)
        lines = self._format_tree(tree, max_lines=25)
        embed.add_field(name="Organization", value="\n".join(lines) or "Empty tree", inline=False)

        # Append hat-to-role sync mappings if any are configured
        mappings = self.role_mapping.get_all()
        if mappings:
            role_lines = []
            for hat_hex, m in list(mappings.items())[:10]:
                role_lines.append(f"\U0001f3a9 {m['hat_name']} \u2192 <@&{m['role_id']}>")
            embed.add_field(name="Role Sync", value="\n".join(role_lines), inline=False)

        embed.set_footer(text="Hats Protocol \u2022 Optimism \u2022 Tree 226")
        await interaction.followup.send(embed=embed)

    def _format_tree(self, nodes: list[dict], max_lines: int = 25) -> list[str]:
        """Format tree nodes into indented text lines for Discord embed display.

        Each line shows an active/inactive indicator, the hat name in bold,
        and the current/max supply. Indentation is controlled by the node's
        ``depth`` field. Recursion stops early once ``max_lines`` is reached
        to stay within Discord embed size limits.

        Args:
            nodes: List of tree node dicts (each may contain 'children').
            max_lines: Maximum number of output lines before truncating.

        Returns:
            A list of formatted strings, one per visible tree node.
        """
        lines = []
        for node in nodes:
            # Indent using em-spaces proportional to tree depth
            indent = "\u2003" * node['depth']
            status = "\u2705" if node['active'] else "\u274c"
            supply_text = f"({node['supply']}/{node['max_supply']})"
            lines.append(f"{indent}{status} **{node['name']}** {supply_text}")

            if len(lines) >= max_lines:
                lines.append("*... and more (use `/hat` to explore)*")
                return lines

            for child in node.get('children', []):
                child_lines = self._format_tree([child], max_lines - len(lines))
                lines.extend(child_lines)
                if len(lines) >= max_lines:
                    return lines
        return lines

    @app_commands.command(
        name="hat",
        description="View details about a specific hat in the ZAO tree"
    )
    @app_commands.describe(name="Hat name to search for (e.g. 'Entrepreneur', 'Community Manager')")
    async def hat_detail(self, interaction: discord.Interaction, name: str):
        """Search for a hat by name and display its detailed information.

        Performs a case-insensitive partial match against the cached tree.
        Shows description (from IPFS metadata), supply, active status,
        hat ID, sub-hats, and thumbnail image if available.

        Args:
            interaction: The Discord interaction context.
            name: Partial or full hat name to search for.
        """
        await interaction.response.defer()

        tree = await self._get_cached_tree()
        if not tree:
            await interaction.followup.send("Could not fetch the hats tree.", ephemeral=True)
            return

        # Search for the hat by name
        found = self._find_hat(tree, name.lower())
        if not found:
            await interaction.followup.send(
                f"No hat found matching \"{name}\". Try `/hats` to see the full tree.",
                ephemeral=True
            )
            return

        # Fetch fresh onchain data and IPFS metadata for the found hat
        hat_data = await _view_hat(found['id'])
        details_meta = await _fetch_ipfs_details(hat_data['details']) if hat_data else {}

        embed = discord.Embed(
            title=f"\U0001f3a9 {found['name']}",
            url=f"https://app.hatsprotocol.xyz/trees/10/226",
            color=0x57F287
        )

        # Use description from IPFS metadata, truncated to fit embed limits
        desc = ""
        if isinstance(details_meta, dict) and details_meta.get('description'):
            desc = details_meta['description'][:500]
        embed.description = desc or "*No description available*"

        embed.add_field(name="Supply", value=f"{found['supply']}/{found['max_supply']}", inline=True)
        embed.add_field(name="Active", value="\u2705 Yes" if found['active'] else "\u274c No", inline=True)
        embed.add_field(
            name="Hat ID",
            value=f"`{found['id_hex'][:18]}...`",
            inline=True
        )

        # List child hats (sub-roles) if any exist under this hat
        if found.get('children'):
            child_names = [c['name'] for c in found['children'][:10]]
            embed.add_field(
                name=f"Sub-hats ({len(found['children'])})",
                value=", ".join(child_names) or "None",
                inline=False
            )

        # Set the hat's image as the embed thumbnail if available
        image_url = _ipfs_to_http(found.get('image_uri', ''))
        if image_url:
            embed.set_thumbnail(url=image_url)

        embed.add_field(
            name="View on Hats",
            value=f"[Open in Hats App](https://app.hatsprotocol.xyz/trees/10/226)",
            inline=False
        )
        embed.set_footer(text="Hats Protocol \u2022 Optimism \u2022 Tree 226")

        await interaction.followup.send(embed=embed)

    def _find_hat(self, nodes: list[dict], query: str) -> dict | None:
        """Depth-first search of the tree for a hat whose name contains *query*.

        The search is case-insensitive (caller must pass a lowercased query).
        Returns the first matching node encountered in depth-first order.

        Args:
            nodes: List of tree node dicts to search (may contain 'children').
            query: Lowercased substring to match against hat names.

        Returns:
            The first matching node dict, or None if no match is found.
        """
        for node in nodes:
            if query in node.get('name', '').lower():
                return node
            # Recurse into children before moving to the next sibling
            found = self._find_hat(node.get('children', []), query)
            if found:
                return found
        return None

    @app_commands.command(
        name="myhats",
        description="See which ZAO hats you wear (requires registered wallet)"
    )
    @app_commands.describe(user="Member to check (default: yourself)")
    async def myhats(self, interaction: discord.Interaction, user: discord.Member = None):
        """Check which hats a member's registered wallet wears onchain.

        Looks up the target member's wallet via the bot's wallet registry,
        then walks the cached hat tree and calls ``isWearerOfHat`` for each
        hat with a non-zero supply.

        Args:
            interaction: The Discord interaction context.
            user: Optional member to check; defaults to the invoking user.
        """
        await interaction.response.defer(ephemeral=True)

        target = user or interaction.user
        registry = getattr(self.bot, 'wallet_registry', None)
        if not registry:
            await interaction.followup.send("Wallet system not available.", ephemeral=True)
            return

        wallet = registry.lookup(target)
        if not wallet:
            await interaction.followup.send(
                f"**{target.display_name}** doesn't have a registered wallet. Use `/register` first.",
                ephemeral=True
            )
            return

        tree = await self._get_cached_tree()
        if not tree:
            await interaction.followup.send("Could not fetch the hats tree.", ephemeral=True)
            return

        # Walk the tree and check each hat for wallet ownership
        worn_hats = []
        await self._check_hats_recursive(wallet, tree, worn_hats)

        embed = discord.Embed(
            title=f"\U0001f3a9 Hats for {target.display_name}",
            color=0x57F287
        )

        if worn_hats:
            lines = []
            for hat in worn_hats:
                lines.append(f"\u2705 **{hat['name']}**")
            embed.description = "\n".join(lines)
        else:
            embed.description = "*No hats found for this wallet.*"

        embed.add_field(
            name="Wallet",
            value=f"`{wallet[:6]}...{wallet[-4:]}`",
            inline=True
        )
        embed.add_field(
            name="Claim Hats",
            value="[Open Hats App](https://app.hatsprotocol.xyz/trees/10/226)",
            inline=True
        )
        embed.set_footer(text="Hats Protocol \u2022 Optimism \u2022 Tree 226")

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _check_hats_recursive(self, wallet: str, nodes: list[dict], results: list):
        """Recursively walk the tree and check if the wallet wears each hat.

        Only hats with ``supply > 0`` are checked (hats with zero supply
        cannot have any wearers, so the RPC call is skipped). Matching
        nodes are appended to ``results`` in-place.

        Args:
            wallet: The 0x-prefixed Ethereum address to check.
            nodes: List of tree node dicts to check.
            results: Accumulator list; matching nodes are appended here.
        """
        for node in nodes:
            # Skip hats with zero supply -- no one can be wearing them
            if node['supply'] > 0:
                is_wearer = await _is_wearer_of_hat(wallet, node['id'])
                if is_wearer:
                    results.append(node)
            for child in node.get('children', []):
                await self._check_hats_recursive(wallet, [child], results)

    @app_commands.command(
        name="claimhat",
        description="Get a link to claim a hat on the Hats Protocol app"
    )
    async def claimhat(self, interaction: discord.Interaction):
        """Send an informational embed with a link to claim hats on the Hats Protocol app."""
        embed = discord.Embed(
            title="\U0001f3a9 Claim a ZAO Hat",
            description=(
                "Hats represent roles and teams in the ZAO. "
                "Claim your hat on the Hats Protocol app to join a team.\n\n"
                "**[Open ZAO Hats Tree \u2192](https://app.hatsprotocol.xyz/trees/10/226)**\n\n"
                "After claiming, use `/myhats` to verify, and your Discord roles "
                "will auto-sync within 10 minutes."
            ),
            color=0x57F287
        )
        embed.set_footer(text="Hats Protocol \u2022 Optimism \u2022 Tree 226")
        await interaction.response.send_message(embed=embed)

    # ── Admin: Role sync management ──

    @app_commands.command(
        name="admin_link_hat",
        description="[ADMIN] Link a hat to a Discord role for auto-sync"
    )
    @app_commands.describe(
        hat_name="Name of the hat (for display)",
        hat_id="Hat ID in hex (from Hats app)",
        role="Discord role to sync with this hat"
    )
    async def admin_link_hat(self, interaction: discord.Interaction,
                             hat_name: str, hat_id: str, role: discord.Role):
        """Create a mapping between an onchain hat and a Discord role.

        Once linked, the background sync loop will automatically grant the
        role to members whose wallets wear the hat, and revoke it from those
        who do not. Restricted to Supreme Admin users.

        Args:
            interaction: The Discord interaction context.
            hat_name: Human-readable name for the hat (used in display).
            hat_id: The hat's hex ID from the Hats Protocol app.
            role: The Discord role to sync with this hat.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role.", ephemeral=True
            )
            return

        # Ensure the hat ID has a 0x prefix and is valid hex
        if not hat_id.startswith('0x'):
            hat_id = '0x' + hat_id

        try:
            int(hat_id, 16)
        except ValueError:
            await interaction.followup.send("Invalid hat ID format.", ephemeral=True)
            return

        self.role_mapping.set(hat_id, role.id, hat_name)

        await interaction.followup.send(
            f"\U0001f3a9 Linked **{hat_name}** (`{hat_id[:18]}...`) \u2192 {role.mention}\n"
            f"Role sync will run every 10 minutes.",
            ephemeral=True
        )

    @app_commands.command(
        name="admin_unlink_hat",
        description="[ADMIN] Remove a hat-to-role mapping"
    )
    @app_commands.describe(hat_id="Hat ID in hex to unlink")
    async def admin_unlink_hat(self, interaction: discord.Interaction, hat_id: str):
        """Remove an existing hat-to-role mapping. Restricted to Supreme Admin users.

        Args:
            interaction: The Discord interaction context.
            hat_id: The hex hat ID to unlink from its Discord role.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role.", ephemeral=True
            )
            return

        # Normalise to include the 0x prefix for consistent lookup
        if not hat_id.startswith('0x'):
            hat_id = '0x' + hat_id

        self.role_mapping.remove(hat_id)
        await interaction.followup.send(f"Unlinked hat `{hat_id[:18]}...`", ephemeral=True)

    @app_commands.command(
        name="admin_hat_roles",
        description="[ADMIN] List all hat-to-role mappings"
    )
    async def admin_hat_roles(self, interaction: discord.Interaction):
        """Display all configured hat-to-Discord-role mappings. Restricted to Supreme Admin users."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role.", ephemeral=True
            )
            return

        mappings = self.role_mapping.get_all()
        if not mappings:
            await interaction.followup.send("No hat-role mappings configured.", ephemeral=True)
            return

        embed = discord.Embed(
            title="\U0001f3a9 Hat \u2192 Role Mappings",
            color=0x57F287
        )

        # Build a line for each mapping showing hat name, truncated ID, and role mention
        lines = []
        for hat_hex, m in mappings.items():
            lines.append(
                f"**{m['hat_name']}** (`{hat_hex[:18]}...`)\n"
                f"\u2003\u2192 <@&{m['role_id']}>"
            )
        embed.description = "\n\n".join(lines)
        embed.set_footer(text="Sync runs every 10 minutes \u2022 Hats Protocol")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="admin_sync_hats",
        description="[ADMIN] Manually trigger hat-to-role sync now"
    )
    async def admin_sync_hats(self, interaction: discord.Interaction):
        """Manually trigger an immediate hat-to-role sync cycle.

        Runs the same logic as the periodic background loop but on-demand.
        Restricted to Supreme Admin users.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role.", ephemeral=True
            )
            return

        mappings = self.role_mapping.get_all()
        if not mappings:
            await interaction.followup.send("No hat-role mappings to sync.", ephemeral=True)
            return

        await interaction.followup.send("\u23f3 Syncing roles...", ephemeral=True)
        # Invoke the loop's coroutine directly for an immediate one-shot sync
        await self.sync_roles_loop.coro(self)
        await interaction.edit_original_response(content="\u2705 Role sync complete!")


async def setup(bot):
    """Entry point called by discord.py's extension loader to register the HatsCog."""
    await bot.add_cog(HatsCog(bot))
