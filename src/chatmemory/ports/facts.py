"""The personal-facts port: what a person has told the assistant about themselves.

Three properties are fixed by the shape of this interface rather than left to
an implementation to remember:

*   **The set of facts is closed.** `FactKind` lists every kind and the table
    has a CHECK constraint naming the same ones. A free-form "remember this"
    would be text that reaches every later prompt -- a durable injection point
    -- so there is no kind for it to be stored under.

*   **A fact cannot be constructed unvalidated.** `PersonalFact` validates and
    normalises in `__post_init__`, so every fact a store is handed, and every
    fact a store returns, has passed the same check. Validation at the Discord
    edge would hold only for the edge that remembered to do it.

*   **Reads take a `Viewer`, and the viewer is the key.** There is no `person`
    argument on `facts_of`: whose facts are read is the viewer's person. Asking
    for somebody else's facts is not expressible, which is the only form of
    "never disclose another person's email" that does not depend on a caller.

Stored facts are DATA. A preferred name is text the person chose; it is used
as a name and never as an instruction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef, Viewer


class FactKind(StrEnum):
    """The facts a person may set. The values are the stored `kind` column."""

    PREFERRED_NAME = "preferred_name"
    EMAIL = "email"
    PREFERRED_LANGUAGE = "preferred_language"
    PHONE = "phone"
    ETH_WALLET = "eth_wallet"
    BTC_WALLET = "btc_wallet"
    FULL_NAME = "full_name"
    HOME_ADDRESS = "home_address"
    BIRTH_DATE = "birth_date"
    PREFERRED_CURRENCY = "preferred_currency"


MULTI_VALUED_KINDS = frozenset({FactKind.ETH_WALLET, FactKind.BTC_WALLET})
"""Kinds a person may hold several of: one row per value, not per kind.

Somebody with a hardware wallet and a hot wallet has two, and saving the second
used to replace the first -- and delete the alerts that watched it."""

MAX_WALLETS_PER_KIND = 5
"""The most addresses of one chain a person may save. Also the most a
portfolio sums, so every saved wallet fits in one question."""


class FactRejection(StrEnum):
    """Why a value was not accepted. Stable codes, so a reply can say why."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    MALFORMED = "malformed"
    DISALLOWED_CHARACTERS = "disallowed_characters"
    # A multi-valued kind already holding `MAX_WALLETS_PER_KIND` values.
    TOO_MANY = "too_many"


class InvalidFact(ValueError):
    """A value that may not be stored as a fact of its kind."""

    def __init__(self, kind: FactKind, reason: FactRejection) -> None:
        super().__init__(f"{kind.value}: {reason.value}")
        self.kind = kind
        self.reason = reason


# A name is addressed to the person in channel replies, and it reaches the
# prompt. 64 characters holds any real name someone wants to be called and
# leaves no room for a paragraph of instructions.
MAX_PREFERRED_NAME_CHARS = 64
MAX_FULL_NAME_CHARS = 128
# RFC 5321's path limit, and the local-part limit inside it.
MAX_EMAIL_CHARS = 254
MAX_EMAIL_LOCAL_CHARS = 64
# "Português (Brasil)" and "pt-BR" both fit; a sentence does not.
MAX_LANGUAGE_CHARS = 32

# The practical address shape, not the full RFC grammar. The RFC allows
# characters such as backticks, asterisks and pipes in the local part; every
# one of them is Discord markdown, and an address is shown back to its owner in
# a reply. Refusing a rare legal address is recoverable; rendering markup is not.
_EMAIL = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9_%+-])?"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)

# Punctuation real names use. Everything else outside letters, marks, digits
# and spaces is refused: `@` and `#` are mentions, `*_~|` and backticks are
# markdown, `<>` and brackets are link and fence syntax. None of them is part
# of how anybody asks to be addressed.
_NAME_PUNCTUATION = frozenset("'-.’")
_LANGUAGE_PUNCTUATION = frozenset("-()")


def _collapse(value: str) -> str:
    # NFC so "José" typed two ways is one stored value, and whitespace collapsed
    # so a newline cannot survive into a prompt as a line break.
    return " ".join(unicodedata.normalize("NFC", value).split())


