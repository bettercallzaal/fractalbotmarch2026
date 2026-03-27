"""
BaseCog -- Shared foundation for all FractalBot cogs.

This module provides common helper methods on ``BaseCog`` -- permission checks,
voice-state validation, and a pre-configured logger -- so that individual cogs
can inherit from ``BaseCog`` instead of reimplementing boilerplate.

Interaction deduplication is handled globally in ``main.py`` via the
``@bot.tree.interaction_check`` hook, so individual cogs do not need their
own dedup logic.
"""

import discord
import logging
from discord.ext import commands
from config.config import SUPREME_ADMIN_ROLE_ID


class BaseCog(commands.Cog):
    """Shared base class for all FractalBot cogs.

    Subclasses automatically get:
    * ``self.bot``   -- reference to the ``commands.Bot`` instance.
    * ``self.logger`` -- a ``logging.Logger`` under the ``'bot'`` namespace.
    * ``is_supreme_admin()``  -- role-based admin check.
    * ``check_voice_state()`` -- validates a user is in a voice channel and
      the channel has an acceptable number of human participants.
    """

    def __init__(self, bot: commands.Bot):
        """Store bot reference and create a child logger for this cog."""
        self.bot = bot
        self.logger = logging.getLogger('bot')

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
