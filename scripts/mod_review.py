#!/usr/bin/env python3
"""
Moderation review tool: generate a readable, chronological view of a user's
activity with full conversational context (what they replied to, what others
replied to them).

Usage:
    python scripts/mod_review.py --user bghira --since 2026-02-20
    python scripts/mod_review.py --user bghira --since 2026-02-20 --channel 1369470423699292170
"""
import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

from scripts.moderate_messages import AuthorCache


def resolve_user(sb, username: str) -> dict | None:
    """Look up a user by username/nick (case-insensitive fuzzy match)."""
    pattern = f"%{username}%"
    resp = (
        sb.table("discord_members")
        .select("member_id, username, global_name, server_nick")
        .or_(
            f"username.ilike.{pattern},"
            f"global_name.ilike.{pattern},"
            f"server_nick.ilike.{pattern}"
        )
        .execute()
    )
    if not resp.data:
        return None
    if len(resp.data) == 1:
        return resp.data[0]
    # Prefer exact username match
    for d in resp.data:
        if (d.get("username") or "").lower() == username.lower():
            return d
    # Show matches and let user pick
    print(f"Multiple matches for '{username}':")
    for i, d in enumerate(resp.data, 1):
        name = d.get("username") or d.get("global_name") or str(d["member_id"])
        nick = d.get("server_nick") or ""
        suffix = f" (nick: {nick})" if nick else ""
        print(f"  {i}. {name}{suffix}  [{d['member_id']}]")
    choice = input("Pick a number (or q to quit): ").strip()
    if choice.lower() == "q":
        sys.exit(0)
    return resp.data[int(choice) - 1]


def fetch_user_messages(sb, author_id: int, since: str, channel_id: str | None) -> list[dict]:
    """Fetch all messages from the target user in the date range."""
    if len(since) == 10:
        since = since + "T00:00:00+00:00"

    query = (
        sb.table("discord_messages")
        .select("message_id, author_id, channel_id, content, created_at, reference_id")
        .eq("author_id", author_id)
        .gte("created_at", since)
        .order("created_at", desc=False)
    )
    if channel_id:
        query = query.eq("channel_id", channel_id)

    all_messages = []
    batch_size = 1000
    offset = 0
    while True:
        resp = query.range(offset, offset + batch_size - 1).execute()
        all_messages.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        offset += batch_size

    return all_messages


def fetch_messages_by_ids(sb, ids: list[int]) -> dict[int, dict]:
    """Fetch messages by their IDs. Returns a dict keyed by message_id."""
    if not ids:
        return {}
    result = {}
    # Batch in groups of 200 to avoid overly large IN clauses
    for i in range(0, len(ids), 200):
        batch = [str(mid) for mid in ids[i:i + 200]]
        resp = (
            sb.table("discord_messages")
            .select("message_id, author_id, channel_id, content, created_at, reference_id")
            .in_("message_id", batch)
            .execute()
        )
        for row in resp.data:
            result[int(row["message_id"])] = row
    return result


def fetch_replies_to(sb, target_ids: list[int]) -> dict[int, list[dict]]:
    """Fetch all messages that are replies to any of the target message IDs.
    Returns a dict: target_message_id -> [reply messages]."""
    if not target_ids:
        return {}
    replies: dict[int, list[dict]] = {}
    for i in range(0, len(target_ids), 200):
        batch = [str(mid) for mid in target_ids[i:i + 200]]
        resp = (
            sb.table("discord_messages")
            .select("message_id, author_id, channel_id, content, created_at, reference_id")
            .in_("reference_id", batch)
            .order("created_at", desc=False)
            .execute()
        )
        for row in resp.data:
            ref = int(row["reference_id"])
            replies.setdefault(ref, []).append(row)
    return replies


def fetch_channel_names(sb, channel_ids: list[int]) -> dict[int, str]:
    """Bulk load channel names from discord_channels."""
    if not channel_ids:
        return {}
    str_ids = [str(cid) for cid in channel_ids]
    resp = (
        sb.table("discord_channels")
        .select("channel_id, channel_name")
        .in_("channel_id", str_ids)
        .execute()
    )
    return {int(row["channel_id"]): row["channel_name"] for row in resp.data}


