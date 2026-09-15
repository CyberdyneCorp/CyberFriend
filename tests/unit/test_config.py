from __future__ import annotations

import pytest
from pydantic import ValidationError

from chatmemory.config import Settings

SECRET = "zzz-discord-bot-token-zzz"

BASE = {
    "discord_token": SECRET,
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
}


def test_indexed_channels_default_to_empty() -> None:
    """Indexing is opt-in: a missing scope must index nothing, not everything."""
    assert Settings(**BASE).indexed_channel_ids == frozenset()


@pytest.mark.parametrize("raw", ["1,2,3", "1 2 3", "1, 2  3"])
def test_indexed_channels_parsed_from_string(raw: str) -> None:
    assert Settings(**BASE, indexed_channel_ids=raw).indexed_channel_ids == {1, 2, 3}


def test_embedding_dimensions_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(**BASE, embedding_dimensions=0)


def test_secrets_are_not_stringified() -> None:
    """Tokens must not leak into logs via repr or str."""
    s = Settings(**BASE)
    assert SECRET not in repr(s.discord_token)
    assert SECRET not in str(s.discord_token)
    assert SECRET not in repr(s)
    assert s.discord_token.get_secret_value() == SECRET


def test_channel_ids_parse_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path that actually runs in production.

    Loading via init kwargs bypasses pydantic-settings' env decoding, so an
    init-only test passes while the deployed process fails to start.
    """
    for key, value in {
        "DISCORD_TOKEN": SECRET,
        "DISCORD_GUILD_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "LLM_API_KEY": "k",
        "INDEXED_CHANNEL_IDS": "100 200",
    }.items():
        monkeypatch.setenv(key, value)

    assert Settings(_env_file=None).indexed_channel_ids == {100, 200}  # type: ignore[call-arg]


def test_comma_separated_channel_ids_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "DISCORD_TOKEN": SECRET,
        "DISCORD_GUILD_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "LLM_API_KEY": "k",
        "INDEXED_CHANNEL_IDS": "100,200,300",
    }.items():
        monkeypatch.setenv(key, value)

    assert Settings(_env_file=None).indexed_channel_ids == {100, 200, 300}  # type: ignore[call-arg]


def test_missing_channel_ids_from_the_environment_indexes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "DISCORD_TOKEN": SECRET,
        "DISCORD_GUILD_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "LLM_API_KEY": "k",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("INDEXED_CHANNEL_IDS", raising=False)

    assert Settings(_env_file=None).indexed_channel_ids == frozenset()  # type: ignore[call-arg]


def test_a_blank_optional_setting_falls_back_to_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform that renders every compose variable writes blanks.

    Declaring a setting in docker-compose.yml must not silently make it
    mandatory: the process sees WINDOW_MAX_MESSAGES="" rather than no such
    variable, and parsing the blank turned every optional setting into a
    required one the moment it was exposed in a UI.
    """
    for key, value in {
        "DISCORD_TOKEN": SECRET,
        "DISCORD_GUILD_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "LLM_API_KEY": "k",
        "WINDOW_MAX_MESSAGES": "",
        "WINDOW_MAX_TOKENS": "",
        "WINDOW_GAP_SECONDS": "",
        "CHAT_MODEL": "",
        "INDEXED_CHANNEL_IDS": "",
    }.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.window_max_messages == 10
    assert settings.chat_model == "gpt-4o"
    assert settings.indexed_channel_ids == frozenset()


def test_a_blank_required_setting_reports_it_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank must not become 'malformed integer' -- that names the wrong problem."""
    for key, value in {
        "DISCORD_TOKEN": SECRET,
        "DISCORD_GUILD_ID": "",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "LLM_API_KEY": "k",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="Field required"):
        Settings(_env_file=None)  # type: ignore[call-arg]
