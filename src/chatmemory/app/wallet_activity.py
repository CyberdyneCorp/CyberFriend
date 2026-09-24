"""What a wallet did: recognising the question, and the span it covers.

Both halves are read from the asker's own words and nothing else. The route
decides from the question that it is about activity, and the provider decides
from the same question -- as the clearance carries it -- which days to read.
The model's arguments are an address and nothing more: a window chosen by the
model is a wrong period nobody would notice.

Recognised before retrieval, like every chain question: no channel holds what
a wallet did this week, and a corpus answer to "what did my wallet do?" is a
colleague's message presented as the asker's history.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from chatmemory.app.periods import padded, portuguese_period
from chatmemory.app.routing import (
    CONVERSATION_VERBS,
    Period,
    named_period,
    recent_chain_address,
)
from chatmemory.domain.chain import find_addresses

DEFAULT_DAYS = 7
MAX_DAYS = 30
"""The widest span one question reads. A month is what a person reviews; more
is a statement export, and it multiplies the explorer pages spent on spam."""

_FOLLOW_UP_TURNS = 3

# --- the question ---------------------------------------------------------------

_WALLET_WORDS = frozenset({"wallet", "wallets", "carteira", "carteiras"})
_FIRST_PERSON = frozenset({"my", "mine", "meu", "meus", "minha", "minhas"})

_ACTIVITY_NOUNS = frozenset({
    "transactions", "transaction", "txs", "tx", "transações", "transacoes",
    "transação", "transacao", "movimentações", "movimentacoes", "movimentação",
    "movimentacao", "transferências", "transferencias",
})
"""Words that are about on-chain history even with only "my" beside them:
"minhas transações de ontem" is a wallet question on this server."""

_CONTEXT_NOUNS = frozenset({"activity", "atividade", "history", "histórico", "historico"})
"""Words that are about history only once a wallet or an address is named:
"my activity this week" could be about Discord, "my wallet activity" cannot."""

_DID_DO = re.compile(
    r"\bwhat\s+(?:did|has|have)\b.*\b(?:do|done)\b"
    r"|\bo\s+que\b.*\b(?:fez|fizeram|movimentou|aconteceu)\b"
    r"|\bmovimentou\b",
    re.IGNORECASE,
)
""""What did this wallet do", "o que essa carteira fez", "o que 0x… movimentou"."""

_PRONOUN_DID_DO = re.compile(
    r"\bwhat\s+(?:did|has)\s+(?:it|that|this\s+one)\s+(?:do|done)\b"
    r"|\bo\s+que\s+(?:ela|essa|esta|isso)\s+fez\b",
    re.IGNORECASE,
)
"""A follow-up about the address asked about just before."""

_PERIOD_FOLLOW_UP = re.compile(
    r"\A\s*(?:e|and)\b[^?]{0,40}\??\s*\Z",
    re.IGNORECASE,
)
""""and last month?", "e ontem?": short, opening with "and"."""


@dataclass(frozen=True, slots=True)
class ActivityQuestion:
    """A request for a wallet's activity.

    `address` is None when none was written or carried; the route then uses
    the asker's saved wallet, or asks which of several, or asks for one.
    """

    address: str | None
    carried: bool = False


def activity_question(text: str, previous: Sequence[str] = ()) -> ActivityQuestion | None:
    """The activity lookup being asked for, or None for everything else.

    "What did people say about my wallet" stays with the corpus: the
    conversation verbs veto this route exactly as they veto the balance one.
    """
    words = set(re.findall(r"[\w]+", text.lower()))
    if words & CONVERSATION_VERBS:
        return None
    if not _asks_about_activity(text, words):
        return _period_follow_up(text, previous)
    addresses = find_addresses(text)
    if addresses:
        return ActivityQuestion(addresses[0])
    if words & _WALLET_WORDS or (words & _FIRST_PERSON and words & _ACTIVITY_NOUNS):
        return ActivityQuestion(None)
    # "What did it do this week?" after a balance question: that address. Only
    # on a history noun or a pronoun subject -- "what did John do?" is not.
    if words & (_ACTIVITY_NOUNS | _CONTEXT_NOUNS) or _PRONOUN_DID_DO.search(text):
        carried = recent_chain_address(previous)
        if carried is not None:
            return ActivityQuestion(carried, carried=True)
    return None


def _asks_about_activity(text: str, words: set[str]) -> bool:
    return bool(words & (_ACTIVITY_NOUNS | _CONTEXT_NOUNS)) or bool(_DID_DO.search(text))


def _period_follow_up(text: str, previous: Sequence[str]) -> ActivityQuestion | None:
    """"and last month?" right after an activity question: the same wallet."""
    if not _PERIOD_FOLLOW_UP.match(text) or activity_period(text) is None:
        return None
    for earlier in reversed(previous[-_FOLLOW_UP_TURNS:]):
        found = activity_question(earlier)
        if found is not None:
            return ActivityQuestion(found.address, carried=found.address is not None)
    return None


# --- the span -------------------------------------------------------------------

_LAST_N_DAYS = re.compile(
    r"\b(?:last|past|[uú]ltimos|nos\s+[uú]ltimos)\s+(\d{1,3})\s+(?:days|dias)\b",
    re.IGNORECASE,
)

_MORE_PERIODS: tuple[tuple[str, Period], ...] = (
    ("essa semana", Period(7)),
    ("nesta semana", Period(7)),
    ("nessa semana", Period(7)),
    ("desta semana", Period(7)),
    ("dessa semana", Period(7)),
    ("de ontem", Period(1, 1)),
    ("de hoje", Period(0)),
)
"""Portuguese spans `periods.PT_PERIODS` does not hold, checked first."""


@dataclass(frozen=True, slots=True)
class ActivityWindow:
    """The days one activity answer covers, and how they were chosen."""

    start: datetime
    end: datetime
    #: The question named a span; False is the seven-day default.
    named: bool
    #: The span named was wider than `MAX_DAYS` and was cut to it.
    clamped: bool = False


def activity_period(text: str) -> Period | None:
    """The span the question named, or None when it named none."""
    counted = _LAST_N_DAYS.search(text)
    if counted:
        return Period(int(counted.group(1)))
    words = padded(text)
    for phrase, period in _MORE_PERIODS:
        if phrase in words:
            return period
    return named_period(text) or portuguese_period(text)


def activity_window(question: str, now: datetime) -> ActivityWindow:
    """The window to read for this question, never wider than `MAX_DAYS`."""
    period = activity_period(question)
    named = period is not None
    start, end = (period or Period(DEFAULT_DAYS)).bounds(now)
    finish = min(end or now, now)
    # A span counts from midnight, so "this month" is thirty days and part of
    # today: that is within the limit, and only a wider one is cut.
    if finish - start > timedelta(days=MAX_DAYS + 1):
        return ActivityWindow(finish - timedelta(days=MAX_DAYS), finish, named, clamped=True)
    return ActivityWindow(start, finish, named)
