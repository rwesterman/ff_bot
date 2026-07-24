import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import utils.chat_rag as chat_rag
from utils.chat_history import HistoryDatabase
from utils.chat_rag import (
    ANSWER_SYSTEM_PROMPT,
    EMBEDDING_BATCH_SIZE,
    ArchivedMessage,
    Evidence,
    HistoryRagService,
    RagAnswer,
    build_conversation_chunks,
    format_discord_answer,
    parse_answer_max_tokens,
    parse_boolean_setting,
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
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.response is not None:
            return self.response
        message = SimpleNamespace(content="Head-to-head record is the documented tiebreaker [1].")
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class FakeAnswerClient:
    def __init__(self, response=None):
        self.chat = SimpleNamespace(completions=FakeCompletions(response=response))


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


def seed_neighborhood_database(path):
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
                (1, "Earlier playoff discussion.", "2024-01-01T12:00:00+00:00"),
                (2, "The playoff tiebreaker is head-to-head record.", "2024-01-02T12:00:00+00:00"),
                (3, "Later confirmation of the final rule.", "2024-01-03T12:00:00+00:00"),
            ],
        )


def make_service(path, embedding_client=None, answer_client=None, **service_options):
    return HistoryRagService(
        database_path=path,
        channel_ids=(10,),
        embedding_client=embedding_client or FakeEmbeddingClient(),
        answer_client=answer_client or FakeAnswerClient(),
        embedding_dimensions=4,
        **service_options,
    )


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_parse_boolean_setting_accepts_enabled_values(value):
    assert parse_boolean_setting("SETTING", value, default=False) is True


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_parse_boolean_setting_accepts_disabled_values(value):
    assert parse_boolean_setting("SETTING", value, default=True) is False


def test_answer_environment_settings_are_validated():
    assert parse_boolean_setting("SETTING", None, default=False) is False
    assert parse_answer_max_tokens(None) == 4_096
    assert parse_answer_max_tokens("8192") == 8_192
    with pytest.raises(ValueError, match="DEEPSEEK_MAX_TOKENS must be between"):
        parse_answer_max_tokens("0")
    with pytest.raises(ValueError, match="SETTING must be one of"):
        parse_boolean_setting("SETTING", "sometimes", default=False)


def test_service_reads_answer_settings_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_THINKING_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "8192")
    monkeypatch.setattr(chat_rag, "AsyncOpenAI", lambda **kwargs: SimpleNamespace(configuration=kwargs))

    service = HistoryRagService.from_environment(tmp_path / "history.db")

    assert service.answer_thinking_enabled is True
    assert service.answer_max_tokens == 8_192


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
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["max_tokens"] == 4_096
    assert call["temperature"] == 0.1


def test_discord_answer_links_the_sources_cited_by_the_model():
    evidence = tuple(
        Evidence(
            chunk_id=number,
            guild_id=1,
            channel_id=10,
            channel_name="general-chat",
            first_message_id=number,
            started_at=f"2025-01-{number:02d}T12:00:00+00:00",
            ended_at=f"2025-01-{number:02d}T12:05:00+00:00",
            content=f"Excerpt {number}",
            score=1.0,
        )
        for number in range(1, 14)
    )
    answer = RagAnswer(
        "The later discussion [Source 13] conflicts with the playoff rule [Source 10].",
        evidence,
    )

    formatted = format_discord_answer(answer)

    assert "[Source 13: #general-chat — 2025-01-13]" in formatted
    assert "https://discord.com/channels/1/10/13" in formatted
    assert "[Source 10: #general-chat — 2025-01-10]" in formatted
    assert "https://discord.com/channels/1/10/10" in formatted
    assert "[Source 1:" not in formatted


def test_retrieval_expands_adjacent_chunks_and_logs_rerank_dominance(tmp_path, caplog):
    path = tmp_path / "history.db"
    seed_neighborhood_database(path)
    service = make_service(path)
    asyncio.run(service.index())

    with caplog.at_level("INFO", logger="utils.chat_rag"):
        evidence = asyncio.run(service.retrieve("What is the playoff tiebreaker?", limit=1))

    assert len(evidence) == 3
    assert "playoff tiebreaker is head-to-head" in evidence[0].content
    assert "Earlier playoff discussion" in evidence[1].content
    assert "Later confirmation" in evidence[2].content
    assert "selected_seeds=1" in caplog.text
    assert "hybrid=1" in caplog.text
    assert "dominance=balanced" in caplog.text
    assert "expanded_neighbors=2" in caplog.text
    assert "evidence_chunks=3" in caplog.text


def test_answer_enables_thinking_and_uses_configured_token_limit(tmp_path):
    path = tmp_path / "history.db"
    seed_database(path)
    service = make_service(path, answer_thinking_enabled=True, answer_max_tokens=8_192)
    asyncio.run(service.index())

    asyncio.run(service.answer("What is the playoff tiebreaker?"))

    call = service.answer_client.chat.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["max_tokens"] == 8_192
    assert "temperature" not in call


def test_answer_reports_empty_model_response_metadata_without_reasoning_text(tmp_path):
    path = tmp_path / "history.db"
    seed_database(path)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", reasoning_content="private model reasoning"),
                finish_reason="length",
            )
        ],
        usage=SimpleNamespace(completion_tokens=900),
    )
    service = make_service(path, answer_client=FakeAnswerClient(response=response))
    asyncio.run(service.index())

    with pytest.raises(RuntimeError) as error:
        asyncio.run(service.answer("What is the playoff tiebreaker?"))

    assert "finish_reason='length'" in str(error.value)
    assert "had_reasoning=True" in str(error.value)
    assert "completion_tokens=900" in str(error.value)
    assert "private model reasoning" not in str(error.value)


def test_split_discord_message_respects_limit():
    parts = split_discord_message("one two three four", limit=7)

    assert parts == ["one two", "three", "four"]
    assert all(len(part) <= 7 for part in parts)
