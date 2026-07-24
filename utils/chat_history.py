import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    parent_id INTEGER,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    latest_message_id INTEGER,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    author_is_bot INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    message_type TEXT NOT NULL,
    reference_message_id INTEGER
);

CREATE INDEX IF NOT EXISTS messages_channel_created_idx
ON messages(channel_id, created_at);

CREATE INDEX IF NOT EXISTS messages_author_idx
ON messages(author_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    url TEXT NOT NULL,
    content_type TEXT,
    size INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS attachments_message_idx
ON attachments(message_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    full_sync INTEGER NOT NULL,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ChannelSyncResult:
    channel_id: int
    channel_name: str
    messages_seen: int
    elapsed_seconds: float


class HistoryDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self.connection.commit()
        self.path.chmod(0o600)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.close()

    def start_sync_run(self, full_sync: bool) -> int:
        cursor = self.connection.execute(
            "INSERT INTO sync_runs (started_at, full_sync, status) VALUES (?, ?, ?)",
            (utc_now(), full_sync, "running"),
        )
        self.connection.commit()
        return cursor.lastrowid

    def finish_sync_run(self, run_id: int, messages_seen: int, error: str | None = None):
        self.connection.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, messages_seen = ?, status = ?, error = ?
            WHERE id = ?
            """,
            (utc_now(), messages_seen, "failed" if error else "complete", error, run_id),
        )
        self.connection.commit()

    def upsert_channel(self, channel):
        guild = channel.guild
        self.connection.execute(
            """
            INSERT INTO guilds (id, name, synced_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, synced_at = excluded.synced_at
            """,
            (guild.id, guild.name, utc_now()),
        )
        self.connection.execute(
            """
            INSERT INTO channels (id, guild_id, parent_id, name, kind)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                guild_id = excluded.guild_id,
                parent_id = excluded.parent_id,
                name = excluded.name,
                kind = excluded.kind
            """,
            (
                channel.id,
                guild.id,
                getattr(channel, "parent_id", None),
                channel.name,
                str(channel.type),
            ),
        )

    def latest_message_id(self, channel_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT latest_message_id FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()
        return row[0] if row else None

    def store_message(self, message):
        reference = getattr(message, "reference", None)
        reference_message_id = getattr(reference, "message_id", None)
        author_name = getattr(message.author, "display_name", None) or getattr(
            message.author, "name", str(message.author.id)
        )
        self.connection.execute(
            """
            INSERT INTO messages (
                id, guild_id, channel_id, author_id, author_name, author_is_bot,
                content, created_at, edited_at, message_type, reference_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                author_name = excluded.author_name,
                content = excluded.content,
                edited_at = excluded.edited_at,
                message_type = excluded.message_type,
                reference_message_id = excluded.reference_message_id
            """,
            (
                message.id,
                message.guild.id,
                message.channel.id,
                message.author.id,
                author_name,
                bool(message.author.bot),
                message.content,
                message.created_at.isoformat(),
                message.edited_at.isoformat() if message.edited_at else None,
                str(message.type),
                reference_message_id,
            ),
        )
        self.connection.execute("DELETE FROM attachments WHERE message_id = ?", (message.id,))
        self.connection.executemany(
            """
            INSERT INTO attachments (id, message_id, filename, url, content_type, size)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    attachment.id,
                    message.id,
                    attachment.filename,
                    attachment.url,
                    attachment.content_type,
                    attachment.size,
                )
                for attachment in message.attachments
            ],
        )

    def finish_channel(self, channel_id: int, latest_message_id: int | None):
        self.connection.execute(
            """
            UPDATE channels
            SET latest_message_id = COALESCE(?, latest_message_id), synced_at = ?
            WHERE id = ?
            """,
            (latest_message_id, utc_now(), channel_id),
        )
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("guilds", "channels", "messages", "attachments")
        }

    def integrity_check(self) -> str:
        return self.connection.execute("PRAGMA integrity_check").fetchone()[0]


async def sync_channel_history(database: HistoryDatabase, channel, full_sync: bool = False) -> ChannelSyncResult:
    """Persist new Discord messages for one channel without retaining them in memory."""
    started_at = perf_counter()
    database.upsert_channel(channel)
    latest_message_id = None if full_sync else database.latest_message_id(channel.id)
    history_options = {"limit": None, "oldest_first": True}

    if latest_message_id is not None:
        import discord

        history_options["after"] = discord.Object(id=latest_message_id)

    messages_seen = 0
    newest_message_id = latest_message_id
    async for message in channel.history(**history_options):
        database.store_message(message)
        messages_seen += 1
        newest_message_id = message.id

    database.finish_channel(channel.id, newest_message_id)
    return ChannelSyncResult(
        channel_id=channel.id,
        channel_name=channel.name,
        messages_seen=messages_seen,
        elapsed_seconds=perf_counter() - started_at,
    )


@dataclass(frozen=True, slots=True)
class ClientSyncResult:
    messages_seen: int
    channels_synced: int
    unavailable_channel_ids: tuple[int, ...]


async def resolve_message_channels(client, channel_ids: Iterable[int]):
    """Resolve an explicit channel allowlist without walking unrelated guild history."""
    channels = []
    unavailable = []
    for channel_id in dict.fromkeys(channel_ids):
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception:
                unavailable.append(channel_id)
                continue
        if not hasattr(channel, "history") or getattr(channel, "guild", None) is None:
            unavailable.append(channel_id)
            continue
        channels.append(channel)
    return channels, unavailable


async def sync_client_history(
    database: HistoryDatabase,
    client,
    channel_ids: Iterable[int],
    full_sync: bool = False,
) -> ClientSyncResult:
    """Incrementally sync only explicitly allowlisted Discord channels."""
    run_id = database.start_sync_run(full_sync)
    messages_seen = 0
    try:
        channels, unavailable = await resolve_message_channels(client, channel_ids)
        if not channels:
            raise RuntimeError("None of the configured RAG channels were accessible")
        for channel in channels:
            result = await sync_channel_history(database, channel, full_sync=full_sync)
            messages_seen += result.messages_seen
        error = None
        if unavailable:
            error = f"Unavailable channel IDs: {','.join(str(channel_id) for channel_id in unavailable)}"
        database.finish_sync_run(run_id, messages_seen, error=error)
        return ClientSyncResult(messages_seen, len(channels), tuple(unavailable))
    except Exception as error:
        database.finish_sync_run(run_id, messages_seen, error=f"{type(error).__name__}: {error}")
        raise
