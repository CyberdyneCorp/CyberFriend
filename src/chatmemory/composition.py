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

Obligation questions are the one kind that leaves before retrieval. "What
did people ask me today" is a filter over extracted `ask` rows by addressee
and time, so the answer service handed to the bot is a thin front door over
the reasoning one: it claims those questions, answers them from records with
no model call and no similarity search, and passes everything else through
untouched. The ingest process gets the other half of the same feature from
`build_ask_pipeline`, which is the only place the extraction model is named.

Federation is the one part of the graph that is allowed to be missing. A
deployment with no external servers configured gets exactly the object graph
it had before federation existed, and a server that is misconfigured,
unreachable, or missing a listed tool degrades the outbound surface rather
than the process: answering questions about Discord must not depend on
somebody else's uptime. Every one of those outcomes is logged at startup,
because the failure this project keeps having is not a crash -- it is a
capability that was built, tested, and silently never reached.

Conversation memory is assembled here too, over the engine the answer stack
already holds: `build_conversations` for the bot, which recalls, remembers and
summarises, and `build_memory_retention` for the ingest process's sweep. The
summariser runs on the extraction model, because it runs after every few
questions and nobody is waiting on it.

Indexing scope -- which channels exist to be read at all, as opposed to which
of them a viewer may read -- is live. `build_live_scope` is the one
construction every gateway-holding process uses, and the resolvers built here
are handed the provider rather than a set, so they ask it on every resolution.
The refresh loop belongs to the entrypoint, because a loop is a property of a
running process and this module only builds objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.chain.alert_targets import ChainTargets
from chatmemory.adapters.chain.registration import ChainToolsConfig, build_chain_tools
from chatmemory.adapters.chain.watch import ChainWatcher
from chatmemory.adapters.discord.acl import (
    DiscordAclResolver,
    DiscordAudienceResolver,
    GuildProvider,
)
from chatmemory.adapters.discord.profile import DiscordProfileResolver
from chatmemory.adapters.documents.http import BoundedHttpFetcher
from chatmemory.adapters.llm.asks_extraction import (
    ExtractorConfig,
    OpenAICompatibleAskExtractor,
    UsageMeter,
)
from chatmemory.adapters.llm.chat import OpenAICompatibleChat
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.llm.transcription import OpenAICompatibleTranscriber
from chatmemory.adapters.market.alert_prices import AlertPrices
from chatmemory.adapters.market.coingecko import CoinGeckoProvider
from chatmemory.adapters.market.registration import (
    MarketToolsConfig,
    build_market_tools,
    build_usd_rates,
)
from chatmemory.adapters.market.usd_rates import UsdReferenceRates
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
from chatmemory.adapters.mcp_client.invoker import InvocationOutcome
from chatmemory.adapters.store.alerts_postgres import PostgresAlertStore
from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.decisions_postgres import PostgresDecisionStore
from chatmemory.adapters.store.facts_postgres import PostgresFactStore
from chatmemory.adapters.store.media_postgres import PostgresVoiceLedger
from chatmemory.adapters.store.memory_postgres import PostgresMemoryStore
from chatmemory.adapters.store.notify_postgres import PostgresNotificationQueue
from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.schedules_postgres import PostgresScheduleStore
from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import (
    LangfuseTraceDeleter,
    LangfuseTraceFinder,
    LangfuseTracer,
)
from chatmemory.adapters.web.limits import CallBudget
from chatmemory.adapters.web.query import ARG_QUERY, web_arguments
from chatmemory.adapters.web.registration import WebToolsConfig, build_web_tools
from chatmemory.adapters.web.results import source_system_for
from chatmemory.app.alert_requests import AlertRequests
from chatmemory.app.alerts import AlertRunner, AlertService
from chatmemory.app.ask import AskService
from chatmemory.app.asks.answering import ObligationAnswerService
from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskPolicy
from chatmemory.app.asks.obligations import ObligationService, discord_message_url
from chatmemory.app.asks.ports import AskExtractor
from chatmemory.app.asks.resolution import ObservedDirectory
from chatmemory.app.asks.state import AskStateService
from chatmemory.app.asks.worker import ExtractionWorker
from chatmemory.app.audit import AuditTrail, InMemoryAuditTrail
from chatmemory.app.authorization import (
    Authorizer,
    ConfirmationLedger,
    CredentialBroker,
    CredentialScope,
    InvocationRequest,
    ToolEffect,
)
from chatmemory.app.catchup import CatchUpService
from chatmemory.app.channel_listing import ChannelListingService
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.confirmation import ConfirmationDesk, with_confirmation
from chatmemory.app.conversation import (
    Conversations,
    ConversationSummariser,
    MemoryPolicy,
    MemoryRetention,
)
from chatmemory.app.currency import PreferredCurrencies
from chatmemory.app.decisions.answering import DecisionAnswerService
from chatmemory.app.decisions.model import DecisionPolicy
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.limits import RateLimiter
from chatmemory.app.notifications import (
    NotificationDelivery,
    NotificationPolicy,
    NotificationPreferences,
    ObligationNotifier,
)
from chatmemory.app.reasoning.capabilities import (
    LOOP_STAGES,
    MissingCapabilityError,
    ModelCapability,
    Stage,
)
from chatmemory.app.reasoning.contract import RunTracer
from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.evidence import SOURCE_WEB
from chatmemory.app.reasoning.loop import FederatedSurface, ToolOutcome
from chatmemory.app.reasoning.ports import (
    ChatModel,
    ExternalTool,
    RetrievalTool,
    ToolCapableChat,
    ToolCompletion,
    ToolDefinition,
    ToolSurface,
)
from chatmemory.app.reasoning.retrieval import (
    CorpusRetrieval,
    WithheldRetrieval,
    discord_urls,
)
from chatmemory.app.reasoning.service import ReasoningAnswerService, build_answer_service
from chatmemory.app.reasoning.stages import ModelSynthesizer, ModelToolProposer
from chatmemory.app.reasoning.tracing import OptOutAwareTracer, TraceWithdrawal
from chatmemory.app.said_by import SaidByService
from chatmemory.app.schedules import (
    ScheduledTaskRunner,
    ScheduleService,
    TaskMessenger,
)
from chatmemory.app.scope import LiveScope, ScopeProvider, StaticScope
from chatmemory.app.self_description import (
    ALERTS,
    ALWAYS_AVAILABLE,
    NOTIFICATIONS,
    SCHEDULED,
    Capabilities,
    Command,
    SelfDescriptionAnswerService,
)
from chatmemory.app.voice import VoiceLimits, VoiceQuestions
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.answers import AnswerService
from chatmemory.ports.notifications import NotificationSender
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
    #: What the bot asks a question of. An `AnswerService` rather than the
    #: reasoning service itself, because obligation questions are answered
    #: from `ask` rows and never reach retrieval at all; `reasoning` below is
    #: the same service seen without that front door, for anything that needs
    #: the run record.
    answers: AnswerService
    reasoning: ReasoningAnswerService
    obligations: ObligationService
    federation: FederatedTools | None = None
    #: What this deployment says it can do. Carried so the Discord surface
    #: describes the same configuration for a bare mention as `answers` does
    #: for "what can you do?".
    capabilities: Capabilities = Capabilities()


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
    declared = frozenset(ModelCapability(c) for c in settings.chat_model_capabilities)
    # The declaration still NARROWS -- an operator whose endpoint cannot do
    # structured output has to be able to say so, and is the only one who
    # knows. Only `chat` is added back, because it is the one capability a
    # model configured as the chat model has by definition, and leaving it
    # out is always a mistake rather than a statement.
    #
    # Without this, enabling tool calling silently withdrew `chat` and the
    # punishment arrived three stages later as "gpt-4o is missing required
    # capability: chat" -- a crash loop caused by adding one word to one
    # environment variable, which is how this deployment broke.
    return declared | {ModelCapability.CHAT}


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
"""Declares a tool read-only: two letters, because it grants nothing."""

