"""Feature requests: what a person may suggest, and what is kept of it.

`FeatureRequestService` is the policy a person meets at `/suggest` and
`/suggestions`:

*   **Only their own words.** The text is kept as given, up to 1000
    characters, with the language and the ids of where it was given. Nothing
    about the conversation around it is stored.
*   **No contact details.** A suggestion carrying an email address, a phone
    number or a wallet address is refused with the reason, before anything
    reaches the store: the team reads these, and "email me at ..." would hand
    them somebody's address outside the facts store that promises to keep it
    private.
*   **Idempotent and bounded.** The same suggestion twice is one row, answered
    with its number; at most five are accepted per person in any 24 hours. Both
    are decided by the store, inside the write.
*   **Privacy choices hold.** Nothing is stored for somebody who opted out, and
    an opt-out or erasure deletes what they suggested (0030's insert guard and
    its delete in `purge_person_derived`).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language, detect
from chatmemory.app.routing import states_own_contact
from chatmemory.domain.chain import find_addresses
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import (
    DEFAULT_DAILY_LIMIT,
    MAX_TEXT_CHARS,
    FeatureRequest,
    FeatureRequestStore,
    NewSuggestion,
    StoreVerdict,
    SuggestionSource,
)

LISTING_LIMIT = 20
"""How many of their own suggestions `/suggestions` shows, newest first."""


class ContactKind(StrEnum):
    """What kind of contact detail made a suggestion unstorable."""

    EMAIL = "email"
    PHONE = "phone"
    WALLET = "wallet"
    CONTACT = "contact"
    """Stated as the person's own contact ("my address is ..."), of any kind."""


class SubmitOutcome(StrEnum):
    """What happened to a suggestion. Each maps to a sentence the person sees."""

    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    CONTAINS_CONTACT = "contains_contact"
    LIMITED = "limited"
    OPTED_OUT = "opted_out"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    outcome: SubmitOutcome
    request_id: int | None = None
    contact: ContactKind | None = None

    @property
    def stored(self) -> bool:
        return self.outcome is SubmitOutcome.RECORDED


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_BRACKETED_AT = r"[\[({<]\s*(?:at|arroba)\s*[\])}>]"
_BRACKETED_DOT = r"[\[({<]\s*(?:dot|ponto)\s*[\])}>]"
_OBFUSCATED_EMAIL = re.compile(
    # "leo[at]gmail.com", "leo (at) gmail [dot] com"
    rf"[\w.+-]+\s*{_BRACKETED_AT}\s*[\w-]+(?:\s*(?:{_BRACKETED_DOT}|\.)\s*[\w-]+)+"
    # "leo at gmail dot com", "leo arroba gmail ponto com": the spelled-out dot
    # is required, so "look at coinbase.com" is not an address.
    r"|[\w.+-]+\s+(?:at|arroba)\s+[\w-]+(?:\s+(?:dot|ponto)\s+[\w-]+)+",
    re.IGNORECASE,
)
_BTC = re.compile(r"\b(?:bc1[a-z0-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
_DATE_TIME = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b"
)
"""Dates and times, taken out before phones are looked for: "2026-09-25 10:00"
is twelve digits in a row of digits and separators, and not a number to call."""
_PHONE = re.compile(r"(?<![\w+])(\+?)\(?\d[\d\s().-]{5,}\d(?!\w)")
_PHONE_WORD = re.compile(
    r"\b(?:tel|phone|telefone|fone|cel|celular|whats(?:app)?|zap|wpp|sms|call|ligar|liga)\b",
    re.IGNORECASE,
)
_PHONE_DIGITS_WITH_PLUS = 8
"""A number written "+55 11 ..." is a phone from eight digits on."""
_PHONE_DIGITS_NAMED = 8
"""So is one next to a word like "tel" or "whatsapp": "zap 98765-4321"."""
_PHONE_DIGITS_BARE = 10
"""Without either, ten: a Brazilian mobile without its area code has nine,
and so do too many ordinary numbers to refuse them all."""
_PHONE_DIGITS_MAX = 15
_HANDLE = re.compile(r"(?<![\w<@])@[A-Za-z0-9_.]{3,}|\b[\w.]{2,32}#\d{4}\b")
_HANDLE_PLACE = re.compile(
    r"\b(?:telegram|tg|twitter|x|instagram|insta|ig|signal|discord|whats(?:app)?|zap|"
    r"contact|contato|dm|message|mensagem|reach|chama|fala)\b",
    re.IGNORECASE,
)
"""A "@name" is only somebody's contact where they say where to find it:
"like @everyone uses" is not."""


