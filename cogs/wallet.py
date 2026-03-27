"""
Wallet registration and ENS resolution cog for the ZAO Fractal Bot.

Provides two lookup tables that map Discord users to Ethereum wallet addresses,
backed by the ZAO OS ``users`` and ``respect_members`` Supabase tables:

1. **Discord ID wallets** -- permanent links created via ``/register`` or
   ``/admin_register``.  Stored in ``users.discord_id`` / ``users.primary_wallet``.
2. **Name wallets** -- pre-populated name-to-wallet mappings from the
   ``respect_members`` table, used as a fallback.  These are "fragile"
   because they break when a user changes their Discord name.

The ``WalletRegistry`` class is attached to the bot instance so other cogs
(proposals, hats, guide leaderboard) can look up wallets without importing
this module directly.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
import json
import os
import re
import aiohttp
from config.config import SUPREME_ADMIN_ROLE_ID
from cogs.base import BaseCog
from utils.supabase_client import get_supabase

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


# ---------------------------------------------------------------------------
# Ethereum / ENS validation helpers
# ---------------------------------------------------------------------------

def is_valid_address(address: str) -> bool:
    """Return ``True`` if *address* matches the 0x-prefixed, 40-hex-char format."""
    return bool(re.match(r'^0x[0-9a-fA-F]{40}$', address))


def is_ens_name(name: str) -> bool:
    """Return ``True`` if *name* looks like an ENS domain (e.g. ``vitalik.eth``)."""
    return bool(re.match(r'^[a-zA-Z0-9\-]+\.eth$', name.strip()))


async def resolve_ens(name: str) -> str | None:
    """Resolve an ENS name to a checksummed Ethereum address.

    Tries two strategies in order:
    1. Direct onchain resolution via the ENS Universal Resolver contract,
       called through Cloudflare's public Ethereum RPC.
    2. Fallback to the ensdata.net REST API if the onchain call fails.

    Returns ``None`` if both strategies fail.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Strategy 1: onchain resolution via Cloudflare ETH gateway.
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{
                    "to": "0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63",  # ENS Universal Resolver
                    "data": _encode_resolve(name)
                }, "latest"]
            }
            async with session.post("https://cloudflare-eth.com", json=payload) as resp:
                data = await resp.json()
                result = data.get("result", "0x")
                if result and result != "0x" and len(result) >= 66:
                    # The resolved address occupies the first 32-byte ABI word
                    # (right-aligned), so bytes 6..66 after the "0x" prefix.
                    address = "0x" + result[26:66]
                    if is_valid_address(address) and address != "0x0000000000000000000000000000000000000000":
                        return address

        # Strategy 2: REST API fallback.
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.ensdata.net/{name}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    address = data.get("address")
                    if address and is_valid_address(address):
                        return address
    except Exception as e:
        logging.getLogger('bot').error(f"ENS resolution failed for {name}: {e}")

    return None


