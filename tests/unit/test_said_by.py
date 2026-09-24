"""The said-by route: what is recognised, who it resolves to, what is said back.

The parser half is a labelled eval, in both languages, including subjects
that are not people ("what did the docs say") and questions another route
owns ("o que o João me pediu", "o que eu perdi"). The service half runs over
fakes that record every viewer and query, so what is asserted is what the
route *asks for* -- the store's side of it is in
tests/integration/test_author_search.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from chatmemory.app.language import Language
from chatmemory.app.people import matching
from chatmemory.app.reasoning.contract import DEPENDENCY_FAILED_TEXT
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import Grounded, PromptContext, RetrievalResult
from chatmemory.app.said_by import (
    PersonSlot,
    SaidByService,
    SlotKind,
    said_by_request,
    span_words,
)
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import PersonCandidate, RelevanceSource, SearchQuery
from chatmemory.ports.answers import Question

TZ = ZoneInfo("America/Sao_Paulo")
#: Thursday 24 September 2026, 15:00 UTC -- noon in Sao Paulo.
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)

GENERAL = ChannelRef("discord", 100)
LEADERSHIP = ChannelRef("discord", 300)
ASKER = PersonRef("discord", 7)
ANA = PersonCandidate(PersonRef("discord", 21), "Ana Souza")
JOAO_SILVA = PersonCandidate(PersonRef("discord", 31), "João Silva")
JOAO_PEREIRA = PersonCandidate(PersonRef("discord", 32), "Joao Pereira")


def parse(text: str) -> tuple[PersonSlot, str, str | None, Language] | None:
    request = said_by_request(text, NOW, TZ)
    if request is None:
        return None
    return request.person, request.topic, request.span.label if request.span else None, (
        request.language
    )


def name(value: str) -> PersonSlot:
    return PersonSlot(SlotKind.NAME, name=value)


SELF = PersonSlot(SlotKind.SELF)
PT, EN = Language.PORTUGUESE, Language.ENGLISH

# --- 3.4 the labelled eval ------------------------------------------------

RECOGNISED = [
    ("o que o João disse sobre o deploy semana passada?", name("João"), "o deploy",
     "last_week", PT),
    ("what did Ana say about pricing yesterday?", name("Ana"), "pricing", "yesterday", EN),
    ("o que eu falei sobre X?", SELF, "X", None, PT),
    ("<@123> comentou algo sobre Y ontem?", PersonSlot(SlotKind.MENTION, user_id=123), "Y",
     "yesterday", PT),
    ("<@!123> falou sobre o deploy?", PersonSlot(SlotKind.MENTION, user_id=123), "o deploy",
     None, PT),
    ("oque o joao falou do deploy", name("joao"), "deploy", None, PT),
    ("o q a Ana escreveu sobre a migração hoje", name("Ana"), "a migração", "today", PT),
    ("o que a Ana achou do novo layout?", name("Ana"), "novo layout", None, PT),
    ("o que o João Silva disse ontem sobre o deploy?", name("João Silva"), "o deploy",
     "yesterday", PT),
    ("o que o João disse sobre o deploy da semana passada?", name("João"), "o deploy",
     "last_week", PT),
    ("o que o Leo disse este mês?", name("Leo"), "", "this_month", PT),
    ("a Ana comentou alguma coisa sobre o preço?", name("Ana"), "o preço", None, PT),
    ("What did I say about the release?", SELF, "the release", None, EN),
    ("did Leo mention anything about the migration last week?", name("Leo"), "the migration",
     "last_week", EN),
    ("what has Leo written about the roadmap this month?", name("Leo"), "the roadmap",
     "this_month", EN),
    ("what did Ana say", name("Ana"), "", None, EN),
    ("what does Bea think about the rollout?", name("Bea"), "the rollout", None, EN),
    ("what did Ana say about pricing since monday?", name("Ana"), "pricing", "since:monday", EN),
    # About the record, not a live figure: the market route declines it.
    ("what did Leo say about the bitcoin price today?", name("Leo"), "the bitcoin price",
     "today", EN),
]


@pytest.mark.parametrize(("text", "person", "topic", "span", "language"), RECOGNISED)
def test_recognised(
    text: str, person: PersonSlot, topic: str, span: str | None, language: Language
) -> None:
    assert parse(text) == (person, topic, span, language)


NOT_RECOGNISED = [
    # Not a person, or not one person.
    "what did the docs say about X",
    "what did you say about X",
    "o que ele disse",
    "o que o pessoal falou sobre o deploy?",
    "what did Ana and Bea say about X",
    "what did everyone say about pricing",
    # Another route owns it.
    "o que o João me pediu",
    "what did Ana ask me to do?",
    "o que eu perdi no <#100>",
    "what did I miss in <#100>?",
    # More than one thing, or more than one span.
    "what did Ana say about pricing and whether Bea replied?",
    "o que o João disse desde segunda até quarta?",
    "o que o João disse hoje e ontem?",
    # A shape the route cannot answer faithfully.
    "what did Ana say in #general about pricing",
    "eu falei sobre isso ontem",
    # Ordinary questions.
    "what is the deploy plan?",
    "quando o deploy vai sair?",
    "who said the deploy is on friday?",
]


@pytest.mark.parametrize("text", NOT_RECOGNISED)
def test_not_recognised(text: str) -> None:
    assert parse(text) is None


def test_the_span_is_the_deployments_calendar_week() -> None:
    request = said_by_request("o que o João disse semana passada?", NOW, TZ)
    assert request is not None and request.span is not None
    # Monday 14 to Monday 21 September, local midnights (UTC-3).
    assert request.span.start == datetime(2026, 9, 14, 3, 0, tzinfo=UTC)
    assert request.span.end == datetime(2026, 9, 21, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("o que o João disse semana passada?", PT, " de 14/09 a 20/09"),
        ("what did Ana say last week?", EN, " from 14 Sep to 20 Sep"),
        ("o que o João disse ontem?", PT, " em 23/09"),
        ("what did Ana say since monday?", EN, " since 21 Sep"),
    ],
)
def test_the_span_is_stated_in_local_dates(text: str, language: Language, expected: str) -> None:
    request = said_by_request(text, NOW, TZ)
    assert request is not None
    assert span_words(request.span, TZ, language) == expected


# --- name matching --------------------------------------------------------


def test_an_exact_full_name_beats_a_first_name() -> None:
    people = [JOAO_SILVA, JOAO_PEREIRA, PersonCandidate(PersonRef("discord", 33), "João Silveira")]
    assert matching("joão silva", people, 6) == [JOAO_SILVA]


def test_a_first_name_matches_everyone_who_has_it_accents_aside() -> None:
    assert matching("Joao", [JOAO_SILVA, JOAO_PEREIRA, ANA], 6) == [JOAO_PEREIRA, JOAO_SILVA]


def test_a_prefix_and_a_surname_match() -> None:
    leo = PersonCandidate(PersonRef("discord", 41), "Leonardo Araujo")
    assert matching("Leo", [leo, ANA], 6) == [leo]
    assert matching("Souza", [leo, ANA], 6) == [ANA]
    assert matching("L", [leo], 6) == [], "one letter matches half the server"


def test_the_list_is_capped() -> None:
    many = [PersonCandidate(PersonRef("discord", i), f"Ana {i}") for i in range(10)]
    assert len(matching("ana", many, 6)) == 6


# --- 3.2 / 3.3 the service ----------------------------------------------------


def question(
    text: str,
    visible: frozenset[ChannelRef] = frozenset({GENERAL, LEADERSHIP}),
    readable: frozenset[ChannelRef] = frozenset({GENERAL, LEADERSHIP}),
) -> Question:
    return Question(
        text=text,
        asker=Viewer(ASKER, visible),
        audience=Audience(
            mode=DeliveryMode.DIRECT_MESSAGE, members=frozenset({ASKER}), readable_channels=readable
        ),
    )


def evidence(text: str, window_id: int = 42, author: str = "Ana Souza") -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=GENERAL,
        text=text,
        score=0.9,
        relevance_source=RelevanceSource.VECTOR,
        url=f"https://discord.com/channels/1/100/{window_id}",
        author_display=author,
        message_ids=(window_id,),
    )


class People:
    def __init__(self, *found: PersonCandidate) -> None:
        self.found = found
        self.viewers: list[Viewer] = []

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = 6
    ) -> Sequence[PersonCandidate]:
        self.viewers.append(viewer)
        return self.found[:limit]


class Retrieval:
    def __init__(self, *items: Evidence, fail: bool = False) -> None:
        self.items = items
        self.fail = fail
        self.viewers: list[Viewer] = []
        self.queries: list[SearchQuery] = []

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        self.viewers.append(viewer)
        self.queries.append(query)
        if self.fail:
            raise RetrievalUnavailable("down")
        return RetrievalResult(items=self.items)


class Synthesizer:
    def __init__(self, cite: bool = True) -> None:
        self.questions: list[str] = []
        self.cite = cite

    async def synthesize(
        self, question: str, evidence: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        self.questions.append(question)
        ids = tuple(e.window_id for e in evidence) if self.cite else ()
        return Grounded(text="Ana said pricing goes up.", cited_window_ids=ids)


def service(
    people: People, retrieval: Retrieval, synthesizer: Synthesizer | None = None
) -> SaidByService:
    return SaidByService(
        retrieval,
        synthesizer or Synthesizer(),
        people,
        tz=TZ,
        clock=lambda: NOW,
    )


async def ask(built: SaidByService, text: str, **kwargs: frozenset[ChannelRef]):  # type: ignore[no-untyped-def]
    request = built.recognise(text)
    assert request is not None
    return await built.answer(question(text, **kwargs), request)


async def test_a_unique_person_is_searched_by_author_topic_and_span() -> None:
    people, synth = People(ANA), Synthesizer()
    retrieval = Retrieval(evidence("Ana: pricing goes up"))

    outcome = await ask(service(people, retrieval, synth), "o que a Ana disse sobre pricing ontem?")

    assert outcome.decision.outcome == "resolved" and outcome.decision.model_calls == 0
    [query] = retrieval.queries
    assert query.authors == frozenset({ANA.ref})
    assert query.text == "pricing"
    assert query.since == datetime(2026, 9, 23, 3, 0, tzinfo=UTC)
    assert query.until == datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
    assert outcome.answer is not None
    assert outcome.answer.text.startswith("-# O que Ana Souza disse sobre pricing em 23/09:")
    assert [c.message_id for c in outcome.answer.citations] == [42]
    assert "Ana Souza" in synth.questions[0] and "Attribute nothing to anyone else" in (
        synth.questions[0]
    )


async def test_resolution_and_retrieval_run_as_the_asker_narrowed_by_the_room() -> None:
    people, retrieval = People(ANA), Retrieval(evidence("Ana: pricing"))

    await ask(
        service(people, retrieval),
        "what did Ana say about pricing?",
        readable=frozenset({GENERAL}),
    )

    narrowed = frozenset({GENERAL})
    assert people.viewers[0].visible_channels == narrowed
    assert retrieval.viewers[0].visible_channels == narrowed


async def test_several_candidates_ask_which_with_no_search_and_no_model() -> None:
    people, retrieval, synth = People(JOAO_SILVA, JOAO_PEREIRA), Retrieval(), Synthesizer()

    outcome = await ask(service(people, retrieval, synth), "o que o João disse?")

    assert outcome.decision.outcome == "ambiguous"
    assert outcome.answer is not None
    assert outcome.answer.text == (
        "Qual João? João Silva, Joao Pereira. Mencione a pessoa com @ ou use o nome completo."
    )
    assert outcome.answer.consulted_channels is None, "not remembered"
    assert retrieval.queries == [] and synth.questions == []


async def test_the_which_reply_is_in_english_for_an_english_question() -> None:
    built = service(People(JOAO_SILVA, JOAO_PEREIRA), Retrieval())
    outcome = await ask(built, "what did João say?")
    assert outcome.answer is not None
    assert outcome.answer.text.startswith("Which João? João Silva, Joao Pereira.")


async def test_nobody_by_that_name_falls_through() -> None:
    retrieval = Retrieval()
    outcome = await ask(service(People(), retrieval), "what did Zed say about pricing?")
    assert outcome.answer is None and outcome.decision.outcome == "fallback"
    assert retrieval.queries == []


async def test_the_asker_and_a_mention_need_no_lookup() -> None:
    people, retrieval = People(ANA), Retrieval(evidence("Ana: x", author="Ana Souza"))
    built = service(people, retrieval)

    mine = await ask(built, "o que eu disse sobre pricing?")
    mentioned = await ask(built, "what did <@21> say about pricing?")

    assert people.viewers == []
    assert [q.authors for q in retrieval.queries] == [
        frozenset({ASKER}),
        frozenset({PersonRef("discord", 21)}),
    ]
    assert mine.answer is not None and mine.answer.text.startswith("-# O que você disse")
    assert mentioned.answer is not None
    assert mentioned.answer.text.startswith("-# What Ana Souza said about pricing:")


async def test_nothing_found_is_one_sentence_whoever_it_is_about() -> None:
    by_name = await ask(service(People(ANA), Retrieval()), "what did Ana say about pricing?")
    by_mention = await ask(service(People(), Retrieval()), "what did <@21> say about pricing?")

    assert by_name.answer is not None and by_mention.answer is not None
    assert by_name.answer.text == (
        "I found nothing Ana Souza said about pricing in the channels I can search here."
    )
    assert by_mention.answer.text == (
        "I found nothing <@21> said about pricing in the channels I can search here."
    )


async def test_nothing_the_model_could_cite_reads_as_nothing_found() -> None:
    outcome = await ask(
        service(People(ANA), Retrieval(evidence("Ana: lunch")), Synthesizer(cite=False)),
        "o que a Ana disse sobre pricing?",
    )
    assert outcome.answer is not None
    assert outcome.answer.text == (
        "Não encontrei nada que Ana Souza tenha dito sobre pricing nos canais que posso "
        "consultar aqui."
    )


async def test_a_retrieval_failure_is_a_failure_not_silence() -> None:
    outcome = await ask(
        service(People(ANA), Retrieval(fail=True)), "what did Ana say about pricing?"
    )
    assert outcome.answer is not None
    assert outcome.answer.text == DEPENDENCY_FAILED_TEXT


async def test_a_failed_name_lookup_is_a_failure_not_a_fall_through() -> None:
    class Broken(People):
        async def people_named(
            self, viewer: Viewer, name: str, limit: int = 6
        ) -> Sequence[PersonCandidate]:
            raise OSError("database down")

    outcome = await ask(service(Broken(), Retrieval()), "what did Ana say about pricing?")
    assert outcome.answer is not None and outcome.answer.text == DEPENDENCY_FAILED_TEXT
    assert outcome.decision.outcome == "unavailable"


async def test_no_topic_asks_for_everything_they_said_in_the_span() -> None:
    retrieval = Retrieval(evidence("Ana: hi"))
    await ask(service(People(ANA), retrieval), "o que a Ana disse hoje?")
    [query] = retrieval.queries
    assert query.text == "" and query.since == datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
