"""Assign the Speaker role to all current non-bot members, oldest first, concurrent."""
import asyncio
import os
import logging
import sys
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("AssignSpeaker")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

CONCURRENCY = 1  # sequential, ~1/sec due to Discord rate limits

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)


def get_speaker_role_id(db: DatabaseHandler, guild_id: int | None) -> int | None:
    if guild_id is None:
        return None
    sc = getattr(db, 'server_config', None)
    if sc:
        role_id = sc.get_server_field(guild_id, 'speaker_role_id', cast=int)
        if role_id:
            return role_id
    env_value = os.getenv("SPEAKER_ROLE_ID")
    return int(env_value) if env_value else None


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    db = DatabaseHandler()
    speaker_role_id = get_speaker_role_id(db, TARGET_GUILD_ID)
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

    # Sort by join date, oldest first; filter out bots and those who already have it
    members = sorted(
        [m for m in guild.members if not m.bot],
        key=lambda m: m.joined_at or m.created_at,
    )
    need_role = [m for m in members if role not in m.roles]
    already = len(members) - len(need_role)
    total = len(need_role)
    logger.info(f"{already} already have role, {total} need it. Assigning with concurrency={CONCURRENCY}...")

    assigned = 0
    errors = 0
    sem = asyncio.Semaphore(CONCURRENCY)
    start = time.time()

    async def assign_one(member):
        nonlocal assigned, errors
        async with sem:
            try:
                await member.add_roles(role, reason="Speaker role — bulk assignment")
                assigned += 1
                if assigned % 200 == 0:
                    elapsed = time.time() - start
                    rate = assigned / elapsed
                    remaining = (total - assigned) / rate if rate > 0 else 0
                    logger.info(
                        f"  Progress: {assigned}/{total} "
                        f"({rate:.1f}/sec, ~{remaining/60:.0f} min remaining)"
                    )
            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.error(f"  Failed for {member.name} ({member.id}): {e}")

    # Fire all tasks concurrently, semaphore limits actual concurrency
    await asyncio.gather(*[assign_one(m) for m in need_role])

    elapsed = time.time() - start
    logger.info(
        f"Done! Assigned: {assigned}, Already had role: {already}, "
        f"Errors: {errors}, Time: {elapsed:.0f}s ({assigned/elapsed:.1f}/sec)"
    )
    await client.close()


client.run(TOKEN)