def _encode_resolve(name: str) -> str:
    """Build the ABI-encoded calldata for ``resolve(bytes,bytes)`` on the ENS Universal Resolver.

    The inner call is ``addr(bytes32 namehash)`` (selector ``0x3b3b57de``).
    The outer call wraps that inside ``resolve(bytes dnsName, bytes data)``
    (selector ``0x9061b923``).
    """
    import hashlib

    # Step 1: DNS-wire-format encode the name (length-prefixed labels + null terminator).
    dns_encoded = b""
    for label in name.split("."):
        encoded_label = label.encode("utf-8")
        dns_encoded += bytes([len(encoded_label)]) + encoded_label
    dns_encoded += b"\x00"

    # Step 2: Build the inner calldata -- addr(namehash).
    namehash = _namehash(name)
    inner_data = bytes.fromhex("3b3b57de") + bytes.fromhex(namehash[2:])

    # Step 3: ABI-encode resolve(bytes, bytes).
    selector = "9061b923"

    # Pad both dynamic byte arrays to 32-byte boundaries for ABI encoding.
    dns_padded_len = ((len(dns_encoded) + 31) // 32) * 32
    inner_padded_len = ((len(inner_data) + 31) // 32) * 32

    offset1 = 64  # byte offset to dns_encoded length word
    offset2 = offset1 + 32 + dns_padded_len  # byte offset to inner_data length word

    result = selector
    result += offset1.to_bytes(32, "big").hex()
    result += offset2.to_bytes(32, "big").hex()
    # First dynamic param: dns_encoded
    result += len(dns_encoded).to_bytes(32, "big").hex()
    result += dns_encoded.hex().ljust(dns_padded_len * 2, "0")
    # Second dynamic param: inner_data
    result += len(inner_data).to_bytes(32, "big").hex()
    result += inner_data.hex().ljust(inner_padded_len * 2, "0")

    return "0x" + result


def _namehash(name: str) -> str:
    """Compute the ENS namehash (EIP-137) for *name*.

    Requires Keccak-256, which is *not* the same as Python's built-in
    ``hashlib.sha3_256`` (that's NIST SHA-3).  The function tries three
    backends in order of correctness:
    1. ``pysha3`` (provides real Keccak)
    2. ``pycryptodome`` (``Crypto.Hash.keccak``)
    3. stdlib ``hashlib.sha3_256`` as a last resort -- technically wrong,
       but the ensdata.net REST fallback in ``resolve_ens`` will still
       succeed even if the onchain resolution doesn't.
    """
    from hashlib import new as hashlib_new
    def keccak256(data: bytes) -> bytes:
        return hashlib_new("sha3_256", data, usedforsecurity=False).digest()

    try:
        import sha3  # pysha3 provides real keccak
        def keccak256(data: bytes) -> bytes:
            k = sha3.keccak_256()
            k.update(data)
            return k.digest()
    except ImportError:
        try:
            from Crypto.Hash import keccak as _keccak
            def keccak256(data: bytes) -> bytes:
                return _keccak.new(digest_bits=256, data=data).digest()
        except ImportError:
            # Last resort: wrong hash but the REST fallback covers us.
            import hashlib
            def keccak256(data: bytes) -> bytes:
                return hashlib.sha3_256(data).digest()

    # Namehash algorithm: start with 32 zero bytes, then iteratively hash
    # ``node + keccak256(label)`` for each label from right to left.
    node = b"\x00" * 32
    if name:
        labels = name.split(".")
        for label in reversed(labels):
            label_hash = keccak256(label.encode("utf-8"))
            node = keccak256(node + label_hash)
    return "0x" + node.hex()


# ---------------------------------------------------------------------------
# Wallet Registry (dual-table lookup)
# ---------------------------------------------------------------------------

class WalletRegistry:
    """Two-tier lookup from Discord users to Ethereum wallet addresses.

    Tier 1 (permanent): Discord snowflake ID -> wallet, stored in the ZAO OS
    ``users`` table (``discord_id`` / ``primary_wallet`` columns).
    Tier 2 (fragile):   Display name -> wallet, stored in the ``respect_members``
    table (``name`` / ``wallet_address`` columns).

    ``lookup()`` checks Tier 1 first, then falls back to Tier 2 using the
    member's display name, username, and global name.  Admins can promote
    Tier 2 matches to Tier 1 with ``/admin_lock_wallets``.
    """

    def __init__(self):
        self.logger = logging.getLogger('bot')
        self.sb = get_supabase()

    def register(self, discord_id: int, wallet: str) -> None:
        """Create or update a permanent Discord-ID -> wallet link.

        Strategy:
        1. If a user row exists with this wallet as primary_wallet, update
           its discord_id.
        2. Else if a user row exists with this discord_id, update its
           primary_wallet.
        3. Otherwise insert a brand-new user row.
        """
        did = str(discord_id)
        # Try to find existing user by wallet
        by_wallet = (
            self.sb.table("users")
            .select("id")
            .eq("primary_wallet", wallet)
            .maybe_single()
            .execute()
        )
        if by_wallet.data:
            self.sb.table("users").update(
                {"discord_id": did}
            ).eq("id", by_wallet.data["id"]).execute()
            return

        # Try to find existing user by discord_id
        by_did = (
            self.sb.table("users")
            .select("id")
            .eq("discord_id", did)
            .maybe_single()
            .execute()
        )
        if by_did.data:
            self.sb.table("users").update(
                {"primary_wallet": wallet}
            ).eq("id", by_did.data["id"]).execute()
            return

        # No existing user -- insert new row
        self.sb.table("users").insert(
            {"discord_id": did, "primary_wallet": wallet}
        ).execute()

    def get_by_discord_id(self, discord_id: int) -> str | None:
        """Return wallet for *discord_id*, or ``None`` if not registered."""
        result = (
            self.sb.table("users")
            .select("primary_wallet")
            .eq("discord_id", str(discord_id))
            .maybe_single()
            .execute()
        )
        if result.data:
            return result.data["primary_wallet"] or None
        return None

    def get_by_name(self, display_name: str) -> str | None:
        """Case-insensitive lookup against the respect_members name table."""
        if not display_name:
            return None
        result = (
            self.sb.table("respect_members")
            .select("wallet_address")
            .ilike("name", display_name.strip())
            .maybe_single()
            .execute()
        )
        if result.data:
            return result.data["wallet_address"]
        return None

    def lookup(self, member: discord.Member) -> str | None:
        """Best-effort wallet lookup: ID -> display name -> username -> global name.

        Returns the first match found, or ``None``.
        """
        # Tier 1: permanent Discord-ID link (from /register)
        wallet = self.get_by_discord_id(member.id)
        if wallet:
            return wallet

        # Tier 2: fragile name-based matching (display name, username, global name)
        wallet = self.get_by_name(member.display_name)
        if wallet:
            return wallet

        wallet = self.get_by_name(member.name)
        if wallet:
            return wallet

        if member.global_name:
            wallet = self.get_by_name(member.global_name)
            if wallet:
                return wallet

        return None

    def get_all_discord(self) -> dict:
        """Return all Discord-ID -> wallet mappings as a dict."""
        result = (
            self.sb.table("users")
            .select("discord_id, primary_wallet")
            .not_.is_("discord_id", "null")
            .not_.is_("primary_wallet", "null")
            .execute()
        )
        return {row["discord_id"]: row["primary_wallet"] for row in result.data}

    def get_all_names(self) -> dict:
        """Return all name -> wallet mappings from respect_members."""
        result = (
            self.sb.table("respect_members")
            .select("name, wallet_address")
            .execute()
        )
        return {row["name"]: row["wallet_address"] for row in result.data}

    def add_name_mapping(self, name: str, wallet: str) -> None:
        """Insert or update a name -> wallet entry in respect_members."""
        self.sb.table("respect_members").upsert(
            {"name": name.strip(), "wallet_address": wallet},
            on_conflict="name",
        ).execute()

    def lock_wallet(self, name: str, discord_id: int) -> bool:
        """Promote a name-matched wallet to a permanent Discord-ID link.

        Looks up the wallet from respect_members by name, then updates
        (or creates) the corresponding users row with the discord_id.

        Returns True if a wallet was found and locked, False otherwise.
        """
        wallet = self.get_by_name(name)
        if not wallet:
            return False
        self.register(discord_id, wallet)
        return True

    def stats(self) -> dict:
        """Return summary counts for the admin dashboard."""
        discord_result = (
            self.sb.table("users")
            .select("id", count="exact")
            .not_.is_("discord_id", "null")
            .not_.is_("primary_wallet", "null")
            .execute()
        )
        name_result = (
            self.sb.table("respect_members")
            .select("id", count="exact")
            .execute()
        )
        empty_wallet_result = (
            self.sb.table("respect_members")
            .select("id", count="exact")
            .or_("wallet_address.is.null,wallet_address.eq.")
            .execute()
        )
        return {
            'discord_linked': discord_result.count or 0,
            'name_entries': name_result.count or 0,
            'names_without_wallet': empty_wallet_result.count or 0,
        }


class WalletCog(BaseCog):
    """Discord slash-command interface for wallet registration and admin tools.

    Exposes ``/register``, ``/wallet``, ``/admin_register``, ``/admin_wallets``,
    ``/admin_lookup``, ``/admin_match_all``, and ``/admin_lock_wallets``.

    On init the ``WalletRegistry`` singleton is attached to ``bot.wallet_registry``
    so other cogs (proposals, hats, history) can resolve wallets without a
    direct import dependency.
    """

    def __init__(self, bot):
        super().__init__(bot)
        self.registry = WalletRegistry()
        # Attach to the bot instance for cross-cog access.
        bot.wallet_registry = self.registry

    @app_commands.command(
        name="register",
        description="Register your Ethereum wallet or ENS name for onchain Respect"
    )
    @app_commands.describe(wallet="Your Ethereum wallet address (0x...) or ENS name (e.g. vitalik.eth)")
    async def register(self, interaction: discord.Interaction, wallet: str):
        """Link the calling user's Discord account to an Ethereum address.

        Accepts either a raw ``0x`` address or an ENS name (resolved on the fly).
        The mapping is stored permanently by Discord ID.
        """
        await interaction.response.defer(ephemeral=True)

        wallet = wallet.strip()

        # Check if it's an ENS name
        if is_ens_name(wallet):
            ens_name = wallet
            resolved = await resolve_ens(ens_name)
            if not resolved:
                await interaction.followup.send(
                    f"❌ Could not resolve ENS name `{ens_name}`. Make sure it exists and has an address set.",
                    ephemeral=True
                )
                return
            wallet = resolved
            short = f"{wallet[:6]}...{wallet[-4:]}"
            self.registry.register(interaction.user.id, wallet)
            await interaction.followup.send(
                f"✅ ENS `{ens_name}` resolved and registered: `{short}`\n"
                f"Your fractal results will now link to this address for onchain submission.",
                ephemeral=True
            )
            return

        if not is_valid_address(wallet):
            await interaction.followup.send(
                "❌ Invalid input. Provide a wallet address (`0x...`) or an ENS name (`name.eth`).",
                ephemeral=True
            )
            return

        self.registry.register(interaction.user.id, wallet)
        short = f"{wallet[:6]}...{wallet[-4:]}"
        await interaction.followup.send(
            f"✅ Wallet registered: `{short}`\n"
            f"Your fractal results will now link to this address for onchain submission.",
            ephemeral=True
        )

    @app_commands.command(
        name="wallet",
        description="Show your registered wallet address"
    )
    async def wallet(self, interaction: discord.Interaction):
        """Display the calling user's linked wallet and how it was matched."""
        await interaction.response.defer(ephemeral=True)

        wallet = self.registry.lookup(interaction.user)
        if wallet:
            source = "Discord ID" if self.registry.get_by_discord_id(interaction.user.id) else "name match"
            await interaction.followup.send(
                f"🔗 Your wallet: `{wallet}`\n(matched via {source})",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ No wallet found. Use `/register 0xYourAddress` to link one.",
                ephemeral=True
            )

    @app_commands.command(
        name="admin_register",
        description="[ADMIN] Register a wallet or ENS for another user"
    )
    @app_commands.describe(user="Discord user", wallet="Their Ethereum wallet address or ENS name")
    async def admin_register(self, interaction: discord.Interaction, user: discord.Member, wallet: str):
        """Admin-only: create a permanent wallet link on behalf of another member."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        wallet = wallet.strip()

        if is_ens_name(wallet):
            ens_name = wallet
            resolved = await resolve_ens(ens_name)
            if not resolved:
                await interaction.followup.send(f"❌ Could not resolve ENS name `{ens_name}`.", ephemeral=True)
                return
            wallet = resolved
            short = f"{wallet[:6]}...{wallet[-4:]}"
            self.registry.register(user.id, wallet)
            await interaction.followup.send(
                f"✅ ENS `{ens_name}` resolved → `{short}` registered for {user.mention}",
                ephemeral=True
            )
            return

        if not is_valid_address(wallet):
            await interaction.followup.send("❌ Invalid input. Provide a wallet address (`0x...`) or ENS name (`name.eth`).", ephemeral=True)
            return

        self.registry.register(user.id, wallet)
        short = f"{wallet[:6]}...{wallet[-4:]}"
        await interaction.followup.send(
            f"✅ Registered `{short}` for {user.mention}",
            ephemeral=True
        )

    @app_commands.command(
        name="admin_wallets",
        description="[ADMIN] List all wallet registrations and stats"
    )
    async def admin_wallets(self, interaction: discord.Interaction):
        """Admin-only: display a summary of all wallet registrations (capped at 20)."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        stats = self.registry.stats()
        discord_wallets = self.registry.get_all_discord()

        msg = f"# 🔗 Wallet Registry\n\n"
        msg += f"**Discord-linked:** {stats['discord_linked']}\n"
        msg += f"**Name entries:** {stats['name_entries']}\n\n"

        if discord_wallets:
            msg += "**Discord ID Registrations:**\n"
            for did, wallet in list(discord_wallets.items())[:20]:
                short = f"{wallet[:6]}...{wallet[-4:]}"
                try:
                    member = interaction.guild.get_member(int(did))
                    name = member.display_name if member else f"ID:{did}"
                except (ValueError, AttributeError):
                    name = f"ID:{did}"
                msg += f"• {name}: `{short}`\n"

            if len(discord_wallets) > 20:
                msg += f"\n... and {len(discord_wallets) - 20} more\n"

        msg += f"\n**Name lookup** has {stats['name_entries']} entries ready for auto-matching."

        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name="admin_lookup",
        description="[ADMIN] Look up a user's wallet (checks both ID and name)"
    )
    @app_commands.describe(user="Discord user to look up")
    async def admin_lookup(self, interaction: discord.Interaction, user: discord.Member):
        """Admin-only: show how a specific user's wallet was resolved (ID vs name match)."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        wallet = self.registry.lookup(user)
        if wallet:
            by_id = self.registry.get_by_discord_id(user.id)
            by_name = self.registry.get_by_name(user.display_name) or self.registry.get_by_name(user.name)
            source = "Discord ID" if by_id else f"name match ({user.display_name})"
            await interaction.followup.send(
                f"🔗 {user.mention}: `{wallet}`\n(matched via {source})",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ No wallet found for {user.mention}\n"
                f"Display name: `{user.display_name}`\n"
                f"Username: `{user.name}`\n"
                f"Global name: `{user.global_name}`",
                ephemeral=True
            )

    @app_commands.command(
        name="admin_match_all",
        description="[ADMIN] Auto-match all server members to wallets — saves full report to file"
    )
    async def admin_match_all(self, interaction: discord.Interaction):
        """Admin-only: iterate every guild member, try to match a wallet, and
        produce a full text + JSON report uploaded as a file attachment.

        Members are bucketed into three categories:
        - *by_discord_id*: permanently linked via ``/register``
        - *by_name_match*: matched by display/user/global name (fragile)
        - *no_wallet*: no match found at all
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        matched_by_id = []      # Already linked via /register
        matched_by_name = []    # Matched via name lookup
        unmatched = []          # No wallet found

        # Also build data for JSON export
        report_data = {"by_discord_id": [], "by_name_match": [], "no_wallet": []}

        for member in interaction.guild.members:
            if member.bot:
                continue

            # Check Discord ID match first (from /register)
            id_wallet = self.registry.get_by_discord_id(member.id)
            if id_wallet:
                short = f"{id_wallet[:6]}...{id_wallet[-4:]}"
                matched_by_id.append(f"[ID]  {member.display_name:<30} ({member.name:<25}) -> {short}")
                report_data["by_discord_id"].append({
                    "discord_id": str(member.id),
                    "display_name": member.display_name,
                    "username": member.name,
                    "wallet": id_wallet
                })
                continue

            # Check name match (display_name, username, global_name)
            name_wallet = self.registry.get_by_name(member.display_name) or \
                          self.registry.get_by_name(member.name) or \
                          (self.registry.get_by_name(member.global_name) if member.global_name else None)
            if name_wallet:
                short = f"{name_wallet[:6]}...{name_wallet[-4:]}"
                matched_name = member.display_name
                if not self.registry.get_by_name(member.display_name):
                    matched_name = member.name if self.registry.get_by_name(member.name) else member.global_name
                matched_by_name.append(f"[NAME] {member.display_name:<30} ({member.name:<25}) -> {short}  (matched: \"{matched_name}\")")
                report_data["by_name_match"].append({
                    "discord_id": str(member.id),
                    "display_name": member.display_name,
                    "username": member.name,
                    "global_name": member.global_name,
                    "matched_via": matched_name,
                    "wallet": name_wallet
                })
            else:
                unmatched.append(f"[NONE] {member.display_name:<30} ({member.name:<25})  global: {member.global_name or 'n/a'}")
                report_data["no_wallet"].append({
                    "discord_id": str(member.id),
                    "display_name": member.display_name,
                    "username": member.name,
                    "global_name": member.global_name
                })

        # Save JSON report to data/
        report_path = os.path.join(DATA_DIR, 'wallet_match_report.json')
        with open(report_path, 'w') as f:
            json.dump(report_data, f, indent=2)

        # Build full text report
        lines = []
        lines.append("=" * 100)
        lines.append("WALLET MATCH REPORT")
        lines.append(f"Server: {interaction.guild.name}")
        lines.append(f"Total members: {len(matched_by_id) + len(matched_by_name) + len(unmatched)}")
        lines.append(f"By Discord ID: {len(matched_by_id)}  |  By Name: {len(matched_by_name)}  |  No Match: {len(unmatched)}")
        lines.append("=" * 100)

        lines.append("")
        lines.append(f"--- LINKED VIA /register ({len(matched_by_id)}) - PERMANENT ---")
        for m in matched_by_id:
            lines.append(m)

        lines.append("")
        lines.append(f"--- MATCHED BY NAME ({len(matched_by_name)}) - FRAGILE (run /admin_lock_wallets to make permanent) ---")
        for m in matched_by_name:
            lines.append(m)

        lines.append("")
        lines.append(f"--- NO WALLET FOUND ({len(unmatched)}) - Need /register or manual link ---")
        for u in unmatched:
            lines.append(u)

        full_report = "\n".join(lines)

        # Save text report too
        txt_path = os.path.join(DATA_DIR, 'wallet_match_report.txt')
        with open(txt_path, 'w') as f:
            f.write(full_report)

        # Send as file attachment + summary
        import io
        file_buffer = io.BytesIO(full_report.encode('utf-8'))
        file = discord.File(file_buffer, filename="wallet_match_report.txt")

        summary = (
            f"# Wallet Match Report\n\n"
            f"**By Discord ID (permanent):** {len(matched_by_id)}\n"
            f"**By Name (fragile):** {len(matched_by_name)}\n"
            f"**No Match:** {len(unmatched)}\n\n"
            f"Full report attached below + saved to `data/wallet_match_report.txt` and `data/wallet_match_report.json`"
        )

        await interaction.followup.send(summary, file=file, ephemeral=True)

    @app_commands.command(
        name="admin_lock_wallets",
        description="[ADMIN] Lock all name-matched wallets to Discord IDs (makes them permanent)"
    )
    async def admin_lock_wallets(self, interaction: discord.Interaction):
        """Admin-only: promote every Tier-2 (name) wallet match to a permanent
        Tier-1 (Discord ID) registration.

        This prevents matches from breaking when users change their display names.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        locked = []
        already_linked = 0
        skipped_empty = 0

        for member in interaction.guild.members:
            if member.bot:
                continue

            # Skip if already linked by Discord ID
            if self.registry.get_by_discord_id(member.id):
                already_linked += 1
                continue

            # Try name match
            wallet = self.registry.get_by_name(member.display_name) or \
                     self.registry.get_by_name(member.name) or \
                     (self.registry.get_by_name(member.global_name) if member.global_name else None)

            if wallet:
                # Found a name match with a real wallet — lock it to their Discord ID
                self.registry.register(member.id, wallet)
                short = f"{wallet[:6]}...{wallet[-4:]}"
                locked.append(f"✅ **{member.display_name}** → `{short}`")
            elif wallet == "":
                skipped_empty += 1

        msg = f"# 🔒 Wallet Lock Results\n\n"
        msg += f"**Newly locked:** {len(locked)}\n"
        msg += f"**Already linked:** {already_linked}\n"
        msg += f"**Skipped (no wallet):** {skipped_empty}\n\n"

        if locked:
            msg += "**Locked to Discord ID:**\n"
            for l in locked[:30]:
                msg += f"{l}\n"
            if len(locked) > 30:
                msg += f"... +{len(locked) - 30} more\n"

        msg += f"\nThese {len(locked)} members now have permanent Discord ID → wallet links that won't break if they change their display name."

        if len(msg) > 1950:
            msg = msg[:1950] + "\n... (truncated)"

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    """Entry point called by ``bot.load_extension('cogs.wallet')``."""
    await bot.add_cog(WalletCog(bot))
