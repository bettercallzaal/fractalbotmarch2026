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
import shutil
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from aiohttp import web
from discord.ext import commands, tasks
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
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")
# When DEBUG is true, the logger emits DEBUG-level messages for deeper tracing.
DEBUG = os.getenv('DEBUG', 'FALSE').upper() == 'TRUE'
HEALTH_PORT = int(os.getenv('HEALTH_PORT', '8080'))

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
from config.config import FRACTAL_BOT_CHANNEL_ID
_seen_interactions = OrderedDict()

@bot.tree.interaction_check
async def global_interaction_dedup(interaction: discord.Interaction) -> bool:
    """Gate every app-command interaction through the dedup cache.

    Registered via ``@bot.tree.interaction_check`` so it runs *before* any
    individual command callback.  Returns ``False`` (= abort) if the
    interaction ID has already been seen, ``True`` otherwise.

    This is the single bot-level dedup guard that catches all duplicates.
    """
    cmd_name = interaction.command.name if interaction.command else "unknown"
    logger.info(f"[DEDUP] interaction_check: id={interaction.id} command={cmd_name} guild={interaction.guild_id}")
    if interaction.id in _seen_interactions:
        logger.warning(f"[DEDUP] BLOCKED duplicate interaction {interaction.id}")
        return False
    _seen_interactions[interaction.id] = True
    bot.last_interaction_time = datetime.now(timezone.utc)
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
    bot.start_time = datetime.now(timezone.utc)

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
# Error Alerting -- post critical errors to #fractal-bot for admin visibility
# ---------------------------------------------------------------------------
# Dedup cache: maps "ErrorType: message" -> last-reported UTC timestamp.
# Errors with the same key within 5 minutes are suppressed to avoid spam.
_recent_errors: dict[str, datetime] = {}
_ERROR_DEDUP_WINDOW = timedelta(minutes=5)


