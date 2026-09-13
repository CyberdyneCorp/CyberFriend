"""Postgres implementation of the corpus store and hybrid search."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.app.fusion import reciprocal_rank_fusion
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()

PLATFORM = "discord"


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
            written = 0
            for m in messages:
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
                    "INSERT INTO message_mention (message_id, person_id) "
                    "VALUES (:m, :p) ON CONFLICT DO NOTHING"
                ),
                {"m": message.platform_message_id, "p": person_id},
            )

    async def tombstone_message(self, platform_message_id: int, at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.TOMBSTONE_MESSAGE, {"id": platform_message_id, "at": at}
            )
            # The window containing it must be rebuilt; until then it must not
            # be retrievable, or the deleted text resurfaces inside it.
            await conn.execute(
                sql.TOMBSTONE_WINDOWS_FOR_MESSAGE,
                {"id": platform_message_id, "at": at},
            )

    async def purge_channel(self, channel: ChannelRef) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sql.PURGE_CHANNEL, {"channel_id": channel.platform_channel_id}
            )
            return result.rowcount or 0

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

        return reciprocal_rank_fusion([lexical_hits, vector_hits], limit=query.limit)

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
