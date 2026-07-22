FROM python:3.13-alpine

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/
COPY --from=litestream/litestream:0.5.14 /usr/local/bin/litestream /usr/local/bin/litestream

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
COPY litestream.yml /etc/litestream.yml

ENV PATH="/app/.venv/bin:$PATH"

CMD ["sh", "-c", "if [ -n \"$BUCKET_NAME\" ]; then exec litestream replicate -restore-if-db-not-exists; else exec python main.py; fi"]
