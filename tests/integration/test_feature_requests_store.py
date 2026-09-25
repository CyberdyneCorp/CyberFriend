"""Feature requests against real Postgres: the bounds the store decides.

Resubmission is one row, the rolling daily limit is part of the insert, an
opted-out person stores nothing, and recording an opt-out (or deleting the
person) removes what they suggested -- by the triggers of migration 0030, so
no path that records an opt-out can keep a suggestion.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.feature_requests_postgres import PostgresFeatureRequestStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.feature_requests import FeatureRequestService, SubmitOutcome
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import RequestStatus, SourceKind, SuggestionSource

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
LEO = PersonRef(PLATFORM, 7)
ANA = PersonRef(PLATFORM, 9)
COMMAND = SuggestionSource(SourceKind.COMMAND, PLATFORM, guild_id=1, channel_id=100)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def service(engine: AsyncEngine, clock: Clock | None = None) -> FeatureRequestService:
    return FeatureRequestService(PostgresFeatureRequestStore(engine), clock=clock or Clock())


async def rows(engine: AsyncEngine) -> list[tuple[int, str, str, int | None, int | None]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT person_id, text, source_kind, guild_id, channel_id "
                "FROM feature_request ORDER BY id"
            )
        )
        return [tuple(r) for r in found]  # type: ignore[misc]


async def test_a_suggestion_is_stored_with_its_source_and_nothing_else(
    clean: AsyncEngine,
) -> None:
    result = await service(clean).submit(LEO, "  Dark   mode, please! ", COMMAND)

    assert result.outcome is SubmitOutcome.RECORDED and result.request_id is not None
    [(_, stored, kind, guild, channel)] = await rows(clean)
    assert (stored, kind, guild, channel) == ("Dark mode, please!", "command", 1, 100)


async def test_resubmitting_is_idempotent_and_gives_the_existing_number(
    clean: AsyncEngine,
) -> None:
    suggestions = service(clean)
    first = await suggestions.submit(LEO, "Dark mode, please!", COMMAND)
    again = await suggestions.submit(LEO, "dark mode please", COMMAND)

    assert again.outcome is SubmitOutcome.ALREADY_RECORDED
    assert again.request_id == first.request_id
    assert len(await rows(clean)) == 1


async def test_the_same_words_from_two_people_are_two_suggestions(clean: AsyncEngine) -> None:
    suggestions = service(clean)
    await suggestions.submit(LEO, "dark mode", COMMAND)
    await suggestions.submit(ANA, "dark mode", COMMAND)
    assert len(await rows(clean)) == 2


async def test_the_sixth_in_24_hours_is_refused_and_not_stored(clean: AsyncEngine) -> None:
    clock = Clock()
    suggestions = service(clean, clock)
    for n in range(5):
        clock.now = NOW + timedelta(hours=n)
        assert (await suggestions.submit(LEO, f"idea {n}", COMMAND)).stored

    refused = await suggestions.submit(LEO, "idea 5", COMMAND)

    assert refused.outcome is SubmitOutcome.LIMITED
    assert len(await rows(clean)) == 5
    # A resubmission is still answered with its number at the limit.
    assert (await suggestions.submit(LEO, "idea 0", COMMAND)).request_id is not None
    # And the window rolls: 24 hours after the first, one more is taken.
    clock.now = NOW + timedelta(hours=24, minutes=1)
    assert (await suggestions.submit(LEO, "idea 5", COMMAND)).stored


async def test_parallel_submissions_cannot_pass_the_daily_limit(clean: AsyncEngine) -> None:
    """Regression: the count in the insert saw only committed rows, so twenty
    submissions at once were all stored."""
    suggestions = service(clean)
    assert (await suggestions.submit(LEO, "warm up", COMMAND)).stored

    results = await asyncio.gather(
        *(suggestions.submit(LEO, f"idea {n}", COMMAND) for n in range(20))
    )

    outcomes = [r.outcome for r in results]
    assert outcomes.count(SubmitOutcome.RECORDED) == 4
    assert outcomes.count(SubmitOutcome.LIMITED) == 16
    assert len(await rows(clean)) == 5


async def test_an_opted_out_person_stores_nothing(clean: AsyncEngine) -> None:
    await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "test")

    result = await service(clean).submit(LEO, "dark mode", COMMAND)

    assert result.outcome is SubmitOutcome.OPTED_OUT
    assert await rows(clean) == []


async def test_an_opted_out_person_keeps_the_placeholder_name(clean: AsyncEngine) -> None:
    """Regression: the Discord name was written before the opt-out was seen."""
    await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "test")

    result = await service(clean).submit(LEO, "dark mode", COMMAND, display_name="Real Name")

    assert result.outcome is SubmitOutcome.OPTED_OUT
    async with clean.connect() as conn:
        name = await conn.scalar(text("SELECT display_name FROM person"))
    assert name == str(LEO.platform_user_id)


async def test_opting_out_deletes_their_suggestions_and_only_theirs(clean: AsyncEngine) -> None:
    suggestions = service(clean)
    await suggestions.submit(LEO, "dark mode", COMMAND)
    await suggestions.submit(ANA, "light mode", COMMAND)

    await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "test")

    assert [r[1] for r in await rows(clean)] == ["light mode"]
    assert await suggestions.list_own(LEO) == []


async def test_deleting_the_person_deletes_their_suggestions(clean: AsyncEngine) -> None:
    """The person-row cascade. Erasure through `purge_person_derived` is not
    on this base yet; see tasks 1.1/1.5 of add-feature-requests."""
    suggestions = service(clean)
    await suggestions.submit(LEO, "dark mode", COMMAND)
    await suggestions.submit(ANA, "light mode", COMMAND)

    async with clean.begin() as conn:
        person_id = await conn.scalar(
            text(
                "DELETE FROM person_platform_id WHERE platform = :p AND platform_user_id = :u "
                "RETURNING person_id"
            ),
            {"p": PLATFORM, "u": LEO.platform_user_id},
        )
        await conn.execute(text("DELETE FROM person WHERE id = :i"), {"i": person_id})

    assert [r[1] for r in await rows(clean)] == ["light mode"]


async def test_the_listing_is_only_their_own_newest_first(clean: AsyncEngine) -> None:
    clock = Clock()
    suggestions = service(clean, clock)
    await suggestions.submit(LEO, "first", COMMAND)
    clock.now = NOW + timedelta(minutes=1)
    await suggestions.submit(LEO, "second", COMMAND)
    await suggestions.submit(ANA, "hers", COMMAND)

    own = await suggestions.list_own(LEO)

    assert [r.text for r in own] == ["second", "first"]
    assert {r.status for r in own} == {RequestStatus.NEW}
    assert await suggestions.list_own(PersonRef(PLATFORM, 404)) == []


async def test_notify_is_set_only_on_their_own_and_starts_from_the_current_status(
    clean: AsyncEngine,
) -> None:
    suggestions = service(clean)
    mine = await suggestions.submit(LEO, "dark mode", COMMAND)
    assert mine.request_id is not None
    # Ana is a known person, so the refusal below is the statement's person
    # predicate and not the lookup finding nobody.
    assert (await suggestions.submit(ANA, "light mode", COMMAND)).stored

    assert not await suggestions.set_notify(ANA, mine.request_id, True)
    async with clean.connect() as conn:
        untouched = await conn.scalar(
            text("SELECT notify_on_change FROM feature_request WHERE id = :i"),
            {"i": mine.request_id},
        )
    assert untouched is False
    assert await suggestions.set_notify(LEO, mine.request_id, True)

    async with clean.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT notify_on_change, notified_status FROM feature_request "
                "WHERE id = :i"
            ),
            {"i": mine.request_id},
        )
        assert tuple(found.one()) == (True, "new")


async def test_a_placeholder_name_is_replaced_by_the_discord_name(clean: AsyncEngine) -> None:
    await service(clean).submit(LEO, "dark mode", COMMAND, display_name="Leo")

    async with clean.connect() as conn:
        name = await conn.scalar(text("SELECT display_name FROM person"))
    assert name == "Leo"