MUTATION_SUFFIX = "enable-mutation"
"""Declares a tool state-changing AND enables it. Spelled out, deliberately.

The asymmetry with `ro` is the point. Read-only is the default posture and
costs two letters; handing the agent the ability to change something outside
CyberFriend costs a whole phrase nobody types by accident, and a second,
separate statement of who may spend it (`FEDERATION_CREDENTIAL_HOLDERS`).
Neither declaration means anything without the other, so a single fat-
fingered variable cannot produce a tool that writes.

A misspelling is a startup error rather than a silent downgrade -- but the
downgrade is the safe direction anyway: without this word a tool's effect is
undetermined, which behaves as mutating and is refused for want of an enable.
"""


@dataclass(frozen=True, slots=True)
class _Declaration:
    """What one suffix means, as the three facts a permit is built from.

    Kept together because they are only coherent together: mutation is
    enabled per-requester or not at all -- the bot's own narrow identity must
    never be the authority a write carries -- and `AllowedTool` refuses the
    incoherent combinations at construction.
    """

    effect: ToolEffect | None
    credential: CredentialScope
    mutation_enabled: bool


_DECLARATIONS: dict[str, _Declaration] = {}
"""The complete vocabulary an allowlist entry may use, filled in below.

A closed set, for the same reason the federation config has no expression
language: what an environment variable can say about a tool must be one of a
few fixed sentences, so an operator cannot widen anything by writing
something clever.
"""

#: No suffix. Undetermined effect, which behaves as mutating -- and with no
#: enable, so the quiet outcome of a typo is a tool that is refused, never
#: one that writes unasked.
UNDECLARED = _Declaration(None, CredentialScope.NARROW_READ_ONLY, False)
_DECLARATIONS[READ_ONLY_SUFFIX] = _Declaration(
    ToolEffect.READ_ONLY, CredentialScope.NARROW_READ_ONLY, False
)
#: The operator's declaration is the ONLY thing that may mark a tool
#: read-only; here they declare the opposite, and the registry keeps that
#: even if the server advertises `readOnlyHint`.
_DECLARATIONS[MUTATION_SUFFIX] = _Declaration(
    ToolEffect.MUTATING, CredentialScope.PER_REQUESTER, True
)

CREDENTIAL_HOLDER_SEPARATOR = "="
"""`server=platform:user_id` -- who may spend a mutating tool on that server."""


