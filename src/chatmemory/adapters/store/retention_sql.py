"""SQL for retention and per-person opt-out, and the adapter that runs it.

Statements and adapter share a module here because the SQL *is* the behaviour:
there are no rows to map back into domain objects, only ordered deletes whose
order is the entire design. Splitting them would put the two halves of one
argument in two files.

The order, and why it is the order:

1.  **Windows before messages.** `conversation_window` holds a block of text
    formed from its messages; search reads that text and never re-derives it.
    Deleting a message cascades away its membership row and leaves the window
    -- still live, still carrying the words. So windows go first.

2.  **Windows by `starts_at`, not `ends_at`.** `starts_at` is the timestamp of
    the earliest message a window carries. A window that straddles the cutoff
    holds pre-cutoff content, and keying on `ends_at` would keep exactly those.

3.  **The affected channels are marked dirty.** Deleting a window takes its
    surviving neighbours out of retrieval until they are re-formed. The
    watermark makes that happen on the next rebuild pass rather than whenever
    the "message in no live window" safety net next runs.

Everything here is a delete keyed on a value that does not change, so every
pass is re-runnable and a pass that fails halfway is repaired by the next one.

The opt-out statements are only half of the opt-out. The other half is the
trigger in migration 0008, which drops an opted-out author's row before it is
stored no matter what issued the INSERT -- so a backfill cannot re-import what
this purged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.app.optout import OptOutRegistry, PersonPurge
from chatmemory.app.retention import CorpusPurge, CorpusRetention
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

# --- identity ----------------------------------------------------------
#
# A person may opt out before they have ever been seen as an author: the row
# is created so the exclusion has something to key on, and so the trigger can
# reject their first message rather than their second.

PERSON_BY_PLATFORM_ID = text("""
SELECT person_id FROM person_platform_id
WHERE platform = :platform AND platform_user_id = :platform_user_id
""")

CREATE_PERSON = text("""
INSERT INTO person (display_name) VALUES (:display_name) RETURNING id
""")

LINK_PLATFORM_ID = text("""
INSERT INTO person_platform_id (platform, platform_user_id, person_id)
VALUES (:platform, :platform_user_id, :person_id)
ON CONFLICT DO NOTHING
""")

# --- retention ---------------------------------------------------------

# Keyed on the window's earliest content, not its latest: a window straddling
# the cutoff carries pre-cutoff text, and `ends_at` would retain it.
PURGE_WINDOWS_BEFORE = text("""
DELETE FROM conversation_window
WHERE starts_at < CAST(:cutoff AS timestamptz)
RETURNING channel_id
""")

# Membership rows, mentions, asks and reactions follow by cascade (0002, 0004).
PURGE_MESSAGES_BEFORE = text("""
DELETE FROM message WHERE created_at < CAST(:cutoff AS timestamptz)
""")

# Asks outlive their source message only if extraction ran against something
# already purged; this catches those rather than trusting the cascade alone.
PURGE_ASKS_BEFORE = text("""
DELETE FROM ask WHERE asked_at < CAST(:cutoff AS timestamptz)
""")

# The fetch log records which message linked which URL. That is a record of
# what people shared, so it ages out with everything else.
PURGE_FETCH_LOG_BEFORE = text("""
DELETE FROM document_fetch WHERE fetched_at < CAST(:cutoff AS timestamptz)
""")

# A window whose every message was purged keeps its text and stays searchable.
# Safe to run concurrently with ingestion: a window and its membership rows are
# written in one transaction, so a window with no members is never observable
# from outside the transaction that is building it.
DELETE_EMPTY_WINDOWS = text("""
DELETE FROM conversation_window w
WHERE NOT EXISTS (
    SELECT 1 FROM conversation_window_message wm WHERE wm.window_id = w.id
)
""")

# --- opt-out -----------------------------------------------------------

RECORD_OPT_OUT = text("""
INSERT INTO person_opt_out (person_id, reason)
VALUES (:person_id, :reason)
ON CONFLICT (person_id) DO UPDATE SET reason = EXCLUDED.reason
""")

CLEAR_OPT_OUT = text("""
DELETE FROM person_opt_out WHERE person_id = :person_id
""")

IS_OPTED_OUT = text("""
SELECT 1 FROM person_opt_out WHERE person_id = :person_id
""")

OPTED_OUT_PEOPLE = text("""
SELECT o.person_id, o.opted_out_at, o.reason,
       p.platform, p.platform_user_id
