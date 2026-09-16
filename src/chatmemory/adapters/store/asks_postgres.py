"""Postgres implementation of the ask store.

Mirrors `postgres.py`: the statements live next door in `asks_sql.py`, and this
binds them. The viewer's channel set and the viewer's own person id are bound
here, from the `Viewer`, and never from anything a caller passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import asks_sql
from chatmemory.app.asks.model import (
    UNATTRIBUTED,
    Addressee,
    AddresseeKind,
    Ask,
    AskKind,
    AskStatus,
    Correction,
    CorrectionOutcome,
    ObligationRequest,
    ReportedAsk,
    StateRefresh,
    to_group,
    to_person,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer

log = structlog.get_logger()

PLATFORM = "discord"

#: Enough of the source message to check the extraction against. The answer
#: layer trims further; this bounds what crosses the process boundary.
EXCERPT_CHARS = 500


def _channel_ids(viewer: Viewer) -> list[int]:
    return [c.platform_channel_id for c in viewer.visible_channels]


class PostgresAskStore:
    """Implements `AskStore`."""

    def __init__(self, engine: AsyncEngine, platform: str = PLATFORM) -> None:
        self._engine = engine
        self._platform = platform

    # --- writes --------------------------------------------------------

    async def record_asks(self, source_message_id: int, asks: Sequence[Ask]) -> int:
        """Replace the asks extracted from one message, in one transaction.

        Idempotent per source message: re-running extraction over the same
        conversation upserts the same keys and withdraws anything no longer
        found, so reprocessing a window neither duplicates obligations nor
        leaves orphans behind.
        """
        async with self._engine.begin() as conn:
            written = 0
            for ask in asks:
                requester_id = await self._person_id(conn, ask.requester, create=True)
                addressee_id = (
                    await self._person_id(conn, ask.addressee.person, create=True)
                    if ask.addressee.person is not None
                    else None
                )
                result = await conn.execute(
                    asks_sql.UPSERT_ASK,
                    {
                        "ask_key": ask.key,
                        "source_message_id": ask.source_message_id,
                        "channel_id": ask.channel.platform_channel_id,
                        "thread_id": ask.thread_id,
                        "requester_person_id": requester_id,
                        "addressee_kind": ask.addressee.kind.value,
                        "addressee_person_id": addressee_id,
                        "addressee_group": ask.addressee.group,
                        "kind": ask.kind.value,
                        "text": ask.text,
                        "confidence": ask.confidence,
                        "asked_at": ask.asked_at,
                    },
                )
                written += result.rowcount or 0

            await conn.execute(
                asks_sql.PRUNE_ASKS,
                {
                    "source_message_id": source_message_id,
                    "keep": [ask.key for ask in asks],
                },
            )
            return written

    async def record_reaction(
        self,
        source_message_id: int,
        person: PersonRef,
        emoji: str,
        at: datetime,
        acknowledging: frozenset[str] = frozenset(),
    ) -> int:
        """Store the reaction and close what it answers, in one transaction.

        Closing here as well as in the periodic pass is not duplication: the
        pass runs every few minutes, and watching a tick you just left do
        nothing for that long reads as the feature being broken. Both run the
        same statement, narrowed.
        """
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person, create=True)
            await conn.execute(
                asks_sql.RECORD_REACTION,
                {
                    "message_id": source_message_id,
                    "person_id": person_id,
                    "emoji": emoji,
                    "at": at,
                },
            )
            if not acknowledging:
                return 0
            closed = await conn.execute(
                asks_sql.CLOSE_ANSWERED_BY_REACTION_FOR_MESSAGE,
                {
                    "emojis": sorted(acknowledging),
                    "source_message_id": source_message_id,
                },
            )
            return int(closed.rowcount or 0)

    async def remove_reaction(
        self,
        source_message_id: int,
        person: PersonRef,
        emoji: str,
        acknowledging: frozenset[str] = frozenset(),
    ) -> int:
        """Withdraw a reaction, and reopen what it had closed.

        A closure that cannot be undone teaches people not to use the tick.
        """
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person, create=True)
            await conn.execute(
                asks_sql.REMOVE_REACTION,
                {
                    "source_message_id": source_message_id,
                    "person_id": person_id,
                    "emoji": emoji,
                },
            )
            if not acknowledging:
                return 0
            reopened = await conn.execute(
                asks_sql.REOPEN_WITHOUT_REACTION,
                {
                    "source_message_id": source_message_id,
                    "emojis": sorted(acknowledging),
                },
            )
            return int(reopened.rowcount or 0)

    async def refresh_state(
        self, now: datetime, stale_after: timedelta, acknowledging: frozenset[str]
    ) -> StateRefresh:
        """Apply every observable transition. No model is consulted."""
        async with self._engine.begin() as conn:
            by_reply = await conn.execute(asks_sql.CLOSE_ANSWERED_BY_REPLY)
            by_reaction = await conn.execute(
                asks_sql.CLOSE_ANSWERED_BY_REACTION, {"emojis": sorted(acknowledging)}
            )
            stale = await conn.execute(
                asks_sql.MARK_STALE, {"cutoff": now - stale_after}
            )
        return StateRefresh(
            answered_by_reply=by_reply.rowcount or 0,
            answered_by_reaction=by_reaction.rowcount or 0,
            marked_stale=stale.rowcount or 0,
        )

    # --- reads ---------------------------------------------------------

    async def obligations(
        self, viewer: Viewer, request: ObligationRequest
    ) -> Sequence[ReportedAsk]:
        params = await self._scope(viewer, request)
        if params is None:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                asks_sql.OBLIGATIONS, {**params, "limit": request.limit}
            )
            return [
                reported
                for row in rows.mappings()
                if (reported := self._to_reported(row)) is not None
            ]

    async def count_outstanding(self, viewer: Viewer, request: ObligationRequest) -> int:
        params = await self._scope(viewer, request)
        if params is None:
            return 0
        async with self._engine.connect() as conn:
            total = await conn.execute(asks_sql.COUNT_OBLIGATIONS, params)
            return int(total.scalar_one())

    async def apply_correction(
        self, viewer: Viewer, correction: Correction
    ) -> CorrectionOutcome:
        channels = _channel_ids(viewer)
        if not channels:
            return CorrectionOutcome.UNKNOWN_ASK
        if correction.by != viewer.person:
            # Defence in depth: the service builds the correction from the
            # viewer, and a mismatch here means somebody assembled one by hand.
            log.warning("asks.correction_identity_mismatch", ask_key=correction.ask_key)
            return CorrectionOutcome.NOT_ADDRESSEE

        async with self._engine.begin() as conn:
            found = await conn.execute(
                asks_sql.ASK_EXISTS,
                {"ask_key": correction.ask_key, "channel_ids": channels},
            )
            if found.first() is None:
                # Either there is no such ask, or it is in a channel this
                # person may not read. The two are reported identically: the
                # difference is itself a disclosure.
                return CorrectionOutcome.UNKNOWN_ASK

            person_id = await self._person_id(conn, viewer.person, create=False)
            if person_id is None:
                return CorrectionOutcome.NOT_ADDRESSEE

            applied = await conn.execute(
                asks_sql.APPLY_CORRECTION,
                {
                    "ask_key": correction.ask_key,
                    "channel_ids": channels,
                    "person_id": person_id,
                    "resolution": correction.resolution.value,
                },
            )
            if applied.first() is None:
                return CorrectionOutcome.NOT_ADDRESSEE
        return CorrectionOutcome.APPLIED

    # --- internals -----------------------------------------------------

    async def _scope(
        self, viewer: Viewer, request: ObligationRequest
    ) -> dict[str, object] | None:
        """Bind parameters for a read, or None when there is nothing to read."""
        channels = _channel_ids(viewer)
        if not channels:
            # No readable channels: an unconstrained query here would return
            # every obligation in the server.
            return None
        async with self._engine.connect() as conn:
            person_id = await self._person_id(conn, viewer.person, create=False)
        if person_id is None:
            return None
        return {
            "channel_ids": channels,
            "person_id": person_id,
            "platform": self._platform,
            "statuses": [s.value for s in request.statuses],
            "kinds": sorted(k.value for k in request.kinds),
            "min_confidence": request.min_confidence,
            "include_commitments": request.include_commitments,
            "since": request.since,
            "until": request.until,
        }

    async def _person_id(
        self, conn: AsyncConnection, person: PersonRef | None, create: bool
    ) -> int | None:
        """The canonical person id for a platform account.

        `create` is False on every read path: resolving a viewer must never
        write, and an unknown person reads as "has no asks" rather than as a
        new identity.
        """
        if person is None:
            return None
        found = await conn.execute(
            asks_sql.RESOLVE_PERSON_ID,
            {"platform": person.platform, "platform_user_id": person.platform_user_id},
        )
        existing = found.scalar()
        if existing is not None:
            return int(existing)
        if not create:
            return None
        return await self._create_person(conn, person)

    async def _create_person(self, conn: AsyncConnection, person: PersonRef) -> int:
        """Mirrors `PostgresStore._person_id`'s create path.

        Repeated rather than shared because reaching into the corpus store for
        it would couple an ask write to that class's transaction handling; the
        insert is small and the conflict clause makes the duplication harmless.
        """
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

    def _to_reported(self, row: RowMapping) -> ReportedAsk | None:
        requester_platform_id = row["requester_platform_id"]
        if requester_platform_id is None:
            # An ask whose requester has no account on this platform cannot be
            # cited, and an obligation nobody can check is not reported.
            log.warning("asks.unresolvable_requester", ask_key=row["ask_key"])
            return None

        addressee = self._to_addressee(row)
        ask = Ask(
            key=str(row["ask_key"]),
            source_message_id=cast(int, row["source_message_id"]),
            channel=ChannelRef(self._platform, cast(int, row["channel_id"])),
            requester=PersonRef(self._platform, int(requester_platform_id)),
            addressee=addressee,
            kind=AskKind(row["kind"]),
            text=str(row["text"]),
            confidence=float(cast(float, row["confidence"])),
            asked_at=cast(datetime, row["asked_at"]),
            status=AskStatus(row["status"]),
            thread_id=cast("int | None", row["thread_id"]),
            ask_id=cast(int, row["id"]),
        )
        return ReportedAsk(
            ask=ask,
            requester_display=str(row["requester_display"]),
            source_excerpt=" ".join(str(row["source_content"]).split())[:EXCERPT_CHARS],
        )

    def _to_addressee(self, row: RowMapping) -> Addressee:
        kind = AddresseeKind(row["addressee_kind"])
        if kind is AddresseeKind.PERSON:
            platform_id = row["addressee_platform_id"]
            if platform_id is None:
                # The person exists but has no account on this platform. We
                # cannot name them, and naming the wrong person is the failure
                # this whole module is arranged to avoid.
                log.warning("asks.unresolvable_addressee", ask_key=row["ask_key"])
                return UNATTRIBUTED
            return to_person(PersonRef(self._platform, int(platform_id)))
        if kind is AddresseeKind.GROUP:
            return to_group(str(row["addressee_group"]))
        return UNATTRIBUTED

