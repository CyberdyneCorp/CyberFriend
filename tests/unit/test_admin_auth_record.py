"""The change record: attributable, append-only, and refusals included.

The record is the only review a configuration change gets -- there is no pull
request on a checkbox -- so these assert the three properties that make it
worth having rather than the fields it happens to carry.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints

import pytest

import chatmemory
from chatmemory.adapters.store import admin_postgres, admin_sql
from chatmemory.admin.audit import (
    SECRET_SETTINGS,
    SHELL_ACTOR,
    ChangeKind,
    ChangeRecordStore,
    ConfigurationChange,
    InMemoryChangeRecord,
    SecretNeverRecorded,
    applied,
    by_shell,
    escalation,
    refused,
)
from chatmemory.admin.auth import Operator

ANA = Operator("ana")
AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


# --- what a change captures --------------------------------------------


async def test_a_change_records_the_operator_the_setting_and_both_values() -> None:
    record = InMemoryChangeRecord()
    entry = await record.record(
        applied(ANA, "retention_days", before="30", after="90"), now=AT
    )
    assert entry.operator == "ana"
    assert entry.setting == "retention_days"
    assert entry.before == "30"
    assert entry.after == "90"
    assert entry.kind is ChangeKind.APPLIED
    assert entry.recorded_at == AT


async def test_a_refused_change_is_recorded_with_what_was_attempted_and_why() -> None:
    """'Somebody tried and was refused' is the entry that matters afterwards."""
    record = InMemoryChangeRecord()
    entry = await record.record(
        refused(
            ANA,
            "allowed_tools",
            "no configured server offers github:merge_pr",
            before="github:search",
            attempted="github:search, github:merge_pr",
        )
    )
    assert entry.kind is ChangeKind.REFUSED
    assert entry.before == "github:search"
    assert entry.after == "github:search, github:merge_pr"
    assert entry.reason == "no configured server offers github:merge_pr"


async def test_enabling_a_mutating_tool_is_recorded_as_an_escalation() -> None:
    """Distinct from an ordinary edit, so 'what got more permissive' is a filter."""
    record = InMemoryChangeRecord()
    entry = await record.record(
        escalation(
            ANA,
            "allowed_tools",
            before="github:search",
            after="github:search, github:merge_pr",
            reason="github:merge_pr modifies state",
        )
    )
    assert entry.kind is ChangeKind.ESCALATION
    assert entry.kind is not ChangeKind.APPLIED


async def test_the_newest_entries_come_back_first() -> None:
    record = InMemoryChangeRecord()
    for value in ("30", "60", "90"):
        await record.record(applied(ANA, "retention_days", before="0", after=value))
    assert [e.after for e in await record.recent(2)] == ["90", "60"]


async def test_the_returned_entries_cannot_be_edited_by_their_reader() -> None:
    record = InMemoryChangeRecord()
    await record.record(applied(ANA, "retention_days", before="30", after="90"))
    entries = await record.recent()
    with pytest.raises(AttributeError):
        entries[0].operator = "ben"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        entries.append(entries[0])  # type: ignore[attr-defined]


# --- attribution ---------------------------------------------------------


def test_the_console_constructors_take_an_operator_not_a_name() -> None:
    """A handler cannot attribute a change to a name it was handed.

    `Operator` comes from `current_operator()`, which comes from the
    credential. A `str` parameter here would be a place for a request field
    to arrive, which is the whole failure this capability is built against.
    """
    for constructor in (applied, refused, escalation):
        hints = get_type_hints(constructor)
        first = list(inspect.signature(constructor).parameters)[0]
        assert first == "operator"
        assert hints["operator"] is Operator


def test_a_cli_change_is_attributed_to_the_shell_not_to_a_person() -> None:
    """Bootstrapping is circular; inventing a name for it would be a lie."""
    change = by_shell("console_access:ana", ChangeKind.ESCALATION, "0", "1")
    assert change.operator == SHELL_ACTOR
    with pytest.raises(ValueError):
        Operator(SHELL_ACTOR)


def test_a_change_must_name_who_made_it_and_what_it_changed() -> None:
    with pytest.raises(ValueError):
        ConfigurationChange(operator="  ", setting="retention_days", kind=ChangeKind.APPLIED)
    with pytest.raises(ValueError):
        ConfigurationChange(operator="ana", setting=" ", kind=ChangeKind.APPLIED)


# --- secrets never reach the record --------------------------------------


@pytest.mark.parametrize("setting", sorted(SECRET_SETTINGS))
def test_a_change_naming_an_environment_only_secret_cannot_be_recorded(
    setting: str,
) -> None:
    """A refusal recorded with the value it refused would defeat the refusal."""
    with pytest.raises(SecretNeverRecorded):
        applied(ANA, setting, before="old", after="hunter2")
    with pytest.raises(SecretNeverRecorded):
        refused(ANA, setting, "secrets are environment-only", attempted="hunter2")


@pytest.mark.parametrize("setting", ["DISCORD_TOKEN", " Database_URL "])
def test_the_secret_check_is_not_defeated_by_spelling(setting: str) -> None:
    with pytest.raises(SecretNeverRecorded):
        applied(ANA, setting, before=None, after="hunter2")


def test_the_three_environment_only_secrets_are_the_ones_the_spec_names() -> None:
    assert {"discord_token", "llm_api_key", "database_url"} == SECRET_SETTINGS


# --- append-only ---------------------------------------------------------


def test_the_port_can_express_nothing_but_an_append_and_a_read() -> None:
    methods = {n for n in vars(ChangeRecordStore) if not n.startswith("_")}
    assert methods == {"record", "recent"}


@pytest.mark.parametrize(
    "implementation", [InMemoryChangeRecord, admin_postgres.PostgresChangeRecord]
)
def test_no_implementation_offers_an_edit_or_a_delete(implementation: type) -> None:
    forbidden = {
        name
        for name in dir(implementation)
        if not name.startswith("_")
        and any(word in name for word in ("update", "delete", "remove", "edit", "purge"))
    }
    assert forbidden == set()


def test_no_sql_rewrites_the_change_record() -> None:
    """The statements are the reachable surface; there must be no way in.

    Checked against the module's source rather than a list of statement
    names, because the failure this guards is a statement somebody adds
    later without reading this file.
    """
    source = (
        Path(chatmemory.__file__).parent / "adapters" / "store" / "admin_sql.py"
    ).read_text()
    tree = ast.parse(source)
    # Only what is handed to `text(...)`. The module docstring explains that
    # there is no UPDATE here and would otherwise match its own explanation.
    statements = [
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "text"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    assert len(statements) >= 7, "the statements were not found; the scan is broken"
    rewrites = [
        sql
        for sql in statements
        if "config_audit" in sql
        and any(verb in sql.upper() for verb in ("UPDATE ", "DELETE ", "TRUNCATE "))
    ]
    assert rewrites == [], f"admin_sql can rewrite the change record: {rewrites}"


def test_the_migration_refuses_a_rewrite_at_the_database_too() -> None:
    """The port only binds callers that went through it, not a psql prompt."""
    migration = (
        Path(chatmemory.__file__).parent.parent.parent
        / "migrations"
        / "versions"
        / "0012_admin_auth_and_change_record.py"
    ).read_text()
    assert "BEFORE UPDATE OR DELETE ON config_audit" in migration
    assert "RAISE EXCEPTION" in migration


def test_the_recent_statement_reads_the_record_and_never_writes_it() -> None:
    assert str(admin_sql.RECENT_CONFIG_AUDIT).strip().upper().startswith("SELECT")
