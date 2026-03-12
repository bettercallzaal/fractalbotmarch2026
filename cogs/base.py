"""
BaseCog -- Shared foundation for all FractalBot cogs.

This module provides two things every other cog relies on:

1. **Interaction deduplication** (`_InteractionDedup` / `is_duplicate_interaction`):
   discord.py may fire the same interaction twice when a command is registered
   both globally and per-guild.  Every command handler should call
   ``BaseCog.is_duplicate_interaction(interaction)`` at its very first line and
   bail out if it returns ``True``.

2. **Common helper methods** on ``BaseCog`` -- permission checks, voice-state
   validation, and a pre-configured logger -- so that individual cogs can
   inherit from ``BaseCog`` instead of reimplementing boilerplate.
"""

import discord
import logging
from collections import OrderedDict
from discord.ext import commands
from config.config import SUPREME_ADMIN_ROLE_ID


class _InteractionDedup:
    """LRU-bounded set of interaction IDs used to block duplicate dispatches.

    discord.py can dispatch the same interaction to a command handler multiple
    times when commands exist in both global and guild trees.  This provides
    a synchronous (no-await) check that catches duplicates before any response
    is attempted, avoiding "interaction already acknowledged" errors.

    The internal store is an ``OrderedDict`` used as an insertion-ordered set.
    When it exceeds *maxsize* entries the oldest item is evicted (FIFO), which
    keeps memory usage constant regardless of uptime.
    """

    def __init__(self, maxsize: int = 200):
        """Initialise with a maximum cache size.

        Parameters
        ----------
        maxsize:
            The number of interaction IDs to retain before evicting the oldest.
        """
        self._seen: OrderedDict = OrderedDict()
        self._maxsize = maxsize

    def is_duplicate(self, interaction_id: int) -> bool:
        """Return ``True`` if *interaction_id* was already recorded.

        If the ID is new it is inserted into the cache (and the oldest entry
        is evicted when the cache is full).
        """
        if interaction_id in self._seen:
            return True
        self._seen[interaction_id] = True
        # Evict the oldest entry to keep memory bounded.
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


# Module-level singleton so every cog that inherits BaseCog shares the same
# dedup state.  This is safe because the bot runs in a single event loop.
_dedup = _InteractionDedup()


class BaseCog(commands.Cog):
    """Shared base class for all FractalBot cogs.

    Subclasses automatically get:
    * ``self.bot``   -- reference to the ``commands.Bot`` instance.
    * ``self.logger`` -- a ``logging.Logger`` under the ``'bot'`` namespace.
    * ``is_duplicate_interaction()`` -- static dedup guard.
    * ``is_supreme_admin()``  -- role-based admin check.
    * ``check_voice_state()`` -- validates a user is in a voice channel and
      the channel has an acceptable number of human participants.
    """

    def __init__(self, bot: commands.Bot):
        """Store bot reference and create a child logger for this cog."""
        self.bot = bot
        self.logger = logging.getLogger('bot')

    @staticmethod
    def is_duplicate_interaction(interaction: discord.Interaction) -> bool:
        """Return ``True`` if this interaction has already been handled.

        Must be called at the **very top** of every slash-command callback.
        If it returns ``True`` the handler should return immediately without
        sending a response (the first dispatch already replied).
        """
        return _dedup.is_duplicate(interaction.id)

    def is_supreme_admin(self, member: discord.Member) -> bool:
        """Check whether *member* holds the Supreme Admin role.

        The role ID is read from ``config.config.SUPREME_ADMIN_ROLE_ID`` so it
        can differ between environments (dev vs. production).
        """
        return any(role.id == SUPREME_ADMIN_ROLE_ID for role in member.roles)

    async def check_voice_state(self, user: discord.Member) -> dict:
        """Validate that *user* is in a voice channel with 1-6 human members.

        Returns a dict with keys:
        * ``success`` (bool) -- whether all checks passed.
        * ``message`` (str) -- a user-facing status/error string.
        * ``members`` (list[discord.Member]) -- non-bot members in the channel
          (empty on failure).
        * ``channel`` (discord.VoiceChannel | None) -- the voice channel, or
          ``None`` if the user is not connected.

        The minimum of 1 member (instead of 2) allows solo testing during
        development while the error message still references the real minimum.
        """
        # Ensure the user is connected to a voice channel at all.
        if not user.voice or not user.voice.channel:
            return {
                'success': False,
                'message': '\u274c You must be in a voice channel to create a fractal group.',
                'members': [],
                'channel': None
            }

        # Filter out bots -- only human participants count for group formation.
        members = [m for m in user.voice.channel.members if not m.bot]

        # Lower bound: at least 1 member (relaxed from 2 for dev/testing).
        if len(members) < 1:
            return {
                'success': False,
                'message': '\u274c You need at least 2 members in your voice channel to create a fractal group.',
                'members': [],
                'channel': user.voice.channel
            }

        # Upper bound: fractal groups are designed for at most 6 participants.
        if len(members) > 6:
            return {
                'success': False,
                'message': '\u274c Fractal groups are limited to 6 members maximum for optimal experience.',
                'members': [],
                'channel': user.voice.channel
            }

        return {
            'success': True,
            'message': f'\u2705 Found {len(members)} eligible members in voice channel.',
            'members': members,
            'channel': user.voice.channel
        }


async def setup(bot: commands.Bot):
    """discord.py extension entry point -- registers BaseCog with the bot."""
    await bot.add_cog(BaseCog(bot))
