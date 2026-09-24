"""A wallet's activity as Discord markdown: figures only, in the asker's language.

Shown verbatim (see `verbatim_answer`): the first line is a header the citation
already carries and is dropped, so the period is stated again in the body.
No model writes a figure here.

Counterparties are the privacy question. In a DM -- the asker alone -- every
address is printed in full, never shortened: first-four/last-four is exactly
the view address poisoning is built to fool, and a poisoned lookalike was in
the real data next to the counterparty it imitated. In a channel, a
counterparty is "an external address": who the owner paid is theirs to share,
and the amounts alone are what the question is about. The wallet itself is
named by its last four characters there, as the portfolio names it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from chatmemory.adapters.chain.activity import (
    Action,
    ChainActivity,
    Kind,
    Leg,
    Part,
    Recognised,
    leg_usd,
    net_by_asset,
)
from chatmemory.adapters.chain.activity_explorer import MAX_ROWS
from chatmemory.adapters.chain.positions_render import amount
from chatmemory.app.language import Language, detect
from chatmemory.app.wallet_activity import DEFAULT_DAYS, MAX_DAYS, ActivityWindow

MAX_LINES = 10
"""Actions listed per chain; the rest are counted, never dropped silently."""

_PORTUGUESE_ASK = re.compile(
    r"\b(?:carteira|transa[cç][oõ]es|movimenta\w*|fez|ontem|semana|[uú]ltimos|dias"
    r"|hist[oó]rico)\b",
    re.IGNORECASE,
)

EN = {
    "header": "Activity of {who} — {span}",
    "period": "_Period: {span}{note}._",
    "default": f" (no period named, so the last {DEFAULT_DAYS} days)",
    "clamped": f" (at most {MAX_DAYS} days are read at a time)",
    "chain": "**{chain}** — {count}",
    "one": "1 action",
    "many": "{count} actions",
    "empty": "{chains} — no activity",
    "unread": "**{chain}** — could not be read ({reason}); this is not the same as no activity",
    "more": "…and {count} more.",
    "external": "an external address",
    "swap": "swapped {out} → {into}{usd}",
    "open": "Uniswap: opened a position{token} — {flows}{usd}",
    "add": "Uniswap: added liquidity — {flows}{usd}",
    "remove": "Uniswap: removed liquidity — {flows}{usd}",
    "collect": "Uniswap: collected fees — {flows}{usd}",
    "withdrawal": "Uniswap: LP withdrawal (liquidity and/or fees) — {flows}{usd}",
    "change": "Uniswap: liquidity change — {flows}{usd}",
    "lending": "Aave: {parts}",
    "sent": "sent {amounts}{usd} to {who}",
    "received": "received {amounts}{usd} from {who}",
    "other": "called {method} on {who}",
    "gas": "Gas paid by this wallet: {eth} ETH{usd} on {count}",
    "gas_none": "Gas paid by this wallet: none",
    "tx_one": "1 transaction",
    "tx_many": "{count} transactions",
    "relayed": "; {count} more submitted by a relayer (any cut it took is listed as a transfer)",
    "hidden": "Hidden: {items}",
    "unsolicited": "{count} unsolicited token transfer(s)",
    "zero_value": "{count} zero-value transfer(s)",
    "dust": "{count} dust deposit(s) under $0.01",
    "lookalike": (
        "⚠️ {count} of the hidden transfers used an address imitating one this "
        "wallet really transacted with (same first and last characters): address "
        "poisoning. Copy an address in full from a trusted place, never from the "
        "wallet's history."
    ),
    "truncated": "More activity than shown: only the latest {rows} records were read.",
    "no_wrappers": "Aave's tokens could not be identified, so Aave actions may be missing.",
    "footer": (
        "_USD at current prices (each chain's Aave oracle), not at the time of each "
        "action. Only tokens this deployment recognises are shown: USDC, USDT, DAI, "
        "WETH and each chain's Aave assets._"
    ),
    "verbs": {
        "supplied": "supplied",
        "withdrew": "withdrew",
        "borrowed": "borrowed",
        "repaid": "repaid",
        "received": "received",
        "paid": "paid",
    },
    "time": "%d %b %H:%M",
    "span": "%d %b %Y %H:%M",
}

PT = {
    "header": "Atividade de {who} — {span}",
    "period": "_Período: {span}{note}._",
    "default": f" (nenhum período pedido, então os últimos {DEFAULT_DAYS} dias)",
    "clamped": f" (leio no máximo {MAX_DAYS} dias por vez)",
    "chain": "**{chain}** — {count}",
    "one": "1 ação",
    "many": "{count} ações",
    "empty": "{chains} — nenhuma atividade",
    "unread": (
        "**{chain}** — não foi possível ler ({reason}); isso não é o mesmo que sem atividade"
    ),
    "more": "…e mais {count}.",
    "external": "um endereço externo",
    "swap": "trocou {out} → {into}{usd}",
    "open": "Uniswap: abriu uma posição{token} — {flows}{usd}",
    "add": "Uniswap: adicionou liquidez — {flows}{usd}",
    "remove": "Uniswap: removeu liquidez — {flows}{usd}",
    "collect": "Uniswap: coletou taxas — {flows}{usd}",
    "withdrawal": "Uniswap: saque de LP (liquidez e/ou taxas) — {flows}{usd}",
    "change": "Uniswap: mudança de liquidez — {flows}{usd}",
    "lending": "Aave: {parts}",
    "sent": "enviou {amounts}{usd} para {who}",
    "received": "recebeu {amounts}{usd} de {who}",
    "other": "chamou {method} em {who}",
    "gas": "Gas pago por esta carteira: {eth} ETH{usd} em {count}",
    "gas_none": "Gas pago por esta carteira: nenhum",
    "tx_one": "1 transação",
    "tx_many": "{count} transações",
    "relayed": (
        "; mais {count} enviadas por um relayer (qualquer valor que ele cobrou "
        "aparece como transferência)"
    ),
    "hidden": "Ocultos: {items}",
    "unsolicited": "{count} transferência(s) de token não solicitada(s)",
    "zero_value": "{count} transferência(s) de valor zero",
    "dust": "{count} depósito(s) de poeira abaixo de US$ 0,01",
    "lookalike": (
        "⚠️ {count} das transferências ocultas usaram um endereço que imita um com "
        "que esta carteira realmente transacionou (mesmos primeiros e últimos "
        "caracteres): envenenamento de endereço. Copie um endereço inteiro de um "
        "lugar confiável, nunca do histórico da carteira."
    ),
    "truncated": (
        "Há mais atividade do que a mostrada: só os {rows} registros mais recentes "
        "foram lidos."
    ),
    "no_wrappers": "Não consegui identificar os tokens do Aave, então ações no Aave podem faltar.",
    "footer": (
        "_USD a preços atuais (oráculo do Aave de cada rede), não do momento de cada "
        "ação. Só aparecem tokens que esta instalação reconhece: USDC, USDT, DAI, "
        "WETH e os ativos do Aave de cada rede._"
    ),
    "verbs": {
        "supplied": "depositou",
        "withdrew": "sacou",
        "borrowed": "pegou emprestado",
        "repaid": "pagou",
        "received": "recebeu",
        "paid": "enviou",
    },
    "time": "%d/%m %H:%M",
    "span": "%d/%m/%Y %H:%M",
}

REASONS_PT = {"timed out": "tempo esgotado", "could not be reached": "sem resposta"}
_PORTUGUESE_DIGITS = str.maketrans(",.", ".,")


def activity_language(question: str) -> Language:
    """Portuguese or English, never unknown: every line is fixed text."""
    detected = detect(question)
    if detected.known:
        return detected
    return Language.PORTUGUESE if _PORTUGUESE_ASK.search(question) else Language.ENGLISH


@dataclass(frozen=True, slots=True)
class _Words:
    """One language's phrasing, and whether counterparties may be named."""

    pt: bool
    private: bool

    @property
    def text(self) -> Mapping[str, object]:
        return PT if self.pt else EN

    def __call__(self, key: str, **values: object) -> str:
        return str(self.text[key]).format(**values)

    def verb(self, verb: str) -> str:
        verbs = self.text["verbs"]
        return verbs[verb] if isinstance(verbs, dict) else verb

    def number(self, value: Decimal) -> str:
        figure = amount(value)
        return figure.translate(_PORTUGUESE_DIGITS) if self.pt else figure

    def usd(self, value: Decimal | None) -> str:
        if value is None:
            return ""
        figure = f"{value:,.2f}"
        return f" (≈ US$ {figure.translate(_PORTUGUESE_DIGITS)})" if self.pt else f" (≈ ${figure})"

    def moment(self, when: datetime, key: str = "time") -> str:
        return when.strftime(str(self.text[key]))

    def reason(self, reason: str) -> str:
        return REASONS_PT.get(reason, reason) if self.pt else reason

    def who(self, address: str) -> str:
        return f"`{address}`" if self.private else self("external")

    def count(self, one: str, many: str, count: int) -> str:
        return self(one) if count == 1 else self(many, count=count)


