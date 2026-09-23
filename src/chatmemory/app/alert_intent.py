"""Recognising "tell me when my LP goes out of range" as a request for an alert.

Lexical, like everything in `routing`, and for the same reasons: this runs on
every message before retrieval, and a model call here would add latency and
nondeterminism to all of them. It also decides something with standing
consequences -- an alert messages somebody on a timer -- which is why nothing
is stored on its say-so: a recognised request is answered with what would be
watched and a Confirm button, and only the button creates anything.

Its route label is `ALERT_CREATE`, the name the unified router will give it.
It is checked before the positions and wallet routes, because "alert me if my
health factor drops below 1.3" names a health factor too, and it is a watch
that was asked for, not a reading.

Two kinds of request, in English and Portuguese:

*   **Range** -- an alert verb, a liquidity word (LP, pool, position, Uniswap)
    and leaving the range: "tell me when my LP goes out of range", "avise
    quando minha posição sair da faixa".
*   **Health** -- an alert verb and the health factor: "me avisa se o health
    factor do aave cair abaixo de 1,3". A request with no limit is still one,
    and is answered by asking for the limit.

"Tell me if" and "let me know if" also start plain questions -- "tell me if my
health factor is ok" is asking for a reading -- so after those a change must
be named ("drops", "goes out", "cair", "sair") or the verb must be "when".
Conversation verbs veto everything, as they do for the wallet routes: "what
did people say about alerting when the LP goes out of range" is a question
about the archive.

The address is the one in the message, or the asker's own from one of their
last few questions -- both their own words -- or else none, and the caller
uses their saved wallet. Nothing retrieved is ever read here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from chatmemory.app.routing import CONVERSATION_VERBS, recent_chain_address
from chatmemory.domain.chain import find_addresses
from chatmemory.ports.alerts import AlertKind

ALERT_CREATE = "ALERT_CREATE"
"""The route label, spelled as the unified router's design spells it."""

# Verbs that only ever ask to be told later: an alert whatever follows.
_STRONG_TRIGGER = re.compile(
    r"\b(?:"
    r"(?:alert|notify|notfy|notifiy|warn|ping|dm|message|remind)\s+me\s+"
    r"(?:when|wen|whenever|if|once|as\s+soon\s+as|in\s+case)"
    r"|(?:set(?:\s+up)?|create|add|make)\s+(?:me\s+)?(?:an?\s+)?(?:alert|alarm|notification)"
    r"|(?:me\s+)?(?:avis(?:e|a|ar)|aviz(?:e|a|ar)|notifi(?:que|ca|car)|alert(?:e|a|ar)|"
    r"chama|manda\s+(?:uma\s+)?mensagem)(?:-me)?\s+(?:quando|qdo|qnd|se|caso|assim\s+que)"
    r"|avis(?:ar|e|a)-?me\b"
    r"|(?:quero\s+)?ser\s+avisad[oa]"
    r"|(?:cri[ae]r?|coloc[ae]r?|configur[ae]r?|quero|faz(?:er)?|bot[ae]r?)\s+"
    r"(?:um\s+)?(?:alerta|aviso|alarme)"
    r")",
    re.IGNORECASE,
)

# Verbs that start questions as often as requests: an alert only with "when",
# or with a change named after them.
_WEAK_TRIGGER = re.compile(
    r"\b(?:(?:tell|tel|let)\s+me\s+(?:know\s+|knwo\s+)?|(?:me\s+)?(?:diga|diz|fala)\s+)"
    r"(?P<conjunction>when|wen|whenever|once|as\s+soon\s+as|if|quando|qdo|se|caso)\b",
    re.IGNORECASE,
)

_CHANGE = re.compile(
    r"\b(?:goes|go|gets|get|falls?|drops?|dips?|leaves?|moves?|exits?|slips?|"
    r"sair|sai|saia|cair|caia|ficar|fique|for|baixar|baixe|chegar|chegue|passar)\b",
    re.IGNORECASE,
)

_LIQUIDITY = re.compile(
    r"\b(?:lps?|liquidity|liquidez|pools?|positions?|posi[cç](?:[aã]o|[oõ]es)|"
    r"uniswap|univ[34]|v[34]|nfts?)\b",
    re.IGNORECASE,
)

_OUT_OF_RANGE = re.compile(
    r"\b(?:out\s*(?:of|side)?\s+(?:the\s+|its\s+|my\s+)?r(?:an|na)ge"
    r"|outta\s+r(?:an|na)ge"
    r"|(?:leaves?|leaving|exits?|exiting)\s+(?:the\s+|its\s+|my\s+)?r(?:an|na)ge"
    r"|(?:sai[ra]?|saiu|sair|saia|ficar|fique|estiver|for|est[aá])\s+(?:d[aeo]\s+|fora\s+d[aeo]\s+)"
    r"(?:sua\s+)?(?:faixa|range|intervalo)"
    r"|fora\s+d[aeo]\s+(?:sua\s+)?(?:faixa|range|intervalo))\b",
    re.IGNORECASE,
)

