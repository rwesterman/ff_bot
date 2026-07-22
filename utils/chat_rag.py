import asyncio
import hashlib
import heapq
import math
import os
import re
import sqlite3
import sys
from array import array
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from openai import AsyncOpenAI

from utils.chat_history import HistoryDatabase, sync_client_history, utc_now


DEFAULT_CHANNEL_IDS = (
    604075794620219414,
    1145771776257818674,
    1252383178912567466,
    1147256554144419881,
    1145771716216356964,
    1151148913756213270,
    1278382036930789550,
    1386706556929966131,
    1147257515927674930,
    1154131955441471640,
    1154133577357869076,
)
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 512
DEFAULT_ANSWER_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MAX_CHUNK_MESSAGES = 40
MAX_CHUNK_CHARACTERS = 3_500
MAX_CHUNK_GAP = timedelta(minutes=60)
CHUNK_OVERLAP_MESSAGES = 2
MAX_EVIDENCE_CHARACTERS = 24_000
EMBEDDING_BATCH_SIZE = 64
VECTOR_SCAN_BATCH_SIZE = 64
RETRIEVAL_CANDIDATE_LIMIT = 20


RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_chunks (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    first_message_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    embedding BLOB,
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS conversation_chunks_channel_time_idx
ON conversation_chunks(channel_id, started_at);

CREATE TABLE IF NOT EXISTS chunk_messages (
    chunk_id INTEGER NOT NULL REFERENCES conversation_chunks(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, message_id)
);

CREATE INDEX IF NOT EXISTS chunk_messages_message_idx
ON chunk_messages(message_id);

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_chunks_fts USING fts5(content);
"""


ANSWER_SYSTEM_PROMPT = """You answer questions about a private fantasy-football league using only the supplied
Discord history excerpts. Treat proposals, jokes, guesses, and tentative discussion as weaker than an explicit final
decision. When the history conflicts, prefer the newest explicit supported decision and mention the older conflict.
Cite factual claims with source labels such as [1]. Do not claim that chat history replaces official league rules.
If the excerpts do not support an answer, say that you could not find enough evidence. Never invent an answer."""


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    id: int
    guild_id: int
    channel_id: int
    channel_name: str
    author_name: str
    content: str
    created_at: datetime
    reference_message_id: int | None = None
    attachment_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationChunk:
    id: int
    guild_id: int
    channel_id: int
    channel_name: str
    first_message_id: int
    last_message_id: int
    started_at: str
    ended_at: str
    content: str
    content_hash: str
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class IndexResult:
    chunks: int
    messages: int
    embeddings_created: int
    embeddings_reused: int


@dataclass(frozen=True, slots=True)
class RefreshResult:
    messages_synced: int
    channels_synced: int
    unavailable_channel_ids: tuple[int, ...]
    index: IndexResult


@dataclass(frozen=True, slots=True)
class Evidence:
    chunk_id: int
    guild_id: int
    channel_id: int
    channel_name: str
    first_message_id: int
    started_at: str
    ended_at: str
    content: str
    score: float

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.first_message_id}"


@dataclass(frozen=True, slots=True)
class RagAnswer:
    text: str
    evidence: tuple[Evidence, ...]


def parse_channel_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return DEFAULT_CHANNEL_IDS
    try:
        ids = tuple(dict.fromkeys(int(part.strip()) for part in value.split(",") if part.strip()))
    except ValueError as error:
        raise ValueError("RAG_CHANNEL_IDS must be a comma-separated list of Discord channel IDs") from error
    if not ids:
        raise ValueError("RAG_CHANNEL_IDS must contain at least one channel ID")
    return ids


def _message_text(message: ArchivedMessage, references: dict[int, ArchivedMessage], chunk_ids: set[int]) -> str:
    timestamp = message.created_at.isoformat(timespec="minutes")
    content = " ".join(message.content.split())
    attachment_text = ""
    if message.attachment_names:
        attachment_text = f" [attachments: {', '.join(message.attachment_names)}]"
    reply_text = ""
    if message.reference_message_id and message.reference_message_id not in chunk_ids:
        parent = references.get(message.reference_message_id)
        if parent:
            parent_excerpt = " ".join(parent.content.split())[:240]
            reply_text = f" [replying to {parent.author_name}: {parent_excerpt}]"
    return f"[{timestamp}] {message.author_name}: {content}{attachment_text}{reply_text}".strip()


def _estimated_message_text(message: ArchivedMessage, references: dict[int, ArchivedMessage]) -> str:
    return _message_text(message, references, set())


def _create_chunk(messages: Sequence[ArchivedMessage], references: dict[int, ArchivedMessage]) -> ConversationChunk:
    chunk_ids = {message.id for message in messages}
    content = "\n".join(_message_text(message, references, chunk_ids) for message in messages)
    digest_input = f"{messages[0].channel_id}:{','.join(str(message.id) for message in messages)}:{content}"
    content_hash = hashlib.sha256(digest_input.encode()).hexdigest()
    chunk_id = int(content_hash[:15], 16)
    return ConversationChunk(
        id=chunk_id,
        guild_id=messages[0].guild_id,
        channel_id=messages[0].channel_id,
        channel_name=messages[0].channel_name,
        first_message_id=messages[0].id,
        last_message_id=messages[-1].id,
        started_at=messages[0].created_at.isoformat(),
        ended_at=messages[-1].created_at.isoformat(),
        content=content,
        content_hash=content_hash,
        message_ids=tuple(message.id for message in messages),
    )


def build_conversation_chunks(messages: Sequence[ArchivedMessage]) -> list[ConversationChunk]:
    """Group sparse messages into bounded, chronological channel conversations."""
    references = {message.id: message for message in messages}
    chunks = []
    for channel_id in dict.fromkeys(message.channel_id for message in messages):
        channel_messages = sorted(
            (message for message in messages if message.channel_id == channel_id),
            key=lambda message: (message.created_at, message.id),
        )
        current = []
        current_characters = 0
        for message in channel_messages:
            if not message.content.strip() and not message.attachment_names:
                continue
            estimated_text = _estimated_message_text(message, references)
            gap_split = bool(current and message.created_at - current[-1].created_at > MAX_CHUNK_GAP)
            size_split = bool(
                current
                and (
                    len(current) >= MAX_CHUNK_MESSAGES
                    or current_characters + len(estimated_text) + 1 > MAX_CHUNK_CHARACTERS
                )
            )
            if gap_split or size_split:
                chunks.append(_create_chunk(current, references))
                current = [] if gap_split else current[-CHUNK_OVERLAP_MESSAGES:]
                current_characters = sum(
                    len(_estimated_message_text(overlap_message, references)) + 1 for overlap_message in current
                )
                while current and current_characters + len(estimated_text) + 1 > MAX_CHUNK_CHARACTERS:
                    removed = current.pop(0)
                    current_characters -= len(_estimated_message_text(removed, references)) + 1
            current.append(message)
            current_characters += len(estimated_text) + 1
        if current:
            chunks.append(_create_chunk(current, references))
    return chunks


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(RAG_SCHEMA)
    return connection


def load_archived_messages(connection: sqlite3.Connection, channel_ids: Sequence[int]) -> list[ArchivedMessage]:
    placeholders = ",".join("?" for _ in channel_ids)
    rows = connection.execute(
        f"""
        SELECT m.id, m.guild_id, m.channel_id, c.name AS channel_name, m.author_name, m.content,
               m.created_at, m.reference_message_id
        FROM messages AS m
        JOIN channels AS c ON c.id = m.channel_id
        WHERE m.channel_id IN ({placeholders})
        ORDER BY m.channel_id, m.created_at, m.id
        """,
        tuple(channel_ids),
    ).fetchall()
    message_ids = [row["id"] for row in rows]
    attachments: dict[int, list[str]] = {}
    for offset in range(0, len(message_ids), 900):
        batch = message_ids[offset : offset + 900]
        batch_placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"SELECT message_id, filename FROM attachments WHERE message_id IN ({batch_placeholders}) ORDER BY id",
            batch,
        ):
            attachments.setdefault(row["message_id"], []).append(row["filename"])
    return [
        ArchivedMessage(
            id=row["id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            author_name=row["author_name"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            reference_message_id=row["reference_message_id"],
            attachment_names=tuple(attachments.get(row["id"], ())),
        )
        for row in rows
    ]


def _fts_query(question: str) -> str:
    stopwords = {"a", "an", "and", "are", "for", "how", "is", "of", "the", "to", "what", "when", "who"}
    terms = [term.lower() for term in re.findall(r"[\w-]+", question) if len(term) > 1]
    useful_terms = [term for term in terms if term not in stopwords] or terms
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in dict.fromkeys(useful_terms))


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


def _sync_index_metadata(
    database_path: Path,
    channel_ids: Sequence[int],
    embedding_model: str,
    embedding_dimensions: int,
) -> tuple[int, int, int]:
    """Rebuild chunk metadata while retaining compatible embeddings in SQLite."""
    with closing(_connect(database_path)) as connection:
        messages = load_archived_messages(connection, channel_ids)
        chunks = build_conversation_chunks(messages)
        indexed_at = utc_now()
        with connection:
            connection.execute("CREATE TEMP TABLE desired_chunk_ids (id INTEGER PRIMARY KEY)")
            connection.execute("DELETE FROM chunk_messages")
            connection.execute("DELETE FROM conversation_chunks_fts")
            for chunk in chunks:
                connection.execute("INSERT INTO desired_chunk_ids (id) VALUES (?)", (chunk.id,))
                connection.execute(
                    """
                    INSERT INTO conversation_chunks (
                        id, guild_id, channel_id, channel_name, first_message_id, last_message_id,
                        started_at, ended_at, content, content_hash, embedding, embedding_model,
                        embedding_dimensions, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        guild_id = excluded.guild_id,
                        channel_id = excluded.channel_id,
                        channel_name = excluded.channel_name,
                        first_message_id = excluded.first_message_id,
                        last_message_id = excluded.last_message_id,
                        started_at = excluded.started_at,
                        ended_at = excluded.ended_at,
                        content = excluded.content,
                        content_hash = excluded.content_hash,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        chunk.id,
                        chunk.guild_id,
                        chunk.channel_id,
                        chunk.channel_name,
                        chunk.first_message_id,
                        chunk.last_message_id,
                        chunk.started_at,
                        chunk.ended_at,
                        chunk.content,
                        chunk.content_hash,
                        indexed_at,
                    ),
                )
                connection.executemany(
                    "INSERT INTO chunk_messages (chunk_id, message_id, position) VALUES (?, ?, ?)",
                    ((chunk.id, message_id, position) for position, message_id in enumerate(chunk.message_ids)),
                )
                connection.execute(
                    "INSERT INTO conversation_chunks_fts (rowid, content) VALUES (?, ?)",
                    (chunk.id, chunk.content),
                )
            connection.execute("DELETE FROM conversation_chunks WHERE id NOT IN (SELECT id FROM desired_chunk_ids)")
        missing_embeddings = connection.execute(
            """
            SELECT COUNT(*) FROM conversation_chunks
            WHERE embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?
               OR embedding_dimensions IS NULL OR embedding_dimensions != ?
            """,
            (embedding_model, embedding_dimensions),
        ).fetchone()[0]
    return len(messages), len(chunks), missing_embeddings


