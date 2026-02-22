#!/usr/bin/env python3
"""
Generic moderation tool: fetch or move Discord messages for investigation.

Usage:
    # Fetch messages by channel/author/date (from Supabase)
    python scripts/moderate_messages.py fetch --channel 1369470423699292170 --authors 636706883859906562,301463647895683072 --after 2026-02-20 --before 2026-02-23
    python scripts/moderate_messages.py fetch --ids 147481356,147493374,147497629

    # Move messages to a moderation thread (via Discord API)
    python scripts/moderate_messages.py move --ids 147481356,147493374 --source-channel 1369470423699292170 --target-channel 1475121919484366962 --thread-title "Dispute — Feb 22" --dry-run
    python scripts/moderate_messages.py move --ids-file dispute_ids.txt --source-channel 1369470423699292170
"""
import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import aiohttp
import discord
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

logger = logging.getLogger("moderate_messages")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snowflake_to_iso(snowflake_id: int) -> str:
    """Convert a Discord snowflake ID to an ISO timestamp."""
    ts_ms = (snowflake_id >> 22) + 1420070400000
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _parse_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of IDs into a sorted list of ints."""
    return sorted(int(x.strip()) for x in raw.split(",") if x.strip())


def _read_ids_file(path: str) -> list[int]:
    """Read one message ID per line from a file."""
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(int(line))
    return sorted(ids)


# ---------------------------------------------------------------------------
# Shared author-name cache backed by Supabase discord_members
# ---------------------------------------------------------------------------


class AuthorCache:
    def __init__(self, supabase_client):
        self._sb = supabase_client
        self._cache: dict[int, str] = {}

    def get(self, author_id) -> str:
        if author_id is None:
            return "Unknown"
        aid = int(author_id)
        if aid in self._cache:
            return self._cache[aid]
        resp = (
            self._sb.table("discord_members")
            .select("member_id, username, global_name, server_nick")
            .eq("member_id", aid)
            .execute()
        )
        if resp.data:
            d = resp.data[0]
            name = d.get("username") or d.get("global_name") or str(aid)
            self._cache[aid] = name
        else:
            self._cache[aid] = str(aid)
        return self._cache[aid]

    def set(self, author_id: int, name: str):
        self._cache[int(author_id)] = name

    def preload(self, author_ids: list[int]):
        if not author_ids:
            return
        str_ids = [str(a) for a in author_ids]
        resp = (
            self._sb.table("discord_members")
            .select("member_id, username, global_name, server_nick")
            .in_("member_id", str_ids)
            .execute()
        )
        for d in resp.data:
            name = d.get("username") or d.get("global_name") or str(d["member_id"])
            self._cache[int(d["member_id"])] = name


# ===========================================================================
# FETCH subcommand — query Supabase and print formatted results
# ===========================================================================


def cmd_fetch(args):
    load_dotenv()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    authors = AuthorCache(sb)

    if args.ids:
        messages = _fetch_by_ids(sb, _parse_ids(args.ids))
    else:
        if not args.channel:
            logger.error("--channel is required when not using --ids")
            sys.exit(1)
        messages = _fetch_by_filters(
            sb,
            channel_id=args.channel,
            author_ids=[int(a) for a in args.authors.split(",")] if args.authors else None,
            after=args.after,
            before=args.before,
            limit=args.limit,
        )

    if not messages:
        print("No messages found.")
        return

    # Preload author names
    author_ids = list({int(m["author_id"]) for m in messages if m.get("author_id")})
    authors.preload(author_ids)

    print(f"Found {len(messages)} messages\n")
    print("=" * 100)

    for msg in messages:
        author_name = authors.get(msg.get("author_id"))
        print(f"Message ID:  {msg['message_id']}")
        print(f"Author:      {author_name} ({msg.get('author_id', '?')})")
        print(f"Created at:  {msg['created_at']}")
        if msg.get("edited_at"):
            print(f"Edited at:   {msg['edited_at']}")
        if msg.get("reference_id"):
            print(f"Reply to:    {msg['reference_id']}")
        if msg.get("attachments"):
            atts = msg["attachments"]
            if isinstance(atts, str):
                try:
                    atts = json.loads(atts)
                except json.JSONDecodeError:
                    atts = []
            if atts:
                print(f"Attachments: {len(atts)}")
        print(f"Content:")
        print(msg.get("content") or "(empty)")
        print("-" * 100)


def _fetch_by_ids(sb, ids: list[int]) -> list[dict]:
    str_ids = [str(i) for i in ids]
    resp = (
        sb.table("discord_messages")
        .select("message_id, author_id, content, created_at, edited_at, reference_id, attachments, embeds")
        .in_("message_id", str_ids)
        .order("created_at", desc=False)
        .execute()
    )
    return resp.data


def _fetch_by_filters(sb, channel_id: str, author_ids: list[int] | None, after: str | None, before: str | None, limit: int) -> list[dict]:
    query = (
        sb.table("discord_messages")
        .select("message_id, author_id, content, created_at, edited_at, reference_id, attachments, embeds")
        .eq("channel_id", channel_id)
    )
    if author_ids:
        query = query.in_("author_id", [str(a) for a in author_ids])
    if after:
        # Accept YYYY-MM-DD or full ISO
        if len(after) == 10:
            after = after + "T00:00:00+00:00"
        query = query.gte("created_at", after)
    if before:
        if len(before) == 10:
            before = before + "T00:00:00+00:00"
        query = query.lt("created_at", before)

    query = query.order("created_at", desc=False)

    # Paginate to respect limit
    all_messages: list[dict] = []
    batch_size = min(limit, 1000)
    offset = 0
    while len(all_messages) < limit:
        resp = query.range(offset, offset + batch_size - 1).execute()
        all_messages.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        offset += batch_size

    return all_messages[:limit]


# ===========================================================================
# MOVE subcommand — Discord bot that fetches, copies, and deletes messages
# ===========================================================================


class MessageMover(discord.Client):
    def __init__(
        self,
        message_ids: list[int],
        source_channel_id: int,
        target_channel_id: int,
        thread_title: str | None,
        note_text: str | None,
        no_delete: bool,
        no_note: bool,
        dry_run: bool,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)

        self.message_ids = message_ids
        self.source_channel_id = source_channel_id
        self.target_channel_id = target_channel_id
        self.thread_title = thread_title
        self.note_text = note_text
        self.no_delete = no_delete
        self.no_note = no_note
        self.dry_run = dry_run

        self._supabase = None
        self._authors: AuthorCache | None = None

    @property
    def supabase(self):
        if self._supabase is None:
            self._supabase = create_client(
                os.getenv("SUPABASE_URL"),
                os.getenv("SUPABASE_SERVICE_KEY"),
            )
        return self._supabase

    @property
    def authors(self) -> AuthorCache:
        if self._authors is None:
            self._authors = AuthorCache(self.supabase)
        return self._authors

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")
        prefix = "[DRY RUN] " if self.dry_run else ""

        try:
            messages = await self._gather_messages()
            if not messages:
                logger.error("No messages gathered, aborting.")
                return
            logger.info(f"{prefix}Gathered {len(messages)} messages to move")

            sent_ids = await self._send_to_target(messages, prefix)

            if not self.no_delete:
                await self._delete_originals(messages, sent_ids, prefix)

            if not self.no_note:
                await self._post_note(prefix)

            logger.info(f"{prefix}Done!")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
        finally:
            await self.close()

    async def _gather_messages(self) -> list[dict]:
        """Fetch messages by ID from Discord, falling back to Supabase for deleted ones."""
        messages = []

        source = self.get_channel(self.source_channel_id)
        if not source:
            source = await self.fetch_channel(self.source_channel_id)

        logger.info(f"Fetching {len(self.message_ids)} messages from #{source.name}...")

        for msg_id in self.message_ids:
            try:
                discord_msg = await source.fetch_message(msg_id)
                row = {
                    "message_id": discord_msg.id,
                    "author_id": discord_msg.author.id,
                    "content": discord_msg.content,
                    "created_at": discord_msg.created_at.isoformat(),
                    "reference_id": discord_msg.reference.message_id if discord_msg.reference else None,
                    "attachments": [
                        {"url": a.url, "filename": a.filename}
                        for a in discord_msg.attachments
                    ],
                    "embeds": [e.to_dict() for e in discord_msg.embeds],
                    "_source": "discord",
                }
                self.authors.set(discord_msg.author.id, discord_msg.author.name)
                messages.append(row)
                logger.info(f"  [{len(messages)}/{len(self.message_ids)}] {discord_msg.author.display_name}: {discord_msg.content[:60]}")
            except discord.NotFound:
                logger.warning(f"  Message {msg_id} not found on Discord — checking DB...")
                resp = (
                    self.supabase.table("discord_messages")
                    .select("message_id, author_id, content, created_at, reference_id, attachments, embeds")
                    .eq("message_id", msg_id)
                    .execute()
                )
                if resp.data:
                    row = resp.data[0]
                    row["_source"] = "db (deleted from Discord)"
                    messages.append(row)
                    author = self.authors.get(row.get("author_id"))
                    logger.info(f"  [{len(messages)}/{len(self.message_ids)}] {author}: {(row.get('content') or '')[:60]} [from DB]")
                else:
                    logger.warning(f"  Message {msg_id} not in DB either")
                    messages.append({
                        "message_id": msg_id,
                        "author_id": None,
                        "content": "[Message was deleted before it could be archived]",
                        "created_at": _snowflake_to_iso(msg_id),
                        "reference_id": None,
                        "attachments": [],
                        "embeds": [],
                        "_source": "deleted",
                    })
            except Exception as e:
                logger.error(f"  Error fetching message {msg_id}: {e}")

            await asyncio.sleep(0.3)

        return messages

    async def _send_to_target(self, messages: list[dict], prefix: str) -> set[int]:
        """Create a thread in the target channel and send each message."""
        sent_ids: set[int] = set()

        target = self.get_channel(self.target_channel_id)
        if not target:
            target = await self.fetch_channel(self.target_channel_id)

        logger.info(f"Target channel: #{target.name} (type: {type(target).__name__})")

        title = self.thread_title or f"Moderation — {datetime.now(timezone.utc).strftime('%b %d %Y')}"
        initial_text = f"**{len(messages)} messages moved for moderation review.**"

        thread = None
        if isinstance(target, discord.ForumChannel):
            if self.dry_run:
                logger.info(f"{prefix}Would create forum thread: '{title}'")
            else:
                thread_with_msg = await target.create_thread(
                    name=title,
                    content=initial_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                thread = thread_with_msg.thread
                logger.info(f"Created forum thread: {thread.name} ({thread.id})")
        elif isinstance(target, discord.TextChannel):
            if self.dry_run:
                logger.info(f"{prefix}Would create thread in #{target.name}: '{title}'")
            else:
                initial = await target.send(
                    initial_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                thread = await initial.create_thread(name=title)
                logger.info(f"Created thread: {thread.name} ({thread.id})")
        else:
            logger.error(f"Unexpected channel type: {type(target).__name__}")
            return sent_ids

        for i, msg in enumerate(messages, 1):
            msg_id = msg["message_id"]
            author = self.authors.get(msg.get("author_id"))

            try:
                dt = datetime.fromisoformat(msg["created_at"].replace("Z", "+00:00"))
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                ts_str = msg["created_at"]

            content = msg.get("content", "") or ""

            # Build formatted text with sanitized mentions
            header = f"**{author}** — {ts_str}"
            if content:
                safe_content = self._sanitize_mentions(content)
                quoted = "\n".join(f"> {line}" for line in safe_content.split("\n"))
                text = f"{header}\n{quoted}"
            else:
                text = header

            if len(text) > 2000:
                text = text[:1997] + "..."

            # Handle attachments
            files = []
            attachments_data = msg.get("attachments", [])
            if isinstance(attachments_data, str):
                try:
                    attachments_data = json.loads(attachments_data)
                except json.JSONDecodeError:
                    attachments_data = []

            if self.dry_run:
                att_info = f" + {len(attachments_data)} attachment(s)" if attachments_data else ""
                logger.info(f"{prefix}[{i}/{len(messages)}] Would send:{att_info}")
                for line in text.split("\n"):
                    logger.info(f"  | {line}")
                sent_ids.add(msg_id)
            else:
                try:
                    if attachments_data:
                        async with aiohttp.ClientSession() as session:
                            for att in attachments_data:
                                url = att.get("url", "")
                                filename = att.get("filename", "attachment")
                                try:
                                    async with session.get(url) as resp:
                                        if resp.status == 200:
                                            data = await resp.read()
                                            files.append(discord.File(io.BytesIO(data), filename=filename))
                                        else:
                                            text += f"\n*[Attachment: {filename} — download failed]*"
                                except Exception:
                                    text += f"\n*[Attachment: {filename} — download failed]*"

                    await thread.send(
                        content=text,
                        files=files if files else discord.utils.MISSING,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    sent_ids.add(msg_id)
                    logger.info(f"Sent [{i}/{len(messages)}]: {author} — {ts_str}")
                except Exception as e:
                    logger.error(f"FAILED to send [{i}/{len(messages)}] (msg {msg_id}): {e}")

                await asyncio.sleep(0.5)

        logger.info(f"{prefix}Successfully sent {len(sent_ids)}/{len(messages)} messages")
        return sent_ids

    def _sanitize_mentions(self, content: str) -> str:
        """Replace @mentions with plain-text usernames so the bot doesn't ping anyone."""
        def _resolve_mention(match):
            uid = int(match.group(2))
            return "@" + self.authors.get(uid)

        content = re.sub(r'<@(!?)(\d+)>', _resolve_mention, content)
        content = re.sub(r'<@&(\d+)>', r'@role-\1', content)
        content = content.replace('@everyone', '@\u200beveryone')
        content = content.replace('@here', '@\u200bhere')
        return content

    async def _delete_originals(self, messages: list[dict], sent_ids: set[int], prefix: str):
        """Delete original messages from the source channel (only those confirmed sent)."""
        source = self.get_channel(self.source_channel_id)
        if not source:
            source = await self.fetch_channel(self.source_channel_id)

        deleted = 0
        skipped = 0
        failed = 0
        for msg in messages:
            msg_id = msg["message_id"]
            if msg_id not in sent_ids:
                logger.warning(f"Skipping delete of {msg_id} — was NOT confirmed sent")
                skipped += 1
                continue

            if self.dry_run:
                logger.info(f"{prefix}Would delete message {msg_id}")
                deleted += 1
                continue

            try:
                discord_msg = await source.fetch_message(msg_id)
                await discord_msg.delete()
                deleted += 1
                logger.info(f"Deleted message {msg_id}")
                await asyncio.sleep(0.5)
            except discord.NotFound:
                logger.warning(f"Message {msg_id} already deleted")
            except discord.Forbidden:
                logger.error(f"No permission to delete message {msg_id}")
                failed += 1
            except Exception as e:
                logger.error(f"Error deleting message {msg_id}: {e}")
                failed += 1

        logger.info(f"{prefix}Deleted: {deleted}, Skipped: {skipped}, Failed: {failed}")

    async def _post_note(self, prefix: str):
        """Post a note in the source channel about the moved messages."""
        source = self.get_channel(self.source_channel_id)
        if not source:
            source = await self.fetch_channel(self.source_channel_id)

        note = self.note_text or "A moderation-related conversation has been moved to a moderation thread for review."

        if self.dry_run:
            logger.info(f'{prefix}Would post note in #{source.name}: "{note}"')
        else:
            await source.send(note)
            logger.info(f"Posted note in #{source.name}")


