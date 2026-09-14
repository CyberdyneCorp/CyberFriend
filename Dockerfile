FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so application edits do not invalidate the layer.
# The stub package exists only so the wheel can build at this stage; the real
# source lands below and is reinstalled over it.
COPY pyproject.toml README.md ./
RUN mkdir -p src/chatmemory \
 && touch src/chatmemory/__init__.py \
 && uv pip install --system --no-cache .

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN uv pip install --system --no-cache --no-deps .

# No secrets are baked in; all configuration arrives as runtime environment.
RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "chatmemory.entrypoints.ingest"]
