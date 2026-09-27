"""The usage API, built by the admin entrypoint, over the real database and a FakeLangfuse.

`entrypoints.admin.build` is what the container runs; the transport it is
given carries both CyberdyneAuth (FakeOIDC) and Langfuse's read APIs
(FakeLangfuse). FakeLangfuse still holds the traces of an opted-out person, an
erased person and a trace whose deletion is pending, as Langfuse does until its
deletion runs; none of them may be counted or shown.
"""

from __future__ import annotations

import base64
import json
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.tracing.langfuse import APP_TAG
from chatmemory.entrypoints import admin
from tests.e2e.harness.oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeOIDC

PUBLIC_URL = "https://admin.test"
LANGFUSE = "langfuse.e2e.test"
NOW = datetime.now(UTC).replace(microsecond=0)
NOTICE = NOW - timedelta(days=5)
ANA, OLLY, ERIN = 1001, 1002, 1003


def _trace(trace_id: str, user: int, at: datetime, question: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": trace_id,
        "name": "corpus.fixed",
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "environment": "production",
        "tags": [APP_TAG, "feature:corpus.fixed"],
        "userId": str(user),
        "input": {"question": question},
        "output": {"answer": "an answer that must never be shown"},
        "metadata": {"prompt_tokens": 10, "completion_tokens": 2, "evidence": []},
        "totalCost": 0.001,
        **extra,
    }


class FakeLangfuse:
    """Langfuse's v1 reads over `held`, honouring the filters a query sends."""

    def __init__(self) -> None:
        self.held = [
            _trace("ana-before", ANA, NOTICE - timedelta(days=1), "before the notice"),
            _trace("ana-after", ANA, NOTICE + timedelta(days=1), "what did I miss?"),
            _trace("ana-pending", ANA, NOTICE + timedelta(days=2), "a withdrawn question"),
            _trace("olly", OLLY, NOTICE + timedelta(days=1), "olly opted out"),
            _trace("erin", ERIN, NOTICE - timedelta(days=2), "erin erased this"),
            _trace("staging", ANA, NOTICE + timedelta(days=1), "staging", environment="staging"),
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/metrics":
            query = json.loads(request.url.params["query"])
            return httpx.Response(200, json={"data": self._metrics(query)})
        params = request.url.params
        rows = [
            r for r in self.held
            if r["userId"] == params["userId"]
            and r["environment"] == params["environment"]
            and params["fromTimestamp"] <= r["timestamp"] < params["toTimestamp"]
        ]
        return httpx.Response(200, json={"data": rows, "meta": {"page": 1, "totalPages": 1}})

    def _metrics(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        if query["view"] != "traces":
            return []
        rows = [r for r in self.held if self._passes(r, query)]
        if not query["dimensions"]:
            return [{"count_count": len(rows)}]
        counts = Counter((r["userId"], r["name"], r["timestamp"][:10]) for r in rows)
        return [
            {"userId": u, "name": n, "time_dimension": d, "count_count": c}
            for (u, n, d), c in counts.items()
        ]

    @staticmethod
    def _passes(row: dict[str, Any], query: dict[str, Any]) -> bool:
        if not query["fromTimestamp"] <= row["timestamp"] < query["toTimestamp"]:
            return False
        for f in query["filters"]:
            value = row["tags"] if f["column"] == "tags" else row.get(f["column"])
            if f["operator"] == "=" and value != f["value"]:
                return False
            if f["operator"] == "any of" and not set(f["value"]) & set(value or ()):
                return False
            if f["operator"] == "none of" and value in f["value"]:
                return False
        return True


class Routed(httpx.AsyncBaseTransport):
    def __init__(self, oidc: FakeOIDC, langfuse: FakeLangfuse) -> None:
        self._oidc = oidc.transport
        self._langfuse = httpx.MockTransport(langfuse)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self._langfuse if request.url.host == LANGFUSE else self._oidc
        return await target.handle_async_request(request)


def _environ(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "ADMIN_OIDC_ISSUER": ISSUER,
        "ADMIN_OIDC_CLIENT_ID": CLIENT_ID,
        "ADMIN_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "ADMIN_SESSION_KEY": base64.b64encode(bytes(range(32))).decode(),
        "ADMIN_PUBLIC_URL": PUBLIC_URL,
        "LANGFUSE_HOST": f"https://{LANGFUSE}",
        "LANGFUSE_PUBLIC_KEY": "pk-e2e",
        "LANGFUSE_SECRET_KEY": "sk-e2e",
    }


async def _seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for uid, name in ((ANA, "Ana"), (OLLY, "Olly"), (ERIN, "")):
            pid = (
                await conn.execute(
                    text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
                    {"n": name},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                    "VALUES ('discord', :u, :p)"
                ),
                {"u": uid, "p": pid},
            )
            if uid == ANA:
                await conn.execute(
                    text("UPDATE person SET tracing_notice_at = :t WHERE id = :p"),
                    {"t": NOTICE, "p": pid},
                )
            if uid == OLLY:
                await conn.execute(
                    text("INSERT INTO person_opt_out (person_id) VALUES (:p)"), {"p": pid}
                )
            if uid == ERIN:
                await conn.execute(
                    text("UPDATE person SET erased_before = :t WHERE id = :p"),
                    {"t": NOTICE, "p": pid},
                )
        await conn.execute(
            text(
                "INSERT INTO trace_export (trace_id, created_at, deletion_requested_at) "
                "VALUES ('ana-pending', :c, now())"
            ),
            {"c": NOTICE + timedelta(days=2)},
        )


class Console:
    def __init__(self, process: admin.ConsoleProcess, fake: FakeOIDC) -> None:
        self.process = process
        self.fake = fake

    async def signed_in(self, sub: str) -> httpx.AsyncClient:
        browser = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.process.app), base_url=PUBLIC_URL
        )
        started = await browser.get("/auth/login")
        code, state = self.fake.authorize(started.headers["location"], sub)
        done = await browser.get("/auth/callback", params={"code": code, "state": state})
        assert done.status_code == 302, done.text
        return browser