def _load_missing_embedding_batch(
    database_path: Path,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[tuple[int, str]]:
    with closing(_connect(database_path)) as connection:
        return [
            (row["id"], row["content"])
            for row in connection.execute(
                """
                SELECT id, content FROM conversation_chunks
                WHERE embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?
                   OR embedding_dimensions IS NULL OR embedding_dimensions != ?
                ORDER BY id
                LIMIT ?
                """,
                (embedding_model, embedding_dimensions, EMBEDDING_BATCH_SIZE),
            )
        ]


def _store_embedding_batch(
    database_path: Path,
    rows: Sequence[tuple[int, bytes]],
    embedding_model: str,
    embedding_dimensions: int,
) -> None:
    with closing(_connect(database_path)) as connection:
        with connection:
            connection.executemany(
                """
                UPDATE conversation_chunks
                SET embedding = ?, embedding_model = ?, embedding_dimensions = ?, indexed_at = ?
                WHERE id = ?
                """,
                (
                    (embedding, embedding_model, embedding_dimensions, utc_now(), chunk_id)
                    for chunk_id, embedding in rows
                ),
            )


def _current_index_result(
    database_path: Path,
    channel_ids: Sequence[int],
    embedding_model: str,
    embedding_dimensions: int,
) -> IndexResult | None:
    with closing(_connect(database_path)) as connection:
        chunks, invalid_embeddings = connection.execute(
            """
            SELECT COUNT(*), SUM(
                CASE WHEN embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?
                           OR embedding_dimensions IS NULL OR embedding_dimensions != ?
                     THEN 1 ELSE 0 END
            )
            FROM conversation_chunks
            """,
            (embedding_model, embedding_dimensions),
        ).fetchone()
        if not chunks or invalid_embeddings:
            return None
        placeholders = ",".join("?" for _ in channel_ids)
        messages = connection.execute(
            f"SELECT COUNT(*) FROM messages WHERE channel_id IN ({placeholders})",
            tuple(channel_ids),
        ).fetchone()[0]
    return IndexResult(chunks=chunks, messages=messages, embeddings_created=0, embeddings_reused=chunks)


def _lexical_chunk_ids(database_path: Path, question: str) -> tuple[list[int], int]:
    with closing(_connect(database_path)) as connection:
        chunk_count = connection.execute("SELECT COUNT(*) FROM conversation_chunks").fetchone()[0]
        query = _fts_query(question)
        if not query:
            return [], chunk_count
        ids = [
            row["id"]
            for row in connection.execute(
                """
                SELECT c.id
                FROM conversation_chunks_fts AS f
                JOIN conversation_chunks AS c ON c.id = f.rowid
                WHERE conversation_chunks_fts MATCH ?
                ORDER BY bm25(conversation_chunks_fts)
                LIMIT ?
                """,
                (query, RETRIEVAL_CANDIDATE_LIMIT),
            )
        ]
    return ids, chunk_count


def _semantic_chunk_ids(
    database_path: Path,
    query_embedding: bytes,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[int]:
    """Find exact cosine nearest neighbors while holding only one SQLite batch in memory."""
    query_vector = _unpack_embedding(query_embedding)
    query_norm = math.sqrt(sum(value * value for value in query_vector))
    top: list[tuple[float, int]] = []
    with closing(_connect(database_path)) as connection:
        cursor = connection.execute(
            """
            SELECT id, embedding FROM conversation_chunks
            WHERE embedding_model = ? AND embedding_dimensions = ? AND embedding IS NOT NULL
            """,
            (embedding_model, embedding_dimensions),
        )
        while rows := cursor.fetchmany(VECTOR_SCAN_BATCH_SIZE):
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


def _load_evidence(database_path: Path, chunk_ids: Sequence[int], scores: dict[int, float]) -> tuple[Evidence, ...]:
    if not chunk_ids:
        return ()
    placeholders = ",".join("?" for _ in chunk_ids)
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT id, guild_id, channel_id, channel_name, first_message_id, started_at, ended_at, content
            FROM conversation_chunks
            WHERE id IN ({placeholders})
            """,
            tuple(chunk_ids),
        ).fetchall()
    rows_by_id = {row["id"]: row for row in rows}
    return tuple(
        Evidence(
            chunk_id=chunk_id,
            guild_id=rows_by_id[chunk_id]["guild_id"],
            channel_id=rows_by_id[chunk_id]["channel_id"],
            channel_name=rows_by_id[chunk_id]["channel_name"],
            first_message_id=rows_by_id[chunk_id]["first_message_id"],
            started_at=rows_by_id[chunk_id]["started_at"],
            ended_at=rows_by_id[chunk_id]["ended_at"],
            content=rows_by_id[chunk_id]["content"],
            score=scores[chunk_id],
        )
        for chunk_id in chunk_ids
        if chunk_id in rows_by_id
    )


class HistoryRagService:
    def __init__(
        self,
        database_path: str | Path,
        channel_ids: Sequence[int],
        embedding_client,
        answer_client,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        answer_model: str = DEFAULT_ANSWER_MODEL,
        answer_concurrency: int = 2,
    ):
        self.database_path = Path(database_path)
        self.channel_ids = tuple(channel_ids)
        self.embedding_client = embedding_client
        self.answer_client = answer_client
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.answer_model = answer_model
        self.refresh_lock = asyncio.Lock()
        self.answer_semaphore = asyncio.Semaphore(answer_concurrency)

    @classmethod
    def from_environment(cls, database_path: str | Path):
        openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        if not openai_key or not deepseek_key:
            missing = [
                name
                for name, value in (("OPENAI_API_KEY", openai_key), ("DEEPSEEK_API_KEY", deepseek_key))
                if not value
            ]
            raise RuntimeError(f"Missing required RAG settings: {', '.join(missing)}")
        dimensions = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIMENSIONS)))
        return cls(
            database_path=database_path,
            channel_ids=parse_channel_ids(os.getenv("RAG_CHANNEL_IDS")),
            embedding_client=AsyncOpenAI(api_key=openai_key),
            answer_client=AsyncOpenAI(
                api_key=deepseek_key,
                base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
            ),
            embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_dimensions=dimensions,
            answer_model=os.getenv("DEEPSEEK_MODEL", DEFAULT_ANSWER_MODEL),
        )

    async def refresh(self, discord_client) -> RefreshResult:
        async with self.refresh_lock:
            with HistoryDatabase(self.database_path) as database:
                sync_result = await sync_client_history(database, discord_client, self.channel_ids)
            index_result = None
            if sync_result.messages_seen == 0:
                index_result = await asyncio.to_thread(
                    _current_index_result,
                    self.database_path,
                    self.channel_ids,
                    self.embedding_model,
                    self.embedding_dimensions,
                )
            if index_result is None:
                index_result = await self.index()
            return RefreshResult(
                messages_synced=sync_result.messages_seen,
                channels_synced=sync_result.channels_synced,
                unavailable_channel_ids=sync_result.unavailable_channel_ids,
                index=index_result,
            )

    async def _create_embeddings(self, texts: Sequence[str]) -> list[bytes]:
        packed = []
        for offset in range(0, len(texts), 64):
            batch = texts[offset : offset + 64]
            response = await self.embedding_client.embeddings.create(
                input=list(batch),
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
                encoding_format="float",
            )
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise RuntimeError("Embedding response did not contain one vector per chunk")
            packed.extend(_pack_embedding(item.embedding, self.embedding_dimensions) for item in data)
        return packed

    async def index(self) -> IndexResult:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        messages, chunks, missing_embeddings = await asyncio.to_thread(
            _sync_index_metadata,
            self.database_path,
            self.channel_ids,
            self.embedding_model,
            self.embedding_dimensions,
        )
        embeddings_created = 0
        while batch := await asyncio.to_thread(
            _load_missing_embedding_batch,
            self.database_path,
            self.embedding_model,
            self.embedding_dimensions,
        ):
            packed_embeddings = await self._create_embeddings([content for _, content in batch])
            await asyncio.to_thread(
                _store_embedding_batch,
                self.database_path,
                list(zip((chunk_id for chunk_id, _ in batch), packed_embeddings, strict=True)),
                self.embedding_model,
                self.embedding_dimensions,
            )
            embeddings_created += len(batch)
        return IndexResult(
            chunks=chunks,
            messages=messages,
            embeddings_created=embeddings_created,
            embeddings_reused=chunks - missing_embeddings,
        )

    async def retrieve(self, question: str, limit: int = 8) -> tuple[Evidence, ...]:
        question = question.strip()
        if not question:
            raise ValueError("Question cannot be empty")
        lexical_ids, chunk_count = await asyncio.to_thread(_lexical_chunk_ids, self.database_path, question)
        if not chunk_count:
            raise RuntimeError("The chat history index is empty")

        semantic_ids = []
        try:
            query_embedding = (await self._create_embeddings([question]))[0]
            semantic_ids = await asyncio.to_thread(
                _semantic_chunk_ids,
                self.database_path,
                query_embedding,
                self.embedding_model,
                self.embedding_dimensions,
            )
        except Exception:
            if not lexical_ids:
                raise

        scores: dict[int, float] = {}
        for ranking in (lexical_ids, semantic_ids):
            for rank, chunk_id in enumerate(ranking, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (60 + rank)
        selected_ids = sorted(scores, key=scores.get, reverse=True)[:limit]
        return await asyncio.to_thread(_load_evidence, self.database_path, selected_ids, scores)

    async def answer(self, question: str) -> RagAnswer:
        async with self.answer_semaphore:
            evidence = await self.retrieve(question)
            context_parts = []
            context_characters = 0
            retained_evidence = []
            for number, item in enumerate(evidence, start=1):
                part = f"[Source {number}] #{item.channel_name}, {item.started_at} to {item.ended_at}\n{item.content}"
                if context_parts and context_characters + len(part) > MAX_EVIDENCE_CHARACTERS:
                    break
                context_parts.append(part)
                context_characters += len(part)
                retained_evidence.append(item)
            response = await self.answer_client.chat.completions.create(
                model=self.answer_model,
                messages=[
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Question: {question.strip()}\n\nDiscord history excerpts:\n\n"
                        + "\n\n".join(context_parts),
                    },
                ],
                temperature=0.1,
                max_tokens=900,
            )
            text = response.choices[0].message.content if response.choices else None
            if not text or not text.strip():
                raise RuntimeError("The answer model returned an empty response")
            return RagAnswer(text.strip(), tuple(retained_evidence))

    def has_index(self) -> bool:
        if not self.database_path.exists():
            return False
        with closing(_connect(self.database_path)) as connection:
            return connection.execute("SELECT EXISTS(SELECT 1 FROM conversation_chunks)").fetchone()[0] == 1


def format_discord_answer(answer: RagAnswer, stale: bool = False) -> str:
    sections = []
    if stale:
        sections.append("_History refresh failed; this answer uses the last successful index._")
    sections.append(answer.text)
    sources = []
    for number, evidence in enumerate(answer.evidence[:5], start=1):
        date = evidence.started_at[:10]
        sources.append(f"[{number}] [#{evidence.channel_name} — {date}]({evidence.jump_url})")
    if sources:
        sections.append("Sources: " + " · ".join(sources))
    sections.append("_Chat history may not match the current official league rules._")
    return "\n\n".join(sections)


def split_discord_message(content: str, limit: int = 1_950) -> list[str]:
    if len(content) <= limit:
        return [content]
    parts = []
    remaining = content
    while remaining:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return parts


async def index_and_ask(service: HistoryRagService, question: str) -> RagAnswer:
    """Local, Discord-free entry point used by the CLI and live validation."""
    await service.index()
    return await service.answer(question)