def truncate(text: str, max_len: int = 200) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if not text:
        return "(empty)"
    text = text.replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def format_ts(iso_str: str) -> str:
    """Format an ISO timestamp to a readable short form."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def main():
    parser = argparse.ArgumentParser(description="Review a user's messages with conversational context")
    parser.add_argument("--user", required=True, help="Username or nick to search for")
    parser.add_argument("--since", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--channel", help="Optional channel ID to filter by")
    args = parser.parse_args()

    load_dotenv()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    authors = AuthorCache(sb)

    # 1. Resolve user
    user = resolve_user(sb, args.user)
    if not user:
        print(f"No user found matching '{args.user}'")
        sys.exit(1)

    author_id = int(user["member_id"])
    display = user.get("username") or user.get("global_name") or str(author_id)
    print(f"User: {display} ({author_id})")
    authors.set(author_id, display)

    # 2. Fetch target user's messages
    messages = fetch_user_messages(sb, author_id, args.since, args.channel)
    if not messages:
        print("No messages found in the given range.")
        sys.exit(0)
    print(f"Found {len(messages)} messages since {args.since}\n")

    # 3. Collect IDs for context fetching
    target_msg_ids = [int(m["message_id"]) for m in messages]
    parent_ids = [int(m["reference_id"]) for m in messages if m.get("reference_id")]

    # 4. Fetch context: parents and replies
    parent_msgs = fetch_messages_by_ids(sb, parent_ids)
    reply_map = fetch_replies_to(sb, target_msg_ids)

    # 5. Collect all author IDs and preload names
    all_author_ids = set()
    for m in messages:
        if m.get("author_id"):
            all_author_ids.add(int(m["author_id"]))
    for m in parent_msgs.values():
        if m.get("author_id"):
            all_author_ids.add(int(m["author_id"]))
    for reply_list in reply_map.values():
        for m in reply_list:
            if m.get("author_id"):
                all_author_ids.add(int(m["author_id"]))
    authors.preload(list(all_author_ids))

    # 6. Fetch channel names
    channel_ids = list({int(m["channel_id"]) for m in messages})
    channel_names = fetch_channel_names(sb, channel_ids)

    # 7. Group by channel
    by_channel: dict[int, list[dict]] = {}
    for m in messages:
        cid = int(m["channel_id"])
        by_channel.setdefault(cid, []).append(m)

    # 8. Print formatted output
    for cid, chan_messages in by_channel.items():
        chan_name = channel_names.get(cid, str(cid))
        print(f"━━━ #{chan_name} ━━━")
        print()

        for msg in chan_messages:
            msg_id = int(msg["message_id"])
            ts = format_ts(msg["created_at"])
            content = truncate(msg.get("content") or "")

            # Show parent message if this is a reply
            ref_id = int(msg["reference_id"]) if msg.get("reference_id") else None
            if ref_id and ref_id in parent_msgs:
                parent = parent_msgs[ref_id]
                parent_author = authors.get(parent.get("author_id"))
                parent_content = truncate(parent.get("content") or "", 120)
                print(f"  {display} replying to {parent_author}: \"{parent_content}\"")

            # Show the target user's message (marked with arrow)
            if ref_id:
                print(f"  \u2192 {display} [{ts}]: {content}")
            else:
                print(f"  {display} [{ts}]: {content}")

            # Show replies from others
            if msg_id in reply_map:
                for reply in reply_map[msg_id]:
                    reply_author_id = int(reply["author_id"]) if reply.get("author_id") else None
                    # Skip the target user's own replies to themselves
                    if reply_author_id == author_id:
                        continue
                    reply_author = authors.get(reply_author_id)
                    reply_ts = format_ts(reply["created_at"])
                    reply_content = truncate(reply.get("content") or "", 120)
                    print(f"    {reply_author} [{reply_ts}]: {reply_content}")

            print()

        print()

    # Summary
    total_replies_received = sum(
        len([r for r in replies if int(r.get("author_id", 0)) != author_id])
        for replies in reply_map.values()
    )
    print(f"--- Summary: {len(messages)} messages across {len(by_channel)} channel(s), {total_replies_received} replies from others ---")


if __name__ == "__main__":
    main()
