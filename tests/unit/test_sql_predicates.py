"""Audit the SQL itself for the permission predicate.

A statement that returns content without binding the viewer's channel set is
the defect these tests exist to catch, and it is easier to catch by reading
the SQL than by constructing a database state that exposes it.
"""

from __future__ import annotations

import re

import pytest

from chatmemory.adapters.store import sql

CONTENT_STATEMENTS = {
    "LEXICAL_SEARCH": sql.LEXICAL_SEARCH,
    "VECTOR_SEARCH": sql.VECTOR_SEARCH,
    "THREAD_CONTEXT": sql.THREAD_CONTEXT,
    "LIST_CHANNELS": sql.LIST_CHANNELS,
    "AUTHOR_SEARCH": sql.AUTHOR_SEARCH,
    "PEOPLE_VISIBLE": sql.PEOPLE_VISIBLE,
}


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_content_statements_bind_the_viewers_channels(name: str) -> None:
    # The column differs by statement (channel.id vs message.channel_id);
    # what must hold is that the viewer's set constrains the rows.
    assert "ANY(:channel_ids)" in str(CONTENT_STATEMENTS[name]), (
        f"{name} returns content without constraining it to the viewer's channels"
    )


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_content_statements_exclude_tombstones(name: str) -> None:
    statement = str(CONTENT_STATEMENTS[name])
    if name == "LIST_CHANNELS":
        pytest.skip("channels are not tombstoned; they carry is_indexed")
    assert "deleted_at IS NULL" in statement, f"{name} can return deleted content"


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_the_filter_is_in_the_same_statement_as_the_ranking(name: str) -> None:
    """Filtering after ranking silently under-returns on an approximate scan.

    The predicate must appear before ORDER BY -- i.e. in the WHERE clause of
    the ranked query, not applied to its output.
    """
    statement = str(CONTENT_STATEMENTS[name])
    if "ORDER BY" not in statement:
        pytest.skip("unranked statement")
    where_pos = statement.index("ANY(:channel_ids)")
    order_pos = statement.index("ORDER BY")
    assert where_pos < order_pos, f"{name} filters after ranking"


def test_author_search_puts_author_and_span_beside_the_viewer() -> None:
    """The person filter narrows the viewer's rows; it never replaces them."""
    statement = str(sql.AUTHOR_SEARCH)
    where = statement.index("ANY(:channel_ids)")
    for predicate in ("ANY(:author_ids)", "m.created_at >= ", "m.created_at < "):
        position = statement.index(predicate)
        assert where < position < statement.index("ORDER BY"), predicate


def test_iterative_scan_is_configured() -> None:
    """pgvector defaults it off, which guarantees under-return with a filter."""
    assert any("hnsw.iterative_scan" in s for s in sql.SESSION_SETUP)
    assert any("relaxed_order" in s for s in sql.SESSION_SETUP)


def test_upsert_keys_on_id_and_revision_not_content_hash() -> None:
    """Dedup must use one identity domain.

    Comparing a hash of parsed text against one over raw text puts them in
    different domains: they never match, so every sync reprocesses everything
    while appearing to work.
    """
    statement = str(sql.UPSERT_MESSAGE)
    assert "ON CONFLICT (id)" in statement
    assert "edited_at IS DISTINCT FROM" in statement
    assert not re.search(r"\bhash\b", statement, re.I)


def test_reingest_does_not_resurrect_a_tombstone() -> None:
    assert "deleted_at = message.deleted_at" in str(sql.UPSERT_MESSAGE)


def test_every_content_statement_is_audited() -> None:
    """Guards the audit itself: a new statement must be added here."""
    assert {str(s) for s in sql.statements()} == {
        str(s) for s in CONTENT_STATEMENTS.values()
    }
