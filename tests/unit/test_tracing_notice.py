"""The one-time "your questions are recorded" notice, and the capabilities line.

Shown on a person's first traced reply per notice version, in their language,
after the answer and its sources; never to an opted-out person, never where
tracing is off, never on a reply that is not traced, and never as part of the
answer that is remembered.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.adapters.discord.bot import _render
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.disclosure import ScopedAnswer
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.language import Language
from chatmemory.app.limits import RateLimiter
from chatmemory.app.self_description import Capabilities
from chatmemory.app.tracing_notice import (
    NOTICE_VERSION,
    TracingNotice,
    notice_text,
    recording_statement,
)
from chatmemory.composition import build_tracing_notice, deployment_capabilities
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.answers import Answer, Citation
from chatmemory.ports.facts import FactKind, PersonalFact
from chatmemory.ports.memory import ConversationLocation, Recollection
from tests.unit.test_ask import (
    GENERAL,
    LEAD,
    LEADERSHIP,
    RecordingAnswerService,
    ch,
    guild,
    person,
)
from tests.unit.test_facts import FakeFactStore

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)

EN_NOTICE = (
    "**Note:** Your questions and my answers are recorded for up to 90 days, and "
    "CyberFriend admins can read them. Use `/privacy` to see how many are kept or "
    "to delete them."
)
PT_NOTICE = (
    "**Aviso:** Suas perguntas e minhas respostas ficam registradas por até 90 dias, "
    "e os administradores do CyberFriend podem lê-las. Use `/privacy` para ver "
    "quantas estão guardadas ou apagá-las."
)


class FakeNoticeStore:
    """`TracingNoticeStore` in memory, with the same once-per-version rule."""

    def __init__(self, opted_out: frozenset[PersonRef] = frozenset()) -> None:
        self.versions: dict[PersonRef, int] = {}
        self.at: dict[PersonRef, datetime] = {}
        self.opted_out = opted_out
        self.fail = False

    async def claim_notice(self, person: PersonRef, version: int, now: datetime) -> bool:
        if self.fail:
            raise ConnectionError("database unavailable")
        if person in self.opted_out or self.versions.get(person, 0) >= version:
            return False
        self.versions[person] = version
        self.at[person] = now
        return True


class RecordingConversations:
    """`Conversations` that stores nothing and keeps each remembered answer."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def recall(self, viewer: Viewer, location: ConversationLocation) -> Recollection:
        return Recollection()

    async def remember(
        self,
        viewer: Viewer,
        location: ConversationLocation,
        question: str,
        answer: Answer,
        *,
        informed_by: Recollection,
    ) -> bool:
        self.answers.append(answer.text)
        return True


def build(
    store: FakeNoticeStore | None = None,
    *,
    version: int = NOTICE_VERSION,
    memory: RecordingConversations | None = None,
    facts: FakeFactStore | None = None,
) -> tuple[AskService, FakeNoticeStore]:
    store = store or FakeNoticeStore()
    g = guild()
    indexed = (GENERAL, LEADERSHIP)
    notice = TracingNotice(store, 90, clock=lambda: NOW, version=version)
    asks = AskService(
        acl=DiscordAclResolver(g, indexed),
        audiences=DiscordAudienceResolver(g, indexed),
        answers=RecordingAnswerService(),  # type: ignore[arg-type]
        limiter=RateLimiter(),
        conversations=memory,  # type: ignore[arg-type]
        facts=PersonalFactsService(facts) if facts is not None else None,  # type: ignore[arg-type]
        tracing_notice=notice,
    )
    return asks, store


def dm(text: str, who: int = LEAD) -> AskRequest:
    return AskRequest(person(who), text, None, location_id=who)


# --- the texts ---------------------------------------------------------------


def test_the_notice_texts() -> None:
    assert notice_text(Language.ENGLISH, 90) == EN_NOTICE
    assert notice_text(Language.PORTUGUESE, 90) == PT_NOTICE
    # A language the notice is not written in gets English.
    assert notice_text(Language.UNKNOWN, 90) == EN_NOTICE


