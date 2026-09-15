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
# --reinstall-package is load-bearing. The project version does not change
# between the stub install above and this one, so without it uv considers
# chatmemory already satisfied and skips the install -- leaving the empty
# stub in site-packages and shipping an image whose package contains
# nothing but __init__.py.
RUN uv pip install --system --no-cache --no-deps --reinstall-package chatmemory .

# Fail the build rather than the deployment. The stub is indistinguishable
# from the real package until something imports a submodule, which first
# happens in a container that then crash-loops with an error that reads like
# an application fault.
RUN python -c "import chatmemory.entrypoints.ingest, chatmemory.entrypoints.bot, \
    chatmemory.entrypoints.mcp_server, chatmemory.entrypoints.migrate" \
 && python -c "import chatmemory.composition, chatmemory.adapters.store.postgres"

# No secrets are baked in; all configuration arrives as runtime environment.
RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "chatmemory.entrypoints.ingest"]
