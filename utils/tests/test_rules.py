import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import utils.rules as rules_module
from utils.rules import (
    RULES_ANSWER_SYSTEM_PROMPT,
    RepositoryVersion,
    RuleDocument,
    RuleMatch,
    RulesSearchResult,
    RulesService,
    build_rule_chunks,
    temporary_pdf_path,
    write_rules_pdf,
)


class FakeGitHubClient:
    def __init__(self, documents):
        self.version = RepositoryVersion(commit_sha="a" * 40, tree_sha="tree-a")
        self.documents = documents
        self.latest_calls = 0
        self.document_calls = 0

    def latest_version(self, ref):
        assert ref == "main"
        self.latest_calls += 1
        return self.version

    def markdown_documents(self, tree_sha):
        assert tree_sha == self.version.tree_sha
        self.document_calls += 1
        return self.documents


class FakeEmbeddings:
    def __init__(self):
        self.inputs = []

    async def create(self, *, input, model, dimensions, encoding_format):
        self.inputs.extend(input)
        data = []
        for index, text in enumerate(input):
            normalized = text.lower()
            vector = [0.0] * dimensions
            vector[0] = 1.0 if "playoff" in normalized or "tiebreak" in normalized else 0.1
            vector[1] = 1.0 if "waiver" in normalized or "faab" in normalized else 0.1
            vector[2] = 1.0
            data.append(SimpleNamespace(index=index, embedding=vector))
        return SimpleNamespace(data=data)


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()


class FakeCompletions:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.response is not None:
            return self.response
        message = SimpleNamespace(
            content="Head-to-head record breaks a playoff seeding tie.\n\n"
            "> Head-to-head record breaks a playoff seeding tie."
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class FakeAnswerClient:
    def __init__(self, response=None):
        self.chat = SimpleNamespace(completions=FakeCompletions(response=response))


def rule_documents(playoff_rule="Head-to-head record breaks a playoff seeding tie."):
    return (
        RuleDocument(
            path="RULES.md",
            content=f"""# Current League Rules

## Playoffs

{playoff_rule}

## Waivers

Waivers use a $100 FAAB budget.
""",
        ),
    )


def make_service(path, github_client, embedding_client, answer_client=None, **service_options):
    return RulesService(
        database_path=path,
        repository="rwesterman/longview_league_rules",
        ref="main",
        github_client=github_client,
        embedding_client=embedding_client,
        answer_client=answer_client or FakeAnswerClient(),
        embedding_dimensions=4,
        **service_options,
    )


def test_markdown_is_chunked_by_heading_hierarchy():
    chunks = build_rule_chunks(
        (
            RuleDocument(
                path="SETTINGS.md",
                content="""# Settings

## Offensive scoring

Scoring introduction.

### Passing

Passing touchdowns are worth six points.
""",
            ),
        )
    )

    assert [chunk.heading for chunk in chunks] == [
        "Settings › Offensive scoring",
        "Settings › Offensive scoring › Passing",
    ]
    assert chunks[1].content == "### Passing\n\nPassing touchdowns are worth six points."


def test_search_checks_github_each_time_but_only_downloads_changed_rules(tmp_path):
    github_client = FakeGitHubClient(rule_documents())
    embedding_client = FakeEmbeddingClient()
    service = make_service(tmp_path / "history.db", github_client, embedding_client)

    first = asyncio.run(service.search("What is the playoff tiebreaker?", limit=1))
    second = asyncio.run(service.search("What is the playoff tiebreaker?", limit=1))

    assert github_client.latest_calls == 2
    assert github_client.document_calls == 1
    assert first.commit_sha == "a" * 40
    assert second.matches[0].heading == "Current League Rules › Playoffs"
    assert "Head-to-head record" in second.matches[0].content


def test_rules_refresh_reuses_unchanged_section_embeddings(tmp_path):
    github_client = FakeGitHubClient(rule_documents())
    embedding_client = FakeEmbeddingClient()
    service = make_service(tmp_path / "history.db", github_client, embedding_client)
    first = asyncio.run(service.refresh())

    github_client.version = RepositoryVersion(commit_sha="b" * 40, tree_sha="tree-b")
    github_client.documents = rule_documents("Total points breaks a playoff seeding tie.")
    second = asyncio.run(service.refresh())

    assert first.embeddings_created == 2
    assert second.embeddings_created == 1
    assert second.embeddings_reused == 1
    assert second.commit_sha == "b" * 40


def test_rules_answer_uses_retrieved_excerpts_and_existing_deepseek_settings(tmp_path):
    github_client = FakeGitHubClient(rule_documents())
    answer_client = FakeAnswerClient()
    service = make_service(
        tmp_path / "history.db",
        github_client,
        FakeEmbeddingClient(),
        answer_client=answer_client,
    )

    answer = asyncio.run(service.answer("What is the playoff tiebreaker?"))

    assert answer.text.startswith("Head-to-head")
    assert answer.sources.matches[0].path == "RULES.md"
    call = answer_client.chat.completions.calls[0]
    assert call["messages"][0]["content"] == RULES_ANSWER_SYSTEM_PROMPT
    assert "Keep the answer succinct" in RULES_ANSWER_SYSTEM_PROMPT
    assert "preferably include a brief exact quote" in RULES_ANSWER_SYSTEM_PROMPT
    assert "Question: What is the playoff tiebreaker?" in call["messages"][1]["content"]
    assert "[Reference 1] RULES.md" in call["messages"][1]["content"]
    assert "Head-to-head record breaks a playoff seeding tie." in call["messages"][1]["content"]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["max_tokens"] == 4_096
    assert call["temperature"] == 0.1


def test_rules_service_reads_deepseek_settings_from_environment(tmp_path, monkeypatch):
    clients = []

    def fake_client(**kwargs):
        client = SimpleNamespace(configuration=kwargs)
        clients.append(client)
        return client

    monkeypatch.setenv("RULES_GITHUB_REPOSITORY", "rwesterman/longview_league_rules")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_THINKING_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "8192")
    monkeypatch.setattr(rules_module, "AsyncOpenAI", fake_client)

    service = RulesService.from_environment(tmp_path / "history.db")

    assert service.answer_thinking_enabled is True
    assert service.answer_max_tokens == 8_192
    assert clients[0].configuration == {"api_key": "test-openai-key"}
    assert clients[1].configuration["api_key"] == "test-deepseek-key"
    assert clients[1].configuration["base_url"] == "https://api.deepseek.com"


