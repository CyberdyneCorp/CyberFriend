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

Four kinds of request, in English and Portuguese:

*   **Range** -- an alert verb, a liquidity word (LP, pool, position, Uniswap)
    and leaving the range: "tell me when my LP goes out of range", "avise
    quando minha posição sair da faixa".
*   **Near the edge** -- the same, with a distance from the range's edge
    instead of leaving it: "warn me when my LP is within 5% of the range
    edge", "avise quando minha posição estiver a 3% da borda". A range alert
    with `edge_percent` set; "near the edge" with no number means 5%.
*   **Health** -- an alert verb and the health factor: "me avisa se o health
    factor do aave cair abaixo de 1,3". A request with no limit is still one,
    and is answered by asking for the limit.
*   **Price** -- an alert verb, BTC or ETH (the market tools' closed
    vocabulary, nothing else), and a direction or a level: "avisa quando o BTC
    passar de 100k", "alert me when ETH goes above $3,000". A level with no
    direction ("when BTC hits 100k") is left for the proposal to settle from
    the price now. Checked last, so "my LP on ethereum" stays a range alert.

"Tell me if" and "let me know if" also start plain questions -- "tell me if my
health factor is ok" is asking for a reading -- so after those a change must
be named ("drops", "goes out", "cair", "sair") or the verb must be "when".
Conversation verbs veto everything, as they do for the wallet routes: "what
did people say about alerting when the LP goes out of range" is a question
about the archive. So does a question about alerting itself -- "does Uniswap
notify me when...", "which app can warn me when...", "como criar um alerta..."
-- told apart by what comes before the trigger in its sentence: a question word,
or a modal with somebody other than the assistant as its subject. "Can you
alert me when..." is a request put politely and stays one. What is watched is
read from that sentence on, so "what does hf mean? tell me when you know" is not
a health alert.

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
from chatmemory.ports.alerts import DEFAULT_EDGE_PERCENT, AlertKind, PriceDirection

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

# Before the trigger, in its sentence: what makes it a question about alerting
# rather than a request for one.
_GREETING = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay|oi|ol[aá]|e\s+a[ií]|bom\s+dia|boa\s+(?:tarde|noite)|gm)"
    r"\b[\s,!.-]*)+"
)
_POLITE = re.compile(
    r"(?:(?:please|pls|plz|por\s+favor)\s*,?\s*)?"
    r"(?:(?:can|could|would|will)\s+(?:you|u|ya)"
    r"|(?:voc[eê]|vc|tu)\s+(?:pode|poderia|consegue|podia)"
    r"|pode|poderia|consegue|podia)?"
    r"(?:\s*,?\s*(?:please|pls|plz|por\s+favor))?\s*,?"
)
_QUESTION_OPENER = re.compile(
    r"(?:does|do|did|is|are|was|were|can|could|will|would|should|which|what|what'?s|"
    r"how|where|who|why|any|como|qual|quais|onde|quem|por\s*que|existe|tem|h[aá]|"
    r"ser[aá]|d[aá]\s+(?:pra|para)|algu[eé]m|algum|alguma)\b"
)
_TRAILING_MODAL = re.compile(
    r"\b(?:can|could|will|would|should|pode|poderia|consegue|vai|deve)\s*$"
)

