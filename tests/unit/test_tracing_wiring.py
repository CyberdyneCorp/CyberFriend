"""Tracing, reached from the two processes that actually run.

This project's recurring failure is a finished module wired to nothing, and
tracing is a feature where that failure is silent in both directions: nothing
exported looks exactly like a quiet week, and nothing withdrawn looks exactly
like nothing having been deleted.

Two chains, because the halves share no memory:

  bot     build_answer_stack -> build_tracer -> OptOutAwareTracer over a
          LangfuseTracer, handed to TracedAnswerService, the outermost
          answer service, which calls it for every answer the chain gives

  ingest  main -> IngestService(traces=build_trace_withdrawal(...)) so a
          tombstone reaches the trace store, and main -> trace_withdrawal_loop
          so a deletion that failed is retried rather than lost

The `trace_export` tables are the seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import (
    LangfuseTraceDeleter,
    LangfuseTraceFinder,
    LangfuseTracer,
)
from chatmemory.app.reasoning.tracing import OptOutAwareTracer, TraceWithdrawal
from chatmemory.composition import build_trace_withdrawal, build_tracer
from chatmemory.config import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
INGEST = SRC / "entrypoints" / "ingest.py"
COMPOSITION = SRC / "composition.py"
TRACING = SRC / "app" / "reasoning" / "tracing.py"

BASE = {
    "discord_token": "x",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}
CONFIGURED = BASE | {
    "tracing_enabled": True,
    "langfuse_host": "https://langfuse.example",
    "langfuse_public_key": "pk",
    "langfuse_secret_key": "sk",
}


def _function(path: Path, name: str) -> ast.AST:
    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str, keyword: str | None = None) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
        and (keyword is None or any(k.arg == keyword for k in node.keywords))
    ]


# --- the bot half: runs are exported ------------------------------------


def test_the_answer_stack_builds_a_tracer_and_hands_it_to_the_seam() -> None:
    """Built but not passed is the failure mode this whole file exists for."""
    stack = _function(COMPOSITION, "build_answer_stack")
    assert _calls(stack, "build_tracer"), "build_answer_stack must build a tracer"
    [seam] = _calls(stack, "TracedAnswerService")
    assert any(
        getattr(arg, "id", None) == "tracer" for arg in ast.walk(seam)
    ), "the tracer must reach TracedAnswerService, not just be constructed"
    assert not _calls(stack, "build_answers", keyword="tracer"), (
        "a second seam inside reasoning would export its runs twice"
    )


def test_the_seam_calls_the_tracer_on_every_answer() -> None:
    answered = _function(TRACING, "answer_run")
    assert _calls(answered, "export_run"), (
        "answer_run of the outermost service is the one point every chain "
        "answer passes through; the trace goes here"
    )
    assert _calls(_function(TRACING, "export_run"), "trace"), (
        "export_run is what hands the run to the tracer"
    )


def test_the_routes_answered_before_the_chain_are_exported_too() -> None:
    """Catch-up and said-by never reach the chain's seam; the ask service
    exports their runs through the same helper."""
    produce = _function(SRC / "app" / "ask.py", "_produce")
    assert _calls(produce, "export_run"), (
        "AskService._produce must export the catch-up / said-by run it answers"
    )


def test_build_tracer_is_off_by_default() -> None:
    assert build_tracer(Settings(**BASE), object()) is None  # type: ignore[arg-type]


def test_build_tracer_returns_the_real_adapters_when_configured() -> None:
    tracer = build_tracer(Settings(**CONFIGURED), object())  # type: ignore[arg-type]
    assert isinstance(tracer, OptOutAwareTracer)
    assert isinstance(tracer._inner, LangfuseTracer)  # noqa: SLF001
    assert isinstance(tracer._inner._index, PostgresTraceIndex)  # noqa: SLF001


def test_half_configured_tracing_is_refused_rather_than_silently_off() -> None:
    """An operator who believes they are recording and is not finds out on the
    day somebody asks what went wrong. Better to refuse the deployment."""
    half = BASE | {"tracing_enabled": True, "langfuse_host": "https://langfuse.example"}
    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        build_tracer(Settings(**half), object())  # type: ignore[arg-type]


# --- the ingest half: deletions are withdrawn ---------------------------


def test_ingest_gives_the_service_a_trace_sink() -> None:
    """Without this a deleted message stays legible in every trace quoting it."""
    main = _function(INGEST, "main")
    built = _calls(main, "IngestService", keyword="traces")
    assert built, "IngestService must be given traces=, or deletion stops at the corpus"
    assert _calls(built[0], "build_trace_withdrawal")


def test_the_deletion_path_calls_the_trace_sink() -> None:
    handler = _function(SRC / "app" / "ingest.py", "handle_delete")
    assert _calls(handler, "withdraw_message"), (
        "handle_delete is the single funnel for a tombstone; the trace goes with it"
    )


def test_ingest_starts_the_retry_sweep() -> None:
    main = _function(INGEST, "main")
    started = [
        call
        for call in _calls(main, "create_task")
        if _calls(call, "trace_withdrawal_loop")
    ]
    assert started, "a deletion the destination refused must be retried by something"


def test_build_trace_withdrawal_is_off_by_default() -> None:
    assert build_trace_withdrawal(Settings(**BASE), object()) is None  # type: ignore[arg-type]


def test_build_trace_withdrawal_returns_the_real_adapters_when_configured() -> None:
    withdrawal = build_trace_withdrawal(Settings(**CONFIGURED), object())  # type: ignore[arg-type]
    assert isinstance(withdrawal, TraceWithdrawal)
    assert isinstance(withdrawal._index, PostgresTraceIndex)  # noqa: SLF001
    assert isinstance(withdrawal._deleter, LangfuseTraceDeleter)  # noqa: SLF001


def test_build_trace_withdrawal_deletes_over_the_process_transport() -> None:
    """Regression: the deleter opened its own client over the real network,
    so a deletion never went through `Edges.http_transport`."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    withdrawal = build_trace_withdrawal(
        Settings(**CONFIGURED), object(), transport  # type: ignore[arg-type]
    )
    assert withdrawal is not None
    assert withdrawal._deleter._transport is transport  # type: ignore[attr-defined]  # noqa: SLF001


def test_build_trace_withdrawal_searches_langfuse_in_our_environment() -> None:
    """The backstop for traces exported before the asker was recorded: an
    opt-out queues a search, and only a withdrawal with a finder runs it."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    withdrawal = build_trace_withdrawal(
        Settings(**CONFIGURED, langfuse_environment="staging"),
        object(),  # type: ignore[arg-type]
        transport,
    )
    assert withdrawal is not None
    finder = withdrawal._finder  # noqa: SLF001
    assert isinstance(finder, LangfuseTraceFinder)
    assert finder._environment == "staging"  # noqa: SLF001
    assert finder._transport is transport  # noqa: SLF001


def test_the_tracer_exports_to_the_configured_environment() -> None:
    tracer = build_tracer(
        Settings(**CONFIGURED, langfuse_environment="staging"), object()  # type: ignore[arg-type]
    )
    assert tracer is not None
    assert tracer._inner._environment == "staging"  # type: ignore[attr-defined]  # noqa: SLF001
