"""What one person said: "o que o João disse sobre o deploy semana passada?".

Like catch-up, not a second way into the corpus. It is the ordinary
retrieval and the ordinary synthesiser with three inputs pinned by a parser
that makes no model call: whose words (`SearchQuery.authors`), when (a
`timespan.Span`) and about what (the topic, embedded once). The viewer is the
same asker-intersected-with-audience every answer runs under, and the store
binds it into the same statement as the author and the span, so naming a
person can only narrow what the asker could already read.

Three decisions are worth stating, because each one is a place this route
could have become a disclosure.

**A name is resolved only among people the room can see speak.** Candidates
come from `SearchBackend.people_named` under the retrieval viewer, so "Qual
João?" never lists somebody who only speaks in a channel the asker -- or
anyone else who will read the reply -- cannot read. No candidate at all is
not answered here: the question falls through to the ordinary route, exactly
as it did before this existed, which is also what keeps "what did the docs
say about X" working.

**One empty reply.** Nothing said in the span, said only in private
channels, opted out, deleted: all four read the same sentence, and a mention
of somebody who never spoke where the asker can read gets it too.

**The model reads only the person's own lines.** Each hit carries just their
messages from a conversation, so a colleague's reply in the same window can
neither reach the prompt nor be attributed to them.

No withheld-evidence probe runs for this route, as for catch-up: "there is
more in your DMs" about a named person is itself a statement about them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Protocol

import structlog

from chatmemory.app.catchup import catch_up_request
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language
from chatmemory.app.reasoning.budgets import Budget, BudgetLedger
from chatmemory.app.reasoning.contract import Decision, DecisionMaker, failure_answer
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import EvidenceLedger
from chatmemory.app.reasoning.fixed import consulted_channels, write_answer
from chatmemory.app.reasoning.ports import RetrievalResult, RetrievalTool, Synthesizer
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.app.routing import (
    fact_intent,
    market_question,
    obligation_question,
    single_lookup,
)
from chatmemory.app.timespan import Span, fold, parse_span, span_phrases
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.domain.search import PersonCandidate, SearchQuery
from chatmemory.ports.answers import Answer, Question

log = structlog.get_logger()

SAID_BY = "SAID_BY"
"""The route label, for provenance and for the planned unified router."""

MAX_CANDIDATES = 6
"""How many people a "which one?" reply lists at most."""


# --- recognising the request --------------------------------------------
#
# Matched against folded text (lowercase, accents stripped), with every time
# phrase blanked out first so "ontem" can sit anywhere in the sentence. The
# slot and topic are then cut from the original text at the same offsets,
# so a name keeps the accents and capitals it was typed with.


class SlotKind(StrEnum):
    MENTION = "mention"
    SELF = "self"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class PersonSlot:
    """Who the question is about, as written: a mention, the asker, or a name."""

    kind: SlotKind
    name: str = ""
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class SaidByRequest:
    """A recognised "what did X say" question. A dataclass the unified router
    can absorb as it is."""

    person: PersonSlot
    #: What it was about, as typed; empty when the question named no topic.
    topic: str
    #: When; None when the question named no time, which searches all of it.
    span: Span | None
    #: The language the question's shape was in, which the fixed replies use.
    language: Language


_SLOT = r"(?P<slot><@!?\d+>|[a-z][\w.'-]*(?:\s+[a-z][\w.'-]*){0,2})"
_END = r"\s*[?.!]*\s*$"
#: Third person, and first person for "o que eu falei".
_PT_VERBS = (
    r"disse|falou|comentou|escreveu|mencionou|postou|contou|achou|opinou"
    r"|falei|comentei|escrevi|mencionei|postei|contei|achei"
)
_PT_WHAT = r"(?:e\s+)?(?:o\s*que|oq|o\s+q)(?:\s+(?:e\s+)?que)?"
_PT_ABOUT = (
    r"(?:sobre|de|do|da|dos|das|no|na|a\s+respeito\s+d[aeo]s?|acerca\s+d[aeo]s?"
    r"|em\s+relacao\s+a[os]?|quanto\s+a[os]?)"
)
_PT_SOMETHING = r"(?:algo|alguma\s+coisa|nada)"
_EN_VERBS = (
    r"say|said|write|wrote|written|mention|mentioned|post|posted|comment|commented"
    r"|think|thought"
)
_EN_ABOUT = r"(?:about|on|regarding|concerning)"
_EN_SOMETHING = r"(?:anything|something)"

_SHAPES: tuple[tuple[re.Pattern[str], Language], ...] = (
    # "o que o João disse (sobre Y)", "oque a Ana achou do Y", "o que eu falei"
    (
        re.compile(
            rf"^{_PT_WHAT}\s+(?:(?:o|a)\s+)?{_SLOT}\s+(?:{_PT_VERBS})"
            rf"(?:\s+{_PT_SOMETHING})?(?:\s+{_PT_ABOUT}\s+(?P<topic>.+?))?{_END}"
        ),
        Language.PORTUGUESE,
    ),
    # "@Maria comentou algo sobre Y?" -- a question mark and a topic required,
    # or "eu falei sobre isso ontem" as a statement would be read as a search.
    (
        re.compile(
            rf"^(?:e\s+)?(?:(?:o|a)\s+)?{_SLOT}\s+(?:{_PT_VERBS})"
            rf"(?:\s+{_PT_SOMETHING})?\s+{_PT_ABOUT}\s+(?P<topic>.+?)\s*\?[?.!\s]*$"
        ),
        Language.PORTUGUESE,
    ),
    # "what did Ana say (about Y)", "what has Leo written about Y"
    (
        re.compile(
            rf"^(?:so\s+|and\s+)?what\s+(?:did|does|has|have|had)\s+{_SLOT}\s+(?:{_EN_VERBS})"
            rf"(?:\s+{_EN_SOMETHING})?(?:\s+{_EN_ABOUT}\s+(?P<topic>.+?))?{_END}"
        ),
        Language.ENGLISH,
    ),
    # "did Ana say anything about Y?"
    (
        re.compile(
            rf"^(?:did|has|have)\s+{_SLOT}\s+(?:{_EN_VERBS})\s+{_EN_SOMETHING}"
            rf"\s+{_EN_ABOUT}\s+(?P<topic>.+?){_END}"
        ),
        Language.ENGLISH,
    ),
)

_MENTION = re.compile(r"<@!?(\d{1,25})>")
_SELF_WORDS = frozenset({"eu", "i", "me", "mim"})

#: Words that make the slot not one person: pronouns the asker's context would
#: have to resolve, groups, things, and "and"/"e", which names two people.
#: Anything else that is not a person simply resolves to nobody and falls
#: through, so this list only has to catch what could match a real name.
_NOT_A_PERSON = frozenset(
    {
        *("the", "a", "an", "this", "that", "these", "those", "it", "they", "we"),
        *("you", "he", "she", "his", "her", "their", "our", "your", "my", "and"),
        *("or", "everyone", "everybody", "someone", "somebody", "anyone", "people"),
        *("team", "bot", "docs", "doc", "article", "report", "message", "channel"),
        *("o", "os", "as", "um", "uma", "ele", "ela", "eles", "elas", "voce"),
        *("voces", "vc", "vcs", "nos", "gente", "alguem", "ninguem", "todos"),
        *("todo", "pessoal", "galera", "time", "equipe", "isso", "isto", "e", "ou"),
        *("se", "documento", "canal", "mensagem", "artigo"),
    }
)

#: Asking verbs belong to obligations ("o que o João me pediu"), which are
#: answered from extracted asks, not from what somebody said.
_ASK_VERBS = re.compile(
    r"\b(?:pediu|pediram|pedir|pedindo|mandou|asked|ask|requested|request)\b"
)

#: A span cut out of "sobre o deploy da semana passada" leaves "o deploy da".
_TRAILING_LINK = re.compile(
    r"\s+(?:de|da|do|na|no|em|in|on|at|from|during|durante|this|last|of)$", re.IGNORECASE
)


def _deferred(text: str) -> bool:
    """Whether another route owns this question, or it asks more than one thing."""
    return (
        not single_lookup(text)
        or obligation_question(text) is not None
        or _ASK_VERBS.search(fold(text)) is not None
        or catch_up_request(text) is not None
        or market_question(text) is not None
        or fact_intent(text) is not None
    )


def _blank(text: str, phrases: Sequence[tuple[int, int]]) -> str:
    """`text` with each phrase replaced by spaces, so offsets stay put."""
    for start, end in phrases:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _slot(typed: str) -> PersonSlot | None:
    folded = fold(typed)
    mention = _MENTION.fullmatch(folded)
    if mention is not None:
        return PersonSlot(SlotKind.MENTION, user_id=int(mention.group(1)))
    if folded in _SELF_WORDS:
        return PersonSlot(SlotKind.SELF)
    if set(folded.split()) & _NOT_A_PERSON:
        return None
    return PersonSlot(SlotKind.NAME, name=" ".join(typed.split()))


def _topic(typed: str) -> str:
    topic = " ".join(typed.split()).strip(" ?.!,")
    return _TRAILING_LINK.sub("", topic).strip()


def _shape(folded: str) -> tuple[re.Match[str], Language] | None:
    for pattern, language in _SHAPES:
        match = pattern.match(folded)
        if match is not None:
            return match, language
    return None


def said_by_request(text: str, now: datetime, tz: tzinfo) -> SaidByRequest | None:
    """The "what did X say" question being asked, or None for everything else.

    None is the common case and the safe one: it leaves the question where it
    went before this route existed. So is a question that names a range or
    two spans, which `parse_span` refuses -- searching one end of it would be
    a narrower search than was asked.
    """
    if _deferred(text):
        return None
    typed = " ".join(text.split())
    folded = fold(typed)
    # Folding keeps offsets for anything typed on a keyboard; when it does
    # not, the slot and topic are simply cut from the folded text instead.
    source = typed if len(folded) == len(typed) else folded
    phrases = span_phrases(folded, now.astimezone(tz).date())
    span = parse_span(typed, now, tz)
    if phrases and span is None:
        return None
    found = _shape(_blank(folded, phrases))
    if found is None:
        return None
    match, language = found
    cut = _blank(source, phrases)
    person = _slot(cut[match.start("slot") : match.end("slot")])
    if person is None:
        return None
    topic = _topic(cut[match.start("topic") : match.end("topic")]) if match["topic"] else ""
    return SaidByRequest(person, topic, span, language)


# --- the replies ----------------------------------------------------------

_TEXT: dict[Language, dict[str, str]] = {
    Language.PORTUGUESE: {
        "you": "você",
        "about": " sobre {topic}",
        "on": " em {day}",
        "range": " de {first} a {last}",
        "since": " desde {first}",
        "header": "-# O que {who} disse{about}{when}:",
        "empty": (
            "Não encontrei nada que {who} tenha dito{about}{when} nos canais que "
            "posso consultar aqui."
        ),
        "which": "Qual {name}? {names}. Mencione a pessoa com @ ou use o nome completo.",
        "date": "%d/%m",
    },
    Language.ENGLISH: {
        "you": "you",
        "about": " about {topic}",
        "on": " on {day}",
        "range": " from {first} to {last}",
        "since": " since {first}",
        "header": "-# What {who} said{about}{when}:",
        "empty": "I found nothing {who} said{about}{when} in the channels I can search here.",
        "which": "Which {name}? {names}. Mention them with @ or use their full name.",
        "date": "%d %b",
    },
}
"""Every fixed reply, in both languages. One empty sentence per language: see
the module docstring for why "nothing" has exactly one wording."""

SAID_BY_DIRECTIVE = (
    "Report what {who} said{about}{when}, using only the messages below: every "
    "one of them was written by {who}. Attribute nothing to anyone else, cite "
    "every claim, and if none of the messages is about what was asked, say so. "
    "Write in the language of the person's own words below, not the language of "
    "these instructions. The person asked, in their own words: {asked}"
)
"""The "question" the synthesiser answers, as catch-up's directive is."""

