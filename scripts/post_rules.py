"""
Delete all existing messages in the rules channel and post the updated rules.

Usage:
    python scripts/post_rules.py          # dry run (shows what would happen)
    python scripts/post_rules.py --send   # actually delete and post
"""

import asyncio
import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
RULES_CHANNEL_ID = 1138515622582562947

# Each entry is one message to post (in order)
RULES_MESSAGES = [
    (
        "```Comprehensive Community Rules```"
    ),
    (
        "```1. Communication Guidelines for a Respectful Community:\n"
        "\n"
        "    Respectful Dialogue: Engage respectfully with all members. No personal attacks or harassment.\n"
        "    No Political or Religious Discourse: Keep discussions focused on AI art and technology, avoiding political or religious debates.\n"
        "    Encourage Inclusivity: Embrace diversity and inclusivity in all communications.\n"
        "    Constructive Feedback: Feedback is welcome and encouraged, but keep the tone constructive and supportive.\n"
        "    Privacy and Confidentiality: Do not share personal information without consent.```"
    ),
    (
        "```2. Advancing Open-Source Together:\n"
        "\n"
        "    The purpose of this space is to bring together people who believe in open-source technology and want to work on it, promote it, and create art with it.\n"
        "    Contribute Back: Engage in giving back to the ecosystem through sharing code, insights, or resources.\n"
        "    Reciprocity Culture: Utilization of open-source tech should come with contributions back to the ecosystem.```"
    ),
    (
        "```3. NSFW Content Guidelines:\n"
        "\n"
        "    NSFW content is generally not allowed, but may be posted in specifically designated NSFW channels.\n"
        "    Strict Age Limit: Only for individuals 18 and older. No content involving minors or anyone who looks like they could be a minor.\n"
        "    Content Restrictions: Avoid content featuring celebrities, politicians, or anyone without explicit consent.\n"
        "    Dignity and Consent: Respect the dignity of all individuals and adhere to consent principles.\n"
        "    Appropriate Tagging: Tag NSFW content clearly for informed engagement decisions.```"
    ),
    (
        "```4. Encourage Sharing and Credit:\n"
        "\n"
        "    Anything posted here may be shared elsewhere, but it must include specific attribution to the original creator, including a direct link to their work or profile.\n"
        "    Respect Copyrights: Understand and respect copyright laws. Ensure you have the right to share or modify works before doing so.```"
    ),
    (
        "```5. Universal Respect:\n"
        "\n"
        "    Value All Contributions: Every member of this community, whether they are developing models, experimenting with technology, creating tools, or producing art, plays a vital role. Recognize and respect the diversity and value of everyone's contributions.\n"
        "    Foster a Supportive Environment: Encourage, support, and uplift one another. Constructive criticism is welcome, but it should always be aimed at helping others improve and feel supported.\n"
        "    Celebrate Achievements: Take the time to celebrate the milestones and achievements within our community. Recognition of hard work and success fosters a positive and motivating environment for all.```"
    ),
    (
        "```6. Moderation and Subjective Judgment:\n"
        "\n"
        "    Our primary goal is to maintain a space that people genuinely enjoy being part of. While the rules above cover clear-cut situations, not everything that affects the quality of a community can be captured by strict rules alone.\n"
        "    Moderators may exercise subjective judgment to preserve the overall atmosphere and experience of the community. When we do, we will always explain our reasoning and aim to be as sparing and fair as possible.\n"
        "    At the end of the day, fostering a welcoming, enjoyable space sometimes requires discretion beyond what any rulebook can define.```"
    ),
]


async def main(send: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(RULES_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(RULES_CHANNEL_ID)

            print(f"Found channel: #{channel.name}")

            # Delete existing messages
            deleted = 0
            async for message in channel.history(limit=200):
                if send:
                    await message.delete()
                    await asyncio.sleep(0.5)  # rate limit safety
                deleted += 1
                print(f"  {'Deleted' if send else 'Would delete'}: {message.id} ({message.content[:50]}...)")

            print(f"\n{'Deleted' if send else 'Would delete'} {deleted} messages.\n")

            # Post new rules
            for i, msg in enumerate(RULES_MESSAGES):
                if send:
                    sent = await channel.send(msg)
                    print(f"  Posted message {i+1}/{len(RULES_MESSAGES)} (id: {sent.id})")
                    await asyncio.sleep(0.5)
                else:
                    print(f"  Would post message {i+1}/{len(RULES_MESSAGES)}:")
                    print(f"    {msg[:80]}...")

            print(f"\n{'Posted' if send else 'Would post'} {len(RULES_MESSAGES)} messages.")
            print("\nDone!" if send else "\nDry run complete. Use --send to execute.")

        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post updated rules to the rules channel")
    parser.add_argument("--send", action="store_true", help="Actually delete and post (default is dry run)")
    args = parser.parse_args()

    asyncio.run(main(send=args.send))
