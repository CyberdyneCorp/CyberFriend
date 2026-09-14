"""Audit the ask SQL itself.

The same audit the corpus statements get, for the same reason: a statement that
returns asks without binding the viewer's channel set is the defect these tests
exist to catch, and it is easier to catch by reading the SQL than by building
the database state that would expose it.
"""

from __future__ import annotations

import pytest

from chatmemory.adapters.store import asks_sql

CONTENT_STATEMENTS = {
    "OBLIGATIONS": asks_sql.OBLIGATIONS,
    "COUNT_OBLIGATIONS": asks_sql.COUNT_OBLIGATIONS,
    "ASK_EXISTS": asks_sql.ASK_EXISTS,
    "APPLY_CORRECTION": asks_sql.APPLY_CORRECTION,
}


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_statements_bind_the_viewers_channels(name: str) -> None:
    assert "ANY(:channel_ids)" in str(CONTENT_STATEMENTS[name]), (
        f"{name} reaches asks without constraining them to the viewer's channels"
    )


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_statements_drop_asks_whose_source_was_deleted(name: str) -> None:
    """Deleted content must stop being returned everywhere, immediately."""
    assert "m.deleted_at IS NULL" in str(CONTENT_STATEMENTS[name]), (
        f"{name} can report an ask extracted from a deleted message"
    )


@pytest.mark.parametrize("name", sorted(CONTENT_STATEMENTS))
def test_the_filter_is_in_the_same_statement_as_the_ranking(name: str) -> None:
    statement = str(CONTENT_STATEMENTS[name])
    if "ORDER BY" not in statement:
        pytest.skip("unranked statement")
    assert statement.index("ANY(:channel_ids)") < statement.index("ORDER BY"), (
        f"{name} filters after ranking"
    )


def test_the_count_uses_the_same_predicate_as_the_read() -> None:
    """Counting unreadable asks discloses them as surely as quoting them does."""
    shared = asks_sql._OBLIGATION_PREDICATE
    assert shared in str(asks_sql.OBLIGATIONS)
    assert shared in str(asks_sql.COUNT_OBLIGATIONS)


def test_reads_exclude_corrected_asks() -> None:
    assert "c.ask_key IS NULL" in str(asks_sql.OBLIGATIONS)


def test_a_correction_requires_the_corrector_to_be_the_addressee() -> None:
    """A predicate, not a step a caller performs, so it cannot be skipped."""
    statement = str(asks_sql.APPLY_CORRECTION)
    assert "a.addressee_person_id = :person_id" in statement
    assert "a.addressee_kind = 'person'" in statement


def test_reextraction_never_resets_observed_state() -> None:
    statement = str(asks_sql.UPSERT_ASK)
    assert "ON CONFLICT (ask_key) DO UPDATE" in statement
    update = statement.split("DO UPDATE")[1]
    for column in ("status", "closed_at", "closed_by"):
        assert f"{column} =" not in update, (
            f"re-extraction overwrites {column}, which is an observed event"
        )


def test_pruning_keeps_corrected_asks() -> None:
    """A dismissed ask must not be deleted and recreated as a fresh obligation."""
    assert "NOT EXISTS (SELECT 1 FROM ask_correction" in str(asks_sql.PRUNE_ASKS)


def test_ageing_marks_stale_and_never_answers() -> None:
    statement = str(asks_sql.MARK_STALE)
    assert "status = 'stale'" in statement
    assert "answered" not in statement


def test_state_transitions_are_driven_by_events() -> None:
    """Both closing statements name a row that exists, not a judgement."""
    assert "FROM message r" in str(asks_sql.CLOSE_ANSWERED_BY_REPLY)
    assert "FROM ask_reaction x" in str(asks_sql.CLOSE_ANSWERED_BY_REACTION)


def test_a_reply_only_closes_in_thread_or_as_a_direct_reply() -> None:
    statement = str(asks_sql.CLOSE_ANSWERED_BY_REPLY)
    assert "r.reply_to_id = a.source_message_id" in statement
    assert "r.thread_id = a.thread_id" in statement


def test_only_the_addressee_closes_an_ask() -> None:
    for statement in (
        asks_sql.CLOSE_ANSWERED_BY_REPLY,
        asks_sql.CLOSE_ANSWERED_BY_REACTION,
    ):
        assert "a.addressee_kind = 'person'" in str(statement)


def test_nullable_parameters_are_cast() -> None:
    """asyncpg cannot infer the type of a NULL bind; the cast is not optional."""
    statement = str(asks_sql.OBLIGATIONS)
    assert "CAST(:since AS timestamptz)" in statement
    assert "CAST(:until AS timestamptz)" in statement
    assert "CAST(:thread_id AS bigint)" in str(asks_sql.UPSERT_ASK)


def test_every_content_statement_is_audited() -> None:
    """Guards the audit: a new read statement has to be added here."""
    assert {str(s) for s in asks_sql.statements()} == {
        str(s) for s in CONTENT_STATEMENTS.values()
    }