def test_the_period_comes_from_the_setting() -> None:
    assert "up to 30 days" in recording_statement(Language.ENGLISH, 30)
    assert "up to 1 day," in recording_statement(Language.ENGLISH, 1)
    assert "por até 1 dia," in recording_statement(Language.PORTUGUESE, 1)


# --- shown once per version --------------------------------------------------


async def test_the_first_reply_carries_it_and_later_ones_do_not() -> None:
    asks, store = build()

    first = await asks.ask(dm("what happened?"))
    second = await asks.ask(dm("and then?"))

    assert first.scoped is not None and second.scoped is not None
    assert first.scoped.notice == EN_NOTICE
    assert second.scoped.notice == ""
    assert store.versions[person(LEAD)] == NOTICE_VERSION
    assert store.at[person(LEAD)] == NOW


async def test_a_new_version_is_shown_again_once() -> None:
    asks, store = build()
    await asks.ask(dm("what happened?"))

    newer, _ = build(store, version=NOTICE_VERSION + 1)
    again = await newer.ask(dm("what happened?"))
    after = await newer.ask(dm("what happened?"))

    assert again.scoped is not None and after.scoped is not None
    assert again.scoped.notice == EN_NOTICE
    assert after.scoped.notice == ""


async def test_a_channel_reply_carries_it_too() -> None:
    asks, _ = build()
    outcome = await asks.ask(
        AskRequest(person(LEAD), "what happened?", ch(GENERAL), location_id=GENERAL)
    )
    assert outcome.scoped is not None
    assert outcome.scoped.notice == EN_NOTICE


