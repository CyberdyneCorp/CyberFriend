"""The SQL audit, derived from the SQL instead of from a list kept beside it.

The previous audit checked the statements someone had remembered to add to a
dict, and completeness was asserted against `sql.statements()` -- a second
hand-maintained list. Three content-returning SELECTs were added to `sql`
outside both, and the module docstring was amended to describe them as an
exempt category. Nothing failed. Catching that is the one thing this file
exists to do, so it no longer asks anyone to remember anything:

*   every `TextClause` bound at module level in the store's SQL modules is
    found by reflection, not by enumeration;
*   each one must either bind `:channel_ids` -- the viewer's readable set --
    or appear in `UNSCOPED` below with a written reason;
*   a reason that is empty, or a registration for a statement that no longer
    exists, fails as loudly as an unregistered statement does.

Adding a statement and not doing one of those two things fails this file by
construction, which is what "the audit cannot be bypassed" has to mean. The
registry is verbose on purpose: writing down why a statement may run without a
viewer is the review, and a category exemption is exactly what removed it last
time.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping

import pytest
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

SQL_MODULES = ("sql", "asks_sql", "documents_sql", "retention_sql")

VIEWER_BIND = ":channel_ids"


def discover() -> dict[str, TextClause]:
    """Every module-level statement in the store's SQL modules, by qualified name.

    Reflection rather than a registry: a statement is audited because it
    exists, not because it was listed. Statements re-exported between modules
    are attributed to the module that defines them, so reusing one does not
    create a second entry that has to be registered twice.
    """
    found: dict[str, TextClause] = {}
    seen: set[int] = set()
    for module_name in SQL_MODULES:
        module = importlib.import_module(f"chatmemory.adapters.store.{module_name}")
        for attribute, value in vars(module).items():
            if isinstance(value, TextClause) and id(value) not in seen:
                seen.add(id(value))
                found[f"{module_name}.{attribute}"] = value
    return found


STATEMENTS = discover()

# Statements that run without a viewer, and why each one may. "Why" is the
# point: a reason has to be written by a person, and writing "returns the text
# of other people's conversations to whoever asks" is hard to do by accident.
UNSCOPED: dict[str, str] = {
    # --- sql: writes -----------------------------------------------------
    "sql.UPDATE_PERSON_DISPLAY": "write; keeps a person's shown name current",
    "sql.ENSURE_CHANNEL": (
        "write; registers the channel a message belongs to. Returns no row, "
        "and cannot be viewer-scoped: it runs during ingestion, where there "
        "is no viewer, and the channel it names is the one being ingested"
    ),
    "sql.UPSERT_MESSAGE": "write; stores one message and returns no row",
    "sql.LOCK_MESSAGE": "advisory lock; reads no table",
    "sql.RECORD_TOMBSTONE": "write; the deletion ledger",
    "sql.TOMBSTONE_MESSAGE": "write; withdraws content",
    "sql.TOMBSTONE_WINDOWS_FOR_MESSAGE": "write; withdraws content",
    "sql.PURGE_CHANNEL": "write; removes a channel that left indexing scope",
    "sql.DELETE_WINDOWS_FOR_MESSAGES": "write; supersedes windows during a rebuild",
    "sql.INSERT_WINDOW": "write; returns the new window's id only",
    "sql.INSERT_WINDOW_MESSAGES": "write; membership rows",
    "sql.STORE_EMBEDDING": "write; stores a vector",
    "sql.SET_DISPLAY_NAME": "write; person metadata",
    "sql.MARK_WINDOWS_DIRTY": "write; rebuild watermark",
    "sql.MARK_DIRTY_FOR_MESSAGE": "write; rebuild watermark",
    "sql.CLEAR_WINDOWS_DIRTY": "write; rebuild watermark",
    "sql.DELETE_WINDOWS_FROM": "write; supersedes windows during a rebuild",
    "sql.TOMBSTONE_WINDOWS_WITH_DEAD_MESSAGES": "write; withdraws content",
    # --- sql: reads that are not content ---------------------------------
    "sql.WINDOW_MESSAGE_IDS": (
        "ids only; maps windows a viewer-scoped search already returned to "
        "their message ids"
    ),
    "sql.DIRTY_CHANNELS": "scheduling metadata; a channel id and a watermark",
    "sql.STORED_REVISIONS": (
        "ingest-only; message ids and revision timestamps for reconciliation, "
        "no message text"
    ),
    # --- sql: content returned to the ingestion process ------------------
    #
    # These four return text with no viewer bound. They run for windowing and
    # embedding, which act for nobody: there is no viewer to bind, and
    # rebuilding only the channels some person may read would leave the rest
    # of the corpus permanently unwindowed and therefore permanently
    # unretrievable. None of them is reachable from a request-scoped surface;
    # the entrypoint that drives them serves no requests.
    "sql.MESSAGES_WITHOUT_WINDOW": (
        "ingest-only; message text for the windowing worker, which acts for "
        "nobody. Unreachable from any request-scoped surface"
    ),
    "sql.MESSAGES_FOR_REWINDOW": (
        "ingest-only; message text for the channel rebuild, which acts for "
        "nobody. Unreachable from any request-scoped surface"
    ),
    "sql.WINDOWS_MISSING_EMBEDDINGS": (
        "ingest-only; window text for the embedding worker. Unreachable from "
        "any request-scoped surface"
    ),
    "sql.SUPERSEDED_EMBEDDINGS": (
        "ingest-only; window text keyed to its vector so a rebuild that "
        "changes nothing keeps it. Unreachable from any request-scoped surface"
    ),
    "sql.EMBEDDINGS_FROM": (
        "ingest-only; window text keyed to its vector, as SUPERSEDED_EMBEDDINGS"
    ),
    # --- asks_sql --------------------------------------------------------
    "asks_sql.RESOLVE_PERSON_ID": "identity lookup; a person id, no content",
    "asks_sql.UPSERT_ASK": "write; stores an extracted ask",
    "asks_sql.PRUNE_ASKS": "write; withdraws asks a re-run no longer finds",
    "asks_sql.RECORD_REACTION": "write; an observed reaction",
    "asks_sql.CLOSE_ANSWERED_BY_REPLY": "write; state transition from an event",
    "asks_sql.CLOSE_ANSWERED_BY_REACTION": "write; state transition from an event",
    "asks_sql.MARK_STALE": "write; ageing, which never closes an ask",
    # --- documents_sql ---------------------------------------------------
    "documents_sql.UPSERT_DOCUMENT": "write; returns the document id only",
    "documents_sql.DOCUMENT_CONTENT_HASH": "a hash; lets an unchanged document skip parsing",
    "documents_sql.RECORD_ENTRY": "write; records one act of sharing",
    "documents_sql.REFRESH_CHUNK_CHANNELS": "write; the permission column itself",
    "documents_sql.DELETE_CHUNKS": "write; discards a document's previous chunks",
    "documents_sql.INSERT_CHUNK": "write; stores one chunk",
    "documents_sql.TOMBSTONE_DOCUMENT": "write; withdraws content",
    "documents_sql.TOMBSTONE_DOCUMENT_CHUNKS": "write; withdraws content",
    "documents_sql.TOMBSTONE_ENTRIES_FOR_MESSAGE": "write; returns document ids only",
    "documents_sql.PURGE_CHANNEL_ENTRIES": "write; returns document ids only",
    "documents_sql.PURGE_PERSON_ENTRIES": "write; opt-out, returns document ids only",
    "documents_sql.PURGE_ENTRIES_BEFORE": "write; retention, returns document ids only",
    "documents_sql.DELETE_ORPHANED_DOCUMENTS": "write; removes unshared documents",
    "documents_sql.RECORD_FETCH": "write; the fetch audit trail",
    "documents_sql.FETCH_ATTEMPTS": "a count; bounds retries",
    "documents_sql.STORE_CHUNK_EMBEDDING": "write; stores a vector",
    "documents_sql.CHUNKS_MISSING_EMBEDDINGS": (
        "ingest-only; chunk text for the embedding worker. Unreachable from "
        "any request-scoped surface"
    ),
    "documents_sql.EXTERNAL_DOCUMENTS_DUE": (
        "ingest-only; document metadata for the reconciliation worker, no "
        "chunk text"
    ),
    "documents_sql.EMBEDDING_COST": "aggregate counts and character totals, no content",
    # --- retention_sql ---------------------------------------------------
    "retention_sql.PERSON_BY_PLATFORM_ID": "identity lookup; a person id, no content",
    "retention_sql.CREATE_PERSON": "write; returns the new person id only",
    "retention_sql.LINK_PLATFORM_ID": "write; identity mapping",
    "retention_sql.PURGE_WINDOWS_BEFORE": "write; retention, returns channel ids only",
    "retention_sql.PURGE_MESSAGES_BEFORE": "write; retention",
    "retention_sql.PURGE_ASKS_BEFORE": "write; retention",
    "retention_sql.PURGE_FETCH_LOG_BEFORE": "write; retention",
    "retention_sql.DELETE_EMPTY_WINDOWS": "write; removes windows left with no messages",
    "retention_sql.RECORD_OPT_OUT": "write; records an exclusion",
    "retention_sql.CLEAR_OPT_OUT": "write; removes an exclusion",
    "retention_sql.IS_OPTED_OUT": "existence check on the exclusion list, no content",
    "retention_sql.OPTED_OUT_PEOPLE": (
        "operator report; who has opted out and when, no message or document text"
    ),
    "retention_sql.PURGE_PERSON_WINDOWS": (
        "write; opt-out, returns channel ids and start times only"
    ),
    "retention_sql.PURGE_PERSON_MESSAGES": "write; opt-out",
    "retention_sql.PURGE_PERSON_ASKS": "write; opt-out",
    "retention_sql.PURGE_PERSON_REACTIONS": "write; opt-out",
    "retention_sql.PURGE_PERSON_MENTIONS": "write; opt-out",
}

# Viewer-scoped statements that do not filter tombstones, and why. Kept
# separate from UNSCOPED so an exemption cannot be smuggled in by reusing a
# reason written for something else.
TOMBSTONE_EXEMPT: dict[str, str] = {
    "sql.LIST_CHANNELS": "channels are not tombstoned; they carry is_indexed",
}


def scoped(name: str) -> bool:
    return VIEWER_BIND in str(STATEMENTS[name])


def unaudited(statements: Mapping[str, TextClause], registry: Mapping[str, str]) -> list[str]:
    """Statements that neither bind the viewer nor carry a written reason."""
    return sorted(
        name
        for name, statement in statements.items()
        if VIEWER_BIND not in str(statement) and not registry.get(name, "").strip()
    )


def test_the_modules_were_actually_scanned() -> None:
    """A reflection bug that finds nothing would make every test below pass."""
    assert len(STATEMENTS) > 50
    for module_name in SQL_MODULES:
        assert any(name.startswith(f"{module_name}.") for name in STATEMENTS)


def test_every_statement_binds_the_viewer_or_says_why_it_does_not() -> None:
    missing = unaudited(STATEMENTS, UNSCOPED)
    assert missing == [], (
        "these statements neither bind the viewer's channel set nor carry a "
        "reason in UNSCOPED: " + ", ".join(missing)
    )


def test_the_audit_fails_on_an_unregistered_content_statement() -> None:
    """The regression itself: a new content SELECT added outside the registry.

    The previous audit could not fail this way, because a statement it had not
    been told about was a statement it did not look at.
    """
    added = {
        **STATEMENTS,
        "sql.RECENT_MESSAGES": text("SELECT content FROM message ORDER BY created_at"),
    }
    assert unaudited(added, UNSCOPED) == ["sql.RECENT_MESSAGES"]


def test_a_blank_reason_does_not_count_as_a_registration() -> None:
    """Otherwise the registry becomes a list of names, which is what it replaced."""
    assert unaudited(STATEMENTS, {**UNSCOPED, "sql.UPSERT_MESSAGE": "   "}) == [
        "sql.UPSERT_MESSAGE"
    ]


@pytest.mark.parametrize("name", sorted(UNSCOPED))
def test_no_stale_registrations(name: str) -> None:
    """A reason left behind for a deleted statement is a reason nobody re-read."""
    assert name in STATEMENTS, f"{name} is registered in UNSCOPED but no longer exists"


@pytest.mark.parametrize("name", sorted(TOMBSTONE_EXEMPT))
def test_no_stale_tombstone_exemptions(name: str) -> None:
    assert name in STATEMENTS, f"{name} is exempt from the tombstone check but does not exist"


def test_a_statement_is_never_both_scoped_and_registered() -> None:
    """A registration on a scoped statement is a reason that stopped being true."""
    contradictions = sorted(n for n in UNSCOPED if n in STATEMENTS and scoped(n))
    assert contradictions == []


@pytest.mark.parametrize("name", sorted(n for n in STATEMENTS if scoped(n)))
def test_the_filter_is_in_the_same_statement_as_the_ranking(name: str) -> None:
    """Filtering after ranking silently under-returns on an approximate scan.

    Worst for the people in the fewest channels, which is the opposite of who
    a permission system should fail.
    """
    statement = str(STATEMENTS[name])
    if "ORDER BY" not in statement:
        pytest.skip("unranked statement")
    assert statement.index(VIEWER_BIND) < statement.index("ORDER BY"), (
        f"{name} binds the viewer's channels after its ranking"
    )


@pytest.mark.parametrize("name", sorted(n for n in STATEMENTS if scoped(n)))
def test_viewer_scoped_statements_exclude_tombstones(name: str) -> None:
    if name in TOMBSTONE_EXEMPT:
        pytest.skip(TOMBSTONE_EXEMPT[name])
    assert "deleted_at IS NULL" in str(STATEMENTS[name]), (
        f"{name} can return withdrawn content"
    )
