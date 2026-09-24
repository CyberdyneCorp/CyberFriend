"""A portfolio as Discord markdown, figures only, in the asker's language.

Shown verbatim (see `verbatim_answer`): the first line is a header the citation
already carries and is dropped, and everything after it is exactly what the
person reads. No model writes a number here.

The language is the question's. The verbatim path has no synthesizer to put an
answer into Portuguese, so the adapter does it, from the asker's own question
as the clearance carries it -- never from anything the model wrote.

Addresses are not printed. A saved wallet is shown only in a DM, and this
answer can be given in a channel; several wallets are told apart by their last
four characters, which the owner recognises and nobody else can look up.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from chatmemory.adapters.chain.portfolio import (
    ChainPortfolio,
    Held,
    Section,
    Total,
    WalletPortfolio,
    total,
)
from chatmemory.adapters.chain.positions_render import DUST_USD, amount
from chatmemory.app.language import Language, detect

MAX_LISTED = 4
"""Wallet balances named on a chain's line; the rest are counted."""

_PORTUGUESE_ASK = re.compile(
    r"\b(?:quanto|tenho|patrim[oô]nio|portf[oó]lio|carteira|no\s+total|ao\s+todo)\b",
    re.IGNORECASE,
)
"""Words of a portfolio question that `detect` does not count. "quanto eu
tenho no total?" has no function word it knows, and must still be answered in
Portuguese."""

EN = {
    "header": "Portfolio for {who}",
    "wallet_heading": "__Wallet …{tail}__ — {usd}",
    "chain": "**{chain}** — {usd}",
    "partial": "at least {usd}",
    "empty_chains": "{chains} — nothing held",
    "wallet": "  Wallet {usd} · {items}",
    "more": "+{count} more",
    "liquidity": "  Liquidity {usd}{fees} · {positions}, {in_range} in range",
    "fees": " (incl. {usd} uncollected fees)",
    "position_one": "1 Uniswap position",
    "position_many": "{count} Uniswap positions",
    "lending": "  Aave net {usd} · {supplied} supplied − {borrowed} borrowed{health}",
    "health": " · HF {value}",
    "unread": "  ⚠️ Not read: {section} ({reason})",
    "total": "**Total ≈ {usd}**",
    "at_least": "**At least {usd}** — not read: {missing}",
    "unpriced": "_No market price, so not counted: {items}._",
    "excluded": (
        "_Not included: tokens other than USDC, USDT, DAI, WETH and each chain's "
        "Aave v3 assets; other exchanges and protocols (Aerodrome, SushiSwap, "
        "Uniswap v2 LP tokens); other Aave markets._"
    ),
    "prices": "_Prices: each chain's Aave oracle{fallback}._",
    "fallback": ", ETH from CoinGecko where the oracle did not answer",
    "details": '_Ask "details of my pools" or "my Aave positions" for each position._',
}

PT = {
    "header": "Portfólio de {who}",
    "wallet_heading": "__Carteira …{tail}__ — {usd}",
    "chain": "**{chain}** — {usd}",
    "partial": "pelo menos {usd}",
    "empty_chains": "{chains} — nada",
    "wallet": "  Carteira {usd} · {items}",
    "more": "+{count} outros",
    "liquidity": "  Liquidez {usd}{fees} · {positions}, {in_range} na faixa",
    "fees": " (inclui {usd} em taxas não coletadas)",
    "position_one": "1 posição na Uniswap",
    "position_many": "{count} posições na Uniswap",
    "lending": "  Aave líquido {usd} · {supplied} depositados − {borrowed} emprestados{health}",
    "health": " · HF {value}",
    "unread": "  ⚠️ Não lido: {section} ({reason})",
    "total": "**Total ≈ {usd}**",
    "at_least": "**Pelo menos {usd}** — não lido: {missing}",
    "unpriced": "_Sem preço de mercado, então não somado: {items}._",
    "excluded": (
        "_Não incluído: tokens além de USDC, USDT, DAI, WETH e dos ativos do Aave v3 "
        "de cada rede; outras exchanges e protocolos (Aerodrome, SushiSwap, tokens "
        "de LP da Uniswap v2); outros mercados do Aave._"
    ),
    "prices": "_Preços: oráculo do Aave de cada rede{fallback}._",
    "fallback": ", ETH pela CoinGecko onde o oráculo não respondeu",
    "details": (
        '_Pergunte "detalhes das minhas pools" ou "minhas posições no Aave" '
        "para ver cada posição._"
    ),
}

