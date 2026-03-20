"""Back-approve all pending intros: grant Speaker role and mark approved in DB."""
import asyncio
import os
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv
from supabase import create_client

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("ApprovePendingIntros")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

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

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    pi_q = sb.table('pending_intros').select('*').eq('status', 'pending').eq('guild_id', TARGET_GUILD_ID)
    pending = pi_q.execute().data
    logger.info(f"Found {len(pending)} pending intros to approve")

    approved = 0
    skipped = 0
    errors = 0

    for intro in pending:
        member_id = intro['member_id']
        message_id = intro['message_id']
        member = guild.get_member(member_id)

        if not member:
            logger.warning(f"  Member {member_id} not in guild, skipping (may have left)")
            skipped += 1
            # Still mark as approved in DB so it doesn't sit pending forever
            sb.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('guild_id', TARGET_GUILD_ID).execute()
            continue

        if role in member.roles:
            logger.info(f"  {member.name} already has Speaker, marking approved in DB")
            sb.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('guild_id', TARGET_GUILD_ID).execute()
            approved += 1
            continue

        try:
            await member.add_roles(role, reason="Back-approved pending intro")
            sb.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('guild_id', TARGET_GUILD_ID).execute()
            approved += 1
            logger.info(f"  Approved {member.name} ({member_id})")
            await asyncio.sleep(1)  # respect rate limits
        except Exception as e:
            errors += 1
            logger.error(f"  Failed to approve {member.name} ({member_id}): {e}")

    logger.info(f"Done! Approved: {approved}, Skipped (left guild): {skipped}, Errors: {errors}")
    await client.close()


client.run(TOKEN)