# --- the answer -------------------------------------------------------------------


def render_activity(
    address: str,
    window: ActivityWindow,
    chains: Sequence[ChainActivity],
    known: dict[str, Recognised],
    language: Language,
    *,
    private: bool,
) -> str:
    """The whole answer. `known` is each chain's recognised tokens, by chain key."""
    words = _Words(language is Language.PORTUGUESE, private)
    span = f"{words.moment(window.start, 'span')} – {words.moment(window.end, 'span')} UTC"
    # The header is dropped from the body but quoted by the citation footer,
    # which is posted too: in a channel it names the wallet as the body does.
    who = f"`{address}`" if private else f"…{address[-4:]}"
    note = words("clamped") if window.clamped else "" if window.named else words("default")
    lines = [words("header", who=who, span=span), words("period", span=span, note=note)]
    quiet = [c.chain.name for c in chains if c.unreachable is None and not _has_news(c)]
    for chain in chains:
        if chain.unreachable is not None:
            reason = words.reason(chain.unreachable)
            lines.extend(("", words("unread", chain=chain.chain.name, reason=reason)))
        elif _has_news(chain):
            lines.append("")
            lines.extend(_chain_lines(chain, known[chain.chain.key], words))
    if quiet:
        lines.extend(("", words("empty", chains=", ".join(f"**{n}**" for n in quiet))))
    lines.extend(("", words("footer")))
    return "\n".join(lines)


