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
from datetime import datetime, timedelta
from cogs.base import BaseCog
from config.config import PROPOSAL_TYPES, MAX_PROPOSAL_OPTIONS, PROPOSALS_CHANNEL_ID

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
PROPOSALS_FILE = os.path.join(DATA_DIR, 'proposals.json')

# Optimism contracts
OG_RESPECT_ADDRESS = '0x34cE89baA7E4a4B00E17F7E4C0cb97105C216957'
ZOR_RESPECT_ADDRESS = '0x9885CCeEf7E8371Bf8d6f2413723D25917E7445c'
ZOR_TOKEN_ID = 0

# Default public Optimism RPC (Alchemy key optional via env)
DEFAULT_OPTIMISM_RPC = 'https://mainnet.optimism.io'

# Embed constants
TYPE_EMOJIS = {'text': '\U0001f4dd', 'governance': '\u2696\ufe0f', 'funding': '\U0001f4b0', 'curate': '\U0001f3a8'}
TYPE_LABELS = {'text': 'Text', 'governance': 'Governance', 'funding': 'Funding', 'curate': 'Curation'}


class RespectBalance:
    """Queries onchain Respect balances with caching"""

    def __init__(self):
        self.logger = logging.getLogger('bot')
        self._cache = {}  # wallet -> {og, zor, total, timestamp}
        self._cache_ttl = 300  # 5 minutes

    def _get_rpc_url(self) -> str:
        return os.getenv('ALCHEMY_OPTIMISM_RPC', DEFAULT_OPTIMISM_RPC)

    async def get_total_respect(self, wallet: str) -> float:
        """Get total Respect (OG + ZOR) for a wallet, with caching"""
        if not wallet:
            return 0.0

        wallet = wallet.lower()
        cached = self._cache.get(wallet)
        if cached and time.time() - cached['timestamp'] < self._cache_ttl:
            return cached['total']

        try:
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
        """Make an eth_call to Optimism"""
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self._get_rpc_url(), json=payload) as resp:
                result = await resp.json()
                return result.get("result", "0x")

    async def _query_erc20_balance(self, wallet: str, contract: str) -> float:
        """Query ERC-20 balanceOf — returns balance as float (18 decimals)"""
        addr_padded = wallet[2:].lower().zfill(64)
        data = f"0x70a08231{addr_padded}"
        result = await self._eth_call(contract, data)
        if result and result != "0x" and len(result) >= 66:
            raw = int(result, 16)
            return raw / 1e18
        return 0.0

    async def _query_erc1155_balance(self, wallet: str, contract: str, token_id: int) -> float:
        """Query ERC-1155 balanceOf — returns balance as integer (no decimals)"""
        addr_padded = wallet[2:].lower().zfill(64)
        id_padded = hex(token_id)[2:].zfill(64)
        data = f"0x00fdd58e{addr_padded}{id_padded}"
        result = await self._eth_call(contract, data)
        if result and result != "0x" and len(result) >= 66:
            return float(int(result, 16))
        return 0.0


# Singleton for caching across votes
_respect_balance = RespectBalance()


async def _get_vote_weight(bot, user: discord.User) -> float:
    """Look up user's wallet and return their total Respect as vote weight"""
    wallet = None
    if hasattr(bot, 'wallet_registry'):
        wallet = bot.wallet_registry.lookup(user)

    if not wallet:
        return 0.0

    return await _respect_balance.get_total_respect(wallet)


async def _scrape_og_tags(url: str) -> dict:
    """Best-effort scrape of Open Graph meta tags from a URL"""
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5),
                                   headers={'User-Agent': 'Mozilla/5.0 (compatible; ZAOBot/1.0)'}) as resp:
                if resp.status != 200:
                    return result
                html = await resp.text()
                # Parse og: meta tags
                for tag in ['title', 'description', 'image']:
                    match = re.search(
                        rf'<meta\s+(?:property|name)=["\']og:{tag}["\']\s+content=["\']([^"\']+)["\']',
                        html, re.IGNORECASE
                    )
                    if not match:
                        # Try reversed attribute order
                        match = re.search(
                            rf'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:{tag}["\']',
                            html, re.IGNORECASE
                        )
                    if match:
                        result[tag] = html.unescape(match.group(1))
    except Exception:
        pass
    return result


