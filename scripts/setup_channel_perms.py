"""Set channel permission overwrites for the Speaker role system."""
import asyncio
import os
import logging

import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ChannelPerms")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
SPEAKER_ROLE_ID = int(os.getenv("SPEAKER_ROLE_ID"))
EXEMPT_CHANNELS = {int(x.strip()) for x in os.getenv("SPEAKER_EXEMPT_CHANNELS", "").split(",") if x.strip()}

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    guild = client.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Guild {GUILD_ID} not found")
        await client.close()
        return

    role = guild.get_role(SPEAKER_ROLE_ID)
    if not role:
        logger.error(f"Speaker role {SPEAKER_ROLE_ID} not found")
        await client.close()
        return

    logger.info(f"Setting permissions on channels in {guild.name}...")

    updated = 0
    skipped = 0
    errors = 0

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            skipped += 1
            continue

        if channel.id in EXEMPT_CHANNELS:
            logger.info(f"  Skipping exempt channel #{channel.name} ({channel.id})")
            skipped += 1
            continue

        try:
            everyone_overwrite = channel.overwrites_for(guild.default_role)
            everyone_overwrite.send_messages = False
            everyone_overwrite.send_messages_in_threads = False
            everyone_overwrite.create_public_threads = False
            everyone_overwrite.create_private_threads = False

            speaker_overwrite = channel.overwrites_for(role)
            speaker_overwrite.send_messages = True
            speaker_overwrite.send_messages_in_threads = True
            speaker_overwrite.create_public_threads = True
            speaker_overwrite.create_private_threads = True

            await channel.set_permissions(
                guild.default_role, overwrite=everyone_overwrite,
                reason="Speaker role — deny send for @everyone",
            )
            await channel.set_permissions(
                role, overwrite=speaker_overwrite,
                reason="Speaker role — allow send for Speaker",
            )
            updated += 1
            if updated % 20 == 0:
                logger.info(f"  Progress: {updated} channels updated")
            await asyncio.sleep(0.5)
        except Exception as e:
            errors += 1
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! Updated: {updated}, Skipped: {skipped}, Errors: {errors}")
    await client.close()


client.run(TOKEN)
