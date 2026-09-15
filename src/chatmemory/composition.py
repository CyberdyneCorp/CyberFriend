"""The composition root: one place where the object graph is assembled.

Every other module takes its collaborators as arguments and knows nothing
about how they were built. This one knows `Settings`, imports the concrete
adapters, and hands the result to the entrypoints -- so "which model answers
questions" and "which database holds the corpus" are decisions made once,
here, rather than discovered at a callsite.

It also fails the process rather than a request. Two things are verified
before anything is served:

*   **What the model can do.** An OpenAI-compatible URL may front anything,
    and structured output is where serving stacks differ most. The enabled
    stages declare what they need and the constructor refuses to build a
    handle that cannot supply it, naming the capability.
*   **How wide its embeddings are.** The schema pins the vector width, so a
    model returning a different one fails at insert with an opaque error far
    from the cause. Asking it once at boot turns that into a message naming
    both widths.

What this file deliberately does *not* decide is retrieval scope. The
reasoning service derives its viewer from each question's audience through
`reasoning.scope`; there is no parameter here through which a wider view
could be composed in.

Federation is the one part of the graph that is allowed to be missing. A
deployment with no external servers configured gets exactly the object graph
it had before federation existed, and a server that is misconfigured,
unreachable, or missing a listed tool degrades the outbound surface rather
than the process: answering questions about Discord must not depend on
somebody else's uptime. Every one of those outcomes is logged at startup,
because the failure this project keeps having is not a crash -- it is a
capability that was built, tested, and silently never reached.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.discord.acl import (
    DiscordAclResolver,
    DiscordAudienceResolver,
    GuildProvider,
)
from chatmemory.adapters.llm.chat import OpenAICompatibleChat
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.mcp_client import (
    AllowedTool,
    Federation,
    FederationConfig,
    FederationStartupError,
    GuardedInvoker,
    Registration,
    ServerConfig,
    ToolRouter,
    connect,
)
from chatmemory.adapters.mcp_client.client import SessionFactory
from chatmemory.adapters.mcp_client.config import (
    ConfigurationError as FederationConfigurationError,
)
from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.app.ask import AskService
from chatmemory.app.audit import AuditTrail, InMemoryAuditTrail
from chatmemory.app.authorization import (
    Authorizer,
    ConfirmationLedger,
    CredentialBroker,
    ToolEffect,
)
from chatmemory.app.conversation import ConversationStore
from chatmemory.app.limits import RateLimiter
from chatmemory.app.reasoning.capabilities import LOOP_STAGES, ModelCapability, Stage
from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.ports import ChatModel, ExternalTool, RetrievalTool, ToolSurface
from chatmemory.app.reasoning.retrieval import (
    CorpusRetrieval,
    WithheldRetrieval,
    discord_urls,
)
from chatmemory.app.reasoning.service import ReasoningAnswerService, build_answer_service
from chatmemory.config import Settings
from chatmemory.ports.answers import AnswerService
from chatmemory.ports.sources import EmbeddingClient
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()

ANSWERING_STAGES: tuple[Stage, ...] = tuple(sorted(LOOP_STAGES))
"""Every stage the bot can reach.