class ProposalStore:
    """JSON-backed store for proposals with Respect-weighted voting"""

    def __init__(self):
        self.logger = logging.getLogger('bot')
        self._data = {'next_id': 1, 'proposals': {}, '_index_message_id': None}
        self._load()

    def _load(self):
        if os.path.exists(PROPOSALS_FILE):
            with open(PROPOSALS_FILE, 'r') as f:
                self._data = json.load(f)
            # Migrate: ensure _index_message_id exists
            if '_index_message_id' not in self._data:
                self._data['_index_message_id'] = None

    def _save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PROPOSALS_FILE, 'w') as f:
            json.dump(self._data, f, indent=2)

    @property
    def index_message_id(self) -> int | None:
        mid = self._data.get('_index_message_id')
        return int(mid) if mid else None

    @index_message_id.setter
    def index_message_id(self, value: int | None):
        self._data['_index_message_id'] = str(value) if value else None
        self._save()

    def create(self, title: str, description: str, proposal_type: str,
               author_id: int, thread_id: int, message_id: int,
               options: list[str] | None = None,
               funding_amount: float | None = None,
               image_url: str | None = None,
               project_url: str | None = None) -> dict:
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
            'created_at': datetime.utcnow().isoformat()
        }

        self._data['proposals'][pid] = proposal
        self._save()
        return proposal

    def get(self, proposal_id: str) -> dict | None:
        return self._data['proposals'].get(str(proposal_id))

    def get_active(self) -> list[dict]:
        return [p for p in self._data['proposals'].values() if p['status'] == 'active']

    def vote(self, proposal_id: str, user_id: int, value: str, weight: float = 1.0) -> bool:
        proposal = self.get(str(proposal_id))
        if not proposal or proposal['status'] != 'active':
            return False
        proposal['votes'][str(user_id)] = {'value': value, 'weight': weight}
        self._save()
        return True

    def close(self, proposal_id: str) -> dict | None:
        proposal = self.get(str(proposal_id))
        if not proposal:
            return None
        proposal['status'] = 'closed'
        proposal['closed_at'] = datetime.utcnow().isoformat()
        self._save()
        return proposal

    def delete(self, proposal_id: str) -> bool:
        pid = str(proposal_id)
        if pid in self._data['proposals']:
            del self._data['proposals'][pid]
            self._save()
            return True
        return False

    def get_vote_summary(self, proposal_id: str) -> dict:
        """Returns {option: {count, weight}} for weighted results"""
        proposal = self.get(str(proposal_id))
        if not proposal:
            return {}
        summary = {}
        for vote_data in proposal['votes'].values():
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
    """Build a formatted vote tally string with progress bars"""
    summary = store.get_vote_summary(proposal_id)
    if not summary:
        return "*No votes yet \u2014 be the first!*"

    total_weight = sum(s['weight'] for s in summary.values() if s['weight'] > 0)
    total_voters = sum(s['count'] for s in summary.values())
    # Don't count abstain weight in percentage calc
    non_abstain_weight = sum(s['weight'] for k, s in summary.items() if k != 'abstain')

    vote_emojis = {'yes': '\u2705', 'no': '\u274c', 'abstain': '\u2b1c'}
    lines = []

    # Show yes/no first, then abstain, then any other options
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
            lines.append(f"{emoji} **Abstain:** {data['count']} vote{'s' if data['count'] != 1 else ''}")
        else:
            pct = (data['weight'] / non_abstain_weight * 100) if non_abstain_weight > 0 else 0
            bar_filled = round(pct / 10)
            bar = '\u2588' * bar_filled + '\u2591' * (10 - bar_filled)
            lines.append(
                f"{emoji} **{value.capitalize()}:** {data['count']} vote{'s' if data['count'] != 1 else ''} "
                f"({data['weight']:,.0f} Respect) {bar} {pct:.0f}%"
            )

    header = f"**Vote Tally** ({total_voters} voter{'s' if total_voters != 1 else ''} \u2022 {total_weight:,.0f} Respect)"

    # Individual voter breakdown for transparency
    proposal = store.get(proposal_id)
    voter_lines = []
    if proposal and proposal.get('votes'):
        for user_id, vote_data in proposal['votes'].items():
            if isinstance(vote_data, str):
                value, weight = vote_data, 1.0
            else:
                value = vote_data['value']
                weight = vote_data.get('weight', 1.0)
            emoji = vote_emojis.get(value, '\U0001f539')
            voter_lines.append(f"{emoji} <@{user_id}> \u2014 **{value.capitalize()}** ({weight:,.0f} Respect)")

    result = header + "\n" + "\n".join(lines)
    if voter_lines:
        result += "\n\n**Votes Cast:**\n" + "\n".join(voter_lines)
    return result


