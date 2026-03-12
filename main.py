import discord
import logging
import asyncio
import os
from discord.ext import commands
from dotenv import load_dotenv

# Load opus for voice support
if os.path.exists('/opt/homebrew/lib/libopus.dylib'):
    discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')  # macOS (Homebrew)
elif not discord.opus.is_loaded():
    discord.opus.load_opus('libopus.so.0')  # Linux

# Load configuration
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DEBUG = os.getenv('DEBUG', 'FALSE').upper() == 'TRUE'

# Configure logging
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format='[\033[92m%(asctime)s\033[0m] \033[94m%(levelname)s\033[0m: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('bot')

# Configure intents (all required for full functionality)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

# Initialize bot with command prefix
bot = commands.Bot(command_prefix='!', intents=intents)

# --- Bot-wide duplicate interaction guard ---
# discord.py can dispatch the same interaction multiple times when commands
# exist in both global and guild trees. This catches duplicates at the
# lowest level, before any command handler runs.
from collections import OrderedDict
_seen_interactions = OrderedDict()

@bot.tree.interaction_check
async def global_interaction_dedup(interaction: discord.Interaction) -> bool:
    """Return False to block duplicate dispatches of the same interaction."""
    cmd_name = interaction.command.name if interaction.command else "unknown"
    logger.info(f"[DEDUP] interaction_check: id={interaction.id} command={cmd_name} guild={interaction.guild_id}")
    if interaction.id in _seen_interactions:
        logger.warning(f"[DEDUP] BLOCKED duplicate interaction {interaction.id}")
        return False
    _seen_interactions[interaction.id] = True
    while len(_seen_interactions) > 200:
        _seen_interactions.popitem(last=False)
    return True

# Load cogs
async def load_extensions():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            logger.info(f"Loaded extension: {filename[:-3]}")

    # Load fractal cog
    await bot.load_extension('cogs.fractal')
    logger.info("Loaded fractal extension")

_ready_fired = False

@bot.event
async def on_ready():
    global _ready_fired
    if _ready_fired:
        logger.info("on_ready fired again (reconnect) — skipping command sync")
        return
    _ready_fired = True

    logger.info(f"=== Bot Starting Up ===")
    logger.info(f"Bot: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")

    # Generate invite link
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

    # Debug: List all commands before syncing
    logger.info(f"Total commands in tree: {len(bot.tree.get_commands())}")
    for cmd in bot.tree.get_commands():
        logger.info(f"Command: /{cmd.name} - {cmd.description}")

    # IMPORTANT: Clear stale guild registrations from previous syncs
    # Without this, commands exist per-guild AND globally = multiple dispatches
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=discord.Object(id=guild.id))
        await bot.tree.sync(guild=discord.Object(id=guild.id))
        logger.info(f"Cleared stale guild commands from {guild.name}")

    # Single global sync — one registration per command
    synced = await bot.tree.sync()
    logger.info(f"Synced {len(synced)} commands globally")

# Run bot
async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
