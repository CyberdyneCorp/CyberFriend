"""Catch-up summaries: an answer over a period of one channel.

A summary is not a second way into the corpus. It is the ordinary answer path
with two of its inputs pinned: the viewer is narrowed to one channel, and the
query carries a time range. Everything else -- the retrieval tool, the fence
that makes retrieved text data, the synthesiser, the citation resolution that
drops any window the run does not hold -- is the machinery `reasoning.fixed`
already uses, imported rather than re-implemented. Reading the channel
directly would have been a second path to content with permission rules of
its own, which is exactly the thing this project keeps arranging not to have.

Three decisions are worth stating, because each one is a place a summary
could have become a disclosure.

**The channel bound is applied by narrowing the viewer, not by naming
channels in the query.** `SearchQuery.channels` is documented as a preference
the backend is free to ignore -- and the Postgres backend does ignore it,
binding only the viewer's readable set into the statement. So the one thing
that actually constrains the scan is the viewer, and this module hands the
store a viewer that is the asker INTERSECT the audience INTERSECT the one
channel asked about. That set is a subset of what the ordinary path would
have searched, so a catch-up can only ever narrow access, never widen it.

**The channel is resolved by looking it up in what the viewer may read.**
There is no "does this channel exist" question asked anywhere here. A channel
id that is not in the narrowed viewer's set produces one refusal, and the
same refusal covers a channel that was never created, one that is not
indexed, one the asker may not read, and one the room the answer lands in may
not read. Four facts, one sentence, decided without consulting anything that
could tell them apart.

**There are no read receipts, so the period is stated or defaulted.** No bot
can see what a person has read. Guessing from someone's last message in the
channel would tell a lurker they missed a month, so the period is the one the
asker named, or `DEFAULT_PERIOD`, and the reply says which it covered either
way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.reasoning.budgets import Budget, BudgetLedger
from chatmemory.app.reasoning.contract import Decision, failure_answer
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import EvidenceLedger
from chatmemory.app.reasoning.fixed import consulted_channels, write_answer
from chatmemory.app.reasoning.ports import RetrievalResult, RetrievalTool, Synthesizer
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.app.routing import (
    Period,
    market_question,
    named_period,
    obligation_question,
)
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import SearchQuery
from chatmemory.ports.answers import Answer, Question

log = structlog.get_logger()


# --- recognising the request --------------------------------------------
#
# Lexical, like the rest of `routing`, and for the same reasons: a model call
# here would be paid on every question to find the few that are catch-ups,
# and routing has to be a decision made with zero model calls.
#
# The phrases are split in two by how much they mean on their own. "what did
# I miss" is a catch-up whatever else the sentence says, so it may default to
# the channel it was asked in. "summarise" and "o que rolou" are ordinary
# words that only mean *this* when a channel is named beside them -- without
# that guard, "summarise the decision about pricing" stops being a search.


def _normalise(text: str) -> str:
    """Lowercased, whitespace-collapsed and space-padded, so " x " matches a word."""
    return f" {' '.join(text.lower().replace('?', ' ').split())} "


# Catch-up on their own. Portuguese sits beside English because this server
# speaks it, and a feature that only works in one of its two languages is a
# feature half the server does not have.
_CATCH_UP_TERMS = (
    "what did i miss",
    "what have i missed",
    "what did we miss",
    "what i missed",
    "catch me up",
    "bring me up to speed",
    "get me up to speed",
    "o que eu perdi",
    "o que perdi",
    "o que que eu perdi",
    "o que e que eu perdi",
    "o que é que eu perdi",
    "me atualiza",
    "me atualize",
    "me poe a par",
    "me põe a par",
    "me poe em dia",
    "me põe em dia",
    "me coloca a par",
)

# Catch-up only when a channel is named. Each of these is also an ordinary
# thing to say about a topic rather than about a channel.
_CATCH_UP_WITH_CHANNEL_TERMS = (
    "catch up on",
    "catch-up on",
    "fill me in on",
    "what happened in",
    "what has happened in",
    "what's been happening in",
    "whats been happening in",
    "what's new in",
    "whats new in",
    "summarise",
    "summarize",
    "summary of",
    "recap",
    "resumo",
    "resuma",
    "resumir",
    "o que rolou",
    "o que aconteceu",
    "o que houve",
    "novidades",
)

#: A Discord channel mention. The id is the only part that matters: a mention
#: is Discord's own reference to a channel, whereas the name beside it in the
#: rendered message is not in the text at all. No length floor -- an id is not
#: validated by its shape here, it is validated by being found among the
#: channels the viewer may read, which is a stronger check than any regex.
_CHANNEL_MENTION = re.compile(r"<#!?(\d{1,25})>")

#: A channel typed as plain text rather than picked from Discord's list. It
#: carries a name and no id, and this module has no directory to turn one
#: into the other -- resolving it would mean matching names, which is how a
#: caller ends up naming a channel they cannot read and learning it exists.
_BARE_CHANNEL = re.compile(r"(?<![\w#])#[a-z0-9][a-z0-9_\-]{1,99}", re.IGNORECASE)

#: Words that turn a named span into an open-ended one. "yesterday" is the
#: day itself; "since yesterday" runs from its start up to now, and a summary
#: that stopped at midnight would leave out most of what was missed.
_SINCE = re.compile(r"\b(since|desde|from|a partir de)\b")

#: Spans named in Portuguese, longest phrase first so "semana passada" is not
#: read as "semana". They live here rather than in `routing._PERIODS` on
#: purpose: that table is also what obligation questions are read against, and
#: teaching it a new language would change what "o que me pediram ontem"
#: answers -- a worthwhile change, and a different one from this.
_PT_PERIODS: tuple[tuple[str, Period], ...] = (
    ("hoje de manha", Period(0)),
    ("hoje de manhã", Period(0)),
    ("esta manha", Period(0)),
    ("esta manhã", Period(0)),
    ("anteontem", Period(2, 1)),
    ("ontem", Period(1, 1)),
    ("hoje", Period(0)),
    ("semana passada", Period(14, 7)),
    ("ultima semana", Period(7)),
    ("última semana", Period(7)),
    ("esta semana", Period(7)),
    ("ultimos 7 dias", Period(7)),
    ("últimos 7 dias", Period(7)),
    ("mes passado", Period(60, 30)),
    ("mês passado", Period(60, 30)),
    ("ultimo mes", Period(30)),
    ("último mês", Period(30)),
    ("este mes", Period(30)),
    ("este mês", Period(30)),
)

DEFAULT_PERIOD = Period(1)
"""What a catch-up covers when nobody says: from the start of yesterday to now.