def _build_proposal_embed(proposal: dict, store: ProposalStore, author_mention: str = None) -> discord.Embed:
    """Build a clean proposal embed with live tally"""
    ptype = proposal['type']
    emoji = TYPE_EMOJIS.get(ptype, '\U0001f4dd')
    label = TYPE_LABELS.get(ptype, ptype.capitalize())

    # Author mention or fallback to ID
    if not author_mention:
        author_mention = f"<@{proposal['author_id']}>"

    # Build description
    desc_parts = [f"\U0001f4cb Proposed by {author_mention} \u2022 {label}\n"]
    desc_parts.append(proposal['description'])

    if proposal.get('funding_amount') is not None:
        desc_parts.append(f"\n\U0001f4b0 **Funding Amount:** ${proposal['funding_amount']:,.2f}")

    if proposal.get('options'):
        options_text = "\n".join(f"**{i+1}.** {opt}" for i, opt in enumerate(proposal['options']))
        desc_parts.append(f"\n**Options:**\n{options_text}")

    embed = discord.Embed(
        title=f"{emoji} {proposal['title']}",
        description="\n".join(desc_parts),
        color=0x57F287,
        url=proposal.get('project_url')
    )

    # Thumbnail image if available
    if proposal.get('image_url'):
        embed.set_thumbnail(url=proposal['image_url'])

    # Live tally
    tally = _build_tally_text(store, proposal['id'])
    embed.add_field(name="\u200b", value=tally, inline=False)

    # Date info
    created = datetime.fromisoformat(proposal['created_at'])
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
    """Edit the original proposal message to refresh the vote tally"""
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
    """Return human-readable time remaining for a proposal (7-day window)"""
    created = datetime.fromisoformat(proposal['created_at'])
    expires = created + timedelta(days=7)
    now = datetime.utcnow()
    remaining = expires - now
    if remaining.total_seconds() <= 0:
        return "Voting closed"
    days = remaining.days
    hours = remaining.seconds // 3600
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
        super().__init__(timeout=None)
        self.store = store
        self.proposal_id = proposal_id
        self.bot = bot

        # Dynamic buttons with unique custom_id per proposal
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
        async def callback(interaction: discord.Interaction):
            await self._handle_vote(interaction, value)
        return callback

    async def _handle_vote(self, interaction: discord.Interaction, value: str):
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


