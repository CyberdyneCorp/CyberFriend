"""A feature request written as a message, and the routes that outrank it.

Only six explicit forms count, at the start of the message: "tenho uma
sugestão", "sugestão:", "seria legal se você", "I have a feature request",
"feature request:" and "it would be nice if you could". There is no bare-noun
or imperative marker, so "qual foi a sugestão do João?" and "you should be
able to tell me X" are questions, as they always were.

A match is never stored. It is proposed, and only when no other route claims
the message or the words after the form (`route_claiming`): "sugestão: me
diga o preço do BTC" is a price question, and "feature request: notify me
when BTC hits 100k" an alert request. The surface shows the proposal with
[Record suggestion] and [No, answer it].
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from chatmemory.app.alert_intent import alert_intent
from chatmemory.app.catchup import catch_up_request
from chatmemory.app.egress import ISO_4217_CODES
from chatmemory.app.language import Language
from chatmemory.app.routing import (
    decision_question,
    explicit_web_search,
    fact_intent,
    indexing_request,
    market_follow_up,
    market_question,
    mcp_change_request,
    obligation_question,
    self_description_question,
    time_question,
)
from chatmemory.app.routing_crypto import crypto_route
from chatmemory.app.said_by import said_by_request
from chatmemory.app.self_description import typed_command
from chatmemory.domain.identity import PersonRef

EN, PT = Language.ENGLISH, Language.PORTUGUESE

PROPOSAL_QUOTE_CHARS = 300
"""How much of the suggestion the proposal quotes back."""


@dataclass(frozen=True, slots=True)
class SuggestionForm:
    """One explicit way to start a suggestion.

    `labels` is True for a form that only announces one ("sugestão: ...");
    what is stored is then the words after it. A form that is part of the
    sentence ("it would be nice if you could show X") is stored whole, since
    "show X" alone no longer says what was asked.
    """

    name: str
    pattern: re.Pattern[str]
    language: Language
    labels: bool


def _form(name: str, pattern: str, language: Language, *, labels: bool) -> SuggestionForm:
    return SuggestionForm(name, re.compile(pattern, re.IGNORECASE), language, labels)


# Accents optional, as people type them on phones; the words themselves fixed.
SUGGESTION_FORMS: tuple[SuggestionForm, ...] = (
    _form("tenho uma sugestão", r"tenho\s+uma\s+sugest[aã]o\b", PT, labels=True),
    _form("sugestão:", r"sugest[aã]o\s*:", PT, labels=True),
    _form("seria legal se você", r"seria\s+legal\s+se\s+voc[eê]\b", PT, labels=False),
    _form("I have a feature request", r"i\s+have\s+a\s+feature\s+request\b", EN, labels=True),
    _form("feature request:", r"feature\s+request\s*:", EN, labels=True),
    _form(
        "it would be nice if you could",
        r"it\s+would\s+be\s+nice\s+if\s+you\s+could\b",
        EN,
        labels=False,
    ),
)

_LEADING_MENTION = re.compile(r"^\s*(?:<@[!&]?\d+>[\s,:;]*)+")
_SEPARATOR = re.compile(r"^[\s:;,.!\-–—]+")
_WORD = re.compile(r"\w")


@dataclass(frozen=True, slots=True)
class SuggestionIntent:
    """A message that starts with an explicit suggestion form.

    `remainder` is what follows the form, which the other routes are asked
    about; `text` is what would be stored.
    """

    form: SuggestionForm
    text: str
    remainder: str

    @property
    def language(self) -> Language:
        return self.form.language


def suggestion_intent(text: str) -> SuggestionIntent | None:
    """The suggestion the message starts with, or None.

    None also for a form with nothing after it: "tenho uma sugestão" alone
    has no suggestion in it to record.
    """
    message = _LEADING_MENTION.sub("", text).strip()
    for form in SUGGESTION_FORMS:
        match = form.pattern.match(message)
        if match is None:
            continue
        remainder = _SEPARATOR.sub("", message[match.end() :]).strip()
        if not _WORD.search(remainder):
            return None
        return SuggestionIntent(form, remainder if form.labels else message, remainder)
    return None


# --- the routes that outrank a suggestion ------------------------------------------

_Claim = Callable[[str, Sequence[str], datetime, tzinfo], object]

#: Every lexical route a message can take instead of the default corpus
#: answer, in the order the pipeline tries them. A new route is added here, and
#: `test_suggestion_intent` holds one case per row, so a suggestion can never
#: win over something the assistant can actually do.
ROUTE_CLAIMS: tuple[tuple[str, _Claim], ...] = (
    ("fact", lambda t, p, n, z: fact_intent(t)),
    ("indexing", lambda t, p, n, z: indexing_request(t)),
    ("typed_command", lambda t, p, n, z: typed_command(t)),
    ("alert", lambda t, p, n, z: alert_intent(t, p)),
    ("catch_up", lambda t, p, n, z: catch_up_request(t)),
    ("said_by", lambda t, p, n, z: said_by_request(t, n, z)),
    ("self_description", lambda t, p, n, z: self_description_question(t)),
    ("obligations", lambda t, p, n, z: obligation_question(t)),
    ("decisions", lambda t, p, n, z: decision_question(t, n, z)),
    ("mcp_change", lambda t, p, n, z: mcp_change_request(t)),
    (
        "market",
        lambda t, p, n, z: (
            market_question(t, ISO_4217_CODES) or market_follow_up(t, p, ISO_4217_CODES)
        ),
    ),
    ("time", lambda t, p, n, z: time_question(t)),
    ("crypto", lambda t, p, n, z: crypto_route(t, p)),
    ("web_search", lambda t, p, n, z: explicit_web_search(t)),
)


def route_claiming(
    text: str,
    previous: Sequence[str] = (),
    *,
    now: datetime | None = None,
    tz: tzinfo = UTC,
) -> str | None:
    """The route that would answer `text` instead of the corpus, or None.

    `previous` is the asker's own earlier questions, oldest first, for the
    follow-ups the market and chain routes read. `now` and `tz` only bound the
    periods said-by and decisions read; whether they match does not depend on
    them.
    """
    moment = now or datetime.now(UTC)
    for name, claims in ROUTE_CLAIMS:
        found = claims(text, previous, moment, tz)
        if found:
            return name
    return None


# --- the proposal -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuggestionProposal:
    """What [Record suggestion] would store, for the asker to confirm."""

    person: PersonRef
    text: str
    language: Language
    form: str


_PROPOSAL = {
    EN: (
        "That sounds like a feature request:\n> {quote}\n"
        "Shall I record it for the team? They will see your text and your name. "
        "Or I can answer it as a question."
    ),
    PT: (
        "Isso parece uma sugestão:\n> {quote}\n"
        "Quer que eu registre para a equipe? Ela vai ver o seu texto e o seu nome. "
        "Ou posso responder como uma pergunta."
    ),
}


def proposal_text(text: str, language: Language) -> str:
    """The proposal, quoting the suggestion back on one line."""
    quote = " ".join(text.split())
    if len(quote) > PROPOSAL_QUOTE_CHARS:
        quote = quote[: PROPOSAL_QUOTE_CHARS - 1] + "…"
    return _PROPOSAL[PT if language is PT else EN].format(quote=quote)
