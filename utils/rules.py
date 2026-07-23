import asyncio
import base64
import hashlib
import heapq
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
from array import array
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

import requests
from fpdf import FPDF
from openai import AsyncOpenAI

from utils.chat_history import utc_now


DEFAULT_GITHUB_REF = "main"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 512
EMBEDDING_BATCH_SIZE = 64
RETRIEVAL_CANDIDATE_LIMIT = 12
DEFAULT_RESULT_LIMIT = 3
MAX_RULES_BYTES = 2_000_000
GITHUB_API_VERSION = "2022-11-28"

logger = logging.getLogger(__name__)

RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules_repository_state (
    repository TEXT PRIMARY KEY,
    ref TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    section_order INTEGER NOT NULL,
    heading TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    UNIQUE(repository, path, section_order)
);

CREATE INDEX IF NOT EXISTS rule_chunks_repository_idx ON rule_chunks(repository);
CREATE INDEX IF NOT EXISTS rule_chunks_content_hash_idx ON rule_chunks(content_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS rule_chunks_fts USING fts5(path, heading, content);
"""


@dataclass(frozen=True, slots=True)
class RepositoryVersion:
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True, slots=True)
class RuleDocument:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class RuleChunk:
    path: str
    section_order: int
    heading: str
    content: str
    content_hash: str

    @property
    def embedding_text(self) -> str:
        return f"{self.path}\n{self.heading}\n{self.content}"


@dataclass(frozen=True, slots=True)
class RuleMatch:
    path: str
    heading: str
    content: str
    score: float


@dataclass(frozen=True, slots=True)
class RulesRefreshResult:
    commit_sha: str
    changed: bool
    chunks: int
    embeddings_created: int
    embeddings_reused: int


@dataclass(frozen=True, slots=True)
class RulesSearchResult:
    repository: str
    ref: str
    commit_sha: str
    matches: tuple[RuleMatch, ...]


class GitHubRulesClient:
    def __init__(self, repository: str, token: str | None = None, session=None):
        if repository.count("/") != 1:
            raise ValueError("RULES_GITHUB_REPOSITORY must use the owner/repository format")
        self.repository = repository
        self.session = session or requests.Session()
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ff-bot",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _get_json(self, url: str) -> dict | list:
        response = self.session.get(url, headers=self.headers, timeout=15)
        response.raise_for_status()
        return response.json()

    def latest_version(self, ref: str) -> RepositoryVersion:
        encoded_ref = quote(ref, safe="")
        payload = self._get_json(f"https://api.github.com/repos/{self.repository}/commits/{encoded_ref}")
        return RepositoryVersion(
            commit_sha=payload["sha"],
            tree_sha=payload["commit"]["tree"]["sha"],
        )

    def markdown_documents(self, tree_sha: str) -> tuple[RuleDocument, ...]:
        tree = self._get_json(f"https://api.github.com/repos/{self.repository}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise RuntimeError("The GitHub rules tree response was truncated")

        documents = []
        total_bytes = 0
        for item in sorted(tree["tree"], key=lambda entry: entry["path"]):
            if item["type"] != "blob" or not item["path"].lower().endswith(".md"):
                continue
            blob = self._get_json(f"https://api.github.com/repos/{self.repository}/git/blobs/{item['sha']}")
            if blob.get("encoding") != "base64":
                raise RuntimeError(f"Unsupported GitHub content encoding for {item['path']}")
            raw_content = base64.b64decode(blob.get("content", ""))
            total_bytes += len(raw_content)
            if total_bytes > MAX_RULES_BYTES:
                raise RuntimeError("The GitHub rules repository exceeds the configured size limit")
            documents.append(RuleDocument(path=item["path"], content=raw_content.decode("utf-8")))
        if not documents:
            raise RuntimeError("The GitHub rules repository does not contain any Markdown files")
        return tuple(documents)


def _create_rule_chunk(
    document: RuleDocument,
    section_order: int,
    heading_parts: Sequence[str],
    lines: Sequence[str],
) -> RuleChunk | None:
    content = "\n".join(lines).strip()
    heading_only = bool(lines and re.match(r"^#{1,6}\s+", lines[0]) and not any(line.strip() for line in lines[1:]))
    if not content or heading_only:
        return None
    heading = " › ".join(heading_parts) if heading_parts else Path(document.path).stem.replace("_", " ").title()
    embedding_text = f"{document.path}\n{heading}\n{content}"
    return RuleChunk(
        path=document.path,
        section_order=section_order,
        heading=heading,
        content=content,
        content_hash=hashlib.sha256(embedding_text.encode()).hexdigest(),
    )


def build_rule_chunks(documents: Sequence[RuleDocument]) -> tuple[RuleChunk, ...]:
    chunks = []
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for document in sorted(documents, key=lambda item: item.path):
        heading_stack: list[tuple[int, str]] = []
        current_heading_parts: list[str] = []
        current_lines: list[str] = []
        section_order = 0

        def finish_section() -> None:
            nonlocal section_order
            chunk = _create_rule_chunk(
                document,
                section_order,
                current_heading_parts,
                current_lines,
            )
            if chunk:
                chunks.append(chunk)
                section_order += 1

        for line in document.content.splitlines():
            heading_match = heading_pattern.match(line)
            if not heading_match:
                current_lines.append(line)
                continue

            finish_section()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip().rstrip("#").strip()
            heading_stack = [entry for entry in heading_stack if entry[0] < level]
            heading_stack.append((level, title))
            current_heading_parts = [part for _, part in heading_stack]
            current_lines = [line]
        finish_section()
    return tuple(chunks)


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(RULES_SCHEMA)
    connection.commit()
    database_path.chmod(0o600)
    return connection


def _pack_embedding(embedding: Sequence[float], dimensions: int) -> bytes:
    vector = array("f", embedding)
    if len(vector) != dimensions:
        raise ValueError(f"Expected a {dimensions}-dimension embedding, received {len(vector)}")
    if sys.byteorder != "little":
        vector.byteswap()
    return vector.tobytes()


def _unpack_embedding(packed: bytes) -> array:
    vector = array("f")
    vector.frombytes(packed)
    if sys.byteorder != "little":
        vector.byteswap()
    return vector


def _fts_query(question: str) -> str:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "do",
        "does",
        "for",
        "how",
        "is",
        "of",
        "our",
        "the",
        "to",
        "what",
        "when",
        "who",
    }
    terms = [term.lower() for term in re.findall(r"[\w-]+", question) if len(term) > 1]
    useful_terms = [term for term in terms if term not in stopwords] or terms
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in dict.fromkeys(useful_terms))


def _current_index(
    database_path: Path,
    repository: str,
    ref: str,
    commit_sha: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> tuple[bool, int]:
    with closing(_connect(database_path)) as connection:
        state = connection.execute(
            "SELECT ref, commit_sha FROM rules_repository_state WHERE repository = ?",
            (repository,),
        ).fetchone()
        chunks, compatible_embeddings = connection.execute(
            """
            SELECT COUNT(*), SUM(
                CASE WHEN embedding_model = ? AND embedding_dimensions = ? THEN 1 ELSE 0 END
            )
            FROM rule_chunks
            WHERE repository = ?
            """,
            (embedding_model, embedding_dimensions, repository),
        ).fetchone()
    is_current = bool(
        state
        and state["ref"] == ref
        and state["commit_sha"] == commit_sha
        and chunks
        and compatible_embeddings == chunks
    )
    return is_current, chunks


def _existing_embeddings(
    database_path: Path,
    repository: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> dict[str, bytes]:
    with closing(_connect(database_path)) as connection:
        return {
            row["content_hash"]: row["embedding"]
            for row in connection.execute(
                """
                SELECT content_hash, embedding
                FROM rule_chunks
                WHERE repository = ? AND embedding_model = ? AND embedding_dimensions = ?
                """,
                (repository, embedding_model, embedding_dimensions),
            )
        }


def _replace_index(
    database_path: Path,
    repository: str,
    ref: str,
    commit_sha: str,
    chunks: Sequence[RuleChunk],
    embeddings: dict[str, bytes],
    embedding_model: str,
    embedding_dimensions: int,
) -> None:
    indexed_at = utc_now()
    with closing(_connect(database_path)) as connection:
        with connection:
            stale_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM rule_chunks WHERE repository = ?",
                    (repository,),
                )
            ]
            connection.executemany(
                "DELETE FROM rule_chunks_fts WHERE rowid = ?",
                ((chunk_id,) for chunk_id in stale_ids),
            )
            connection.execute("DELETE FROM rule_chunks WHERE repository = ?", (repository,))
            for chunk in chunks:
                cursor = connection.execute(
                    """
                    INSERT INTO rule_chunks (
                        repository, commit_sha, path, section_order, heading, content, content_hash,
                        embedding, embedding_model, embedding_dimensions, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository,
                        commit_sha,
                        chunk.path,
                        chunk.section_order,
                        chunk.heading,
                        chunk.content,
                        chunk.content_hash,
                        embeddings[chunk.content_hash],
                        embedding_model,
                        embedding_dimensions,
                        indexed_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO rule_chunks_fts (rowid, path, heading, content) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, chunk.path, chunk.heading, chunk.content),
                )
            connection.execute(
                """
                INSERT INTO rules_repository_state (repository, ref, commit_sha, refreshed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repository) DO UPDATE SET
                    ref = excluded.ref,
                    commit_sha = excluded.commit_sha,
                    refreshed_at = excluded.refreshed_at
                """,
                (repository, ref, commit_sha, indexed_at),
            )