Stated in the reply every time. A default nobody is told about is one that
silently decides what "missed" means.
"""


@dataclass(frozen=True, slots=True)
class CatchUpRequest:
    """A recognised catch-up, and what its words named.

    `channel_id` is a platform id and deliberately not a `ChannelRef`: this
    module does not know which platform it is running on, and the id only
    becomes a channel by being found in what the viewer may read. That lookup
    is the permission check, so there is no moment where a `ChannelRef` for a
    forbidden channel exists to be mishandled.
    """

    channel_id: int | None
    period: Period
    #: Whether the asker named the period. False means `DEFAULT_PERIOD`.
    period_named: bool
    #: Whether the words named a channel at all, resolvable or not. A bare
    #: "#name" sets this with no id, which is a different answer from naming
    #: nothing -- and one decided without looking at any channel.
    named_a_channel: bool


def catch_up_period(text: str) -> Period | None:
    """The span a catch-up named, or None when it named none.

    `routing.named_period` does the reading for English; Portuguese is read
    here. Either way a bounded span that "since" made open-ended is widened.
    """
    padded = _normalise(text)
    period = named_period(text) or next(
        (p for phrase, p in _PT_PERIODS if phrase in padded), None
    )
    if period is None:
        return None
    if period.days_wide is not None and _SINCE.search(padded):
        return replace(period, days_wide=None)
    return period


def catch_up_request(text: str) -> CatchUpRequest | None:
    """The catch-up being asked for, or None for everything else.

    None is the common case and the safe one: it leaves the question on the
    retrieval path, which is where every question went before this existed.
    """
    if obligation_question(text) is not None:
        # "catch me up on what's still open" is an obligation question with a
        # catch-up phrase in front of it. Obligations are answered from rows
        # by addressee; summarising a channel instead would answer a
        # different question and lose the one that was asked.
        return None
    mention = _CHANNEL_MENTION.search(text)
    channel_id = int(mention.group(1)) if mention else None
    named_a_channel = mention is not None or _BARE_CHANNEL.search(text) is not None
    padded = _normalise(text)
    strong = any(term in padded for term in _CATCH_UP_TERMS)
    weak = named_a_channel and any(term in padded for term in _CATCH_UP_WITH_CHANNEL_TERMS)
    if not strong and not weak:
        return None
    if not named_a_channel and market_question(text) is not None:
        # "what did I miss on bitcoin today" names no channel, so the
        # fallback would summarise whichever room it was asked in -- when
        # what was asked for is a current figure, which a channel message
        # quoting a price is not. Naming a channel settles it the other way:
        # "what did I miss in #trading about the BTC price" is a summary of
        # #trading, and the market route would lose the channel entirely.
        return None
    period = catch_up_period(text)
    return CatchUpRequest(
        channel_id=channel_id,
        period=period or DEFAULT_PERIOD,
        period_named=period is not None,
        named_a_channel=named_a_channel,
    )


# --- refusals ------------------------------------------------------------

CHANNEL_UNAVAILABLE = (
    "I can't catch you up on that channel here. I only summarise channels I "
    "index, that you can read, and that everyone who can see this "
    "conversation can read — and I won't say which of those it fails, or "
    "whether the channel exists at all."
)
"""One sentence for four different facts.

