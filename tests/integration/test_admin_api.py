"""The console against a live Postgres, wired exactly as the container wires it.

Every test here runs `chatmemory.entrypoints.admin.build` -- the same function
the service calls at startup -- so what is exercised is the deployed
arrangement rather than a test's idea of it. The unit tests assert the shape;
these assert that the shape survives contact with the schema, the append-only
trigger, and a corpus with real rows in it.

Two of them are the ones worth the database:

*   a change made through the API appears in `config_audit` with the value
    before and the value after, written by the store in the same transaction
    as the setting;
*   a message with a distinctive string in it is seeded into the corpus, and
    every endpoint is asked for everything it will give -- the string appears
    in none of it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.admin.auth import Operator
from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints import admin
from chatmemory.mcp.auth import PostgresTokenStore
from tests.integration.conftest import DB_URL

ANA = Operator("ana")
BEN = Operator("ben")

#: A string that exists only inside a message. If it comes back from the
#: console, the console has become a way to read private channels.
PRIVATE = "the-renewal-number-is-four-hundred-thousand"

ENVIRON = {
    "DATABASE_URL": DB_URL,
    "INDEXED_CHANNEL_IDS": "100 200",
    "FEDERATION_SERVERS": "issues=https://issues.internal/mcp",
    "FEDERATION_TOOL_ALLOWLIST": "issues:search_issues:ro",
    "FEDERATION_CREDENTIAL_HOLDERS": "issues=discord:7",
}

READ_ONLY_PATHS = [
    "/api/status",
    "/api/settings",
    "/api/channels",
    "/api/optouts",
    "/api/tokens",
    "/api/audit",
]


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(text(f"SELECT to_regclass('public.{table}')"))
        return row.scalar() is not None


@pytest.fixture(autouse=True)
async def _require_the_console_schema(clean: AsyncEngine) -> None:
    """Fail rather than skip when the database is behind the migrations.

    A skipped test is not a passing test: "no app_setting table" means
    `alembic upgrade head` has not been run, which an operator has to see.
    """
    for table in ("app_setting", "admin_token", "config_audit"):
        assert await _table_exists(clean, table), (
            f"{table} is missing; run `alembic upgrade head` against {DB_URL}"
        )


@pytest_asyncio.fixture
async def console(clean: AsyncEngine) -> AsyncIterator[admin.ConsoleProcess]:
    """The service as the container builds it, against the test database."""
    process = admin.build(ENVIRON)
    await process.configuration.refresh()
    yield process
    await process.engine.dispose()


def client(
    process: admin.ConsoleProcess, token: str | None = None
) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=process.app),
        base_url="http://console.test",
        headers=headers,
    )


async def credential(process: admin.ConsoleProcess, operator: Operator = ANA) -> str:
    from chatmemory.adapters.store.admin_postgres import grant_console_access

    issued = await grant_console_access(process.engine, operator, "laptop")
    return issued.token


async def seed_message(engine: AsyncEngine, content: str = PRIVATE) -> None:
    """One message in one channel, written straight to the schema.

    Deliberately not through the ingestion path: what is under test is what the
    console will say about a corpus that has content in it, and the shortest
    way to have one is to put a row in it.
    """
    async with engine.begin() as conn:
        person = await conn.execute(
            text("INSERT INTO person (display_name) VALUES ('ana') RETURNING id")
        )
        person_id = int(person.scalar_one())
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id)"
                " VALUES ('discord', 42, :person_id)"
            ),
            {"person_id": person_id},
        )
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed)"
                " VALUES (100, 'discord', 'general', TRUE)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO message (id, channel_id, author_person_id, content, created_at)"
                " VALUES (9001, 100, :person_id, :content, :at)"
            ),
            {"person_id": person_id, "content": content, "at": datetime.now(UTC)},
        )


# --- the corpus is not reachable ---------------------------------------


async def test_no_endpoint_returns_a_word_of_a_message(
    console: admin.ConsoleProcess,
) -> None:
    await seed_message(console.engine)
    token = await credential(console)

    async with client(console, token) as http:
        bodies = [(await http.get(path)).text for path in READ_ONLY_PATHS]
        bodies.append((await http.get("/api/messages")).text)
        bodies.append((await http.get("/api/messages/9001")).text)

    for body in bodies:
        assert PRIVATE not in body


async def test_the_status_screen_counts_what_it_will_not_quote(
    console: admin.ConsoleProcess,
) -> None:
    await seed_message(console.engine)
    token = await credential(console)

    async with client(console, token) as http:
        body = (await http.get("/api/status")).json()

    assert body["database_reachable"] is True
    assert body["ingestion"]["messages"] == 1
    assert body["ingestion"]["newest_message_at"]
    assert body["embedding_backlog"] == 0


async def test_a_channel_with_messages_reads_as_readable(
    console: admin.ConsoleProcess,
) -> None:
    await seed_message(console.engine)
    token = await credential(console)

    async with client(console, token) as http:
        rows = {c["id"]: c for c in (await http.get("/api/channels")).json()}

    assert rows["100"]["readable_by_bot"] is True
    assert rows["100"]["name"] == "general"
    assert rows["100"]["messages"] == 1
    # In scope from the environment, but nothing has ever been read from it.
    assert rows["200"]["readable_by_bot"] is False
    assert rows["200"]["evidence"]


# --- the change record --------------------------------------------------


async def test_a_change_made_through_the_api_lands_in_the_audit_with_both_values(
    console: admin.ConsoleProcess,
) -> None:
    token = await credential(console)

    async with client(console, token) as http:
        first = await http.put("/api/settings/ask_min_confidence", json={"value": "0.7"})
        second = await http.put("/api/settings/ask_min_confidence", json={"value": "0.9"})
        entries = (await http.get("/api/audit")).json()

    assert (first.status_code, second.status_code) == (200, 200)
    latest = entries[0]
    assert latest["operator"] == "ana"
    assert latest["setting"] == "ask_min_confidence"
    assert latest["before"] == "0.7"
    assert latest["after"] == "0.9"
    assert latest["kind"] == "applied"


async def test_a_stored_setting_reports_the_operator_who_wrote_it(
    console: admin.ConsoleProcess,
) -> None:
    token = await credential(console)

    async with client(console, token) as http:
        await http.put("/api/settings/ask_min_confidence", json={"value": "0.9"})
        await console.configuration.refresh()
        views = {v["key"]: v for v in (await http.get("/api/settings")).json()}

    assert views["ask_min_confidence"]["source"] == "db"
    assert views["ask_min_confidence"]["updated_by"] == "ana"
    assert views["ask_min_confidence"]["value"] == 0.9


async def test_a_refused_change_is_recorded_without_what_was_refused(
    console: admin.ConsoleProcess,
) -> None:
    """The likeliest bad value is a credential pasted into the wrong box."""
    token = await credential(console)

    async with client(console, token) as http:
        refusal = await http.put(
            "/api/settings/llm_api_key", json={"value": "sk-live-zzz"}
        )
        entries = (await http.get("/api/audit")).json()

    assert refusal.status_code == 400
    refused = [e for e in entries if e["kind"] == "refused"]
    assert refused, "the attempt itself has to be recorded"
    assert all("sk-live-zzz" not in str(e) for e in entries)


async def test_a_credential_pasted_into_the_federation_screen_never_reaches_the_table(
    console: admin.ConsoleProcess,
) -> None:
    """The other refusal path, against the table that cannot be cleaned up.

    `federation_tool_allowlist` is not one of the environment-only secrets, so
    `SECRET_SETTINGS` does not guard it: what keeps a token out of the record
    here is the handler refusing to repeat a name it did not recognise. Asked
    of `config_audit` directly rather than only of `/api/audit`, because the
    trigger behind that table refuses DELETE -- a row that lands is a row that
    stays.
    """
    pasted = "ghp_REALTOKEN_abcdef123456"
    token = await credential(console)

    async with client(console, token) as http:
        # A server that is not configured, so nothing is probed and no
        # network is touched: the refusal happens before the value has been
        # recognised as anything.
        refusal = await http.post(
            "/api/federation/allowlist",
            json={"server": pasted, "tool": pasted, "read_only": True},
        )
        entries = (await http.get("/api/audit")).json()

    async with console.engine.connect() as conn:
        stored = await conn.execute(
            text("SELECT operator, setting, kind, before_value, after_value, reason "
                 "FROM config_audit")
        )
        rows = [tuple(str(v) for v in row) for row in stored]

    assert all(pasted not in " ".join(row) for row in rows), "it is in the table forever"
    assert refusal.status_code == 400
    assert pasted not in refusal.text
    assert [e for e in entries if e["kind"] == "refused"], "the attempt is recorded"
    assert all(pasted not in str(e) for e in entries)


async def test_the_database_itself_refuses_to_rewrite_the_record(
    console: admin.ConsoleProcess,
) -> None:
    """A shape only binds the code that went through it; this binds psql too."""
    token = await credential(console)
    async with client(console, token) as http:
        await http.put("/api/settings/ask_min_confidence", json={"value": "0.7"})

    with pytest.raises(Exception):  # noqa: B017 - the trigger's error, whatever it is
        async with console.engine.begin() as conn:
            await conn.execute(text("UPDATE config_audit SET operator = 'nobody'"))


# --- credentials --------------------------------------------------------


async def test_a_revoked_operator_stops_working_and_others_do_not(
    console: admin.ConsoleProcess,
) -> None:
    from chatmemory.adapters.store.admin_postgres import withdraw_console_access

    ana = await credential(console, ANA)
    ben = await credential(console, BEN)

    await withdraw_console_access(console.engine, ANA)

    async with client(console) as http:
        refused = await http.get("/api/settings", headers={"Authorization": f"Bearer {ana}"})
        served = await http.get("/api/settings", headers={"Authorization": f"Bearer {ben}"})

    assert refused.status_code == 401
    assert served.status_code == 200


async def test_the_console_cannot_mint_a_corpus_credential(
    console: admin.ConsoleProcess,
) -> None:
    """The escalation the console must not be able to perform.

    An `mcp_token` row binds a viewer: `mcp/auth.py` turns the credential into
    a `PersonRef`, the ACL resolver turns that into a `Viewer`, and the
    retrieval tools then answer for every channel that Discord account can
    read. A console operator holds a database URL and a `cfa_` token and
    nothing else, so minting one here would be a route from the configuration
    surface into private channels. Asserted against the real service and the
    real table, because the row is the thing that would grant it.
    """
    token = await credential(console)

    async with client(console, token) as http:
        refused = await http.post(
            "/api/tokens",
            json={"label": "laptop", "platform": "discord", "platform_user_id": 42},
        )
        listing = await http.get("/api/tokens")
        entries = (await http.get("/api/audit")).json()

    assert refused.status_code == 404
    assert "cfm_" not in refused.text
    assert listing.json() == []
    assert [e for e in entries if e["setting"].startswith("mcp_token:")] == []

    async with console.engine.connect() as conn:
        rows = await conn.execute(text("SELECT count(*) FROM mcp_token"))
        assert rows.scalar() == 0


async def test_a_credential_issued_from_the_shell_is_listed_and_can_be_revoked(
    console: admin.ConsoleProcess,
) -> None:
    """What the console keeps: reviewing, and taking access away.

    The credential is minted the way production mints one -- the store the
    shell CLI drives -- and the console sees a person, a label and timestamps,
    never the credential, and can revoke it.
    """
    store = PostgresTokenStore(console.engine)
    issued = await store.issue(PersonRef("discord", 42), "laptop")
    token = await credential(console)

    async with client(console, token) as http:
        listing = await http.get("/api/tokens")
        revoked = await http.delete("/api/tokens/discord:42")
        after = await http.get("/api/tokens")
        entries = (await http.get("/api/audit")).json()

    assert issued.token not in listing.text
    assert issued.token_hash not in listing.text
    assert listing.json()[0]["person"] == "discord:42"
    assert revoked.status_code == 200
    assert after.json() == []
    assert [e["kind"] for e in entries if e["setting"] == "mcp_token:discord:42"] == [
        "applied"
    ]
    # Revoked, not deleted: the record that it existed is what an audit needs.
    assert await store.person_for_token(issued.token) is None


# --- opt-out ------------------------------------------------------------


async def test_an_opt_out_through_the_api_purges_and_then_lists_the_person(
    console: admin.ConsoleProcess,
) -> None:
    await seed_message(console.engine)
    token = await credential(console)

    async with client(console, token) as http:
        recorded = await http.post(
            "/api/optouts", json={"platform": "discord", "platform_user_id": 42}
        )
        listed = (await http.get("/api/optouts")).json()
        status = (await http.get("/api/status")).json()

    assert recorded.status_code == 200
    assert recorded.json()["removed"]["messages"] == 1
    assert [e["person"] for e in listed] == ["discord:42"]
    assert listed[0]["since"]
    # The message is gone, and nothing quoted it on the way out.
    assert status["ingestion"]["messages"] == 0
    assert PRIVATE not in recorded.text


async def test_opting_back_in_clears_the_exclusion(
    console: admin.ConsoleProcess,
) -> None:
    token = await credential(console)

    async with client(console, token) as http:
        await http.post("/api/optouts", json={"platform": "discord", "platform_user_id": 42})
        cleared = await http.delete("/api/optouts/discord/42")
        listed = (await http.get("/api/optouts")).json()

    assert cleared.status_code == 200
    assert listed == []


# --- indexing scope -----------------------------------------------------


async def test_a_channel_added_through_the_api_is_in_force_for_the_agent(
    console: admin.ConsoleProcess,
) -> None:
    """The change the ingestion loop will pick up at its next refresh.

    Asserted through a second, independent read of the stored configuration,
    because the question is whether it was *stored* -- not whether this
    process remembers it.
    """
    token = await credential(console)

    async with client(console, token) as http:
        added = await http.post("/api/channels", json={"id": 900})

    assert added.status_code == 200
    stored = {s.key: s.raw for s in await console.services.editor.store.load()}
    assert sorted(stored["indexed_channel_ids"].split()) == ["100", "200", "900"]
