import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from utils.chat_history import HistoryDatabase, resolve_message_channels, sync_channel_history


class FakeChannel:
    id = 200
    name = "general"
    type = "text"
    parent_id = None
    guild = SimpleNamespace(id=100, name="Test Guild")

    def __init__(self, messages):
        self.messages = messages
        self.history_calls = []

    async def history(self, **kwargs):
        self.history_calls.append(kwargs)
        after = kwargs.get("after")
        for message in self.messages:
            if after is None or message.id > after.id:
                yield message


def discord_message(message_id, content, attachment=None):
    attachments = []
    if attachment:
        attachments.append(
            SimpleNamespace(
                id=message_id + 1_000,
                filename="image.png",
                url=attachment,
                content_type="image/png",
                size=123,
            )
        )
    return SimpleNamespace(
        id=message_id,
        guild=FakeChannel.guild,
        channel=SimpleNamespace(id=FakeChannel.id),
        author=SimpleNamespace(id=300, display_name="Alice", bot=False),
        content=content,
        created_at=datetime(2024, 1, message_id, tzinfo=UTC),
        edited_at=None,
        type="default",
        reference=None,
        attachments=attachments,
    )


def test_sync_channel_history_persists_messages_and_attachments(tmp_path):
    channel = FakeChannel(
        [
            discord_message(1, "First"),
            discord_message(2, "Second", "https://example.com/image.png"),
        ]
    )

    with HistoryDatabase(tmp_path / "history.db") as database:
        result = asyncio.run(sync_channel_history(database, channel))

        assert result.messages_seen == 2
        assert database.counts() == {"guilds": 1, "channels": 1, "messages": 2, "attachments": 1}
        assert database.integrity_check() == "ok"
        assert database.latest_message_id(channel.id) == 2
        stored_content = database.connection.execute("SELECT content FROM messages ORDER BY id").fetchall()
        assert stored_content == [("First",), ("Second",)]


def test_incremental_sync_requests_only_messages_after_cached_id(tmp_path):
    channel = FakeChannel([discord_message(1, "First"), discord_message(2, "Second")])

    with HistoryDatabase(tmp_path / "history.db") as database:
        asyncio.run(sync_channel_history(database, channel))
        no_changes = asyncio.run(sync_channel_history(database, channel))

        channel.messages.append(discord_message(3, "Third"))
        one_change = asyncio.run(sync_channel_history(database, channel))

        assert no_changes.messages_seen == 0
        assert one_change.messages_seen == 1
        assert channel.history_calls[1]["after"].id == 2
        assert channel.history_calls[2]["after"].id == 2
        assert database.latest_message_id(channel.id) == 3
        assert database.counts()["messages"] == 3


def test_full_sync_refetches_messages_idempotently(tmp_path):
    channel = FakeChannel([discord_message(1, "Original")])

    with HistoryDatabase(tmp_path / "history.db") as database:
        asyncio.run(sync_channel_history(database, channel))
        channel.messages[0].content = "Edited"

        result = asyncio.run(sync_channel_history(database, channel, full_sync=True))

        assert result.messages_seen == 1
        assert "after" not in channel.history_calls[-1]
        assert database.counts()["messages"] == 1
        assert database.connection.execute("SELECT content FROM messages").fetchone()[0] == "Edited"


def test_resolve_message_channels_uses_explicit_allowlist():
    allowed = FakeChannel([])

    class FakeClient:
        def get_channel(self, channel_id):
            return allowed if channel_id == allowed.id else None

        async def fetch_channel(self, channel_id):
            raise RuntimeError("not accessible")

    channels, unavailable = asyncio.run(resolve_message_channels(FakeClient(), [allowed.id, 999]))

    assert channels == [allowed]
    assert unavailable == [999]
