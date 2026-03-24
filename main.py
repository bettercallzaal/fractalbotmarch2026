"""
FractalBot -- Entry point and bot initialisation.

This module bootstraps the Discord bot: loads environment config, configures
logging, sets up required intents, registers a global interaction dedup guard,
discovers/loads all cog extensions, syncs slash commands, and starts the event
loop.

Key design decisions
--------------------
* **Opus loading**: Attempted at import time so voice-related cogs can assume
  it is already available.  macOS (Homebrew) and Linux paths are tried.
* **Interaction deduplication**: discord.py may dispatch the same interaction
  twice when commands are registered both globally and per-guild.  A thin
  OrderedDict-based LRU cache (capped at 200 entries) blocks the second
  dispatch before any handler runs.
* **Command sync strategy**: On first ready, stale per-guild registrations are
  cleared and a single global sync is performed so each command exists exactly
  once.
"""

import discord
import logging
import asyncio
import os
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Opus codec -- required for sending/receiving voice audio.
# Try the Homebrew path first (macOS dev machines), then fall back to the
# standard Linux shared-library name.
# ---------------------------------------------------------------------------
if os.path.exists('/opt/homebrew/lib/libopus.dylib'):
    discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')  # macOS (Homebrew)
elif not discord.opus.is_loaded():
    discord.opus.load_opus('libopus.so.0')  # Linux

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
# When DEBUG is true, the logger emits DEBUG-level messages for deeper tracing.
DEBUG = os.getenv('DEBUG', 'FALSE').upper() == 'TRUE'

# ---------------------------------------------------------------------------
# Logging -- ANSI colour codes make log output easier to scan in a terminal.
# ---------------------------------------------------------------------------
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format='[\033[92m%(asctime)s\033[0m] \033[94m%(levelname)s\033[0m: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('bot')

# ---------------------------------------------------------------------------
# Discord intents -- we need the privileged message_content and members
# intents for slash-command argument parsing and voice-channel member lists.
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

# Initialize bot with the legacy '!' prefix (slash commands are the primary
# interface, but the prefix is still required by the commands.Bot constructor).
bot = commands.Bot(command_prefix='!', intents=intents)

# ---------------------------------------------------------------------------
# Bot-wide duplicate interaction guard
# ---------------------------------------------------------------------------
# discord.py can dispatch the same interaction multiple times when commands
# exist in both global and guild trees.  This OrderedDict-based LRU cache
# catches duplicates at the lowest level, before any command handler runs.
# The cache is capped at 200 entries to bound memory usage.
# ---------------------------------------------------------------------------
from collections import OrderedDict
_seen_interactions = OrderedDict()

@bot.tree.interaction_check
async def global_interaction_dedup(interaction: discord.Interaction) -> bool:
    """Gate every app-command interaction through the dedup cache.

    Registered via ``@bot.tree.interaction_check`` so it runs *before* any
    individual command callback.  Returns ``False`` (= abort) if the
    interaction ID has already been seen, ``True`` otherwise.

    Note: This is the bot-level guard.  Individual cogs also have access to
    ``BaseCog.is_duplicate_interaction()`` for an additional per-cog check.
    """
    cmd_name = interaction.command.name if interaction.command else "unknown"
    logger.info(f"[DEDUP] interaction_check: id={interaction.id} command={cmd_name} guild={interaction.guild_id}")
    if interaction.id in _seen_interactions:
        logger.warning(f"[DEDUP] BLOCKED duplicate interaction {interaction.id}")
        return False
    _seen_interactions[interaction.id] = True
    # Evict oldest entries to keep the cache bounded at 200 items.
    while len(_seen_interactions) > 200:
        _seen_interactions.popitem(last=False)
    return True


async def load_extensions():
    """Discover and load all cog modules from the ``cogs/`` directory.

    Every ``.py`` file in ``./cogs/`` is loaded as a discord.py extension.
    The ``cogs.fractal`` extension is loaded separately afterwards because
    it lives in a sub-package and is not picked up by the directory scan.
    """
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            logger.info(f"Loaded extension: {filename[:-3]}")

    # fractal cog lives in a sub-package, so load it explicitly.
    await bot.load_extension('cogs.fractal')
    logger.info("Loaded fractal extension")


# Guard against on_ready firing multiple times.  Discord triggers on_ready
# on every reconnect, but we only want to sync commands once to avoid
# hitting the Discord API rate limit for command registration.
_ready_fired = False


@bot.event
async def on_ready():
    """Handle the bot becoming ready (connected and cached).

    On the *first* invocation this function:
    1. Logs identity and invite-link information.
    2. Clears any stale per-guild command registrations left over from
       previous runs -- without this step commands could be registered both
       globally and per-guild, causing duplicate dispatches.
    3. Performs a single global command sync so every slash command is
       registered exactly once.

    Subsequent invocations (caused by reconnects) are skipped entirely.
    """
    global _ready_fired
    if _ready_fired:
        # Reconnect -- commands are already synced, nothing to do.
        logger.info("on_ready fired again (reconnect) — skipping command sync")
        return
    _ready_fired = True

    logger.info(f"=== Bot Starting Up ===")
    logger.info(f"Bot: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")

    # Build and log an OAuth2 invite URL with the minimum permissions the bot
    # needs.  Useful for quickly adding the bot to new servers during dev.
    invite_link = discord.utils.oauth_url(
        bot.user.id,
        permissions=discord.Permissions(
            send_messages=True,
            embed_links=True,
            attach_files=True,
            read_messages=True,
            manage_messages=True,
            manage_threads=True,
            create_public_threads=True,
            create_private_threads=True,
            read_message_history=True,
            add_reactions=True,
            move_members=True,
            connect=True,
        ),
        scopes=["bot", "applications.commands"]
    )
    logger.info(f"Invite link: {invite_link}")

    # Log all registered commands for debugging before the sync.
    logger.info(f"Total commands in tree: {len(bot.tree.get_commands())}")
    for cmd in bot.tree.get_commands():
        logger.info(f"Command: /{cmd.name} - {cmd.description}")

    # IMPORTANT: Clear stale guild-level command registrations left over from
    # earlier syncs.  If these are not removed, Discord sees both a guild copy
    # and a global copy of each command and may dispatch the interaction twice.
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=discord.Object(id=guild.id))
        await bot.tree.sync(guild=discord.Object(id=guild.id))
        logger.info(f"Cleared stale guild commands from {guild.name}")

    # Single global sync -- ensures one canonical registration per command.
    synced = await bot.tree.sync()
    logger.info(f"Synced {len(synced)} commands globally")

    # Auto-match: scan all guild members and lock name-matched wallets to
    # their Discord IDs so they become permanent.
    if hasattr(bot, 'wallet_registry'):
        registry = bot.wallet_registry
        locked_count = 0
        for guild in bot.guilds:
            for member in guild.members:
                if member.bot:
                    continue
                # Skip if already linked by Discord ID
                if registry.get_by_discord_id(member.id):
                    continue
                # Try name match (display name, username, global name)
                wallet = registry.get_by_name(member.display_name) or \
                         registry.get_by_name(member.name) or \
                         (registry.get_by_name(member.global_name) if member.global_name else None)
                if wallet:
                    registry.register(member.id, wallet)
                    locked_count += 1
                    logger.info(f"[AUTO-LOCK] {member.display_name} ({member.id}) -> {wallet[:10]}...")
        logger.info(f"Auto-locked {locked_count} wallet(s) from name matching")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    """Load extensions and start the bot under a managed async context.

    Using ``async with bot`` ensures that the bot's internal HTTP session and
    gateway connection are properly closed on shutdown.
    """
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
