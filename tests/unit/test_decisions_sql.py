"""Audit the decision read SQL itself.

The same audit the ask statements get: a statement that returns decisions
without binding the viewer's channel set, or that can report a decision whose
source or evidence was deleted, is the defect these tests exist to catch, and
reading the SQL catches it without building the state that would expose it.
"""

from __future__ import annotations

import pytest

from chatmemory.adapters.store import decisions_sql

CONTENT_STATEMENTS = {"SEARCH_DECISIONS": decisions_sql.SEARCH_DECISIONS}


def test_every_content_statement_is_audited() -> None:
    """Guards the audit: a new read statement has to be added here."""
    assert {str(s) for s in decisions_sql.statements()} == {
        str(s) for s in CONTENT_STATEMENTS.values()
    }


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_statements_bind_the_viewers_channels_before_ranking(name: str) -> None:
    statement = str(CONTENT_STATEMENTS[name])
    assert "d.channel_id = ANY(:channel_ids)" in statement
    assert statement.index("ANY(:channel_ids)") < statement.index("ORDER BY"), (
        f"{name} filters after ranking"
    )


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_statements_drop_decisions_whose_source_was_deleted(name: str) -> None:
    assert "m.deleted_at IS NULL" in str(CONTENT_STATEMENTS[name])


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_statements_require_every_evidence_message_alive_and_readable(name: str) -> None:
    """The summary may restate any of them, so one missing is enough to withhold it."""
    statement = str(CONTENT_STATEMENTS[name])
    assert "unnest(d.evidence_message_ids)" in statement
    assert "em.id IS NULL" in statement
    assert "em.deleted_at IS NOT NULL" in statement
    assert "NOT em.channel_id = ANY(:channel_ids)" in statement


def test_the_ranking_is_the_documented_blend() -> None:
    statement = str(decisions_sql.SEARCH_DECISIONS)
    assert "0.7 * COALESCE(1 - (d.embedding <=> CAST(:embedding AS vector)), 0)" in statement
    assert "0.3 * ts_rank(d.search_tsv, plainto_tsquery('simple', :terms))" in statement
    assert "confidence >= :min_confidence" in statement
    assert "similarity >= :min_similarity" in statement


def test_nullable_parameters_are_cast() -> None:
    """asyncpg cannot infer the type of a NULL bind; the cast is not optional."""
    statement = str(decisions_sql.SEARCH_DECISIONS)
    for bind in ("CAST(:since AS timestamptz)", "CAST(:until AS timestamptz)",
                 "CAST(:embedding AS vector)", "CAST(:has_topic AS boolean)"):
        assert bind in statement
