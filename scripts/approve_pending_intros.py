"""Back-approve all pending intros: grant Speaker role and mark approved in DB."""
import asyncio
import os
import logging
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger("ApprovePendingIntros")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
SPEAKER_ROLE_ID = int(os.getenv("SPEAKER_ROLE_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

intents = discord.Intents.default()
intents.members = True
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

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    pending = sb.table('pending_intros').select('*').eq('status', 'pending').execute().data
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
            }).eq('message_id', message_id).execute()
            continue

        if role in member.roles:
            logger.info(f"  {member.name} already has Speaker, marking approved in DB")
            sb.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).execute()
            approved += 1
            continue

        try:
            await member.add_roles(role, reason="Back-approved pending intro")
            sb.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).execute()
            approved += 1
            logger.info(f"  Approved {member.name} ({member_id})")
            await asyncio.sleep(1)  # respect rate limits
        except Exception as e:
            errors += 1
            logger.error(f"  Failed to approve {member.name} ({member_id}): {e}")

    logger.info(f"Done! Approved: {approved}, Skipped (left guild): {skipped}, Errors: {errors}")
    await client.close()


client.run(TOKEN)
