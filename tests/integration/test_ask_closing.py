"""Closing an obligation, against real SQL.

The unit tests prove the event semantics and the wiring. What can only be
proved here is that the statements agree with them: the permission predicate
is a WHERE clause, the addressee check on a reaction is a join condition, and
the spelling of the emoji a gateway records has to be the spelling the closing
statement compares against. A fake written by the same hand as the code cannot
show any of that.

The whole path is exercised the way the processes run it: a message is
persisted as the live loop persists it, an ask is extracted from it, a raw
reaction event is handed to the real `GatewayEventHandler`, and the state pass
the ingest process runs on a timer is what closes the ask. Corrections go in
through the real `/resolve` command object.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from discord import app_commands
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.bot import (
    CORRECTION_APPLIED,
    CORRECTION_REFUSED,
    CyberFriendClient,
)
from chatmemory.adapters.discord.gateway import GatewayEventHandler
from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.ask import AskService
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import (
    AskCandidate,
    AskKind,
    AskStatus,
    ExtractedAsk,
    Extraction,
    ObligationRequest,
)
from chatmemory.app.asks.resolution import ObservedDirectory
from chatmemory.app.asks.state import AskStateService
from chatmemory.app.asks.worker import ExtractionWorker
from chatmemory.app.ingest import IngestService
from chatmemory.app.limits import RateLimiter
from chatmemory.app.windowing import WindowBuilder
from chatmemory.composition import ask_policy
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.ports.store import Store
from tests.integration.conftest import DB_URL

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
GUILD = 4242

OPEN_CH = ChannelRef(PLATFORM, 100)
PRIVATE_CH = ChannelRef(PLATFORM, 300)

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)
CARA = PersonRef(PLATFORM, 3)

ASKED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
REACTED_AT = ASKED_AT + timedelta(minutes=5)
NOW = ASKED_AT + timedelta(hours=1)


@pytest.fixture(autouse=True)
async def _requires_ask_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.ask_reaction')"))
        if present.scalar() is None:
            pytest.skip("ask tables are missing; run `alembic upgrade head`")


def settings() -> Settings:
    return Settings(
        discord_token=SecretStr("x"),
        discord_guild_id=GUILD,
        database_url=SecretStr(DB_URL),
        llm_api_key=SecretStr("k"),
        indexed_channel_ids=frozenset({100, 300}),
    )


def message(
    message_id: int,
    author: PersonRef,
    content: str,
    channel: ChannelRef = OPEN_CH,
    mentions: frozenset[PersonRef] = frozenset(),
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=author,
        content=content,
        created_at=ASKED_AT,
        mentions=mentions,
        author_display="alice" if author == ALICE else "bob",
    )


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


class StubExtractor:
    """One request per candidate, so the pipeline varies and the model does not."""

    async def extract(self, candidate: AskCandidate) -> Extraction:
        return Extraction(
            asks=(
                ExtractedAsk(
                    kind=AskKind.REQUEST, text="review the migration", confidence=0.9
                ),
            )
        )


# --- the event objects the gateway is handed ----------------------------


class Emoji:
    def __init__(self, name: str, custom_id: int | None = None) -> None:
        self.name = name
        self.id = custom_id


class ReactionPayload:
    def __init__(self, message_id: int, user_id: int, emoji: Emoji) -> None:
        self.message_id = message_id
        self.user_id = user_id
        self.emoji = emoji


class _NoSink:
    """The gateway's other collaborators, which a reaction never touches."""

    def is_indexed(self, channel: ChannelRef) -> bool:
        return True

    async def handle_edit(self, message: Message) -> None: ...

    async def handle_delete(
        self, platform_message_id: int, at: datetime | None = None
    ) -> None: ...

    def publish(self, message: Message) -> None: ...


class StaticAcl:
    """Stands in for `DiscordAclResolver`, which needs live guild state.

    Which channels a person can read is Discord's answer, and it is resolved
    against a real guild in the unit tests. What matters here is that whatever
    it answers is bound into the SQL, so the viewer is pinned and the store is
    real.
    """

    def __init__(self, *viewers: Viewer) -> None:
        self._by_person = {v.person: v for v in viewers}

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return self._by_person.get(person, Viewer(person, frozenset()))


class _Response:
    def __init__(self) -> None:
        self.deferred: list[bool] = []

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred.append(ephemeral)


class _Followup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        assert ephemeral, "a correction is nobody else's business"
        self.sent.append(content)


class Interaction:
    def __init__(self, person: PersonRef) -> None:
        self.user = type("User", (), {"id": person.platform_user_id})()
        self.response = _Response()
        self.followup = _Followup()


# --- the pipeline, wired the way the processes wire it ------------------


