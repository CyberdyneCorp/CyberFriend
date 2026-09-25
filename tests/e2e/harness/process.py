"""The bot process, assembled the way `main` assembles it, over fake edges.

`assemble` is the function production calls; only the `Edges` differ. So a
wiring omission in the real graph -- a collaborator built and handed to
nothing -- is an omission here too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.composition import Edges
from chatmemory.config import Settings
from chatmemory.entrypoints.bot import assemble
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.discord_wire import ChannelSpec, FakeDiscord, GuildLayout
from tests.e2e.harness.ingest import Ingest
from tests.e2e.harness.model import HashEmbeddings, ScriptedChat
from tests.e2e.harness.web import FakeWeb, NetworkSeal, default_fixtures

GUILD_ID = 7
GENERAL = 100
LEADERSHIP = 300
LEAD = "lead"

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
"""The clock every scenario starts at."""


class FakeClock:
    """`Edges.clock`, standing still at `NOW` until a scenario moves it."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> datetime:
        self.now += by
        return self.now


def layout() -> GuildLayout:
    """#general, which everyone reads, and #leadership, which only `lead` does."""
    return GuildLayout(
        guild_id=GUILD_ID,
        roles=(LEAD,),
        channels=(
            ChannelSpec(GENERAL, "general"),
            ChannelSpec(LEADERSHIP, "leadership", readable_by=(LEAD,)),
        ),
    )


def e2e_settings(database_url: str) -> Settings:
    """A deployment with wallet and positions lookups over a fake Infura key.

    Everything else is the default a deployment gets. Every value a scenario
    depends on is set here, where it beats the environment and `.env`.
    """
    return Settings.model_validate(
        {
            "discord_token": "e2e-token",
            "discord_guild_id": GUILD_ID,
            "database_url": database_url,
            "llm_api_key": "e2e-key",
            "indexed_channel_ids": f"{GENERAL} {LEADERSHIP}",
            "wallet_tools_enabled": True,
            "infura_key": "e2e-infura",
            "web_tools_enabled": False,
            "market_tools_enabled": False,
            "tracing_enabled": False,
            "notifications_enabled": True,
            "scheduled_tasks_enabled": False,
            "federation_servers": "",
            "chat_model_capabilities": "",
        }
    )


async def start(settings: Settings, engine: AsyncEngine, seal: NetworkSeal) -> E2EBot:
    """Assemble the process over fakes, and connect it to the fake guild.

    The ingest half shares the bot's model script and database, so what a
    scenario captures and extracts is what the bot then answers from.
    """
    chat = ScriptedChat()
    web = FakeWeb(default_fixtures())
    embeddings = HashEmbeddings(settings.embedding_dimensions)
    edges = Edges(
        chat=chat,
        summary_chat=chat,
        embeddings=embeddings,
        engine=engine,
        http_transport=web.transport,
        clock=FakeClock(),
    )
    process = await assemble(settings, edges)
    wire = FakeDiscord(process.graph.client, layout())
    await wire.start()
    return E2EBot(process, wire, chat, web, embeddings, seal, Ingest(settings, engine, chat))
