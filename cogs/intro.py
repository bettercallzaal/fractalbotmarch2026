"""
Intro cog for the ZAO Fractal Discord bot.

Provides slash commands to look up member introductions posted in the
#intros channel. Introductions are stored in the Supabase ``intros`` table
so that repeated lookups do not require re-scanning channel history. An
admin command is available to rebuild the entire cache from scratch.

Key components:
    - slugify(): Converts display names to URL-safe slugs for thezao.com links.
    - IntroCache: Supabase-backed cache mapping Discord user IDs to their
      introduction text, message ID, and timestamp.
    - IntroCog: Discord cog exposing /intro and /admin_refresh_intros commands.
"""

import discord
from discord import app_commands
from discord.ext import commands
import re
from datetime import datetime
from cogs.base import BaseCog
from config.config import INTROS_CHANNEL_ID
from utils.supabase_client import get_supabase


def slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug for thezao.com community pages.

    Applies the following transformations in order:
        1. Lowercase and strip leading/trailing whitespace.
        2. Remove all characters that are not alphanumeric, whitespace, or hyphens.
        3. Collapse whitespace and underscores into single hyphens.
        4. Collapse consecutive hyphens into a single hyphen.
        5. Strip leading and trailing hyphens.

    Args:
        name: The member's display name to slugify.

    Returns:
        A URL-safe, lowercase, hyphen-separated slug string.
    """
    # Lowercase and strip outer whitespace
    slug = name.lower().strip()
    # Remove special characters (keep word chars, whitespace, hyphens)
    slug = re.sub(r'[^\w\s-]', '', slug)
    # Replace whitespace runs and underscores with a single hyphen
    slug = re.sub(r'[\s_]+', '-', slug)
    # Collapse multiple consecutive hyphens into one
    slug = re.sub(r'-+', '-', slug)
    # Remove any leading/trailing hyphens left over
    return slug.strip('-')


class IntroCache:
    """Supabase-backed cache of member introductions from the #intros channel.

    Stores each member's first non-empty message from the #intros channel
    in the ``intros`` Supabase table, keyed by their Discord user ID.

    Public methods return data in the same dict format the command handlers
    expect::

        {
            "text": "<intro message content>",
            "message_id": <Discord message snowflake (int or str)>,
            "timestamp": "<ISO-8601 datetime string>"
        }
    """

    def __init__(self):
        """Initialise with a Supabase client."""
        self.sb = get_supabase()

    @staticmethod
    def _row_to_entry(row: dict) -> dict:
        """Convert a Supabase ``intros`` row to the legacy cache format."""
        return {
            "text": row["intro_text"],
            "message_id": int(row["message_id"]) if row.get("message_id") else None,
            "timestamp": row.get("posted_at", row.get("cached_at", "")),
        }

    def get(self, discord_id: int) -> dict | None:
        """Retrieve a cached intro entry by Discord user ID.

        Args:
            discord_id: The Discord user's snowflake ID.

        Returns:
            A dict with 'text', 'message_id', and 'timestamp' keys,
            or None if no intro is cached for this user.
        """
        result = (
            self.sb.table("intros")
            .select("*")
            .eq("discord_id", str(discord_id))
            .maybe_single()
            .execute()
        )
        if result.data:
            return self._row_to_entry(result.data)
        return None

    def set(self, discord_id: int, text: str, message_id: int, timestamp: str):
        """Store or update an intro entry in Supabase.

        Args:
            discord_id: The Discord user's snowflake ID.
            text: The full text of the introduction message.
            message_id: The Discord message snowflake ID.
            timestamp: ISO-8601 formatted creation timestamp.
        """
        self.sb.table("intros").upsert(
            {
                "discord_id": str(discord_id),
                "intro_text": text,
                "message_id": str(message_id),
                "posted_at": timestamp,
            },
            on_conflict="discord_id",
        ).execute()

    def clear(self):
        """Remove all cached entries from Supabase."""
        self.sb.table("intros").delete().neq("id", 0).execute()

    @property
    def size(self) -> int:
        """Return the number of cached introductions."""
        result = (
            self.sb.table("intros")
            .select("id", count="exact")
            .execute()
        )
        return result.count or 0


class IntroCog(BaseCog):
    """Discord cog for looking up member introductions from the #intros channel.

    Provides two slash commands:
        /intro <user>            -- Look up a specific member's introduction.
        /admin_refresh_intros    -- (Admin only) Rebuild the entire intro cache
                                    by scanning #intros channel history.

    The cog lazily populates the cache: if a user's intro is not cached when
    /intro is invoked, it scans the channel history for that user's first
    message and caches it for future lookups.
    """

    def __init__(self, bot):
        """Initialise the cog and create the intro cache instance.

        Args:
            bot: The Discord bot instance this cog is attached to.
        """
        super().__init__(bot)
        self.intro_cache = IntroCache()

    @app_commands.command(
        name="intro",
        description="Look up a member's introduction from #intros"
    )
    @app_commands.describe(user="The member to look up")
    async def intro(self, interaction: discord.Interaction, user: discord.Member):
        """Show a member's introduction from the #intros channel.

        First checks the local cache; if the user's intro is not cached,
        scans #intros channel history (oldest-first) for their first non-empty
        message. Displays the intro in a rich embed with a link to their
        thezao.com community page, wallet address (if registered), and a jump
        link to the original Discord message.

        Args:
            interaction: The Discord interaction context.
            user: The guild member whose introduction to look up.
        """
        await interaction.response.defer()

        intro_data = self.intro_cache.get(user.id)

        # If not cached, search the #intros channel
        if not intro_data:
            channel = self.bot.get_channel(INTROS_CHANNEL_ID)
            if not channel:
                await interaction.followup.send(
                    "Could not find the #intros channel.", ephemeral=True
                )
                return

            # Search for user's first message in #intros
            found = False
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.id == user.id and message.content.strip():
                    self.intro_cache.set(
                        user.id,
                        message.content,
                        message.id,
                        message.created_at.isoformat()
                    )
                    intro_data = self.intro_cache.get(user.id)
                    found = True
                    break

            if not found:
                await interaction.followup.send(
                    f"No introduction found for **{user.display_name}** in <#{INTROS_CHANNEL_ID}>.",
                    ephemeral=True
                )
                return

        # Build the response embed with the intro text
        intro_text = intro_data['text']
        # Truncate to fit within Discord embed field limits (1024 chars)
        if len(intro_text) > 1024:
            intro_text = intro_text[:1021] + "..."

        # Generate a URL-safe slug for the member's thezao.com community page
        slug = slugify(user.display_name)
        community_url = f"https://thezao.com/community/{slug}"

        embed = discord.Embed(
            title=f"Introduction: {user.display_name}",
            color=0x57F287
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Intro", value=intro_text, inline=False)
        embed.add_field(
            name="Community Page",
            value=f"[thezao.com/community/{slug}]({community_url})",
            inline=True
        )

        # Show the member's wallet address (abbreviated) if they have one registered
        wallet = None
        if hasattr(self.bot, 'wallet_registry'):
            wallet = self.bot.wallet_registry.lookup(user)
        if wallet:
            short = f"{wallet[:6]}...{wallet[-4:]}"
            embed.add_field(name="Wallet", value=f"`{short}`", inline=True)

        # Add a "Jump to intro" link pointing to the original Discord message
        if intro_data.get('message_id'):
            msg_link = f"https://discord.com/channels/{interaction.guild_id}/{INTROS_CHANNEL_ID}/{intro_data['message_id']}"
            embed.add_field(
                name="Original Message",
                value=f"[Jump to intro]({msg_link})",
                inline=True
            )

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="admin_refresh_intros",
        description="[ADMIN] Rebuild the intro cache from #intros channel history"
    )
    async def admin_refresh_intros(self, interaction: discord.Interaction):
        """Rebuild the entire intro cache by scanning #intros channel history.

        Clears the existing cache, then iterates through all messages in the
        #intros channel (oldest first), recording each non-bot user's first
        non-empty message. This is useful when the cache file is out of date
        or has been lost.

        Restricted to users with the Supreme Admin role.

        Args:
            interaction: The Discord interaction context.
        """
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send(
                "You need the **Supreme Admin** role to use this command.",
                ephemeral=True
            )
            return

        channel = self.bot.get_channel(INTROS_CHANNEL_ID)
        if not channel:
            await interaction.followup.send(
                "Could not find the #intros channel.", ephemeral=True
            )
            return

        # Wipe the cache and rebuild from scratch
        self.intro_cache.clear()
        count = 0
        seen_users = set()  # Track which users we've already recorded

        async for message in channel.history(limit=None, oldest_first=True):
            # Skip bot messages and empty messages
            if message.author.bot or not message.content.strip():
                continue
            # Only record the first message per user (their intro)
            if message.author.id in seen_users:
                continue

            seen_users.add(message.author.id)
            self.intro_cache.set(
                message.author.id,
                message.content,
                message.id,
                message.created_at.isoformat()
            )
            count += 1

        await interaction.followup.send(
            f"Intro cache rebuilt. **{count}** introductions cached.",
            ephemeral=True
        )


async def setup(bot):
    """Entry point called by discord.py's extension loader to register the IntroCog."""
    await bot.add_cog(IntroCog(bot))
