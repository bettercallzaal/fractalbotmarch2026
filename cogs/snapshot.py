"""
Snapshot governance poll notification cog for the ZAO Fractal Discord bot.

Monitors the ZAO Snapshot space for governance proposals and posts
notifications to Discord when:
    - A new proposal is created (gold embed)
    - A proposal is within 24h of closing (red embed)
    - A proposal closes (results embed with winning choice)

Commands:
    /snapshot           -- List active Snapshot proposals with vote links.
    /admin_set_snapshot_space -- (admin) Change the Snapshot space ID.

Background task:
    A once-per-5-minute loop queries the Snapshot GraphQL API, detects new
    proposals and closing/closed transitions, and posts embeds to the
    configured channel.
"""

import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone

from cogs.base import BaseCog
from config.config import FRACTAL_BOT_CHANNEL_ID

# ---------------------------------------------------------------------------
# Snapshot Configuration
# ---------------------------------------------------------------------------
SNAPSHOT_API_URL = "https://hub.snapshot.org/graphql"

# The Snapshot space ID, overridable via env var.  Can also be changed at
# runtime with /admin_set_snapshot_space.
DEFAULT_SNAPSHOT_SPACE = os.getenv("SNAPSHOT_SPACE", "zao.eth")

# ---------------------------------------------------------------------------
# GraphQL Queries
# ---------------------------------------------------------------------------
PROPOSALS_QUERY = """
query Proposals($space: String!) {
  proposals(
    where: { space: $space, state: "active" }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    body
    choices
    start
    end
    state
    scores
    scores_total
    votes
    link
  }
}
"""

CLOSED_PROPOSALS_QUERY = """
query ClosedProposals($space: String!) {
  proposals(
    where: { space: $space, state: "closed" }
    orderBy: "end"
    orderDirection: desc
    first: 10
  ) {
    id
    title
    body
    choices
    start
    end
    state
    scores
    scores_total
    votes
    link
  }
}
"""


def _snapshot_link(proposal_id: str, space: str) -> str:
    """Build a direct link to a Snapshot proposal."""
    return f"https://snapshot.org/#/{space}/proposal/{proposal_id}"


def _truncate(text: str, length: int = 200) -> str:
    """Truncate text to *length* characters, appending an ellipsis if needed."""
    if not text:
        return "*No description provided.*"
    text = text.replace("\n", " ").strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "..."


def _format_choices(choices: list[str], scores: list[float] | None = None) -> str:
    """Format proposal choices into a readable list with optional scores."""
    if not choices:
        return "*No choices listed.*"
    lines = []
    for i, choice in enumerate(choices):
        if scores and i < len(scores):
            lines.append(f"**{i + 1}.** {choice} — {scores[i]:,.2f} votes")
        else:
            lines.append(f"**{i + 1}.** {choice}")
    return "\n".join(lines)


