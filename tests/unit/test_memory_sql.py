"""Structural checks on the memory statements.

The SQL audit proves every content-returning statement binds `:channel_ids`.
Memory needs two more predicates in the same statement -- whose conversation,
and which location -- because a statement that bound the viewer's channels but
not the viewer's identity would return another person's turns that happen to
draw on readable channels.
"""

from __future__ import annotations

import pytest
from sqlalchemy.sql.elements import TextClause

from chatmemory.adapters.store import memory_sql

STATEMENTS: dict[str, TextClause] = {
    name: value for name, value in vars(memory_sql).items() if isinstance(value, TextClause)
}

READS = sorted(name for name, s in STATEMENTS.items() if str(s).lstrip().startswith("SELECT")
               and "pg_advisory" not in str(s))

TEXT_COLUMNS = ("question", "answer", "text")


def test_reads_were_found() -> None:
    assert READS == ["RECALL_SUMMARIES", "RECALL_TURNS"]


@pytest.mark.parametrize("name", READS)
def test_every_read_binds_viewer_identity_location_and_channels(name: str) -> None:
    statement = str(STATEMENTS[name])
    for bind in (
        ":channel_ids",
        ":platform",
        ":platform_user_id",
        ":location_direct",
        ":location_platform",
        ":location_id",
    ):
        assert bind in statement, f"{name} does not bind {bind}"


@pytest.mark.parametrize("name", READS)
def test_every_read_checks_containment_before_ranking(name: str) -> None:
    statement = str(STATEMENTS[name])
    assert "<@ CAST(:channel_ids AS bigint[])" in statement
    assert statement.index(":channel_ids") < statement.index("ORDER BY")


@pytest.mark.parametrize("name", sorted(set(STATEMENTS) - set(READS)))
def test_nothing_else_returns_remembered_text(name: str) -> None:
    """Only a viewer-scoped read may hand back a question, answer or summary."""
    statement = str(STATEMENTS[name])
    if "RETURNING" not in statement:
        return
    returning = statement[statement.rindex("RETURNING"):]
    assert not any(column in returning for column in TEXT_COLUMNS), name


def test_no_statement_takes_a_person_id_for_a_read() -> None:
    """A read keyed on a bare person id would let a caller name whose memory."""
    for name in READS:
        assert ":person_id" not in str(STATEMENTS[name]), name