class RoutedToolSurface:
    """The reasoning layer's federated surface, backed by the router and the guard.

    It lives in the composition root because this is the one module allowed
    to know both sides. The loop sees a port that answers a question with
    names, asks a model for a call, and hands that call back; the router,
    the permits and the sessions stay on this side. The only federated types
    that cross into the app layer are `ExternalTool` and `ToolOutcome`, and
    neither carries a session, a permit or a route to a server.

    Offering and invoking are the same object on purpose. The invoke-time
    check asks whether this run was offered the tool being called, and it
    answers that by routing the *question* again -- the same deterministic
    routing that produced the offer. The loop never holds the offer set that
    decides its own call.
    """

    def __init__(
        self,
        router: ToolRouter,
        registration: Registration,
        invoker: GuardedInvoker,
        proposer: ModelToolProposer | None = None,
    ) -> None:
        self._router = router
        self._registration = registration
        self._invoker = invoker
        # None when the deployed model cannot call tools. The run is still
        # offered them -- an operator reading the record must see that the
        # federation is there -- and simply never asks for one.
        self._proposer = proposer

    def offer(self, question: str) -> tuple[ExternalTool, ...]:
        routed = self._router.route(question, self._registration)
        return tuple(
            ExternalTool(
                qualified_name=tool.qualified_name,
                server=tool.server,
                description=tool.description,
                # Carried, because a name without an argument shape is a tool
                # the model can only guess at, and a guessed call is one the
                # egress guard refuses.
                input_schema=tool.input_schema,
            )
            for tool in routed.tools
        )

    async def propose(
        self, question: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        if self._proposer is None:
            return ToolCompletion()
        return await self._proposer.propose(question, tools)

    async def invoke(self, request: InvocationRequest) -> ToolOutcome:
        """Authorize, call, audit -- through the one door, never around it.

        The routed set is recomputed here from the question on the request.
        Routing is lexical and deterministic, so this is the same set the run
        was offered; taking it from the caller instead would let the thing
        being checked supply the check.
        """
        routed = self._router.route(request.question, self._registration)
        call = _with_call_site_arguments(request)
        # A mutating call comes back refused, carrying a prompt for the person
        # who asked. Putting that prompt to them -- and then retrying the same
        # guarded door rather than telling it the answer, so the gate re-checks
        # the digest of the arguments actually about to be sent -- is the whole
        # of what makes an enabled mutating tool reachable. Without this, the
        # prompt is built here and dropped, which reads exactly like a working
        # feature in any test that drives the desk itself.
        outcome = await with_confirmation(
            lambda: self._invoker.invoke(call, routed), lambda pass_: pass_.prompt
        )
        if not outcome.invoked or outcome.result is None:
            return ToolOutcome(detail=_refusal_detail(outcome))
        fenced = outcome.result.as_evidence()
        return ToolOutcome(
            invoked=True,
            # The system a reader will see this attributed to. Web providers
            # collapse onto one source system -- what a reader needs to know is
            # "the internet", not which vendor answered -- and everything else
            # is named by its own server.
            source_system=source_system_for(request.qualified_name)
            or outcome.result.server,
            # The federation layer's own fencing accessor, which is the only
            # path out of it that a prompt may reach. Its body is neutralised
            # text; the reasoning layer fences it again on the way into a
            # prompt, exactly as it fences a retrieved message.
            text=fenced.body,
            attribution=outcome.result.attribution,
            detail=outcome.result.notice() or "",
        )


def _with_call_site_arguments(request: InvocationRequest) -> InvocationRequest:
    """Supply the argument a web tool needs and the model is never shown.

    A web provider checks the query it is about to send against the asking
    person's own words, so it needs both. The model is offered only the query
    half on purpose -- a model that wrote both halves of a comparison would be
    comparing nothing -- which leaves the other half to the call site, here.

    Every other federated tool is handed exactly what the model proposed: an
    argument nobody's schema asked for is noise to the server receiving it,
    and inventing one is how a call starts failing for reasons no log explains.
    """
    if source_system_for(request.qualified_name) != SOURCE_WEB:
        return request
    proposed = request.arguments.get(ARG_QUERY)
    return replace(
        request,
        arguments=web_arguments(
            request.question, proposed if isinstance(proposed, str) else ""
        ),
    )


def _refusal_detail(outcome: InvocationOutcome) -> str:
    """Why a call produced nothing, for the operator record only.

    Never returned to a model and never rendered to a requester: a refusal
    reason is a description of the guard, and the one reader who must not have
    it is the thing trying to get past it.
    """
    if outcome.decision.refusal is not None:
        return str(outcome.decision.refusal)
    return outcome.notice() or "no result"


@dataclass(frozen=True, slots=True)
class FederatedTools:
    """The outbound surface a deployment runs with, or nothing at all.

    Held as one value because the four are one arrangement: the router offers
    from `federation`'s registration, and `invoker` re-checks every call
    against the same registration's permits. Splitting them is how an offer
    ends up authorising an invocation.
    """

    federation: Federation
    #: Typed as the port rather than the class, so a type check proves the
    #: object this root hands the reasoning loop really can do what the loop
    #: asks of it. A surface that only offered would satisfy `ToolSurface` and
    #: silently never call anything, which is this project's recurring bug.
    surface: FederatedSurface
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
    """Read one `server:tool` entry and whatever the operator declared about it.

    An entry without a suffix leaves the effect undetermined, which counts as
    mutating and is not enabled -- so the quiet outcome of a typo is a tool
    that is refused, never one that writes unasked. `:ro` is the operator's
    own determination that a tool is read-only, and the only thing in the
    system that may make one: a server's own advertisement never reaches
    here. `:enable-mutation` is the opposite declaration, and the only way a
    state-changing tool becomes callable at all.
    """
    server, _, rest = spec.partition(":")
    tool, _, suffix = rest.partition(":")
    if not server or not tool:
        raise FederationConfigurationError(
            f"federation tool {spec!r} must be written "
            f"server:tool[:{READ_ONLY_SUFFIX}|:{MUTATION_SUFFIX}]"
        )
    if suffix and suffix not in _DECLARATIONS:
        known = ", ".join(repr(w) for w in sorted(_DECLARATIONS))
        raise FederationConfigurationError(
            f"federation tool {spec!r}: {suffix!r} is not a known declaration; "
            f"the only suffixes are {known}"
        )
    declared = _DECLARATIONS.get(suffix, UNDECLARED)
    return AllowedTool(
        server=server,
        tool=tool,
        credential=declared.credential,
        effect=declared.effect,
        mutation_enabled=declared.mutation_enabled,
    )


def parse_credential_holder(spec: str) -> tuple[str, PersonRef]:
    """Read one `server=platform:user_id` entry.

    A mutating call carries the requester's own authority, so the operator
    has to say whose. Written per person rather than as "anyone in the
    server": the set of people who may change something through the agent is
    exactly the set somebody wrote down.
    """
    server, separator, who = spec.partition(CREDENTIAL_HOLDER_SEPARATOR)
    platform, qualifier, user_id = who.partition(":")
    if not separator or not server.strip() or not qualifier or not platform.strip():
        raise FederationConfigurationError(
            f"federation credential holder {spec!r} must be written "
            "server=platform:user_id"
        )
    if not user_id.strip().isdigit():
        raise FederationConfigurationError(
            f"federation credential holder {spec!r}: {user_id!r} is not an account id"
        )
    return server.strip(), PersonRef(platform.strip(), int(user_id.strip()))


def build_credential_holders(settings: Settings) -> dict[str, frozenset[PersonRef]]:
    """Who holds their own credential on each federated server.

    Empty is the correct default and the safe one: with nobody named, a
    per-requester tool is refused rather than falling back to the bot's own
    identity, which would hand every member of the server whatever the bot
    holds.
    """
    holders: dict[str, frozenset[PersonRef]] = {}
    for spec in settings.federation_credential_holders:
        server, person = parse_credential_holder(spec)
        holders[server] = holders.get(server, frozenset()) | {person}
    return holders


def _check_mutation_is_spendable(
    config: FederationConfig, holders: Mapping[str, frozenset[PersonRef]]
) -> None:
    """Refuse a half-written grant, in either direction.

    An enabled mutating tool whose server names no holder is refused at
    invocation for want of a credential -- which is safe, and indistinguishable
    from a working configuration until somebody tries. This project has
    shipped that shape seven times, so it is a startup error instead: the two
    declarations that together enable a mutation must both be present, or
    neither is honoured.
    """
    for entry in config.allowlist:
        if entry.mutation_enabled and not holders.get(entry.server):
            raise FederationConfigurationError(
                f"{entry.qualified_name} is declared {MUTATION_SUFFIX} but nobody "
                f"holds a credential on {entry.server!r}; name them in "
                "FEDERATION_CREDENTIAL_HOLDERS as server=platform:user_id"
            )
    for server in holders:
        if config.server(server) is None:
            raise FederationConfigurationError(
                f"federation credential holder names server {server!r}, "
                "which is not configured"
            )


def federates_anything(settings: Settings) -> bool:
    """Whether any tool -- remote, web, market or wallet -- could be registered.

    One predicate for both callers. The config and the proposer used to repeat
    the condition, and a third kind of tool added to one copy and not the other
    is exactly a tool registered, offered, and never called.
    """
    return bool(
        settings.federation_servers
        or settings.web_tools_enabled
        or settings.market_tools_enabled
        or settings.wallet_tools_enabled
    )


def build_federation_config(settings: Settings) -> FederationConfig | None:
    """The outbound surface an operator asked for, or None for "no federation".

    Raises `FederationConfigurationError` for anything it cannot honour --
    a malformed entry, a tool naming a server that is not configured. The
    caller decides what that costs; here it is simply not silently dropped.
    """
    # Any registered tool needs a proposer, not only a remote one. Gating on
    # MCP servers alone left a deployment with local web tools offering them
    # every run and calling none -- the log said "offer_only" and the
    # behaviour looked like a model that never wanted a tool.
    if not federates_anything(settings):
        return None
    config = FederationConfig(
        servers=tuple(parse_server(s) for s in settings.federation_servers),
        allowlist=tuple(parse_allowed_tool(t) for t in settings.federation_tool_allowlist),
        max_tools_per_run=settings.federation_max_tools_per_run,
    )
    _check_mutation_is_spendable(config, build_credential_holders(settings))
    return config


def report_federation(
    config: FederationConfig,
    registration: Registration,
    holders: Mapping[str, frozenset[PersonRef]] | None = None,
) -> None:
    """Say, at startup, exactly what is callable and what is not.

    An operator must be able to tell a silently empty registry from a working
    one without reading the code, so the registered tool names are logged by
    name and an empty registry is a warning rather than an absence of output.

    Tools that can change something are reported separately and at warning
    level. A deployment where the agent may only read and one where it may
    write used to produce byte-identical startup output, which left "we
    granted that six deploys ago" as something nobody could see.
    """
    mutating = sorted(t.qualified_name for t in registration.tools if t.permit.mutation_enabled)
    log.info(
        "composition.federation.registered",
        servers=list(config.server_names),
        tools=sorted(registration.names),
        read_only=sorted(registration.names - frozenset(mutating)),
        mutating=mutating,
        unreachable=list(registration.unreachable_servers),
        unavailable=list(registration.unavailable_tools),
        max_tools_per_run=config.max_tools_per_run,
    )
    if mutating:
        log.warning(
            "composition.federation.mutation_enabled",
            tools=mutating,
            holders=sorted(
                str(person)
                for server, people in (holders or {}).items()
                if any(t.startswith(f"{server}:") for t in mutating)
                for person in people
            ),
            hint="these tools change state on their server; every call is put "
            "to the person who asked before it is made",
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


def web_tools_config(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> WebToolsConfig:
    """Operator settings, translated for the web adapter.

    The key is unwrapped here and nowhere else: `WebToolsConfig` holds a
    plain string because the provider needs one to sign a request, and the
    narrower the place a secret is readable the fewer places can log it.
    """
    return WebToolsConfig(
        serpapi_key=(
            settings.serpapi_key.get_secret_value() if settings.serpapi_key else None
        ),
        max_calls_per_run=settings.web_max_calls_per_run,
        timeout_seconds=settings.web_timeout_seconds,
        transport=transport,
    )


def chain_tools_config(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> ChainToolsConfig:
    """Operator settings, translated for the wallet adapter.

    One Infura key covers every chain: the endpoint differs only in its host
    segment, so a second variable would be a second place for the same secret
    to be set wrong.
    """
    return ChainToolsConfig(
        infura_key=(
            settings.infura_key.get_secret_value() if settings.infura_key else None
        ),
        max_calls_per_run=settings.wallet_max_calls_per_run,
        timeout_seconds=settings.wallet_timeout_seconds,
        positions_timeout_seconds=settings.positions_timeout_seconds,
        transport=transport,
    )


def market_tools_config(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> MarketToolsConfig:
    """Operator settings, translated for the market adapter.

    The SerpApi key is the web tools' key: the S&P 500 comes from Google
    Finance through the same account, so a second variable would only be a
    second place for the same secret to be set wrong.
    """
    return MarketToolsConfig(
        serpapi_key=(
            settings.serpapi_key.get_secret_value() if settings.serpapi_key else None
        ),
        max_calls_per_run=settings.market_max_calls_per_run,
        timeout_seconds=settings.market_timeout_seconds,
        transport=transport,
    )


async def build_federation(
    settings: Settings,
    factory: SessionFactory | None = None,
    *,
    proposer: ModelToolProposer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FederatedTools | None:
    """Connect to the configured servers, or run without any.

    `transport` is what the local web, market and wallet providers send
    through -- the process's `Edges.http_transport`. None is httpx's own.

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

    # Local providers -- Wikipedia, and SerpApi when a key is configured.
    # They are not remote MCP servers, but they are governed by the same
    # allowlist, routing, invoke-time authorization and audit, because a
    # second ungoverned path for tools is exactly what that machinery
    # exists to prevent. They register even when no MCP server is
    # configured, which is the ordinary case for this deployment.
    web = (
        build_web_tools(web_tools_config(settings, transport))
        if settings.web_tools_enabled
        else None
    )
    if web is not None and web.providers:
        config = web.merge_into(config or FederationConfig())
        factory = web.factory(factory)
        log.info(
            "composition.web_tools.registered",
            providers=sorted(web.providers),
        )
    elif settings.web_tools_enabled:
        log.warning("composition.web_tools.none_available")

    # The USD rates behind a second figure in the asker's preferred currency:
    # the market tools' own FX provider when they are on, else one of the same
    # class over the same host, registered as no tool.
    rates: UsdReferenceRates | None = None

    # Market data, through the same door and after the web tools, so its
    # factory is outermost and anything it does not own falls through to the
    # web factory and then to remote MCP. Read-only is declared by
    # `build_market_tools` itself; nothing here can widen it. What leaves is
    # held to membership in `app.egress.CLOSED_VOCABULARIES` by the invoker's
    # guard, not by anything this function chooses.
    if settings.market_tools_enabled:
        market = build_market_tools(market_tools_config(settings, transport))
        rates = market.rates
        config = market.merge_into(config or FederationConfig())
        factory = market.factory(factory)
        log.info(
            "composition.market_tools.registered",
            providers=sorted(market.providers),
        )

    # Wallet balances, last, so its factory is outermost and anything it does
    # not own falls through to market, then web, then remote MCP. Held to
    # *rooting* rather than to a closed vocabulary -- an address has no fixed
    # set to be a member of, and rooting is what makes the lookup only ever
    # reach an address the asker typed themselves.
    if settings.wallet_tools_enabled:
        chain = build_chain_tools(
            chain_tools_config(settings, transport),
            rates=rates or build_usd_rates(market_tools_config(settings, transport)),
        )
        if chain.servers:
            config = chain.merge_into(config or FederationConfig())
            factory = chain.factory(factory)
            log.info("composition.wallet_tools.registered", chains=list(chain.server_names))
        else:
            log.warning("composition.wallet_tools.none_available", reason="no INFURA_KEY")

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

    holders = build_credential_holders(settings)
    report_federation(config, federation.registration, holders)
    if proposer is None:
        # Tools that can be offered and never called. Said out loud, because a
        # federation wired to a model that cannot call tools looks identical in
        # every log line except this one.
        log.warning(
            "composition.federation.offer_only",
            hint="no tool-calling model handle; runs will be offered tools but call none",
        )
    return _federated_tools(config, federation, proposer, holders)


def _federated_tools(
    config: FederationConfig,
    federation: Federation,
    proposer: ModelToolProposer | None = None,
    holders: Mapping[str, frozenset[PersonRef]] | None = None,
) -> FederatedTools:
    """Assemble the guarded door around a connected federation.

    The credential broker holds only the people an operator wrote down, and
    is empty for a deployment that wrote none: a per-requester tool is
    refused until somebody's credential is actually registered, which fails
    closed rather than falling back to the bot's own identity and handing
    every server member whatever it holds.
    """
    confirmations = ConfirmationLedger()
    audit = InMemoryAuditTrail()
    broker = CredentialBroker(dict(holders or {}))
    authorizer = Authorizer(federation.permits, confirmations, broker)
    invoker = GuardedInvoker(federation, authorizer, audit, confirmations)
    return FederatedTools(
        federation=federation,
        # The same invoker the surface hands calls to, so there is one guarded
        # door rather than one for the loop and another for anything else that
        # reaches in here.
        surface=RoutedToolSurface(
            ToolRouter(config.max_tools_per_run),
            federation.registration,
            invoker,
            proposer,
        ),
        invoker=invoker,
        audit=audit,
        confirmations=confirmations,
    )


def available_commands(settings: Settings) -> tuple[Command, ...]:
    """The commands this deployment registers, for the capability reply.

    Kept beside the wiring rather than inside `self_description`, because
    whether a command exists is a deployment question and that module must not
    read settings to answer it.
    """
    commands = list(ALWAYS_AVAILABLE)
    if settings.notifications_enabled:
        commands.append(NOTIFICATIONS)
    if settings.scheduled_tasks_enabled:
        commands.extend(SCHEDULED)
    if alerts_available(settings):
        commands.extend(ALERTS)
    return tuple(commands)


def deployment_capabilities(
    settings: Settings, external_tools: Sequence[str] = (), *, personal_facts: bool = False
) -> Capabilities:
    """What "what can you do?" describes for this deployment.

    `external_tools` are the registered tool names, not the settings that
    asked for them: a wallet tool switched on without an Infura key is not
    registered, and must not be promised.
    """
    return Capabilities(
        tuple(external_tools),
        # Only the commands this deployment actually registers. Listing
        # `/notifications` where the feature is off tells somebody to use a
        # command Discord will not show them.
        available_commands(settings),
        personal_facts=personal_facts,
        # Without extraction there are no asks, and "what do I need to do?"
        # would only ever be answered that nothing was asked.
        obligations=settings.ask_extraction_enabled,
        # Whether a voice message in a DM is heard.
        voice_questions=settings.voice_questions_enabled,
    )


def _infura_key(settings: Settings) -> str:
    return settings.infura_key.get_secret_value().strip() if settings.infura_key else ""


def alerts_available(settings: Settings) -> bool:
    """Alerts are on, and there is a chain endpoint for them to read."""
    return settings.alerts_enabled and bool(_infura_key(settings))


def build_alert_prices(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> AlertPrices:
    """BTC and ETH for price alerts: the market tools' CoinGecko provider.

    Its own instance, on its own cache and rate limit, but the same class,
    endpoint and constant request, so a price alert reaches no host the
    portfolio's ether pricing does not already reach, whether or not the
    market tools are switched on. The budget is never spent: `latest` is
    not a tool call.
    """
    provider = CoinGeckoProvider(
        CallBudget(settings.market_max_calls_per_run),
        timeout_seconds=settings.market_timeout_seconds,
        transport=transport,
    )
    return AlertPrices(provider)


def build_alert_requests(
    settings: Settings,
    engine: AsyncEngine,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = utc_now,
) -> AlertRequests | None:
    """Creating alerts from a request, or None when the feature is off.

    Built on the same condition as the sweep: an alert nothing would check
    must not be creatable. None answers an alert request that alerts are not
    available here. `transport` and `clock` are the process's edges, so the
    creation read and the first check time are what a test fakes too.
    """
    if not alerts_available(settings):
        return None
    return AlertRequests(
        AlertService(PostgresAlertStore(engine), sweep_seconds=settings.alert_sweep_seconds),
        ChainTargets(_infura_key(settings), transport=transport),
        prices=build_alert_prices(settings, transport),
        sweep_seconds=settings.alert_sweep_seconds,
        clock=clock,
    )


def build_schedules(settings: Settings, engine: AsyncEngine) -> ScheduleService | None:
    """The `/schedule` commands, or None when the feature is off.

    None rather than a service over an empty store: the commands then say the
    feature is not switched on, which is the honest answer, and a deployment
    that has not chosen to send unprompted messages does not quietly acquire
    the ability to.
    """
    if not settings.scheduled_tasks_enabled:
        return None
    return ScheduleService(
        PostgresScheduleStore(engine, cap=settings.scheduled_tasks_per_person),
        tasks_per_person=settings.scheduled_tasks_per_person,
    )


def build_task_runner(
    settings: Settings, engine: AsyncEngine, asks: AskService, messenger: TaskMessenger
) -> ScheduledTaskRunner | None:
    """The sweep that runs due tasks, or None when the feature is off.

    Takes the process's own `AskService`, not a second one: a scheduled run is
    the same question by the same person through the same stack, and a second
    instance would be a second set of rules about what they may read.
    """
    if not settings.scheduled_tasks_enabled:
        return None
    return ScheduledTaskRunner(
        PostgresScheduleStore(engine, cap=settings.scheduled_tasks_per_person),
        asks,
        messenger,
    )


def build_alert_runner(
    settings: Settings,
    engine: AsyncEngine,
    messenger: TaskMessenger,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AlertRunner | None:
    """The position-alert sweep, or None when the feature is off.

    Off unless switched on, and never without an Infura key: the sweep reads
    the chain and nothing else, so with no endpoint there is nothing to build
    and no half-feature to leave running. `transport` is the process's
    `Edges.http_transport`, as for every other provider.
    """
    if not settings.alerts_enabled:
        return None
    key = _infura_key(settings)
    if not key:
        log.warning("composition.alerts_without_key", hint="ALERTS_ENABLED needs INFURA_KEY")
        return None
    return AlertRunner(
        PostgresAlertStore(engine),
        ChainWatcher(key, transport=transport),
        messenger,
        prices=build_alert_prices(settings, transport),
        sweep_seconds=settings.alert_sweep_seconds,
        # The owner's preferred currency beside the dollar figures, priced by
        # the same FX provider and host the market tools use.
        currencies=PreferredCurrencies(
            PostgresFactStore(engine), build_usd_rates(market_tools_config(settings, transport))
        ),
    )


def build_channel_listing(
    guild: GuildProvider, scope: ScopeProvider
) -> ChannelListingService:
    """`/channels`, over the same resolver that scopes retrieval.

    The same `DiscordAclResolver` the ask path uses, deliberately. A listing
    built from a second permission check could disagree with what the person
    can actually search, and the disagreement would show up as a channel they
    were told about returning nothing -- or one they were not told about
    returning something.
    """
    return ChannelListingService(scope, DiscordAclResolver(guild, scope))


def build_answers(
    retrieval: RetrievalTool,
    chat: ChatModel,
    tools: ToolSurface | FederatedSurface | None = None,
    tracer: RunTracer | None = None,
    clock: Clock = utc_now,
) -> ReasoningAnswerService:
    """The real answer service: both paths, over one retrieval tool.

    `tools` is the loop's whole federated capability. A `FederatedSurface`
    reaches the loop as one object that offers, proposes and invokes, so the
    thing that decides what a run may call is the thing that calls it.

    `tracer` is None for a deployment that configured no destination, and a
    no-op tracer is then used rather than a branch at the call site.

    `clock` is what the time route answers from and what the prompt's clock
    notice states -- the process's `Edges.clock`.
    """
    return build_answer_service(retrieval, chat, tools=tools, tracer=tracer, clock=clock)


def build_tracer(
    settings: Settings,
    engine: AsyncEngine,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunTracer | None:
    """The trace exporter, or None when nothing is configured to receive one.

    Three things have to be true, and a missing one is silence rather than a
    boot failure: an operator turned it on, named a host, and supplied both
    keys. Half-configured tracing is the case worth failing on loudly, because
    a deployment that believes it is recording and is not will discover it the
    day somebody asks what went wrong.
    """
    if not settings.tracing_enabled:
        return None
    missing = [
        name
        for name, value in (
            ("LANGFUSE_HOST", settings.langfuse_host),
            ("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
            ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "TRACING_ENABLED is on but " + ", ".join(missing) + " is not set"
        )
    assert settings.langfuse_public_key is not None
    assert settings.langfuse_secret_key is not None
    return OptOutAwareTracer(
        LangfuseTracer(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            index=PostgresTraceIndex(engine),
            timeout=settings.tracing_timeout_seconds,
            environment=settings.langfuse_environment,
            transport=transport,
        ),
        PostgresRetentionStore(engine),
    )


def build_corpus_store(settings: Settings, engine: AsyncEngine) -> PostgresStore:
    """The store capture writes through, recording channel media once enabled.

    One builder for the ingest process and anything standing in for it, so the
    moment media recording starts is decided in one place.
    """
    return PostgresStore(engine, media_since=settings.media_capture_since)


def build_trace_withdrawal(
    settings: Settings,
    engine: AsyncEngine,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TraceWithdrawal | None:
    """The deletion side of tracing, for the ingest process.

    Built from the same three settings as the exporter, because a deployment
    that exports must withdraw and one that does not has nothing to withdraw.
    `transport` replaces httpx's default network transport. The ingest
    entrypoint passes none; the end-to-end harness passes FakeWeb's, which
    without this parameter it could not, so no test could see a trace being
    withdrawn.
    """
    if not settings.tracing_enabled or not settings.langfuse_host:
        return None
    if settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
        return None
    public_key = settings.langfuse_public_key.get_secret_value()
    secret_key = settings.langfuse_secret_key.get_secret_value()
    return TraceWithdrawal(
        PostgresTraceIndex(engine),
        LangfuseTraceDeleter(
            host=settings.langfuse_host,
            public_key=public_key,
            secret_key=secret_key,
            timeout=settings.tracing_timeout_seconds,
            transport=transport,
        ),
        # The backstop for traces exported before the index recorded who
        # asked them: an opt-out queues a search by platform id, this runs it.
        LangfuseTraceFinder(
            host=settings.langfuse_host,
            public_key=public_key,
            secret_key=secret_key,
            environment=settings.langfuse_environment,
            timeout=settings.tracing_timeout_seconds,
            transport=transport,
        ),
    )


def build_catch_up(
    settings: Settings,
    search: SearchBackend,
    chat: ChatModel,
    clock: Clock = utc_now,
) -> CatchUpService:
    """The catch-up summariser, over the same two things an answer is made of.

    A `CorpusRetrieval` and a `ModelSynthesizer`, built exactly as
    `build_answer_stack` builds them. Both are stateless adapters over the
    search backend and the chat handle the process already holds, so a second
    instance is a second reference and not a second door: what either returns
    is decided entirely by the viewer it is handed, and `CatchUpService`
    narrows that viewer rather than widening it.

    `clock` decides where "since" starts and what the prompt says the time
    is -- the process's `Edges.clock`.
    """
    return CatchUpService(
        CorpusRetrieval(search, discord_urls(settings.discord_guild_id)),
        ModelSynthesizer(chat, clock),
        clock=clock,
    )


def build_said_by(
    settings: Settings,
    search: SearchBackend,
    chat: ChatModel,
    clock: Clock = utc_now,
) -> SaidByService:
    """"What did Ana say about X last week", over the answer stack's backend.

    Built like `build_catch_up`: a `CorpusRetrieval` and a `ModelSynthesizer`
    over the search backend and chat handle the process already holds, plus
    that same backend as the name lookup. Every one of them takes the viewer
    the service narrows to. `clock` and `ANSWER_TIMEZONE` decide what "ontem"
    and "semana passada" mean.
    """
    return SaidByService(
        CorpusRetrieval(search, discord_urls(settings.discord_guild_id)),
        ModelSynthesizer(chat, clock),
        search,
        tz=ZoneInfo(settings.answer_timezone),
        clock=clock,
    )


def build_tool_proposer(
    settings: Settings, chat: ToolCapableChat
) -> ModelToolProposer | None:
    """The stage that asks the model whether a federated tool should be called.

    None for a deployment that federates nothing -- there is no question to
    ask -- and None, loudly, for one whose model cannot call tools. The second
    is deliberately not a boot failure: federation widens where answers may
    come from, and taking the bot down over it would make somebody else's
    capability a prerequisite for answering questions about Discord.
    """
    # Any registered tool needs a proposer, not only a remote one. Gating on
    # MCP servers alone left a deployment with local web tools offering them
    # every run and calling none -- the log said "offer_only" and the
    # behaviour looked like a model that never wanted a tool.
    if not federates_anything(settings):
        return None
    try:
        # Raises here, naming `tool_calling`, rather than as an endpoint's 400
        # on the first question that happens to route a tool.
        caller = chat.tool_caller()
    except MissingCapabilityError as exc:
        log.error("composition.federation.tool_calling_unavailable", error=str(exc))
        return None
    return ModelToolProposer(caller)


def notification_policy(settings: Settings) -> NotificationPolicy:
    """The bounds on messaging somebody who asked for nothing.

    Built once and shared by both halves, as `ask_policy` is: the confidence
    the enqueue sweep refuses below is the same one the answer path refuses to
    report below, and two copies of that number would mean a deployment that
    direct-messages people about obligations it will not show them in a list.
    """
    return NotificationPolicy(
        batch_window=timedelta(seconds=settings.notification_batch_window_seconds),
        min_interval=timedelta(seconds=settings.notification_min_interval_seconds),
        max_age=timedelta(hours=settings.notification_max_age_hours),
        expire_after=timedelta(hours=settings.notification_expire_hours),
        max_items=settings.notification_max_items,
        min_confidence=settings.ask_min_confidence,
    )


def build_obligation_notifier(
    settings: Settings, engine: AsyncEngine
) -> ObligationNotifier:
    """The ingest half: extracted obligations become queue rows.

    Over the engine that process already holds, because the sweep is a write
    over the `ask` table it has just written to. It reaches no network and
    sends nothing: the process that can reach Discord is the other one.
    """
    log.info(
        "composition.notifications",
        batch_window_seconds=settings.notification_batch_window_seconds,
        min_interval_seconds=settings.notification_min_interval_seconds,
        max_age_hours=settings.notification_max_age_hours,
    )
    return ObligationNotifier(
        PostgresNotificationQueue(engine), notification_policy(settings)
    )


def build_notification_delivery(
    settings: Settings,
    engine: AsyncEngine,
    guild: GuildProvider,
    sender: NotificationSender,
    scope: ScopeProvider | None = None,
) -> NotificationDelivery:
    """The bot half: queue rows become one direct message per person.

    The resolver handed in here is the whole send-time permission re-check. It
    is built over the same live guild state and the same indexing scope every
    answer is bounded by -- not a set captured when the obligation was
    extracted -- so a person who lost access to a channel between extraction
    and delivery is not told what was said in it.
    """
    indexed = scope if scope is not None else StaticScope(settings.indexed_channel_ids)
    return NotificationDelivery(
        queue=PostgresNotificationQueue(engine),
        acl=DiscordAclResolver(guild, indexed),
        sender=sender,
        policy=notification_policy(settings),
    )


def build_notification_preferences(engine: AsyncEngine) -> NotificationPreferences:
    """The person's own switch, for `/notifications`.

    Over the same engine as delivery, so the row a person writes with the
    command is the row the claim statement reads. A second store here would be
    a switch that turns nothing off.
    """
    return NotificationPreferences(PostgresNotificationQueue(engine))


def ask_policy(settings: Settings) -> AskPolicy:
    """The thresholds both halves of the feature are tuned by.

    Built once and shared: the confidence the extractor writes below is the
    confidence the answer path refuses to report above, and two copies of that
    number drift into a store full of asks nothing will ever show.
    """
    return AskPolicy(
        min_confidence=settings.ask_min_confidence,
        stale_after=timedelta(days=settings.ask_stale_after_days),
    )


def build_obligations(settings: Settings, engine: AsyncEngine) -> ObligationService:
    """The read path for "what did people ask me".

    Takes the engine the rest of the graph already holds, so obligations are
    read through the same pool as everything else rather than opening a second
    one for a question that is a filter over a few rows.
    """
    return ObligationService(
        PostgresAskStore(engine),
        policy=ask_policy(settings),
        # Citations are the whole defence against an inferred obligation: an
        # ask is a claim the system made about a person, so the reader has to
        # be able to open the message it came from in one click.
        message_url=discord_message_url(settings.discord_guild_id),
    )


def build_decision_answers(
    settings: Settings,
    engine: AsyncEngine,
    embeddings: EmbeddingClient,
    fallback: AnswerService,
    clock: Clock = utc_now,
) -> DecisionAnswerService:
    """"What did we decide about Y?", answered from the decision rows.

    Over the engine and embeddings client the answer stack already holds: the
    store only reads here, and its one outbound call is the topic's embedding.
    `clock` and `ANSWER_TIMEZONE` decide what "semana passada" means, as for
    said-by. Everything it does not claim, or finds nothing for, reaches
    `fallback` unchanged.
    """
    return DecisionAnswerService(
        PostgresDecisionStore(engine, embeddings),
        embeddings,
        fallback,
        tz=ZoneInfo(settings.answer_timezone),
        clock=clock,
        policy=DecisionPolicy(min_similarity=settings.decision_min_similarity),
        # The same one-click citation obligations give: a decision is a claim
        # the system made about a conversation.
        message_url=discord_message_url(settings.discord_guild_id),
    )


@dataclass(frozen=True, slots=True)
class AskPipeline:
    """The ingest-side half of the feature, assembled as one piece.

    Returned together because they are only correct together: the worker
    writes through the same store the state pass reads, and the directory the
    worker teaches is the one resolution asks who a name refers to. Building
    them apart is how the worker ends up resolving every name to nobody.

    `usage` is carried so the standing cost can be reported rather than
    guessed -- extraction is billed per message of traffic, not per question.
    """

    store: PostgresAskStore
    worker: ExtractionWorker
    state: AskStateService
    directory: ObservedDirectory
    usage: UsageMeter
    decisions: PostgresDecisionStore


def build_decision_store(
    settings: Settings, engine: AsyncEngine, embeddings: EmbeddingClient | None = None
) -> PostgresDecisionStore:
    """The decision log, embedding through `embeddings` or the configured endpoint.

    Its own builder because ingest needs it even with extraction switched off:
    deleting a message has to withdraw the decisions recorded before it was.
    """
    return PostgresDecisionStore(
        engine, embeddings if embeddings is not None else build_embeddings(settings)
    )


def build_ask_pipeline(
    settings: Settings,
    engine: AsyncEngine,
    extractor: AskExtractor | None = None,
    embeddings: EmbeddingClient | None = None,
) -> AskPipeline:
    """Assemble the extraction pass for the ingest process.

    On the cheap model, deliberately: extraction runs over traffic rather than
    over questions, so a frontier model here is a standing bill nobody asked
    for.

    `extractor` is the model edge, and the only one: production leaves it
    unset and gets the OpenAI-compatible extractor named here. An end-to-end
    test hands a scripted one, so everything behind it -- candidate
    filtering, resolution, the store -- is the pipeline ingest runs. `usage`
    then counts nothing, because nothing here was billed.

    The same pass records decisions, embedded at write time through
    `embeddings`: production leaves it unset and gets the configured
    embedding endpoint, and an end-to-end test hands its offline one.
    """
    store = PostgresAskStore(engine)
    decisions = build_decision_store(settings, engine, embeddings)
    directory = ObservedDirectory()
    usage = UsageMeter()
    extraction = ExtractionService(
        extractor=extractor
        if extractor is not None
        else OpenAICompatibleAskExtractor(
            ExtractorConfig(
                api_key=settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url,
                model=settings.extraction_model,
            ),
            usage,
        ),
        store=store,
        directory=directory,
        candidates=CandidateFilter(),
        policy=ask_policy(settings),
        decisions=decisions,
    )
    log.info(
        "composition.ask_extraction",
        extraction_model=settings.extraction_model,
        window_messages=settings.ask_extraction_window_messages,
        min_confidence=settings.ask_min_confidence,
        stale_after_days=settings.ask_stale_after_days,
    )
    return AskPipeline(
        store=store,
        worker=ExtractionWorker(
            extraction,
            window_messages=settings.ask_extraction_window_messages,
            directory=directory,
        ),
        state=AskStateService(store, ask_policy(settings)),
        directory=directory,
        usage=usage,
        decisions=decisions,
    )


@dataclass(frozen=True, slots=True)
class Edges:
    """Everything the object graph reaches the outside world through.

    Production builds these from `Settings` in `Edges.production`; an
    end-to-end test hands fakes here and nowhere else, so everything between
    the edges is the graph production runs, built by the same functions.

    `http_transport` is what every HTTP client the federation's local
    providers and the tracer open sends through; `clock` is what the time
    route and the prompt's clock notice read, and what the bot's scheduled
    and notification loops pass as "now". Production leaves both at their
    defaults -- httpx's own transport and the wall clock -- which is exactly
    what those adapters used before the seam.
    """

    chat: ToolCapableChat
    summary_chat: ChatModel
    embeddings: EmbeddingClient
    engine: AsyncEngine
    http_transport: httpx.AsyncBaseTransport | None = None
    clock: Clock = utc_now

    @classmethod
    def production(cls, settings: Settings) -> Edges:
        """The edges a deployment runs over, built exactly as before the seam.

        The chat handle first: its capability check is declarative and free,
        so a deployment that cannot serve fails before a pool is opened or an
        embedding call is spent.
        """
        chat = build_chat_model(settings)
        engine = create_async_engine(
            settings.database_url.get_secret_value(), pool_pre_ping=True
        )
        return cls(
            chat=chat,
            summary_chat=build_summary_model(settings),
            embeddings=build_embeddings(settings),
            engine=engine,
        )


async def build_answer_stack(
    settings: Settings, *, personal_facts: bool = False, edges: Edges
) -> AnswerStack:
    """Assemble everything between the corpus and an answer, over `edges`.

    `personal_facts` says whether the process answering through this stack
    keeps them, so "what can you do?" only offers to remember a name where
    something will.

    Ordering is the point. The two checks that can refuse the deployment run
    before the graph is returned, so a process that reaches its gateway
    connection is one whose model and corpus agree with its configuration.
    The first -- the chat model's capabilities -- runs when the edges are
    built; the embedding width is checked here, against whatever `edges`
    holds, fakes included.
    """
    chat = edges.chat
    engine = edges.engine
    embeddings = edges.embeddings
    await verify_embedding_width(
        embeddings, settings.embedding_model, settings.embedding_dimensions
    )
    search = HybridSearch(engine, embeddings)

    retrieval = CorpusRetrieval(search, discord_urls(settings.discord_guild_id))
    obligations = build_obligations(settings, engine)
    # After the two checks that can refuse the deployment: a federated server
    # is reached over the network, and a slow handshake must not sit in front
    # of the failures that stop the process.
    proposer = build_tool_proposer(settings, chat)
    federation = await build_federation(
        settings, proposer=proposer, transport=edges.http_transport
    )
    log.info(
        "composition.answer_stack",
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        stages=[str(s) for s in ANSWERING_STAGES],
        federated_tools=sorted(federation.federation.registration.names) if federation else [],
        # Whether a run can actually call one of those, as opposed to being
        # shown it. The two used to be indistinguishable from the outside, and
        # the answer was "no" for the whole life of the federation layer.
        federated_tool_calls=proposer is not None,
        ask_min_confidence=settings.ask_min_confidence,
    )
    tracer = build_tracer(settings, engine, edges.http_transport)
    log.info("composition.tracing", enabled=tracer is not None)
    reasoning = build_answers(
        retrieval,
        chat,
        tools=federation.surface if federation else None,
        tracer=tracer,
        clock=edges.clock,
    )
    capabilities = deployment_capabilities(
        settings,
        sorted(federation.federation.permits) if federation is not None else (),
        personal_facts=personal_facts,
    )
    return AnswerStack(
        engine=engine,
        search=search,
        chat=chat,
        # The obligation path goes in front of the reasoning service rather
        # than inside it. "What did people ask me today" is a filter over rows
        # by addressee and time; embedding that sentence and hoping the right
        # windows surface is exactly how this feature fails, and it is why the
        # rows exist. Decisions sit behind it for the same reason, and hand
        # back to reasoning whatever they find nothing for. Everything neither
        # claims reaches `reasoning` unchanged.
        # Outermost, so "what can you do" is answered from configuration
        # before anything can search the corpus for it.
        answers=SelfDescriptionAnswerService(
            # The same clock as the reasoning service, so "this week" and
            # the time route agree on when now is.
            ObligationAnswerService(
                obligations,
                build_decision_answers(
                    settings, engine, embeddings, reasoning, clock=edges.clock
                ),
                clock=edges.clock,
            ),
            capabilities,
        ),
        capabilities=capabilities,
        reasoning=reasoning,
        obligations=obligations,
        federation=federation,
    )


def memory_policy(settings: Settings) -> MemoryPolicy:
    return MemoryPolicy(
        recent_turns=settings.memory_recent_turns,
        summarise_after_turns=settings.memory_summarise_after_turns,
    )


def build_summary_model(settings: Settings) -> OpenAICompatibleChat:
    """The cheap handle the summariser writes with.

    Declared for no answering stage: it asks for plain text, which every chat
    model provides, so there is no capability to check and no reason for a
    summariser to be able to refuse the deployment.
    """
    return OpenAICompatibleChat(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.extraction_model,
        stages=(),
        provides=frozenset({ModelCapability.CHAT}),
    )


def build_conversations(
    settings: Settings, engine: AsyncEngine, model: ChatModel | None = None
) -> Conversations:
    """Conversation memory for the bot, over the answer stack's engine."""
    store = PostgresMemoryStore(engine)
    policy = memory_policy(settings)
    log.info(
        "composition.conversation_memory",
        recent_turns=policy.recent_turns,
        summarise_after_turns=policy.summarise_after_turns,
        summary_model=settings.extraction_model,
    )
    return Conversations(
        store,
        ConversationSummariser(store, model or build_summary_model(settings), policy),
        policy,
        facts=PostgresFactStore(engine),
    )


def build_memory_retention(settings: Settings, engine: AsyncEngine) -> MemoryRetention:
    """The retention sweep, for the ingest process."""
    return MemoryRetention(
        PostgresMemoryStore(engine), timedelta(days=settings.memory_retention_days)
    )


def build_voice_questions(
    settings: Settings,
    engine: AsyncEngine,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = utc_now,
) -> VoiceQuestions | None:
    """Voice questions in DMs, or None when the switch is off.

    None rather than a service that refuses: nothing is built that could reach
    the transcription endpoint, and the bot answers a voice message that voice
    is not enabled here. `transport` is the process's `Edges.http_transport`,
    for the CDN download and the transcription alike, so a test that fakes the
    network fakes both.
    """
    if not settings.voice_questions_enabled or settings.media_api_key is None:
        return None
    limits = VoiceLimits(
        max_seconds=settings.voice_max_seconds,
        max_bytes=settings.voice_max_bytes,
        person_monthly_seconds=settings.voice_person_monthly_minutes * 60,
        overall_monthly_seconds=settings.media_audio_monthly_minutes * 60,
    )
    log.info(
        "composition.voice_questions",
        model=settings.media_audio_model,
        max_seconds=limits.max_seconds,
        person_monthly_minutes=settings.voice_person_monthly_minutes,
        monthly_minutes=settings.media_audio_monthly_minutes,
    )
    return VoiceQuestions(
        OpenAICompatibleTranscriber(
            base_url=settings.media_base_url,
            api_key=settings.media_api_key.get_secret_value(),
            model=settings.media_audio_model,
            timeout_seconds=settings.media_timeout_seconds,
            transport=transport,
        ),
        BoundedHttpFetcher(transport=transport, follow_redirects=False),
        PostgresVoiceLedger(engine),
        limits,
        fetch_timeout=settings.media_timeout_seconds,
        clock=clock,
    )


def build_personal_facts(engine: AsyncEngine) -> PersonalFactsService:
    """Personal facts for the bot, over the answer stack's engine.

    The same engine memory uses, so a fact and a remembered turn for one
    account resolve to one person and are purged together.
    """
    return PersonalFactsService(PostgresFactStore(engine))


def build_live_scope(
    settings: Settings, engine: AsyncEngine, environ: Mapping[str, str]
) -> LiveScope:
    """Indexing scope as stored configuration says it is now.

    The same construction ingest uses, so the bot and the MCP server agree
    with it about which channels exist. Not yet refreshed: the caller awaits
    `refresh()` before serving, then runs the loop, because a process that
    answers from the environment's scope for its first period would show a
    channel an operator already removed.
    """
    return LiveScope.from_settings(PostgresConfigurationStore(engine), settings, environ)


def build_ask_service(
    settings: Settings,
    guild: GuildProvider,
    answers: AnswerService,
    search: SearchBackend | None = None,
    confirmations: ConfirmationLedger | None = None,
    conversations: Conversations | None = None,
    scope: ScopeProvider | None = None,
    facts: PersonalFactsService | None = None,
    catchup: CatchUpService | None = None,
    alerts: AlertRequests | None = None,
    said_by: SaidByService | None = None,
) -> AskService:
    """The Discord-facing use case, over whichever answer service it is given.

    `guild` is late-bound because permissions are resolved from live guild
    state that does not exist until the gateway connects; a cold cache reads
    as an empty guild, so resolution fails closed during startup.

    `scope` is the provider both resolvers ask on every resolution. None falls
    back to the environment's fixed set, which is for callers with no
    database; the bot process passes its `LiveScope`, and
    `test_scope_and_market_wiring` proves that from the entrypoint down.
    """
    indexed = scope if scope is not None else StaticScope(settings.indexed_channel_ids)
    return AskService(
        acl=DiscordAclResolver(guild, indexed),
        audiences=DiscordAudienceResolver(guild, indexed),
        answers=answers,
        limiter=RateLimiter(),
        # None only for a caller that builds a surface without a database. The
        # bot process passes one; `test_memory_integration` proves it from the
        # entrypoint down.
        conversations=conversations,
        # Reads the same cached member the permission resolver reads, and only
        # ever for the asker: there is no call here that takes anyone else.
        profiles=DiscordProfileResolver(guild),
        # The asker's own facts: set from their own message, shown only to
        # them, and rendered into the prompt beside the profile. None answers
        # every fact request that this deployment keeps none.
        facts=facts,
        # "What did I miss in #x", summarised from the same viewer-scoped
        # retrieval every answer uses. None answers such a question by
        # searching the corpus for its words instead -- which is a worse
        # answer under exactly the same access, never a wider one.
        catchup=catchup,
        # "What did Ana say about X", from Ana's own messages under the same
        # viewer. None answers it by the ordinary search, as before.
        said_by=said_by,
        # Without this the withheld-evidence notice is built, tested, and
        # structurally unable to fire: retrieval is pre-scoped to
        # asker INTERSECT audience, so nothing is ever dropped later for the
        # notice to report. The probe is what actually searches the gap.
        withheld=WithheldRetrieval(search) if search is not None else None,
        # The desk writes into the federation's own ledger, which is the one
        # the invoke-time gate reads. A desk over a ledger of its own would
        # collect approvals nothing ever checks -- and a deployment with no
        # federation has no mutating tool to confirm, so it gets no desk and
        # every prompt would be refused for want of one.
        desk=ConfirmationDesk(confirmations) if confirmations is not None else None,
        # Alert requests turned into proposals to confirm. None answers them
        # that alerts are not available here, never with a corpus search.
        alerts=alerts,
    )
