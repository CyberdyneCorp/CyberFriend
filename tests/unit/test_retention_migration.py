"""Migration 0008 describes the opt-out the code relies on.

The opt-out's guarantee -- that a backfill cannot re-import what somebody asked
us to forget -- is enforced by a trigger, not by a caller. So the migration is
the security control, and these assert the three things about it that the rest
of the system takes for granted.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

import chatmemory

MIGRATION = (
    Path(chatmemory.__file__).parent.parent.parent / "migrations" / "versions" / "0008_optout.py"
)


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load(MIGRATION)
SOURCE = MIGRATION.read_text()


def test_the_migration_is_chained_where_it_says_it_is() -> None:
    assert MODULE.revision == "0008"
    assert MODULE.down_revision == "0007"


@pytest.mark.parametrize("column", ["person_id", "reason", "opted_out_at"])
def test_the_exclusion_list_has_the_columns_the_adapter_writes(column: str) -> None:
    assert column in SOURCE


def test_the_exclusion_is_keyed_on_the_person_not_the_account() -> None:
    """Somebody with two Discord accounts opted out once, not once per account."""
    assert 'sa.ForeignKey("person.id"' in SOURCE


@pytest.mark.parametrize("guard", ["MESSAGE_GUARD", "ENTRY_GUARD"])
def test_both_corpora_are_guarded(guard: str) -> None:
    """Messages and uploads. A guard on one is not an opt-out."""
    body = getattr(MODULE, guard)
    assert "person_opt_out" in body
    assert "RETURN NULL" in body


@pytest.mark.parametrize("guard", ["MESSAGE_GUARD", "ENTRY_GUARD"])
def test_a_withdrawal_is_exempt_from_the_guard(guard: str) -> None:
    """Blocking an UPDATE that sets `deleted_at` would make a half-purged
    person's rows permanently un-tombstonable."""
    assert "NEW.deleted_at IS NOT NULL" in getattr(MODULE, guard)


@pytest.mark.parametrize(
    "table", ["ON message", "ON document_entry"]
)
def test_the_triggers_fire_on_insert_and_update(table: str) -> None:
    assert f"BEFORE INSERT OR UPDATE {table} " in SOURCE


def test_the_guards_skip_rather_than_raise() -> None:
    """An exception would abort the whole backfill page, stopping ingestion for
    everyone else in the channel rather than dropping one row."""
    assert "RAISE" not in SOURCE.upper().replace("RAISE_", "")
