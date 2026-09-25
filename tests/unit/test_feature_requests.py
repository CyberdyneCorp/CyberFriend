"""Feature requests: the text rules, and what `/suggest` and `/suggestions` say.

The store's bounds (resubmission, the daily limit, opt-out) are decided in SQL
and pinned in `tests/integration/test_feature_requests_store.py`. Here: what
is refused before the store is asked, how a resubmission is recognised, and
the replies in both languages.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from chatmemory.adapters.discord.suggestions import (
    submitted_message,
    suggestion_listing,
)
from chatmemory.app.feature_requests import (
    ContactKind,
    FeatureRequestService,
    SubmitOutcome,
    SubmitResult,
    contact_in,
    language_code,
    normalized_hash,
)
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import (
    MAX_TEXT_CHARS,
    FeatureRequest,
    NewSuggestion,
    RequestStatus,
    SourceKind,
    StoreResult,
    StoreVerdict,
    SuggestionSource,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
LEO = PersonRef("discord", 7)
DM = SuggestionSource(SourceKind.COMMAND, "discord")


class FakeStore:
    def __init__(self, verdict: StoreVerdict = StoreVerdict.STORED) -> None:
        self.verdict = verdict
        self.submitted: list[tuple[NewSuggestion, datetime, int]] = []

    async def submit(
        self, suggestion: NewSuggestion, *, now: datetime, daily_limit: int
    ) -> StoreResult:
        self.submitted.append((suggestion, now, daily_limit))
        return StoreResult(self.verdict, 12 if self.verdict is not StoreVerdict.LIMITED else None)

    async def for_person(self, person: PersonRef, limit: int) -> Sequence[FeatureRequest]:
        return []

    async def set_notify(self, person: PersonRef, request_id: int, notify: bool) -> bool:
        return True


def service(store: FakeStore) -> FeatureRequestService:
    return FeatureRequestService(store, clock=lambda: NOW)


# --- contact details -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("email me at leo@example.com when it ships", ContactKind.EMAIL),
        ("me avisa no leo.silva+bot@empresa.com.br", ContactKind.EMAIL),
        ("call me on +55 11 91234-5678 about it", ContactKind.PHONE),
        ("my number (11) 91234-5678 for updates", ContactKind.PHONE),
        ("555-123-4567 is my cell", ContactKind.PHONE),
        ("track 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5 for me", ContactKind.WALLET),
        ("watch bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq please", ContactKind.WALLET),
        ("watch 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa please", ContactKind.WALLET),
        # Too few digits to be a phone on its own, but stated as one.
        ("my phone is 91234 5678", ContactKind.CONTACT),
    ],
)
def test_contact_details_are_found(text: str, kind: ContactKind) -> None:
    assert contact_in(text) is kind


@pytest.mark.parametrize(
    "text",
    [
        "notify me when BTC goes above 100000",
        "show the date like 2026-09-25 in answers",
        "support v4 positions on Base",
        "avisar quando alguém me marcar",
        "a /remind command, like @everyone uses",
    ],
)
def test_ordinary_suggestions_carry_no_contact(text: str) -> None:
    assert contact_in(text) is None


async def test_a_suggestion_with_an_email_is_refused_before_the_store() -> None:
    store = FakeStore()
    result = await service(store).submit(LEO, "email me at leo@example.com", DM)

    assert result == SubmitResult(SubmitOutcome.CONTAINS_CONTACT, contact=ContactKind.EMAIL)
    assert store.submitted == []


@pytest.mark.parametrize(
    ("kind", "english", "portuguese"),
    [
        (ContactKind.EMAIL, "an email address", "um endereço de e-mail"),
        (ContactKind.PHONE, "a phone number", "um número de telefone"),
        (ContactKind.WALLET, "a wallet address", "um endereço de carteira"),
    ],
)
def test_the_refusal_says_why(kind: ContactKind, english: str, portuguese: str) -> None:
    result = SubmitResult(SubmitOutcome.CONTAINS_CONTACT, contact=kind)
    assert english in submitted_message(result, Language.ENGLISH)
    assert "without it" in submitted_message(result, Language.ENGLISH)
    assert portuguese in submitted_message(result, Language.PORTUGUESE)


# --- the other refusals ----------------------------------------------------------


async def test_empty_and_too_long_never_reach_the_store() -> None:
    store = FakeStore()
    suggestions = service(store)

    assert (await suggestions.submit(LEO, "   ", DM)).outcome is SubmitOutcome.EMPTY
    too_long = "x" * (MAX_TEXT_CHARS + 1)
    assert (await suggestions.submit(LEO, too_long, DM)).outcome is SubmitOutcome.TOO_LONG
    assert (await suggestions.submit(LEO, "x" * MAX_TEXT_CHARS, DM)).stored
    assert len(store.submitted) == 1


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (StoreVerdict.STORED, SubmitOutcome.RECORDED),
        (StoreVerdict.DUPLICATE, SubmitOutcome.ALREADY_RECORDED),
        (StoreVerdict.LIMITED, SubmitOutcome.LIMITED),
        (StoreVerdict.OPTED_OUT, SubmitOutcome.OPTED_OUT),
    ],
)
async def test_the_store_verdict_is_the_outcome(
    verdict: StoreVerdict, outcome: SubmitOutcome
) -> None:
    result = await service(FakeStore(verdict)).submit(LEO, "dark mode", DM)
    assert result.outcome is outcome


async def test_the_store_is_handed_the_clock_the_limit_and_clean_text() -> None:
    store = FakeStore()
    await service(store).submit(
        LEO, "  dark\n mode  ", DM, display_name="Leo", language=Language.PORTUGUESE
    )

    [(suggestion, now, limit)] = store.submitted
    assert (suggestion.text, suggestion.display_name, now, limit) == ("dark mode", "Leo", NOW, 5)
    assert suggestion.normalized_hash == normalized_hash("Dark mode!")


# --- recognising a resubmission ------------------------------------------------------


def test_resubmissions_hash_alike() -> None:
    assert normalized_hash("Dark mode, please!") == normalized_hash("dark   mode please")
    assert normalized_hash("dark mode") != normalized_hash("light mode")


def test_the_language_is_the_texts_own_else_the_callers() -> None:
    assert language_code("seria ótimo ter um modo escuro nas respostas", Language.ENGLISH) == "pt"
    assert language_code("dark mode", Language.PORTUGUESE) == "pt"
    assert language_code("dark mode", Language.UNKNOWN) is None


# --- the replies -------------------------------------------------------------------


def test_the_acknowledgement_gives_the_number_and_says_who_sees_it() -> None:
    recorded = SubmitResult(SubmitOutcome.RECORDED, request_id=12)

    english = submitted_message(recorded, Language.ENGLISH)
    portuguese = submitted_message(recorded, Language.PORTUGUESE)

    assert "#12" in english and "The team will see your text and your Discord name" in english
    assert "status changes" in english
    assert "#12" in portuguese and "A equipe vai ver o seu texto" in portuguese


def test_a_resubmission_gives_the_existing_number() -> None:
    again = SubmitResult(SubmitOutcome.ALREADY_RECORDED, request_id=12)
    assert "#12" in submitted_message(again, Language.ENGLISH)
    assert "já sugeriu" in submitted_message(again, Language.PORTUGUESE)


def test_the_limit_reply_names_the_limit() -> None:
    limited = SubmitResult(SubmitOutcome.LIMITED)
    assert "5 suggestions in the last 24 hours" in submitted_message(limited, Language.ENGLISH)
    assert "Nothing was recorded" in submitted_message(limited, Language.ENGLISH)


def test_the_listing_shows_numbers_and_statuses_in_the_callers_language() -> None:
    requests = [
        FeatureRequest(3, "dark mode", RequestStatus.PLANNED, NOW),
        FeatureRequest(1, "y" * 300, RequestStatus.NEW, NOW),
    ]

    english = suggestion_listing(requests, Language.ENGLISH)
    portuguese = suggestion_listing(requests, Language.PORTUGUESE)

    assert "**#3** - planned - dark mode" in english
    assert "**#3** - planejada - dark mode" in portuguese
    assert "y" * 300 not in english, "a long suggestion is clipped"
    assert "`/suggest`" in suggestion_listing([], Language.ENGLISH)
