"""Postgres implementation of the corpus store and hybrid search."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.app.fusion import reciprocal_rank_fusion
from chatmemory.app.people import matching, name_key
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import DirtyChannel, Message, Window
from chatmemory.domain.search import (
    PersonCandidate,
    RelevanceSource,
    SearchHit,
    SearchQuery,
)
from chatmemory.ports.sources import EmbeddingClient
from chatmemory.ports.store import ExtractionContext, PendingExtraction

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
        # Empty when the row predates display-name capture; windowing then
        # falls back to the account id, as it did before.
        author_display=cast(str, row["author_display"] or ""),
        content=str(row["content"]),
        created_at=cast(datetime, row["created_at"]),
        edited_at=cast("datetime | None", row["edited_at"]),
        reply_to_id=cast("int | None", row["reply_to_id"]),
        thread_id=cast("int | None", row["thread_id"]),
    )


def _channel_ids(viewer: Viewer) -> list[int]:
    return [c.platform_channel_id for c in viewer.visible_channels]


class PostgresStore:
    def __init__(self, engine: AsyncEngine, media_since: datetime | None = None) -> None:
        """`media_since` is the oldest message whose attachments are recorded as
        pending media; None, the default, records none."""
        self._engine = engine
        self._media_since = media_since

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
                person_id = await self._person_id(conn, m.author, m.author_display)
                if m.author_display:
                    # Keep the latest name the platform gave us, so a rename
                    # does not leave every past citation showing the old one.
                    await conn.execute(sql.UPDATE_PERSON_DISPLAY,
                                       {"id": person_id, "n": m.author_display})
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
                await self._replace_media(conn, m)
            return written

    async def _person_id(
        self, conn: AsyncConnection, person: PersonRef, display: str = ""
    ) -> int:
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
            # The account id only as a last resort: it is what a reader sees
            # in a citation when nothing better is known.
            {"n": display or str(person.platform_user_id)},
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

    async def _replace_media(self, conn: AsyncConnection, message: Message) -> None:
        """Match the message's media rows to the attachments it carries now.

        Attachments an edit removed lose their row whatever `media_since` says.
        New rows are written only from `media_since` on, so switching media on
        never sweeps in what people posted before it.
        """
        await conn.execute(
            sql.DROP_UNATTACHED_MEDIA,
            {
                "message_id": message.platform_message_id,
                "attachment_ids": [ref.attachment_id for ref in message.media],
            },
        )
        if self._media_since is None or message.created_at < self._media_since:
            return
        for ref in message.media:
            await conn.execute(
                sql.UPSERT_MEDIA,
                {
                    "message_id": message.platform_message_id,
                    "attachment_id": ref.attachment_id,
                    "kind": ref.kind.value,
                    "declared_type": ref.content_type,
                    "filename": ref.filename,
                    "byte_size": ref.byte_size,
                    "source_url": ref.url,
                    "duration_secs": ref.duration_seconds,
                },
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
            # Anything transcribed or described from its attachments goes too.
            await conn.execute(sql.WITHDRAW_MEDIA_FOR_MESSAGE, {"id": platform_message_id})
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

    async def unnamed_people(self, limit: int) -> Sequence[PersonRef]:
        """People whose only name is their account id, for `name_people`."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.UNNAMED_PEOPLE, {"platform": PLATFORM, "cap": limit}
            )
            return [PersonRef(PLATFORM, int(r["platform_user_id"])) for r in rows.mappings()]

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

    # --- ask extraction --------------------------------------------------

    async def messages_pending_extraction(
        self, limit: int, channels: Sequence[ChannelRef] = ()
    ) -> Sequence[PendingExtraction]:
        """Messages whose current revision nothing has extracted asks from.

        Mentions come back with them, unlike every other read here: windowing
        renders text and does not care who was tagged, while extraction cannot
        work without it -- the mention is both the reason a message is worth a
        model call and the strongest evidence of who the ask fell to.
        """
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.MESSAGES_PENDING_EXTRACTION,
                {
                    "limit": limit,
                    "platform": PLATFORM,
                    "indexed_channel_ids": [c.platform_channel_id for c in channels],
                },
            )
            return [
                PendingExtraction(
                    message=replace(
                        _message_from_row(r),
                        mentions=frozenset(
                            PersonRef(PLATFORM, int(uid))
                            for uid in cast("list[int]", r["mention_ids"])
                        ),
                    ),
                    generation=cast(int, r["asks_extraction_seq"]),
                )
                for r in rows.mappings()
            ]

    async def record_extraction(self, entries: Sequence[PendingExtraction]) -> int:
        """Mark each message extracted as of the revision its caller read."""
        if not entries:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sql.RECORD_EXTRACTION,
                {
                    "ids": [e.message.platform_message_id for e in entries],
                    "generations": [e.generation for e in entries],
                },
            )
            return result.rowcount or 0

    async def extraction_context(
        self, messages: Sequence[Message], limit: int
    ) -> ExtractionContext:
        ids = [m.platform_message_id for m in messages]
        if not ids:
            return ExtractionContext()
        # Every message asked about gets an entry, empty or not: an absent
        # entry would send the caller back to the batch's partial context.
        preceding: dict[int, list[Message]] = {mid: [] for mid in ids}
        parents: dict[int, Message] = {}
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.EXTRACTION_CONTEXT, {"ids": ids, "limit": limit, "platform": PLATFORM}
            )
            for row in rows.mappings():
                shown = _message_from_row(row)
                if row["role"] == "parent":
                    parents[shown.platform_message_id] = shown
                else:
                    preceding[cast(int, row["context_for"])].append(shown)
        return ExtractionContext(
            preceding={
                mid: tuple(sorted(found, key=lambda m: (m.created_at, m.platform_message_id)))
                for mid, found in preceding.items()
            },
            parents=parents,
        )

    async def pending_extraction_count(
        self, cap: int = 1000, channels: Sequence[ChannelRef] = ()
    ) -> int:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                sql.PENDING_EXTRACTION_COUNT,
                {"cap": cap, "indexed_channel_ids": [c.platform_channel_id for c in channels]},
            )
            return int(row.scalar_one())

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