class GovernanceVoteView(discord.ui.View):
    """Dynamic option voting buttons for governance proposals"""

    def __init__(self, store: ProposalStore, proposal_id: str, options: list[str], bot=None):
        super().__init__(timeout=None)
        self.store = store
        self.proposal_id = proposal_id
        self.bot = bot

        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
        ]

        for i, option in enumerate(options):
            style = styles[i % len(styles)]
            button = discord.ui.Button(
                style=style,
                label=option[:80],
                custom_id=f"gov_option_{proposal_id}_{i}"
            )
            button.callback = self._make_callback(option)
            self.add_item(button)

        abstain_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Abstain",
            custom_id=f"gov_abstain_{proposal_id}"
        )
        abstain_btn.callback = self._make_callback("abstain")
        self.add_item(abstain_btn)

    def _make_callback(self, value: str):
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
    """Modal to collect voting options for governance proposals"""

    options_text = discord.ui.TextInput(
        label=f"Options (one per line, max {MAX_PROPOSAL_OPTIONS})",
        placeholder="Option A\nOption B\nOption C",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    def __init__(self, cog, title_text: str, description: str):
        super().__init__()
        self.cog = cog
        self.proposal_title = title_text
        self.proposal_description = description

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

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
    """Cog for the proposal voting system with Respect-weighted votes"""

    def __init__(self, bot):
        super().__init__(bot)
        self.store = ProposalStore()

    async def cog_load(self):
        """Re-register persistent views for active proposals"""
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
        """Immediately close any overdue proposals on startup (don't wait for hourly loop)"""
        now = datetime.utcnow()
        expired_count = 0
        for proposal in self.store.get_active():
            try:
                created = datetime.fromisoformat(proposal['created_at'])
                if now - created >= timedelta(days=7):
                    self.store.close(proposal['id'])
                    expired_count += 1
                    self.logger.info(f"Startup expiry: closed proposal #{proposal['id']}")
                    try:
                        await _update_proposal_embed(self.bot, self.store, self.store.get(proposal['id']))
                    except Exception as e:
                        self.logger.error(f"Failed to update embed for expired proposal #{proposal['id']}: {e}")
                    try:
                        thread = self.bot.get_channel(int(proposal['thread_id']))
                        if not thread:
                            thread = await self.bot.fetch_channel(int(proposal['thread_id']))
                        if thread:
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
                self.logger.error(f"Error in startup expiry for proposal #{proposal.get('id', '?')}: {e}")
        if expired_count:
            self.logger.info(f"Startup expiry: closed {expired_count} overdue proposals")
            await self._update_proposals_index()

    @_catchup_expiry.before_loop
    async def _before_catchup(self):
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def _migrate_buttons(self):
        """Re-edit all active proposal messages to update button custom_ids (runs once after ready)"""
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
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self._expire_proposals.cancel()
        self._catchup_expiry.cancel()
        self._migrate_buttons.cancel()

    @tasks.loop(hours=1)
    async def _expire_proposals(self):
        """Close proposals older than 7 days"""
        now = datetime.utcnow()
        for proposal in self.store.get_active():
            try:
                created = datetime.fromisoformat(proposal['created_at'])
                if now - created >= timedelta(days=7):
                    self.store.close(proposal['id'])
                    self.logger.info(f"Auto-closed proposal #{proposal['id']} after 7 days")
                    # Update the embed to show it's closed
                    try:
                        await _update_proposal_embed(self.bot, self.store, self.store.get(proposal['id']))
                    except Exception as e:
                        self.logger.error(f"Failed to update embed for expired proposal #{proposal['id']}: {e}")
                    # Post closure notice in the thread
                    try:
                        thread = self.bot.get_channel(int(proposal['thread_id']))
                        if not thread:
                            thread = await self.bot.fetch_channel(int(proposal['thread_id']))
                        if thread:
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
                self.logger.error(f"Error processing expiry for proposal #{proposal.get('id', '?')}: {e}")

    @_expire_proposals.before_loop
    async def _before_expire(self):
        await self.bot.wait_until_ready()

    # ── Proposals channel helpers ──

    async def _get_proposals_channel(self) -> discord.TextChannel | None:
        """Get the dedicated proposals channel"""
        return self.bot.get_channel(PROPOSALS_CHANNEL_ID)

    async def _post_to_proposals_channel(self, proposal: dict, thread: discord.Thread):
        """Post an announcement in the proposals channel when a new proposal is created"""
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
        """Update or create a pinned index of all active proposals in the proposals channel"""
        channel = await self._get_proposals_channel()
        if not channel:
            return

        active = self.store.get_active()

        embed = discord.Embed(
            title="\U0001f5f3\ufe0f Active Proposals",
            color=0x57F287
        )

        if active:
            lines = []
            for p in active:
                emoji = TYPE_EMOJIS.get(p['type'], '\U0001f4dd')
                voter_count = len(p['votes'])
                summary = self.store.get_vote_summary(p['id'])
                total_respect = sum(s['weight'] for s in summary.values()) if summary else 0

                time_left = _time_remaining_text(p)
                lines.append(
                    f"{emoji} **#{p['id']} \u2014 {p['title']}**\n"
                    f"\u2003\u2003{voter_count} voter{'s' if voter_count != 1 else ''} \u2022 "
                    f"{total_respect:,.0f} Respect \u2022 {time_left} \u2022 <#{p['thread_id']}>"
                )
            embed.description = "\n\n".join(lines)
        else:
            embed.description = "*No active proposals right now.*"

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        # Try to edit existing index message, or create a new one
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
        """Create a new proposal"""
        ptype = proposal_type.value if proposal_type else 'text'

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
        """Quick-create a yes/no curation vote for a project"""
        await interaction.response.defer()

        project_name = project
        project_url = None
        image_url = image

        if 'artizen.fund' in project or project.startswith('http'):
            project_url = project
            # Extract name from URL slug (works for Artizen and generic paths)
            slug_match = re.search(r'/([^/?#]+?)(?:\?|#|$)', project.rstrip('/'))
            if slug_match:
                slug = slug_match.group(1)
                # Only use slug if it looks like a name (not a domain or route keyword)
                if slug not in ('index', 'p', 'mf', 'project') and len(slug) > 2:
                    project_name = slug.replace('-', ' ').replace('_', ' ').title()

            # Try to scrape og: tags for title, description, image
            scraped = await _scrape_og_tags(project_url)
            if scraped.get('title'):
                project_name = scraped['title']
            if scraped.get('description') and not description:
                description = scraped['description']
            if scraped.get('image') and not image_url:
                image_url = scraped['image']

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
        """Internal method to create and post a proposal"""
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

        # Placeholder message to get ID
        placeholder = await thread.send("\u23f3 Setting up proposal...")

        # Store proposal
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

        # Build clean embed
        embed = _build_proposal_embed(proposal, self.store, interaction.user.mention)

        # Create voting view
        pid = proposal['id']
        if ptype == 'governance' and options:
            view = GovernanceVoteView(self.store, pid, options, bot=self.bot)
        else:
            view = ProposalVoteView(self.store, pid, bot=self.bot)

        self.bot.add_view(view, message_id=placeholder.id)
        await placeholder.edit(content=None, embed=embed, view=view)

        try:
            await interaction.followup.send(
                f"Proposal **#{pid}** created! Vote here \u2192 {thread.mention}"
            )
        except discord.NotFound:
            pass  # Interaction expired, but proposal was created successfully

        # Post announcement + update index in proposals channel
        await self._post_to_proposals_channel(proposal, thread)
        await self._update_proposals_index()

        # Notify #general with a rich embed linking to proposals channel
        GENERAL_CHANNEL_ID = 1127115903113367738
        general = interaction.guild.get_channel(GENERAL_CHANNEL_ID)
        if general:
            emoji = TYPE_EMOJIS.get(ptype, '\U0001f4dd')
            label = TYPE_LABELS.get(ptype, ptype.capitalize())
            proposals_channel = interaction.guild.get_channel(PROPOSALS_CHANNEL_ID)
            channel_mention = proposals_channel.mention if proposals_channel else '#proposals'

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
        """List active proposals with pagination"""
        await interaction.response.defer(ephemeral=True)

        active = self.store.get_active()
        if not active:
            await interaction.followup.send("No active proposals.", ephemeral=True)
            return

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
            # Truncate title to stay within field name limit (256 chars)
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
        """View a specific proposal"""
        await interaction.response.defer(ephemeral=True)

        proposal = self.store.get(str(proposal_id))
        if not proposal:
            await interaction.followup.send(
                f"Proposal #{proposal_id} not found.", ephemeral=True
            )
            return

        embed = _build_proposal_embed(proposal, self.store)
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
        """Close a proposal and post final results"""
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

        # Build results embed
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

            # Disable buttons on original message
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
        """Delete a proposal"""
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
        """Reopen a closed proposal"""
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

        # Reopen: set status back to active, reset created_at to now for a fresh 7-day window
        proposal['status'] = 'active'
        proposal['created_at'] = datetime.utcnow().isoformat()
        if 'closed_at' in proposal:
            del proposal['closed_at']
        self.store._save()

        # Re-register voting view
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
        """Scan the proposals channel for threads that exist in Discord but are missing from proposals.json"""
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

        # Get all known thread IDs from the store
        known_thread_ids = set()
        for p in self.store._data['proposals'].values():
            known_thread_ids.add(str(p['thread_id']))

        recovered = []
        skipped = []
        errors = []

        # Only scan active (non-archived) threads — fast, no extra API calls
        all_threads = channel.threads.copy()

        # Also try to fetch recently archived threads (limit to 20 to stay fast)
        try:
            async for thread in channel.archived_threads(limit=20):
                all_threads.append(thread)
        except Exception as e:
            self.logger.error(f"Error fetching archived threads: {e}")

        # Filter to only untracked proposal threads first
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
                # Find the bot's first message with an embed
                bot_message = None
                async for msg in thread.history(limit=3, oldest_first=True):
                    if msg.author.id == self.bot.user.id and msg.embeds:
                        bot_message = msg
                        break

                if not bot_message or not bot_message.embeds:
                    skipped.append(thread.name)
                    continue

                embed = bot_message.embeds[0]

                # Parse proposal data from the embed
                title = embed.title or thread.name.replace("Proposal: ", "")
                for e_char in TYPE_EMOJIS.values():
                    if title.startswith(e_char):
                        title = title[len(e_char):].strip()
                        break

                description = embed.description or ""
                project_url = embed.url
                image_url = embed.thumbnail.url if embed.thumbnail else None

                # Determine type
                proposal_type = 'text'
                if title.startswith("Curate:") or "Should the ZAO fund this project?" in description:
                    proposal_type = 'curate'
                elif "Funding Amount" in description:
                    proposal_type = 'funding'

                # Extract author
                author_id = str(self.bot.user.id)
                author_match = re.search(r'Proposed by <@(\d+)>', description)
                if author_match:
                    author_id = author_match.group(1)

                # Use message creation time
                created_at = bot_message.created_at.replace(tzinfo=None).isoformat()

                # Check 7-day window
                age = datetime.utcnow() - bot_message.created_at.replace(tzinfo=None)
                status = 'closed' if age >= timedelta(days=7) else 'active'

                # Create in store
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

                # Register voting view if active (no embed edit — keep it fast)
                if status == 'active':
                    view = ProposalVoteView(self.store, pid, bot=self.bot)
                    self.bot.add_view(view, message_id=bot_message.id)

            except Exception as e:
                errors.append(f"{thread.name[:40]}: {str(e)[:60]}")
                self.logger.error(f"Error recovering thread {thread.name}: {e}", exc_info=True)

        # Save all at once
        if recovered:
            self.store._save()
            await self._update_proposals_index()

        # Build report
        report = f"**Proposal Recovery Report**\n\n"
        report += f"Threads scanned: {len(all_threads)}\n"
        report += f"Already tracked: {len(known_thread_ids)}\n\n"

        if recovered:
            report += f"**Recovered ({len(recovered)}):**\n"
            for r in recovered:
                report += f"+ {r}\n"

        if skipped:
            report += f"\n**Skipped ({len(skipped)}):** (no bot embed found)\n"
            for s in skipped:
                report += f"- {s}\n"

        if errors:
            report += f"\n**Errors ({len(errors)}):**\n"
            for e in errors:
                report += f"! {e}\n"

        if recovered:
            report += f"\nUse `/proposals` to see updated list. Use `/admin_reopen_proposal` to reactivate any closed ones."

        await interaction.followup.send(report, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ProposalsCog(bot))