@pytest_asyncio.fixture
async def console(clean: AsyncEngine, e2e_database_url: str) -> AsyncIterator[Console]:
    await _seed(clean)
    fake = FakeOIDC()
    fake.add("ana-sub", "ana@cyberdyne.test", [fake.role("admin")])
    fake.add("ben-sub", "ben@cyberdyne.test", [fake.role("operator")])
    process = admin.build(
        _environ(e2e_database_url), transport=Routed(fake, FakeLangfuse())
    )
    try:
        yield Console(process, fake)
    finally:
        assert process.sign_in is not None
        await process.sign_in.provider.aclose()
        await process.engine.dispose()


async def test_counts_leave_out_opted_out_erased_pending_and_foreign_traces(
    console: Console,
) -> None:
    ben = await console.signed_in("ben-sub")
    response = await ben.get("/api/usage/summary", params={"group": "person"})
    refused = await ben.get(f"/api/usage/people/{ANA}/questions")
    await ben.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(r["key"], r["name"], r["questions"]) for r in body["rows"]] == [
        (str(ANA), "Ana", 2)
    ]
    assert refused.status_code == 403


async def test_an_admin_reads_only_questions_after_the_notice_and_it_is_recorded(
    console: Console, clean: AsyncEngine
) -> None:
    ana = await console.signed_in("ana-sub")
    response = await ana.get(f"/api/usage/people/{ANA}/questions")
    erased = await ana.get(f"/api/usage/people/{ERIN}/questions")
    await ana.aclose()

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [q["question"] for q in body["questions"]] == ["what did I miss?"]
    assert body["hidden_before_notice"] == 1
    assert "never be shown" not in response.text
    assert erased.json()["questions"] == [] and "erin erased" not in erased.text
    async with clean.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT operator, operator_display, after_value FROM config_audit "
                    "WHERE setting = 'usage.questions_viewed' ORDER BY id"
                )
            )
        ).all()
    assert [(r.operator, r.operator_display) for r in rows] == [
        ("oidc:ana-sub", "ana@cyberdyne.test")
    ] * 2
    assert f"(Ana), platform id {ANA}" in rows[0].after_value