_HEALTH = re.compile(
    r"\b(?:health\s*-?\s*factor|healthfactor|hf|fator\s+de\s+sa[uú]de)\b",
    re.IGNORECASE,
)

_BELOW = re.compile(
    r"(?:\bbelow|\bunder|\bless\s+than|\blower\s+than|\bbeneath|<|\bto"
    r"|\babaixo\s+de|\bmenor\s+(?:que|do\s+que)|\bmenos\s+de|\bpara|\bpra|\ba|\bem)"
    r"\s*(?P<number>\d+(?:[.,]\d+)?)(?![\w.,]\d)",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"(?<![\w.,#/])(?P<number>\d+(?:[.,]\d+)?)(?![\w]|[.,]\d)")

_CHAIN = re.compile(
    r"\b(?:on|in|at|na|no|em|da|do)\s+(?:the\s+|a\s+)?"
    r"(?P<chain>base|arbitrum|arb|ethereum|mainnet)\b",
    re.IGNORECASE,
)
_CHAIN_KEYS = {
    "base": "base",
    "arbitrum": "arbitrum",
    "arb": "arbitrum",
    "ethereum": "ethereum",
    "mainnet": "ethereum",
}

_TOKEN_ID = re.compile(r"#\s?(?P<id>\d{2,})\b")
_ADDRESS_TEXT = re.compile(r"0x[0-9a-fA-F]{40}")


@dataclass(frozen=True, slots=True)
class AlertIntent:
    """An alert somebody asked for, as far as their words say.

    `threshold` is None when a health alert named no limit, and is carried as
    written even when it is out of bounds, so the reply can say which bounds.
    `address` is None when none was written; the caller then uses the asker's
    saved wallet or asks for one.
    """

    kind: AlertKind
    threshold: Decimal | None = None
    chain: str | None = None
    address: str | None = None
    #: The address came from the asker's own earlier question, not this one.
    carried: bool = False
    #: A single position named by its number ("#4558452").
    token_id: int | None = None


def alert_intent(text: str, previous_questions: Sequence[str] = ()) -> AlertIntent | None:
    """The alert being asked for, or None for everything else.

    `previous_questions` is the asker's own earlier questions in this
    conversation, oldest first, read only for an address they typed there.
    """
    kind = _kind(text)
    if kind is None:
        return None
    address, carried = _address(text, previous_questions)
    return AlertIntent(
        kind=kind,
        threshold=_threshold(text) if kind is AlertKind.AAVE_HEALTH else None,
        chain=_chain(text),
        address=address,
        carried=carried,
        token_id=_token_id(text) if kind is AlertKind.LP_RANGE else None,
    )


def _kind(text: str) -> AlertKind | None:
    """Which alert the words ask for, if they ask for one at all."""
    words = set(re.findall(r"[\w']+", text.lower()))
    if words & CONVERSATION_VERBS or not _asks_to_be_told(text):
        return None
    if _HEALTH.search(text):
        return AlertKind.AAVE_HEALTH
    if _OUT_OF_RANGE.search(text) and _LIQUIDITY.search(text):
        return AlertKind.LP_RANGE
    return None


def _asks_to_be_told(text: str) -> bool:
    """An alert verb, or a question verb with "when" or a change after it."""
    if _STRONG_TRIGGER.search(text):
        return True
    weak = _WEAK_TRIGGER.search(text)
    if weak is None:
        return False
    if weak.group("conjunction").lower() not in {"if", "se", "caso"}:
        return True
    # "Tell me if my health factor is ok" asks for a reading, not a watch.
    return bool(_CHANGE.search(text[weak.end() :]))


def _threshold(text: str) -> Decimal | None:
    """The limit, preferring a number after "below" or "abaixo de"."""
    cleaned = _TOKEN_ID.sub(" ", _ADDRESS_TEXT.sub(" ", text))
    cleaned = re.sub(r"\bv[34]\b", " ", cleaned, flags=re.IGNORECASE)
    match = _BELOW.search(cleaned) or _NUMBER.search(cleaned)
    if match is None:
        return None
    try:
        # "1,3" is how half the people here write 1.3.
        return Decimal(match.group("number").replace(",", "."))
    except InvalidOperation:  # pragma: no cover - the pattern admits only digits
        return None


def _chain(text: str) -> str | None:
    match = _CHAIN.search(text)
    return _CHAIN_KEYS[match.group("chain").lower()] if match else None


def _token_id(text: str) -> int | None:
    match = _TOKEN_ID.search(text)
    return int(match.group("id")) if match else None


def _address(text: str, previous: Sequence[str]) -> tuple[str | None, bool]:
    """The asker's own address: in this message, else in a recent question."""
    written = find_addresses(text)
    if written:
        return written[0], False
    carried = recent_chain_address(previous)
    return (carried, True) if carried is not None else (None, False)
