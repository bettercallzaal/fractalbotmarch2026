"""
Fractal history tracking cog for the ZAO Fractal Bot.

Records the outcome of every completed fractal session (rankings, Respect
earned, facilitator, etc.) in ``history.json``.  Provides slash commands
for searching past fractals, viewing personal stats, and displaying the
cumulative Respect leaderboard.

The ``FractalHistory`` data store is attached to ``bot.fractal_history``
so the core fractal cog can call ``record()`` when a session ends.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import logging
from datetime import datetime, timezone
from cogs.base import BaseCog

# Resolve the path to the persistent data directory (project_root/data/)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

# File path for the JSON-backed fractal history store
HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')


class FractalHistory:
    """Append-only JSON store of completed fractal session results.

    Each entry captures the group name, facilitator, participant rankings,
    and Respect points awarded.  The file is a flat list (not indexed),
    so queries are linear scans -- fine for the expected scale (hundreds,
    not millions of fractals).
    """

    def __init__(self):
        """Initialise the store with an empty fractals list and load persisted data."""
        self.logger = logging.getLogger('bot')
        self._data = {'fractals': []}  # Top-level dict wrapping the list of entries
        self._load()

    def _load(self):
        """Read history from disk if the file exists."""
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                self._data = json.load(f)
        # Backwards compatibility: add next_id counter if missing
        if 'next_id' not in self._data:
            self._data['next_id'] = len(self._data['fractals']) + 1

    def _save(self):
        """Persist history atomically to prevent corruption."""
        from utils.safe_json import atomic_save
        atomic_save(HISTORY_FILE, self._data)

    def record(self, group_name: str, facilitator_id: int, facilitator_name: str,
               fractal_number: str, group_number: str, guild_id: int,
               thread_id: int, rankings: list[dict]):
        """Append a new completed fractal entry and persist to disk.

        Args:
            group_name: The display name of the fractal group.
            facilitator_id: Discord user ID of the session facilitator.
            facilitator_name: Display name of the facilitator.
            fractal_number: Identifier string for the fractal session.
            group_number: Identifier string for the group within the session.
            guild_id: Discord guild (server) snowflake where the session ran.
            thread_id: Discord thread snowflake where the session took place.
            rankings: List of dicts, each with keys ``user_id``,
                ``display_name``, ``level``, and ``respect``.  Ordered by
                rank (index 0 = 1st place).

        Returns:
            The newly created entry dict (includes an auto-incremented ``id``).
        """
        # Auto-increment ID using a persistent counter (survives deletions)
        entry = {
            'id': self._data['next_id'],
            'group_name': group_name,
            'facilitator_id': str(facilitator_id),
            'facilitator_name': facilitator_name,
            'fractal_number': fractal_number,
            'group_number': group_number,
            'guild_id': str(guild_id),
            'thread_id': str(thread_id),
            'rankings': rankings,
            'completed_at': datetime.now(timezone.utc).isoformat()
        }
        self._data['fractals'].append(entry)
        self._data['next_id'] += 1
        self._save()
        return entry

    def get_all(self) -> list[dict]:
        """Return every recorded fractal entry.

        Returns:
            A shallow copy of the full list of fractal entry dicts.
        """
        return list(self._data['fractals'])

    def get_recent(self, count: int = 10) -> list[dict]:
        """Return the *count* most recent fractal entries.

        Args:
            count: Maximum number of entries to return (default 10).

        Returns:
            A slice of the most recent entries (may be fewer than *count*).
        """
        return self._data['fractals'][-count:]

    def get_by_user(self, user_id: int) -> list[dict]:
        """Return all fractals in which *user_id* participated.

        Args:
            user_id: The Discord user snowflake to search for.

        Returns:
            A list of fractal entry dicts where the user appears in rankings.
        """
        uid = str(user_id)
        results = []
        for fractal in self._data['fractals']:
            for r in fractal['rankings']:
                # Compare as strings since user IDs are stored as strings in entries
                if str(r['user_id']) == uid:
                    results.append(fractal)
                    break  # Found the user in this fractal; move to the next one
        return results

    def get_user_stats(self, user_id: int) -> dict:
        """Aggregate lifetime stats for a single user.

        Args:
            user_id: The Discord user snowflake to aggregate stats for.

        Returns:
            A dict with keys: ``total_respect``, ``participations``,
            ``first_place``, ``second_place``, ``third_place``.
        """
        uid = str(user_id)
        total_respect = 0
        participations = 0
        placements = {1: 0, 2: 0, 3: 0}  # podium finish counts

        for fractal in self._data['fractals']:
            for i, r in enumerate(fractal['rankings']):
                if str(r['user_id']) == uid:
                    total_respect += r.get('respect', 0)
                    participations += 1
                    rank = i + 1  # rankings list is 0-indexed; rank is 1-indexed
                    if rank in placements:
                        placements[rank] += 1
                    break

        return {
            'total_respect': total_respect,
            'participations': participations,
            'first_place': placements[1],
            'second_place': placements[2],
            'third_place': placements[3],
        }

    def get_leaderboard(self) -> list[dict]:
        """Build a cumulative Respect leaderboard across all recorded fractals.

        Returns a list of user dicts sorted by total Respect descending,
        each annotated with a ``rank`` field.
        """
        user_totals = {}  # user_id -> {name, respect, participations}

        for fractal in self._data['fractals']:
            for r in fractal['rankings']:
                uid = str(r['user_id'])
                if uid not in user_totals:
                    user_totals[uid] = {
                        'user_id': uid,
                        'display_name': r['display_name'],
                        'respect': 0,
                        'participations': 0
                    }
                user_totals[uid]['respect'] += r.get('respect', 0)
                user_totals[uid]['participations'] += 1
                # Always keep the latest display name in case it changed.
                user_totals[uid]['display_name'] = r['display_name']

        # Sort by total Respect descending and assign 1-indexed ranks
        ranked = sorted(user_totals.values(), key=lambda x: -x['respect'])
        for i, entry in enumerate(ranked):
            entry['rank'] = i + 1
        return ranked

    def search(self, query: str) -> list[dict]:
        """Case-insensitive substring search across fractal entries.

        Matches are checked in priority order for each fractal:
            1. Group name
            2. Fractal number
            3. Any participant's display name

        A fractal is included at most once even if it matches on multiple
        criteria.

        Args:
            query: The search string (case-insensitive substring match).

        Returns:
            A list of matching fractal entry dicts.
        """
        query = query.lower()
        results = []
        for fractal in self._data['fractals']:
            # Priority 1: match against the group name
            if query in fractal['group_name'].lower():
                results.append(fractal)
                continue
            # Priority 2: match against the fractal session number
            if query in fractal.get('fractal_number', '').lower():
                results.append(fractal)
                continue
            # Priority 3: match against any participant's display name
            for r in fractal['rankings']:
                if query in r['display_name'].lower():
                    results.append(fractal)
                    break
        return results

    @property
    def total_fractals(self) -> int:
        """Total number of completed fractals on record."""
        return len(self._data['fractals'])


class HistoryCog(BaseCog):
    """Discord cog exposing ``/history``, ``/mystats``, and ``/rankings`` commands.

    On init, ``FractalHistory`` is attached to ``bot.fractal_history`` so the
    core fractal cog can call ``record()`` at the end of each session.
    """

    def __init__(self, bot):
        """Initialise the cog, create the history store, and attach it to the bot.

        Args:
            bot: The Discord bot instance this cog is attached to.
        """
        super().__init__(bot)
        self.history = FractalHistory()
        # Attach to bot so the fractal session cog can record results directly.
        bot.fractal_history = self.history

    @app_commands.command(
        name="history",
        description="Search completed fractal history by member name, group, or fractal number"
    )
    @app_commands.describe(query="Search by member name, group name, or fractal number (leave empty for recent)")
    async def history_search(self, interaction: discord.Interaction, query: str = None):
        """Search or browse fractal history.  Shows the 10 most recent if no query.

        Args:
            interaction: The Discord interaction context.
            query: Optional search string; omit to list the 10 most recent fractals.
        """
        await interaction.response.defer(ephemeral=True)

        if query:
            results = self.history.search(query)
            title = f"Search Results: \"{query}\""
        else:
            results = self.history.get_recent(10)
            title = "Recent Fractals"

        if not results:
            await interaction.followup.send(
                f"No fractals found{f' matching \"{query}\"' if query else ''}.",
                ephemeral=True
            )
            return

        embed = discord.Embed(title=title, color=0x57F287)

        # Cap output at 10 entries to stay within embed size limits
        for fractal in results[-10:]:
            rankings_text = []
            for i, r in enumerate(fractal['rankings']):
                # Assign medal emoji for top 3, plain number for the rest
                medal = "\U0001f947" if i == 0 else "\U0001f948" if i == 1 else "\U0001f949" if i == 2 else f"{i+1}."
                rankings_text.append(f"{medal} {r['display_name']} (+{r.get('respect', 0)})")

            # Extract just the date portion from the ISO timestamp
            date = fractal['completed_at'][:10]
            embed.add_field(
                name=f"#{fractal['id']} \u2014 {fractal['group_name']} ({date})",
                value="\n".join(rankings_text),
                inline=False
            )

        embed.set_footer(text=f"{self.history.total_fractals} total fractals \u2022 ZAO Fractal \u2022 zao.frapps.xyz")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="mystats",
        description="View your cumulative fractal stats and Respect earned"
    )
    @app_commands.describe(user="Member to look up (default: yourself)")
    async def my_stats(self, interaction: discord.Interaction, user: discord.Member = None):
        """Display lifetime fractal participation stats, podium finishes, and recent results.

        Args:
            interaction: The Discord interaction context.
            user: Optional member to look up; defaults to the invoking user.
        """
        await interaction.response.defer(ephemeral=True)

        # Default to the command invoker when no user is specified
        target = user or interaction.user
        stats = self.history.get_user_stats(target.id)

        if stats['participations'] == 0:
            await interaction.followup.send(
                f"No fractal history found for **{target.display_name}**.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Fractal Stats: {target.display_name}",
            color=0x57F287
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Total Respect Earned", value=f"**{stats['total_respect']:,}**", inline=True)
        embed.add_field(name="Fractals Participated", value=f"**{stats['participations']}**", inline=True)
        embed.add_field(
            name="Avg Respect / Fractal",
            value=f"**{stats['total_respect'] / stats['participations']:.0f}**",
            inline=True
        )
        embed.add_field(
            name="Podium Finishes",
            value=f"\U0001f947 {stats['first_place']}x  |  \U0001f948 {stats['second_place']}x  |  \U0001f949 {stats['third_place']}x",
            inline=False
        )

        # Show the user's 5 most recent fractal participations (newest first)
        recent = self.history.get_by_user(target.id)[-5:]
        if recent:
            recent_lines = []
            for f in reversed(recent):
                for i, r in enumerate(f['rankings']):
                    if str(r['user_id']) == str(target.id):
                        medal = "\U0001f947" if i == 0 else "\U0001f948" if i == 1 else "\U0001f949" if i == 2 else f"{i+1}."
                        date = f['completed_at'][:10]
                        recent_lines.append(f"{medal} {f['group_name']} \u2014 +{r.get('respect', 0)} ({date})")
                        break
            embed.add_field(name="Recent Fractals", value="\n".join(recent_lines), inline=False)

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="rankings",
        description="View cumulative Respect rankings from fractal history"
    )
    async def rankings(self, interaction: discord.Interaction):
        """Show the top-20 cumulative Respect leaderboard built from local fractal history.

        Args:
            interaction: The Discord interaction context.
        """
        await interaction.response.defer(ephemeral=True)

        leaderboard = self.history.get_leaderboard()
        if not leaderboard:
            await interaction.followup.send("No fractal history yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Cumulative Respect Rankings",
            description="Total Respect earned across all completed fractals",
            color=0x57F287
        )

        # Display the top 20 members ranked by cumulative Respect
        lines = []
        for entry in leaderboard[:20]:
            medal = ""
            if entry['rank'] == 1:
                medal = "\U0001f947 "
            elif entry['rank'] == 2:
                medal = "\U0001f948 "
            elif entry['rank'] == 3:
                medal = "\U0001f949 "

            lines.append(
                f"{medal}**{entry['rank']}.** {entry['display_name']} \u2014 "
                f"**{entry['respect']:,}** Respect ({entry['participations']} fractals)"
            )

        embed.add_field(name="Top Members", value="\n".join(lines) or "None", inline=False)
        embed.set_footer(
            text=f"{self.history.total_fractals} fractals recorded \u2022 ZAO Fractal \u2022 zao.frapps.xyz"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    """Entry point called by discord.py's extension loader to register the HistoryCog."""
    await bot.add_cog(HistoryCog(bot))
