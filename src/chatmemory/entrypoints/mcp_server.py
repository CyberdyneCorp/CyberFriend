"""HTTP MCP retrieval interface.

The only service given a public domain. Every content-returning tool requires
an authenticated bearer token, and the token determines which person's view
the caller gets -- the request cannot name a viewer. A leaked token therefore
exposes one person's view rather than the whole corpus.

It also holds a gateway connection, because permissions are resolved at query
time from discord.py's guild cache rather than denormalised into the corpus.
That connection registers no *message* handlers, so it cannot double-ingest
alongside the `ingest` service; it exists only to answer "what may this
person read", and when it is cold every answer is "nothing".

It does register permission handlers, and that distinction is the bug this
file used to have. "Handler-free" was read as "no listeners at all", so the
resolved-viewer cache in front of it was never invalidated and a revoked role
kept reading for a full TTL. Invalidation listeners change nothing about
double-ingestion -- they touch no corpus -- so there was never a reason to
leave them off.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import cast

import discord
import structlog
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import DiscordAclResolver, PermissionCaches, _Guild
from chatmemory.adapters.discord.gateway import (
    CachingAclResolver,
    attach_permission_listeners,
)
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.config import Settings, get_settings
from chatmemory.health import HealthState, ReadinessCheck
from chatmemory.mcp.auth import Authenticator, PostgresTokenStore
from chatmemory.mcp.server import build_app
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()


class AclClient(discord.Client):
    """A gateway connection kept solely to keep permissions current.

    Deliberately free of *message* handlers. `permissions_for` reads
    discord.py's guild cache, and the cache is only populated by an identified
    connection, so this service needs one -- but registering no `on_message`
    is what makes a second connection on the same bot token safe rather than a
    silent doubling of the corpus.

    It does register the permission listeners, and it takes the cache group as
    a constructor argument so that it cannot be built without them: this
    client exists to answer "what may this person read", and one that cannot
    notice that the answer changed is worse than none.
    """

    def __init__(self, caches: PermissionCaches, liveness: GatewayLiveness) -> None:
        intents = discord.Intents.default()
        # Members, because per-member channel overwrites decide readability
        # and a role-set comparison gets exactly those cases wrong. It is also
        # what makes on_member_update arrive at all.
        intents.members = True
        super().__init__(intents=intents)
        self._liveness = liveness
        attach_permission_listeners(self, caches)

    async def on_ready(self) -> None:
        self._liveness.mark_live()

    async def on_disconnect(self) -> None:
        # Resumable blips land here too. The cost of treating one as stale is
        # a few refused queries; the cost of not is serving a revoked
        # permission from a cache that stopped updating.
        self._liveness.mark_dead("disconnected")


@dataclass(frozen=True, slots=True)
class AclGraph:
    """The MCP server's permission path, assembled as one piece.

    The connection, the caches it invalidates and the resolver reading through
    them are returned together because they are only correct together; the
    defect this replaces was precisely these three drifting apart in `main`.
    """

    client: AclClient
    caches: PermissionCaches
    resolver: CachingAclResolver
    liveness: GatewayLiveness


class GatewayLiveness:
    """Whether the permission source is currently authoritative.

    discord.py keeps its guild cache after the connection dies, so
    `get_guild` goes on returning a fully populated guild whose permissions
    stopped updating at the moment of the failure. Reading it is therefore
    not the fail-closed direction it looks like: a role revoked after the
    gateway died would be served as still granted, indefinitely.
    """

    def __init__(self) -> None:
        self._live = False

    @property
    def live(self) -> bool:
        return self._live

    def mark_live(self) -> None:
        self._live = True

    def mark_dead(self, reason: str) -> None:
        if self._live:
            log.error("mcp.permissions_stale", reason=reason)
        self._live = False


def build_acl(settings: Settings) -> AclGraph:
    """The gateway connection and the viewer cache that connection invalidates."""
    caches = PermissionCaches()
    liveness = GatewayLiveness()
    client = AclClient(caches, liveness)

    def guild() -> _Guild | None:
        if not liveness.live:
            # Refuse the stale cache rather than serving it. Every viewer
            # resolves to an empty channel set, which is the direction this
            # is allowed to fail in.
            return None
        found = client.get_guild(settings.discord_guild_id)
        # discord.Guild satisfies `_Guild` in practice but not structurally --
        # `permissions_for` takes Member | Role rather than any `_Member`. The
        # cast is the adapter boundary; see bot.py for the same note.
        return None if found is None else cast(_Guild, found)

    resolver = CachingAclResolver(
        DiscordAclResolver(guild, settings.indexed_channel_ids), invalidation=caches
    )
    return AclGraph(client=client, caches=caches, resolver=resolver, liveness=liveness)


def allowed_hosts() -> list[str]:
    """Host-header allowlist, if the operator set one.

    Read from the environment rather than `Settings` because host validation
    is a property of how this one service is published, not of the corpus.
    Unset means the check is off; see `mcp.server._transport_security`.
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.replace(",", " ").split() if h.strip()]


