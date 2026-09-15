"""Postgres implementation of the corpus store and hybrid search."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.app.fusion import reciprocal_rank_fusion
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import DirtyChannel, Message, Window
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()

PLATFORM = "discord"


WindowFactory = Callable[[ChannelRef, Sequence[Message]], Sequence[Window]]
"""Builds windows from messages. Injected so the store stays unaware of the
windowing rules, which live in the app layer and change independently."""


def _message_from_row(row: RowMapping, channel: ChannelRef | None = None) -> Message:
    return Message(
        platform_message_id=cast(int, row["id"]),
        channel=channel or ChannelRef(PLATFORM, cast(int, row["channel_id"])),
        author=PersonRef(PLATFORM, cast(int, row["platform_user_id"])),
        content=str(row["content"]),
        created_at=cast(datetime, row["created_at"]),
        edited_at=cast("datetime | None", row["edited_at"]),
        reply_to_id=cast("int | None", row["reply_to_id"]),
        thread_id=cast("int | None", row["thread_id"]),
    )


def _channel_ids(viewer: Viewer) -> list[int]:
    return [c.platform_channel_id for c in viewer.visible_channels]


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _connect(self) -> AsyncConnection:
        conn = await self._engine.connect()
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        return conn

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        if not messages:
            return 0
        async with self._engine.begin() as conn:
            # The channel row has to exist before any message can reference
            # it, and nothing else creates one: a channel is discovered by
            # ingesting from it, not configured in advance.
            for channel in {m.channel for m in messages}:
                await conn.execute(
                    sql.ENSURE_CHANNEL,
                    {
                        "id": channel.platform_channel_id,
                        "platform": channel.platform,
                        "name": "",
                    },
                )
            written = 0
            # Sorted by id so that two batches overlapping on the same message
            # take the per-message locks below in the same order and cannot
            # deadlock against each other.
            for m in sorted(messages, key=lambda message: message.platform_message_id):
                # Serialises this write against a deletion of the same id: the
                # statement below reads the deletion ledger, and a read is only
                # as good as the ordering around it.
                await conn.execute(sql.LOCK_MESSAGE, {"id": m.platform_message_id})
                person_id = await self._person_id(conn, m.author)
                result = await conn.execute(
                    sql.UPSERT_MESSAGE,
                    {
                        "id": m.platform_message_id,
                        "channel_id": m.channel.platform_channel_id,
                        "author_person_id": person_id,
                        "content": m.content,
                        "created_at": m.created_at,
                        "edited_at": m.edited_at,
                        "reply_to_id": m.reply_to_id,
                        "thread_id": m.thread_id,
                    },
                )
                written += result.rowcount or 0
                await self._replace_mentions(conn, m, person_id)
            return written

    async def _person_id(self, conn: AsyncConnection, person: PersonRef) -> int:
        row = await conn.execute(
            text(
                "SELECT person_id FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"p": person.platform, "u": person.platform_user_id},
        )
        existing = row.scalar()
        if existing is not None:
            return int(existing)

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

    async def _replace_mentions(
        self, conn: AsyncConnection, message: Message, _author_id: int
    ) -> None:
        await conn.execute(
            text("DELETE FROM message_mention WHERE message_id = :m"),
            {"m": message.platform_message_id},
        )
        for mentioned in message.mentions:
            person_id = await self._person_id(conn, mentioned)
            await conn.execute(
                text(
                    # Conditional on the message actually existing. The
                    # opt-out trigger drops an excluded author's row by
                    # returning NULL -- silently, by design, so one opted-out
                    # person cannot fail a whole backfill page. An
                    # unconditional insert here then violated the foreign key
                    # and aborted the transaction; because reconciliation
                    # applies edits before deletions, that abort permanently
                    # killed deletion repair for the channel. Guarding on
                    # existence covers every reason the row may be absent,
                    # rather than just this one.
                    "INSERT INTO message_mention (message_id, person_id) "
                    "SELECT :m, :p WHERE EXISTS "
                    "(SELECT 1 FROM message WHERE id = :m) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"m": message.platform_message_id, "p": person_id},
            )

    async def tombstone_message(self, platform_message_id: int, at: datetime) -> None:
        """Withdraw a message, whether or not the corpus has ever seen it.

        The ledger write comes first and is unconditional. Live messages are
        published to a queue and written later while deletions go straight to
        the database, so a deletion routinely arrives before the insert it
        retracts: the UPDATE below then matched nothing, the insert landed
        afterwards, and the retracted content stayed retrievable forever.
        Recording the tombstone against the bare id makes the withdrawal
        durable in either order -- the insert reads the ledger and is born
        dead -- rather than durable only when the timing happens to co-operate.
        """
        async with self._engine.begin() as conn:
            await conn.execute(sql.LOCK_MESSAGE, {"id": platform_message_id})
            await conn.execute(
                sql.RECORD_TOMBSTONE, {"id": platform_message_id, "at": at}
            )
            await conn.execute(
                sql.TOMBSTONE_MESSAGE, {"id": platform_message_id, "at": at}
            )
            # The window containing it must be rebuilt; until then it must not
            # be retrievable, or the deleted text resurfaces inside it.
            await conn.execute(
                sql.TOMBSTONE_WINDOWS_FOR_MESSAGE,
                {"id": platform_message_id, "at": at},
            )
            # And the neighbours it was windowed with must come back. Derived
            # from the row here so that every deletion path gets it, including
            # reconciliation, which knows only an id.
            await conn.execute(
                sql.MARK_DIRTY_FOR_MESSAGE, {"id": platform_message_id}
            )

    async def purge_channel(self, channel: ChannelRef) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sql.PURGE_CHANNEL, {"channel_id": channel.platform_channel_id}
            )
            return result.rowcount or 0

    async def resolve_person(self, person: PersonRef, display_name: str) -> int:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if display_name:
                # `_person_id` seeds the name from the account id, because the
                # capture path has no name to hand. This path does, so it is
                # written through -- a person row named after a snowflake is
                # unreadable in every operator query that touches it.
                await conn.execute(
                    sql.SET_DISPLAY_NAME,
                    {"person_id": person_id, "display_name": display_name},
                )
            return person_id

    # --- window maintenance --------------------------------------------

    async def messages_without_window(self, limit: int) -> Sequence[Message]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.MESSAGES_WITHOUT_WINDOW, {"limit": limit, "platform": PLATFORM}
            )
            return [_message_from_row(r) for r in rows.mappings()]

    async def replace_windows(self, channel: ChannelRef, windows: Sequence[Window]) -> int:
        """Swap in the windows covering one batch of messages, atomically.

        Re-runnable: the incoming windows supersede exactly the stored windows
        holding the same messages, so running it twice leaves the same rows
        rather than a second copy. One transaction, so a crash mid-rebuild
        leaves the channel with its old windows rather than none.
        """
        if not windows:
            return 0

        channel_id = channel.platform_channel_id
        message_ids = sorted({mid for w in windows for mid in w.message_ids})

        async with self._engine.begin() as conn:
            kept = await self._superseded_embeddings(conn, channel_id, message_ids)
            await conn.execute(
                sql.DELETE_WINDOWS_FOR_MESSAGES,
                {"channel_id": channel_id, "message_ids": message_ids},
            )
            for window in windows:
                created = await conn.execute(
                    sql.INSERT_WINDOW,
                    {
                        "channel_id": channel_id,
                        "text": window.text,
                        "starts_at": window.starts_at,
                        "ends_at": window.ends_at,
                        "thread_id": window.thread_id,
                        # Carried over when the text is byte-identical. The
                        # rebuild loop re-forms a window whenever any of its
                        # messages changes, so without this a single edit
                        # re-embeds the whole conversation around it.
                        "embedding": kept.get(window.text),
                    },
                )
                await conn.execute(
                    sql.INSERT_WINDOW_MESSAGES,
                    {
                        "window_id": int(created.scalar_one()),
                        "message_ids": list(window.message_ids),
                    },
                )
            # A deletion landing between reading these messages and writing
            # their windows tombstoned nothing, because no window held the
            # message yet. Re-applying tombstones here, in the same
            # transaction as the insert, means such a window is never
            # observable live.
            await conn.execute(
                sql.TOMBSTONE_WINDOWS_WITH_DEAD_MESSAGES, {"channel_id": channel_id}
            )
        return len(windows)

    async def mark_windows_dirty(self, channel: ChannelRef, at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.MARK_WINDOWS_DIRTY,
                {"channel_id": channel.platform_channel_id, "at": at},
            )

    async def dirty_channels(self, limit: int = 20) -> Sequence[DirtyChannel]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(sql.DIRTY_CHANNELS, {"limit": limit})
            return [
                DirtyChannel(
                    channel=ChannelRef(PLATFORM, cast(int, r["channel_id"])),
                    since=cast(datetime, r["windows_dirty_from"]),
                    generation=cast(int, r["windows_dirty_seq"]),
                )
                for r in rows.mappings()
            ]

    async def clear_windows_dirty(self, channel: ChannelRef, generation: int) -> None:
        """Clear the mark only if nothing was marked since it was read."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.CLEAR_WINDOWS_DIRTY,
                {"channel_id": channel.platform_channel_id, "seq": generation},
            )

    async def rewindow_channel(
        self, channel: ChannelRef, since: datetime, builder: WindowFactory, limit: int = 2000
    ) -> int:
        """Re-form every window in a channel from `since`, in one transaction.

        Reading the messages and writing the windows must be one transaction:
        otherwise a deletion landing in the gap tombstones nothing (no window
        holds the message yet) and the rebuild then publishes the retracted
        text in a fresh live window.
        """
        channel_id = channel.platform_channel_id
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                sql.MESSAGES_FOR_REWINDOW,
                {
                    "channel_id": channel_id,
                    "since": since,
                    "limit": limit,
                    "platform": PLATFORM,
                },
            )
            messages = [_message_from_row(r, channel) for r in rows.mappings()]
            windows = builder(channel, messages)

            kept = await self._embeddings_in_range(conn, channel_id, since)
            await conn.execute(
                sql.DELETE_WINDOWS_FROM, {"channel_id": channel_id, "since": since}
            )
            for window in windows:
                created = await conn.execute(
                    sql.INSERT_WINDOW,
                    {
                        "channel_id": channel_id,
                        "text": window.text,
                        "starts_at": window.starts_at,
                        "ends_at": window.ends_at,
                        "thread_id": window.thread_id,
                        "embedding": kept.get(window.text),
                    },
                )
                await conn.execute(
                    sql.INSERT_WINDOW_MESSAGES,
                    {
                        "window_id": int(created.scalar_one()),
                        "message_ids": list(window.message_ids),
                    },
                )
            await conn.execute(
                sql.TOMBSTONE_WINDOWS_WITH_DEAD_MESSAGES, {"channel_id": channel_id}
            )
        return len(windows)

    async def _embeddings_in_range(
        self, conn: AsyncConnection, channel_id: int, since: datetime
    ) -> dict[str, str]:
        rows = await conn.execute(
            sql.EMBEDDINGS_FROM, {"channel_id": channel_id, "since": since}
        )
        return {str(r["text"]): str(r["embedding"]) for r in rows.mappings()}

    async def _superseded_embeddings(
        self, conn: AsyncConnection, channel_id: int, message_ids: Sequence[int]
    ) -> dict[str, str]:
        """Vectors of the windows about to be replaced, keyed by their text."""
        rows = await conn.execute(
            sql.SUPERSEDED_EMBEDDINGS,
            {"channel_id": channel_id, "message_ids": list(message_ids)},
        )
        return {str(r["text"]): str(r["embedding"]) for r in rows.mappings()}

    async def windows_missing_embeddings(self, limit: int) -> Sequence[Window]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(sql.WINDOWS_MISSING_EMBEDDINGS, {"limit": limit})
            return [
                Window(
                    channel=ChannelRef(PLATFORM, cast(int, r["channel_id"])),
                    message_ids=tuple(cast("list[int]", r["message_ids"])),
                    text=str(r["text"]),
                    starts_at=cast(datetime, r["starts_at"]),
                    ends_at=cast(datetime, r["ends_at"]),
                    thread_id=cast("int | None", r["thread_id"]),
                    # Populated so the worker can write the result back; a
                    # window read for embedding and returned without its id is
                    # one the worker silently drops.
                    window_id=cast(int, r["id"]),
                )
                for r in rows.mappings()
            ]

    async def store_embedding(self, window_id: int, embedding: Sequence[float]) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.STORE_EMBEDDING,
                {"window_id": window_id, "embedding": sql.vector_literal(embedding)},
            )

    async def get_cursor(self, channel: ChannelRef) -> int | None:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                text("SELECT oldest_message_id FROM ingest_cursor WHERE channel_id = :c"),
                {"c": channel.platform_channel_id},
            )
            value = row.scalar()
            return None if value is None else int(value)

    async def set_cursor(self, channel: ChannelRef, oldest_message_id: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingest_cursor (channel_id, oldest_message_id) "
                    "VALUES (:c, :m) ON CONFLICT (channel_id) DO UPDATE SET "
                    "oldest_message_id = LEAST(ingest_cursor.oldest_message_id, :m), "
                    "updated_at = now()"
                ),
                {"c": channel.platform_channel_id, "m": oldest_message_id},
            )

    # --- reconciliation --------------------------------------------------

    async def stored_revisions(
        self, channel: ChannelRef, since: datetime
    ) -> Mapping[int, datetime]:
        """Message id -> revision, for messages stored since `since`.

        Satisfies `RevisionLedger`. It returns no content and therefore binds
        no viewer -- see `sql.STORED_REVISIONS` for why that is safe here and
        nowhere else.
        """
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.STORED_REVISIONS,
                {"channel_id": channel.platform_channel_id, "since": since},
            )
            return {
                int(r["id"]): cast(datetime, r["revision"]) for r in rows.mappings()
            }


