"""Postgres implementation of the decision store.

Embeds at write time, with the embeddings client the ingest process already
owns. Decisions are few, so this is one small call per extracted message that
concluded something -- and doing it here means the read path needs no
backfill before it can rank anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import decisions_sql
from chatmemory.adapters.store.sql import vector_literal
from chatmemory.app.decisions.backfill import BackfillLedger, ScannedMessage
from chatmemory.app.decisions.model import (
    Decision,
    DecisionRequest,
    ReportedDecision,
    search_terms,
)
from chatmemory.app.decisions.ports import DecisionSearch, DecisionStore
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()


class PostgresDecisionStore:
    """Implements `DecisionStore` and `DecisionSearch`."""

    def __init__(
        self, engine: AsyncEngine, embeddings: EmbeddingClient, platform: str = "discord"
    ) -> None:
        self._engine = engine
        self._embeddings = embeddings
        self._platform = platform

    async def record_decisions(
        self, source_message_id: int, decisions: Sequence[Decision]
    ) -> int:
        """Replace the decisions extracted from one message, in one transaction.

        The embedding call is made before the transaction opens, so a slow
        endpoint never holds a connection, and its failure stores NULL rather
        than losing the decision: a row without a vector is still found by its
        words.
        """
        vectors = await self._embed(decisions)
        async with self._engine.begin() as conn:
            written = 0
            for decision, vector in zip(decisions, vectors, strict=True):
                author_id = await self._person_id(conn, decision.author)
                result = await conn.execute(
                    decisions_sql.UPSERT_DECISION,
                    {
                        "decision_key": decision.key,
                        "source_message_id": decision.source_message_id,
                        "channel_id": decision.channel.platform_channel_id,
                        "thread_id": decision.thread_id,
                        "author_person_id": author_id,
                        "evidence_message_ids": list(decision.evidence_message_ids),
                        "summary": decision.summary,
                        "topic": decision.topic,
                        "confidence": decision.confidence,
                        "decided_at": decision.decided_at,
                        "embedding": vector,
                    },
                )
                written += result.rowcount or 0

            await conn.execute(
                decisions_sql.PRUNE_DECISIONS,
                {
                    "source_message_id": source_message_id,
                    "keep": [d.key for d in decisions],
                },
            )
            return written

    async def withdraw(self, source_message_ids: Sequence[int]) -> int:
        if not source_message_ids:
            return 0
        async with self._engine.begin() as conn:
            removed = await conn.execute(
                decisions_sql.WITHDRAW_DECISIONS,
                {"source_message_ids": list(source_message_ids)},
            )
            return int(removed.rowcount or 0)

    async def withdraw_message(self, message_id: int) -> int:
        async with self._engine.begin() as conn:
            removed = await conn.execute(
                decisions_sql.WITHDRAW_MESSAGE_DECISIONS, {"message_id": message_id}
            )
            return int(removed.rowcount or 0)

    async def search(
        self,
        viewer: Viewer,
        request: DecisionRequest,
        query_embedding: Sequence[float] | None,
    ) -> Sequence[ReportedDecision]:
        channels = [c.platform_channel_id for c in viewer.visible_channels]
        if not channels:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                decisions_sql.SEARCH_DECISIONS,
                {
                    "channel_ids": channels,
                    "embedding": (
                        vector_literal(query_embedding) if query_embedding is not None else None
                    ),
                    "terms": search_terms(request.topic),
                    "has_topic": bool(request.topic),
                    "min_confidence": request.min_confidence,
                    "min_similarity": request.min_similarity,
                    "since": request.since,
                    "until": request.until,
                    "limit": request.limit,
                },
            )
            return [self._reported(row) for row in rows.mappings()]

    def _reported(self, row: RowMapping) -> ReportedDecision:
        return ReportedDecision(
            source_message_id=int(row["source_message_id"]),
            channel=ChannelRef(self._platform, int(row["channel_id"])),
            summary=row["summary"],
            topic=row["topic"],
            decided_at=row["decided_at"],
            author_display=row["author_display"],
            source_excerpt=row["source_content"],
        )

    async def _embed(self, decisions: Sequence[Decision]) -> list[str | None]:
        if not decisions:
            return []
        try:
            vectors = await self._embeddings.embed([d.embedding_text for d in decisions])
        except Exception:  # noqa: BLE001 - the decision is worth more than its vector
            log.warning("decisions.embedding_failed", count=len(decisions), exc_info=True)
            return [None] * len(decisions)
        if len(vectors) != len(decisions):
            log.warning("decisions.embedding_count_mismatch", count=len(decisions))
            return [None] * len(decisions)
        return [vector_literal(v) for v in vectors]

    async def _person_id(self, conn: AsyncConnection, person: PersonRef) -> int:
        """The canonical person id for the author, created if unseen.

        Mirrors `PostgresAskStore._create_person`, for the reason given there.
        In practice the author exists: the source message could not have been
        stored without them.
        """
        found = await conn.execute(
            decisions_sql.RESOLVE_PERSON_ID,
            {"platform": person.platform, "platform_user_id": person.platform_user_id},
        )
        existing = found.scalar()
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


class PostgresDecisionBackfill:
    """Implements `BackfillLedger` over the message table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def live_messages_since(
        self,
        since: datetime,
        until: datetime | None,
        channel_ids: frozenset[int],
        after_id: int,
        limit: int,
    ) -> Sequence[ScannedMessage]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                decisions_sql.BACKFILL_SCAN,
                {
                    "since": since,
                    "until": until,
                    "indexed_channel_ids": sorted(channel_ids),
                    "after_id": after_id,
                    "limit": limit,
                },
            )
            return [ScannedMessage(int(r.id), str(r.content)) for r in rows]

    async def reset_extraction(self, message_ids: Sequence[int]) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(decisions_sql.BACKFILL_RESET, {"ids": list(message_ids)})
            return result.rowcount or 0

if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresDecisionStore) -> DecisionStore:
        return store

    def _searches(store: PostgresDecisionStore) -> DecisionSearch:
        return store

    def _backfills(ledger: PostgresDecisionBackfill) -> BackfillLedger:
        return ledger
