"""
Proposal system cog for the ZAO Fractal Discord bot.

This module implements a complete on-chain-governance-inspired proposal and voting
system. Community members can create proposals of various types (text, governance,
funding, curation), and other members vote using Discord buttons. Votes are weighted
by each voter's on-chain Respect token balance (queried from the Optimism network),
ensuring that voting power reflects community standing.

Key components:
    - RespectBalance: Queries OG Respect (ERC-20) and ZOR Respect (ERC-1155) balances
      from the Optimism blockchain, with a 5-minute TTL cache to reduce RPC calls.
    - ProposalStore: A JSON-file-backed data store that handles creation, retrieval,
      voting, closing, and deletion of proposals. All mutations are atomically saved.
    - ProposalVoteView / GovernanceVoteView: Persistent Discord UI button views that
      survive bot restarts by encoding the proposal ID into each button's custom_id.
    - ProposalsCog: The discord.py cog that wires everything together, including slash
      commands (/propose, /curate, /proposals, /proposal, admin commands), background
      tasks for automatic 7-day expiry, startup catch-up expiry, and button migration.

Proposal lifecycle:
    1. A user runs /propose or /curate, which creates a thread in the proposals channel,
       stores the proposal as "active", and posts an embed with voting buttons.
    2. Members click voting buttons; votes are recorded with Respect-weighted values.
       The embed is updated live after each vote to reflect the current tally.
    3. After 7 days, a background task automatically closes the proposal, posts final
       results, and updates the proposals index.
    4. Admins can manually close, delete, reopen, or recover proposals at any time.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import re
import html
import logging
import time
import aiohttp
from datetime import datetime, timedelta, timezone
from cogs.base import BaseCog
from config.config import PROPOSAL_TYPES, MAX_PROPOSAL_OPTIONS, PROPOSALS_CHANNEL_ID

# Path to the JSON file that persists all proposal data between bot restarts
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
PROPOSALS_FILE = os.path.join(DATA_DIR, 'proposals.json')


def _parse_utc(iso_str: str) -> datetime:
    """Parse an ISO datetime string, ensuring the result is timezone-aware (UTC).

    Older proposal records may have been stored without timezone info. This
    function treats any naive datetime as UTC to prevent comparison errors
    between naive and aware datetime objects.

    Args:
        iso_str: An ISO 8601 formatted datetime string (e.g. '2025-01-15T12:00:00').

    Returns:
        A timezone-aware datetime in UTC.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# ── On-chain contract addresses on Optimism mainnet ──
# OG Respect is an ERC-20 token (18 decimals) representing legacy community standing
OG_RESPECT_ADDRESS = '0x34cE89baA7E4a4B00E17F7E4C0cb97105C216957'
# ZOR Respect is an ERC-1155 token (token ID 0, no decimals) for newer respect scores
ZOR_RESPECT_ADDRESS = '0x9885CCeEf7E8371Bf8d6f2413723D25917E7445c'
ZOR_TOKEN_ID = 0

# Default public Optimism RPC; can be overridden via ALCHEMY_OPTIMISM_RPC env var
DEFAULT_OPTIMISM_RPC = 'https://mainnet.optimism.io'

# ── Embed display constants ──
# Maps proposal type keys to emoji and human-readable labels for Discord embeds
TYPE_EMOJIS = {'text': '\U0001f4dd', 'governance': '\u2696\ufe0f', 'funding': '\U0001f4b0', 'curate': '\U0001f3a8'}
TYPE_LABELS = {'text': 'Text', 'governance': 'Governance', 'funding': 'Funding', 'curate': 'Curation'}