SECTIONS = {
    Language.ENGLISH: {
        Section.WALLET: "wallet balances",
        Section.LIQUIDITY: "Uniswap",
        Section.LENDING: "Aave",
    },
    Language.PORTUGUESE: {
        Section.WALLET: "saldo da carteira",
        Section.LIQUIDITY: "Uniswap",
        Section.LENDING: "Aave",
    },
}

REASONS_PT = {"timed out": "tempo esgotado", "could not be reached": "sem resposta"}


def portfolio_language(question: str) -> Language:
    """Portuguese or English, never unknown: every line is fixed text."""
    detected = detect(question)
    if detected.known:
        return detected
    return Language.PORTUGUESE if _PORTUGUESE_ASK.search(question) else Language.ENGLISH


class _Words:
    """The phrasing and number format of one language."""

    def __init__(self, language: Language) -> None:
        self.language = language
        self.pt = language is Language.PORTUGUESE
        self.text = PT if self.pt else EN
        self.sections = SECTIONS[Language.PORTUGUESE if self.pt else Language.ENGLISH]

    def __call__(self, key: str, **values: object) -> str:
        return self.text[key].format(**values)

    def usd(self, value: Decimal) -> str:
        figure = f"{value:,.2f}"
        return f"US$ {_swap(figure)}" if self.pt else f"${figure}"

    def number(self, value: Decimal) -> str:
        figure = amount(value)
        return _swap(figure) if self.pt else figure

    def reason(self, reason: str) -> str:
        return REASONS_PT.get(reason, reason) if self.pt else reason


_PORTUGUESE_DIGITS = str.maketrans(",.", ".,")


def _swap(figure: str) -> str:
    """1,234.56 as Portuguese writes it: 1.234,56."""
    return figure.translate(_PORTUGUESE_DIGITS)


# --- the answer ---------------------------------------------------------------


def render_portfolio(wallets: Sequence[WalletPortfolio], language: Language) -> str:
    words = _Words(language)
    # The header is dropped from the body but quoted by the citation footer,
    # which is posted too: it names wallets the same way the body does.
    who = ", ".join(f"…{w.address[-4:]}" for w in wallets)
    lines = [words("header", who=who)]
    for wallet in wallets:
        lines.append("")
        if len(wallets) > 1:
            lines.append(_wallet_heading(wallet, words))
        lines.extend(_chain_lines(wallet.chains, words))
    lines.append("")
    lines.append(_headline(total(wallets), words))
    lines.extend(_footer(wallets, words))
    return "\n".join(lines)


def _wallet_heading(wallet: WalletPortfolio, words: _Words) -> str:
    subtotal = total([wallet])
    usd = words.usd(subtotal.usd)
    if not subtotal.complete:
        usd = words("partial", usd=usd)
    return words("wallet_heading", tail=wallet.address[-4:], usd=usd)


def _chain_lines(chains: Sequence[ChainPortfolio], words: _Words) -> list[str]:
    """Chains by value, largest first; chains holding nothing share one line."""
    held = sorted((c for c in chains if not c.empty), key=lambda c: c.usd(), reverse=True)
    empty = [c.chain.name for c in chains if c.empty]
    lines: list[str] = []
    for chain in held:
        lines.extend(_chain_block(chain, words))
    if empty:
        lines.append(words("empty_chains", chains=", ".join(f"**{n}**" for n in empty)))
    return lines


