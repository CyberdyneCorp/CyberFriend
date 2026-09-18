"""Postgres implementation of the notification queue.

Mirrors `asks_postgres.py`: the statements live next door in `notify_sql.py`
and this binds them. The two bindings that matter are the ones a caller never
supplies -- the recipient's person id, resolved from their platform account,
and their readable channel set, taken from the `Viewer` the delivery service
resolved from live guild state a moment earlier.

A person who has never been seen before resolves to nothing on every read
path. `create=False` everywhere: deciding whether to message somebody must not
create them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import notify_sql
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.notifications import (
    NotificationPreference,
    PendingNotification,
    QueueDepth,
    Recipient,
)

log = structlog.get_logger()

PLATFORM = "discord"

#: How much of the source message crosses the process boundary. The message
#: itself is trimmed much shorter before it is sent; this only bounds what a
#: notification row can carry out of the database.
EXCERPT_CHARS = 500


def _channel_ids(viewer: Viewer) -> list[int]:
    return [c.platform_channel_id for c in viewer.visible_channels]


class PostgresNotificationQueue:
    """Implements `NotificationQueue`."""

    def __init__(self, engine: AsyncEngine, platform: str = PLATFORM) -> None:
        self._engine = engine
        self._platform = platform

    # --- the extraction side -------------------------------------------

    async def queue_obligations(
        self,
        now: datetime,
        min_confidence: float,
        not_before: datetime,
        limit: int,
    ) -> int:
        async with self._engine.begin() as conn:
            queued = await conn.execute(
                notify_sql.QUEUE_OBLIGATIONS,
                {
                    "now": now,
                    "min_confidence": min_confidence,
                    "not_before": not_before,
                    "limit": limit,
                },
            )
            return int(queued.rowcount or 0)

    async def withdraw_settled_asks(self, now: datetime) -> int:
        async with self._engine.begin() as conn:
            withdrawn = await conn.execute(
                notify_sql.WITHDRAW_SETTLED, {"now": now}
            )
            return int(withdrawn.rowcount or 0)

    async def expire_pending(self, cutoff: datetime, now: datetime) -> int:
        async with self._engine.begin() as conn:
            expired = await conn.execute(
                notify_sql.EXPIRE_PENDING, {"cutoff": cutoff, "now": now}
            )
            return int(expired.rowcount or 0)

    # --- the delivery side ---------------------------------------------

    async def recipients_due(
        self,
        now: datetime,
        batch_window: timedelta,
        min_interval: timedelta,
        limit: int,
    ) -> Sequence[Recipient]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                notify_sql.RECIPIENTS_DUE,
                {
                    "platform": self._platform,
                    "batch_ready": now - batch_window,
                    "rate_ready": now - min_interval,
                    "limit": limit,
                },
            )
            return [
                Recipient(
                    person=PersonRef(self._platform, int(row["platform_user_id"])),
                    first_time=bool(row["first_time"]),
                )
                for row in rows.mappings()
            ]

    async def pending_for(
        self, viewer: Viewer, limit: int
    ) -> Sequence[PendingNotification]:
        channels = _channel_ids(viewer)
        if not channels:
            # Nothing is readable, so nothing is sendable. Returning early
            # rather than binding an empty array is the same guard the ask
            # store uses: an unconstrained read here would be the whole queue.
            return []
        async with self._engine.connect() as conn:
            person_id = await self._person_id(conn, viewer.person)
            if person_id is None:
                return []
            rows = await conn.execute(
                notify_sql.PENDING_FOR_VIEWER,
                {"person_id": person_id, "channel_ids": channels, "limit": limit},
            )
            return [self._to_pending(row) for row in rows.mappings()]

    async def settle_unreadable(self, viewer: Viewer, now: datetime) -> int:
        channels = _channel_ids(viewer)
        if not channels:
            # A viewer with nothing readable is either somebody who left the
            # server or a guild cache that is still cold, and the two are
            # indistinguishable from here. Settling on that would silently
            # discard everybody's queue during a reconnect, so this does
            # nothing and the rows wait for the next pass.
            return 0
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, viewer.person)
            if person_id is None:
                return 0
            settled = await conn.execute(
                notify_sql.SETTLE_UNREADABLE,
                {"person_id": person_id, "channel_ids": channels, "now": now},
            )
            return int(settled.rowcount or 0)

    async def mark_sent(
        self, person: PersonRef, ask_keys: Sequence[str], now: datetime
    ) -> int:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return 0
            marked = await conn.execute(
                notify_sql.MARK_SENT,
                {"person_id": person_id, "ask_keys": list(ask_keys), "now": now},
            )
            # In the same transaction as the settle: a delivery recorded
            # without its rate-limit stamp would let the next pass send the
            # same person another message immediately.
            await conn.execute(
                notify_sql.RECORD_SENT, {"person_id": person_id, "now": now}
            )
            return int(marked.rowcount or 0)

    async def record_attempt(self, person: PersonRef, now: datetime) -> None:
        """Stamp a failed attempt, so the rate limit covers it too.

        Nothing is settled here: the obligations are still owed and are tried
        again once the interval has passed. An account the corpus has never
        seen cannot have been queued anything, so an unresolved person is a
        no-op rather than a reason to create one.
        """
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return
            await conn.execute(
                notify_sql.RECORD_ATTEMPT, {"person_id": person_id, "now": now}
            )

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> int:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return 0
            await conn.execute(
                notify_sql.RECORD_UNDELIVERABLE, {"person_id": person_id, "now": now}
            )
            settled = await conn.execute(
                notify_sql.SETTLE_UNDELIVERABLE, {"person_id": person_id, "now": now}
            )
            return int(settled.rowcount or 0)

    # --- the person's own switch ---------------------------------------

    async def set_enabled(
        self, person: PersonRef, enabled: bool
    ) -> NotificationPreference:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person, create=True)
            if person_id is None:  # pragma: no cover - create=True always resolves
                return NotificationPreference(enabled=enabled)
            stored = await conn.execute(
                notify_sql.SET_ENABLED, {"person_id": person_id, "enabled": enabled}
            )
            row = stored.mappings().first()
        if row is None:  # pragma: no cover - the upsert always returns its row
            return NotificationPreference(enabled=enabled)
        return _to_preference(row)

    async def preference(self, person: PersonRef) -> NotificationPreference:
        async with self._engine.connect() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return NotificationPreference()
            found = await conn.execute(
                notify_sql.READ_PREFERENCE, {"person_id": person_id}
            )
            row = found.mappings().first()
        return NotificationPreference() if row is None else _to_preference(row)

    async def depth(self, cap: int) -> QueueDepth:
        async with self._engine.connect() as conn:
            counted = await conn.execute(notify_sql.QUEUE_DEPTH, {"cap": cap})
            pending = int(counted.scalar_one())
        return QueueDepth(pending=pending, capped=pending >= cap)

    # --- internals ------------------------------------------------------

    async def _person_id(
        self, conn: AsyncConnection, person: PersonRef, create: bool = False
    ) -> int | None:
        """The canonical person id for a platform account.

        `create` is False on every path but the one where somebody has just
        told us to stop: a preference has to be storable for a person the
        corpus has never seen, and it is the only write here that a person
        asked for. Everywhere else an unknown account reads as "nothing
        queued" rather than as a new identity.
        """
        found = await conn.execute(
            notify_sql.RESOLVE_PERSON_ID,
            {"platform": person.platform, "platform_user_id": person.platform_user_id},
        )
        existing = found.scalar()
        if existing is not None:
            return int(existing)
        if not create:
            return None
        return await self._create_person(conn, person)

    async def _create_person(self, conn: AsyncConnection, person: PersonRef) -> int:
        """Mirrors the ask store's create path, for the same reason."""
        created = await conn.execute(
            text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
            {"n": str(person.platform_user_id)},
        )
        person_id = int(created.scalar_one())
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES (:p, :u, :i) ON CONFLICT DO NOTHING"
            ),
            {"p": person.platform, "u": person.platform_user_id, "i": person_id},
        )
        return person_id

    def _to_pending(self, row: RowMapping) -> PendingNotification:
        return PendingNotification(
            ask_key=str(row["ask_key"]),
            channel=ChannelRef(self._platform, int(row["channel_id"])),
            channel_name=str(row["channel_name"]),
            source_message_id=int(row["source_message_id"]),
            kind=str(row["kind"]),
            requester_display=str(row["requester_display"]),
            text=str(row["text"]),
            excerpt=" ".join(str(row["source_content"]).split())[:EXCERPT_CHARS],
            asked_at=row["asked_at"],
        )


def _to_preference(row: RowMapping) -> NotificationPreference:
    return NotificationPreference(
        enabled=bool(row["enabled"]),
        undeliverable=row["undeliverable_at"] is not None,
    )
