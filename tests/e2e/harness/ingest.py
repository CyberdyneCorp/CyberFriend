"""The ingest process's capture and ask-extraction halves, over fake edges.

The bot process never sees chatter it is not addressed in; ingest captures it.
So a scenario about obligations needs this other half: a message FakeDiscord
builds goes through the one conversion live capture uses (`to_message`), is
persisted by the production `IngestService`, and is handed to the extraction
worker `build_ask_pipeline` assembles -- the same calls `live_loop` makes, in
the same order.

Only the extractor is swapped, and it still speaks the real prompt: the
candidate is rendered by `render_candidate`, sent to `ScriptedChat` as the
`ask_extraction` schema, and the reply is read by `parse_extractions`. So what
a scenario scripts is the model's JSON, and everything after it -- candidate
filtering, addressee resolution, the ask store's SQL -- is production's.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import cast

import discord
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.source import RawMessage, to_message
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.asks.model import AskCandidate, ExtractedAsk
from chatmemory.app.asks.prompt import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    parse_extractions,
    render_candidate,
)
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.composition import build_ask_pipeline
from chatmemory.config import Settings
from chatmemory.domain.messages import Message
from chatmemory.ports.sources import ChatSource
from tests.e2e.harness.model import ASK_EXTRACTION, ScriptedChat


class ChatAskExtractor:
    """An `AskExtractor` that asks a `ChatModel` with the production prompt."""

    def __init__(self, chat: ScriptedChat) -> None:
        self._chat = chat

    async def extract(self, candidate: AskCandidate) -> Sequence[ExtractedAsk]:
        reply = await self._chat.complete_json(
            SYSTEM_PROMPT, render_candidate(candidate), OUTPUT_SCHEMA, ASK_EXTRACTION
        )
        return parse_extractions(reply.data)


class Ingest:
    """Capture, then extraction, over the database the bot answers from."""

    def __init__(self, settings: Settings, engine: AsyncEngine, chat: ScriptedChat) -> None:
        store = PostgresStore(engine)
        self.service = IngestService(
            # Only backfill reads the source, and no scenario backfills.
            source=cast(ChatSource, None),
            store=store,
            windows=WindowBuilder(
                max_messages=settings.window_max_messages,
                max_tokens=settings.window_max_tokens,
                gap=timedelta(seconds=settings.window_gap_seconds),
            ),
            indexed_channels=settings.indexed_channel_ids,
        )
        self.asks = build_ask_pipeline(settings, engine, extractor=ChatAskExtractor(chat))
        self.asks.worker.records_through(store)

    async def capture(self, raw: discord.Message) -> Message:
        """What `live_loop` does with one gateway message: persist, then submit."""
        message = to_message(cast(RawMessage, raw))
        assert message is not None, f"not indexable: {raw.content!r}"
        assert await self.service.capture(message), f"out of scope: {raw.content!r}"
        self.asks.worker.submit(message)
        return message

    async def extract(self) -> int:
        """Run the extraction pass over everything captured, ready or not."""
        return await self.asks.worker.flush_all()
