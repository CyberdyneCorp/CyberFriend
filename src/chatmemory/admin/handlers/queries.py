"""Everything the console reads from the corpus. Counts and timings only.

This is the only module in the console that touches the corpus tables, and it
is deliberately small enough to read in one sitting, because the rule it has
to keep is a negative one: **no statement here selects a content column.** Not
`message.content`, not `conversation_window.text`, not an ask's text, not a
document's. An operator asking "is ingestion keeping up" is answered with
numbers and timestamps; the console is not a second way into private channels,
and a console that could quote one line of a private channel would be exactly
that regardless of how the line got there.

`tests/unit/test_admin_api_queries.py` reflects over the statements below and
fails on one that names a content column, so the rule survives somebody adding
a statement without reading this docstring. The wider SQL audit
(`test_sql_audit.py`) reflects over `adapters/store/*`, which these are not:
they run for an operator rather than for a viewer, and there is no viewer to
scope them to. That is precisely why they must return no content -- a count
cannot disclose who said what, so there is nothing for a permission predicate
to protect.

Every read is wrapped so that a database that is down reports itself as down.
A status page that renders zeros when it cannot reach the database says
"nothing has been ingested", which is the one answer an operator must not be
given when the truth is "I cannot tell".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.retention_sql import OPTED_OUT_PEOPLE
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()


# --- what an operator is told ------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestionProgress:
    """How much has been archived, and how recently.

    `newest_message_at` is the figure that catches a silently dead gateway: a
    process can be up, healthy and connected while ingesting nothing, and the
    only external symptom is that this timestamp stops moving.
    """

    messages: int
    windows: int
    newest_message_at: datetime | None
    channels_with_cursor: int
    channels_backfilled: int
    channels_pending_rebuild: int


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """The console's read-only view of the system's work.

    Every count is optional and is `None` -- never zero -- when the database
    could not be read. "Nothing is stored" and "I could not ask" are different
    operational facts, and an operator acts differently on each.
    """

    database_reachable: bool
    ingestion: IngestionProgress | None = None
    embedding_backlog: int | None = None
    indexed_channels: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelEvidence:
    """What the corpus records about one channel.

    Named for what it is. The console holds no platform credential -- that is
    the whole point of the service -- so it cannot ask Discord whether the bot
    can read a channel. What it can say is whether the bot ever *has*: a
    channel with stored messages was readable at least as recently as the last
    one. That is evidence, not a permission check, and the channel handler
    reports it as such rather than dressing it up as an answer it does not
    have.
    """

    channel_id: int
    name: str
    is_indexed: bool
    messages: int
    last_message_at: datetime | None

    @property
    def read_by_bot(self) -> bool:
        return self.messages > 0


@dataclass(frozen=True, slots=True)
class OptOutEntry:
    """One person who has withdrawn, and when.

    `person` is None for somebody whose platform account is not linked, which
    happens when a person row exists from a purge and nothing else. The reason
    text stored beside the exclusion is deliberately not carried: it is free
    text an operator wrote about a person, and the console asks for a list of
    who has opted out, not for a file on them.
    """

    person: PersonRef | None
    person_id: int
    since: datetime

    def __str__(self) -> str:
        return str(self.person) if self.person is not None else f"person#{self.person_id}"


# --- ports -------------------------------------------------------------


class CorpusStatus(Protocol):
    async def snapshot(self) -> StatusSnapshot:
        """Counts and timings. Never raises: a failure is part of the answer."""
        ...


class ChannelDirectory(Protocol):
    async def channels(self) -> Sequence[ChannelEvidence]: ...

    async def channel(self, channel_id: int) -> ChannelEvidence | None: ...


class OptOutDirectory(Protocol):
    async def opted_out(self) -> Sequence[OptOutEntry]: ...


# --- statements --------------------------------------------------------
#
# Scalar subqueries in one round trip rather than six statements: the console
# polls this, and six queries that disagree with each other by a few hundred
# milliseconds make a backlog look like it is moving when it is not.

STATUS_COUNTS = text("""
SELECT
    (SELECT count(*) FROM message WHERE deleted_at IS NULL) AS messages,
    (SELECT max(created_at) FROM message WHERE deleted_at IS NULL) AS newest_message_at,
    (SELECT count(*) FROM conversation_window WHERE deleted_at IS NULL) AS windows,
    (SELECT count(*) FROM conversation_window
      WHERE deleted_at IS NULL AND embedding IS NULL) AS embedding_backlog,
    (SELECT count(*) FROM channel WHERE is_indexed) AS indexed_channels,
    (SELECT count(*) FROM ingest_cursor) AS channels_with_cursor,
    (SELECT count(*) FROM ingest_cursor WHERE backfill_complete) AS channels_backfilled,
    (SELECT count(*) FROM ingest_cursor
      WHERE windows_dirty_from IS NOT NULL) AS channels_pending_rebuild