SAID_BY_WINDOWS = 20
"""How many conversations the answer may rest on, each holding only the
person's own lines from it."""

SAID_BY_BUDGET = Budget(max_attempts=1, max_model_calls=1, max_tool_calls=1)
"""One retrieval and one synthesis; the topic embedding is the retrieval's."""


def _words(language: Language) -> dict[str, str]:
    return _TEXT.get(language, _TEXT[Language.ENGLISH])


def span_words(span: Span | None, tz: tzinfo, language: Language) -> str:
    """" em 21/09", " from 24 Aug to 30 Aug", " since 15 Sep", or nothing."""
    if span is None:
        return ""
    words = _words(language)
    first = span.start.astimezone(tz)
    if span.label.startswith("since:"):
        return words["since"].format(first=f"{first:{words['date']}}")
    last = (span.end - timedelta(microseconds=1)).astimezone(tz)
    if first.date() == last.date():
        return words["on"].format(day=f"{first:{words['date']}}")
    return words["range"].format(first=f"{first:{words['date']}}", last=f"{last:{words['date']}}")


# --- producing the answer -------------------------------------------------


class PeopleDirectory(Protocol):
    """`SearchBackend.people_named`, the one thing resolution needs."""

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = MAX_CANDIDATES
    ) -> Sequence[PersonCandidate]: ...