def _lexical_ids(database_path: Path, repository: str, question: str) -> list[int]:
    query = _fts_query(question)
    if not query:
        return []
    with closing(_connect(database_path)) as connection:
        return [
            row["id"]
            for row in connection.execute(
                """
                SELECT c.id
                FROM rule_chunks_fts AS f
                JOIN rule_chunks AS c ON c.id = f.rowid
                WHERE rule_chunks_fts MATCH ? AND c.repository = ?
                ORDER BY bm25(rule_chunks_fts, 0.5, 5.0, 1.0)
                LIMIT ?
                """,
                (query, repository, RETRIEVAL_CANDIDATE_LIMIT),
            )
        ]


def _semantic_ids(
    database_path: Path,
    repository: str,
    query_embedding: bytes,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[int]:
    query_vector = _unpack_embedding(query_embedding)
    query_norm = math.sqrt(sum(value * value for value in query_vector))
    top: list[tuple[float, int]] = []
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT id, embedding
            FROM rule_chunks
            WHERE repository = ? AND embedding_model = ? AND embedding_dimensions = ?
            """,
            (repository, embedding_model, embedding_dimensions),
        )
        for row in rows:
            vector = _unpack_embedding(row["embedding"])
            if len(vector) != embedding_dimensions:
                continue
            vector_norm = math.sqrt(sum(value * value for value in vector))
            denominator = max(vector_norm * query_norm, 1e-12)
            similarity = sum(left * right for left, right in zip(vector, query_vector, strict=True)) / denominator
            candidate = (similarity, row["id"])
            if len(top) < RETRIEVAL_CANDIDATE_LIMIT:
                heapq.heappush(top, candidate)
            elif candidate > top[0]:
                heapq.heapreplace(top, candidate)
    return [chunk_id for _, chunk_id in sorted(top, reverse=True)]


def _load_matches(
    database_path: Path,
    repository: str,
    chunk_ids: Sequence[int],
    scores: dict[int, float],
) -> tuple[RuleMatch, ...]:
    if not chunk_ids:
        return ()
    placeholders = ",".join("?" for _ in chunk_ids)
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT id, path, heading, content
            FROM rule_chunks
            WHERE repository = ? AND id IN ({placeholders})
            """,
            (repository, *chunk_ids),
        ).fetchall()
    rows_by_id = {row["id"]: row for row in rows}
    return tuple(
        RuleMatch(
            path=rows_by_id[chunk_id]["path"],
            heading=rows_by_id[chunk_id]["heading"],
            content=rows_by_id[chunk_id]["content"],
            score=scores[chunk_id],
        )
        for chunk_id in chunk_ids
        if chunk_id in rows_by_id
    )


