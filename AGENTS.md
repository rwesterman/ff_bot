# AGENTS.md

## Project overview

This repository runs a Python Discord bot that reports ESPN fantasy-football data. The active entry point is `main.py`:
it creates an `espn_api.football.League`, registers the league commands plus `/ask` and `/rules` with `discord.py`, and
starts the bot using `DISCORD_BOT_TOKEN`.

The README and some supporting files still describe the project's older GroupMe, Slack, webhook, Heroku, and scheduled-message implementations. Treat the code as the source of truth when those descriptions disagree with the current implementation.

## Repository map

- `main.py` — runtime entry point and Discord command handlers. Runtime initialization is behind `create_application()`
  and `main()`; keep imports side-effect free for unit tests and utility scripts.
- `utils/commands.py` — chat-independent fantasy-football calculations and text formatting. Most feature logic belongs here and should be tested with a fake league object.
- `utils/contest.py` — weekly payout ledger for `/weeklycontest`: a cached ESPN snapshot per player-week, the
  side-contest resolvers, tie-splitting, and season totals. Payouts are recorded once and read back from SQLite so
  ESPN stat corrections cannot move money that was already awarded.
- `utils/contest_schedule.py` — the league's fourteen side contests as data, including which ESPN scoring period each
  one reads. Contest week and ESPN week diverge for Thanksgiving, so that mapping is explicit rather than inferred.
- `utils/chat_history.py` — SQLite schema and idempotent, incremental Discord channel-history persistence.
- `utils/chat_rag.py` — allowlisted conversation chunking, OpenAI embeddings, hybrid SQLite retrieval, DeepSeek answers, and Discord source formatting.
- `utils/rules.py` — GitHub-backed Markdown rules synchronization, heading-based indexing, hybrid retrieval, and PDF
  rendering for `/rules`.
- `scripts/sync_discord_history.py` — read-only Discord history importer. It sends no Discord messages and defaults to the ignored `data/chat_history.db` path.
- `scripts/ask_chat_history.py` — local, Discord-free question runner over the cached history database.
- `utils/transaction.py` — formats ESPN recent-activity actions for the waiver table. Trades are not implemented.
- `utils/bots.py` — legacy synchronous GroupMe, Slack, and Discord webhook clients. The active Discord bot does not use these classes.
- `utils/players.py` — experimental Sleeper player/projection helpers; it expects a top-level `players.json`, which is not committed.
- `utils/slack_client.py` — standalone Slack experiment with network activity at import time; it is not part of the active runtime.
- `utils/tests/` — deterministic pytest coverage for the Discord webhook client and command behavior.
- `pyproject.toml` and `uv.lock` — Python 3.13 project metadata, bounded direct dependencies, development tools, and exact resolved versions.
- `Dockerfile` — syncs the frozen uv environment and runs the bot as an unprivileged user on Python 3.13 Alpine.
- `deploy/compose.production.yaml` — single-service production configuration for the DigitalOcean droplet.
- `.github/workflows/deploy.yml` — tests, publishes immutable GHCR images, and deploys them to DigitalOcean over SSH.
- `deploy/README.md` — droplet bootstrap, secrets, GHCR authentication, deployment, and rollback instructions.
- `app.json`, `manifest.yaml`, `Procfile`, and `fly.toml` — legacy deployment metadata retained during the hosting
  migration.

## Setup and common commands

