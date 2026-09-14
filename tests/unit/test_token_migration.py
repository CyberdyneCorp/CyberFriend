"""The token table is the schema's, not the process's.

`ensure_token_schema` created `mcp_token` at start-up, which meant the table
existed in every running deployment and in no schema dump: alembic did not
know about it, and a restore came back without anyone's credentials. It now
belongs to migration 0005, and these assert that the two descriptions of the
table have not drifted apart and that no long-running service creates it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

import chatmemory
from chatmemory.mcp import auth

SRC = Path(chatmemory.__file__).parent
MIGRATION = SRC.parent.parent / "migrations" / "versions" / "0005_mcp_token.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_chained_where_it_says_it_is() -> None:
    module = _load(MIGRATION)
    assert module.revision == "0005"
    assert module.down_revision == "0004"


@pytest.mark.parametrize(
    "column",
    ["token_hash", "platform", "platform_user_id", "label", "issued_at", "revoked_at"],
)
def test_the_migration_and_the_runtime_creator_agree(column: str) -> None:
    """Two writers of one table must describe the same table.

    A column added to one and not the other is invisible until a fresh
    database behaves differently from every existing one.
    """
    migration = MIGRATION.read_text()
    runtime = "\n".join(auth.TOKEN_SCHEMA)
    assert column in migration and column in runtime


def test_both_index_the_live_credential_lookup() -> None:
    migration = MIGRATION.read_text()
    runtime = "\n".join(auth.TOKEN_SCHEMA)
    for source in (migration, runtime):
        assert "ix_mcp_token_person" in source
        assert re.search(r"revoked_at IS NULL", source)


@pytest.mark.parametrize("entrypoint", ["mcp_server.py", "ingest.py", "bot.py"])
def test_no_service_creates_its_own_token_schema(entrypoint: str) -> None:
    """Schema created by a service drifts from alembic without a sound."""
    source = (SRC / "entrypoints" / entrypoint).read_text()
    assert "ensure_token_schema" not in source
