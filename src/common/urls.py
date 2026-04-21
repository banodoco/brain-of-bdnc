import logging
from typing import Any, Dict, Iterable

_log = logging.getLogger(__name__)


def message_jump_url(guild_id, channel_id, message_id, thread_id=None) -> str:
    """Build a Discord jump URL. For messages inside a thread or forum post,
    pass thread_id so the URL routes to the specific thread instead of the
    parent channel's main page."""
    route_id = thread_id if thread_id else channel_id
    return f"https://discord.com/channels/{guild_id}/{route_id}/{message_id}"


def resolve_thread_ids(db_handler: Any, message_ids: Iterable) -> Dict[int, int]:
    """Batch-look-up thread_id for each message_id so callers can build
    correct jump URLs for forum posts. Returns {message_id: thread_id} only
    for messages whose thread_id is non-null — a missing key means "no thread,
    use channel_id in the URL". Swallows DB errors and returns an empty map."""
    if db_handler is None:
        return {}
    ids = []
    for m in message_ids:
        try:
            if m:
                ids.append(int(m))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    try:
        rows = db_handler.get_messages_by_ids(list(set(ids)))
    except Exception as e:
        _log.warning("Could not resolve thread_ids: %s", e)
        return {}
    out: Dict[int, int] = {}
    for row in rows or []:
        tid = row.get('thread_id')
        mid = row.get('message_id')
        if tid and mid:
            out[int(mid)] = int(tid)
    return out