def build_readiness(
    engine: AsyncEngine, liveness: GatewayLiveness, state: HealthState
) -> ReadinessCheck:
    """Ready means: the corpus is reachable and permissions are resolvable.

    Both matter, and for opposite reasons. Without the database there are no
    answers; without the guild cache there are answers that are silently
    empty, which is the worse failure because it looks like a quiet server.
    """

    async def ready() -> bool:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            database_ok = True
        except Exception as exc:  # noqa: BLE001 - reported, never raised at a probe
            log.warning("mcp.database_unreachable", error=type(exc).__name__)
            database_ok = False

        # Deliberately NOT `bool(client.guilds)`: discord.py retains its
        # guild cache after the connection dies, so that stays true forever
        # and the probe reported healthy while permissions were frozen.
        guild_ok = liveness.live
        state.gateway_connected = guild_ok
        state.details["database_reachable"] = database_ok
        return database_ok and guild_ok

    return ready


def _search_backend(settings: Settings, engine: AsyncEngine) -> SearchBackend:
    embeddings = OpenAICompatibleEmbeddings(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    return HybridSearch(engine, embeddings)


GATEWAY_RETRY_SECONDS = 5.0
GATEWAY_RETRY_MAX = 300.0


async def _run_gateway(
    client: discord.Client, token: str, liveness: GatewayLiveness
) -> None:
    """Keep the permission source current, and reconnect when it fails.

    Returning after one failure left the endpoint serving queries against a
    permanently frozen permission snapshot while reporting itself healthy.
    Crashing instead would turn a Discord blip into an outage, so it retries
    with backoff -- and marks permissions stale for as long as it is down, so
    resolution fails closed rather than answering from the retained cache.
    """
    delay = GATEWAY_RETRY_SECONDS
    while True:
        try:
            await client.start(token)
            liveness.mark_dead("gateway returned")
        except Exception as exc:  # noqa: BLE001 - any failure means stale
            liveness.mark_dead(type(exc).__name__)
            log.exception("mcp.gateway_failed", retry_in=delay)
        if not client.is_closed():
            await client.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, GATEWAY_RETRY_MAX)


async def main() -> None:
    log_setup.configure()
    settings = get_settings()

    # No schema creation here: `mcp_token` is migration 0005's, and a service
    # that creates its own tables hides an unmigrated database until the first
    # query against a table it did not think to create.
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)

    graph = build_acl(settings)
    client = graph.client
    authenticator = Authenticator(tokens=PostgresTokenStore(engine), acl=graph.resolver)

    state = HealthState()
    app = build_app(
        search=_search_backend(settings, engine),
        authenticator=authenticator,
        guild_id=settings.discord_guild_id,
        state=state,
        readiness=build_readiness(engine, graph.liveness, state),
        allowed_hosts=allowed_hosts(),
    )

    log.info(
        "mcp.starting",
        port=settings.mcp_port,
        guild_id=settings.discord_guild_id,
        indexed_channels=len(settings.indexed_channel_ids),
    )
    if not settings.indexed_channel_ids:
        # Every tool answers emptily in this state, which looks identical to
        # "nobody has said anything".
        log.warning("mcp.no_indexed_channels", hint="set INDEXED_CHANNEL_IDS")

    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.mcp_port, log_level="warning"
    )
    await asyncio.gather(
        _run_gateway(client, settings.discord_token.get_secret_value(), graph.liveness),
        uvicorn.Server(config).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
