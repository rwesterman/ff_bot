FROM python:3.13-alpine

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/
COPY --from=litestream/litestream:0.5.14 /usr/local/bin/litestream /usr/local/bin/litestream

RUN addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app

WORKDIR /app
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY --chown=app:app . .
COPY --chown=app:app litestream.yml /etc/litestream.yml
RUN mkdir -p /data && chown app:app /data

ENV CHAT_HISTORY_DB="/data/chat_history.db" \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

LABEL org.opencontainers.image.source="https://github.com/rwesterman/ff_bot"

USER app
CMD ["sh", "-c", "if [ -n \"$BUCKET_NAME\" ]; then exec litestream replicate -restore-if-db-not-exists; else exec python main.py; fi"]