""")

# The channel's own row plus how much of it the bot has stored. `name` is
# platform metadata, not conversation: it is what an operator recognises a
# channel by, and without it the console lists bare snowflakes and an operator
# removes the wrong one.
CHANNEL_EVIDENCE = text("""
SELECT c.id, c.name, c.is_indexed,
       (SELECT count(*) FROM message m
         WHERE m.channel_id = c.id AND m.deleted_at IS NULL) AS messages,
       (SELECT max(m.created_at) FROM message m
         WHERE m.channel_id = c.id AND m.deleted_at IS NULL) AS last_message_at
FROM channel c
ORDER BY c.id
""")


# --- adapters ----------------------------------------------------------


class PostgresCorpusStatus:
    """Implements `CorpusStatus`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def snapshot(self) -> StatusSnapshot:
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(STATUS_COUNTS)
                row = result.mappings().one()
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never raised
            # A status page is the one surface that has to survive the failure
            # it is reporting on. Raising here would render an error where the
            # operator needs the words "the database is unreachable".
            log.warning("admin.status_unavailable", error=type(exc).__name__)
            return StatusSnapshot(database_reachable=False)
        return StatusSnapshot(
            database_reachable=True,
            ingestion=IngestionProgress(
                messages=int(row["messages"]),
                windows=int(row["windows"]),
                newest_message_at=_moment(row["newest_message_at"]),
                channels_with_cursor=int(row["channels_with_cursor"]),
                channels_backfilled=int(row["channels_backfilled"]),
                channels_pending_rebuild=int(row["channels_pending_rebuild"]),
            ),
            embedding_backlog=int(row["embedding_backlog"]),
            indexed_channels=int(row["indexed_channels"]),
        )


class PostgresChannelDirectory:
    """Implements `ChannelDirectory`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def channels(self) -> Sequence[ChannelEvidence]:
        async with self._engine.connect() as conn:
            result = await conn.execute(CHANNEL_EVIDENCE)
            return tuple(_channel(row) for row in result.mappings())

    async def channel(self, channel_id: int) -> ChannelEvidence | None:
        # Filtered here rather than in SQL: the channel list is bounded by how
        # many channels a guild has, and one statement is one thing to audit.
        return next((c for c in await self.channels() if c.channel_id == channel_id), None)


class PostgresOptOutDirectory:
    """Implements `OptOutDirectory`.

    Runs `retention_sql.OPTED_OUT_PEOPLE`, which was written for this list and
    until now had no caller. Reusing it rather than writing a second statement
    keeps the exclusion list something the SQL audit already covers.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def opted_out(self) -> Sequence[OptOutEntry]:
        async with self._engine.connect() as conn:
            result = await conn.execute(OPTED_OUT_PEOPLE)
            return tuple(_opt_out(row) for row in result.mappings())


def _channel(row: RowMapping) -> ChannelEvidence:
    return ChannelEvidence(
        channel_id=int(row["id"]),
        name=str(row["name"] or ""),
        is_indexed=bool(row["is_indexed"]),
        messages=int(row["messages"]),
        last_message_at=_moment(row["last_message_at"]),
    )


def _opt_out(row: RowMapping) -> OptOutEntry:
    platform = row["platform"]
    user_id = row["platform_user_id"]
    person = (
        PersonRef(str(platform), int(user_id))
        if platform is not None and user_id is not None
        else None
    )
    at = _moment(row["opted_out_at"])
    return OptOutEntry(
        person=person,
        person_id=int(row["person_id"]),
        # Not nullable in the schema; the fallback exists so a list of who has
        # withdrawn can still be shown if one row is odd.
        since=at if at is not None else datetime.fromtimestamp(0).astimezone(),
    )


def _moment(value: object) -> datetime | None:
    """A timestamp column, or None. Never a string: the caller formats it.

    Defensive about the type because these rows come back from `max(...)` over
    an empty table as NULL, and a status page must not raise on a corpus that
    is simply empty.
    """
    return value if isinstance(value, datetime) else None