class Pipeline:
    def __init__(self, engine: AsyncEngine, *viewers: Viewer) -> None:
        self.store: Store = PostgresStore(engine)
        self.asks = PostgresAskStore(engine)
        self.ingest = IngestService(
            source=None,  # type: ignore[arg-type]
            store=self.store,
            windows=WindowBuilder(max_messages=10, max_tokens=512, gap=timedelta(minutes=15)),
            indexed_channels=frozenset({100, 300}),
        )
        directory = ObservedDirectory()
        self.extraction = ExtractionService(
            extractor=StubExtractor(), store=self.asks, directory=directory
        )
        self.worker = ExtractionWorker(
            self.extraction, window_messages=1, directory=directory
        )
        self.state = AskStateService(self.asks)

        # Exactly what `entrypoints/ingest.py` does.
        self.handler = GatewayEventHandler(
            _NoSink(), _NoSink(), now=lambda: REACTED_AT
        )
        self.handler.acknowledge_asks_with(self.state)

        # Exactly what `entrypoints/bot.py` does.
        self.service = AskService(
            acl=StaticAcl(*viewers),
            audiences=None,  # type: ignore[arg-type]
            answers=None,  # type: ignore[arg-type]
            limiter=RateLimiter(),
        )
        # Built through `ask_policy` rather than by hand: the confidence the
        # extractor writes and the one the menu refuses to offer are the same
        # number, and two copies of it drift.
        self.service.attach_corrections(
            CorrectionService(self.asks, ask_policy(settings()))
        )
        self.resolve = CyberFriendClient(self.service, GUILD)._build_resolve_command()

    async def capture(self, *messages: Message) -> None:
        for item in messages:
            assert await self.ingest.capture(item)
            self.worker.submit(item)
        await self.worker.flush_all()

    async def react(
        self, message_id: int, person: PersonRef, emoji: str, custom_id: int | None = None
    ) -> None:
        await self.handler.on_reaction_add(
            ReactionPayload(message_id, person.platform_user_id, Emoji(emoji, custom_id))
        )

    async def refresh(self) -> None:
        await self.state.refresh(NOW)

    async def status(self, ask_key: str) -> str | None:
        async with self.asks._engine.connect() as conn:  # noqa: SLF001 - assertion only
            found = await conn.execute(
                text("SELECT status FROM ask WHERE ask_key = :k"), {"k": ask_key}
            )
            row = found.first()
            return None if row is None else str(row[0])

    async def only_key(self) -> str:
        async with self.asks._engine.connect() as conn:  # noqa: SLF001 - assertion only
            found = await conn.execute(text("SELECT ask_key FROM ask"))
            keys = [str(r[0]) for r in found]
        assert len(keys) == 1, keys
        return keys[0]

    async def reaction_rows(self) -> int:
        async with self.asks._engine.connect() as conn:  # noqa: SLF001 - assertion only
            found = await conn.execute(text("SELECT count(*) FROM ask_reaction"))
            return int(found.scalar_one())

    async def outstanding(self, person: PersonRef) -> int:
        return await self.asks.count_outstanding(
            await self.service._acl.resolve_viewer(person),  # noqa: SLF001
            ObligationRequest(),
        )

    async def run_resolve(self, actor: PersonRef, ask_key: str, said: str) -> str:
        interaction = Interaction(actor)
        await self.resolve.callback(
            interaction,
            ask=ask_key,
            outcome=app_commands.Choice(name=said, value=said),
        )
        [note] = interaction.followup.sent
        return note

    async def menu(self, actor: PersonRef) -> list[app_commands.Choice[str]]:
        which = self.resolve._params["ask"].autocomplete  # noqa: SLF001
        assert which is not None
        return list(await which(Interaction(actor), ""))


@pytest.fixture
def pipeline(clean: AsyncEngine) -> Pipeline:
    return Pipeline(clean, viewer(BOB, OPEN_CH), viewer(ALICE, OPEN_CH, PRIVATE_CH))


ASKING = "@bob can you review the migration?"


async def seeded(pipeline: Pipeline, channel: ChannelRef = OPEN_CH) -> str:
    await pipeline.capture(
        message(10, ALICE, ASKING, channel=channel, mentions=frozenset({BOB}))
    )
    return await pipeline.only_key()


# --- route one: the addressee's tick ------------------------------------


async def test_the_addressees_tick_closes_the_ask_through_real_sql(
    pipeline: Pipeline,
) -> None:
    key = await seeded(pipeline)
    assert await pipeline.status(key) == AskStatus.OPEN.value

    await pipeline.react(10, BOB, "✅")
    await pipeline.refresh()

    assert await pipeline.status(key) == AskStatus.ANSWERED.value
    assert await pipeline.outstanding(BOB) == 0


async def test_the_close_records_the_event_that_produced_it(pipeline: Pipeline) -> None:
    """State is auditable precisely because it names an observable event."""
    key = await seeded(pipeline)
    await pipeline.react(10, BOB, "✅")
    await pipeline.refresh()

    async with pipeline.asks._engine.connect() as conn:  # noqa: SLF001
        found = await conn.execute(
            text("SELECT closed_by, closed_at FROM ask WHERE ask_key = :k"), {"k": key}
        )
        closed_by, closed_at = found.one()
    assert closed_by == "reaction"
    assert closed_at == REACTED_AT


