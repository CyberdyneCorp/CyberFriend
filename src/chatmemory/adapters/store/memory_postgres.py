"""Postgres implementation of the conversation-memory store.

Binds the statements in `memory_sql`. Its one decision of its own is how a set
of `ChannelRef`s becomes the bigint array the permission predicate compares,
and that is where it fails closed: a channel from a platform other than the
conversation's cannot be compared with a readable set of platform channel ids,
so a turn claiming one is refused rather than stored with a provenance that
would mean something else.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import memory_sql, retention_sql
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.memory import (
    ConversationLocation,
    MemoryPurge,
    MemoryStore,
    Recollection,
    RememberedSummary,
    RememberedTurn,
)

log = structlog.get_logger()


class UnverifiableProvenance(ValueError):
    """A turn's source channels cannot be checked against a readable set."""


def _location_params(location: ConversationLocation) -> dict[str, object]:
    return {
        "location_platform": location.platform,
        "location_id": location.platform_location_id,
        "location_direct": location.direct,
    }


def _person_params(person: PersonRef) -> dict[str, object]:
    return {"platform": person.platform, "platform_user_id": person.platform_user_id}


def _source_channel_ids(
    location: ConversationLocation, channels: frozenset[ChannelRef]
) -> list[int]:
    """Provenance as the ids a viewer's readable set is bound as.

    `:channel_ids` elsewhere is the viewer's platform channel ids, with the
    platform implied. A foreign-platform channel id could coincide with a
    readable one and pass a check it has nothing to do with.
    """
    foreign = sorted(str(c) for c in channels if c.platform != location.platform)
    if foreign:
        raise UnverifiableProvenance(
            f"source channels {foreign} are not on {location.platform!r}; "
            "their readability cannot be re-checked, so the turn is not stored"
        )
    return sorted({c.platform_channel_id for c in channels})


def _recalled_channels(
    location: ConversationLocation, ids: Sequence[int]
) -> frozenset[ChannelRef]:
    """The inverse of `_source_channel_ids`: stored ids are the location's platform's."""
    return frozenset(ChannelRef(location.platform, int(i)) for i in ids)


def _viewer_channel_ids(viewer: Viewer) -> list[int]:
    # Same derivation as `PostgresStore`'s `_channel_ids`, so memory is judged
    # by exactly the readable set retrieval is.
    return sorted({c.platform_channel_id for c in viewer.visible_channels})


async def _person_id(conn: AsyncConnection, person: PersonRef) -> int:
    """Resolve, creating on miss: the asker may never have posted a message.

    Re-reads after linking. `LINK_PLATFORM_ID` does nothing on conflict, so a
    concurrent first write can leave this call's freshly created row unlinked;
    the id that counts is whichever one the mapping holds.
    """
    params = _person_params(person)
    existing = (await conn.execute(retention_sql.PERSON_BY_PLATFORM_ID, params)).scalar()
    if existing is not None:
        return int(existing)
    created = await conn.execute(
        retention_sql.CREATE_PERSON, {"display_name": str(person.platform_user_id)}
    )
    await conn.execute(
        retention_sql.LINK_PLATFORM_ID,
        {**params, "person_id": int(created.scalar_one())},
    )
    linked = await conn.execute(retention_sql.PERSON_BY_PLATFORM_ID, params)
    return int(linked.scalar_one())


class PostgresMemoryStore:
    """Implements `MemoryStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_turn(
        self,
        person: PersonRef,
        location: ConversationLocation,
        question: str,
        answer: str,
        source_channels: frozenset[ChannelRef],
    ) -> bool:
        channel_ids = _source_channel_ids(location, source_channels)
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            result = await conn.execute(
                memory_sql.INSERT_TURN,
                {
                    "person_id": person_id,
                    **_location_params(location),
                    "question": question,
                    "answer": answer,
                    "source_channel_ids": channel_ids,
                },
            )
            stored = result.scalar() is not None
        if not stored:
            # The opt-out trigger dropped it. Not an error: the question was
            # answered, it is just not remembered.
            log.info("memory.turn_not_stored", person=str(person), reason="opted_out")
        return stored

    async def recall(
        self, viewer: Viewer, location: ConversationLocation, turn_limit: int
    ) -> Recollection:
        if turn_limit <= 0:
            return Recollection()
        params: dict[str, object] = {
            **_person_params(viewer.person),
            **_location_params(location),
            "channel_ids": _viewer_channel_ids(viewer),
        }
        async with self._engine.connect() as conn:
            summary_rows = await conn.execute(memory_sql.RECALL_SUMMARIES, params)
            summaries = tuple(
                RememberedSummary(
                    text=str(row["text"]),
                    through_turn_id=int(row["through_turn_id"]),
                    created_at=cast(datetime, row["created_at"]),
                    covered_channels=_recalled_channels(
                        location, cast(Sequence[int], row["covered_channel_ids"])
                    ),
                )
                for row in summary_rows.mappings()
            )
            turn_rows = await conn.execute(
                memory_sql.RECALL_TURNS, {**params, "turn_limit": turn_limit}
            )
            turns = tuple(
                RememberedTurn(
                    turn_id=int(row["id"]),
                    question=str(row["question"]),
                    answer=str(row["answer"]),
                    asked_at=cast(datetime, row["created_at"]),
                    source_channels=_recalled_channels(
                        location, cast(Sequence[int], row["channel_ids"])
                    ),
                )
                for row in turn_rows.mappings()
            )
        return Recollection(summaries=summaries, turns=turns)

    async def record_summary(
        self,
        person: PersonRef,
        location: ConversationLocation,
        text: str,
        through_turn_id: int,
    ) -> bool:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            params: dict[str, object] = {"person_id": person_id, **_location_params(location)}
            await conn.execute(memory_sql.LOCK_PERSON_MEMORY, {"person_id": person_id})
            result = await conn.execute(
                memory_sql.REPLACE_WITH_SUMMARY,
                {**params, "text": text, "through_turn_id": through_turn_id},
            )
            return result.scalar() is not None

    async def forget(
        self, person: PersonRef, location: ConversationLocation | None
    ) -> MemoryPurge:
        params = _person_params(person)
        async with self._engine.begin() as conn:
            if location is None:
                turns = await conn.execute(memory_sql.FORGET_TURNS_EVERYWHERE, params)
                summaries = await conn.execute(memory_sql.FORGET_SUMMARIES_EVERYWHERE, params)
            else:
                scoped = {**params, **_location_params(location)}
                turns = await conn.execute(memory_sql.FORGET_TURNS_AT, scoped)
                summaries = await conn.execute(memory_sql.FORGET_SUMMARIES_AT, scoped)
        purge = MemoryPurge(turns=turns.rowcount or 0, summaries=summaries.rowcount or 0)
        log.info(
            "memory.forgotten",
            person=str(person),
            location=str(location) if location else "everywhere",
            turns=purge.turns,
            summaries=purge.summaries,
        )
        return purge

    async def purge_before(self, cutoff: datetime) -> MemoryPurge:
        async with self._engine.begin() as conn:
            turns = await conn.execute(memory_sql.PURGE_TURNS_BEFORE, {"cutoff": cutoff})
            summaries = await conn.execute(
                memory_sql.PURGE_SUMMARIES_BEFORE, {"cutoff": cutoff}
            )
        return MemoryPurge(turns=turns.rowcount or 0, summaries=summaries.rowcount or 0)


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresMemoryStore) -> MemoryStore:
        return store