def cmd_move(args):
    load_dotenv()

    # Resolve message IDs
    if args.ids:
        message_ids = _parse_ids(args.ids)
    elif args.ids_file:
        message_ids = _read_ids_file(args.ids_file)
    else:
        logger.error("Either --ids or --ids-file is required")
        sys.exit(1)

    if not message_ids:
        logger.error("No message IDs provided")
        sys.exit(1)

    if not args.source_channel:
        logger.error("--source-channel is required")
        sys.exit(1)

    target = args.target_channel or os.getenv("MODERATION_CHANNEL_ID")
    if not target:
        logger.error("--target-channel is required (or set MODERATION_CHANNEL_ID env var)")
        sys.exit(1)

    logger.info(f"Message IDs: {len(message_ids)}")
    logger.info(f"Source channel: {args.source_channel}")
    logger.info(f"Target channel: {target}")
    if args.dry_run:
        logger.info("DRY RUN — no changes will be made")

    client = MessageMover(
        message_ids=message_ids,
        source_channel_id=int(args.source_channel),
        target_channel_id=int(target),
        thread_title=args.thread_title,
        note_text=args.note,
        no_delete=args.no_delete,
        no_note=args.no_note,
        dry_run=args.dry_run,
    )
    client.run(os.getenv("DISCORD_BOT_TOKEN"))