A refusal that distinguished "there is no such channel" from "you may not
read it" would answer, for any id somebody cared to try, whether a private
channel exists. Same words, same path, no store consulted.
"""

NAME_THE_CHANNEL = (
    "Tell me which channel to catch up on, and pick it from Discord's channel "
    "list so it arrives as a link — like `what did I miss in #general`."
)
"""Said when nothing identified a channel at all.

Reached before any channel is looked up -- a plain "#general" carries a name
this module has no directory for, and a direct message has no channel to
default to -- so its wording cannot vary with what exists.
"""

NO_SUMMARY = (
    "There was activity in that period, but I couldn't put together a summary "
    "I'd stand behind. Ask me something specific about it and I'll look again."
)

QUIET_PERIOD = "Nothing I can show you was said there in that period."
"""Said for an empty result, and carefully not "the channel was quiet".

The retrieval behind it is scoped to this asker and this room, so an empty
result means nothing *they* may read was said -- which is not the same claim.
"""


# --- producing the summary ------------------------------------------------

CATCH_UP_WINDOWS = 25
"""How many conversation windows a summary may rest on.

Every one of them is rendered into the prompt at up to `MAX_EVIDENCE_CHARS`,
so this is a prompt-size bound as much as a coverage one. A day of one
channel is usually fewer windows than this, and the backend overfetches
before fusing, so the common case is "all of it".
"""

SUMMARY_BUDGET = Budget(max_attempts=1, max_model_calls=2, max_tool_calls=2)
"""One retrieval and one synthesis. There is no corrective round here: a
period either holds messages or it does not, and rewriting the query cannot
make a quiet Tuesday busier."""

SUMMARY_DIRECTIVE = (
    "Summarise, for someone who was away, what was said in one chat channel "
    "{span}. Group what you say by topic rather than by message: what was "
    "discussed, what was decided, and what was left open. Do not invent "
    "activity and do not pad — if the evidence is thin, say only what it "
    "supports. The person asked, in their own words: {asked}"
)
"""The "question" a summary is an answer to.

