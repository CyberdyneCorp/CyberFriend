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
