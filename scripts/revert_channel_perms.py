"""Remove the Speaker role channel overwrites and restore @everyone send permissions."""
import asyncio
import os
import logging

import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("RevertPerms")
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

    logger.info(f"Reverting channel permissions in {guild.name}...")

    reverted = 0
    skipped = 0

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            skipped += 1
            continue

        if channel.id in EXEMPT_CHANNELS:
            logger.info(f"  Skipping exempt channel #{channel.name} ({channel.id})")
            skipped += 1
            continue

        try:
            # Reset @everyone send perms back to None (inherit)
            everyone_overwrite = channel.overwrites_for(guild.default_role)
            everyone_overwrite.send_messages = None
            everyone_overwrite.send_messages_in_threads = None
            everyone_overwrite.create_public_threads = None
            everyone_overwrite.create_private_threads = None

            await channel.set_permissions(
                guild.default_role, overwrite=everyone_overwrite,
                reason="Revert Speaker role — restore @everyone send perms",
            )

            # Remove Speaker role overwrite entirely
            if role in channel.overwrites:
                await channel.set_permissions(
                    role, overwrite=None,
                    reason="Revert Speaker role — remove Speaker overwrite",
                )

            reverted += 1
            if reverted % 20 == 0:
                logger.info(f"  Progress: {reverted} channels reverted")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! Reverted: {reverted}, Skipped: {skipped}")
    await client.close()


client.run(TOKEN)
