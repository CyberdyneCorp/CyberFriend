"""The console's own SQL audit: counts and timings, and nothing else.

`test_sql_audit.py` reflects over `adapters/store/*` and asks of each statement
"is this scoped to the viewer's readable channels, or is there a written reason
it need not be". The console's statements are not in those modules and cannot
answer that question: they run for an operator, and an operator is not a viewer
of the corpus -- there is no reading of it that would be correct for them.

So they answer a different question, and this file asks it: **does this
statement return content at all?** A count cannot disclose who said what, which
is why the console is allowed to run one without a permission predicate, and
which is exactly why a statement here that returned a row of text would be a
second path into private channels with no predicate anywhere near it.

Like the wider audit, the registry below is verbose on purpose. Writing down
what a statement returns is the review.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest
from sqlalchemy.sql.elements import TextClause

import chatmemory.admin.handlers as handlers_package
from chatmemory.admin.handlers import queries

HANDLERS = Path(handlers_package.__file__).parent

#: Every module-level statement in `queries`, by name, with what it returns.
#: A statement that is not here fails the completeness test below, and so does
#: a registration for one that no longer exists.
RETURNS: dict[str, str] = {
    "STATUS_COUNTS": (
        "counts and one max(created_at); no row of it is content, and the "
        "timestamp is what catches a gateway that is connected and ingesting "
        "nothing"
    ),
    "CHANNEL_EVIDENCE": (
        "one row per channel: its id, the platform's name for it, whether it is "
        "indexed, how many messages are stored and when the last one arrived. "
        "The name is what an operator recognises a channel by; without it the "
        "console lists bare snowflakes and somebody removes the wrong one"
    ),
    "OPTED_OUT_PEOPLE": (
        "borrowed from retention_sql, where the wider audit already covers it: "
        "who has withdrawn and when. It also selects the reason an operator "
        "typed, which the adapter drops -- the console asks for a list of who "
        "has opted out, not for a file on them"
    ),
}

#: Column names that would make a statement a disclosure. Checked as
#: substrings of the SQL, so a join that reached one fails too.
CONTENT_COLUMNS = ("content", "text", "body", "excerpt", "question", "answer", "chunk")

WRITES = ("insert ", "update ", "delete ", "truncate ", "drop ", "alter ")


def discover() -> dict[str, TextClause]:
    return {
        name: value
        for name, value in vars(queries).items()
        if isinstance(value, TextClause)
    }


STATEMENTS = discover()


def test_every_statement_is_registered() -> None:
    assert set(STATEMENTS) == set(RETURNS), (
        "a statement in the console's query module is not described in this "
        "file, or a description names one that no longer exists"
    )


@pytest.mark.parametrize("name", sorted(RETURNS))
def test_every_registration_says_something(name: str) -> None:
    assert len(RETURNS[name].split()) >= 8, f"{name}: write what it returns"


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_no_statement_selects_a_content_column(name: str) -> None:
    sql = str(STATEMENTS[name]).lower()
    found = [column for column in CONTENT_COLUMNS if column in sql]
    assert not found, (
        f"{name} names {found}; the console reports counts and timings, and a "
        "statement that returns corpus content makes it a second way into "
        "private channels"
    )


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_every_statement_is_read_only(name: str) -> None:
    """The console changes configuration, never the corpus.

    Opting somebody out is the one exception, and it goes through the opt-out
    service -- whose ordered deletes are audited where they live.
    """
    sql = str(STATEMENTS[name]).lower()
    assert not any(verb in sql for verb in WRITES), f"{name} writes"


def _module_paths() -> list[Path]:
    return [
        Path(info.module_finder.path) / f"{info.name}.py"  # type: ignore[union-attr]
        for info in pkgutil.iter_modules([str(HANDLERS)])
    ]


@pytest.mark.parametrize("path", _module_paths(), ids=lambda p: p.name)
def test_only_the_query_module_speaks_sql(path: Path) -> None:
    """One module reaches the corpus, and it is the one that is audited here.

    A handler that grew its own SELECT would be outside every audit in the
    repository: not in `adapters/store`, so the viewer-scope audit never sees
    it, and not in `queries`, so this one never sees it either.
    """
    if path.name == "queries.py":
        return
    tree = ast.parse(path.read_text())
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "sqlalchemy" not in imported, f"{path.name} reaches the database directly"


def test_the_query_module_is_reachable_from_the_running_process() -> None:
    """The adapters exist *and* the entrypoint builds them.

    This project's recurring defect is a module that is finished, tested and
    connected to nothing. Asserting the import here is cheap; the wiring test
    next door asserts the service actually holds these objects.
    """
    entrypoint = importlib.import_module("chatmemory.entrypoints.admin")
    source = Path(entrypoint.__file__ or "").read_text()
    for adapter in (
        "PostgresCorpusStatus",
        "PostgresChannelDirectory",
        "PostgresOptOutDirectory",
    ):
        assert adapter in source