It goes through the same synthesiser as any answer, so the same system
prompt applies: every claim rests on evidence, the window ids are listed, and
the text between the evidence markers is data. The asker's own words ride
along because they often narrow the summary usefully, and the synthesiser
neutralises the whole string before it reaches the prompt.
"""


@dataclass(frozen=True, slots=True)
class _Target:
    """The channel a summary will be built from, or the refusal instead."""

    channel: ChannelRef | None
    refusal: str = ""


def resolve_channel(
    viewer: Viewer, request: CatchUpRequest, here: ChannelRef | None
) -> _Target:
    """Find the asked-for channel among the ones `viewer` may read.

    `viewer` is already the asker narrowed by the audience, so "may read"
    here means "both the asker and everyone who will see the answer may read
    it". Nothing outside that set can be named, so nothing outside it can be
    distinguished from nothing at all.
    """
    if request.named_a_channel and request.channel_id is None:
        return _Target(None, NAME_THE_CHANNEL)
    wanted = request.channel_id if request.channel_id is not None else _here_id(here)
    if wanted is None:
        return _Target(None, NAME_THE_CHANNEL)
    found = next(
        (c for c in viewer.visible_channels if c.platform_channel_id == wanted), None
    )
    return _Target(found) if found is not None else _Target(None, CHANNEL_UNAVAILABLE)


def _here_id(here: ChannelRef | None) -> int | None:
    """The channel the question was asked in, which "what did I miss" defaults to."""
    return here.platform_channel_id if here is not None else None


def period_note(channel: ChannelRef, bounds: tuple[datetime, datetime | None], named: bool) -> str:
    """The line that says exactly what was covered.

    Always present, not only when the period was defaulted: a reader who has
    to infer the span from the summary's contents cannot tell a quiet week
    from a narrow window.
    """
    since, until = bounds
    span = f"since {_stamp(since)}" if until is None else f"from {_stamp(since)} to {_stamp(until)}"
    mention = f"<#{channel.platform_channel_id}>"
    if named:
        return f"-# Catching you up on {mention} {span}."
    return f"-# No period given, so I used my default: {mention} {span}."


def _stamp(moment: datetime) -> str:
    return moment.strftime("%a %d %b %Y %H:%M %Z").strip()


class CatchUpService:
    """Summarise a period of one channel, for one viewer, with citations.

    Holds a retrieval tool and a synthesiser and nothing else -- no store, no
    connection, no channel directory. The only way it can reach content is
    the viewer-scoped `RetrievalTool` every answer already goes through.
    """

    def __init__(
        self,
        retrieval: RetrievalTool,
        synthesizer: Synthesizer,
        *,
        limit: int = CATCH_UP_WINDOWS,
        clock: Clock = utc_now,
    ) -> None:
        self._retrieval = retrieval
        self._synthesizer = synthesizer
        self._limit = limit
        self._clock = clock

    async def summarise(
        self, question: Question, request: CatchUpRequest, here: ChannelRef | None
    ) -> Answer:
        """The summary, the refusal, or the honest "it was quiet"."""
        viewer = retrieval_viewer(question)
        target = resolve_channel(viewer, request, here)
        if target.channel is None:
            log.info(
                "catchup.refused",
                asker=str(viewer.person),
                # Which refusal, never which channel: logs are read by
                # operators, and an id here would record exactly the
                # disclosure the refusal exists to prevent.
                reason="unnamed" if target.refusal is NAME_THE_CHANNEL else "unavailable",
            )
            return Answer(text=target.refusal, consulted_channels=frozenset())

        bounds = request.period.bounds(self._clock())
        # The viewer, not the query, is what bounds this to one channel: see
        # the module docstring. Intersecting rather than assigning means a
        # channel the asker may not read cannot enter the set even if
        # `resolve_channel` were one day changed to admit one.
        scope = Viewer(
            person=viewer.person,
            visible_channels=viewer.visible_channels & {target.channel},
        )
        try:
            result = await self._retrieval.retrieve(scope, self._query(question, bounds, target))
        except RetrievalUnavailable as exc:
            # Reported as a failure, never as "it was quiet": saying nothing
            # happened when we could not look is the one answer always wrong.
            log.warning("catchup.retrieval_unavailable", error=str(exc))
            return replace(failure_answer(), consulted_channels=frozenset())

        note = period_note(target.channel, bounds, request.period_named)
        log.info(
            "catchup.summarised",
            asker=str(viewer.person),
            windows=len(result.items),
            period_named=request.period_named,
            days_back=request.period.days_back,
        )
        if not result.items:
            return Answer(
                text=f"{note}\n{QUIET_PERIOD}",
                # Consulted and found empty. Memory re-checks this channel
                # before recalling the turn, which is right: "nothing was
                # said" is itself a statement about a channel.
                consulted_channels=frozenset({target.channel}),
            )
        return await self._write(question, result, bounds, note)

    def _query(
        self, question: Question, bounds: tuple[datetime, datetime | None], target: _Target
    ) -> SearchQuery:
        """Intent and a time range. No authorization -- that is in the viewer.

        The text is the asker's own question. The channel and the period are
        what make this a summary; the words still usefully rank within them
        when somebody asks what they missed *about the deploy*.
        """
        since, until = bounds
        assert target.channel is not None
        return SearchQuery(
            text=question.text,
            since=since,
            until=until,
            # Stated as the preference it is, so a backend that does honour
            # it agrees with the viewer rather than contradicting it.
            channels=frozenset({target.channel}),
            limit=self._limit,
        )

    async def _write(
        self,
        question: Question,
        result: RetrievalResult,
        bounds: tuple[datetime, datetime | None],
        note: str,
    ) -> Answer:
        """Synthesise over the retrieved windows, exactly as an answer is written.

        `write_answer` is the fixed path's own, so the summary gets its
        grounding rule for free: the model lists the windows it used, ids the
        run does not hold are dropped, and an answer left with no citation at
        all is not delivered as one.
        """
        evidence = EvidenceLedger()
        evidence.add(result.items, result.source_system)
        decisions: list[Decision] = []
        answer = await write_answer(
            self._synthesizer,
            replace(question, text=self._directive(question.text, bounds)),
            evidence,
            BudgetLedger(SUMMARY_BUDGET),
            decisions,
            partial=result.truncated,
        )
        consulted = consulted_channels(evidence)
        if answer.abstained:
            # Not "nothing was found": windows were retrieved, so the period
            # was not quiet. What failed is the summary, and saying so keeps
            # the two apart for whoever reads it.
            return replace(answer, text=f"{note}\n{NO_SUMMARY}", consulted_channels=consulted)
        return replace(answer, text=f"{note}\n\n{answer.text}", consulted_channels=consulted)

    def _directive(self, asked: str, bounds: tuple[datetime, datetime | None]) -> str:
        since, until = bounds
        span = (
            f"since {_stamp(since)}"
            if until is None
            else f"between {_stamp(since)} and {_stamp(until)}"
        )
        return SUMMARY_DIRECTIVE.format(span=span, asked=asked)
