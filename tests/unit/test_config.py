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
