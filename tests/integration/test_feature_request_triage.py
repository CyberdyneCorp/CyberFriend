"""Triage and status news against real Postgres.

The console's list and its audited change, and the bot's sweep: a message is
due only for an author who said Yes, once per status change, and never for
somebody who opted out, erased their data, turned notifications off or
closed their direct messages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.feature_requests_postgres import (
    PostgresFeatureRequestStore,
    PostgresFeatureRequestTriage,
    PostgresStatusNewsStore,
)
from chatmemory.adapters.store.notify_postgres import PostgresNotificationQueue
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.feature_request_news import StatusNewsRunner
from chatmemory.app.feature_requests import FeatureRequestService
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import (
    RequestStatus,
    SourceKind,
    SuggestionSource,
    TriageChange,
    TriageRefusal,
)
from chatmemory.ports.notifications import DeliveryResult

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
LEO = PersonRef(PLATFORM, 7)
ANA = PersonRef(PLATFORM, 9)
COMMAND = SuggestionSource(SourceKind.COMMAND, PLATFORM, guild_id=1, channel_id=100)


class Messenger:
    def __init__(self, result: DeliveryResult = DeliveryResult.SENT) -> None:
        self.result = result
        self.sent: list[tuple[PersonRef, int, str]] = []

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        if self.result is DeliveryResult.SENT:
            self.sent.append((person, task_id, text))
        return self.result


async def suggest(
    engine: AsyncEngine, person: PersonRef, words: str, *, notify: bool, name: str = ""
) -> int:
    service = FeatureRequestService(PostgresFeatureRequestStore(engine), clock=lambda: NOW)
    result = await service.submit(person, words, COMMAND, display_name=name)
    assert result.request_id is not None
    if notify:
        assert await service.set_notify(person, result.request_id, True)
    return result.request_id


async def set_status(engine: AsyncEngine, request_id: int, status: RequestStatus) -> None:
    result = await PostgresFeatureRequestTriage(engine).triage(
        request_id, TriageChange(status=status), actor="ana", now=NOW
    )
    assert result.after is not None


def sweep(engine: AsyncEngine, messenger: Messenger) -> StatusNewsRunner:
    return StatusNewsRunner(PostgresStatusNewsStore(engine), messenger, clock=lambda: NOW)


async def person_id(engine: AsyncEngine, person: PersonRef) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT person_id FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"p": person.platform, "u": person.platform_user_id},
        )
    return int(found)


# --- the console's list and change ----------------------------------------------


async def test_the_list_is_newest_first_with_names_and_same_text_counts(
    clean: AsyncEngine,
) -> None:
    first = await suggest(clean, LEO, "Dark mode, please", notify=False, name="Leo")
    second = await suggest(clean, ANA, "dark mode please", notify=False, name="Ana")
    await set_status(clean, first, RequestStatus.PLANNED)

    page = await PostgresFeatureRequestTriage(clean).triage_page(None, offset=0, limit=10)

    assert page.total == 2
    assert [e.id for e in page.entries] == [second, first]
    assert [e.person_name for e in page.entries] == ["Ana", "Leo"]
    assert [e.same_text_elsewhere for e in page.entries] == [1, 1]
    planned = await PostgresFeatureRequestTriage(clean).triage_page(
        RequestStatus.PLANNED, offset=0, limit=10
    )
    assert (planned.total, [e.id for e in planned.entries]) == (1, [first])


async def test_a_change_returns_the_row_before_and_after(clean: AsyncEngine) -> None:
    original = await suggest(clean, LEO, "dark mode", notify=False)
    copy = await suggest(clean, ANA, "night mode", notify=False)

    result = await PostgresFeatureRequestTriage(clean).triage(
        copy,
        TriageChange(
            status=RequestStatus.DUPLICATE,
            set_note=True,
            admin_note="same as dark mode",
            set_duplicate=True,
            duplicate_of=original,
        ),
        actor="oidc:ana-sub",
        now=NOW + timedelta(hours=1),
    )

    assert result.before is not None and result.after is not None
    assert result.before.status is RequestStatus.NEW
    assert result.after.status is RequestStatus.DUPLICATE
    assert result.after.duplicate_of == original
    assert result.after.admin_note == "same as dark mode"
    assert result.after.updated_by == "oidc:ana-sub"
    assert result.after.updated_at == NOW + timedelta(hours=1)


async def test_an_unknown_row_or_duplicate_target_changes_nothing(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=False)
    triage = PostgresFeatureRequestTriage(clean)

    missing = await triage.triage(
        mine + 100, TriageChange(status=RequestStatus.DONE), actor="ana", now=NOW
    )
    unknown = await triage.triage(
        mine,
        TriageChange(status=RequestStatus.DUPLICATE, set_duplicate=True, duplicate_of=mine + 100),
        actor="ana",
        now=NOW,
    )

    assert missing.refusal is TriageRefusal.NOT_FOUND
    assert unknown.refusal is TriageRefusal.UNKNOWN_DUPLICATE
    [entry] = (await triage.triage_page(None, offset=0, limit=10)).entries
    assert entry.status is RequestStatus.NEW


# --- status news ------------------------------------------------------------------


async def test_one_message_per_status_change_to_an_author_who_asked(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    messenger = Messenger()

    assert await sweep(clean, messenger).run_due() == 0  # nothing has changed yet
    await set_status(clean, mine, RequestStatus.PLANNED)
    assert await sweep(clean, messenger).run_due() == 1
    assert await sweep(clean, messenger).run_due() == 0
    await set_status(clean, mine, RequestStatus.DONE)
    assert await sweep(clean, messenger).run_due() == 1

    assert [(p, i) for p, i, _ in messenger.sent] == [(LEO, mine), (LEO, mine)]
    assert "**planned**" in messenger.sent[0][2]
    assert "**done**" in messenger.sent[1][2]


async def test_no_message_without_the_yes(clean: AsyncEngine) -> None:
    theirs = await suggest(clean, ANA, "light mode", notify=False)
    await set_status(clean, theirs, RequestStatus.PLANNED)
    messenger = Messenger()

    assert await sweep(clean, messenger).run_due() == 0
    assert messenger.sent == []


async def test_a_change_undone_before_the_sweep_sends_nothing(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)
    await set_status(clean, mine, RequestStatus.NEW)
    messenger = Messenger()

    assert await sweep(clean, messenger).run_due() == 0


async def test_undeliverable_people_are_skipped_until_they_reopen(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)
    closed = Messenger(DeliveryResult.CLOSED)

    assert await sweep(clean, closed).run_due() == 0
    # Recorded as undeliverable, and the claim put back: the next sweep does
    # not even try.
    async with clean.connect() as conn:
        marked = await conn.scalar(
            text("SELECT undeliverable_at FROM notification_preference WHERE person_id = :i"),
            {"i": await person_id(clean, LEO)},
        )
    assert marked == NOW
    opened = Messenger()
    assert await sweep(clean, opened).run_due() == 0
    assert opened.sent == []

    # `/notifications on` clears the mark; the change is then told once.
    await PostgresNotificationQueue(clean).set_enabled(LEO, True)
    assert await sweep(clean, opened).run_due() == 1


async def test_a_transient_failure_is_retried(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)

    assert await sweep(clean, Messenger(DeliveryResult.FAILED)).run_due() == 0
    assert await sweep(clean, Messenger()).run_due() == 1


async def test_notifications_off_means_no_message(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await PostgresNotificationQueue(clean).set_enabled(LEO, False)
    await set_status(clean, mine, RequestStatus.PLANNED)

    assert await sweep(clean, Messenger()).run_due() == 0


async def test_nothing_is_sent_after_an_opt_out(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)

    await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "test")

    assert await sweep(clean, Messenger()).run_due() == 0


async def test_nothing_is_sent_after_erasure(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)

    async with clean.begin() as conn:
        await conn.execute(
            text("SELECT purge_person_derived(:i)"), {"i": await person_id(clean, LEO)}
        )

    assert await sweep(clean, Messenger()).run_due() == 0


async def test_no_message_after_yes_then_no(clean: AsyncEngine) -> None:
    # Answering No after Yes leaves `notified_status` set; only the opt-in
    # guard keeps the change from being announced.
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    service = FeatureRequestService(PostgresFeatureRequestStore(clean), clock=lambda: NOW)
    assert await service.set_notify(LEO, mine, False)
    await set_status(clean, mine, RequestStatus.PLANNED)
    messenger = Messenger()

    assert await sweep(clean, messenger).run_due() == 0
    assert messenger.sent == []


async def test_an_opted_out_persons_leftover_rows_neither_send_nor_block(
    clean: AsyncEngine,
) -> None:
    # The purge normally deletes the rows. If it ever missed them, the claim
    # must skip them itself: the row guard would drop the UPDATE, but rows
    # picked and then dropped would fill every batch and starve everyone else.
    theirs = await suggest(clean, LEO, "dark mode", notify=True)
    mine = await suggest(clean, ANA, "light mode", notify=True)
    await set_status(clean, theirs, RequestStatus.PLANNED)
    await set_status(clean, mine, RequestStatus.PLANNED)
    async with clean.begin() as conn:
        await conn.execute(text("ALTER TABLE person_opt_out DISABLE TRIGGER USER"))
        await conn.execute(
            text("INSERT INTO person_opt_out (person_id) VALUES (:i)"),
            {"i": await person_id(clean, LEO)},
        )
        await conn.execute(text("ALTER TABLE person_opt_out ENABLE TRIGGER USER"))
    messenger = Messenger()

    runner = StatusNewsRunner(
        PostgresStatusNewsStore(clean), messenger, clock=lambda: NOW, batch=1
    )
    assert await runner.run_due() == 1
    assert [(p, i) for p, i, _ in messenger.sent] == [(ANA, mine)]


async def test_the_message_goes_to_the_account_the_suggestion_came_from(
    clean: AsyncEngine,
) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    # The same person on another platform, with a lower account id.
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES ('another', 1, :i)"
            ),
            {"i": await person_id(clean, LEO)},
        )
    await set_status(clean, mine, RequestStatus.PLANNED)
    messenger = Messenger()

    assert await sweep(clean, messenger).run_due() == 1
    assert [p for p, _, _ in messenger.sent] == [LEO]


async def test_a_late_put_back_does_not_undo_a_newer_claim(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "dark mode", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)
    store = PostgresStatusNewsStore(clean)
    [stale] = await store.claim_status_news(10)
    # While the first message is in flight the status moves on and another
    # sweep claims and sends the newer change.
    await set_status(clean, mine, RequestStatus.DONE)
    [newer] = await store.claim_status_news(10)
    assert newer.status is RequestStatus.DONE

    await store.release(stale)

    assert await store.claim_status_news(10) == []


async def test_a_raised_delivery_loses_no_claim(clean: AsyncEngine) -> None:
    # Regression: an exception from deliver() abandoned the batch with every
    # claim advanced, so those authors were never told.
    first = await suggest(clean, LEO, "dark mode", notify=True)
    second = await suggest(clean, ANA, "light mode", notify=True)
    await set_status(clean, first, RequestStatus.PLANNED)
    await set_status(clean, second, RequestStatus.PLANNED)

    class Raising(Messenger):
        async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
            raise OSError("connection reset")

    assert await sweep(clean, Raising()).run_due() == 0
    working = Messenger()
    assert await sweep(clean, working).run_due() == 2
    assert sorted(i for _, i, _ in working.sent) == sorted([first, second])


async def test_the_message_is_in_the_suggestions_language(clean: AsyncEngine) -> None:
    mine = await suggest(clean, LEO, "tenho uma ideia: modo escuro para o painel", notify=True)
    await set_status(clean, mine, RequestStatus.PLANNED)
    messenger = Messenger()

    await sweep(clean, messenger).run_due()

    [(_, _, message)] = messenger.sent
    assert message.startswith(f"Sua sugestão **#{mine}** agora está: **planejada**.")