def _has_news(chain: ChainActivity) -> bool:
    return bool(chain.actions or chain.hidden.total or chain.gas_transactions or chain.truncated)


def _chain_lines(chain: ChainActivity, known: Recognised, words: _Words) -> list[str]:
    count = words.count("one", "many", len(chain.actions))
    lines = [words("chain", chain=chain.chain.name, count=count)]
    for action in chain.actions[:MAX_LINES]:
        lines.append(f"`{words.moment(action.at)}` {_action_text(action, chain, known, words)}")
    if len(chain.actions) > MAX_LINES:
        lines.append(words("more", count=len(chain.actions) - MAX_LINES))
    lines.append(_gas_line(chain, known, words))
    lines.extend(_warnings(chain, words))
    return lines


def _action_text(action: Action, chain: ChainActivity, known: Recognised, words: _Words) -> str:
    kind = action.kind
    if kind is Kind.SWAP:
        return _swap_text(action, chain, known, words)
    if kind is Kind.LIQUIDITY:
        token = f" #{action.token_id}" if action.token_id else ""
        flows = _flows(action.legs, words, signed=True)
        usd = words.usd(_worth(action.legs, chain, known))
        return words(action.detail or "change", token=token, flows=flows, usd=usd)
    if kind is Kind.LENDING:
        parts = "; ".join(_part_text(p, chain, known, words) for p in action.parts)
        return words("lending", parts=parts)
    if kind is Kind.OTHER:
        return words("other", method=f"`{action.detail or '?'}`", who=_party(action.called, words))
    counterparty = action.legs[0].counterparty if action.legs else ""
    return words(
        str(kind),
        amounts=_flows(action.legs, words, signed=False),
        usd=words.usd(_worth(action.legs, chain, known)),
        who=_party(counterparty, words),
    )


