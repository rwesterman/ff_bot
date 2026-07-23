import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from utils.rules import (
    RepositoryVersion,
    RuleDocument,
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


def make_service(path, github_client, embedding_client):
    return RulesService(
        database_path=path,
        repository="rwesterman/longview_league_rules",
        ref="main",
        github_client=github_client,
        embedding_client=embedding_client,
        embedding_dimensions=4,
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


def test_temporary_pdf_is_deleted_after_use():
    with temporary_pdf_path() as output_path:
        output_path.write_bytes(b"%PDF-test")
        assert output_path.exists()

    assert not output_path.exists()