AUTHOR_CANDIDATES = 200
"""How many of a person's best-ranked messages in the span are grouped into hits.

The topic is ranked over all of their messages in the span first, so this
caps what is grouped, not what is considered: an old on-topic message still
beats newer off-topic ones. With no topic the order is newest first, and a
span in which one person wrote more than this is answered from the newest.
"""

PEOPLE_SCANNED = 200
"""How many visible people whose name contains the typed word are compared.

The statement narrows by a substring of the first word, so this is far more
than any real server shares one; it only bounds a pathological name."""

# Accents folded in SQL the way `timespan.fold` folds them in Python, for the
# narrowing LIKE. The exact decision is made in `app.people` on the full fold.
_ACCENTED = "áàâãäåéèêëíìîïóòôõöúùûüçñý"
_PLAIN = "aaaaaaeeeeiiiiooooouuuucny"


def _like_escape(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _line(row: RowMapping) -> str:
    at = cast(datetime, row["created_at"]).astimezone(UTC)
    return f"[{at:%Y-%m-%d %H:%M} UTC] {row['author_display']}: {row['content']}"


def authored_hits(rows: Sequence[RowMapping], limit: int) -> list[SearchHit]:
    """Group ranked author rows by window: best window first, lines in order.

    A window's rank is its best message's. Its text is only the authors'
    lines, oldest first, so the model reads what was said in the order it was
    said, and nothing anybody else said beside it.
    """
    grouped: dict[int, list[RowMapping]] = {}
    seen: set[int] = set()
    for row in rows:
        # A message held by two live windows is cited from the better one.
        if cast(int, row["message_id"]) not in seen:
            seen.add(cast(int, row["message_id"]))
            grouped.setdefault(cast(int, row["window_id"]), []).append(row)
    return [_authored_hit(window_id, members) for window_id, members in grouped.items()][
        :limit
    ]


def _authored_hit(window_id: int, members: Sequence[RowMapping]) -> SearchHit:
    best = members[0]["score"]
    ordered = sorted(members, key=lambda r: cast(datetime, r["created_at"]))
    return SearchHit(
        window_id=window_id,
        channel=ChannelRef(PLATFORM, cast(int, members[0]["channel_id"])),
        text="\n".join(_line(r) for r in ordered),
        starts_at=cast(datetime, ordered[0]["created_at"]),
        ends_at=cast(datetime, ordered[-1]["created_at"]),
        # Exact cosine against the window; 0 when no topic was ranked.
        score=float(best) if best is not None else 0.0,
        relevance_source=RelevanceSource.VECTOR,
        message_ids=tuple(cast(int, r["message_id"]) for r in ordered),
        author_display=str(ordered[0]["author_display"] or ""),
    )


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

        if query.authors is not None:
            # Set, even to nobody, means "what these people said": an empty set
            # is a search for no one, never a fall back to everyone.
            return await self._authored(channels, query)

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

    async def _authored(self, channels: list[int], query: SearchQuery) -> Sequence[SearchHit]:
        """The authors' own messages, grouped into the windows that cite them.

        One statement carries the viewer's channels, tombstones, the authors
        and the span; see `sql.AUTHOR_SEARCH`. Each hit holds only the
        authors' lines and message ids, so the model never reads what anyone
        else said in the same conversation, and the citation lands on the
        author's own message rather than the window's opening line.
        """
        authors = [a.platform_user_id for a in query.authors or () if a.platform == PLATFORM]
        if not authors:
            return []
        topic = query.text.strip()
        # One embedding for the topic, none without one: the order is then
        # simply newest first.
        embedding = (
            sql.vector_literal((await self._embeddings.embed([topic]))[0]) if topic else None
        )
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.AUTHOR_SEARCH,
                {
                    "channel_ids": channels,
                    "author_ids": authors,
                    "platform": PLATFORM,
                    "since": query.since,
                    "until": query.until,
                    "candidates": AUTHOR_CANDIDATES,
                    "embedding": embedding,
                    "q": topic,
                },
            )
            return authored_hits(list(rows.mappings()), query.limit)

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = 6
    ) -> Sequence[PersonCandidate]:
        channels = _channel_ids(viewer)
        typed = name_key(name).split()
        if not channels or not typed:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.PEOPLE_VISIBLE,
                {
                    "channel_ids": channels,
                    "platform": PLATFORM,
                    "accented": _ACCENTED,
                    "plain": _PLAIN,
                    "pattern": f"%{_like_escape(typed[0])}%",
                    "cap": PEOPLE_SCANNED,
                },
            )
            visible = [
                PersonCandidate(
                    PersonRef(PLATFORM, int(r["platform_user_id"])), str(r["display_name"])
                )
                for r in rows.mappings()
                if r["platform_user_id"] is not None
            ]
        return matching(name, visible, limit)

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
                    "platform": PLATFORM,
                },
            )
            return [
                Message(
                    platform_message_id=cast(int, r["id"]),
                    channel=ChannelRef(PLATFORM, cast(int, r["channel_id"])),
                    author=PersonRef(PLATFORM, cast(int, r["platform_user_id"])),
                    content=str(r["content"]),
                    created_at=cast(datetime, r["created_at"]),
                    edited_at=cast("datetime | None", r["edited_at"]),
                    reply_to_id=cast("int | None", r["reply_to_id"]),
                    thread_id=cast("int | None", r["thread_id"]),
                )
                for r in rows.mappings()
            ]