class HybridSearch:
    """Lexical and vector retrieval, fused.

    Both legs bind the viewer's channel set into their own statement, so the
    permission filter constrains each scan rather than the fused output.
    """

    def __init__(
        self, engine: AsyncEngine, embeddings: EmbeddingClient, overfetch: int = 3
    ) -> None:
        self._engine = engine
        self._embeddings = embeddings
        self._overfetch = overfetch

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        channels = _channel_ids(viewer)
        if not channels:
            # No readable channels: there is nothing to search, and an
            # unconstrained query here would return everything.
            return []

        params = {
            "channel_ids": channels,
            "since": query.since,
            "until": query.until,
            "limit": query.limit * self._overfetch,
        }

        async with self._engine.connect() as conn:
            for statement in sql.SESSION_SETUP:
                await conn.execute(text(statement))

            lexical = await conn.execute(sql.LEXICAL_SEARCH, {**params, "q": query.text})
            lexical_hits = [
                self._hit(r, RelevanceSource.LEXICAL) for r in lexical.mappings()
            ]

            vector_hits: list[SearchHit] = []
            embedded = (await self._embeddings.embed([query.text]))[0]
            vector = await conn.execute(
                sql.VECTOR_SEARCH, {**params, "embedding": sql.vector_literal(embedded)}
            )
            vector_hits = [
                self._hit(r, RelevanceSource.VECTOR) for r in vector.mappings()
            ]

            fused = reciprocal_rank_fusion([lexical_hits, vector_hits], limit=query.limit)
            # Hydrated here, inside the search's own connection, because a
            # window without its message ids can only be cited as a link to
            # the channel -- which asks a reader to go and find the claim
            # themselves, and a citation nobody can check is not a citation.
            # Only the fused survivors are resolved, so the cost is one
            # statement per search rather than one per overfetched candidate.
            return await self._with_message_ids(conn, fused)

    async def _with_message_ids(
        self, conn: AsyncConnection, hits: Sequence[SearchHit]
    ) -> Sequence[SearchHit]:
        """Attach each window's messages, in the order they were said."""
        if not hits:
            return hits
        rows = await conn.execute(
            sql.WINDOW_MESSAGE_IDS, {"window_ids": [h.window_id for h in hits]}
        )
        # The statement orders by (window_id, position), so appending in row
        # order reconstructs each window's own sequence.
        by_window: dict[int, list[int]] = {}
        for r in rows.mappings():
            by_window.setdefault(cast(int, r["window_id"]), []).append(
                cast(int, r["message_id"])
            )
        return [
            replace(hit, message_ids=tuple(by_window.get(hit.window_id, ()))) for hit in hits
        ]

    def _hit(self, row: RowMapping, source: RelevanceSource) -> SearchHit:
        return SearchHit(
            window_id=cast(int, row["id"]),
            channel=ChannelRef(PLATFORM, cast(int, row["channel_id"])),
            text=str(row["text"]),
            starts_at=cast(datetime, row["starts_at"]),
            ends_at=cast(datetime, row["ends_at"]),
            score=cast(float, row["score"]),
            relevance_source=source,
        )

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        channels = _channel_ids(viewer)
        if not channels:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(sql.LIST_CHANNELS, {"channel_ids": channels})
            return [ChannelRef(PLATFORM, int(r[0])) for r in rows]

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:
        channels = _channel_ids(viewer)
        if not channels:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.THREAD_CONTEXT,
                {
                    "message_id": platform_message_id,
                    "channel_ids": channels,
                    "limit": radius * 2,
                },
            )
            return [
                Message(
                    platform_message_id=cast(int, r["id"]),
                    channel=ChannelRef(PLATFORM, cast(int, r["channel_id"])),
                    author=PersonRef(PLATFORM, cast(int, r["author_person_id"])),
                    content=str(r["content"]),
                    created_at=cast(datetime, r["created_at"]),
                    edited_at=cast("datetime | None", r["edited_at"]),
                    reply_to_id=cast("int | None", r["reply_to_id"]),
                    thread_id=cast("int | None", r["thread_id"]),
                )
                for r in rows.mappings()
            ]
