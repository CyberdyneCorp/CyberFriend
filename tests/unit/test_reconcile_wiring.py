"""Reconciliation must be wired, not offered.

The loop, the comparison and the repairs were all implemented and tested, and
none of it ever ran. The entrypoint probed the store for `stored_revisions`,
no store had it, one line went to the log at boot and the process served for
good without reconciling -- so every edit and deletion that happened during a
deploy stayed unrepaired, and a message deleted while the process was down
remained retrievable indefinitely, with every health check green.

That is the same shape as a process that silently does not embed, which this
codebase already refuses to allow. These tests assert the refusal: the method
is part of the port, the store implements it, and the job starts
unconditionally.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import chatmemory
from chatmemory.adapters.discord.source import RevisionLedger
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.ports.store import Store

ENTRYPOINT = Path(chatmemory.__file__).parent / "entrypoints" / "ingest.py"
CONDITIONAL = (ast.If, ast.Try, ast.While)


def _source() -> ast.Module:
    return ast.parse(ENTRYPOINT.read_text())


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _parameters(function: object) -> tuple[str, ...]:
    return tuple(inspect.signature(function).parameters)  # type: ignore[arg-type]


def test_the_store_port_declares_the_revision_ledger() -> None:
    """Declared, so the type checker proves it at the wiring site.

    A runtime probe can only discover the absence and carry on; the port makes
    a store that cannot reconcile fail to type-check instead.
    """
    assert callable(getattr(Store, "stored_revisions", None))
    assert _parameters(Store.stored_revisions) == ("self", "channel", "since")


def test_the_postgres_store_is_a_revision_ledger() -> None:
    assert callable(getattr(PostgresStore, "stored_revisions", None))
    assert _parameters(PostgresStore.stored_revisions) == _parameters(
        RevisionLedger.stored_revisions
    )


def test_the_entrypoint_no_longer_probes_for_the_ledger() -> None:
    source = ENTRYPOINT.read_text()
    for probe in ("_supports", '"stored_revisions"', "reconcile_unavailable"):
        assert probe not in source, (
            "reconciliation must not depend on a runtime probe: the probe that "
            "fails is indistinguishable from the feature that is off"
        )


@pytest.mark.parametrize("job", ["Reconciler", "reconcile_loop"])
def test_reconciliation_starts_unconditionally(job: str) -> None:
    """Structural, because the failure mode is a process that looks healthy.

    Nothing in `main` may guard either call: a branch here is how the loop
    came to be skipped in every deployment for the life of the feature.
    """
    started = [
        node
        for node in ast.walk(_source())
        if isinstance(node, ast.Expr | ast.Assign) and job in _called_names(node)
    ]
    assert started, f"{job} is never constructed; reconciliation cannot run"

    guarded = [
        node
        for node in ast.walk(_source())
        if isinstance(node, CONDITIONAL) and job in _called_names(node)
    ]
    assert not guarded, f"{job} is reached only conditionally"
