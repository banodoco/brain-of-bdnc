from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.features.sharing.subfeatures.notify_user import (
    PostShareNotificationView,
    send_post_share_notification,
)


pytestmark = pytest.mark.anyio


class FakeDB:
    def __init__(self, publications):
        self.publications = {row["publication_id"]: dict(row) for row in publications}
        self.server_config = None

    def get_social_publication_by_id(self, publication_id, guild_id=None):
        row = self.publications.get(publication_id)
        if row and guild_id is not None and guild_id != 456:
            return None
        return dict(row) if row else None

    def update_member_sharing_permission(self, *_args, **_kwargs):
        return True


class FakeService:
    def __init__(self):
        self.calls = []

    async def delete_publication(self, publication_id):
        self.calls.append(publication_id)
        return True


class FakeMessage:
    def __init__(self):
        self.id = 10
        self.jump_url = "https://discord.com/channels/456/222/10"
        self.guild = SimpleNamespace(id=456)
        self.content = "shared"
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeDMChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, view=None):
        message = FakeMessage()
        self.sent.append({"content": content, "view": view, "message": message})
        return message


class FakeUser:
    def __init__(self, user_id=123):
        self.id = user_id
        self.display_name = "Poster"
        self._dm = FakeDMChannel()

    async def create_dm(self):
        return self._dm


class FakeResponse:
    def __init__(self):
        self.deferred = []

    async def defer(self, ephemeral=False):
        self.deferred.append(ephemeral)

    async def send_message(self, content, ephemeral=False):
        self.last_message = (content, ephemeral)


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, user_id=123):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.guild_id = 456


async def test_send_post_share_notification_carries_publication_id_in_dm_view():
    db_handler = FakeDB([])
    bot = SimpleNamespace(dev_mode=False, rate_limiter=None)
    user = FakeUser()
    message = FakeMessage()

    await send_post_share_notification(
        bot=bot,
        user=user,
        discord_message=message,
        publication_id="pub-1",
        tweet_id="tweet-1",
        tweet_url="https://x.com/example/status/1",
        db_handler=db_handler,
    )

    sent = user._dm.sent[0]
    assert isinstance(sent["view"], PostShareNotificationView)
    assert sent["view"].publication_id == "pub-1"


async def test_delete_button_targets_exact_publication_id_with_multiple_publications():
    now = datetime.now(timezone.utc).isoformat()
    db_handler = FakeDB(
        [
            {
                "publication_id": "pub-1",
                "user_id": 123,
                "action": "post",
                "delete_supported": True,
                "status": "succeeded",
                "completed_at": now,
            },
            {
                "publication_id": "pub-2",
                "user_id": 123,
                "action": "post",
                "delete_supported": True,
                "status": "succeeded",
                "completed_at": now,
            },
        ]
    )
    service = FakeService()
    view = PostShareNotificationView(
        db_handler=db_handler,
        discord_message_id=99,
        discord_user_id=123,
        publication_id="pub-2",
        tweet_id="tweet-2",
        tweet_url="https://x.com/example/status/2",
        bot=SimpleNamespace(social_publish_service=service),
        guild_id=456,
    )
    view.message = FakeMessage()

    button = next(item for item in view.children if getattr(item, "custom_id", None) == "delete_post")
    interaction = FakeInteraction(user_id=123)
    await button.callback(interaction)

    assert service.calls == ["pub-2"]
    assert interaction.followup.messages[-1][0] == "Your post has been deleted from X."
    assert button.disabled is True
    assert button.label == "Post Deleted"


async def test_delete_button_rejects_retweet_with_clear_message():
    now = datetime.now(timezone.utc).isoformat()
    db_handler = FakeDB(
        [
            {
                "publication_id": "pub-retweet",
                "user_id": 123,
                "action": "retweet",
                "delete_supported": False,
                "status": "succeeded",
                "completed_at": now,
            }
        ]
    )
    service = FakeService()
    view = PostShareNotificationView(
        db_handler=db_handler,
        discord_message_id=11,
        discord_user_id=123,
        publication_id="pub-retweet",
        tweet_id="tweet-retweet",
        tweet_url="https://x.com/example/status/retweet",
        bot=SimpleNamespace(social_publish_service=service),
        guild_id=456,
    )

    button = next(item for item in view.children if getattr(item, "custom_id", None) == "delete_post")
    interaction = FakeInteraction(user_id=123)
    await button.callback(interaction)

    assert service.calls == []
    assert interaction.followup.messages[-1][0] == "Retweets can't be deleted through this button."
