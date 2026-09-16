"""What a market data tool will accept, decided before anything is sent.

The egress guard checks membership against the text a call would send, and
that is the boundary. This is the per-argument check behind it, for the same
reason `adapters.web.query` exists behind the rooting gate: the guard sees
the arguments joined into one string, while a provider has to know that
`from` is a currency, `asset` is an instrument, and nothing else was passed.

A refusal here happens before a clearance is even consulted and long before a
socket is opened, which is what "rejected before any outbound request" means
in practice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

MAX_AMOUNT = Decimal(10) ** 15
"""Above this an "amount" is not money anybody is converting.

It never leaves the process -- conversion is applied locally -- so the bound
is about producing a sensible figure, not about egress."""


class ArgumentRefusal(StrEnum):
    """Why arguments were not accepted. Logged; never echoed to the model."""

    UNEXPECTED_ARGUMENT = "unexpected_argument"
    MISSING_ARGUMENT = "missing_argument"
    NOT_A_MEMBER = "not_a_member"
    INVALID_AMOUNT = "invalid_amount"
    SAME_CURRENCY = "same_currency"


@dataclass(frozen=True, slots=True)
class Lookup:
    """Validated arguments, in the only form a provider may use them.

    `terms` are canonical members of the provider's closed vocabulary and are
    the only text that reaches the wire. `amount` is kept apart on purpose:
    it is applied in process and never sent, so a number cannot become a
    channel the membership check does not see.
    """

    terms: tuple[str, ...]
    amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ArgumentCheck:
    """The verdict on one set of arguments."""

    lookup: Lookup | None = None
    refusal: ArgumentRefusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.lookup is not None


def refused(reason: ArgumentRefusal) -> ArgumentCheck:
    return ArgumentCheck(refusal=reason)


def member(value: object, vocabulary: frozenset[str]) -> str | None:
    """The canonical spelling of `value`, or None when it is not a member.

    Exact membership after folding case and trimming the ends. Nothing is
    extracted from a longer string: "USD please" is not USD, it is free text
    that happens to contain a code.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if candidate in vocabulary else None


def amount(value: object) -> Decimal | None:
    """A positive, finite JSON number, or None.

    A string is refused even when it looks numeric. The schema says number,
    and a string amount would also be a string argument the egress guard
    counts against the closed vocabulary -- where it is refused anyway.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    parsed = Decimal(str(value))
    return parsed if Decimal(0) < parsed <= MAX_AMOUNT else None


def only(arguments: Mapping[str, object], expected: frozenset[str]) -> ArgumentRefusal | None:
    """Refuse extra or missing keys before looking at any value.

    An extra key is not harmless noise here: it is a field the model chose to
    fill, and nothing downstream reads it -- which is exactly where text
    goes when it is meant to be carried rather than used.
    """
    keys = set(arguments)
    if keys - expected:
        return ArgumentRefusal.UNEXPECTED_ARGUMENT
    if expected - keys:
        return ArgumentRefusal.MISSING_ARGUMENT
    return None


def single_member(
    arguments: Mapping[str, object], key: str, vocabulary: frozenset[str]
) -> ArgumentCheck:
    """Arguments that are exactly one member of one vocabulary."""
    shape = only(arguments, frozenset({key}))
    if shape is not None:
        return refused(shape)
    canonical = member(arguments[key], vocabulary)
    if canonical is None:
        return refused(ArgumentRefusal.NOT_A_MEMBER)
    return ArgumentCheck(lookup=Lookup(terms=(canonical,)))