FROM person_opt_out o
LEFT JOIN person_platform_id p ON p.person_id = o.person_id
ORDER BY o.opted_out_at, o.person_id
""")

# The whole window, not the person's share of it. A window is one block of
# text: the only way to remove their words from it is to remove it and let the
# rebuild re-form the surviving conversation without them. `starts_at` comes
# back so the channel can be marked dirty from the earliest point affected.
PURGE_PERSON_WINDOWS = text("""
DELETE FROM conversation_window w
WHERE EXISTS (
    SELECT 1 FROM conversation_window_message wm
    JOIN message m ON m.id = wm.message_id
    WHERE wm.window_id = w.id AND m.author_person_id = :person_id
)
RETURNING w.channel_id, w.starts_at
""")

PURGE_PERSON_MESSAGES = text("""
DELETE FROM message WHERE author_person_id = :person_id
""")

# Asks they made go with their messages by cascade; asks *addressed to* them do
# not, and those name them in a list of outstanding obligations that other
# people can read. An opt-out that leaves "Alice, can you review this" in the
# obligation report has not withdrawn Alice from anything.
PURGE_PERSON_ASKS = text("""
DELETE FROM ask
WHERE requester_person_id = :person_id OR addressee_person_id = :person_id
""")

PURGE_PERSON_REACTIONS = text("""
DELETE FROM ask_reaction WHERE person_id = :person_id
""")

# The mention index is how "who asked me" finds them without reading text. The
# mentioning message belongs to somebody else and stays; this removes the
# person-keyed path into it. See docs/operations.md for that boundary.
PURGE_PERSON_MENTIONS = text("""
DELETE FROM message_mention WHERE person_id = :person_id
""")


async def _person_id(conn: AsyncConnection, person: PersonRef) -> int:
    """Resolve a platform identity to a person row, creating it if unseen.

    Mirrors `PostgresStore._person_id`. Creating on miss is load-bearing here:
    somebody who has never posted must still be able to opt out in advance, and
    the exclusion is keyed on the person row.
    """
    row = await conn.execute(
        PERSON_BY_PLATFORM_ID,
        {"platform": person.platform, "platform_user_id": person.platform_user_id},
    )
    existing = row.scalar()
    if existing is not None:
        return int(existing)

    created = await conn.execute(
        CREATE_PERSON, {"display_name": str(person.platform_user_id)}
    )
    person_id = int(created.scalar_one())
    await conn.execute(
        LINK_PLATFORM_ID,
        {
            "platform": person.platform,
            "platform_user_id": person.platform_user_id,
            "person_id": person_id,
        },
    )
    return person_id


class PostgresRetentionStore:
    """Retention and opt-out against Postgres.

    Each operation is one transaction. A purge that half-applied would leave
    messages whose windows are gone (invisible but re-formable) or windows
    whose messages are gone (visible and not re-formable); only the second is
    a disclosure, and the transaction is what rules it out.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # --- retention -----------------------------------------------------

    async def purge_corpus_before(self, cutoff: datetime) -> CorpusPurge:
        async with self._engine.begin() as conn:
            removed = await conn.execute(PURGE_WINDOWS_BEFORE, {"cutoff": cutoff})
            channels = [int(row[0]) for row in removed]

            messages = await conn.execute(PURGE_MESSAGES_BEFORE, {"cutoff": cutoff})
            asks = await conn.execute(PURGE_ASKS_BEFORE, {"cutoff": cutoff})
            fetches = await conn.execute(PURGE_FETCH_LOG_BEFORE, {"cutoff": cutoff})
            orphaned = await conn.execute(DELETE_EMPTY_WINDOWS)

            # From the cutoff, because that is the earliest point at which a
            # surviving message may now be in no window.
            await self._mark_dirty(conn, {cid: cutoff for cid in channels})

            return CorpusPurge(
                windows=len(channels) + (orphaned.rowcount or 0),
                messages=messages.rowcount or 0,
                asks=asks.rowcount or 0,
                fetch_records=fetches.rowcount or 0,
            )

    # --- opt-out -------------------------------------------------------

    async def record_opt_out(self, person: PersonRef, reason: str = "") -> None:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            await conn.execute(RECORD_OPT_OUT, {"person_id": person_id, "reason": reason})

    async def clear_opt_out(self, person: PersonRef) -> None:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            await conn.execute(CLEAR_OPT_OUT, {"person_id": person_id})

    async def is_opted_out(self, person: PersonRef) -> bool:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                PERSON_BY_PLATFORM_ID,
                {
                    "platform": person.platform,
                    "platform_user_id": person.platform_user_id,
                },
            )
            person_id = row.scalar()
            if person_id is None:
                # Never seen, so nothing is excluded. Deliberately does not
                # create the row: a read must not write.
                return False
            found = await conn.execute(IS_OPTED_OUT, {"person_id": int(person_id)})
            return found.scalar() is not None

    async def purge_person(self, person: PersonRef) -> PersonPurge:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)

            removed = await conn.execute(PURGE_PERSON_WINDOWS, {"person_id": person_id})
            # Earliest affected point per channel: re-forming from there is what
            # brings the surviving neighbours back into retrieval.
            dirty: dict[int, datetime] = {}
            windows = 0
            for channel_id, starts_at in removed:
                windows += 1
                at = _as_utc(starts_at)
                current = dirty.get(int(channel_id))
                if current is None or at < current:
                    dirty[int(channel_id)] = at

            messages = await conn.execute(PURGE_PERSON_MESSAGES, {"person_id": person_id})
            asks = await conn.execute(PURGE_PERSON_ASKS, {"person_id": person_id})
            reactions = await conn.execute(
                PURGE_PERSON_REACTIONS, {"person_id": person_id}
            )
            mentions = await conn.execute(PURGE_PERSON_MENTIONS, {"person_id": person_id})
            orphaned = await conn.execute(DELETE_EMPTY_WINDOWS)

            await self._mark_dirty(conn, dirty)

            return PersonPurge(
                windows=windows + (orphaned.rowcount or 0),
                messages=messages.rowcount or 0,
                asks=asks.rowcount or 0,
                reactions=reactions.rowcount or 0,
                mentions=mentions.rowcount or 0,
            )

    async def _mark_dirty(
        self, conn: AsyncConnection, channels: dict[int, datetime]
    ) -> None:
        for channel_id, at in channels.items():
            await conn.execute(
                sql.MARK_WINDOWS_DIRTY, {"channel_id": channel_id, "at": at}
            )


def _as_utc(value: datetime) -> datetime:
    """Postgres returns timestamptz aware, but a naive row must not crash a purge."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


if TYPE_CHECKING:  # pragma: no cover - these exist to fail type-checking, not to run
    # Protocols are structural, so nothing else would notice this drifting
    # apart from the ports it is built for.
    def _retention_conforms(store: PostgresRetentionStore) -> CorpusRetention:
        return store

    def _optout_conforms(store: PostgresRetentionStore) -> OptOutRegistry:
        return store