def _part_text(part: Part, chain: ChainActivity, known: Recognised, words: _Words) -> str:
    worth = leg_usd(Leg(part.asset, part.symbol, part.amount, ""), chain.prices, known)
    return f"{words.verb(part.verb)} {words.number(part.amount)} {part.symbol}{words.usd(worth)}"


def _swap_text(action: Action, chain: ChainActivity, known: Recognised, words: _Words) -> str:
    net = net_by_asset(action.legs)
    gave = [leg for leg in _net_legs(action.legs, net) if leg.amount < 0]
    got = [leg for leg in _net_legs(action.legs, net) if leg.amount > 0]
    worth = _worth(gave, chain, known) or _worth(got, chain, known)
    return words(
        "swap",
        out=_flows(gave, words, signed=False),
        into=_flows(got, words, signed=False),
        usd=words.usd(worth),
    )


def _net_legs(legs: Sequence[Leg], net: dict[str, Decimal]) -> list[Leg]:
    """One leg per asset, carrying its net amount."""
    first: dict[str, Leg] = {}
    for leg in legs:
        first.setdefault(leg.token, leg)
    return [
        Leg(token, first[token].symbol, value, first[token].counterparty, first[token].wrapper)
        for token, value in net.items()
    ]


def _flows(legs: Sequence[Leg], words: _Words, *, signed: bool) -> str:
    net = net_by_asset(legs)
    shown = []
    for leg in _net_legs(legs, net):
        sign = ("+" if leg.amount > 0 else "−") if signed else ""
        shown.append(f"{sign}{words.number(abs(leg.amount))} {leg.symbol}")
    return ", ".join(shown) or "—"


def _worth(legs: Sequence[Leg], chain: ChainActivity, known: Recognised) -> Decimal | None:
    """What the legs are worth now, or None when any of them is unpriced."""
    values = [leg_usd(leg, chain.prices, known) for leg in legs]
    if not values or any(v is None for v in values):
        return None
    return sum((v or Decimal(0) for v in values), Decimal(0))


def _party(address: str, words: _Words) -> str:
    return words.who(address) if address else "?"


def _gas_line(chain: ChainActivity, known: Recognised, words: _Words) -> str:
    relayed = words("relayed", count=chain.relayed) if chain.relayed else ""
    if not chain.gas_transactions:
        return words("gas_none") + relayed
    price = chain.prices.get(known.weth)
    usd = words.usd(None if price is None else chain.gas * price)
    count = words.count("tx_one", "tx_many", chain.gas_transactions)
    return words("gas", eth=words.number(chain.gas), usd=usd, count=count) + relayed


def _warnings(chain: ChainActivity, words: _Words) -> list[str]:
    hidden = chain.hidden
    items = [
        words(key, count=count)
        for key, count in (
            ("unsolicited", hidden.unsolicited),
            ("zero_value", hidden.zero_value),
            ("dust", hidden.dust),
        )
        if count
    ]
    lines = [words("hidden", items=", ".join(items))] if items else []
    if hidden.lookalike:
        lines.append(words("lookalike", count=hidden.lookalike))
    if chain.truncated:
        lines.append(words("truncated", rows=MAX_ROWS))
    if not chain.wrappers_known:
        lines.append(words("no_wrappers"))
    return lines

