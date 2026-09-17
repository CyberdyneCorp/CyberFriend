"""The personal-facts port: what a person has told the assistant about themselves.

Three properties are fixed by the shape of this interface rather than left to
an implementation to remember:

*   **The set of facts is closed.** `FactKind` has three members and the table
    has a CHECK constraint naming the same three. A free-form "remember this"
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
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef, Viewer


class FactKind(StrEnum):
    """The facts a person may set. The values are the stored `kind` column."""

    PREFERRED_NAME = "preferred_name"
    EMAIL = "email"
    PREFERRED_LANGUAGE = "preferred_language"


class FactRejection(StrEnum):
    """Why a value was not accepted. Stable codes, so a reply can say why."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    MALFORMED = "malformed"
    DISALLOWED_CHARACTERS = "disallowed_characters"


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


def _normalise_name(value: str) -> str:
    name = _collapse(value)
    if not name:
        raise InvalidFact(FactKind.PREFERRED_NAME, FactRejection.EMPTY)
    if len(name) > MAX_PREFERRED_NAME_CHARS:
        raise InvalidFact(FactKind.PREFERRED_NAME, FactRejection.TOO_LONG)
    if not _only(name, _NAME_PUNCTUATION, digits=True):
        raise InvalidFact(FactKind.PREFERRED_NAME, FactRejection.DISALLOWED_CHARACTERS)
    return name


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


_NORMALISERS = {
    FactKind.PREFERRED_NAME: _normalise_name,
    FactKind.EMAIL: _normalise_email,
    FactKind.PREFERRED_LANGUAGE: _normalise_language,
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
    """Everything one person has set, at most one per kind."""

    facts: tuple[StoredFact, ...] = ()

    def get(self, kind: FactKind) -> str | None:
        for stored in self.facts:
            if stored.fact.kind is kind:
                return stored.fact.value
        return None

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

        Returns False when nothing was stored -- the person has opted out, and
        the database dropped the row.
        """
        ...

    async def facts_of(self, viewer: Viewer) -> PersonalFacts:
        """The viewer's own facts. There is no way to name another person."""
        ...

    async def forget_fact(self, person: PersonRef, kind: FactKind) -> bool:
        """Delete one kind, keeping the others. Returns whether one existed."""
        ...