async def test_a_tick_from_anybody_else_closes_nothing(pipeline: Pipeline) -> None:
    """The addressee check is a join condition, not a step a caller performs."""
    key = await seeded(pipeline)

    await pipeline.react(10, CARA, "✅")
    await pipeline.react(10, ALICE, "✅")
    await pipeline.refresh()

    assert await pipeline.status(key) == AskStatus.OPEN.value
    assert await pipeline.outstanding(BOB) == 1


async def test_the_same_reaction_twice_is_one_row_and_one_close(
    pipeline: Pipeline,
) -> None:
    key = await seeded(pipeline)

    await pipeline.react(10, BOB, "✅")
    await pipeline.refresh()
    await pipeline.react(10, BOB, "✅")
    await pipeline.refresh()

    assert await pipeline.reaction_rows() == 1
    assert await pipeline.status(key) == AskStatus.ANSWERED.value


async def test_the_spelling_a_gateway_records_is_one_the_close_matches(
    pipeline: Pipeline,
) -> None:
    """Discord may send ☑ with or without the selector that makes it render as
    an emoji. Stored in the wrong spelling the row closes nothing, for ever."""
    key = await seeded(pipeline)

    await pipeline.react(10, BOB, "☑")  # no U+FE0F
    await pipeline.refresh()

    assert await pipeline.status(key) == AskStatus.ANSWERED.value


async def test_a_reaction_that_acknowledges_nothing_never_reaches_the_table(
    pipeline: Pipeline,
) -> None:
    key = await seeded(pipeline)

    await pipeline.react(10, BOB, "👀")
    await pipeline.react(10, BOB, "white_check_mark", custom_id=99)
    await pipeline.refresh()

    assert await pipeline.reaction_rows() == 0
    assert await pipeline.status(key) == AskStatus.OPEN.value


async def test_a_reaction_on_a_deleted_message_is_not_kept(pipeline: Pipeline) -> None:
    """Deleted content stops being referred to everywhere, immediately."""
    await seeded(pipeline)
    await pipeline.ingest.handle_delete(10, NOW)

    await pipeline.react(10, BOB, "✅")

    assert await pipeline.reaction_rows() == 0
    assert await pipeline.outstanding(BOB) == 0


# --- route two: the addressee's own word --------------------------------


async def test_the_addressee_closes_their_own_ask_from_discord(
    pipeline: Pipeline,
) -> None:
    key = await seeded(pipeline)

    note = await pipeline.run_resolve(BOB, key, "done")

    assert note == CORRECTION_APPLIED["done"]
    assert await pipeline.outstanding(BOB) == 0


async def test_a_correction_outranks_every_later_extraction_pass(
    pipeline: Pipeline,
) -> None:
    """The requirement that makes the feature usable: the model must never
    re-raise an ask a human has already dismissed."""
    key = await seeded(pipeline)
    await pipeline.run_resolve(BOB, key, "not_mine")

    source = message(10, ALICE, ASKING, mentions=frozenset({BOB}))
    await pipeline.extraction.extract_window([source])
    await pipeline.extraction.extract_window([source])

    assert await pipeline.outstanding(BOB) == 0
    assert await pipeline.status(key) == AskStatus.OPEN.value, "the row is kept, not reported"


async def test_only_the_addressee_may_correct(pipeline: Pipeline) -> None:
    key = await seeded(pipeline)

    assert await pipeline.run_resolve(ALICE, key, "done") == CORRECTION_REFUSED
    assert await pipeline.outstanding(BOB) == 1


async def test_a_correction_on_an_unreadable_ask_does_not_confirm_it_exists(
    pipeline: Pipeline,
) -> None:
    """Bob cannot read #leadership. "Not yours" and "no such ask" have to be
    the same sentence, or the difference answers the question."""
    key = await seeded(pipeline, channel=PRIVATE_CH)

    unreadable = await pipeline.run_resolve(BOB, key, "done")
    invented = await pipeline.run_resolve(BOB, "999:request:person:discord:2", "done")
    not_theirs = await pipeline.run_resolve(ALICE, key, "done")

    assert unreadable == invented == not_theirs == CORRECTION_REFUSED


async def test_the_menu_is_scoped_by_the_same_predicate_the_answers_are(
    pipeline: Pipeline,
) -> None:
    key = await seeded(pipeline)

    assert [c.value for c in await pipeline.menu(BOB)] == [key]
    # Alice asked for it; it is not hers to close and not hers to be offered.
    assert await pipeline.menu(ALICE) == []
    assert await pipeline.menu(CARA) == []


async def test_a_closed_ask_leaves_the_menu(pipeline: Pipeline) -> None:
    """Both routes end in the same place: off the list."""
    key = await seeded(pipeline)

    await pipeline.react(10, BOB, "✅")
    await pipeline.refresh()

    assert await pipeline.menu(BOB) == []
    assert await pipeline.run_resolve(BOB, key, "done") == CORRECTION_APPLIED["done"]