The loop's stages, not the fixed path's: routing is lexical and decides at
request time, so a model that cannot plan would fail on the first question
that happens to route to the loop. Checking the union means that question
cannot arrive at a process that started.
"""

EMBEDDING_PROBE = "capability probe"


@dataclass(frozen=True, slots=True)
class AnswerStack:
    """The assembled answer path, and the resources it holds open.

    The engine is carried so the caller that built it can dispose of it;
    nothing else in the graph owns a connection pool.

    `federation` is None whenever this deployment has no working outbound
    surface -- none configured, or none that came up. It is carried rather
    than hidden inside the answer service so an entrypoint can close its
    sessions and so a health surface can say whether external tools are
    actually there.
    """

    engine: AsyncEngine
    search: SearchBackend
    chat: ChatModel
    answers: ReasoningAnswerService
    federation: FederatedTools | None = None


def declared_capabilities(settings: Settings) -> frozenset[ModelCapability] | None:
    """What the deployment says its endpoint provides, or None for the default.

    None is not "anything": the adapter falls back to what is known about the
    model name, and an unrecognised name is assumed to do chat and nothing
    else. A typo here is a boot failure listing the accepted values rather
    than a silently narrower set of capabilities.
    """
    if not settings.chat_model_capabilities:
        return None
    known = {c.value for c in ModelCapability}
    unknown = sorted(settings.chat_model_capabilities - known)
    if unknown:
        raise ConfigurationError(
            f"unknown model capability {', '.join(repr(u) for u in unknown)}; "
            f"CHAT_MODEL_CAPABILITIES accepts {', '.join(sorted(known))}"
        )
    return frozenset(ModelCapability(c) for c in settings.chat_model_capabilities)


def build_chat_model(settings: Settings) -> OpenAICompatibleChat:
    """The answering handle. Raises `MissingCapabilityError` if it cannot serve."""
    return OpenAICompatibleChat(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.chat_model,
        stages=ANSWERING_STAGES,
        provides=declared_capabilities(settings),
        scoring_model=settings.extraction_model,
    )


def build_embeddings(settings: Settings) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


async def verify_embedding_width(
    embeddings: EmbeddingClient, model: str, expected: int
) -> None:
    """Ask the endpoint once, at boot, how wide its vectors are.

    The adapter checks every batch it receives, but the first batch is
    request one in production -- and by then the failure is an insert into a
    `vector(n)` column, which reports a type error rather than a model
    mismatch. Changing the embedding model is a reindex, not a swap, so the
    right place to find out is before anything is served.
    """
    try:
        vectors = await embeddings.embed([EMBEDDING_PROBE])
    except ValueError as exc:
        # The adapter's own width check, surfaced as the deployment failure
        # it is. Transport errors are deliberately not caught: an unreachable
        # endpoint is not a misconfigured one, and must not be reported as one.
        raise ConfigurationError(f"embedding model {model!r}: {exc}") from exc

    if not vectors:
        raise ConfigurationError(
            f"embedding model {model!r} returned no vector for a probe; "
            "the endpoint cannot be used for retrieval"
        )
    width = len(vectors[0])
    if width != expected:
        raise ConfigurationError(
            f"embedding model {model!r} returns {width} dimensions, but the schema "
            f"pins {expected}; changing the model requires a reindex"
        )


READ_ONLY_SUFFIX = "ro"
"""The only word an allowlist entry may add after `server:tool`.

