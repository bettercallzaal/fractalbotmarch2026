"""
Fractal history tracking cog for the ZAO Fractal Bot.

Records the outcome of every completed fractal session (rankings, Respect
earned, facilitator, etc.) in the Supabase ``fractal_sessions`` and
``fractal_rankings`` tables.  Provides slash commands for searching past
fractals, viewing personal stats, and displaying the cumulative Respect
leaderboard.

The ``FractalHistory`` data store is attached to ``bot.fractal_history``
so the core fractal cog can call ``record()`` when a session ends.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone
from cogs.base import BaseCog
from utils.supabase_client import get_supabase


class FractalHistory:
    """Supabase-backed store of completed fractal session results.

    Session metadata lives in ``fractal_sessions`` and per-participant
    rankings live in ``fractal_rankings``.  All queries go directly to
    Supabase; there is no in-memory state.
    """

    def __init__(self):
        """Initialise the store with a Supabase client."""
        self.logger = logging.getLogger('bot')
        self.sb = get_supabase()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_to_entry(session: dict) -> dict:
        """Convert a Supabase session row (with embedded rankings) to the
        dict format the rest of the codebase expects.

        Expected output format::

            {
                'id': int,
                'group_name': str,
                'facilitator_id': str,
                'facilitator_name': str,
                'fractal_number': str,
                'group_number': str,
                'guild_id': str,
                'thread_id': str,
                'rankings': [{'user_id': str, 'display_name': str,
                              'level': int, 'respect': int}, ...],
                'completed_at': str (ISO-8601),
            }
        """
        raw_rankings = session.get("fractal_rankings", [])
        # Sort by rank_position so index order matches the old list convention
        sorted_rankings = sorted(raw_rankings, key=lambda r: r.get("rank_position", 0))
        rankings = [
            {
                "user_id": r["user_id"],
                "display_name": r["display_name"],
                "level": r["level"],
                "respect": r["respect"],
            }
            for r in sorted_rankings
        ]
        return {
            "id": session["id"],
            "group_name": session["group_name"],
            "facilitator_id": session["facilitator_id"],
            "facilitator_name": session["facilitator_name"],
            "fractal_number": session.get("fractal_number", ""),
            "group_number": session.get("group_number", ""),
            "guild_id": session["guild_id"],
            "thread_id": session.get("thread_id", ""),
            "rankings": rankings,
            "completed_at": session["completed_at"],
        }

    # ------------------------------------------------------------------
    # Public API (same interface the command handlers rely on)
    # ------------------------------------------------------------------

    def record(self, group_name: str, facilitator_id: int, facilitator_name: str,
               fractal_number: str, group_number: str, guild_id: int,
               thread_id: int, rankings: list[dict]):
        """Insert a completed fractal session and its rankings into Supabase.

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
            The newly created entry dict (same shape as the old JSON format).
        """
        session_row = {
            "group_name": group_name,
            "facilitator_id": str(facilitator_id),
            "facilitator_name": facilitator_name,
            "fractal_number": fractal_number,
            "group_number": group_number,
            "guild_id": str(guild_id),
            "thread_id": str(thread_id),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        session_result = (
            self.sb.table("fractal_sessions")
            .insert(session_row)
            .execute()
        )
        session = session_result.data[0]
        session_id = session["id"]

        # Bulk-insert rankings
        ranking_rows = [
            {
                "session_id": session_id,
                "user_id": str(r["user_id"]),
                "display_name": r["display_name"],
                "level": r["level"],
                "respect": r.get("respect", 0),
                "rank_position": i + 1,
            }
            for i, r in enumerate(rankings)
        ]
        if ranking_rows:
            self.sb.table("fractal_rankings").insert(ranking_rows).execute()

        # Return in the legacy dict format
        entry = dict(session_row)
        entry["id"] = session_id
        entry["rankings"] = rankings
        return entry

    def get_all(self) -> list[dict]:
        """Return every recorded fractal entry with embedded rankings."""
        result = (
            self.sb.table("fractal_sessions")
            .select("*, fractal_rankings(*)")
            .order("id")
            .execute()
        )
        return [self._session_to_entry(s) for s in result.data]

    def get_recent(self, count: int = 10) -> list[dict]:
        """Return the *count* most recent fractal entries.

        Args:
            count: Maximum number of entries to return (default 10).

        Returns:
            The most recent entries sorted oldest-first (matching old behaviour).
        """
        result = (
            self.sb.table("fractal_sessions")
            .select("*, fractal_rankings(*)")
            .order("completed_at", desc=True)
            .limit(count)
            .execute()
        )
        entries = [self._session_to_entry(s) for s in result.data]
        # Reverse so the list is oldest-first (matches old slice behaviour)
        entries.reverse()
        return entries

    def get_by_user(self, user_id: int) -> list[dict]:
        """Return all fractals in which *user_id* participated.

        Uses an inner join on ``fractal_rankings`` to filter sessions
        where the user appears.
        """
        uid = str(user_id)
        result = (
            self.sb.table("fractal_sessions")
            .select("*, fractal_rankings!inner(*)")
            .eq("fractal_rankings.user_id", uid)
            .order("id")
            .execute()
        )
        # The inner join may return only the matched ranking; re-fetch full
        # rankings for each matched session so the entry looks complete.
        if not result.data:
            return []
        session_ids = [s["id"] for s in result.data]
        full_result = (
            self.sb.table("fractal_sessions")
            .select("*, fractal_rankings(*)")
            .in_("id", session_ids)
            .order("id")
            .execute()
        )
        return [self._session_to_entry(s) for s in full_result.data]

    def get_user_stats(self, user_id: int) -> dict:
        """Aggregate lifetime stats for a single user.

        Tries the ``respect_leaderboard`` VIEW first for efficiency;
        falls back to a manual scan if the view is not available.

        Returns:
            A dict with keys: ``total_respect``, ``participations``,
            ``first_place``, ``second_place``, ``third_place``.
        """
        uid = str(user_id)
        try:
            result = (
                self.sb.table("respect_leaderboard")
                .select("*")
                .eq("user_id", uid)
                .maybe_single()
                .execute()
            )
            if result.data:
                row = result.data
                return {
                    "total_respect": int(row.get("total_respect", 0)),
                    "participations": int(row.get("participations", 0)),
                    "first_place": int(row.get("first_place", 0)),
                    "second_place": int(row.get("second_place", 0)),
                    "third_place": int(row.get("third_place", 0)),
                }
        except Exception:
            self.logger.debug("respect_leaderboard view unavailable, falling back to manual scan")

        # Fallback: compute from raw rankings
        rankings_result = (
            self.sb.table("fractal_rankings")
            .select("respect, rank_position")
            .eq("user_id", uid)
            .execute()
        )
        total_respect = 0
        participations = 0
        placements = {1: 0, 2: 0, 3: 0}
        for r in rankings_result.data:
            total_respect += r.get("respect", 0)
            participations += 1
            pos = r.get("rank_position", 0)
            if pos in placements:
                placements[pos] += 1

        return {
            "total_respect": total_respect,
            "participations": participations,
            "first_place": placements[1],
            "second_place": placements[2],
            "third_place": placements[3],
        }

    def get_leaderboard(self) -> list[dict]:
        """Build a cumulative Respect leaderboard across all recorded fractals.

        Uses the ``respect_leaderboard`` VIEW when available, otherwise
        aggregates manually from ``fractal_rankings``.

        Returns a list of user dicts sorted by total Respect descending,
        each annotated with a ``rank`` field.
        """
        try:
            result = (
                self.sb.table("respect_leaderboard")
                .select("*")
                .execute()
            )
            if result.data:
                return [
                    {
                        "user_id": row["user_id"],
                        "display_name": row["display_name"],
                        "respect": int(row.get("total_respect", 0)),
                        "participations": int(row.get("participations", 0)),
                        "rank": int(row.get("rank", 0)),
                    }
                    for row in result.data
                ]
        except Exception:
            self.logger.debug("respect_leaderboard view unavailable, falling back to manual scan")

        # Fallback: manual aggregation
        rankings_result = (
            self.sb.table("fractal_rankings")
            .select("user_id, display_name, respect")
            .execute()
        )
        user_totals: dict[str, dict] = {}
        for r in rankings_result.data:
            uid = r["user_id"]
            if uid not in user_totals:
                user_totals[uid] = {
                    "user_id": uid,
                    "display_name": r["display_name"],
                    "respect": 0,
                    "participations": 0,
                }
            user_totals[uid]["respect"] += r.get("respect", 0)
            user_totals[uid]["participations"] += 1
            user_totals[uid]["display_name"] = r["display_name"]

        ranked = sorted(user_totals.values(), key=lambda x: -x["respect"])
        for i, entry in enumerate(ranked):
            entry["rank"] = i + 1
        return ranked

    def search(self, query: str) -> list[dict]:
        """Case-insensitive substring search across fractal entries.

        Searches group_name, fractal_number, and participant display names.

        Args:
            query: The search string (case-insensitive substring match).

        Returns:
            A list of matching fractal entry dicts.
        """
        q = query.strip()
        # Search sessions by group_name or fractal_number
        session_result = (
            self.sb.table("fractal_sessions")
            .select("*, fractal_rankings(*)")
            .or_(f"group_name.ilike.%{q}%,fractal_number.ilike.%{q}%")
            .order("id")
            .execute()
        )
        found_ids = {s["id"] for s in session_result.data}
        entries = [self._session_to_entry(s) for s in session_result.data]

        # Also search by participant display name
        name_result = (
            self.sb.table("fractal_rankings")
            .select("session_id")
            .ilike("display_name", f"%{q}%")
            .execute()
        )
        extra_ids = [r["session_id"] for r in name_result.data if r["session_id"] not in found_ids]
        if extra_ids:
            extra_result = (
                self.sb.table("fractal_sessions")
                .select("*, fractal_rankings(*)")
                .in_("id", list(set(extra_ids)))
                .order("id")
                .execute()
            )
            entries.extend(self._session_to_entry(s) for s in extra_result.data)

        return entries

    @property
    def total_fractals(self) -> int:
        """Total number of completed fractals on record."""
        result = (
            self.sb.table("fractal_sessions")
            .select("id", count="exact")
            .execute()
        )
        return result.count or 0


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