async def test_in_portuguese_for_a_portuguese_question() -> None:
    asks, _ = build()
    outcome = await asks.ask(dm("o que aconteceu com o deploy?"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == PT_NOTICE


async def test_a_message_with_no_language_gets_english() -> None:
    """No saved preferred language here, so the fallback is English."""
    asks, _ = build()
    outcome = await asks.ask(dm("ok"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == EN_NOTICE


async def test_a_message_with_no_language_uses_the_saved_one() -> None:
    """Nothing to detect in "ok", so the person's saved language decides."""
    facts = FakeFactStore()
    await facts.set_fact(person(LEAD), PersonalFact(FactKind.PREFERRED_LANGUAGE, "pt"))
    asks, _ = build(facts=facts)
    outcome = await asks.ask(dm("ok"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == PT_NOTICE


# --- and only where it is true ------------------------------------------------


async def test_never_to_an_opted_out_person() -> None:
    asks, store = build(FakeNoticeStore(opted_out=frozenset({person(LEAD)})))
    outcome = await asks.ask(dm("what happened?"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == ""
    assert person(LEAD) not in store.versions


async def test_never_where_tracing_is_off() -> None:
    """No notice is built with tracing off, and a service without one shows none."""
    settings = Settings.model_validate(
        {
            "discord_token": "t",
            "discord_guild_id": 1,
            "database_url": "postgresql+asyncpg://u:p@h/d",
            "llm_api_key": "k",
        }
    )
    assert build_tracing_notice(settings, object()) is None  # type: ignore[arg-type]
    on = settings.model_copy(update={"tracing_enabled": True})
    assert isinstance(build_tracing_notice(on, object()), TracingNotice)  # type: ignore[arg-type]

    g = guild()
    asks = AskService(
        acl=DiscordAclResolver(g, (GENERAL,)),
        audiences=DiscordAudienceResolver(g, (GENERAL,)),
        answers=RecordingAnswerService(),  # type: ignore[arg-type]
        limiter=RateLimiter(),
    )
    outcome = await asks.ask(dm("what happened?"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == ""


async def test_not_on_a_reply_that_is_not_traced() -> None:
    """A typed slash command never reaches the answer path or its tracer."""
    asks, store = build()
    outcome = await asks.ask(dm("/forget"))
    assert outcome.scoped is not None
    assert outcome.scoped.notice == ""
    assert person(LEAD) not in store.versions


async def test_not_on_a_scheduled_run() -> None:
    """Its delivery sends the answer alone, so claiming it there would lose it."""
    asks, store = build()
    outcome = await asks.ask(dm("what happened?"), metered=False)
    assert outcome.scoped is not None
    assert outcome.scoped.notice == ""
    assert person(LEAD) not in store.versions


async def test_a_store_failure_costs_the_notice_not_the_answer() -> None:
    store = FakeNoticeStore()
    store.fail = True
    asks, _ = build(store)
    outcome = await asks.ask(dm("what happened?"))
    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == "here you go"
    assert outcome.scoped.notice == ""


async def test_the_answer_itself_is_left_alone() -> None:
    """The notice rides beside the answer, so memory and the trace never hold it."""
    asks, _ = build()
    outcome = await asks.ask(dm("what happened?"))
    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == "here you go"


async def test_the_remembered_turn_never_holds_it() -> None:
    """On the very reply that carries it, the stored turn is the answer alone."""
    remembered = RecordingConversations()
    asks, _ = build(memory=remembered)

    outcome = await asks.ask(dm("what happened?"))

    assert outcome.scoped is not None and outcome.scoped.notice == EN_NOTICE
    assert remembered.answers == ["here you go"]


# --- where Discord shows it --------------------------------------------------


def test_discord_shows_it_after_the_sources() -> None:
    citation = Citation(ch(GENERAL), 1, "Ana", "text", "https://discord.com/1")
    rendered = _render(
        ScopedAnswer(
            Answer("the deploy was rolled back", citations=(citation,)),
            frozenset(),
            notice=EN_NOTICE,
        )
    )
    assert rendered.startswith("the deploy was rolled back")
    assert rendered.endswith("\n\n" + EN_NOTICE)
    assert rendered.index("discord.com/1") < rendered.index(EN_NOTICE)


def test_discord_shows_nothing_extra_without_one() -> None:
    rendered = _render(ScopedAnswer(Answer("hello"), frozenset()))
    assert rendered == "hello"


# --- the capabilities reply --------------------------------------------------


def test_the_capabilities_reply_states_it_where_tracing_is_on() -> None:
    caps = Capabilities(trace_retention_days=90)
    english = caps.describe(Language.ENGLISH, direct_message=True)
    portuguese = caps.describe(Language.PORTUGUESE, direct_message=True)

    assert (
        "**Privacy**\n• Your questions and my answers are recorded for up to 90 days, "
        "and CyberFriend admins can read them. Use `/privacy` to see how many are "
        "kept or to delete them."
    ) in english.split("\n\n")
    assert (
        "**Privacidade**\n• Suas perguntas e minhas respostas ficam registradas por "
        "até 90 dias, e os administradores do CyberFriend podem lê-las. Use "
        "`/privacy` para ver quantas estão guardadas ou apagá-las."
    ) in portuguese.split("\n\n")
    # Kept in a DM, where guild-only commands are dropped.
    assert caps.offered(direct_message=True).trace_retention_days == 90


def test_the_capabilities_reply_says_nothing_of_it_where_tracing_is_off() -> None:
    base = {
        "discord_token": "t",
        "discord_guild_id": 1,
        "database_url": "postgresql+asyncpg://u:p@h/d",
        "llm_api_key": "k",
        "trace_retention_days": 30,
    }
    off = deployment_capabilities(Settings.model_validate(base))
    on = deployment_capabilities(Settings.model_validate({**base, "tracing_enabled": True}))

    assert off.trace_retention_days is None
    assert "recorded" not in off.describe(Language.ENGLISH)
    assert on.trace_retention_days == 30
    assert "recorded for up to 30 days" in on.describe(Language.ENGLISH)
