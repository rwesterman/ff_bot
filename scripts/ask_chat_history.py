import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from utils.chat_rag import HistoryRagService, format_discord_answer, index_and_ask


def parse_args():
    parser = argparse.ArgumentParser(description="Ask a question against the local Discord history cache.")
    parser.add_argument("question", help="Question to answer from the cached history")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("CHAT_HISTORY_DB", "data/chat_history.db")),
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Validate indexing and answering without printing private answer text",
    )
    return parser.parse_args()


async def run(question: str, database_path: Path, summary_only: bool = False):
    service = HistoryRagService.from_environment(database_path)
    answer = await index_and_ask(service, question)
    if summary_only:
        print(f"Answer generated successfully with {len(answer.evidence)} retrieved source chunks.")
    else:
        print(format_discord_answer(answer))


def main():
    load_dotenv(Path(__file__).parents[1] / ".env", override=False)
    args = parse_args()
    asyncio.run(run(args.question, args.database, summary_only=args.summary_only))


if __name__ == "__main__":
    main()
