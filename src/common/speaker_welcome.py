"""Shared helpers for building new-speaker welcome blurbs."""
import logging

import discord

from src.common.llm import get_llm_response
from src.common.soul import BOT_VOICE

logger = logging.getLogger('DiscordBot')

NEW_SPEAKER_BLURB_PROMPT = """\
You are writing a brief welcome blurb for a new member who just got approved to speak \
in Banodoco, an open-source AI art community on Discord.

{bot_voice}

Write a single natural-sounding blurb (1-2 sentences) that:
- Introduces them based on what they said in their intro — focus on who they are and \
what they're into, NOT any problems or issues they mentioned.
- Weaves in 3-4 channel suggestions naturally (using <#channel_id> format), like \
"you might enjoy <#123> and <#456>" or "sounds like <#123> would be right up your alley."
- If their intro is too vague to say anything specific about them, just write something \
warm and characterful — "another mysterious soul joins the ranks" or similar. Don't \
force specifics you don't have.

Respond in EXACTLY this format (no extra text):
BLURB: <your blurb here>

Use only channel IDs from the provided list. Pick channels relevant to their interests — \
prioritise tool-specific or model-specific channels over generic ones."""


def get_recommendable_channels(guild: discord.Guild) -> list[tuple[int, str]]:
    """Return (id, name) pairs for text channels the bot can recommend to new members."""
    skip_segments = {'rules', 'mod', 'admin', 'logs', 'bot', 'bots', 'gate', 'intro',
                     'introductions', 'welcome', 'announcements', 'staff', 'test'}
    channels = []
    for ch in guild.text_channels:
        segments = set(ch.name.lower().replace('-', ' ').replace('_', ' ').split())
        if segments & skip_segments:
            continue
        if ch.permissions_for(guild.me).view_channel:
            channels.append((ch.id, ch.name))
    return channels[:60]


async def build_speaker_blurb(
    guild: discord.Guild,
    intro: dict,
    member: discord.Member,
    recommendable: list[tuple[int, str]],
) -> str | None:
    """Fetch the member's intro message and generate a welcome blurb with channel suggestions."""
    fallback = f"- {member.mention} — welcome aboard."

    intro_channel = guild.get_channel(intro['channel_id'])
    if not intro_channel:
        return fallback
    try:
        intro_msg = await intro_channel.fetch_message(intro['message_id'])
    except (discord.NotFound, discord.HTTPException):
        return fallback

    intro_text = intro_msg.content or "(no text — media only)"
    channel_list = "\n".join(f"- {cid}: #{name}" for cid, name in recommendable)

    try:
        response = await get_llm_response(
            client_name="claude",
            model="claude-opus-4-6",
            system_prompt=NEW_SPEAKER_BLURB_PROMPT.format(bot_voice=BOT_VOICE),
            messages=[{
                "role": "user",
                "content": (
                    f"Introduction from {member.display_name}:\n\n"
                    f"{intro_text}\n\n"
                    f"Available channels:\n{channel_list}"
                ),
            }],
            max_tokens=200,
        )
        response = response.strip()
    except Exception as e:
        logger.error(f"speaker_welcome: failed to generate blurb for {member}: {e}")
        return fallback

    blurb_text = ""
    for line in response.split('\n'):
        line = line.strip()
        if line.upper().startswith('BLURB:'):
            blurb_text = line.split(':', 1)[1].strip()
            break

    if blurb_text:
        return f"- {member.mention} — {blurb_text}"
    return fallback