def test_rules_answer_honors_thinking_and_token_settings(tmp_path):
    answer_client = FakeAnswerClient()
    service = make_service(
        tmp_path / "history.db",
        FakeGitHubClient(rule_documents()),
        FakeEmbeddingClient(),
        answer_client=answer_client,
        answer_thinking_enabled=True,
        answer_max_tokens=8_192,
    )

    asyncio.run(service.answer("What is the playoff tiebreaker?"))

    call = answer_client.chat.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["max_tokens"] == 8_192
    assert "temperature" not in call


def test_pdf_contains_verbatim_rules_and_accuracy_timestamp(tmp_path):
    github_client = FakeGitHubClient(rule_documents())
    service = make_service(tmp_path / "history.db", github_client, FakeEmbeddingClient())
    result = asyncio.run(service.search("What is the playoff tiebreaker?", limit=1))
    output_path = tmp_path / "rules.pdf"

    write_rules_pdf(
        output_path,
        "What is the playoff tiebreaker?",
        result,
        generated_at=datetime(2026, 7, 23, 18, 30, tzinfo=UTC),
    )
    pdf_content = output_path.read_bytes()

    assert pdf_content.startswith(b"%PDF")
    assert b"Accurate as of 2026-07-23 18:30 UTC" in pdf_content
    assert b"RULES.md - Current League Rules" in pdf_content
    assert b"Head-to-head record breaks a playoff seeding tie." in pdf_content
    assert b"github.com" not in pdf_content


def test_pdf_renders_markdown_table_as_pdf_cells(tmp_path):
    result = RulesSearchResult(
        repository="rwesterman/longview_league_rules",
        ref="main",
        commit_sha="b" * 40,
        matches=(
            RuleMatch(
                path="RECORDS.md",
                heading="League champions",
                content="""## League champions

| Year | Champion | Record |
| ---: | --- | :---: |
| 2025 | **Wolves** | 11-3 |
| 2024 | Bears | 10-4 |
""",
                score=1.0,
            ),
        ),
    )
    output_path = tmp_path / "records.pdf"

    write_rules_pdf(output_path, "Who won the league?", result)
    pdf_content = output_path.read_bytes()

    assert b"Year" in pdf_content
    assert b"Wolves" in pdf_content
    assert b"11-3" in pdf_content
    assert b"| ---:" not in pdf_content


def test_temporary_pdf_is_deleted_after_use():
    with temporary_pdf_path() as output_path:
        output_path.write_bytes(b"%PDF-test")
        assert output_path.exists()

    assert not output_path.exists()