def _only(value: str, punctuation: frozenset[str], *, digits: bool) -> bool:
    for char in value:
        category = unicodedata.category(char)
        if category[0] in "LM" or char == " " or char in punctuation:
            continue
        if digits and category == "Nd":
            continue
        # Control and format characters (zero-width joiners, bidi overrides)
        # land here too: they make a stored name read differently from how it
        # displays.
        return False
    return True


def _normalise_name(value: str, kind: FactKind = FactKind.PREFERRED_NAME) -> str:
    name = _collapse(value)
    limit = MAX_FULL_NAME_CHARS if kind is FactKind.FULL_NAME else MAX_PREFERRED_NAME_CHARS
    if not name:
        raise InvalidFact(kind, FactRejection.EMPTY)
    if len(name) > limit:
        raise InvalidFact(kind, FactRejection.TOO_LONG)
    if not _only(name, _NAME_PUNCTUATION, digits=True):
        raise InvalidFact(kind, FactRejection.DISALLOWED_CHARACTERS)
    return name


def _normalise_full_name(value: str) -> str:
    return _normalise_name(value, FactKind.FULL_NAME)


def _normalise_email(value: str) -> str:
    email = value.strip()
    if not email:
        raise InvalidFact(FactKind.EMAIL, FactRejection.EMPTY)
    if len(email) > MAX_EMAIL_CHARS:
        raise InvalidFact(FactKind.EMAIL, FactRejection.TOO_LONG)
    if not _EMAIL.fullmatch(email) or ".." in email:
        raise InvalidFact(FactKind.EMAIL, FactRejection.MALFORMED)
    local, _, domain = email.partition("@")
    if len(local) > MAX_EMAIL_LOCAL_CHARS:
        raise InvalidFact(FactKind.EMAIL, FactRejection.TOO_LONG)
    # Domains are case-insensitive; local parts are not, so only one is folded.
    return f"{local}@{domain.lower()}"


def _normalise_language(value: str) -> str:
    language = _collapse(value)
    if not language:
        raise InvalidFact(FactKind.PREFERRED_LANGUAGE, FactRejection.EMPTY)
    if len(language) > MAX_LANGUAGE_CHARS:
        raise InvalidFact(FactKind.PREFERRED_LANGUAGE, FactRejection.TOO_LONG)
    if not _only(language, _LANGUAGE_PUNCTUATION, digits=False):
        raise InvalidFact(
            FactKind.PREFERRED_LANGUAGE, FactRejection.DISALLOWED_CHARACTERS
        )
    return language


# E.164 allows fifteen digits; the rest is punctuation people type.
MAX_PHONE_CHARS = 32
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
_PHONE_ALLOWED = frozenset(" +-().")

# Base58 (1..., 3...) and bech32 (bc1...). Deliberately a shape check and not a
# checksum: a checksum would reject a valid address on a chain this does not
# know, and the cost of storing a typo is that a lookup returns an empty
# wallet, which is visible. Nothing is signed with it, ever.
_BTC = re.compile(r"\A(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{11,71})\Z")


def _normalise_phone(value: str) -> str:
    """Digits and the punctuation people type around them, and nothing else.

    Not `_only`, which permits letters in every script -- right for a name and
    wrong here, because "call me on my mobile" would otherwise be a phone
    number.
    """
    phone = _collapse(value)
    if not phone:
        raise InvalidFact(FactKind.PHONE, FactRejection.EMPTY)
    if len(phone) > MAX_PHONE_CHARS:
        raise InvalidFact(FactKind.PHONE, FactRejection.TOO_LONG)
    if any(not c.isdigit() and c not in _PHONE_ALLOWED for c in phone):
        raise InvalidFact(FactKind.PHONE, FactRejection.DISALLOWED_CHARACTERS)
    digits = [c for c in phone if c.isdigit()]
    if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        raise InvalidFact(FactKind.PHONE, FactRejection.MALFORMED)
    return phone


# A street address, typed as the person types it. Free text is the one shape
# every country's address fits, so it is bounded and its characters are held
# to what addresses use: no markdown, no mention syntax, no brackets.
MAX_HOME_ADDRESS_CHARS = 200
_ADDRESS_PUNCTUATION = frozenset(",.-'’/()ºª°&")


