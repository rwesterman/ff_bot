import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from utils.chat_history import HistoryDatabase
from utils.chat_rag import (
    ANSWER_SYSTEM_PROMPT,
    EMBEDDING_BATCH_SIZE,
    ArchivedMessage,
    HistoryRagService,
    build_conversation_chunks,
    format_discord_answer,
    split_discord_message,
)


class FakeEmbeddings:
    def __init__(self, fail_on_call=None):
        self.inputs = []
        self.batches = []
        self.fail_on_call = fail_on_call

    async def create(self, *, input, model, dimensions, encoding_format):
        self.batches.append(list(input))
        if len(self.batches) == self.fail_on_call:
            raise RuntimeError("simulated embedding failure")
        self.inputs.extend(input)
        data = []
        for index, text in enumerate(input):
            vector = [0.0] * dimensions
            vector[0] = 1.0 if "tiebreak" in text.lower() else 0.1
            vector[1] = 1.0 if "waiver" in text.lower() else 0.1
            vector[2] = 1.0
            data.append(SimpleNamespace(index=index, embedding=vector))
        return SimpleNamespace(data=data)


class FakeEmbeddingClient:
    def __init__(self, fail_on_call=None):
        self.embeddings = FakeEmbeddings(fail_on_call=fail_on_call)


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="Head-to-head record is the documented tiebreaker [1].")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeAnswerClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


def archived_message(message_id, content, *, created_at=None, channel_id=10, reference_message_id=None):
    return ArchivedMessage(
        id=message_id,
        guild_id=1,
        channel_id=channel_id,
        channel_name="league-rules",
        author_name="Alice",
        content=content,
        created_at=created_at or datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=message_id),
        reference_message_id=reference_message_id,
    )


def seed_database(path):
    with HistoryDatabase(path) as database:
        database.connection.execute(
            "INSERT INTO guilds (id, name, synced_at) VALUES (1, 'League', '2024-01-01T00:00:00+00:00')"
        )
        database.connection.executemany(
            "INSERT INTO channels (id, guild_id, name, kind) VALUES (?, 1, ?, 'text')",
            [(10, "league-rules"), (20, "off-topic")],
        )
        database.connection.executemany(
            """
            INSERT INTO messages (
                id, guild_id, channel_id, author_id, author_name, author_is_bot, content,
                created_at, message_type, reference_message_id
            ) VALUES (?, 1, ?, 100, 'Alice', 0, ?, ?, 'default', ?)
            """,
            [
                (1, 10, "The playoff tiebreaker is head-to-head record.", "2023-09-01T12:00:00+00:00", None),
                (2, 10, "We confirmed that rule for this season.", "2023-09-01T12:05:00+00:00", 1),
                (3, 20, "Private unrelated conversation.", "2023-09-01T12:00:00+00:00", None),
            ],
        )


def seed_sparse_database(path, message_count):
    with HistoryDatabase(path) as database:
        database.connection.execute(
            "INSERT INTO guilds (id, name, synced_at) VALUES (1, 'League', '2024-01-01T00:00:00+00:00')"
        )
        database.connection.execute(
            "INSERT INTO channels (id, guild_id, name, kind) VALUES (10, 1, 'league-rules', 'text')"
        )
        database.connection.executemany(
            """
            INSERT INTO messages (
                id, guild_id, channel_id, author_id, author_name, author_is_bot, content,
                created_at, message_type, reference_message_id
            ) VALUES (?, 1, 10, 100, 'Alice', 0, ?, ?, 'default', NULL)
            """,
            [
                (
                    message_id,
                    f"Rule discussion {message_id}",
                    (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=message_id * 2)).isoformat(),
                )
                for message_id in range(1, message_count + 1)
            ],
        )


def make_service(path, embedding_client=None):
    return HistoryRagService(
        database_path=path,
        channel_ids=(10,),
        embedding_client=embedding_client or FakeEmbeddingClient(),
        answer_client=FakeAnswerClient(),
        embedding_dimensions=4,
    )


