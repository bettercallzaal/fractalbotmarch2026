"""Minimal test bot — just /timer and /ping to isolate duplicate message bug."""
import discord
import logging
import os
from collections import OrderedDict
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

logging.basicConfig(
    level=logging.INFO,
    format='[\033[92m%(asctime)s\033[0m] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('test')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# --- Dedup tracking ---
_seen = OrderedDict()

@bot.tree.interaction_check
async def dedup_check(interaction: discord.Interaction) -> bool:
    cmd = interaction.command.name if interaction.command else "?"
    logger.info(f"[CHECK] id={interaction.id} cmd=/{cmd}")
    if interaction.id in _seen:
        logger.warning(f"[CHECK] BLOCKED duplicate id={interaction.id}")
        return False
    _seen[interaction.id] = True
    while len(_seen) > 100:
        _seen.popitem(last=False)
    return True

# --- Simple ping command ---
@bot.tree.command(name="ping", description="Test - should reply once")
async def ping(interaction: discord.Interaction):
    logger.info(f"[PING] handler called id={interaction.id}")
    await interaction.response.send_message("Pong!", ephemeral=True)

# --- Timer command ---
@bot.tree.command(name="timer_test", description="Test timer - should reply once")
async def timer_test(interaction: discord.Interaction):
    logger.info(f"[TIMER] handler called id={interaction.id}")

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "\u274c You must be in a voice channel.", ephemeral=True
        )
        return

    members = [m for m in interaction.user.voice.channel.members if not m.bot]
    if len(members) < 2:
        await interaction.response.send_message(
            f"\u274c Need at least 2 members. Found {len(members)}.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"\u2705 Timer would start for {len(members)} members!", ephemeral=True
    )

# --- Startup ---
_ready_fired = False

@bot.event
async def on_ready():
    global _ready_fired
    if _ready_fired:
        return
    _ready_fired = True

    logger.info(f"Bot ready: {bot.user}")

    # Clear ALL guild commands, sync global only
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=discord.Object(id=guild.id))
        await bot.tree.sync(guild=discord.Object(id=guild.id))
        logger.info(f"Cleared guild commands: {guild.name}")

    synced = await bot.tree.sync()
    logger.info(f"Synced {len(synced)} commands globally")

bot.run(TOKEN)