A closed vocabulary of exactly one word, for the same reason the federation
config has no expression language: "declare this tool read-only" must be the
only thing an environment variable can say about a tool's effect, so an
operator cannot widen anything by writing something clever.
"""


class RoutedToolSurface:
    """The reasoning layer's `ToolSurface`, backed by the federation router.

    It lives in the composition root because this is the one module allowed
    to know both sides. The loop sees a port that answers a question with
    names; the router sees a registration the loop has no reference to. The
    only federated type that crosses into the app layer is `ExternalTool`,
    which carries no session, no permit and no route to a server.
    """

    def __init__(self, router: ToolRouter, registration: Registration) -> None:
        self._router = router
        self._registration = registration

    def offer(self, question: str) -> tuple[ExternalTool, ...]:
        routed = self._router.route(question, self._registration)
        return tuple(
            ExternalTool(
                qualified_name=tool.qualified_name,
                server=tool.server,
                description=tool.description,
            )
            for tool in routed.tools
        )


@dataclass(frozen=True, slots=True)
class FederatedTools:
    """The outbound surface a deployment runs with, or nothing at all.

    Held as one value because the four are one arrangement: the router offers
    from `federation`'s registration, and `invoker` re-checks every call
    against the same registration's permits. Splitting them is how an offer
    ends up authorising an invocation.
    """

    federation: Federation
    surface: RoutedToolSurface
    invoker: GuardedInvoker
    audit: AuditTrail
    confirmations: ConfirmationLedger


def parse_server(spec: str) -> ServerConfig:
    """Read one `name=target` entry."""
    name, separator, target = spec.partition("=")
    if not separator:
        raise FederationConfigurationError(
            f"federation server {spec!r} must be written name=target"
        )
    return ServerConfig(name=name.strip(), target=target.strip())


def parse_allowed_tool(spec: str) -> AllowedTool:
    """Read one `server:tool` entry, optionally declared read-only.

    An entry without the suffix leaves the effect undetermined, which counts
    as mutating -- so the quiet outcome of a typo is a tool that needs a
    confirmation, never one that writes unasked.
    """
    server, _, rest = spec.partition(":")
    tool, _, suffix = rest.partition(":")
    if not server or not tool:
        raise FederationConfigurationError(
            f"federation tool {spec!r} must be written server:tool[:{READ_ONLY_SUFFIX}]"
        )
    if suffix and suffix != READ_ONLY_SUFFIX:
        raise FederationConfigurationError(
            f"federation tool {spec!r}: {suffix!r} is not a known declaration; "
            f"the only suffix is {READ_ONLY_SUFFIX!r}"
        )
    return AllowedTool(
        server=server,
        tool=tool,
        effect=ToolEffect.READ_ONLY if suffix == READ_ONLY_SUFFIX else None,
    )


def build_federation_config(settings: Settings) -> FederationConfig | None:
    """The outbound surface an operator asked for, or None for "no federation".

    Raises `FederationConfigurationError` for anything it cannot honour --
    a malformed entry, a tool naming a server that is not configured. The
    caller decides what that costs; here it is simply not silently dropped.
    """
    if not settings.federation_servers:
        return None
    return FederationConfig(
        servers=tuple(parse_server(s) for s in settings.federation_servers),
        allowlist=tuple(parse_allowed_tool(t) for t in settings.federation_tool_allowlist),
        max_tools_per_run=settings.federation_max_tools_per_run,
    )


def report_federation(config: FederationConfig, registration: Registration) -> None:
    """Say, at startup, exactly what is callable and what is not.

    An operator must be able to tell a silently empty registry from a working
    one without reading the code, so the registered tool names are logged by
    name and an empty registry is a warning rather than an absence of output.
    """
    log.info(
        "composition.federation.registered",
        servers=list(config.server_names),
        tools=sorted(registration.names),
        unreachable=list(registration.unreachable_servers),
        unavailable=list(registration.unavailable_tools),
        max_tools_per_run=config.max_tools_per_run,
    )
    if registration.unreachable_servers:
        log.warning(
            "composition.federation.degraded",
            unreachable=list(registration.unreachable_servers),
            unavailable=list(registration.unavailable_tools),
        )
    if not registration.tools:
        log.warning(
            "composition.federation.no_tools_registered",
            servers=list(config.server_names),
            allowlisted=len(config.allowlist),
            hint="FEDERATION_TOOL_ALLOWLIST names no tool a reachable server provides",
        )


async def build_federation(
    settings: Settings, factory: SessionFactory | None = None
) -> FederatedTools | None:
    """Connect to the configured servers, or run without any.

    Every failure degrades to None. That is a deliberate asymmetry with the
    model and embedding checks above, which refuse the deployment: those
    decide whether the bot can answer at all, while federation only widens
    where answers may come from. A team whose issue tracker is down still
    gets answers about Discord.

    Degrading is not the same as going quiet. A missing tool, an unreachable
    server and a malformed variable each produce a log line naming what is
    wrong, at error level when an operator's configuration is not being
    honoured.
    """
    try:
        config = build_federation_config(settings)
    except FederationConfigurationError as exc:
        log.error("composition.federation.misconfigured", error=str(exc))
        return None

    if config is None:
        log.info("composition.federation.disabled", reason="no servers configured")
        return None

    try:
        federation = await connect(config, factory)
    except FederationStartupError as exc:
        # The spec's own rule: a listed tool no server provides is an error,
        # not a quiet reduction in capability. It is reported as one -- and
        # the whole outbound surface is dropped rather than half-honoured,
        # because a registry missing the tool an operator asked for is not
        # the configuration they wrote.
        log.error("composition.federation.startup_failed", error=str(exc))
        return None
    except Exception as exc:
        # Any transport failure connect() did not already absorb per-server.
        log.error("composition.federation.unavailable", error=str(exc))
        return None

    report_federation(config, federation.registration)
    return _federated_tools(config, federation)


def _federated_tools(config: FederationConfig, federation: Federation) -> FederatedTools:
    """Assemble the guarded door around a connected federation.

    The credential broker starts empty on purpose: a per-requester tool is
    refused until somebody's credential is actually registered, which fails
    closed rather than falling back to the bot's own identity and handing
    every server member whatever it holds.
    """
    confirmations = ConfirmationLedger()
    audit = InMemoryAuditTrail()
    authorizer = Authorizer(federation.permits, confirmations, CredentialBroker())
    return FederatedTools(
        federation=federation,
        surface=RoutedToolSurface(
            ToolRouter(config.max_tools_per_run), federation.registration
        ),
        invoker=GuardedInvoker(federation, authorizer, audit, confirmations),
        audit=audit,
        confirmations=confirmations,
    )


def build_answers(
    retrieval: RetrievalTool, chat: ChatModel, tools: ToolSurface | None = None
) -> ReasoningAnswerService:
    """The real answer service: both paths, over one retrieval tool."""
    return build_answer_service(retrieval, chat, tools=tools)


async def build_answer_stack(settings: Settings) -> AnswerStack:
    """Assemble everything between the corpus and an answer.

    Ordering is the point. The two checks that can refuse the deployment run
    before the graph is returned, so a process that reaches its gateway
    connection is one whose model and corpus agree with its configuration.
    """
    chat = build_chat_model(settings)

    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    embeddings = build_embeddings(settings)
    await verify_embedding_width(
        embeddings, settings.embedding_model, settings.embedding_dimensions
    )
    search = HybridSearch(engine, embeddings)

    retrieval = CorpusRetrieval(search, discord_urls(settings.discord_guild_id))
    # After the two checks that can refuse the deployment: a federated server
    # is reached over the network, and a slow handshake must not sit in front
    # of the failures that stop the process.
    federation = await build_federation(settings)
    log.info(
        "composition.answer_stack",
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        stages=[str(s) for s in ANSWERING_STAGES],
        federated_tools=sorted(federation.federation.registration.names) if federation else [],
    )
    return AnswerStack(
        engine=engine,
        search=search,
        chat=chat,
        answers=build_answers(
            retrieval, chat, tools=federation.surface if federation else None
        ),
        federation=federation,
    )


def build_ask_service(
    settings: Settings,
    guild: GuildProvider,
    answers: AnswerService,
    search: SearchBackend | None = None,
) -> AskService:
    """The Discord-facing use case, over whichever answer service it is given.

    `guild` is late-bound because permissions are resolved from live guild
    state that does not exist until the gateway connects; a cold cache reads
    as an empty guild, so resolution fails closed during startup.
    """
    indexed = settings.indexed_channel_ids
    return AskService(
        acl=DiscordAclResolver(guild, indexed),
        audiences=DiscordAudienceResolver(guild, indexed),
        answers=answers,
        limiter=RateLimiter(),
        conversations=ConversationStore(),
        # Without this the withheld-evidence notice is built, tested, and
        # structurally unable to fire: retrieval is pre-scoped to
        # asker INTERSECT audience, so nothing is ever dropped later for the
        # notice to report. The probe is what actually searches the gap.
        withheld=WithheldRetrieval(search) if search is not None else None,
    )
