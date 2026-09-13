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
from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.app.ask import AskService
from chatmemory.app.conversation import ConversationStore
from chatmemory.app.limits import RateLimiter
from chatmemory.app.reasoning.capabilities import LOOP_STAGES, ModelCapability, Stage
from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.ports import ChatModel, RetrievalTool
from chatmemory.app.reasoning.retrieval import CorpusRetrieval, discord_urls
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
    """

    engine: AsyncEngine
    search: SearchBackend
    chat: ChatModel
    answers: ReasoningAnswerService


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


def build_answers(retrieval: RetrievalTool, chat: ChatModel) -> ReasoningAnswerService:
    """The real answer service: both paths, over one retrieval tool."""
    return build_answer_service(retrieval, chat)


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
    log.info(
        "composition.answer_stack",
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        stages=[str(s) for s in ANSWERING_STAGES],
    )
    return AnswerStack(
        engine=engine, search=search, chat=chat, answers=build_answers(retrieval, chat)
    )


def build_ask_service(
    settings: Settings, guild: GuildProvider, answers: AnswerService
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
    )