def _normalise_home_address(value: str) -> str:
    address = _collapse(value).strip(" ,.;")
    if not address:
        raise InvalidFact(FactKind.HOME_ADDRESS, FactRejection.EMPTY)
    if len(address) > MAX_HOME_ADDRESS_CHARS:
        raise InvalidFact(FactKind.HOME_ADDRESS, FactRejection.TOO_LONG)
    if not _only(address, _ADDRESS_PUNCTUATION, digits=True):
        raise InvalidFact(FactKind.HOME_ADDRESS, FactRejection.DISALLOWED_CHARACTERS)
    return address


_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan", "janeiro"),
            ("february", "feb", "fevereiro", "fev"),
            ("march", "mar", "março", "marco"),
            ("april", "apr", "abril", "abr"),
            ("may", "maio", "mai"),
            ("june", "jun", "junho"),
            ("july", "jul", "julho"),
            ("august", "aug", "agosto", "ago"),
            ("september", "sep", "sept", "setembro", "set"),
            ("october", "oct", "outubro", "out"),
            ("november", "nov", "novembro"),
            ("december", "dec", "dezembro", "dez"),
        ),
        start=1,
    )
    for name in names
}
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# "21 de junho de 1981", "21 June 1981", "June 21 1981", "June 21st, 1981".
_DAY_MONTH_YEAR = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th|º)?\s+(?:de\s+|of\s+)?([a-zç]+)\.?,?\s+(?:de\s+)?(\d{4})"
)
_MONTH_DAY_YEAR = re.compile(r"([a-zç]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_EARLIEST_BIRTH_YEAR = 1900


def _date_parts(text: str) -> tuple[int, int, int] | None:
    """(year, month, day) candidates as written, before any calendar check."""
    if match := _ISO_DATE.fullmatch(text):
        return int(match[1]), int(match[2]), int(match[3])
    if match := _NUMERIC_DATE.fullmatch(text):
        return int(match[3]), int(match[2]), int(match[1])
    if (match := _DAY_MONTH_YEAR.fullmatch(text)) and match[2] in _MONTHS:
        return int(match[3]), _MONTHS[match[2]], int(match[1])
    if (match := _MONTH_DAY_YEAR.fullmatch(text)) and match[1] in _MONTHS:
        return int(match[3]), _MONTHS[match[1]], int(match[2])
    return None


def _normalise_birth_date(value: str) -> str:
    """An ISO date (`1981-06-21`) from the ways people write one.

    Day first for `21/06/1981`, as Brazil and most of the world write it; the
    month-first reading is tried only when day-first is impossible
    (`06/21/1981`). A date in the future, or before 1900, is refused: it is a
    typo, not a birthday.
    """
    text = _collapse(value).lower()
    if not text:
        raise InvalidFact(FactKind.BIRTH_DATE, FactRejection.EMPTY)
    parts = _date_parts(text)
    if parts is None:
        raise InvalidFact(FactKind.BIRTH_DATE, FactRejection.MALFORMED)
    year, month, day = parts
    born = _calendar_date(year, month, day) or _calendar_date(year, day, month)
    if born is None or born.year < _EARLIEST_BIRTH_YEAR or born > date.today():
        raise InvalidFact(FactKind.BIRTH_DATE, FactRejection.MALFORMED)
    return born.isoformat()


def _calendar_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# "o real brasileiro" and "reais" are words for a currency, not a sentence.
MAX_CURRENCY_CHARS = 32


def _normalise_currency(value: str) -> str:
    """The ISO 4217 code of a currency the rate source publishes.

    Stored as the code, so what later leaves for the rate lookup is a member
    of `domain.currency.SUPPORTED_CODES` and never the words somebody typed.
    A currency outside that set -- or anything that is not one -- is refused
    as malformed, and the reply lists the supported codes.
    """
    from chatmemory.domain.currency import currency_code

    text = _collapse(value)
    if not text:
        raise InvalidFact(FactKind.PREFERRED_CURRENCY, FactRejection.EMPTY)
    if len(text) > MAX_CURRENCY_CHARS:
        raise InvalidFact(FactKind.PREFERRED_CURRENCY, FactRejection.TOO_LONG)
    code = currency_code(text)
    if code is None:
        raise InvalidFact(FactKind.PREFERRED_CURRENCY, FactRejection.MALFORMED)
    return code


def _normalise_eth_wallet(value: str) -> str:
    """A 20-byte hex address, stored lowercase.

    The same vocabulary the wallet tool uses, from `domain.chain`, so the
    address somebody saves is exactly the shape the lookup will accept. Two
    definitions would let a person store an address the tool then refuses.
    """
    from chatmemory.domain.chain import is_address, normalise

    wallet = _collapse(value)
    if not wallet:
        raise InvalidFact(FactKind.ETH_WALLET, FactRejection.EMPTY)
    if not is_address(wallet):
        raise InvalidFact(FactKind.ETH_WALLET, FactRejection.MALFORMED)
    # Lowercase, for the reason `domain.chain` gives: EIP-55 mixes case as a
    # checksum, and lowercase is the form that cannot be a *wrong* checksum.
    return normalise(wallet)


def _normalise_btc_wallet(value: str) -> str:
    wallet = _collapse(value)
    if not wallet:
        raise InvalidFact(FactKind.BTC_WALLET, FactRejection.EMPTY)
    if not _BTC.fullmatch(wallet):
        raise InvalidFact(FactKind.BTC_WALLET, FactRejection.MALFORMED)
    # bech32 is defined lowercase; base58 is case-sensitive and left alone.
    return wallet.lower() if wallet.lower().startswith("bc1") else wallet


_NORMALISERS = {
    FactKind.PREFERRED_NAME: _normalise_name,
    FactKind.EMAIL: _normalise_email,
    FactKind.PREFERRED_LANGUAGE: _normalise_language,
    FactKind.PHONE: _normalise_phone,
    FactKind.ETH_WALLET: _normalise_eth_wallet,
    FactKind.BTC_WALLET: _normalise_btc_wallet,
    FactKind.FULL_NAME: _normalise_full_name,
    FactKind.HOME_ADDRESS: _normalise_home_address,
    FactKind.BIRTH_DATE: _normalise_birth_date,
    FactKind.PREFERRED_CURRENCY: _normalise_currency,
}


def normalise_fact(kind: FactKind, value: str) -> str:
    """The stored form of `value`, or `InvalidFact` saying why there is none."""
    return _NORMALISERS[FactKind(kind)](value)


@dataclass(frozen=True, slots=True)
class PersonalFact:
    """One validated fact. Constructing it is validating it.

    `value` is normalised in place, so `PersonalFact(kind, " Leo ").value` is
    "Leo". A value that is already normal is returned unchanged, which is what
    lets a store rebuild a fact from its row through the same check.
    """

    kind: FactKind
    value: str

    def __post_init__(self) -> None:
        kind = FactKind(self.kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", normalise_fact(kind, self.value))


@dataclass(frozen=True, slots=True)
class StoredFact:
    """A fact as read back, with when it was last set."""

    fact: PersonalFact
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PersonalFacts:
    """Everything one person has set: one per kind, or several of a wallet kind."""

    facts: tuple[StoredFact, ...] = ()

    def get(self, kind: FactKind) -> str | None:
        """The value of a single-valued kind; for a wallet kind, the oldest."""
        for stored in self.facts:
            if stored.fact.kind is kind:
                return stored.fact.value
        return None

    def values(self, kind: FactKind) -> tuple[str, ...]:
        """Every value of `kind`, oldest first."""
        return tuple(s.fact.value for s in self.facts if s.fact.kind is kind)

    @property
    def empty(self) -> bool:
        return not self.facts


class PersonFactEraser(Protocol):
    """The deletion half, which `/forget` everywhere needs and nothing more."""

    async def forget_all_facts(self, person: PersonRef) -> int:
        """Delete every fact this person set. Returns how many were deleted."""
        ...


class FactStore(PersonFactEraser, Protocol):
    async def set_fact(self, person: PersonRef, fact: PersonalFact) -> bool:
        """Store or replace this person's fact of `fact.kind`.

        A multi-valued kind (`MULTI_VALUED_KINDS`) is added to rather than
        replaced, and saving a value already held only refreshes it. The cap
        is the service's to apply before calling this.

        Returns False when nothing was stored -- the person has opted out, and
        the database dropped the row.
        """
        ...

    async def facts_of(self, viewer: Viewer) -> PersonalFacts:
        """The viewer's own facts. There is no way to name another person."""
        ...

    async def forget_fact(
        self, person: PersonRef, kind: FactKind, value: str | None = None
    ) -> bool:
        """Delete one kind, keeping the others. Returns whether one existed.

        With `value`, only that value of the kind: one wallet among several.
        `value` is the stored (normalised) form.
        """
        ...
