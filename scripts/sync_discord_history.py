import argparse
import asyncio
import os
from pathlib import Path
from time import perf_counter

import discord
from dotenv import load_dotenv

from utils.chat_history import HistoryDatabase, sync_channel_history


def parse_args():
    parser = argparse.ArgumentParser(description="Cache accessible Discord history in SQLite.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("CHAT_HISTORY_DB", "data/chat_history.db")),
    )
    parser.add_argument("--full", action="store_true", help="Refetch all messages instead of only newer messages.")
    return parser.parse_args()


async def discover_message_channels(client):
    channels = {}
    errors = []

    for guild in client.guilds:
        member = guild.me
        for channel in [*guild.text_channels, *guild.voice_channels, *guild.stage_channels]:
            permissions = channel.permissions_for(member)
            if permissions.view_channel and permissions.read_message_history:
                channels[channel.id] = channel

        for thread in guild.threads:
            channels[thread.id] = thread

        for parent in [*guild.text_channels, *guild.forums]:
            try:
                async for thread in parent.archived_threads(limit=None):
                    channels[thread.id] = thread
            except (discord.Forbidden, discord.HTTPException) as error:
                errors.append((guild.name, parent.name, type(error).__name__))

            if isinstance(parent, discord.TextChannel):
                try:
                    async for thread in parent.archived_threads(limit=None, private=True, joined=True):
                        channels[thread.id] = thread
                except (discord.Forbidden, discord.HTTPException) as error:
                    errors.append((guild.name, parent.name, type(error).__name__))

    return list(channels.values()), errors


async def run_sync(database_path: Path, full_sync: bool):
    load_dotenv(Path(__file__).parents[1] / ".env", override=False)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required in the environment or repository-level .env")

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    ready_handled = False

    @client.event
    async def on_ready():
        nonlocal ready_handled
        if ready_handled:
            return
        ready_handled = True

        started_at = perf_counter()
        total_messages = 0
        with HistoryDatabase(database_path) as database:
            run_id = database.start_sync_run(full_sync)
            try:
                channels, discovery_errors = await discover_message_channels(client)
                print(f"Discovered {len(channels)} readable message channels and threads.", flush=True)
                if discovery_errors:
                    print(f"History discovery errors: {len(discovery_errors)}", flush=True)

                for channel in channels:
                    result = await sync_channel_history(database, channel, full_sync=full_sync)
                    total_messages += result.messages_seen
                    print(
                        f"{channel.guild.name} / {result.channel_name}: "
                        f"{result.messages_seen:,} messages in {result.elapsed_seconds:.2f}s",
                        flush=True,
                    )

                database.finish_sync_run(run_id, total_messages)
                counts = database.counts()
                integrity = database.integrity_check()
                print(
                    f"Sync complete in {perf_counter() - started_at:.2f}s; "
                    f"fetched={total_messages:,}, stored={counts['messages']:,}, "
                    f"attachments={counts['attachments']:,}, integrity={integrity}",
                    flush=True,
                )
            except Exception as error:
                database.finish_sync_run(run_id, total_messages, error=str(error))
                raise
            finally:
                await client.close()

    await client.start(token)


def main():
    args = parse_args()
    asyncio.run(run_sync(args.database, args.full))


if __name__ == "__main__":
    main()