class RulesService:
    def __init__(
        self,
        database_path: str | Path,
        repository: str,
        ref: str,
        github_client,
        embedding_client,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ):
        self.database_path = Path(database_path)
        self.repository = repository
        self.ref = ref
        self.github_client = github_client
        self.embedding_client = embedding_client
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.refresh_lock = asyncio.Lock()

    @classmethod
    def from_environment(cls, database_path: str | Path):
        repository = os.getenv("RULES_GITHUB_REPOSITORY")
        if not repository:
            raise RuntimeError("RULES_GITHUB_REPOSITORY is required")
        openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        dimensions = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIMENSIONS)))
        return cls(
            database_path=database_path,
            repository=repository,
            ref=os.getenv("RULES_GITHUB_REF", DEFAULT_GITHUB_REF),
            github_client=GitHubRulesClient(repository, os.getenv("RULES_GITHUB_TOKEN")),
            embedding_client=AsyncOpenAI(api_key=openai_key),
            embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_dimensions=dimensions,
        )

    async def _create_embeddings(self, texts: Sequence[str]) -> list[bytes]:
        packed = []
        for offset in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[offset : offset + EMBEDDING_BATCH_SIZE]
            response = await self.embedding_client.embeddings.create(
                input=list(batch),
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
                encoding_format="float",
            )
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise RuntimeError("Embedding response did not contain one vector per rules chunk")
            packed.extend(_pack_embedding(item.embedding, self.embedding_dimensions) for item in data)
        return packed

    async def refresh(self) -> RulesRefreshResult:
        async with self.refresh_lock:
            version = await asyncio.to_thread(self.github_client.latest_version, self.ref)
            is_current, chunk_count = await asyncio.to_thread(
                _current_index,
                self.database_path,
                self.repository,
                self.ref,
                version.commit_sha,
                self.embedding_model,
                self.embedding_dimensions,
            )
            if is_current:
                return RulesRefreshResult(
                    commit_sha=version.commit_sha,
                    changed=False,
                    chunks=chunk_count,
                    embeddings_created=0,
                    embeddings_reused=chunk_count,
                )

            documents = await asyncio.to_thread(self.github_client.markdown_documents, version.tree_sha)
            chunks = build_rule_chunks(documents)
            if not chunks:
                raise RuntimeError("The GitHub rules repository did not produce any searchable sections")
            reusable_embeddings = await asyncio.to_thread(
                _existing_embeddings,
                self.database_path,
                self.repository,
                self.embedding_model,
                self.embedding_dimensions,
            )
            missing_chunks = [chunk for chunk in chunks if chunk.content_hash not in reusable_embeddings]
            created_embeddings = await self._create_embeddings([chunk.embedding_text for chunk in missing_chunks])
            embeddings = dict(reusable_embeddings)
            embeddings.update(
                {
                    chunk.content_hash: embedding
                    for chunk, embedding in zip(missing_chunks, created_embeddings, strict=True)
                }
            )
            await asyncio.to_thread(
                _replace_index,
                self.database_path,
                self.repository,
                self.ref,
                version.commit_sha,
                chunks,
                embeddings,
                self.embedding_model,
                self.embedding_dimensions,
            )
            logger.info(
                "Rules index refreshed: repository=%s ref=%s commit=%s chunks=%d new_embeddings=%d reused=%d",
                self.repository,
                self.ref,
                version.commit_sha[:12],
                len(chunks),
                len(missing_chunks),
                len(chunks) - len(missing_chunks),
            )
            return RulesRefreshResult(
                commit_sha=version.commit_sha,
                changed=True,
                chunks=len(chunks),
                embeddings_created=len(missing_chunks),
                embeddings_reused=len(chunks) - len(missing_chunks),
            )

    async def search(self, question: str, limit: int = DEFAULT_RESULT_LIMIT) -> RulesSearchResult:
        question = question.strip()
        if not question:
            raise ValueError("Question cannot be empty")
        refresh_result = await self.refresh()
        lexical_ids = await asyncio.to_thread(
            _lexical_ids,
            self.database_path,
            self.repository,
            question,
        )
        semantic_ids = []
        try:
            query_embedding = (await self._create_embeddings([question]))[0]
            semantic_ids = await asyncio.to_thread(
                _semantic_ids,
                self.database_path,
                self.repository,
                query_embedding,
                self.embedding_model,
                self.embedding_dimensions,
            )
        except Exception:
            if not lexical_ids:
                raise
            logger.exception("Rules semantic retrieval failed; using keyword matches")

        scores: dict[int, float] = {}
        for ranking in (lexical_ids, semantic_ids):
            for rank, chunk_id in enumerate(ranking, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (60 + rank)
        selected_ids = sorted(scores, key=scores.get, reverse=True)[:limit]
        matches = await asyncio.to_thread(
            _load_matches,
            self.database_path,
            self.repository,
            selected_ids,
            scores,
        )
        logger.info(
            "Rules retrieval: commit=%s keyword_candidates=%d embedding_candidates=%d matches=%d",
            refresh_result.commit_sha[:12],
            len(lexical_ids),
            len(semantic_ids),
            len(matches),
        )
        return RulesSearchResult(
            repository=self.repository,
            ref=self.ref,
            commit_sha=refresh_result.commit_sha,
            matches=matches,
        )


def _pdf_safe_text(value: str) -> str:
    replacements = str.maketrans(
        {
            "—": "-",
            "–": "-",
            "’": "'",
            "“": '"',
            "”": '"',
            "…": "...",
            "•": "-",
            "→": "->",
            "›": ">",
            "≤": "<=",
            "≥": ">=",
        }
    )
    text = value.translate(replacements)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.encode("latin-1", errors="replace").decode("latin-1")


class _RulesPDF(FPDF):
    def __init__(self, accurate_as: str):
        super().__init__(format="letter")
        self.accurate_as = accurate_as
        self.set_margins(16, 18, 16)
        self.set_auto_page_break(auto=True, margin=16)
        self.set_title("Relevant league rules")
        self.set_author("Longview League Discord Bot")

    def header(self):
        self.set_font("helvetica", "I", 8)
        self.set_text_color(90, 90, 90)
        self.cell(0, 5, _pdf_safe_text(f"Accurate as of {self.accurate_as}"), align="R")
        self.ln(7)
        self.set_draw_color(205, 205, 205)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 5, f"Page {self.page_no()}", align="C")