@dataclass(frozen=True, slots=True)
class SaidByOutcome:
    """The answer, or None to fall through; and the decision either way."""

    answer: Answer | None
    decision: Decision


@dataclass(frozen=True, slots=True)
class _Target:
    ref: PersonRef
    #: The name to show; empty for a mention or the asker, whose name comes
    #: from their own messages or a pronoun.
    display: str
    kind: SlotKind


def _decision(outcome: str, request: SaidByRequest) -> Decision:
    label = request.span.label if request.span else "any_time"
    return Decision(
        name="said_by",
        outcome=outcome,
        made_by=DecisionMaker.HEURISTIC,
        detail=f"{request.person.kind}:{label}",
    )


class SaidByService:
    """Answer "what did X say", from X's own messages, for one viewer.

    Holds a retrieval tool, a synthesiser and a name lookup -- no store and no
    connection. Every one of them takes the viewer this narrows to.
    """

    def __init__(
        self,
        retrieval: RetrievalTool,
        synthesizer: Synthesizer,
        people: PeopleDirectory,
        *,
        tz: tzinfo,
        clock: Clock = utc_now,
        limit: int = SAID_BY_WINDOWS,
    ) -> None:
        self._retrieval = retrieval
        self._synthesizer = synthesizer
        self._people = people
        self._tz = tz
        self._clock = clock
        self._limit = limit

    def recognise(self, text: str) -> SaidByRequest | None:
        """`said_by_request` on this process's clock and zone."""
        return said_by_request(text, self._clock(), self._tz)

    async def answer(self, question: Question, request: SaidByRequest) -> SaidByOutcome:
        viewer = retrieval_viewer(question)
        try:
            target = await self._resolve(viewer, question, request)
        except Exception as exc:  # noqa: BLE001 - any failure is "could not look"
            log.warning("said_by.resolution_unavailable", error=type(exc).__name__)
            return SaidByOutcome(
                replace(failure_answer(), consulted_channels=frozenset()),
                _decision("unavailable", request),
            )
        if target is None:
            return SaidByOutcome(None, _decision("fallback", request))
        if isinstance(target, list):
            return SaidByOutcome(self._which(request, target), _decision("ambiguous", request))
        return SaidByOutcome(
            await self._search(question, viewer, request, target), _decision("resolved", request)
        )

    async def _resolve(
        self, viewer: Viewer, question: Question, request: SaidByRequest
    ) -> _Target | list[PersonCandidate] | None:
        """One person, several to choose from, or None to fall through."""
        slot = request.person
        platform = question.asker.person.platform
        if slot.kind is SlotKind.SELF:
            return _Target(question.asker.person, "", slot.kind)
        if slot.kind is SlotKind.MENTION and slot.user_id is not None:
            # No lookup: a mention is the platform's own reference. If the
            # person never spoke where the viewer can read, the search finds
            # nothing and the reply is the one every empty result gets.
            return _Target(PersonRef(platform, slot.user_id), "", slot.kind)
        found = list(await self._people.people_named(viewer, slot.name, MAX_CANDIDATES))
        if not found:
            return None
        if len(found) > 1:
            return found
        return _Target(found[0].ref, found[0].display, slot.kind)

    def _which(self, request: SaidByRequest, candidates: Sequence[PersonCandidate]) -> Answer:
        """"Qual João?", naming only people the viewer can see speak.

        No retrieval and no model call. Not remembered (no consulted set): the
        names are a statement about who speaks where, and a remembered turn
        would outlive a change in who may read those channels.
        """
        names = ", ".join(c.display for c in candidates)
        text = _words(request.language)["which"].format(name=request.person.name, names=names)
        return Answer(text=text)

    async def _search(
        self, question: Question, viewer: Viewer, request: SaidByRequest, target: _Target
    ) -> Answer:
        span = request.span
        query = SearchQuery(
            text=request.topic,
            since=span.start if span else None,
            until=span.end if span else None,
            authors=frozenset({target.ref}),
            limit=self._limit,
        )
        try:
            result = await self._retrieval.retrieve(viewer, query)
        except RetrievalUnavailable as exc:
            # A failure, never "they said nothing": that is the one answer
            # that is always wrong when we could not look.
            log.warning("said_by.retrieval_unavailable", error=str(exc))
            return replace(failure_answer(), consulted_channels=frozenset())
        log.info(
            "said_by.searched",
            asker=str(viewer.person),
            slot=str(target.kind),
            span=span.label if span else "any_time",
            windows=len(result.items),
        )
        who = self._who(request, target, result)
        if not result.items:
            # Consulted and found empty, across every channel searched.
            return Answer(
                text=self._line("empty", request, who),
                consulted_channels=viewer.visible_channels,
            )
        return await self._write(question, request, who, result)

    def _who(self, request: SaidByRequest, target: _Target, result: RetrievalResult) -> str:
        if target.kind is SlotKind.SELF:
            return _words(request.language)["you"]
        shown = target.display or next(
            (item.author_display for item in result.items if item.author_display), ""
        )
        return shown or f"<@{target.ref.platform_user_id}>"

    def _line(self, key: str, request: SaidByRequest, who: str) -> str:
        words = _words(request.language)
        about = words["about"].format(topic=request.topic) if request.topic else ""
        when = span_words(request.span, self._tz, request.language)
        return words[key].format(who=who, about=about, when=when)

    async def _write(
        self, question: Question, request: SaidByRequest, who: str, result: RetrievalResult
    ) -> Answer:
        evidence = EvidenceLedger()
        evidence.add(result.items, result.source_system)
        decisions: list[Decision] = []
        answer = await write_answer(
            self._synthesizer,
            replace(question, text=self._directive(question.text, request, who)),
            evidence,
            BudgetLedger(SAID_BY_BUDGET),
            decisions,
            partial=result.truncated,
        )
        consulted = consulted_channels(evidence)
        if answer.abstained:
            # Their messages were found, but none the model could cite about
            # this: to the asker that is "nothing about it", the same sentence.
            return replace(
                answer,
                text=self._line("empty", request, who),
                abstained=False,
                consulted_channels=consulted,
            )
        header = self._line("header", request, who)
        return replace(answer, text=f"{header}\n\n{answer.text}", consulted_channels=consulted)

    def _directive(self, asked: str, request: SaidByRequest, who: str) -> str:
        about = f" about {request.topic}" if request.topic else ""
        when = span_words(request.span, self._tz, Language.ENGLISH)
        subject = "the person asking" if request.person.kind is SlotKind.SELF else who
        return SAID_BY_DIRECTIVE.format(who=subject, about=about, when=when, asked=asked)
