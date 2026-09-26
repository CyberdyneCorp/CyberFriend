# The operator console is Svelte + TypeScript and has to be compiled before the Python
# image can serve it. Building it in its own stage keeps node out of the
# runtime image entirely -- the service serves static files and has no reason
# to carry a toolchain that can execute code.
FROM node:22-slim AS console

WORKDIR /console
# Lockfile first, so an application edit does not reinstall the world.
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
# `build` runs svelte-check first, so a type error fails the image rather than
# shipping a console that breaks in somebody's browser.
RUN npm run build


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
# The compiled console, at the path ADMIN_CONSOLE_DIR names. Without this the
# admin service starts, finds nothing to serve, and the API works while the
# interface 404s -- which looks like a routing bug rather than a missing build.
COPY --from=console /console/dist ./console
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
    chatmemory.entrypoints.mcp_server, chatmemory.entrypoints.migrate, \
    chatmemory.entrypoints.admin" \
 && python -c "import chatmemory.composition, chatmemory.adapters.store.postgres"

# No secrets are baked in; all configuration arrives as runtime environment.
RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "chatmemory.entrypoints.ingest"]