async def _report_error_to_channel(
    error: BaseException,
    *,
    command_name: str = "unknown",
    user: str = "unknown",
):
    """Post a concise error embed to #fractal-bot and log the full trace.

    Deduplicates by error type + message: if the same combination was already
    reported within the last 5 minutes, the alert is skipped (but the full
    traceback is still logged to console every time).
    """
    # Always log the full traceback to console for debugging.
    logger.error(
        f"[ERROR ALERT] command=/{command_name} user={user} "
        f"error={type(error).__name__}: {error}"
    )
    logger.error(
        "".join(traceback.format_exception(type(error), error, error.__traceback__))
    )

    # --- Deduplication ---
    dedup_key = f"{type(error).__name__}: {str(error)[:200]}"
    now = datetime.now(timezone.utc)
    last_seen = _recent_errors.get(dedup_key)
    if last_seen and (now - last_seen) < _ERROR_DEDUP_WINDOW:
        logger.debug(f"[ERROR ALERT] Suppressed duplicate: {dedup_key}")
        return
    _recent_errors[dedup_key] = now

    # Evict expired entries to keep the cache from growing unbounded.
    expired = [k for k, ts in _recent_errors.items() if (now - ts) >= _ERROR_DEDUP_WINDOW]
    for k in expired:
        _recent_errors.pop(k, None)

    # --- Build and send the embed ---
    channel = bot.get_channel(FRACTAL_BOT_CHANNEL_ID)
    if channel is None:
        logger.warning(f"[ERROR ALERT] Cannot find channel {FRACTAL_BOT_CHANNEL_ID}")
        return

    embed = discord.Embed(
        title="Bot Error",
        color=discord.Color.red(),
        timestamp=now,
    )
    embed.add_field(name="Command", value=f"`/{command_name}`", inline=True)
    embed.add_field(name="User", value=user, inline=True)
    embed.add_field(name="Error", value=f"`{type(error).__name__}`", inline=True)
    # Truncate the message to stay within embed field limits (1024 chars).
    error_msg = str(error)[:1000] or "(no message)"
    embed.add_field(name="Message", value=error_msg, inline=False)
    embed.set_footer(text="Full traceback logged to console")

    try:
        await channel.send(embed=embed)
    except Exception as send_err:
        logger.error(f"[ERROR ALERT] Failed to send alert embed: {send_err}")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: discord.app_commands.AppCommandError,
):
    """Global handler for slash-command (app command) errors."""
    # Unwrap the original exception if wrapped by discord.py.
    original = getattr(error, "original", error)
    cmd_name = interaction.command.name if interaction.command else "unknown"
    user_str = str(interaction.user)

    # Send a polite reply to the user so the interaction doesn't hang.
    try:
        msg = "Something went wrong. The admins have been notified."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass  # interaction may have expired; nothing we can do

    await _report_error_to_channel(original, command_name=cmd_name, user=user_str)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Global handler for prefix-command errors (the ! commands)."""
    original = getattr(error, "original", error)
    cmd_name = ctx.command.name if ctx.command else "unknown"
    user_str = str(ctx.author)

    await _report_error_to_channel(original, command_name=cmd_name, user=user_str)


# ---------------------------------------------------------------------------
# Automated daily data backups
# ---------------------------------------------------------------------------
# Copies all files from data/ to backups/YYYY-MM-DD/ every 24 hours.
# Keeps the last 30 days of backups and auto-deletes older ones.
# The first backup runs shortly after bot startup.
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUPS_DIR = BASE_DIR / "backups"
BACKUP_RETENTION_DAYS = 30


def _run_backup():
    """Synchronous backup: copy data/ to backups/YYYY-MM-DD/ and prune old backups."""
    today = datetime.now().strftime("%Y-%m-%d")
    dest = BACKUPS_DIR / today

    # Copy data files (overwrite if already ran today)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DATA_DIR, dest)
    logger.info(f"[BACKUP] Created backup: {dest}")

    # Prune backups older than retention period
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    for entry in BACKUPS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            dir_date = datetime.strptime(entry.name, "%Y-%m-%d")
        except ValueError:
            continue  # skip non-date directories
        if dir_date < cutoff:
            shutil.rmtree(entry)
            logger.info(f"[BACKUP] Pruned old backup: {entry.name}")


@tasks.loop(hours=24)
async def daily_backup():
    """Background task that runs the data backup every 24 hours."""
    BACKUPS_DIR.mkdir(exist_ok=True)
    try:
        await asyncio.to_thread(_run_backup)
    except Exception as e:
        logger.error(f"[BACKUP] Backup failed: {e}")


@daily_backup.before_loop
async def _before_daily_backup():
    """Wait until the bot is ready before starting the backup loop."""
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------

async def health_handler(request):
    """Return JSON health status for monitoring/orchestration tools."""
    now = datetime.now(timezone.utc)
    uptime = (now - bot.start_time).total_seconds() if hasattr(bot, 'start_time') else 0
    last_interaction = (
        bot.last_interaction_time.isoformat() if hasattr(bot, 'last_interaction_time') else None
    )
    return web.json_response({
        "status": "ok",
        "bot_connected": bot.is_ready(),
        "guild_count": len(bot.guilds),
        "uptime_seconds": round(uptime, 2),
        "last_interaction": last_interaction,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    """Load extensions and start the bot under a managed async context.

    Using ``async with bot`` ensures that the bot's internal HTTP session and
    gateway connection are properly closed on shutdown.  A lightweight aiohttp
    health-check server is started alongside the bot for monitoring.
    """
    app = web.Application()
    app.router.add_get('/health', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()

    try:
        site = web.TCPSite(runner, '0.0.0.0', HEALTH_PORT)
        await site.start()
        logger.info(f"Health check server listening on port {HEALTH_PORT}")
    except OSError as e:
        logger.warning(f"Could not start health server on port {HEALTH_PORT}: {e}")

    try:
        async with bot:
            await load_extensions()
            daily_backup.start()
            await bot.start(TOKEN)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
