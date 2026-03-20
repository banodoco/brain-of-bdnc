"""Set channel permission overwrites for the Speaker role system.

Reads speaker_mode from the database for each channel.  Falls back to the
SPEAKER_EXEMPT_CHANNELS env var for backward compatibility.
"""
import asyncio
import os
import sys
import logging
from pathlib import Path

import discord
from dotenv import load_dotenv

# Add project root so we can import src.common
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.speaker_perms import apply_perms_to_channel
from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("ChannelPerms")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None
EXEMPT_CHANNELS = {int(x.strip()) for x in os.getenv("SPEAKER_EXEMPT_CHANNELS", "").split(",") if x.strip()}

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    db = DatabaseHandler()
    speaker_role_id = None
    sc = getattr(db, 'server_config', None)
    if sc:
        speaker_role_id = sc.get_server_field(TARGET_GUILD_ID, 'speaker_role_id', cast=int)
    if speaker_role_id is None:
        env_value = os.getenv("SPEAKER_ROLE_ID")
        speaker_role_id = int(env_value) if env_value else None

    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        return

    role = guild.get_role(speaker_role_id) if speaker_role_id else None
    if not role:
        logger.error(f"Speaker role {speaker_role_id} not found")
        await client.close()
        return

    # Load channel modes from DB
    modes = {}
    try:
        modes = db.get_all_channel_speaker_modes()
        logger.info(f"Loaded {len(modes)} channel modes from DB")
    except Exception as e:
        logger.warning(f"Could not load modes from DB, using env var only: {e}")

    logger.info(f"Setting permissions on channels in {guild.name}...")

    updated = 0
    skipped = 0
    errors = 0

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            skipped += 1
            continue

        mode = modes.get(channel.id) or 'normal'

        # Env var fallback
        if channel.id in EXEMPT_CHANNELS:
            mode = 'exempt'

        try:
            changed, api_calls = await apply_perms_to_channel(channel, role, mode)
            if changed:
                updated += 1
                logger.info(f"  Applied mode={mode} to #{channel.name} ({channel.id}), api_calls={api_calls}")
            else:
                skipped += 1
            if updated % 20 == 0 and updated > 0:
                logger.info(f"  Progress: {updated} channels updated")
            await asyncio.sleep(0.5)
        except Exception as e:
            errors += 1
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! Updated: {updated}, Skipped: {skipped}, Errors: {errors}")
    await client.close()


client.run(TOKEN)