def test_chunking_overlaps_size_splits_but_not_time_gaps():
    messages = [archived_message(message_id, f"Message {message_id}") for message_id in range(1, 43)]
    chunks = build_conversation_chunks(messages)

    assert len(chunks) == 2
    assert chunks[0].message_ids == tuple(range(1, 41))
    assert chunks[1].message_ids == (39, 40, 41, 42)

    late = archived_message(43, "Tomorrow", created_at=messages[-1].created_at + timedelta(hours=2))
    gap_chunks = build_conversation_chunks([*messages, late])
    assert gap_chunks[-1].message_ids == (43,)


def test_chunking_adds_parent_context_when_reply_is_outside_chunk():
    parent = archived_message(1, "The commissioner confirmed head-to-head.")
    reply = archived_message(
        2,
        "That is correct.",
        created_at=parent.created_at + timedelta(hours=2),
        reference_message_id=1,
    )

    chunks = build_conversation_chunks([parent, reply])

    assert len(chunks) == 2
    assert "replying to Alice: The commissioner confirmed head-to-head." in chunks[1].content


def test_index_honors_allowlist_and_reuses_unchanged_embeddings(tmp_path):
    path = tmp_path / "history.db"
    seed_database(path)
    service = make_service(path)

    first = asyncio.run(service.index())
    calls_after_first_index = len(service.embedding_client.embeddings.inputs)
    second = asyncio.run(service.index())

    assert first.messages == 2
    assert first.chunks == 1
    assert first.embeddings_created == 1
    assert second.embeddings_created == 0
    assert second.embeddings_reused == 1
    assert len(service.embedding_client.embeddings.inputs) == calls_after_first_index
    with HistoryDatabase(path) as database:
        indexed_channels = database.connection.execute("SELECT DISTINCT channel_id FROM conversation_chunks").fetchall()
    assert indexed_channels == [(10,)]


def test_index_commits_embedding_batches_and_resumes_after_failure(tmp_path):
    path = tmp_path / "history.db"
    chunk_count = EMBEDDING_BATCH_SIZE + 2
    seed_sparse_database(path, chunk_count)
    failing_client = FakeEmbeddingClient(fail_on_call=2)

    with pytest.raises(RuntimeError, match="simulated embedding failure"):
        asyncio.run(make_service(path, failing_client).index())

    with HistoryDatabase(path) as database:
        stored_embeddings = database.connection.execute(
            "SELECT COUNT(*) FROM conversation_chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    assert stored_embeddings == EMBEDDING_BATCH_SIZE

    retry_client = FakeEmbeddingClient()
    result = asyncio.run(make_service(path, retry_client).index())

    assert result.chunks == chunk_count
    assert result.embeddings_created == 2
    assert result.embeddings_reused == EMBEDDING_BATCH_SIZE
    assert [len(batch) for batch in retry_client.embeddings.batches] == [2]


def test_hybrid_retrieval_and_answer_include_grounding_and_sources(tmp_path):
    path = tmp_path / "history.db"
    seed_database(path)
    service = make_service(path)
    asyncio.run(service.index())

    answer = asyncio.run(service.answer("What is the playoff tiebreaker?"))
    formatted = format_discord_answer(answer)

    assert answer.text.startswith("Head-to-head")
    assert answer.evidence[0].channel_name == "league-rules"
    assert "https://discord.com/channels/1/10/1" in formatted
    assert "official league rules" in formatted
    call = service.answer_client.chat.completions.calls[0]
    assert call["messages"][0]["content"] == ANSWER_SYSTEM_PROMPT
    assert "playoff tiebreaker" in call["messages"][1]["content"]


def test_split_discord_message_respects_limit():
    parts = split_discord_message("one two three four", limit=7)

    assert parts == ["one two", "three", "four"]
    assert all(len(part) <= 7 for part in parts)