Install [uv](https://docs.astral.sh/uv/), then sync the locked Python 3.13 environment:

```bash
uv sync --frozen
```

Run checks from the repository root:

```bash
uv run --frozen pytest -m "not live"
uv run --frozen pytest -m live
uv run --frozen ruff format --check .
uv run --frozen ruff check .
```

Run the bot only when valid credentials are available:

```bash
LEAGUE_ID=... LEAGUE_YEAR=... DISCORD_BOT_TOKEN=... uv run --frozen python main.py
```

`CONTEST_ADMIN_IDS` accepts a comma-separated list of Discord user IDs allowed to settle weeks and log penalties. When
it is unset every user may write to the payout ledger, which is intended only for local development.

Private ESPN leagues also require `ESPN_S2` and `SWID`. `main.py` adds missing braces around `SWID`. Public leagues use neither value. The webhook variables described in the README (`BOT_ID`, `SLACK_WEBHOOK_URL`, and `DISCORD_WEBHOOK_URL`) belong to legacy clients and are not read by the active entry point.

## Testing expectations

- Add or update focused unit tests for behavior changes. Prefer small fake ESPN objects over live ESPN requests; tests must be deterministic and should not require real league credentials.
- Mock outbound HTTP requests in Discord client tests with `requests_mock`.
- Keep tests on pytest fixtures and plain `assert` statements; do not add unittest-style classes or live ESPN credentials.
- Live ESPN tests are marked `live`, load credentials from the ignored repository-level `.env`, and must never import
  `main.py` or initialize Discord. Run them explicitly with `uv run --frozen pytest -m live`.
- RAG tests must inject fake OpenAI-compatible clients. Do not make paid API calls in the deterministic test suite.
- Test and lint dependencies are declared in the `dev` dependency group and installed by the default `uv sync` command.
- Do not import `main.py` during tests: it connects to ESPN during module initialization and starts a Discord client.

## Implementation conventions

- Keep Discord transport concerns in `main.py` and league/query/formatting logic in `utils/commands.py`.
- Store money as integer cents and split pots with `contest.split_pot`, which distributes the remainder so recorded
  payouts always sum back to the pot. Never render a payout by recomputing it from live ESPN data.
- `/weeklycontest` reads up to fourteen ESPN weeks, so its handlers dispatch through `asyncio.to_thread` and open a
  short-lived SQLite connection per operation rather than sharing one across threads.
- Preserve asynchronous `discord.py` handler behavior: await sends and edits, and keep messages within Discord's size limits. The waiver command currently emits batches of ten table rows for this reason.
- ESPN objects are mutable and network-backed. `Commands.refresh_league()` is cached for one hour with a single-entry `TTLCache`; consider that cache when testing refresh-dependent behavior.
- Match existing four-space indentation in actively maintained files. `utils/bots.py` and `utils/players.py` retain older tab indentation; avoid unrelated formatting churn.
- Keep lines within Ruff's configured limit of 120 characters.
- Do not silently broaden exception handling around ESPN calls. User-facing command failures should log the underlying error and send a concise response to Discord.
- Avoid opportunistic edits to legacy deployment files unless the task is specifically about that deployment path.

## Security and external effects

- Never add real Discord tokens, ESPN cookies, Slack URLs, GroupMe IDs, or league credentials to source, tests, logs, or documentation. Use environment variables and obvious dummy values.
- Historical revisions contained credential-like values. Do not restore or reproduce them; use fixtures and obvious placeholders.
- Running `main.py`, `utils/players.py`, or `utils/slack_client.py` can make external requests; use mocks unless an explicitly requested integration test provides authorized credentials.
- Discord history databases contain private chat content. Keep `data/` ignored, never print stored content during diagnostics, and use a persistent volume rather than an image layer in production.
- Only channel IDs in `RAG_CHANNEL_IDS` may be indexed. OpenAI receives all allowlisted chunks for embedding; DeepSeek receives only retrieved excerpts.
- Before changing production output, verify both the formatted content and platform limits. The bot's responses are user-visible league messages.

## Before handing off changes

1. Run the narrowest relevant tests, then `uv run --frozen pytest -m "not live"`. Run the live suite explicitly when
   ESPN integration behavior is in scope and authorized credentials are available.
2. Run `uv run --frozen ruff format --check .` and `uv run --frozen ruff check .`.
3. Report any pre-existing test failures or checks that could not run; do not claim the legacy suite is green without verifying it.
4. Confirm no secrets or generated artifacts were introduced with `git diff` and `git status --short`.