_CHANGE = re.compile(
    r"\b(?:goes|go|gets|get|falls?|drops?|dips?|leaves?|moves?|exits?|slips?|"
    r"rises?|hits?|reach(?:es)?|cross(?:es)?|breaks?|"
    r"sair|sai|saia|cair|caia|ficar|fique|for|baixar|baixe|chegar|chegue|passar|"
    r"subir|suba|atingir|bater)\b",
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

# A distance from the range's edge: "within 5% of the range edge", "a 3% da
# borda", "3% do limite da faixa"; or "near the edge", which means the default.
_EDGE_WORD = (
    r"(?:(?:range|faixa)\s+)?(?:edges?|borders?|bounds?|boundar(?:y|ies)|limits?|"
    r"bordas?|limites?|extremidades?|pontas?)"
)
_EDGE = re.compile(
    r"(?:(?P<percent>\d+(?:[.,]\d+)?)\s*%\s*(?:away\s+)?(?:of|from|to|d[aeo]s?)?\s*"
    r"(?:the\s+|its\s+|my\s+|an?\s+|sua\s+)?" + _EDGE_WORD
    + r"|\b(?:near|close\s+to|perto\s+d[aeo]s?|pr[oó]xim[oa]\s+d[aeo]s?)\s+"
    r"(?:the\s+|its\s+|an?\s+|sua\s+)?" + _EDGE_WORD + r")",
    re.IGNORECASE,
)

_ASSET = re.compile(
    r"\b(?:(?P<btc>btc|bitcoin|xbt)|(?P<eth>eth|ether|ethereum|[eé]ter))\b", re.IGNORECASE
)
_ABOVE = re.compile(
    r"\b(?:above|over|exceeds?|surpass(?:es)?|breaks?|tops?|past|higher\s+than|"
    r"more\s+than|rises?|acima|passar|passa|ultrapassar|ultrapassa|subir|sobe|suba|"
    r"maior\s+(?:que|do\s+que)|mais\s+de)\b|>",
    re.IGNORECASE,
)
_BELOW_PRICE = re.compile(
    r"\b(?:below|under|beneath|less\s+than|lower\s+than|drops?|falls?|dips?|sinks?|"
    r"abaixo|cair|cai|caia|baixar|baixe|menor\s+(?:que|do\s+que)|menos\s+de)\b|<",
    re.IGNORECASE,
)
# "$3,000", "100k", "2.500", "100 mil": a level in US dollars. Not a percentage.
_LEVEL = re.compile(
    r"(?<![\w.,#])(?:US\$|\$)?\s?(?P<number>\d[\d.,]*\d|\d)\s?(?P<mult>k|mil)?"
    r"(?![\w%]|\s?%)",
    re.IGNORECASE,
)
_MULTIPLIERS = {"k": 1000, "mil": 1000}

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
    #: A single position named by its number ("#4558452").
    token_id: int | None = None
    #: A range alert that also warns this near an edge, as written.
    edge_percent: Decimal | None = None
    #: A price alert's ticker (BTC or ETH), direction and level in US dollars.
    #: The direction is None when only a level was given ("hits 100k").
    asset: str | None = None
    direction: PriceDirection | None = None
    level: Decimal | None = None


def alert_intent(text: str, previous_questions: Sequence[str] = ()) -> AlertIntent | None:
    """The alert being asked for, or None for everything else.

    `previous_questions` is the asker's own earlier questions in this
    conversation, oldest first, read only for an address they typed there.
    """
    request = _request(text)
    if request is None:
        return None
    kind = _kind(text, request)
    if kind is None:
        return None
    if kind is AlertKind.PRICE:
        return _price_intent(request)
    address = _address(text, previous_questions)
    return AlertIntent(
        kind=kind,
        threshold=_threshold(text) if kind is AlertKind.AAVE_HEALTH else None,
        chain=_chain(text),
        address=address,
        token_id=_token_id(text) if kind is AlertKind.LP_RANGE else None,
        edge_percent=_edge_percent(request) if kind is AlertKind.LP_RANGE else None,
    )


def _request(text: str) -> str | None:
    """The sentence asking to be told, from its start on, if one does."""
    words = set(re.findall(r"[\w']+", text.lower()))
    if words & CONVERSATION_VERBS:
        return None
    trigger = _trigger(text)
    if trigger is None:
        return None
    # The sentence the request is in, from its start to the end of the message:
    # "what does hf mean? tell me when you know" names a health factor, but not
    # in the sentence that asks to be told.
    start = _sentence_start(text, trigger.start())
    if _asks_about_alerting(text[start : trigger.start()]):
        return None
    return text[start:]


def _kind(text: str, request: str) -> AlertKind | None:
    """Which alert the words ask for, if they ask for one at all."""
    if _HEALTH.search(request):
        return AlertKind.AAVE_HEALTH
    leaves = _OUT_OF_RANGE.search(request) or _EDGE.search(request)
    if leaves and _LIQUIDITY.search(text):
        return AlertKind.LP_RANGE
    if _ASSET.search(request) and (_direction(request) or _level(request)):
        return AlertKind.PRICE
    return None


def _trigger(text: str) -> re.Match[str] | None:
    """An alert verb, or a question verb with "when" or a change after it."""
    strong = _STRONG_TRIGGER.search(text)
    if strong is not None:
        return strong
    weak = _WEAK_TRIGGER.search(text)
    if weak is None:
        return None
    if weak.group("conjunction").lower() not in {"if", "se", "caso"}:
        return weak
    # "Tell me if my health factor is ok" asks for a reading, not a watch.
    return weak if _CHANGE.search(text[weak.end() :]) else None


def _sentence_start(text: str, index: int) -> int:
    """Where the sentence holding `index` begins."""
    return max(text.rfind(mark, 0, index) for mark in ".?!\n") + 1


def _asks_about_alerting(prefix: str) -> bool:
    """The words before the trigger make it a question about alerts.

    "Does Uniswap notify me when...", "which app can warn me when...", "como
    criar um alerta...": a question word, or a modal whose subject is somebody
    other than the assistant. "Can you alert me when..." and "você pode me
    avisar quando..." are requests put politely, and stay requests.
    """
    lead = _GREETING.sub("", prefix.strip().lower())
    if _POLITE.fullmatch(lead):
        return False
    return bool(_QUESTION_OPENER.match(lead) or _TRAILING_MODAL.search(lead))


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


def _edge_percent(request: str) -> Decimal | None:
    """The distance from the edge, as written; None when none was asked for."""
    match = _EDGE.search(request)
    if match is None:
        return None
    written = match.group("percent")
    return Decimal(written.replace(",", ".")) if written else DEFAULT_EDGE_PERCENT


def _price_intent(request: str) -> AlertIntent:
    asset = _ASSET.search(request)
    ticker = "BTC" if asset is not None and asset.group("btc") else "ETH"
    return AlertIntent(
        AlertKind.PRICE, asset=ticker, direction=_direction(request), level=_level(request)
    )


def _direction(request: str) -> PriceDirection | None:
    """Above or below, whichever the request says first; None for neither."""
    above, below = _ABOVE.search(request), _BELOW_PRICE.search(request)
    if above is None or below is None:
        return PriceDirection.ABOVE if above else PriceDirection.BELOW if below else None
    return PriceDirection.ABOVE if above.start() < below.start() else PriceDirection.BELOW


def _level(request: str) -> Decimal | None:
    """The first level after the asset, in dollars: "100k" is 100000."""
    asset = _ASSET.search(request)
    after = request[asset.end() :] if asset is not None else request
    match = _LEVEL.search(after)
    if match is None:
        return None
    amount = parse_amount(match.group("number"))
    multiplier = _MULTIPLIERS.get((match.group("mult") or "").lower(), 1)
    return None if amount is None else amount * multiplier


def parse_amount(written: str) -> Decimal | None:
    """A number as either notation writes it: 2.500 and 2,500 are both 2500.

    With both marks the last one is the decimal point. With one, it groups
    thousands when every group after it is three digits (or it repeats), and
    is the decimal point otherwise, so "1,5" and "1.5" are one and a half.
    """
    if "." in written and "," in written:
        point = max(written.rfind("."), written.rfind(","))
        whole = re.sub(r"[.,]", "", written[:point])
        return _decimal(f"{whole}.{written[point + 1 :]}")
    mark = "." if "." in written else ","
    parts = written.split(mark)
    if len(parts) == 1:
        return _decimal(written)
    if len(parts) > 2 or len(parts[1]) == 3:
        return _decimal("".join(parts))
    return _decimal(f"{parts[0]}.{parts[1]}")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _chain(text: str) -> str | None:
    match = _CHAIN.search(text)
    return _CHAIN_KEYS[match.group("chain").lower()] if match else None


def _token_id(text: str) -> int | None:
    match = _TOKEN_ID.search(text)
    return int(match.group("id")) if match else None


def _address(text: str, previous: Sequence[str]) -> str | None:
    """The asker's own address: in this message, else in a recent question."""
    written = find_addresses(text)
    return written[0] if written else recent_chain_address(previous)
