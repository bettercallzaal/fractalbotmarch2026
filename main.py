import discord
import logging
import asyncio
import os
from discord.ext import commands
from dotenv import load_dotenv

# Load opus for voice support
discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')

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

# Load cogs
async def load_extensions():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            logger.info(f"Loaded extension: {filename[:-3]}")

    # Load fractal cog
    await bot.load_extension('cogs.fractal')
    logger.info("Loaded fractal extension")

@bot.event
async def on_ready():
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
        ),
        scopes=["bot", "applications.commands"]
    )
    logger.info(f"Invite link: {invite_link}")

    # Debug: List all commands before syncing
    logger.info(f"Total commands in tree: {len(bot.tree.get_commands())}")
    for cmd in bot.tree.get_commands():
        logger.info(f"Command: /{cmd.name} - {cmd.description}")

    # Clear any existing guild commands first
    for guild in bot.guilds:
        logger.info(f"Clearing existing commands for guild: {guild.name}")
        bot.tree.clear_commands(guild=discord.Object(id=guild.id))

    # Copy global commands to guild for faster sync
    for guild in bot.guilds:
        logger.info(f"Copying commands to guild: {guild.name}")
        bot.tree.copy_global_to(guild=discord.Object(id=guild.id))
        synced = await bot.tree.sync(guild=discord.Object(id=guild.id))
        logger.info(f"Commands synced to guild {guild.id}: {len(synced)} commands")

# Run bot
async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
