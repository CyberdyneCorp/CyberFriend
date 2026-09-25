"""Console credentials and the change record, against a live Postgres.

The unit tests assert the shape. These assert that the shape survives contact
with the schema migration 0012 actually creates -- including the two things
that cannot be tested in memory at all: that the database itself refuses to
rewrite the change record, and that the operator CLI, run as a real process,
reaches both tables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import chatmemory
from chatmemory.adapters.store.admin_postgres import (
    PostgresChangeRecord,
    PostgresOperatorTokens,
    grant_console_access,
    withdraw_console_access,
)
from chatmemory.admin.access import Access, RouteAccess
from chatmemory.admin.audit import ChangeKind, applied, escalation, refused
from chatmemory.admin.auth import (
    AdminAuthenticator,
    AdminAuthMiddleware,
    Operator,
    current_operator,
    generate_admin_token,
)
from tests.integration.conftest import DB_URL

ANA = Operator("ana")
BEN = Operator("ben")
CASS = Operator("cass")


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(text(f"SELECT to_regclass('public.{table}')"))
        return row.scalar() is not None


@pytest.fixture(autouse=True)
async def _require_the_admin_schema(clean: AsyncEngine) -> None:
    """Fail, rather than skip, when migration 0012 has not been applied.

    A skipped test is not a passing test. "No admin_token table" means the
    database is behind `alembic upgrade head` -- an environment problem an
    operator has to see, not a reason for this file to report green.
    """
    for table in ("admin_token", "config_audit"):
        assert await _table_exists(clean, table), (
            f"{table} is missing; run `alembic upgrade head` against {DB_URL}"
        )


# --- credentials --------------------------------------------------------


async def test_a_credential_resolves_to_the_operator_it_was_issued_to(
    clean: AsyncEngine,
) -> None:
    tokens = PostgresOperatorTokens(clean)
    issued = await tokens.issue(ANA, "laptop")
    assert await tokens.operator_for_token(issued.token) == ANA


async def test_the_plaintext_credential_is_never_stored(clean: AsyncEngine) -> None:
    tokens = PostgresOperatorTokens(clean)
    issued = await tokens.issue(ANA, "laptop")
    async with clean.connect() as conn:
        rows = await conn.execute(text("SELECT token_hash FROM admin_token"))
        stored = [str(r[0]) for r in rows]
    assert stored == [issued.token_hash]
    assert issued.token not in stored


async def test_an_unknown_and_a_revoked_credential_are_both_simply_nothing(
    clean: AsyncEngine,
) -> None:
    tokens = PostgresOperatorTokens(clean)
    revoked = await tokens.issue(ANA)
    await tokens.revoke(ANA)
    assert await tokens.operator_for_token(revoked.token) is None
    assert await tokens.operator_for_token(generate_admin_token()) is None


async def test_revoking_one_operator_leaves_every_other_credential_working(
    clean: AsyncEngine,
) -> None:
    """The property that decides whether revocation happens when it should."""
    tokens = PostgresOperatorTokens(clean)
    ana = await tokens.issue(ANA, "laptop")
    ben = await tokens.issue(BEN, "laptop")
    cass = await tokens.issue(CASS, "phone")
    cass_desktop = await tokens.issue(CASS, "desktop")

    assert await tokens.revoke(ANA) == 1

    assert await tokens.operator_for_token(ana.token) is None
    assert await tokens.operator_for_token(ben.token) == BEN
    assert await tokens.operator_for_token(cass.token) == CASS
    assert await tokens.operator_for_token(cass_desktop.token) == CASS


async def test_revocation_keeps_the_withdrawn_credential_on_record(
    clean: AsyncEngine,
) -> None:
    """'Was this ever valid, and when did it stop' has to stay answerable."""
    tokens = PostgresOperatorTokens(clean)
    await tokens.issue(ANA)
    await tokens.revoke(ANA)
    async with clean.connect() as conn:
        rows = await conn.execute(
            text("SELECT operator, revoked_at FROM admin_token")
        )
        found = [(str(r[0]), r[1]) for r in rows]
    assert len(found) == 1
    assert found[0][0] == "ana"
    assert found[0][1] is not None
    assert await tokens.active_tokens() == ()


# --- the change record --------------------------------------------------


async def test_a_change_round_trips_with_both_values(clean: AsyncEngine) -> None:
    record = PostgresChangeRecord(clean)
    written = await record.record(applied(ANA, "retention_days", "30", "90"))
    [read] = await record.recent(1)
    assert read == written
    assert (read.operator, read.setting, read.before, read.after) == (
        "ana",
        "retention_days",
        "30",
        "90",
    )


async def test_a_refusal_is_recorded_alongside_what_was_applied(
    clean: AsyncEngine,
) -> None:
    record = PostgresChangeRecord(clean)
    await record.record(applied(ANA, "retention_days", "30", "90"))
    await record.record(
        refused(BEN, "allowed_tools", "no server offers it", attempted="github:merge_pr")
    )
    kinds = [e.kind for e in await record.recent()]
    assert kinds == [ChangeKind.REFUSED, ChangeKind.APPLIED]


async def test_an_escalation_is_distinguishable_from_an_ordinary_edit(
    clean: AsyncEngine,
) -> None:
    record = PostgresChangeRecord(clean)
    await record.record(applied(ANA, "retention_days", "30", "90"))
    await record.record(
        escalation(ANA, "allowed_tools", "", "github:merge_pr", "modifies state")
    )
    escalations = [e for e in await record.recent() if e.kind is ChangeKind.ESCALATION]
    assert [e.after for e in escalations] == ["github:merge_pr"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE config_audit SET operator = 'someone_else'",
        "UPDATE config_audit SET after_value = 'never happened'",
        "DELETE FROM config_audit",
    ],
)
async def test_the_database_refuses_to_rewrite_the_record(
    clean: AsyncEngine, statement: str
) -> None:
    """The port binds callers that went through it. This binds a psql prompt.

    The console offers no way to alter or remove an entry, and neither does
    anything else with these credentials -- which is what makes the record
    worth reading after the incident it is read for.
    """
    record = PostgresChangeRecord(clean)
    await record.record(applied(ANA, "retention_days", "30", "90"))

    with pytest.raises(SQLAlchemyError) as raised:
        async with clean.begin() as conn:
            await conn.execute(text(statement))
    assert "append-only" in str(raised.value)

    [survivor] = await record.recent()
    assert (survivor.operator, survivor.after) == ("ana", "90")


# --- grant and withdrawal are recorded, atomically ----------------------


async def test_granting_console_access_records_it_as_an_escalation(
    clean: AsyncEngine,
) -> None:
    issued = await grant_console_access(clean, ANA, "laptop")
    tokens = PostgresOperatorTokens(clean)
    assert await tokens.operator_for_token(issued.token) == ANA

    [entry] = await PostgresChangeRecord(clean).recent()
    assert entry.kind is ChangeKind.ESCALATION
    assert entry.setting == "console_access:ana"
    assert entry.before == "0 live credential(s)"
    assert entry.after == "1 live credential(s)"
    # Attributed to the shell, because the first credential is necessarily
    # issued by somebody who holds none.
    assert entry.operator == "cli:shell"
    assert issued.token not in str(entry)


async def test_withdrawing_console_access_is_recorded_as_a_narrowing(
    clean: AsyncEngine,
) -> None:
    await grant_console_access(clean, ANA, "laptop")
    await grant_console_access(clean, ANA, "desktop")
    assert await withdraw_console_access(clean, ANA) == 2

    latest = (await PostgresChangeRecord(clean).recent())[0]
    assert latest.kind is ChangeKind.APPLIED
    assert (latest.before, latest.after) == ("2 live credential(s)", "0 live credential(s)")


async def test_a_grant_that_cannot_be_recorded_issues_no_credential(
    clean: AsyncEngine,
) -> None:
    """The credential and the record of who granted it are one transaction.

    Issued in one and recorded in another, a crash between them leaves a
    working console credential that nothing accounts for -- which is exactly
    what the record exists to make impossible.
    """
    async with clean.begin() as conn:
        await conn.execute(text("ALTER TABLE config_audit ADD CONSTRAINT tmp_block CHECK (false)"))
    try:
        with pytest.raises(SQLAlchemyError):
            await grant_console_access(clean, ANA, "laptop")
    finally:
        async with clean.begin() as conn:
            await conn.execute(text("ALTER TABLE config_audit DROP CONSTRAINT tmp_block"))

    assert await PostgresOperatorTokens(clean).active_tokens() == ()


# --- the middleware, against the live store -----------------------------


def _console(engine: AsyncEngine) -> Starlette:
    async def whoami(_: Request) -> JSONResponse:
        return JSONResponse({"operator": current_operator().name})

    app = Starlette(routes=[Route("/api/whoami", whoami, methods=["GET", "POST"])])
    table = {("GET", "/api/whoami"): Access.OPERATOR, ("POST", "/api/whoami"): Access.ADMIN}
    app.add_middleware(
        AdminAuthMiddleware,
        authenticator=AdminAuthenticator(PostgresOperatorTokens(engine)),
        access=RouteAccess(table, app.router.routes),
    )
    return app


def _client(engine: AsyncEngine) -> httpx2.AsyncClient:
    """An in-process client on this test's event loop.

    Starlette's TestClient drives the app from its own loop, which the
    asyncpg connections here do not belong to; the transport keeps both on
    one loop, as tests/integration/test_mcp_server.py does.
    """
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=_console(engine)),
        base_url="http://console.test",
    )


async def test_the_console_acts_as_the_operator_the_credential_names(
    clean: AsyncEngine,
) -> None:
    issued = await grant_console_access(clean, ANA, "laptop")
    async with _client(clean) as client:
        response = await client.post(
            "/api/whoami?operator=ben",
            headers={"Authorization": f"Bearer {issued.token}", "X-Operator": "ben"},
            json={"operator": "ben"},
        )
    assert response.json() == {"operator": "ana"}


async def test_a_withdrawn_credential_stops_working_and_the_others_do_not(
    clean: AsyncEngine,
) -> None:
    ana = await grant_console_access(clean, ANA, "laptop")
    ben = await grant_console_access(clean, BEN, "laptop")
    await withdraw_console_access(clean, ANA)

    async with _client(clean) as client:
        refusal = await client.get(
            "/api/whoami", headers={"Authorization": f"Bearer {ana.token}"}
        )
        allowed = await client.get(
            "/api/whoami", headers={"Authorization": f"Bearer {ben.token}"}
        )
        unknown = await client.get(
            "/api/whoami",
            headers={"Authorization": f"Bearer {generate_admin_token()}"},
        )
    assert refusal.status_code == 401
    assert allowed.json() == {"operator": "ben"}
    # The withdrawn credential and a credential that never existed are the
    # same answer, byte for byte.
    assert (refusal.status_code, refusal.content) == (
        unknown.status_code,
        unknown.content,
    )


# --- the operator CLI, as a real process --------------------------------


def _cli(*args: str) -> str:
    """Run the operator CLI against the test database, as an operator would."""
    environment = {
        **os.environ,
        "DATABASE_URL": DB_URL,
        "DISCORD_TOKEN": "unused-by-the-console",
        "DISCORD_GUILD_ID": "1",
        "LLM_API_KEY": "unused-by-the-console",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "chatmemory.admin.issue_token", *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(Path(chatmemory.__file__).parent.parent.parent),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


async def test_the_cli_issues_a_credential_that_authenticates_the_console(
    clean: AsyncEngine,
) -> None:
    """The whole chain: CLI process -> admin_postgres -> admin_sql -> 0012's tables."""
    printed = _cli("issue", "ana", "--label", "laptop").split()
    assert printed[0] == "ana"
    token = printed[1]

    async with _client(clean) as client:
        response = await client.get(
            "/api/whoami", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.json() == {"operator": "ana"}


async def test_the_cli_revokes_one_operator_and_records_both_acts(
    clean: AsyncEngine,
) -> None:
    ana_token = _cli("issue", "ana", "--label", "laptop").split()[1]
    ben_token = _cli("issue", "ben").split()[1]
    assert "revoked 1 credential(s) for ana" in _cli("revoke", "ana")

    tokens = PostgresOperatorTokens(clean)
    assert await tokens.operator_for_token(ana_token) is None
    assert await tokens.operator_for_token(ben_token) == BEN

    entries = await PostgresChangeRecord(clean).recent()
    assert [(e.setting, e.kind) for e in entries] == [
        ("console_access:ana", ChangeKind.APPLIED),
        ("console_access:ben", ChangeKind.ESCALATION),
        ("console_access:ana", ChangeKind.ESCALATION),
    ]


async def test_the_cli_lists_hashes_and_reads_the_record_back(
    clean: AsyncEngine,
) -> None:
    issued = _cli("issue", "ana", "--label", "laptop").split()[1]

    listed = _cli("list")
    assert "ana" in listed and "laptop" in listed
    assert issued not in listed

    logged = _cli("log", "--limit", "5")
    assert "console_access:ana" in logged
    assert "cli:shell" in logged
    assert issued not in logged
