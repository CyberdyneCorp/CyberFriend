"""A second figure beside US dollars, in the currency the asker prefers.

Every figure the assistant reads is in US dollars: CoinGecko quotes BTC and ETH
in dollars, and the chain readers value positions with Aave's dollar oracles.
A person who said "prefiro ver em reais" gets the dollar figure and, beside it,
the same amount in reais -- "US$ 63.210,00 (R$ 345.120,00)".

Three rules shape this module:

*   **Converted in code, never by a model.** A rendered answer multiplies by
    the rate itself, and a market answer carries the converted figure in the
    tool result, so no model is ever asked to do arithmetic on money.
*   **One rate, read the usual way.** The rate is the market FX provider's
    daily ECB reference rate, USD to the preferred code, read once and cached
    briefly by the adapter behind `UsdRates`. The code sent is a member of
    `domain.currency.SUPPORTED_CODES` -- the stored preference, which cannot be
    anything else -- and nothing about the person goes with it.
*   **No rate, no second figure.** A rate that cannot be read leaves the answer
    in dollars alone. It never fails the answer and is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import structlog

from chatmemory.app.asker import current_facts
from chatmemory.app.language import Language, detect, language_named
from chatmemory.domain.currency import CURRENCIES, SUPPORTED_CODES, USD
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import FactKind, PersonalFacts

log = structlog.get_logger()

EN, PT = Language.ENGLISH, Language.PORTUGUESE

CURRENCY_NAMES: dict[Language, dict[str, str]] = {
    EN: {
        "AUD": "Australian dollar", "BRL": "Brazilian real", "CAD": "Canadian dollar",
        "CHF": "Swiss franc", "CNY": "Chinese yuan", "CZK": "Czech koruna",
        "DKK": "Danish krone", "EUR": "euro", "GBP": "pound sterling",
        "HKD": "Hong Kong dollar", "HUF": "Hungarian forint", "IDR": "Indonesian rupiah",
        "ILS": "Israeli shekel", "INR": "Indian rupee", "ISK": "Icelandic króna",
        "JPY": "Japanese yen", "KRW": "South Korean won", "MXN": "Mexican peso",
        "MYR": "Malaysian ringgit", "NOK": "Norwegian krone", "NZD": "New Zealand dollar",
        "PHP": "Philippine peso", "PLN": "Polish złoty", "RON": "Romanian leu",
        "SEK": "Swedish krona", "SGD": "Singapore dollar", "THB": "Thai baht",
        "TRY": "Turkish lira", USD: "US dollar", "ZAR": "South African rand",
    },
    PT: {
        "AUD": "dólar australiano", "BRL": "real brasileiro", "CAD": "dólar canadense",
        "CHF": "franco suíço", "CNY": "yuan chinês", "CZK": "coroa tcheca",
        "DKK": "coroa dinamarquesa", "EUR": "euro", "GBP": "libra esterlina",
        "HKD": "dólar de Hong Kong", "HUF": "florim húngaro", "IDR": "rupia indonésia",
        "ILS": "shekel israelense", "INR": "rupia indiana", "ISK": "coroa islandesa",
        "JPY": "iene", "KRW": "won sul-coreano", "MXN": "peso mexicano",
        "MYR": "ringgit malaio", "NOK": "coroa norueguesa", "NZD": "dólar neozelandês",
        "PHP": "peso filipino", "PLN": "zloty polonês", "RON": "leu romeno",
        "SEK": "coroa sueca", "SGD": "dólar de Singapura", "THB": "baht tailandês",
        "TRY": "lira turca", USD: "dólar americano", "ZAR": "rand sul-africano",
    },
}


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


def currency_name(code: str, language: Language) -> str:
    """"real brasileiro (BRL)" / "Brazilian real (BRL)"."""
    return f"{CURRENCY_NAMES[_lang(language)][code]} ({code})"


def supported_list() -> str:
    """Every supported code, for the reply that refuses an unknown one."""
    return ", ".join(sorted(SUPPORTED_CODES))


_PORTUGUESE_DIGITS = str.maketrans(",.", ".,")


def money(value: Decimal, code: str, language: Language) -> str:
    """An amount as its reader writes it: "R$ 345.120,00" or "R$345,120.00".

    Portuguese groups with dots and a decimal comma, and puts a space after the
    symbol; English the other way round. A currency with no symbol of its own
    is written with its code ("CHF 1,234.56"), which reads the same in both.
    """
    currency = CURRENCIES[code]
    quantum = Decimal(1).scaleb(-currency.places)
    figure = f"{value.quantize(quantum):,.{currency.places}f}"
    pt = language is PT
    if pt:
        figure = figure.translate(_PORTUGUESE_DIGITS)
    if not currency.symbol:
        return f"{code} {figure}"
    return f"{currency.symbol} {figure}" if pt else f"{currency.symbol}{figure}"


@dataclass(frozen=True, slots=True)
class Conversion:
    """US dollars into one preferred currency, at one rate, for one reader."""

    code: str
    #: Units of `code` per US dollar.
    rate: Decimal
    language: Language = EN

    def shown(self, usd: Decimal) -> str:
        """The dollar amount in this currency: "R$ 345.120,00"."""
        return money(usd * self.rate, self.code, self.language)


def beside(conversion: Conversion | None, usd: Decimal | None, *, wrap: str = " ({})") -> str:
    """The converted figure to put after a dollar one, or "" when there is none."""
    if conversion is None or usd is None:
        return ""
    return wrap.format(conversion.shown(usd))


_RATE_NOTE = {
    EN: "_{code} at the daily reference rate: 1 USD = {rate} {code} (Frankfurter/ECB)._",
    PT: "_{code} pela taxa de referência diária: 1 USD = {rate} {code} (Frankfurter/BCE)._",
}


def rate_note(conversion: Conversion | None) -> str:
    """The footnote saying which rate a converted figure used, or "".

    A daily reference rate is a record of a fixing, not a live rate, and the
    converted figure is only as current as it -- so a rendered answer says so.
    """
    if conversion is None:
        return ""
    rate = f"{conversion.rate:,.4f}"
    if conversion.language is PT:
        rate = rate.translate(_PORTUGUESE_DIGITS)
    return _RATE_NOTE[_lang(conversion.language)].format(code=conversion.code, rate=rate)


def rate_lines(conversion: Conversion | None) -> list[str]:
    """`rate_note` as the lines to append to a rendered answer: none, or one."""
    note = rate_note(conversion)
    return [note] if note else []


class UsdRates(Protocol):
    """Where the USD rate of a supported currency comes from."""

    async def usd_to(self, code: str) -> Decimal | None:
        """Units of `code` per US dollar, or None. Must not raise: a missing
        rate leaves an answer in dollars, it never fails one."""
        ...


async def conversion_to(
    code: str | None, rates: UsdRates | None, language: Language
) -> Conversion | None:
    """The conversion for a stored preference, or None when there is nothing to add.

    None for no preference, for dollars themselves -- the figure is already in
    them -- and for a rate that could not be read.
    """
    if rates is None or code is None or code == USD or code not in SUPPORTED_CODES:
        return None
    rate = await rates.usd_to(code)
    if rate is None:
        log.info("currency.rate_unavailable", currency=code)
        return None
    return Conversion(code, rate, language)


async def asker_conversion(
    rates: UsdRates | None, question: str, language: Language | None = None
) -> Conversion | None:
    """The current asker's conversion, for a tool rendering its answer.

    The preference is read from the asker's facts for this answer (see
    `app.asker.answering_with_facts`), never from anything the model wrote;
    outside an answer there are none, and nothing is added. `language` is the
    one the renderer already settled on, when it has its own rule: the second
    figure and its footnote must read like the rest of the answer.
    """
    facts = current_facts()
    if facts is None or facts.preferred_currency is None:
        return None
    written = language if language is not None else figure_language(question)
    return await conversion_to(facts.preferred_currency, rates, written)


def figure_language(question: str) -> Language:
    """How the figures are written: the question's language, else the saved one."""
    detected = detect(question)
    if detected.known:
        return detected
    facts = current_facts()
    return language_named(facts.preferred_language if facts else None)


class FactReader(Protocol):
    """The one read `PreferredCurrencies` needs of a fact store."""

    async def facts_of(self, viewer: Viewer) -> PersonalFacts: ...


class PreferredCurrencies:
    """A person's conversion, for a message with no answer around it.

    An alert is sent by a sweep, not in reply to a question, so there are no
    asker facts for this answer to read. This reads the one person's stored
    preference instead -- keyed by the alert's owner, the only reader of the
    direct message it goes into.
    """

    def __init__(self, facts: FactReader, rates: UsdRates) -> None:
        self._facts = facts
        self._rates = rates

    async def conversion_for(self, person: PersonRef, language: Language) -> Conversion | None:
        try:
            # No channels: only the person's own facts are read, never a message.
            stored = await self._facts.facts_of(Viewer(person, frozenset()))
        except Exception:  # noqa: BLE001 - no preference is a dollar-only message
            log.exception("currency.preference_failed", person=str(person))
            return None
        code = stored.get(FactKind.PREFERRED_CURRENCY)
        return await conversion_to(code, self._rates, language)