class RespectBalance:
    """Queries on-chain Respect token balances from the Optimism network with caching.

    Supports two token contracts:
        - OG Respect (ERC-20, 18 decimals) at ``OG_RESPECT_ADDRESS``
        - ZOR Respect (ERC-1155, token ID 0, integer) at ``ZOR_RESPECT_ADDRESS``

    Balances are cached in memory for 5 minutes (configurable via ``_cache_ttl``)
    to avoid hitting the RPC endpoint on every vote. A single module-level
    instance (``_respect_balance``) is shared across all voting interactions.
    """

    def __init__(self):
        self.logger = logging.getLogger('bot')
        # In-memory cache keyed by lowercase wallet address.
        # Each entry stores {og, zor, total, timestamp} to avoid redundant RPC calls.
        self._cache = {}
        self._cache_ttl = 300  # Cache entries expire after 5 minutes

    def _get_rpc_url(self) -> str:
        """Return the Optimism RPC URL, preferring a user-supplied Alchemy key."""
        return os.getenv('ALCHEMY_OPTIMISM_RPC', DEFAULT_OPTIMISM_RPC)

    async def get_total_respect(self, wallet: str) -> float:
        """Get total Respect (OG + ZOR) for a wallet, with caching.

        The total is the sum of the user's OG Respect (ERC-20, 18 decimals)
        and ZOR Respect (ERC-1155, integer). Results are cached for 5 minutes
        to avoid hitting the RPC on every vote.

        Returns 0.0 if the wallet is empty, not found, or if any RPC call fails.
        """
        if not wallet:
            return 0.0

        wallet = wallet.lower()
        # Return cached value if it hasn't expired
        cached = self._cache.get(wallet)
        if cached and time.time() - cached['timestamp'] < self._cache_ttl:
            return cached['total']

        try:
            # Query both token contracts in sequence and sum the balances
            og = await self._query_erc20_balance(wallet, OG_RESPECT_ADDRESS)
            zor = await self._query_erc1155_balance(wallet, ZOR_RESPECT_ADDRESS, ZOR_TOKEN_ID)
            total = og + zor

            self._cache[wallet] = {
                'og': og, 'zor': zor, 'total': total,
                'timestamp': time.time()
            }
            return total
        except Exception as e:
            self.logger.error(f"Failed to query Respect for {wallet}: {e}")
            return 0.0

    async def _eth_call(self, to: str, data: str) -> str:
        """Execute a read-only eth_call against an Optimism smart contract.

        Sends a JSON-RPC ``eth_call`` request to the configured Optimism RPC
        endpoint. This is a read-only operation that does not create a
        transaction or cost gas.

        Args:
            to: The contract address to call (checksummed or lowercase hex).
            data: The ABI-encoded call data (function selector + arguments).

        Returns:
            The hex-encoded return value from the contract, or "0x" on failure.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self._get_rpc_url(), json=payload) as resp:
                result = await resp.json()
                return result.get("result", "0x")

    async def _query_erc20_balance(self, wallet: str, contract: str) -> float:
        """Query ERC-20 balanceOf -- returns balance as float (18 decimals).

        Constructs the ABI-encoded call data for ``balanceOf(address)``
        (selector 0x70a08231) with the wallet address zero-padded to 32 bytes.
        """
        # Strip '0x' prefix and left-pad the address to 32 bytes for ABI encoding
        addr_padded = wallet[2:].lower().zfill(64)
        data = f"0x70a08231{addr_padded}"
        result = await self._eth_call(contract, data)
        if result and result != "0x" and len(result) >= 66:
            raw = int(result, 16)
            return raw / 1e18  # Convert from wei-like 18-decimal representation
        return 0.0

    async def _query_erc1155_balance(self, wallet: str, contract: str, token_id: int) -> float:
        """Query ERC-1155 balanceOf -- returns balance as integer (no decimals).

        Constructs ABI-encoded call data for ``balanceOf(address, uint256)``
        (selector 0x00fdd58e) with the wallet address and token ID each
        zero-padded to 32 bytes.
        """
        # ABI-encode both arguments: address and token ID, each as 32-byte words
        addr_padded = wallet[2:].lower().zfill(64)
        id_padded = hex(token_id)[2:].zfill(64)
        data = f"0x00fdd58e{addr_padded}{id_padded}"
        result = await self._eth_call(contract, data)
        if result and result != "0x" and len(result) >= 66:
            return float(int(result, 16))
        return 0.0


# Module-level singleton so the in-memory cache is shared across all vote
# interactions throughout the bot's lifetime.
_respect_balance = RespectBalance()


async def _get_vote_weight(bot, user: discord.User) -> float:
    """Look up the user's registered wallet and return their total Respect as vote weight.

    The bot's wallet_registry (provided by the wallets cog) maps Discord users
    to Ethereum addresses. If the user has no registered wallet or holds zero
    Respect, returns 0.0 -- which prevents them from voting.
    """
    wallet = None
    if hasattr(bot, 'wallet_registry'):
        wallet = bot.wallet_registry.lookup(user)

    if not wallet:
        return 0.0

    return await _respect_balance.get_total_respect(wallet)


async def _scrape_og_tags(url: str) -> dict:
    """Best-effort scrape of Open Graph meta tags from a URL.

    Fetches the page HTML (with a 5-second timeout) and uses regex to extract
    ``og:title``, ``og:description``, and ``og:image`` meta tag values. These
    are used by the /curate command to auto-populate proposal metadata when the
    user provides a project URL instead of a name.

    Handles both common attribute orderings for ``<meta>`` tags:
        - ``<meta property="og:title" content="...">``
        - ``<meta content="..." property="og:title">``

    Args:
        url: The URL to fetch and scrape.

    Returns:
        A dict with any found keys ('title', 'description', 'image') mapped
        to their unescaped string values. Returns an empty dict on any error.
    """
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5),
                                   headers={'User-Agent': 'Mozilla/5.0 (compatible; ZAOBot/1.0)'}) as resp:
                if resp.status != 200:
                    return result
                html = await resp.text()
                # Extract each OG tag we care about via regex (avoids a full HTML parser dependency)
                for tag in ['title', 'description', 'image']:
                    # Try standard attribute order: property/name first, then content
                    match = re.search(
                        rf'<meta\s+(?:property|name)=["\']og:{tag}["\']\s+content=["\']([^"\']+)["\']',
                        html, re.IGNORECASE
                    )
                    if not match:
                        # Some sites put content before property -- try reversed order
                        match = re.search(
                            rf'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:{tag}["\']',
                            html, re.IGNORECASE
                        )
                    if match:
                        # Unescape HTML entities like &amp; in the extracted value
                        result[tag] = html.unescape(match.group(1))
    except Exception:
        pass  # Silently return empty dict -- scraping is best-effort only
    return result


class ProposalStore:
    """JSON-backed persistent store for proposals with Respect-weighted voting.

    All proposal data is held in memory as a dict and written atomically to
    ``data/proposals.json`` on every mutation. The data structure contains:
        - ``next_id``: Auto-incrementing integer counter for proposal IDs.
        - ``proposals``: Dict mapping string proposal IDs to proposal dicts.
        - ``_index_message_id``: The Discord message ID of the pinned index
          embed in the proposals channel (used for in-place editing).

    Each proposal dict contains: id, title, description, type, author_id,
    thread_id, message_id, status ('active'/'closed'), votes (dict keyed by
    user ID), options (list), funding_amount, image_url, project_url,
    created_at, and optionally closed_at.
    """

    def __init__(self):
        self.logger = logging.getLogger('bot')
        # Default data structure: auto-incrementing ID counter, proposals dict, and
        # the Discord message ID of the pinned proposals index embed.
        self._data = {'next_id': 1, 'proposals': {}, '_index_message_id': None}
        self._load()

    def _load(self):
        """Load proposal data from disk if the JSON file exists."""
        if os.path.exists(PROPOSALS_FILE):
            with open(PROPOSALS_FILE, 'r') as f:
                self._data = json.load(f)
            # Migrate: ensure _index_message_id exists for older data files
            if '_index_message_id' not in self._data:
                self._data['_index_message_id'] = None

    def _save(self):
        """Persist proposal data to disk using atomic write to prevent corruption."""
        from utils.safe_json import atomic_save
        atomic_save(PROPOSALS_FILE, self._data)

    @property
    def index_message_id(self) -> int | None:
        """Get the Discord message ID of the pinned active-proposals index embed."""
        mid = self._data.get('_index_message_id')
        return int(mid) if mid else None

    @index_message_id.setter
    def index_message_id(self, value: int | None):
        """Set (and persist) the Discord message ID of the pinned index embed."""
        self._data['_index_message_id'] = str(value) if value else None
        self._save()

    def create(self, title: str, description: str, proposal_type: str,
               author_id: int, thread_id: int, message_id: int,
               options: list[str] | None = None,
               funding_amount: float | None = None,
               image_url: str | None = None,
               project_url: str | None = None) -> dict:
        """Create a new proposal and persist it to disk.

        Assigns an auto-incrementing ID, sets the initial status to 'active',
        and records the creation timestamp in UTC. Returns the full proposal dict.

        Args:
            title: Short human-readable title for the proposal.
            description: Full markdown description shown in the embed.
            proposal_type: One of 'text', 'governance', 'funding', or 'curate'.
            author_id: Discord user ID of the proposer.
            thread_id: Discord thread ID where the proposal lives.
            message_id: Discord message ID of the embed with voting buttons.
            options: List of option strings (governance proposals only).
            funding_amount: Dollar amount requested (funding proposals only).
            image_url: Optional thumbnail image URL for the embed.
            project_url: Optional link to the project (curation proposals).
        """
        pid = str(self._data['next_id'])
        self._data['next_id'] += 1

        proposal = {
            'id': pid,
            'title': title,
            'description': description,
            'type': proposal_type,
            'author_id': str(author_id),
            'thread_id': str(thread_id),
            'message_id': str(message_id),
            'status': 'active',
            'votes': {},
            'options': options or [],
            'funding_amount': funding_amount,
            'image_url': image_url,
            'project_url': project_url,
            'created_at': datetime.now(timezone.utc).isoformat()
        }

        self._data['proposals'][pid] = proposal
        self._save()
        return proposal

    def get(self, proposal_id: str) -> dict | None:
        """Retrieve a proposal by its string ID, or None if not found."""
        return self._data['proposals'].get(str(proposal_id))

    def get_active(self) -> list[dict]:
        """Return a list of all proposals whose status is 'active'."""
        return [p for p in self._data['proposals'].values() if p['status'] == 'active']

    def vote(self, proposal_id: str, user_id: int, value: str, weight: float = 1.0) -> bool:
        """Record or update a user's vote on a proposal.

        Each user can only have one active vote per proposal; re-voting overwrites
        the previous choice. Only active proposals accept votes.

        Args:
            proposal_id: The proposal to vote on.
            user_id: Discord user ID of the voter.
            value: The vote choice (e.g. 'yes', 'no', 'abstain', or a governance option).
            weight: The voter's Respect balance, used for weighted tallying.

        Returns:
            True if the vote was recorded, False if the proposal is closed or missing.
        """
        proposal = self.get(str(proposal_id))
        if not proposal or proposal['status'] != 'active':
            return False
        # Keyed by user ID string so each user can only have one vote
        proposal['votes'][str(user_id)] = {'value': value, 'weight': weight}
        self._save()
        return True

    def close(self, proposal_id: str) -> dict | None:
        """Close a proposal, preventing further votes. Returns the proposal or None."""
        proposal = self.get(str(proposal_id))
        if not proposal:
            return None
        proposal['status'] = 'closed'
        proposal['closed_at'] = datetime.now(timezone.utc).isoformat()
        self._save()
        return proposal

    def delete(self, proposal_id: str) -> bool:
        """Permanently remove a proposal from the store. Returns True if found and deleted."""
        pid = str(proposal_id)
        if pid in self._data['proposals']:
            del self._data['proposals'][pid]
            self._save()
            return True
        return False

    def get_vote_summary(self, proposal_id: str) -> dict:
        """Aggregate votes into a summary of {option: {count, weight}}.

        Handles both legacy string-only vote records (from older data) and
        the current dict format with 'value' and 'weight' keys. Returns an
        empty dict if the proposal is not found.
        """
        proposal = self.get(str(proposal_id))
        if not proposal:
            return {}
        summary = {}
        for vote_data in proposal['votes'].values():
            # Legacy format: vote_data is just a string like 'yes'
            if isinstance(vote_data, str):
                value, weight = vote_data, 1.0
            else:
                value = vote_data['value']
                weight = vote_data.get('weight', 1.0)
            if value not in summary:
                summary[value] = {'count': 0, 'weight': 0.0}
            summary[value]['count'] += 1
            summary[value]['weight'] += weight
        return summary


def _build_tally_text(store: ProposalStore, proposal_id: str) -> str:
    """Build a formatted vote tally string with progress bars and voter breakdown.

    The tally shows each option's vote count, total Respect weight, a visual
    progress bar, and percentage. Abstain votes are shown separately without
    a percentage (they don't count toward the yes/no ratio). A per-voter
    breakdown is appended for full transparency.
    """
    summary = store.get_vote_summary(proposal_id)
    if not summary:
        return "*No votes yet \u2014 be the first!*"

    total_weight = sum(s['weight'] for s in summary.values() if s['weight'] > 0)
    total_voters = sum(s['count'] for s in summary.values())
    # Abstain weight is excluded from percentage calculations so it doesn't
    # dilute the yes/no ratio -- abstainers signal participation without preference.
    non_abstain_weight = sum(s['weight'] for k, s in summary.items() if k != 'abstain')

    vote_emojis = {'yes': '\u2705', 'no': '\u274c', 'abstain': '\u2b1c'}
    lines = []

    # Establish a deterministic display order: yes/no first (standard choices),
    # then any governance-specific options, and abstain always last.
    ordered_keys = []
    for key in ['yes', 'no']:
        if key in summary:
            ordered_keys.append(key)
    for key in summary:
        if key not in ordered_keys and key != 'abstain':
            ordered_keys.append(key)
    if 'abstain' in summary:
        ordered_keys.append('abstain')

    for value in ordered_keys:
        data = summary[value]
        emoji = vote_emojis.get(value, '\U0001f539')

        if value == 'abstain':
            # Abstain shows count only -- no percentage or progress bar
            lines.append(f"{emoji} **Abstain:** {data['count']} vote{'s' if data['count'] != 1 else ''}")
        else:
            # Calculate percentage of non-abstain Respect weight
            pct = (data['weight'] / non_abstain_weight * 100) if non_abstain_weight > 0 else 0
            # Build a 10-segment progress bar using block characters
            bar_filled = round(pct / 10)
            bar = '\u2588' * bar_filled + '\u2591' * (10 - bar_filled)
            lines.append(
                f"{emoji} **{value.capitalize()}:** {data['count']} vote{'s' if data['count'] != 1 else ''} "
                f"({data['weight']:,.0f} Respect) {bar} {pct:.0f}%"
            )

    header = f"**Vote Tally** ({total_voters} voter{'s' if total_voters != 1 else ''} \u2022 {total_weight:,.0f} Respect)"

    # Per-voter breakdown so the community can see exactly who voted what
    # and verify that weights match on-chain balances.
    proposal = store.get(proposal_id)
    voter_lines = []
    if proposal and proposal.get('votes'):
        for user_id, vote_data in proposal['votes'].items():
            # Handle legacy string format vs current dict format
            if isinstance(vote_data, str):
                value, weight = vote_data, 1.0
            else:
                value = vote_data['value']
                weight = vote_data.get('weight', 1.0)
            emoji = vote_emojis.get(value, '\U0001f539')
            voter_lines.append(f"{emoji} <@{user_id}> \u2014 **{value.capitalize()}** ({weight:,.0f} Respect)")

    # Combine header (total stats), per-option bars, and individual voter breakdown
    result = header + "\n" + "\n".join(lines)
    if voter_lines:
        result += "\n\n**Votes Cast:**\n" + "\n".join(voter_lines)
    return result


def _build_proposal_embed(proposal: dict, store: ProposalStore, author_mention: str = None) -> discord.Embed:
    """Build a rich Discord embed for a proposal with its live vote tally.

    This embed is used both when the proposal is first created and every time
    a vote is cast (the original message is edited in-place). It includes the
    proposal description, funding amount (if applicable), governance options,
    a live vote tally with progress bars, and the voting window timing.

    Args:
        proposal: The proposal dict from ProposalStore.
        store: The ProposalStore instance (needed for vote summary).
        author_mention: Optional Discord mention string; falls back to raw ID.
    """
    ptype = proposal['type']
    emoji = TYPE_EMOJIS.get(ptype, '\U0001f4dd')
    label = TYPE_LABELS.get(ptype, ptype.capitalize())

    # Author mention or fallback to raw user ID mention
    if not author_mention:
        author_mention = f"<@{proposal['author_id']}>"

    # Assemble the embed description from parts: author line, body, funding, options
    desc_parts = [f"\U0001f4cb Proposed by {author_mention} \u2022 {label}\n"]
    desc_parts.append(proposal['description'])

    # Append funding amount if this is a funding proposal
    if proposal.get('funding_amount') is not None:
        desc_parts.append(f"\n\U0001f4b0 **Funding Amount:** ${proposal['funding_amount']:,.2f}")

    # Append numbered governance options if present
    if proposal.get('options'):
        options_text = "\n".join(f"**{i+1}.** {opt}" for i, opt in enumerate(proposal['options']))
        desc_parts.append(f"\n**Options:**\n{options_text}")

    # Green color (0x57F287) for the embed; URL links to the project if available
    embed = discord.Embed(
        title=f"{emoji} {proposal['title']}",
        description="\n".join(desc_parts),
        color=0x57F287,
        url=proposal.get('project_url')
    )

    # Show project thumbnail in the top-right corner of the embed if available
    if proposal.get('image_url'):
        embed.set_thumbnail(url=proposal['image_url'])

    # Live tally field -- updated every time a vote is cast
    tally = _build_tally_text(store, proposal['id'])
    embed.add_field(name="\u200b", value=tally, inline=False)

    # Voting window: proposals have a fixed 7-day lifespan from creation
    created = _parse_utc(proposal['created_at'])
    expires = created + timedelta(days=7)
    time_left = _time_remaining_text(proposal)
    status_label = proposal.get('status', 'active').capitalize()
    if proposal.get('status') == 'active':
        embed.add_field(
            name="\u23f0 Voting Window",
            value=f"Opened: <t:{int(created.timestamp())}:R>\nCloses: <t:{int(expires.timestamp())}:f> ({time_left})",
            inline=False
        )
    else:
        embed.add_field(
            name="\U0001f512 Status",
            value=f"**{status_label}** \u2014 voting ended <t:{int(expires.timestamp())}:R>",
            inline=False
        )

    embed.set_footer(text="Vote with your Respect \u2022 ZAO Fractal \u2022 zao.frapps.xyz")
    return embed


async def _update_proposal_embed(bot, store: ProposalStore, proposal: dict):
    """Edit the original proposal message in-place to refresh the vote tally.

    Called after every vote and when a proposal is closed or reopened. Rebuilds
    the full embed from scratch (including the updated tally) and edits the
    message. If the thread or message has been deleted, the error is logged
    and silently ignored.

    Args:
        bot: The Discord bot instance (used to look up channels).
        store: The ProposalStore instance (passed through to _build_proposal_embed).
        proposal: The proposal dict whose embed should be refreshed.
    """
    try:
        thread = bot.get_channel(int(proposal['thread_id']))
        if not thread:
            return
        message = await thread.fetch_message(int(proposal['message_id']))
        embed = _build_proposal_embed(proposal, store)
        await message.edit(embed=embed)
    except (discord.NotFound, discord.HTTPException) as e:
        logging.getLogger('bot').error(f"Failed to update proposal embed: {e}")


def _time_remaining_text(proposal: dict) -> str:
    """Return a human-readable string for how much time is left in the 7-day voting window.

    Handles timezone-naive legacy timestamps by assuming UTC. Progressively
    narrows the display unit as the deadline approaches: days+hours, then
    hours only, then minutes only. Returns "Voting closed" if the window
    has already elapsed.

    Args:
        proposal: A proposal dict containing a 'created_at' ISO timestamp.

    Returns:
        A string like "3d 12h remaining", "5h remaining", "42m remaining",
        or "Voting closed".
    """
    created = datetime.fromisoformat(proposal['created_at'])
    # Guard against timezone-naive timestamps from older data
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    expires = created + timedelta(days=7)
    now = datetime.now(timezone.utc)
    remaining = expires - now
    if remaining.total_seconds() <= 0:
        return "Voting closed"
    days = remaining.days
    hours = remaining.seconds // 3600
    # Show the most relevant time unit(s) based on how much time is left
    if days > 0:
        return f"{days}d {hours}h remaining"
    if hours > 0:
        return f"{hours}h remaining"
    minutes = remaining.seconds // 60
    return f"{minutes}m remaining"


class ProposalVoteView(discord.ui.View):
    """Yes/No/Abstain voting buttons for text, funding, and curate proposals.

    Uses per-proposal custom_id so each proposal's buttons are unique and
    survive bot restarts without colliding.
    """

    def __init__(self, store: ProposalStore, proposal_id: str, bot=None):
        # timeout=None makes this a persistent view that survives bot restarts
        super().__init__(timeout=None)
        self.store = store
        self.proposal_id = proposal_id
        self.bot = bot

        # Each button gets a unique custom_id containing the proposal ID, so
        # Discord can route button clicks to the correct proposal even after
        # a bot restart (persistent views are re-registered in cog_load).
        yes_btn = discord.ui.Button(
            label="Yes", style=discord.ButtonStyle.success,
            custom_id=f"proposal_yes_{proposal_id}"
        )
        no_btn = discord.ui.Button(
            label="No", style=discord.ButtonStyle.danger,
            custom_id=f"proposal_no_{proposal_id}"
        )
        abstain_btn = discord.ui.Button(
            label="Abstain", style=discord.ButtonStyle.secondary,
            custom_id=f"proposal_abstain_{proposal_id}"
        )

        yes_btn.callback = self._make_callback("yes")
        no_btn.callback = self._make_callback("no")
        abstain_btn.callback = self._make_callback("abstain")

        self.add_item(yes_btn)
        self.add_item(no_btn)
        self.add_item(abstain_btn)

    def _make_callback(self, value: str):
        """Create a closure that handles a vote for the given value ('yes'/'no'/'abstain')."""
        async def callback(interaction: discord.Interaction):
            await self._handle_vote(interaction, value)
        return callback

    async def _handle_vote(self, interaction: discord.Interaction, value: str):
        """Process a vote button click: check Respect balance, record vote, update embed.

        The interaction is deferred ephemerally so the user sees a private confirmation.
        If the user has zero Respect, they are told to register a wallet first.
        On success, the proposal embed is edited in-place with the updated tally,
        and a public confirmation message is posted in the thread.
        """
        await interaction.response.defer(ephemeral=True)

        # Look up the voter's on-chain Respect balance to use as vote weight
        bot = self.bot or interaction.client
        weight = await _get_vote_weight(bot, interaction.user)

        if weight <= 0:
            await interaction.followup.send(
                "You need to hold ZAO Respect tokens to vote. "
                "Make sure your wallet is registered with `/register` and holds OG or ZOR Respect.",
                ephemeral=True
            )
            return

        success = self.store.vote(self.proposal_id, interaction.user.id, value, weight)
        if success:
            await interaction.followup.send(
                f"Vote recorded: **{value}** (weight: {weight:,.0f} Respect)",
                ephemeral=True
            )
            # Update the embed with live tally
            proposal = self.store.get(self.proposal_id)
            if proposal:
                await _update_proposal_embed(bot, self.store, proposal)
                # Public confirmation in the thread
                time_left = _time_remaining_text(proposal)
                thread = bot.get_channel(int(proposal['thread_id']))
                if thread:
                    await thread.send(
                        f"\u2705 **Vote accepted** from {interaction.user.mention} "
                        f"({weight:,.0f} Respect) \u2014 {time_left}"
                    )
        else:
            await interaction.followup.send(
                "This proposal is no longer accepting votes.", ephemeral=True
            )


class GovernanceVoteView(discord.ui.View):
    """Dynamic option voting buttons for governance proposals.

    Unlike ProposalVoteView (yes/no/abstain), governance proposals have
    user-defined options. Each option gets its own button, plus an Abstain
    button. Button styles cycle through primary/success/danger for visual
    distinction. Like ProposalVoteView, this is a persistent view with
    proposal-specific custom_ids.
    """

    def __init__(self, store: ProposalStore, proposal_id: str, options: list[str], bot=None):
        super().__init__(timeout=None)
        self.store = store
        self.proposal_id = proposal_id
        self.bot = bot

        # Cycle through button styles so adjacent options are visually distinct
        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
        ]

        for i, option in enumerate(options):
            style = styles[i % len(styles)]
            button = discord.ui.Button(
                style=style,
                label=option[:80],  # Discord button labels are limited to 80 chars
                custom_id=f"gov_option_{proposal_id}_{i}"
            )
            button.callback = self._make_callback(option)
            self.add_item(button)

        # Abstain is always available regardless of proposal type
        abstain_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Abstain",
            custom_id=f"gov_abstain_{proposal_id}"
        )
        abstain_btn.callback = self._make_callback("abstain")
        self.add_item(abstain_btn)

    def _make_callback(self, value: str):
        """Create a closure that handles a vote for the given option value.

        The callback checks the voter's Respect balance, records the vote
        (or rejects if zero Respect), updates the proposal embed with the
        new tally, and posts a public confirmation in the thread.
        """
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)

            bot = self.bot or interaction.client
            weight = await _get_vote_weight(bot, interaction.user)

            if weight <= 0:
                await interaction.followup.send(
                    "You need to hold ZAO Respect tokens to vote. "
                    "Make sure your wallet is registered with `/register` and holds OG or ZOR Respect.",
                    ephemeral=True
                )
                return

            success = self.store.vote(self.proposal_id, interaction.user.id, value, weight)
            if success:
                await interaction.followup.send(
                    f"Vote recorded: **{value}** (weight: {weight:,.0f} Respect)",
                    ephemeral=True
                )
                # Update the embed with live tally
                proposal = self.store.get(self.proposal_id)
                if proposal:
                    await _update_proposal_embed(bot, self.store, proposal)
                    # Public confirmation in the thread
                    time_left = _time_remaining_text(proposal)
                    thread = bot.get_channel(int(proposal['thread_id']))
                    if thread:
                        await thread.send(
                            f"\u2705 **Vote accepted** from {interaction.user.mention} "
                            f"({weight:,.0f} Respect) \u2014 {time_left}"
                        )
            else:
                await interaction.followup.send(
                    "This proposal is no longer accepting votes.", ephemeral=True
                )
        return callback


class GovernanceOptionsModal(discord.ui.Modal, title="Governance Proposal Options"):
    """Discord modal dialog that collects voting options for governance proposals.

    This modal is shown when a user selects 'governance' as the proposal type
    in the /propose command. It presents a multi-line text input where the user
    enters one voting option per line. On submission, the options are parsed
    and the governance proposal is created with custom voting buttons for each
    option (plus an Abstain button).
    """

    options_text = discord.ui.TextInput(
        label=f"Options (one per line, max {MAX_PROPOSAL_OPTIONS})",
        placeholder="Option A\nOption B\nOption C",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    def __init__(self, cog, title_text: str, description: str):
        """Store the proposal metadata that was collected before the modal was shown.

        Args:
            cog: The ProposalsCog instance (used to call _create_proposal).
            title_text: The proposal title entered in the /propose command.
            description: The proposal description entered in the /propose command.
        """
        super().__init__()
        self.cog = cog
        self.proposal_title = title_text
        self.proposal_description = description

    async def on_submit(self, interaction: discord.Interaction):
        """Parse the newline-separated options and create the governance proposal.

        Options are split by newlines, stripped of whitespace, and capped at
        MAX_PROPOSAL_OPTIONS. At least 2 options are required for a meaningful vote.
        """
        await interaction.response.defer()

        # Parse one option per line, ignoring blanks, capped at the configured maximum
        raw_options = self.options_text.value.strip().split('\n')
        options = [o.strip() for o in raw_options if o.strip()][:MAX_PROPOSAL_OPTIONS]

        if len(options) < 2:
            await interaction.followup.send(
                "Governance proposals need at least 2 options.", ephemeral=True
            )
            return

        await self.cog._create_proposal(
            interaction, self.proposal_title, self.proposal_description,
            'governance', options=options
        )


class ProposalsCog(BaseCog):
    """Discord cog implementing the full proposal lifecycle with Respect-weighted voting.

    Provides slash commands for creating proposals (/propose, /curate), listing
    them (/proposals), viewing details (/proposal), and admin management
    (close, delete, reopen, recover). Manages three background tasks for
    automatic expiry, startup catch-up, and button migration. Extends BaseCog
    for shared admin-check utilities and logging.
    """

    def __init__(self, bot):
        """Initialize the proposals cog with a fresh ProposalStore loaded from disk."""
        super().__init__(bot)
        self.store = ProposalStore()

    async def cog_load(self):
        """Re-register persistent views for active proposals on bot startup.

        Discord persistent views must be re-added to the bot on every startup
        so that button clicks on existing messages are handled. This iterates
        all active proposals and registers the appropriate view type (governance
        multi-option or standard yes/no/abstain) keyed by the original message ID.

        Also starts three background tasks:
        - _expire_proposals: Hourly check for proposals past their 7-day window.
        - _catchup_expiry: One-shot task to immediately close any proposals that
          expired while the bot was offline.
        - _migrate_buttons: One-shot task to re-edit active proposal messages so
          their button custom_ids match the current format.
        """
        for proposal in self.store.get_active():
            pid = proposal['id']
            if proposal['type'] == 'governance' and proposal.get('options'):
                view = GovernanceVoteView(self.store, pid, proposal['options'], bot=self.bot)
            else:
                view = ProposalVoteView(self.store, pid, bot=self.bot)
            self.bot.add_view(view, message_id=int(proposal['message_id']))
        self._expire_proposals.start()
        self._catchup_expiry.start()
        self._migrate_buttons.start()

    @tasks.loop(count=1)
    async def _catchup_expiry(self):
        """Immediately close any overdue proposals on startup (don't wait for hourly loop).

        This one-shot task runs once after the bot connects. It handles the case
        where proposals expired while the bot was offline -- the hourly loop
        would eventually catch them, but this ensures immediate closure so users
        don't see stale "active" proposals when the bot comes back online.
        """
        now = datetime.now(timezone.utc)
        expired_count = 0
        for proposal in self.store.get_active():
            try:
                created = _parse_utc(proposal['created_at'])
                if now - created >= timedelta(days=7):
                    # Mark as closed in the store first (prevents further votes)
                    self.store.close(proposal['id'])
                    expired_count += 1
                    self.logger.info(f"Startup expiry: closed proposal #{proposal['id']}")
                    # Update the embed to reflect closed status (best-effort)
                    try:
                        await _update_proposal_embed(self.bot, self.store, self.store.get(proposal['id']))
                    except Exception as e:
                        self.logger.error(f"Failed to update embed for expired proposal #{proposal['id']}: {e}")
                    # Post a closure notice with final results to the proposal thread
                    try:
                        thread = self.bot.get_channel(int(proposal['thread_id']))
                        if not thread:
                            # Thread may not be in cache; try fetching from Discord API
                            thread = await self.bot.fetch_channel(int(proposal['thread_id']))
                        if thread:
                            # Build a sorted results summary, highest Respect weight first
                            summary = self.store.get_vote_summary(proposal['id'])
                            result_lines = []
                            for option, data in sorted(summary.items(), key=lambda x: x[1]['weight'], reverse=True):
                                result_lines.append(f"**{option.upper()}**: {data['count']} votes ({data['weight']:,.0f} Respect)")
                            result_text = "\n".join(result_lines) if result_lines else "No votes cast."
                            await thread.send(
                                f"⏰ **Voting has closed** (7-day limit reached)\n\n"
                                f"**Final Results:**\n{result_text}"
                            )
                    except Exception as e:
                        self.logger.error(f"Error posting startup closure for proposal #{proposal['id']}: {e}")
            except Exception as e:
                # Catch per-proposal errors so one bad proposal doesn't block others
                self.logger.error(f"Error in startup expiry for proposal #{proposal.get('id', '?')}: {e}")
        if expired_count:
            self.logger.info(f"Startup expiry: closed {expired_count} overdue proposals")
            # Refresh the pinned index to remove the newly-closed proposals
            await self._update_proposals_index()

    @_catchup_expiry.before_loop
    async def _before_catchup(self):
        """Wait for the bot to be fully connected before checking for expired proposals."""
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def _migrate_buttons(self):
        """Re-edit all active proposal messages to ensure button custom_ids match current format.

        This runs once on startup. It fetches each active proposal's message,
        rebuilds the embed and view, and edits the message. This ensures that
        any format changes to custom_ids or embed layout are applied to existing
        proposals without requiring manual intervention.
        """
        for proposal in self.store.get_active():
            pid = proposal['id']
            if proposal['type'] == 'governance' and proposal.get('options'):
                view = GovernanceVoteView(self.store, pid, proposal['options'], bot=self.bot)
            else:
                view = ProposalVoteView(self.store, pid, bot=self.bot)
            try:
                thread = self.bot.get_channel(int(proposal['thread_id']))
                if not thread:
                    thread = await self.bot.fetch_channel(int(proposal['thread_id']))
                msg = await thread.fetch_message(int(proposal['message_id']))
                embed = _build_proposal_embed(proposal, self.store)
                await msg.edit(embed=embed, view=view)
                self.logger.info(f"Migrated buttons for proposal #{pid}")
            except Exception as e:
                self.logger.error(f"Failed to migrate proposal #{pid} buttons: {e}")

    @_migrate_buttons.before_loop
    async def _before_migrate(self):
        """Wait for the bot to be fully connected before migrating buttons."""
        await self.bot.wait_until_ready()

    def cog_unload(self):
        """Cancel all background tasks when the cog is unloaded."""
        self._expire_proposals.cancel()
        self._catchup_expiry.cancel()
        self._migrate_buttons.cancel()

    @tasks.loop(hours=1)
    async def _expire_proposals(self):
        """Hourly background task: close any active proposals older than 7 days.

        For each expired proposal, this task:
        1. Marks the proposal as 'closed' in the store.
        2. Updates the proposal embed to show final results and "closed" status.
        3. Posts a closure notice with the final vote tally in the proposal thread.
        Errors are caught per-proposal so one failure doesn't block others.
        """
        now = datetime.now(timezone.utc)
        for proposal in self.store.get_active():
            try:
                created = _parse_utc(proposal['created_at'])
                # Check if the 7-day voting window has elapsed
                if now - created >= timedelta(days=7):
                    # Mark as closed in the store (prevents further votes)
                    self.store.close(proposal['id'])
                    self.logger.info(f"Auto-closed proposal #{proposal['id']} after 7 days")
                    # Refresh the embed to show "closed" status instead of active voting window
                    try:
                        await _update_proposal_embed(self.bot, self.store, self.store.get(proposal['id']))
                    except Exception as e:
                        self.logger.error(f"Failed to update embed for expired proposal #{proposal['id']}: {e}")
                    # Post a human-readable closure notice with the final vote tally
                    try:
                        thread = self.bot.get_channel(int(proposal['thread_id']))
                        if not thread:
                            thread = await self.bot.fetch_channel(int(proposal['thread_id']))
                        if thread:
                            # Sort results by Respect weight (descending) for readability
                            summary = self.store.get_vote_summary(proposal['id'])
                            result_lines = []
                            for option, data in sorted(summary.items(), key=lambda x: x[1]['weight'], reverse=True):
                                result_lines.append(f"**{option.upper()}**: {data['count']} votes ({data['weight']:,.0f} Respect)")
                            result_text = "\n".join(result_lines) if result_lines else "No votes cast."
                            await thread.send(
                                f"⏰ **Voting has closed** (7-day limit reached)\n\n"
                                f"**Final Results:**\n{result_text}"
                            )
                    except Exception as e:
                        self.logger.error(f"Error posting closure for proposal #{proposal['id']}: {e}")
            except Exception as e:
                # Per-proposal error isolation: one failure doesn't block other expirations
                self.logger.error(f"Error processing expiry for proposal #{proposal.get('id', '?')}: {e}")

    @_expire_proposals.before_loop
    async def _before_expire(self):
        """Wait for the bot to be fully connected before running the expiry loop."""
        await self.bot.wait_until_ready()

    # ── Proposals channel helpers ──

    async def _get_proposals_channel(self) -> discord.TextChannel | None:
        """Return the dedicated proposals channel from the bot's cache, or None if not found.

        Uses the PROPOSALS_CHANNEL_ID from config. Returns None if the bot
        hasn't cached this channel yet (e.g. the channel was deleted or the
        bot lacks access).
        """
        return self.bot.get_channel(PROPOSALS_CHANNEL_ID)

    async def _post_to_proposals_channel(self, proposal: dict, thread: discord.Thread):
        """Post a brief announcement in the proposals channel linking to the new thread."""
        channel = await self._get_proposals_channel()
        if not channel:
            return

        emoji = TYPE_EMOJIS.get(proposal['type'], '\U0001f4dd')
        label = TYPE_LABELS.get(proposal['type'], proposal['type'].capitalize())

        await channel.send(
            f"{emoji} **New {label} Proposal:** {proposal['title']}\n"
            f"Vote and discuss here \u2192 {thread.mention}"
        )

    async def _update_proposals_index(self):
        """Update or create a pinned index embed listing all active proposals.

        This maintains a single pinned message in the proposals channel that
        serves as a live dashboard. It shows each active proposal's title,
        voter count, total Respect weight, time remaining, and a link to its
        thread. If the previously pinned message is deleted, a new one is
        created and pinned automatically.
        """
        channel = await self._get_proposals_channel()
        if not channel:
            return

        active = self.store.get_active()

        embed = discord.Embed(
            title="\U0001f5f3\ufe0f Active Proposals",
            color=0x57F287
        )

        if active:
            # Build one summary line per active proposal for the index
            lines = []
            for p in active:
                emoji = TYPE_EMOJIS.get(p['type'], '\U0001f4dd')
                voter_count = len(p['votes'])
                summary = self.store.get_vote_summary(p['id'])
                total_respect = sum(s['weight'] for s in summary.values()) if summary else 0

                time_left = _time_remaining_text(p)
                # Each entry shows: type emoji, ID, title, voter stats, and thread link
                lines.append(
                    f"{emoji} **#{p['id']} \u2014 {p['title']}**\n"
                    f"\u2003\u2003{voter_count} voter{'s' if voter_count != 1 else ''} \u2022 "
                    f"{total_respect:,.0f} Respect \u2022 {time_left} \u2022 <#{p['thread_id']}>"
                )
            embed.description = "\n\n".join(lines)
        else:
            embed.description = "*No active proposals right now.*"

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        # Try to edit the existing pinned index message; if it was deleted, create a new one
        index_mid = self.store.index_message_id
        if index_mid:
            try:
                msg = await channel.fetch_message(index_mid)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass

        # Create new index message and pin it
        msg = await channel.send(embed=embed)
        self.store.index_message_id = msg.id
        try:
            await msg.pin()
        except discord.HTTPException:
            pass

    # ── Commands ──

    @app_commands.command(
        name="propose",
        description="Create a new proposal for community voting"
    )
    @app_commands.describe(
        title="Short title for the proposal",
        description="Detailed description of the proposal",
        proposal_type="Type of proposal",
        amount="Funding amount (only for funding proposals)"
    )
    @app_commands.choices(proposal_type=[
        app_commands.Choice(name="Text", value="text"),
        app_commands.Choice(name="Governance", value="governance"),
        app_commands.Choice(name="Funding", value="funding"),
    ])
    async def propose(self, interaction: discord.Interaction, title: str,
                      description: str,
                      proposal_type: app_commands.Choice[str] | None = None,
                      amount: float | None = None):
        """Slash command entry point: create a new proposal for community voting.

        For governance proposals, this shows a modal to collect voting options
        before creating the proposal. For all other types (text, funding), it
        creates the proposal directly. Defaults to 'text' if no type is specified.

        Args:
            interaction: The Discord slash command interaction.
            title: Short title for the proposal (shown in embed header).
            description: Full markdown description of the proposal.
            proposal_type: Optional type choice; defaults to 'text'.
            amount: Funding amount in dollars (only used for funding proposals).
        """
        ptype = proposal_type.value if proposal_type else 'text'

        # Governance proposals need a follow-up modal to collect voting options
        if ptype == 'governance':
            modal = GovernanceOptionsModal(self, title, description)
            await interaction.response.send_modal(modal)
            return

        await interaction.response.defer()
        await self._create_proposal(
            interaction, title, description, ptype, funding_amount=amount
        )

    @app_commands.command(
        name="curate",
        description="Nominate a project for the ZAO Fund \u2014 creates a Respect-weighted yes/no vote"
    )
    @app_commands.describe(
        project="Project name or Artizen Fund URL",
        description="Why should the ZAO fund this? (optional)",
        image="Image URL for the project thumbnail (optional)"
    )
    async def curate(self, interaction: discord.Interaction, project: str,
                     description: str = None, image: str = None):
        """Quick-create a yes/no curation vote for a project.

        If the project argument is a URL (especially an Artizen Fund URL), the
        command will attempt to extract the project name from the URL slug and
        scrape Open Graph meta tags for title, description, and image. This
        auto-enrichment makes it easy to propose a project with minimal input.
        """
        await interaction.response.defer()

        project_name = project
        project_url = None
        image_url = image

        if 'artizen.fund' in project or project.startswith('http'):
            project_url = project
            # Try to extract a human-readable name from the last URL path segment
            slug_match = re.search(r'/([^/?#]+?)(?:\?|#|$)', project.rstrip('/'))
            if slug_match:
                slug = slug_match.group(1)
                # Skip generic route segments that aren't meaningful project names
                if slug not in ('index', 'p', 'mf', 'project') and len(slug) > 2:
                    project_name = slug.replace('-', ' ').replace('_', ' ').title()

            # Best-effort OG tag scrape: prefer scraped metadata over slug-derived name
            scraped = await _scrape_og_tags(project_url)
            if scraped.get('title'):
                project_name = scraped['title']
            if scraped.get('description') and not description:
                description = scraped['description']
            if scraped.get('image') and not image_url:
                image_url = scraped['image']

        # Build the curation proposal title and description with a standard format
        title = f"Curate: {project_name}"
        desc_parts = ["**Should the ZAO fund this project?**\n"]
        desc_parts.append(f"**Project:** {project_name}")
        if project_url:
            desc_parts.append(f"**Link:** [View Project]({project_url})")
        if description:
            desc_parts.append(f"\n{description}")
        desc_parts.append("\nVote **Yes** to include or **No** to pass.")

        await self._create_proposal(
            interaction, title, "\n".join(desc_parts), 'curate',
            image_url=image_url, project_url=project_url
        )

    async def _create_proposal(self, interaction: discord.Interaction,
                                title: str, description: str, ptype: str,
                                options: list[str] | None = None,
                                funding_amount: float | None = None,
                                image_url: str | None = None,
                                project_url: str | None = None):
        """Internal method shared by /propose, /curate, and GovernanceOptionsModal.

        This orchestrates the full proposal creation flow:
        1. Create a public thread in the proposals channel.
        2. Post a placeholder message in the thread (needed to get a message ID).
        3. Store the proposal in ProposalStore with an auto-assigned ID.
        4. Build the embed and voting buttons, then edit the placeholder.
        5. Register the persistent view so buttons survive restarts.
        6. Announce in the proposals channel and notify #general.
        7. Update the pinned proposals index.
        """
        # Always create threads in the dedicated proposals channel so everyone can see them
        from config.config import PROPOSALS_CHANNEL_ID
        channel = interaction.guild.get_channel(PROPOSALS_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send("❌ Proposals channel not found. Contact an admin.", ephemeral=True)
            return

        # Create thread for discussion
        thread = await channel.create_thread(
            name=f"Proposal: {title[:90]}",
            type=discord.ChannelType.public_thread,
            reason="ZAO Proposal"
        )

        # Send a placeholder first to obtain a message ID, which is needed by
        # ProposalStore.create() and for registering the persistent view.
        placeholder = await thread.send("\u23f3 Setting up proposal...")

        # Persist the proposal to the JSON store (assigns auto-incrementing ID)
        proposal = self.store.create(
            title=title,
            description=description,
            proposal_type=ptype,
            author_id=interaction.user.id,
            thread_id=thread.id,
            message_id=placeholder.id,
            options=options,
            funding_amount=funding_amount,
            image_url=image_url,
            project_url=project_url
        )

        # Build the rich embed with the author's mention (not just their ID)
        embed = _build_proposal_embed(proposal, self.store, interaction.user.mention)

        # Choose the appropriate voting view based on proposal type
        pid = proposal['id']
        if ptype == 'governance' and options:
            view = GovernanceVoteView(self.store, pid, options, bot=self.bot)
        else:
            view = ProposalVoteView(self.store, pid, bot=self.bot)

        # Register the view as persistent (keyed by message ID) and replace the placeholder
        self.bot.add_view(view, message_id=placeholder.id)
        await placeholder.edit(content=None, embed=embed, view=view)

        try:
            await interaction.followup.send(
                f"Proposal **#{pid}** created! Vote here \u2192 {thread.mention}"
            )
        except discord.NotFound:
            pass  # Interaction expired, but proposal was created successfully

        # Post announcement in the proposals channel and refresh the pinned index
        await self._post_to_proposals_channel(proposal, thread)
        await self._update_proposals_index()

        # Cross-post a notification to #general so the broader community is aware
        # Hardcoded #general channel ID for cross-posting proposal announcements
        GENERAL_CHANNEL_ID = 1127115903113367738
        general = interaction.guild.get_channel(GENERAL_CHANNEL_ID)
        if general:
            emoji = TYPE_EMOJIS.get(ptype, '\U0001f4dd')
            label = TYPE_LABELS.get(ptype, ptype.capitalize())
            proposals_channel = interaction.guild.get_channel(PROPOSALS_CHANNEL_ID)
            channel_mention = proposals_channel.mention if proposals_channel else '#proposals'

            # Build a compact notification embed with truncated description
            notify_embed = discord.Embed(
                title=f"{emoji} {title}",
                description=description[:300] + ('...' if len(description) > 300 else ''),
                color=0x57F287,
                url=project_url if project_url else None,
            )
            if image_url:
                notify_embed.set_thumbnail(url=image_url)
            notify_embed.add_field(
                name='Vote Now',
                value=f"Head to {channel_mention} to cast your Respect-weighted vote!\nVoting closes in **7 days**.",
                inline=False
            )
            notify_embed.set_footer(text=f'{label} Proposal • ZAO Fractal • zao.frapps.xyz')
            await general.send(embed=notify_embed)

    @app_commands.command(
        name="proposals",
        description="List all active proposals"
    )
    @app_commands.describe(page="Page number (10 proposals per page)")
    async def proposals(self, interaction: discord.Interaction, page: int = 1):
        """List all active proposals in a paginated ephemeral embed.

        Each page displays up to 10 proposals with their voter count, total
        Respect weight, time remaining, and a link to the discussion thread.
        The response is ephemeral so it doesn't clutter the channel.

        Args:
            interaction: The Discord slash command interaction.
            page: Which page of results to show (1-indexed, 10 per page).
        """
        await interaction.response.defer(ephemeral=True)

        active = self.store.get_active()
        if not active:
            await interaction.followup.send("No active proposals.", ephemeral=True)
            return

        # Calculate pagination boundaries, clamping page to valid range
        per_page = 10
        total_pages = max(1, (len(active) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_proposals = active[start:start + per_page]

        title = f"\U0001f5f3\ufe0f Active Proposals ({len(active)})"
        if total_pages > 1:
            title += f" — Page {page}/{total_pages}"

        embed = discord.Embed(title=title, color=0x57F287)

        for p in page_proposals:
            emoji = TYPE_EMOJIS.get(p['type'], '\U0001f4dd')
            summary = self.store.get_vote_summary(p['id'])
            total_voters = sum(s['count'] for s in summary.values()) if summary else 0
            total_respect = sum(s['weight'] for s in summary.values()) if summary else 0
            time_left = _time_remaining_text(p)
            # Discord embed field names are capped at 256 chars; truncate to stay safe
            field_title = f"{emoji} #{p['id']} \u2014 {p['title']}"
            if len(field_title) > 250:
                field_title = field_title[:247] + "..."
            embed.add_field(
                name=field_title,
                value=f"{total_voters} voters \u2022 {total_respect:,.0f} Respect \u2022 {time_left}\n<#{p['thread_id']}>",
                inline=False
            )

        if total_pages > 1:
            embed.set_footer(text=f"Page {page}/{total_pages} \u2022 Use /proposals page:<n> for more \u2022 ZAO Fractal")
        else:
            embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="proposal",
        description="View details and vote breakdown for a specific proposal"
    )
    @app_commands.describe(proposal_id="The proposal number to view")
    async def proposal_detail(self, interaction: discord.Interaction, proposal_id: int):
        """View a specific proposal's full embed with vote breakdown and discussion link.

        Shows the same rich embed used in the proposal thread, plus an extra
        field linking to the discussion thread. Useful for quickly checking a
        proposal's status without navigating to its thread.

        Args:
            interaction: The Discord slash command interaction.
            proposal_id: The numeric proposal ID to look up.
        """
        await interaction.response.defer(ephemeral=True)

        proposal = self.store.get(str(proposal_id))
        if not proposal:
            await interaction.followup.send(
                f"Proposal #{proposal_id} not found.", ephemeral=True
            )
            return

        embed = _build_proposal_embed(proposal, self.store)
        # Add a direct link to the discussion thread for convenience
        embed.add_field(
            name="Discussion",
            value=f"<#{proposal['thread_id']}>",
            inline=False
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="admin_close_proposal",
        description="[ADMIN] Close voting on a proposal and post results"
    )
    @app_commands.describe(proposal_id="The proposal number to close")
    async def admin_close_proposal(self, interaction: discord.Interaction, proposal_id: int):
        """Manually close a proposal before its 7-day window expires and post final results."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role to use this command.",
                ephemeral=True
            )
            return

        proposal = self.store.close(str(proposal_id))
        if not proposal:
            await interaction.followup.send(
                f"Proposal #{proposal_id} not found.", ephemeral=True
            )
            return

        # Build a final results embed (red color to indicate closure)
        tally = _build_tally_text(self.store, str(proposal_id))

        embed = discord.Embed(
            title=f"\U0001f512 Proposal #{proposal['id']} \u2014 CLOSED",
            description=f"**{proposal['title']}**\n\n{proposal['description']}",
            color=0xED4245
        )
        embed.add_field(name="Final Results", value=tally, inline=False)
        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        # Post results to the proposal thread
        thread = self.bot.get_channel(int(proposal['thread_id']))
        if thread:
            await thread.send(embed=embed)

            # Remove voting buttons from the original message since voting is over
            try:
                original = await thread.fetch_message(int(proposal['message_id']))
                await original.edit(view=None)
            except discord.NotFound:
                pass

        await interaction.followup.send(
            f"Proposal #{proposal_id} closed. Results posted to <#{proposal['thread_id']}>.",
            ephemeral=True
        )

        # Update proposals index
        await self._update_proposals_index()

    @app_commands.command(
        name="admin_delete_proposal",
        description="[ADMIN] Delete a proposal entirely"
    )
    @app_commands.describe(proposal_id="The proposal number to delete")
    async def admin_delete_proposal(self, interaction: discord.Interaction, proposal_id: int):
        """Permanently delete a proposal from the data store (does not delete the Discord thread)."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role to use this command.",
                ephemeral=True
            )
            return

        success = self.store.delete(str(proposal_id))
        if success:
            await interaction.followup.send(
                f"Proposal #{proposal_id} deleted.", ephemeral=True
            )
            # Update proposals index
            await self._update_proposals_index()
        else:
            await interaction.followup.send(
                f"Proposal #{proposal_id} not found.", ephemeral=True
            )


    @app_commands.command(
        name="admin_reopen_proposal",
        description="[ADMIN] Reopen a closed proposal so voting can continue"
    )
    @app_commands.describe(proposal_id="The proposal number to reopen")
    async def admin_reopen_proposal(self, interaction: discord.Interaction, proposal_id: int):
        """Reopen a closed proposal with a fresh 7-day voting window.

        This resets created_at to now, removes the closed_at timestamp, sets
        status back to 'active', re-registers the persistent voting view,
        and re-attaches voting buttons to the original message.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role to use this command.",
                ephemeral=True
            )
            return

        proposal = self.store.get(str(proposal_id))
        if not proposal:
            await interaction.followup.send(
                f"Proposal #{proposal_id} not found.", ephemeral=True
            )
            return

        if proposal['status'] == 'active':
            await interaction.followup.send(
                f"Proposal #{proposal_id} is already active.", ephemeral=True
            )
            return

        # Reset the proposal to active state with a fresh 7-day countdown
        proposal['status'] = 'active'
        proposal['created_at'] = datetime.now(timezone.utc).isoformat()
        if 'closed_at' in proposal:
            del proposal['closed_at']
        self.store._save()

        # Re-register the persistent voting view so button clicks are handled again
        pid = proposal['id']
        if proposal['type'] == 'governance' and proposal.get('options'):
            view = GovernanceVoteView(self.store, pid, proposal['options'], bot=self.bot)
        else:
            view = ProposalVoteView(self.store, pid, bot=self.bot)
        self.bot.add_view(view, message_id=int(proposal['message_id']))

        # Update the embed
        await _update_proposal_embed(self.bot, self.store, proposal)

        # Re-attach buttons to the message
        try:
            thread = self.bot.get_channel(int(proposal['thread_id']))
            if not thread:
                thread = await self.bot.fetch_channel(int(proposal['thread_id']))
            if thread:
                msg = await thread.fetch_message(int(proposal['message_id']))
                embed = _build_proposal_embed(proposal, self.store)
                await msg.edit(embed=embed, view=view)
                await thread.send(
                    f"**Proposal reopened by admin!** Voting continues for 7 more days."
                )
        except Exception as e:
            self.logger.error(f"Error updating reopened proposal embed: {e}")

        await self._update_proposals_index()
        await interaction.followup.send(
            f"Proposal #{proposal_id} reopened with a fresh 7-day voting window.",
            ephemeral=True
        )

    @app_commands.command(
        name="admin_recover_proposals",
        description="[ADMIN] Scan #proposals channel for threads missing from the database and recover them"
    )
    async def admin_recover_proposals(self, interaction: discord.Interaction):
        """Scan the proposals channel for orphaned threads and recover them into the data store.

        This handles cases where the bot's proposals.json was lost, reset, or
        corrupted while proposal threads still exist in Discord. It scans both
        active and recently archived threads, parses proposal metadata from the
        bot's embed, determines whether the 7-day window has passed, and
        re-creates the proposal records. Only threads named "Proposal: ..." with
        a bot-authored embed are recovered.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role to use this command.",
                ephemeral=True
            )
            return

        channel = await self._get_proposals_channel()
        if not channel:
            await interaction.followup.send(
                "Proposals channel not found.", ephemeral=True
            )
            return

        # Build a set of thread IDs already tracked to avoid duplicates
        known_thread_ids = set()
        for p in self.store._data['proposals'].values():
            known_thread_ids.add(str(p['thread_id']))

        recovered = []
        skipped = []
        errors = []

        # Start with active (non-archived) threads which are already cached locally
        all_threads = channel.threads.copy()

        # Also fetch recently archived threads (capped at 20 to avoid API rate limits)
        try:
            async for thread in channel.archived_threads(limit=20):
                all_threads.append(thread)
        except Exception as e:
            self.logger.error(f"Error fetching archived threads: {e}")

        # Filter to threads that look like proposals (name prefix) but aren't in the store
        untracked = []
        for thread in all_threads:
            if str(thread.id) not in known_thread_ids and thread.name.startswith("Proposal:"):
                untracked.append(thread)

        if not untracked:
            await interaction.followup.send(
                f"**Proposal Recovery Report**\n\n"
                f"Scanned {len(all_threads)} threads — all proposals are already tracked.\n"
                f"Currently tracking {len(known_thread_ids)} proposals.",
                ephemeral=True
            )
            return

        for thread in untracked:
            try:
                # Look at the first 3 messages in the thread for the bot's proposal embed.
                # The embed is always posted by the bot as the first or second message.
                bot_message = None
                async for msg in thread.history(limit=3, oldest_first=True):
                    if msg.author.id == self.bot.user.id and msg.embeds:
                        bot_message = msg
                        break

                if not bot_message or not bot_message.embeds:
                    skipped.append(thread.name)
                    continue

                embed = bot_message.embeds[0]

                # Reconstruct proposal metadata from the embed fields
                title = embed.title or thread.name.replace("Proposal: ", "")
                # Strip the leading type emoji (e.g. pencil, scales) from the title
                for e_char in TYPE_EMOJIS.values():
                    if title.startswith(e_char):
                        title = title[len(e_char):].strip()
                        break

                description = embed.description or ""
                project_url = embed.url
                image_url = embed.thumbnail.url if embed.thumbnail else None

                # Infer proposal type from content heuristics
                proposal_type = 'text'
                if title.startswith("Curate:") or "Should the ZAO fund this project?" in description:
                    proposal_type = 'curate'
                elif "Funding Amount" in description:
                    proposal_type = 'funding'

                # Try to extract the original author from the "Proposed by @user" line
                author_id = str(self.bot.user.id)  # fallback to bot if not found
                author_match = re.search(r'Proposed by <@(\d+)>', description)
                if author_match:
                    author_id = author_match.group(1)

                # Use the Discord message creation timestamp as the proposal start time
                msg_created = bot_message.created_at
                if msg_created.tzinfo is None:
                    msg_created = msg_created.replace(tzinfo=timezone.utc)
                created_at = msg_created.isoformat()

                # Determine if the proposal is still within its 7-day voting window
                age = datetime.now(timezone.utc) - msg_created
                status = 'closed' if age >= timedelta(days=7) else 'active'

                # Assign a new ID from the auto-incrementing counter
                pid = str(self.store._data['next_id'])
                self.store._data['next_id'] += 1

                proposal = {
                    'id': pid,
                    'title': title,
                    'description': description,
                    'type': proposal_type,
                    'author_id': author_id,
                    'thread_id': str(thread.id),
                    'message_id': str(bot_message.id),
                    'status': status,
                    'votes': {},
                    'options': [],
                    'funding_amount': None,
                    'image_url': image_url,
                    'project_url': project_url,
                    'created_at': created_at
                }

                self.store._data['proposals'][pid] = proposal
                recovered.append(f"#{pid} — {title[:50]} ({status})")

                # Only register a voting view for still-active proposals; skip
                # embed edits during recovery to keep the operation fast.
                if status == 'active':
                    view = ProposalVoteView(self.store, pid, bot=self.bot)
                    self.bot.add_view(view, message_id=bot_message.id)

            except Exception as e:
                errors.append(f"{thread.name[:40]}: {str(e)[:60]}")
                self.logger.error(f"Error recovering thread {thread.name}: {e}", exc_info=True)

        # Batch-save all recovered proposals at once to minimize disk writes
        if recovered:
            self.store._save()
            await self._update_proposals_index()

        # Build a human-readable report summarizing recovered, skipped, and errored threads
        report = f"**Proposal Recovery Report**\n\n"
        report += f"Threads scanned: {len(all_threads)}\n"
        report += f"Already tracked: {len(known_thread_ids)}\n\n"

        if recovered:
            report += f"**Recovered ({len(recovered)}):**\n"
            for r in recovered:
                report += f"+ {r}\n"

        if skipped:
            # Skipped threads had the "Proposal:" prefix but no bot embed to parse
            report += f"\n**Skipped ({len(skipped)}):** (no bot embed found)\n"
            for s in skipped:
                report += f"- {s}\n"

        if errors:
            report += f"\n**Errors ({len(errors)}):**\n"
            for e in errors:
                report += f"! {e}\n"

        if recovered:
            # Guide the admin on next steps after recovery
            report += f"\nUse `/proposals` to see updated list. Use `/admin_reopen_proposal` to reactivate any closed ones."

        await interaction.followup.send(report, ephemeral=True)


async def setup(bot):
    """discord.py extension entry point: register the ProposalsCog with the bot."""
    await bot.add_cog(ProposalsCog(bot))
