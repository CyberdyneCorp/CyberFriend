"""Migration 0014 describes the fact table the code relies on.

Opt-out and person deletion reach personal facts through the database, not
through a caller, so the migration is the control. These check its shape
without a database; tests/integration/test_facts_store.py checks it runs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import chatmemory

MIGRATIONS = Path(chatmemory.__file__).parent.parent.parent / "migrations" / "versions"
MIGRATION = MIGRATIONS / "0014_person_fact.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load(MIGRATION)
SOURCE = MIGRATION.read_text()


def test_the_migration_follows_the_current_head() -> None:
    assert MODULE.revision == "0014"
    assert MODULE.down_revision == "0013"


def test_one_fact_per_person_and_kind() -> None:
    assert 'sa.UniqueConstraint("person_id", "kind"' in SOURCE


def test_the_kind_is_closed_in_the_database() -> None:
    assert "kind IN (" in SOURCE


def test_deleting_a_person_deletes_their_facts() -> None:
    assert 'sa.ForeignKey("person.id", ondelete="CASCADE")' in SOURCE


def test_facts_are_guarded_and_purged_by_the_opt_out() -> None:
    assert "person_opt_out" in MODULE.FACT_GUARD
    assert "RETURN NULL" in MODULE.FACT_GUARD
    assert "DELETE FROM person_fact" in MODULE.FACT_PURGE_ON_OPT_OUT
    assert "AFTER INSERT OR UPDATE ON person_opt_out" in SOURCE
    assert "BEFORE INSERT OR UPDATE ON person_fact" in SOURCE


def test_the_guard_does_not_share_a_function_with_memory() -> None:
    """0013's downgrade drops its own function; sharing it would let that
    remove the fact guard as a side effect."""
    assert "reject_opted_out_memory" not in SOURCE
    assert "purge_memory_on_opt_out" not in SOURCE