class SnapshotCog(BaseCog):
    """Monitors Snapshot governance proposals and posts Discord notifications."""

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.snapshot_space: str = DEFAULT_SNAPSHOT_SPACE
        # Track proposal IDs we have already announced, keyed by notification type.
        self._seen_new: set[str] = set()
        self._warned_closing: set[str] = set()
        self._announced_closed: set[str] = set()

    async def cog_load(self):
        """Start the polling loop when the cog is loaded."""
        self.snapshot_poll.start()

    async def cog_unload(self):
        """Stop the polling loop when the cog is unloaded."""
        self.snapshot_poll.cancel()

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------
    async def _query_snapshot(self, query: str, variables: dict) -> dict | None:
        """Execute a GraphQL query against the Snapshot Hub API.

        Returns the ``data`` portion of the response, or ``None`` on error.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    SNAPSHOT_API_URL,
                    json={"query": query, "variables": variables},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            f"[SNAPSHOT] API returned status {resp.status}"
                        )
                        return None
                    result = await resp.json()
                    return result.get("data")
        except Exception as exc:
            self.logger.error(f"[SNAPSHOT] GraphQL request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Embed builders
    # ------------------------------------------------------------------
    def _new_proposal_embed(self, proposal: dict) -> discord.Embed:
        """Build a gold embed for a newly detected proposal."""
        start_ts = proposal["start"]
        end_ts = proposal["end"]

        embed = discord.Embed(
            title=f"\U0001f5f3\ufe0f {proposal['title']}",
            description=_truncate(proposal["body"]),
            color=discord.Color.gold(),
            url=_snapshot_link(proposal["id"], self.snapshot_space),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Choices",
            value=_format_choices(proposal["choices"]),
            inline=False,
        )
        embed.add_field(
            name="Voting Opens",
            value=f"<t:{start_ts}:F>",
            inline=True,
        )
        embed.add_field(
            name="Voting Closes",
            value=f"<t:{end_ts}:F>",
            inline=True,
        )
        embed.add_field(
            name="Current Votes",
            value=str(proposal.get("votes", 0)),
            inline=True,
        )
        embed.add_field(
            name="Vote Now",
            value=f"[\U0001f517 Open on Snapshot]({_snapshot_link(proposal['id'], self.snapshot_space)})",
            inline=False,
        )
        embed.set_footer(text="Vote with your Respect on Snapshot")
        return embed

    def _closing_soon_embed(self, proposal: dict) -> discord.Embed:
        """Build a red embed warning that a proposal is closing within 24h."""
        end_ts = proposal["end"]
        embed = discord.Embed(
            title=f"\u26a0\ufe0f Snapshot vote closing soon!",
            description=f"**{proposal['title']}** ends <t:{end_ts}:R>.",
            color=discord.Color.red(),
            url=_snapshot_link(proposal["id"], self.snapshot_space),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Choices & Current Scores",
            value=_format_choices(proposal["choices"], proposal.get("scores")),
            inline=False,
        )
        embed.add_field(
            name="Total Votes",
            value=str(proposal.get("votes", 0)),
            inline=True,
        )
        embed.add_field(
            name="Closes",
            value=f"<t:{end_ts}:F>",
            inline=True,
        )
        embed.add_field(
            name="Vote Now",
            value=f"[\U0001f517 Open on Snapshot]({_snapshot_link(proposal['id'], self.snapshot_space)})",
            inline=False,
        )
        embed.set_footer(text="Vote with your Respect on Snapshot")
        return embed

    def _closed_proposal_embed(self, proposal: dict) -> discord.Embed:
        """Build a blue embed showing final results of a closed proposal."""
        choices = proposal.get("choices", [])
        scores = proposal.get("scores", [])
        total = proposal.get("scores_total", 0)
        votes = proposal.get("votes", 0)

        # Determine the winning choice
        winner = "N/A"
        if scores and choices:
            max_idx = scores.index(max(scores))
            winner = choices[max_idx]

        embed = discord.Embed(
            title=f"\U0001f4ca Snapshot Vote Closed",
            description=f"**{proposal['title']}**",
            color=discord.Color.blue(),
            url=_snapshot_link(proposal["id"], self.snapshot_space),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Winning Choice",
            value=f"\U0001f3c6 **{winner}**",
            inline=False,
        )
        embed.add_field(
            name="Final Results",
            value=_format_choices(choices, scores),
            inline=False,
        )
        embed.add_field(
            name="Total Vote Weight",
            value=f"{total:,.2f}",
            inline=True,
        )
        embed.add_field(
            name="Voters",
            value=str(votes),
            inline=True,
        )
        embed.set_footer(text="Vote with your Respect on Snapshot")
        return embed

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------
    @tasks.loop(minutes=5)
    async def snapshot_poll(self):
        """Poll Snapshot for new, closing, and closed proposals."""
        channel = self.bot.get_channel(FRACTAL_BOT_CHANNEL_ID)
        if channel is None:
            self.logger.warning(
                "[SNAPSHOT] Cannot find notification channel "
                f"{FRACTAL_BOT_CHANNEL_ID}"
            )
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # ---- Active proposals ----
        data = await self._query_snapshot(
            PROPOSALS_QUERY, {"space": self.snapshot_space}
        )
        if data and data.get("proposals"):
            for prop in data["proposals"]:
                pid = prop["id"]

                # New proposal notification
                if pid not in self._seen_new:
                    self._seen_new.add(pid)
                    embed = self._new_proposal_embed(prop)
                    await channel.send(
                        content="\U0001f5f3\ufe0f **New Snapshot Proposal!**",
                        embed=embed,
                    )
                    self.logger.info(
                        f"[SNAPSHOT] Announced new proposal: {prop['title']}"
                    )

                # Closing soon (within 24h) notification
                end_ts = prop["end"]
                if (
                    pid not in self._warned_closing
                    and 0 < (end_ts - now_ts) <= 86400
                ):
                    self._warned_closing.add(pid)
                    embed = self._closing_soon_embed(prop)
                    await channel.send(
                        content="\u26a0\ufe0f **Snapshot vote closing soon!**",
                        embed=embed,
                    )
                    self.logger.info(
                        f"[SNAPSHOT] Warned closing soon: {prop['title']}"
                    )

        # ---- Recently closed proposals ----
        closed_data = await self._query_snapshot(
            CLOSED_PROPOSALS_QUERY, {"space": self.snapshot_space}
        )
        if closed_data and closed_data.get("proposals"):
            for prop in closed_data["proposals"]:
                pid = prop["id"]
                # Only announce proposals that we previously tracked as active
                # and have not yet announced as closed.
                if pid in self._seen_new and pid not in self._announced_closed:
                    self._announced_closed.add(pid)
                    embed = self._closed_proposal_embed(prop)
                    await channel.send(
                        content="\U0001f4ca **Snapshot Vote Results**",
                        embed=embed,
                    )
                    self.logger.info(
                        f"[SNAPSHOT] Announced closed proposal: {prop['title']}"
                    )

    @snapshot_poll.before_loop
    async def before_snapshot_poll(self):
        """Wait until the bot is ready, then seed seen IDs to avoid spam."""
        await self.bot.wait_until_ready()

        # Seed the seen set with currently active proposals so the bot does
        # not re-announce everything on first startup.
        data = await self._query_snapshot(
            PROPOSALS_QUERY, {"space": self.snapshot_space}
        )
        if data and data.get("proposals"):
            for prop in data["proposals"]:
                self._seen_new.add(prop["id"])
            self.logger.info(
                f"[SNAPSHOT] Seeded {len(self._seen_new)} existing active proposals"
            )

        # Also seed closed proposals so we do not announce old results.
        closed_data = await self._query_snapshot(
            CLOSED_PROPOSALS_QUERY, {"space": self.snapshot_space}
        )
        if closed_data and closed_data.get("proposals"):
            for prop in closed_data["proposals"]:
                self._announced_closed.add(prop["id"])
            self.logger.info(
                f"[SNAPSHOT] Seeded {len(self._announced_closed)} existing closed proposals"
            )

    # ------------------------------------------------------------------
    # /snapshot -- list active proposals
    # ------------------------------------------------------------------
    @app_commands.command(
        name="snapshot",
        description="List active Snapshot governance proposals",
    )
    async def snapshot_list(self, interaction: discord.Interaction):
        """Show all currently active proposals on the configured Snapshot space."""
        await interaction.response.defer()

        data = await self._query_snapshot(
            PROPOSALS_QUERY, {"space": self.snapshot_space}
        )

        if not data or not data.get("proposals"):
            await interaction.followup.send(
                "No active Snapshot proposals right now. "
                f"Check back later or visit <https://snapshot.org/#/{self.snapshot_space}>.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="\U0001f5f3\ufe0f Active Snapshot Proposals",
            description=f"Space: **{self.snapshot_space}**",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )

        for prop in data["proposals"][:10]:  # Cap at 10 to stay within embed limits
            end_ts = prop["end"]
            votes = prop.get("votes", 0)
            link = _snapshot_link(prop["id"], self.snapshot_space)
            embed.add_field(
                name=prop["title"],
                value=(
                    f"{_truncate(prop['body'], 100)}\n"
                    f"**Votes:** {votes} | **Closes:** <t:{end_ts}:R>\n"
                    f"[\U0001f517 Vote on Snapshot]({link})"
                ),
                inline=False,
            )

        embed.set_footer(text="Vote with your Respect on Snapshot")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /admin_set_snapshot_space -- change the space at runtime
    # ------------------------------------------------------------------
    @app_commands.command(
        name="admin_set_snapshot_space",
        description="(Admin) Set the Snapshot space ID for governance poll tracking",
    )
    @app_commands.describe(space="Snapshot space ID (e.g. zao.eth)")
    async def admin_set_snapshot_space(
        self, interaction: discord.Interaction, space: str
    ):
        """Update the Snapshot space ID used for polling."""
        if not self.is_supreme_admin(interaction.user):
            await interaction.response.send_message(
                "\u274c You need the Supreme Admin role to change the Snapshot space.",
                ephemeral=True,
            )
            return

        old_space = self.snapshot_space
        self.snapshot_space = space.strip()

        # Clear tracking sets since the space changed
        self._seen_new.clear()
        self._warned_closing.clear()
        self._announced_closed.clear()

        embed = discord.Embed(
            title="\u2699\ufe0f Snapshot Space Updated",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Previous", value=old_space, inline=True)
        embed.add_field(name="New", value=self.snapshot_space, inline=True)
        embed.set_footer(text=f"Changed by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)
        self.logger.info(
            f"[SNAPSHOT] Space changed from '{old_space}' to '{self.snapshot_space}' "
            f"by {interaction.user}"
        )


async def setup(bot: commands.Bot):
    """discord.py extension entry point -- registers SnapshotCog with the bot."""
    await bot.add_cog(SnapshotCog(bot))