def _render_markdown(pdf: FPDF, markdown: str) -> None:
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for original_line in markdown.splitlines():
        line = _pdf_safe_text(original_line.rstrip())
        if not line:
            pdf.ln(2)
            continue
        heading = heading_pattern.match(line)
        if heading:
            level = len(heading.group(1))
            size = max(11, 16 - level)
            pdf.set_font("helvetica", "B", size)
            pdf.set_text_color(32, 54, 78)
            pdf.multi_cell(
                0,
                6,
                heading.group(2).strip().rstrip("#").strip(),
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.ln(1)
            continue
        if line.startswith("|"):
            pdf.set_font("courier", size=8)
            pdf.set_text_color(25, 25, 25)
            pdf.multi_cell(0, 4, line, new_x="LMARGIN", new_y="NEXT")
            continue
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(25, 25, 25)
        pdf.multi_cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")


def write_rules_pdf(
    path: str | Path,
    question: str,
    result: RulesSearchResult,
    generated_at: datetime | None = None,
) -> None:
    timestamp = (generated_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    pdf = _RulesPDF(timestamp)
    pdf.set_compression(False)
    pdf.add_page()
    pdf.set_font("helvetica", "B", 18)
    pdf.set_text_color(26, 52, 78)
    pdf.multi_cell(0, 9, "Relevant league rules", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(20, 5, "Question:")
    pdf.set_font("helvetica", size=10)
    pdf.multi_cell(0, 5, _pdf_safe_text(question.strip()), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(20, 5, "Revision:")
    pdf.set_font("courier", size=9)
    pdf.cell(0, 5, result.commit_sha[:12])

    for number, match in enumerate(result.matches, start=1):
        pdf.ln(10)
        pdf.set_draw_color(190, 190, 190)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)
        pdf.set_font("helvetica", "B", 12)
        pdf.set_text_color(32, 54, 78)
        section_title = _pdf_safe_text(f"{number}. {match.path} - {match.heading}")
        pdf.multi_cell(0, 7, section_title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        _render_markdown(pdf, match.content)
    pdf.output(str(path))


@contextmanager
def temporary_pdf_path():
    temporary_file = tempfile.NamedTemporaryFile(prefix="ff-bot-rules-", suffix=".pdf", delete=False)
    temporary_file.close()
    path = Path(temporary_file.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
