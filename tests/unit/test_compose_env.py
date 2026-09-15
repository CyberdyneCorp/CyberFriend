"""Every setting an operator can set must reach the container that reads it.

Coolify only accepts environment variables the compose file declares, so a
setting absent here is not merely undocumented -- the platform refuses it and
the operator silently gets the hardcoded default while believing otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()

# Settings whose value genuinely differs per deployment. Ports are fixed by
# the compose file itself and are deliberately not listed.
DEPLOYMENT_SETTINGS = {
    "DISCORD_TOKEN": ("ingest", "bot", "mcp"),
    "DISCORD_GUILD_ID": ("ingest", "bot", "mcp"),
    "INDEXED_CHANNEL_IDS": ("ingest", "bot"),
    "DATABASE_URL": ("ingest", "bot", "mcp"),
    "LLM_BASE_URL": ("ingest", "bot", "mcp"),
    "LLM_API_KEY": ("ingest", "bot", "mcp"),
    "EMBEDDING_MODEL": ("ingest", "bot", "mcp"),
    "EMBEDDING_DIMENSIONS": ("ingest", "bot", "mcp"),
    "CHAT_MODEL": ("bot",),
    "EXTRACTION_MODEL": ("ingest",),
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


def test_only_the_mcp_service_is_published() -> None:
    """ingest and bot hold gateway connections and must not be reachable."""
    assert "SERVICE_FQDN" in service_block("mcp")
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


@pytest.mark.parametrize("service", ["ingest", "bot", "mcp"])
def test_long_running_services_wait_for_the_migration(service: str) -> None:
    block = service_block(service)
    assert "service_completed_successfully" in block, (
        f"{service} may start before the schema exists"
    )


def test_only_one_service_migrates() -> None:
    """Three containers racing the same DDL is a real race: alembic takes no
    lock of its own, so concurrent upgrades can both try to create a table."""
    migrating = [
        s for s in ("migrate", "ingest", "bot", "mcp")
        if "entrypoints.migrate" in service_block(s)
    ]
    assert migrating == ["migrate"], f"more than one service migrates: {migrating}"