def _has_phone(text: str) -> bool:
    undated = _DATE_TIME.sub(" ", text)
    named = _PHONE_WORD.search(undated) is not None
    for match in _PHONE.finditer(undated):
        digits = sum(c.isdigit() for c in match.group(0))
        if _phone_minimum(plus=bool(match.group(1)), named=named) <= digits <= _PHONE_DIGITS_MAX:
            return True
    return False


def _phone_minimum(*, plus: bool, named: bool) -> int:
    if plus:
        return _PHONE_DIGITS_WITH_PLUS
    return _PHONE_DIGITS_NAMED if named else _PHONE_DIGITS_BARE


def _has_handle(text: str) -> bool:
    return _HANDLE.search(text) is not None and _HANDLE_PLACE.search(text) is not None


def contact_in(text: str) -> ContactKind | None:
    """The first kind of contact detail `text` carries, or None.

    NFKC first, so a fullwidth "＠" or digit is read as the ASCII one.
    """
    folded = unicodedata.normalize("NFKC", text)
    if find_addresses(folded) or _BTC.search(folded):
        return ContactKind.WALLET
    if _EMAIL.search(folded) or _OBFUSCATED_EMAIL.search(folded):
        return ContactKind.EMAIL
    if _has_phone(folded):
        return ContactKind.PHONE
    if _has_handle(folded) or states_own_contact(folded):
        return ContactKind.CONTACT
    return None


def normalized_hash(text: str) -> bytes:
    """sha256 of the text lowercased, punctuation stripped, whitespace collapsed.

    So "Dark mode, please!" and "dark mode please" are one suggestion.
    """
    folded = unicodedata.normalize("NFC", text).lower()
    unpunctuated = "".join(c for c in folded if not unicodedata.category(c).startswith("P"))
    return hashlib.sha256(" ".join(unpunctuated.split()).encode()).digest()


def language_code(text: str, fallback: Language) -> str | None:
    """"pt" or "en": the text's own language, else the caller's."""
    detected = detect(text)
    language = detected if detected.known else fallback
    return {Language.PORTUGUESE: "pt", Language.ENGLISH: "en"}.get(language)


_VERDICTS = {
    StoreVerdict.STORED: SubmitOutcome.RECORDED,
    StoreVerdict.DUPLICATE: SubmitOutcome.ALREADY_RECORDED,
    StoreVerdict.LIMITED: SubmitOutcome.LIMITED,
    StoreVerdict.OPTED_OUT: SubmitOutcome.OPTED_OUT,
}


class FeatureRequestService:
    """Submitting, listing and opting into news of a person's own suggestions."""

    def __init__(
        self,
        store: FeatureRequestStore,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._daily_limit = daily_limit
        self._clock = clock

    async def submit(
        self,
        person: PersonRef,
        text: str,
        source: SuggestionSource,
        *,
        display_name: str = "",
        language: Language = Language.UNKNOWN,
    ) -> SubmitResult:
        cleaned = " ".join(text.split())
        refusal = _refusal(cleaned)
        if refusal is not None:
            return refusal
        suggestion = NewSuggestion(
            person=person,
            text=cleaned,
            normalized_hash=normalized_hash(cleaned),
            language=language_code(cleaned, language),
            source=source,
            display_name=display_name,
        )
        result = await self._store.submit(
            suggestion, now=self._clock(), daily_limit=self._daily_limit
        )
        return SubmitResult(_VERDICTS[result.verdict], request_id=result.request_id)

    async def list_own(self, person: PersonRef) -> Sequence[FeatureRequest]:
        return await self._store.for_person(person, LISTING_LIMIT)

    async def set_notify(self, person: PersonRef, request_id: int, notify: bool) -> bool:
        return await self._store.set_notify(person, request_id, notify)


def _refusal(text: str) -> SubmitResult | None:
    """Why `text` cannot be stored, decided before the store is asked."""
    if not text:
        return SubmitResult(SubmitOutcome.EMPTY)
    if len(text) > MAX_TEXT_CHARS:
        return SubmitResult(SubmitOutcome.TOO_LONG)
    contact = contact_in(text)
    if contact is not None:
        return SubmitResult(SubmitOutcome.CONTAINS_CONTACT, contact=contact)
    return None
