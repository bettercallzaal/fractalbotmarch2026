import discord
import logging
from collections import OrderedDict
from discord.ext import commands
from config.config import SUPREME_ADMIN_ROLE_ID


class _InteractionDedup:
    """Track seen interaction IDs to prevent duplicate command dispatch.

    discord.py can dispatch the same interaction to a command handler multiple
    times when commands exist in both global and guild trees. This provides
    a synchronous (no-await) check that catches duplicates before any response.
    """
    def __init__(self, maxsize=200):
        self._seen = OrderedDict()
        self._maxsize = maxsize

    def is_duplicate(self, interaction_id: int) -> bool:
        if interaction_id in self._seen:
            return True
        self._seen[interaction_id] = True
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


# Single shared instance across all cogs
_dedup = _InteractionDedup()


class BaseCog(commands.Cog):
    """Base cog with utility methods for all cogs"""
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('bot')

    @staticmethod
    def is_duplicate_interaction(interaction: discord.Interaction) -> bool:
        """Check if this interaction was already handled. Call at the TOP of every command."""
        return _dedup.is_duplicate(interaction.id)

    def is_supreme_admin(self, member: discord.Member) -> bool:
        """Check if a member has the Supreme Admin role"""
        return any(role.id == SUPREME_ADMIN_ROLE_ID for role in member.roles)

    async def check_voice_state(self, user):
        """Check if user is in a voice channel and return eligible members"""
        # Validate user is in voice channel
        if not user.voice or not user.voice.channel:
            return {
                'success': False,
                'message': '\u274c You must be in a voice channel to create a fractal group.',
                'members': [],
                'channel': None
            }

        # Get non-bot members
        members = [m for m in user.voice.channel.members if not m.bot]

        # Validate member count (1-6 members) — minimum 1 for testing
        if len(members) < 1:
            return {
                'success': False,
                'message': '\u274c You need at least 2 members in your voice channel to create a fractal group.',
                'members': [],
                'channel': user.voice.channel
            }

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


async def setup(bot):
    await bot.add_cog(BaseCog(bot))