def _chain_block(chain: ChainPortfolio, words: _Words) -> list[str]:
    usd = words.usd(chain.usd())
    if chain.unreadable():
        usd = words("partial", usd=usd)
    lines = [words("chain", chain=chain.chain.name, usd=usd)]
    if chain.wallet.holdings:
        lines.append(words("wallet", usd=words.usd(chain.wallet_usd()),
                           items=_holdings(chain.wallet.holdings, words)))
    if chain.liquidity.positions:
        lines.append(_liquidity_line(chain, words))
    if not chain.lending.unreachable and not chain.lending.empty:
        lines.append(_lending_line(chain, words))
    lines.extend(f"  _{note}._" for note in chain.liquidity.notes)
    for section in chain.unreadable():
        reason = _reason(chain, section)
        lines.append(words("unread", section=words.sections[section],
                           reason=words.reason(reason)))
    return lines


def _reason(chain: ChainPortfolio, section: Section) -> str:
    return {
        Section.WALLET: chain.wallet.unreachable,
        Section.LIQUIDITY: chain.liquidity.unreachable,
        Section.LENDING: chain.lending.unreachable,
    }[section]


def _holdings(holdings: Sequence[Held], words: _Words) -> str:
    """The balances worth naming, most valuable first; dust is summed, not named."""
    shown = sorted(
        (h for h in holdings if h.usd is None or h.usd >= DUST_USD),
        key=lambda h: h.usd if h.usd is not None else Decimal(-1),
        reverse=True,
    )
    items = [f"{words.number(h.amount)} {h.symbol}" for h in shown[:MAX_LISTED]]
    if len(shown) > MAX_LISTED:
        items.append(words("more", count=len(shown) - MAX_LISTED))
    return ", ".join(items) or words.usd(Decimal(0))


def _liquidity_line(chain: ChainPortfolio, words: _Words) -> str:
    positions = chain.liquidity.positions
    fees = chain.fees_usd()
    count = len(positions)
    return words(
        "liquidity",
        usd=words.usd(chain.liquidity_usd()),
        fees=words("fees", usd=words.usd(fees)) if fees >= DUST_USD else "",
        positions=words("position_one") if count == 1 else words("position_many", count=count),
        in_range=sum(p.in_range for p in positions),
    )


def _lending_line(chain: ChainPortfolio, words: _Words) -> str:
    priced = [a for a in chain.lending.assets if a.usd_price is not None]
    supplied = sum((a.supplied * (a.usd_price or 0) for a in priced), Decimal(0))
    borrowed = sum((a.borrowed * (a.usd_price or 0) for a in priced), Decimal(0))
    health = chain.lending.health_factor
    return words(
        "lending",
        usd=words.usd(chain.lending_net_usd()),
        supplied=words.usd(supplied),
        borrowed=words.usd(borrowed),
        health="" if health is None else words("health", value=_swap_if(words, f"{health:.2f}")),
    )


def _swap_if(words: _Words, figure: str) -> str:
    return _swap(figure) if words.pt else figure


def _headline(grand: Total, words: _Words) -> str:
    usd = words.usd(grand.usd)
    if grand.complete:
        return words("total", usd=usd)
    by_chain: dict[str, list[str]] = {}
    for chain, section in grand.missing:
        by_chain.setdefault(chain.name, []).append(words.sections[section])
    missing = "; ".join(f"{name} ({', '.join(parts)})" for name, parts in by_chain.items())
    return words("at_least", usd=usd, missing=missing)


def _footer(wallets: Sequence[WalletPortfolio], words: _Words) -> list[str]:
    chains = [c for w in wallets for c in w.chains]
    lines: list[str] = []
    unpriced = sorted({item for c in chains for item in c.unpriced()})
    if unpriced:
        lines.append(words("unpriced", items=", ".join(unpriced)))
    lines.append(words("excluded"))
    fallback = any(c.wallet.fallback_priced for c in chains)
    lines.append(words("prices", fallback=words("fallback") if fallback else ""))
    if any(c.liquidity.positions or not c.lending.empty for c in chains):
        lines.append(words("details"))
    return lines
