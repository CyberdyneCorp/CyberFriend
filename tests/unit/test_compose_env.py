"""Every setting an operator can set must reach the container that reads it.

Coolify only accepts environment variables the compose file declares, so a
setting absent here is not merely undocumented -- the platform refuses it and
the operator silently gets the hardcoded default while believing otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chatmemory.app.configuration import SECRETS, SETTINGS

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()

# Settings whose value genuinely differs per deployment. Ports are fixed by
# the compose file itself and are deliberately not listed.
DEPLOYMENT_SETTINGS = {
    "DISCORD_TOKEN": ("ingest", "bot", "mcp"),
    "DISCORD_GUILD_ID": ("ingest", "bot", "mcp"),
    # The MCP server reads scope live too, and the environment is its baseline
    # until something is stored.
    "INDEXED_CHANNEL_IDS": ("ingest", "bot", "mcp", "admin"),
    "DATABASE_URL": ("ingest", "bot", "mcp", "admin"),
    "LLM_BASE_URL": ("ingest", "bot", "mcp"),
    "LLM_API_KEY": ("ingest", "bot", "mcp"),
    "EMBEDDING_MODEL": ("ingest", "bot", "mcp"),
    "EMBEDDING_DIMENSIONS": ("ingest", "bot", "mcp"),
    "CHAT_MODEL": ("bot",),
    # The bot summarises conversation memory on the extraction model, so it
    # needs the operator's choice as much as the ingest process does.
    "EXTRACTION_MODEL": ("ingest", "bot"),
    # Federation and the web tools decide what the agent may reach; asks
    # decide what it extracts. All of them are settings an operator changes
    # per deployment, so the platform has to accept them.
    "FEDERATION_SERVERS": ("bot", "admin"),
    "FEDERATION_TOOL_ALLOWLIST": ("bot", "admin"),
    # The other half of enabling a state-changing tool. Undeclared here, an
    # operator naming holders in the platform gets an empty broker and every
    # mutating call is refused for want of a credential -- the confirmation
    # gate would be configured, reported at startup, and still unreachable.
    "FEDERATION_CREDENTIAL_HOLDERS": ("bot", "admin"),
    "FEDERATION_MAX_TOOLS_PER_RUN": ("bot", "admin"),
    "WEB_TOOLS_ENABLED": ("bot", "admin"),
    "SERPAPI_KEY": ("bot",),
    # Market data is merged into the bot's federation; declared here or an
    # operator enabling it in the platform silently keeps it off.
    "MARKET_TOOLS_ENABLED": ("bot",),
    "MARKET_MAX_CALLS_PER_RUN": ("bot",),
    "MARKET_TIMEOUT_SECONDS": ("bot",),
    # Wallet balances are merged into the bot's federation like the market
    # tools; declared here or an operator enabling it in the platform silently
    # keeps it off.
    "WALLET_TOOLS_ENABLED": ("bot",),
    "INFURA_KEY": ("bot",),
    "WALLET_MAX_CALLS_PER_RUN": ("bot",),
    "WALLET_TIMEOUT_SECONDS": ("bot",),
    "ASK_EXTRACTION_ENABLED": ("ingest", "admin"),
    "ASK_MIN_CONFIDENCE": ("ingest", "admin"),
    # Conversation memory. Retention is enforced by the ingest sweep, so a
    # window set in the platform but never passed there is a retention policy
    # that silently does not apply.
    "MEMORY_RECENT_TURNS": ("bot",),
    "MEMORY_SUMMARISE_AFTER_TURNS": ("bot",),
    "MEMORY_RETENTION_DAYS": ("ingest",),
    # Notifications are produced in one process and delivered by another, so
    # each half gets the settings it actually applies. The switch reaches
    # both: an operator turning it off in the platform must stop the queueing
    # and the sending, and a half-off feature would queue rows for ever.
    "NOTIFICATIONS_ENABLED": ("ingest", "bot"),
    # The batching window and the rate are applied where the sending happens.
    # Declared only in the platform and never passed here, an operator's
    # chosen rate would be a number nothing reads while the bot messages
    # people on the hardcoded default.
    "NOTIFICATION_BATCH_WINDOW_SECONDS": ("bot",),
    "NOTIFICATION_MIN_INTERVAL_SECONDS": ("bot",),
    "NOTIFICATION_MAX_ITEMS": ("bot",),
    # The two bounds on what may enter the queue are applied where the
    # queueing happens.
    "NOTIFICATION_MAX_AGE_HOURS": ("ingest",),
    "NOTIFICATION_EXPIRE_HOURS": ("ingest",),
    # Tracing exports from the bot and withdraws from ingest, so both halves
    # need the same three settings. Declared for the bot alone, a deletion
    # would never reach the trace store and deleted text would stay legible
    # there -- the failure this project's deletion guarantee exists to stop.
    # Scheduled tasks run in the bot, because running one means answering a
    # question and the answer stack is there. Declared for the bot alone for
    # that reason -- ingest has nothing to do with them.
    "SCHEDULED_TASKS_ENABLED": ("bot",),
    "SCHEDULED_TASKS_PER_PERSON": ("bot",),
    "SCHEDULED_SWEEP_SECONDS": ("bot",),
    "TRACING_ENABLED": ("ingest", "bot"),
    "LANGFUSE_HOST": ("ingest", "bot"),
    "LANGFUSE_PUBLIC_KEY": ("ingest", "bot"),
    "LANGFUSE_SECRET_KEY": ("ingest", "bot"),
    "TRACING_TIMEOUT_SECONDS": ("ingest", "bot"),
}


def service_block(name: str) -> str:
    match = re.search(rf"^  {name}:$(.*?)(?=^  \w+:$|\Z)", COMPOSE, re.M | re.S)
    assert match, f"service {name} missing from docker-compose.yml"
    return match.group(1)


@pytest.mark.parametrize(
    ("setting", "services"),
    [(k, v) for k, vs in DEPLOYMENT_SETTINGS.items() for v in [vs]],
)
def test_setting_reaches_every_service_that_reads_it(
    setting: str, services: tuple[str, ...]
) -> None:
    for service in services:
        assert f"{setting}=${{{setting}}}" in service_block(service), (
            f"{service} never receives {setting}; an operator setting it in the "
            "platform gets the hardcoded default instead, silently"
        )


def test_ingest_runs_exactly_one_replica() -> None:
    """Two containers on one bot token double-ingest, silently."""
    assert "replicas: 1" in service_block("ingest")


@pytest.mark.parametrize("service", ["mcp", "admin"])
def test_the_published_services_each_get_their_own_domain(service: str) -> None:
    """Separate domains, so the console can be closed to the internet without
    taking retrieval down with it."""
    assert "SERVICE_FQDN" in service_block(service)


def test_the_gateway_services_are_not_published() -> None:
    """ingest and bot hold gateway connections and must not be reachable."""
    for private in ("ingest", "bot"):
        assert "SERVICE_FQDN" not in service_block(private)


# --- deploy ordering ---------------------------------------------------


def test_a_migrate_service_exists() -> None:
    """Without it the containers start against a schema-less database and
    fail on their first statement, which reads as an application bug."""
    assert "\n  migrate:\n" in COMPOSE


def test_migrate_runs_once_and_exits() -> None:
    block = service_block("migrate")
    assert 'restart: "no"' in block, "a one-shot job must not be restarted"
    assert "chatmemory.entrypoints.migrate" in block


@pytest.mark.parametrize("service", ["ingest", "bot", "mcp", "admin"])
def test_long_running_services_wait_for_the_migration(service: str) -> None:
    block = service_block(service)
    assert "service_completed_successfully" in block, (
        f"{service} may start before the schema exists"
    )


def test_only_one_service_migrates() -> None:
    """Three containers racing the same DDL is a real race: alembic takes no
    lock of its own, so concurrent upgrades can both try to create a table."""
    migrating = [
        s for s in ("migrate", "ingest", "bot", "mcp", "admin")
        if "entrypoints.migrate" in service_block(s)
    ]
    assert migrating == ["migrate"], f"more than one service migrates: {migrating}"


# --- migrations must not need application credentials ------------------

MIGRATION_SOURCES = [
    ROOT / "migrations" / "env.py",
    ROOT / "src" / "chatmemory" / "entrypoints" / "migrate.py",
    *(ROOT / "migrations" / "versions").glob("*.py"),
]


@pytest.mark.parametrize("path", MIGRATION_SOURCES, ids=lambda p: p.name)
def test_migrations_do_not_load_application_settings(path: Path) -> None:
    """A migration needs a database and nothing else.

    Settings validates every field, including the Discord credentials. Loading
    it from a migration coupled creating the schema to having a bot token, so
    the first deploy of a new environment -- where no token is configured yet
    -- failed on a validation error about `discord_guild_id` and took the
    whole deployment with it. The coupling was three layers deep: the
    entrypoint, alembic's env.py, and two migrations reading the embedding
    width.
    """
    source = path.read_text()
    assert "get_settings" not in source, (
        f"{path.name} loads application settings; migrations must need only "
        "DATABASE_URL, or a missing unrelated credential blocks the schema"
    )


# --- the console -------------------------------------------------------


def test_the_console_service_exists_and_runs_its_own_entrypoint() -> None:
    block = service_block("admin")
    assert "chatmemory.entrypoints.admin" in block


def test_every_editable_setting_reaches_the_console() -> None:
    """Derived from the setting registry, not from a list kept beside it.

    The console reports whether each value came from the database, the
    environment or a default. A setting the platform never passes to this
    container reads as "default" there while the agent is using the
    operator's environment value -- which is the screen lying about the one
    thing it exists to explain. Adding a setting and forgetting the compose
    file fails here rather than three months later on a support call.
    """
    block = service_block("admin")
    missing = [
        key.upper()
        for key in SETTINGS
        if f"{key.upper()}=${{{key.upper()}}}" not in block
    ]
    assert not missing, (
        f"the console never receives {missing}; it will report them as defaults "
        "while the agent runs on the operator's values"
    )


def test_the_console_holds_database_credentials_and_no_others() -> None:
    """The point of the whole change.

    A console that could redeploy would need platform access, which is far
    more authority than configuring the agent requires -- and one holding the
    bot token could impersonate the bot everywhere it is installed.
    """
    block = service_block("admin")
    held = [
        secret.env_var for secret in SECRETS.values() if secret.env_var in block
    ]
    assert held == ["DATABASE_URL"], f"the console is given {held}"


def test_the_console_is_not_given_the_model_endpoint_either() -> None:
    """It neither embeds nor answers, so it has no reason to reach the model.

    Named separately from the credentials because LLM_BASE_URL is not a
    secret -- it is the *reachability*, and a service with no reason to hold
    a route to the model endpoint should not be given one.
    """
    block = service_block("admin")
    for variable in ("LLM_BASE_URL", "EMBEDDING_MODEL", "CHAT_MODEL", "DISCORD_GUILD_ID"):
        assert variable not in block


def test_the_console_serves_its_interface_from_the_same_container() -> None:
    """One container: no second domain, no certificate, and no CORS policy on
    the highest-privilege surface in the system."""
    assert "ADMIN_CONSOLE_DIR=" in service_block("admin")


def test_the_console_runs_exactly_one_replica() -> None:
    """Two replicas make editing a list-shaped setting a read-modify-write
    race between operators, and the losing edit is silent."""
    assert "replicas: 1" in service_block("admin")


# --- a dead service must not report healthy ----------------------------


@pytest.mark.parametrize("service", ["ingest", "bot", "mcp"])
def test_gateway_services_are_checked_on_readiness(service: str) -> None:
    """`/health` is liveness and deliberately dependency-free.

    A process that serves HTTP while failing to reach Discord answered it
    200 for as long as it stayed up, so a crash-looping bot went an hour
    reporting healthy. These three hold gateway connections; readiness is
    what says whether they can do their job.
    """
    block = service_block(service)
    assert "/ready" in block, f"{service} is checked on liveness only"
    assert "/health'" not in block


@pytest.mark.parametrize("service", ["ingest", "bot", "mcp"])
def test_the_check_tolerates_a_reconnect(service: str) -> None:
    """Readiness dips during a normal reconnect; restarting on the first dip
    would turn a Discord blip into a restart loop."""
    block = service_block(service)
    assert "retries: 5" in block
    assert "start_period: 90s" in block
