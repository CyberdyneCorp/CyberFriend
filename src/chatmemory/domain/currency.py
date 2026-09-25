"""The currencies a person may ask to see money in, besides US dollars.

A closed set, and deliberately the rate source's own: every code here is one
Frankfurter publishes a daily ECB reference rate for, so a preference that is
accepted is one that can always be converted, and a code that leaves for the
rate lookup is always a member of this set -- never text somebody typed.

People name a currency, they rarely type its code: "reais", "real
brasileiro", "euros", "libra". `currency_code` reads those in Portuguese and
English. A few currencies people do name are not published by the source
(the Argentine peso, the Chilean peso); they are recognised as currencies so
the reply can say which ones are supported, and are never stored.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

USD = "USD"


@dataclass(frozen=True, slots=True)
class Currency:
    """How one supported currency is written."""

    code: str
    #: Written before the figure ("R$", "€"), or "" to write the code instead.
    symbol: str = ""
    #: Places after the decimal point: none for the yen or the won.
    places: int = 2


_TABLE = (
    Currency("AUD", "A$"),
    Currency("BRL", "R$"),
    Currency("CAD", "C$"),
    Currency("CHF"),
    Currency("CNY", "CN¥"),
    Currency("CZK"),
    Currency("DKK"),
    Currency("EUR", "€"),
    Currency("GBP", "£"),
    Currency("HKD", "HK$"),
    Currency("HUF"),
    Currency("IDR"),
    Currency("ILS", "₪"),
    Currency("INR", "₹"),
    Currency("ISK", places=0),
    Currency("JPY", "¥", places=0),
    Currency("KRW", "₩", places=0),
    Currency("MXN", "MX$"),
    Currency("MYR"),
    Currency("NOK"),
    Currency("NZD", "NZ$"),
    Currency("PHP", "₱"),
    Currency("PLN"),
    Currency("RON"),
    Currency("SEK"),
    Currency("SGD", "S$"),
    Currency("THB", "฿"),
    Currency("TRY", "₺"),
    Currency(USD, "US$"),
    Currency("ZAR"),
)

CURRENCIES: Mapping[str, Currency] = MappingProxyType({c.code: c for c in _TABLE})
"""Every currency a preference may name, by ISO 4217 code."""

SUPPORTED_CODES = frozenset(CURRENCIES)

_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "BRL": ("real", "reais", "real brasileiro", "reais brasileiros", "brazilian real",
                "brazilian reais", "r$"),
        USD: ("dolar", "dolares", "dolar americano", "dolares americanos", "dollar",
              "dollars", "us dollar", "us dollars", "american dollar", "us$"),
        "EUR": ("euro", "euros", "€"),
        "GBP": ("libra", "libras", "libra esterlina", "libras esterlinas", "pound",
                "pounds", "pound sterling", "british pound", "british pounds", "£"),
        "JPY": ("iene", "ienes", "yen", "japanese yen"),
        "CHF": ("franco suico", "francos suicos", "swiss franc", "swiss francs"),
        "CAD": ("dolar canadense", "dolares canadenses", "canadian dollar",
                "canadian dollars"),
        "AUD": ("dolar australiano", "dolares australianos", "australian dollar",
                "australian dollars"),
        "MXN": ("peso mexicano", "pesos mexicanos", "mexican peso", "mexican pesos"),
        "CNY": ("yuan", "iuane", "renminbi", "yuan chines", "chinese yuan"),
        "INR": ("rupia", "rupias", "rupia indiana", "rupee", "rupees", "indian rupee"),
        "KRW": ("won", "won sul-coreano", "korean won"),
        "TRY": ("lira turca", "liras turcas", "turkish lira"),
        "ZAR": ("rand", "rand sul-africano", "south african rand"),
        "SEK": ("coroa sueca", "coroas suecas", "swedish krona"),
        "NOK": ("coroa norueguesa", "coroas norueguesas", "norwegian krone"),
        "DKK": ("coroa dinamarquesa", "coroas dinamarquesas", "danish krone"),
        "PLN": ("zloty", "zlotys"),
    }
)

UNSUPPORTED_NAMES = frozenset({
    "peso argentino", "pesos argentinos", "argentine peso", "argentine pesos",
    "peso chileno", "pesos chilenos", "chilean peso", "chilean pesos",
    "peso colombiano", "pesos colombianos", "colombian peso", "colombian pesos",
    "sol peruano", "peruvian sol", "bitcoin", "bitcoins", "ether", "ethereum",
})
"""Money people name that the rate source does not publish -- or that is not a
currency to convert into at all. Recognised only to be refused by name."""

_ALIASES: Mapping[str, str] = MappingProxyType(
    {name: code for code, names in _NAMES.items() for name in names}
)

_ARTICLE = re.compile(r"^(?:o|a|os|as|em|in|the)\s+")


def _fold(text: str) -> str:
    """Lowercase, accents off, whitespace collapsed: "Dólares " is "dolares"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.strip(" .,;:!?").split())


def currency_code(text: str) -> str | None:
    """The supported code `text` names ("reais", "o euro", "BRL"), or None."""
    folded = _ARTICLE.sub("", _fold(text))
    code = folded.upper()
    if code in CURRENCIES:
        return code
    return _ALIASES.get(folded)


def names_a_currency(text: str) -> bool:
    """Whether `text` names money at all, supported or not."""
    folded = _ARTICLE.sub("", _fold(text))
    return currency_code(text) is not None or folded in UNSUPPORTED_NAMES


def _written(name: str) -> str:
    # Accents optional, so the pattern matches "dólares" as well as "dolares".
    accented = {"a": "[aá]", "o": "[oóô]", "e": "[eéê]", "i": "[ií]", "u": "[uú]"}
    escaped = re.escape(name).replace(r"\ ", r"\s+")
    return "".join(accented.get(c, c) for c in escaped)


def _alternation() -> str:
    spoken = {n for n in (*_ALIASES, *UNSUPPORTED_NAMES) if n.isascii() and n[0].isalpha()}
    # Longest first, so "real brasileiro" wins over "real".
    names = sorted(spoken, key=len, reverse=True)
    codes = "|".join(sorted(CURRENCIES))
    # A code only in capitals: "uso git" names no currency, "uso BRL" does.
    return "(?:" + "|".join(_written(n) for n in names) + f"|(?-i:{codes}))"


CURRENCY_NAMES = _alternation()
"""A regex alternation of every currency name (and a supported code in capitals),
for the phrases that are about a currency only when one is named: "uso reais"
is a preference, "uso o Discord" is not. Unanchored; the caller bounds it."""