# ===========================================================================
# CLI
# ===========================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Generic moderation tool: fetch or move Discord messages",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- fetch ---
    fetch_p = subparsers.add_parser("fetch", help="Query messages from Supabase and print formatted output")
    fetch_p.add_argument("--channel", help="Channel ID to search in")
    fetch_p.add_argument("--authors", help="Comma-separated author IDs to filter by")
    fetch_p.add_argument("--after", help="Start date (YYYY-MM-DD or ISO format)")
    fetch_p.add_argument("--before", help="End date (YYYY-MM-DD or ISO format)")
    fetch_p.add_argument("--ids", help="Comma-separated message IDs (direct lookup, ignores other filters)")
    fetch_p.add_argument("--limit", type=int, default=200, help="Max messages to return (default: 200)")

    # --- move ---
    move_p = subparsers.add_parser("move", help="Move messages to a moderation thread via Discord API")
    move_p.add_argument("--ids", help="Comma-separated message IDs")
    move_p.add_argument("--ids-file", help="Path to file with one message ID per line")
    move_p.add_argument("--source-channel", help="Channel to fetch messages from (required)")
    move_p.add_argument("--target-channel", help="Channel to create moderation thread in (default: MODERATION_CHANNEL_ID env)")
    move_p.add_argument("--thread-title", help="Title for the new thread (default: auto-generated)")
    move_p.add_argument("--note", help="Custom note to post in source channel after moving")
    move_p.add_argument("--no-delete", action="store_true", help="Don't delete originals (just copy)")
    move_p.add_argument("--no-note", action="store_true", help="Don't post a note in the source channel")
    move_p.add_argument("--dry-run", action="store_true", help="Preview only, no changes")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "move":
        cmd_move(args)


if __name__ == "__main__":
    main()
