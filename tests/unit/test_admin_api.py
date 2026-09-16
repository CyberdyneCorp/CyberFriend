"""The console API: what it refuses, and what it refuses to say.

The refusals are the product here, so they are what this file spends its time
on. A request that names a different operator, a revoked credential, a request
for message content, and any response carrying a secret are each tested
directly rather than inferred from a handler's shape.

The harness at the top is shared with the other `test_admin_api_*` modules. It
builds the real application -- the real routes, the real middleware, the real
`ConfigurationEditor` -- over in-memory ports, so a test exercises the same
code path a request takes in production and not a hand-rolled imitation of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from chatmemory.adapters.mcp_client.config import ServerConfig
from chatmemory.adapters.mcp_client.registry import ServerDiscovery
from chatmemory.adapters.mcp_client.session import DiscoveredTool
from chatmemory.admin.audit import ChangeKind, InMemoryChangeRecord
from chatmemory.admin.auth import InMemoryOperatorTokens, Operator
from chatmemory.admin.handlers.queries import (
    ChannelEvidence,
    IngestionProgress,
    OptOutEntry,
    StatusSnapshot,
)
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.server import NO_CORPUS, build_app
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.configuration import ConfigurationEditor, RuntimeConfiguration
from chatmemory.app.optout import OptOutService, PersonPurge
from chatmemory.app.tokens import InMemoryTokenStore, TokenRecord
from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints.admin import environment_baseline
from chatmemory.ports.configuration import StoredSetting

ANA = Operator("ana")
BEN = Operator("ben")
AT = datetime(2026, 1, 1, tzinfo=UTC)

#: A deployment's worth of environment. These are the *editable* settings; the
#: console is never given a credential, which is why no secret appears here.
ENVIRON = {
    "INDEXED_CHANNEL_IDS": "100 200",
    "FEDERATION_SERVERS": "issues=https://issues.internal/mcp",
    "FEDERATION_TOOL_ALLOWLIST": "issues:search_issues:ro",
    "FEDERATION_CREDENTIAL_HOLDERS": "issues=discord:7",
    "ASK_MIN_CONFIDENCE": "0.6",
}

#: Values that must never appear in a response, in any form. Written here as
#: the literal strings a leak would contain, so the assertion is a substring
#: search rather than a claim about which fields exist.
SECRETS = {
    "discord": "zzz-discord-bot-token-zzz",
    "model": "sk-live-model-key-zzz",
    "database": "postgresql+asyncpg://user:hunter2@db/chatmemory",
}


# --- fakes -------------------------------------------------------------


class FakeConfigurationStore:
    """A `ConfigurationStore` that keeps rows in memory."""

    def __init__(self, rows: Sequence[StoredSetting] = ()) -> None:
        self.rows = list(rows)
        self.refusals: list[tuple[str, str, str]] = []

    async def load(self) -> Sequence[StoredSetting]:
        return list(self.rows)

    async def put(self, key: str, raw: str, operator: str) -> None:
        self.rows = [r for r in self.rows if r.key != key]
        self.rows.append(
            StoredSetting(key=key, raw=raw, updated_by=operator, updated_at=AT)
        )

    async def clear(self, key: str, operator: str) -> None:
        self.rows = [r for r in self.rows if r.key != key]

    async def record_refusal(self, key: str, operator: str, reason: str) -> None:
        self.refusals.append((key, operator, reason))


class FakeCorpusStatus:
    def __init__(self, snapshot: StatusSnapshot) -> None:
        self.snapshot_value = snapshot

    async def snapshot(self) -> StatusSnapshot:
        return self.snapshot_value


class FakeChannelDirectory:
    def __init__(self, rows: Sequence[ChannelEvidence] = ()) -> None:
        self.rows = list(rows)

    async def channels(self) -> Sequence[ChannelEvidence]:
        return list(self.rows)

    async def channel(self, channel_id: int) -> ChannelEvidence | None:
        return next((c for c in self.rows if c.channel_id == channel_id), None)


class FakeOptOutDirectory:
    def __init__(self, rows: Sequence[OptOutEntry] = ()) -> None:
        self.rows = list(rows)

    async def opted_out(self) -> Sequence[OptOutEntry]:
        return list(self.rows)


class FakeOptOutRegistry:
    """The registry half of an opt-out, with the purge counted rather than done."""

    def __init__(self) -> None:
        self.excluded: set[PersonRef] = set()
        self.purged: list[PersonRef] = []

    async def record_opt_out(self, person: PersonRef, reason: str = "") -> None:
        self.excluded.add(person)

    async def clear_opt_out(self, person: PersonRef) -> None:
        self.excluded.discard(person)

    async def is_opted_out(self, person: PersonRef) -> bool:
        return person in self.excluded

    async def purge_person(self, person: PersonRef) -> PersonPurge:
        self.purged.append(person)
        return PersonPurge(windows=2, messages=9, asks=1, reactions=0, mentions=3)


class FakeDocumentPurge:
    async def purge_person_documents(self, person: PersonRef) -> int:
        return 1


@dataclass
class FakeProbe:
    """What a federated server would say, without a federated server.

    `offers` is keyed by server name; a name that is absent is a server that
    cannot be reached, which is the case the console has to report rather than
    store.
    """

    offers: dict[str, tuple[DiscoveredTool, ...]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def __call__(self, server: ServerConfig) -> ServerDiscovery:
        self.calls.append(server.name)
        tools = self.offers.get(server.name)
        if tools is None:
            return ServerDiscovery(name=server.name, failure="connection refused")
        return ServerDiscovery(name=server.name, tools=tools)


def tool(name: str, effect: ToolEffect = ToolEffect.UNDETERMINED) -> DiscoveredTool:
    return DiscoveredTool(name=name, description="ignored", effect=effect)


@dataclass(frozen=True, slots=True)
class ReviewAndRevokeOnly:
    """The half of a token store the console is allowed to hold.

    A `TokenDirectory` over an `InMemoryTokenStore`. The tests still need the
    store itself to *arrange* a credential -- one issued from the shell, which
    is the only place issuing happens -- but what the application gets is this,
    so a handler that reached for `issue` fails here instead of shipping.
    """

    store: InMemoryTokenStore

    async def revoke(self, person: PersonRef) -> int:
        return await self.store.revoke(person)

    async def active_tokens(self) -> Sequence[TokenRecord]:
        return await self.store.active_tokens()


# --- the harness -------------------------------------------------------


@dataclass
class Console:
    """One built console, its ports, and a client that speaks to it."""

    client: TestClient
    services: AdminServices
    tokens: InMemoryOperatorTokens
    store: FakeConfigurationStore
    changes: InMemoryChangeRecord
    probe: FakeProbe
    mcp_tokens: InMemoryTokenStore
    optouts: FakeOptOutRegistry
    channels: FakeChannelDirectory
    credential: str

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.credential}"}

    def kinds(self, kind: ChangeKind) -> list[str]:
        return [e.setting for e in self.changes._entries if e.kind is kind]  # noqa: SLF001


async def build_console(
    *,
    rows: Sequence[StoredSetting] = (),
    offers: dict[str, tuple[DiscoveredTool, ...]] | None = None,
    channels: Sequence[ChannelEvidence] = (),
    optouts: Sequence[OptOutEntry] = (),
    status: StatusSnapshot | None = None,
) -> Console:
    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(ANA, "laptop")
    store = FakeConfigurationStore(rows)
    configuration = RuntimeConfiguration(store, environment_baseline(ENVIRON))
    await configuration.refresh()
    changes = InMemoryChangeRecord()
    probe = FakeProbe(offers or {})
    registry = FakeOptOutRegistry()
    mcp_tokens = InMemoryTokenStore()
    directory = FakeChannelDirectory(channels)

    services = AdminServices(
        configuration=configuration,
        editor=ConfigurationEditor(store),
        changes=changes,
        status=FakeCorpusStatus(status or _healthy()),
        channels=directory,
        optout_directory=FakeOptOutDirectory(optouts),
        optouts=OptOutService(registry, FakeDocumentPurge()),
        mcp_tokens=ReviewAndRevokeOnly(mcp_tokens),
        probe=probe,
    )
    app = build_app(services, tokens)
    return Console(
        client=TestClient(app),
        services=services,
        tokens=tokens,
        store=store,
        changes=changes,
        probe=probe,
        mcp_tokens=mcp_tokens,
        optouts=registry,
        channels=directory,
        credential=issued.token,
    )


def _healthy() -> StatusSnapshot:
    return StatusSnapshot(
        database_reachable=True,
        ingestion=IngestionProgress(
            messages=1200,
            windows=140,
            newest_message_at=AT,
            channels_with_cursor=2,
            channels_backfilled=1,
            channels_pending_rebuild=0,
        ),
        embedding_backlog=7,
        indexed_channels=2,
    )


# --- authentication ----------------------------------------------------


async def test_a_request_with_a_credential_acts_as_that_operator() -> None:
    console = await build_console()

    console.client.post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    recorded = [e.operator for e in await console.changes.recent()]
    assert recorded == ["ana"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer cfa_not-a-real-credential"},
    ],
    ids=["missing", "malformed", "wrong-scheme", "unknown"],
)
async def test_every_bad_credential_is_refused_identically(headers: dict[str, str]) -> None:
    """A refusal that distinguishes them is an oracle for who holds a token."""
    console = await build_console()
    revoked = await console.tokens.issue(BEN, "old")
    await console.tokens.revoke(BEN)

    responses = [
        console.client.get("/api/settings", headers=headers),
        console.client.get("/api/settings", headers={"Authorization": f"Bearer {revoked.token}"}),
    ]

    assert {r.status_code for r in responses} == {401}
    assert {r.content for r in responses} == {b'{"error":"unauthorized"}'}


@pytest.mark.parametrize(
    "path",
    ["/api/settings", "/api/audit", "/api/tokens", "/api/status", "/api/federation/servers"],
)
async def test_every_api_route_is_refused_when_the_console_sits_behind_a_prefix(
    path: str,
) -> None:
    """Mounting the console under /console/ must not open the whole API.

    The deployment notes offer that shape, and a proxy serving it sets
    `root_path`. The router resolves handlers on the path with `root_path`
    stripped, so a guard reading the raw path would stop matching while the
    router kept dispatching -- every read here would answer anonymously, and
    `/api/federation/servers` would probe every federated server on behalf of
    a caller holding nothing.
    """
    console = await build_console()
    behind = TestClient(console.client.app, root_path="/console")

    response = behind.get(f"/console{path}")

    assert response.status_code == 401
    assert response.content == b'{"error":"unauthorized"}'
    assert console.probe.calls == []


async def test_a_credential_is_still_honoured_behind_a_prefix() -> None:
    console = await build_console()
    behind = TestClient(console.client.app, root_path="/console")

    response = behind.get("/console/api/settings", headers=console.auth())

    assert response.status_code == 200


async def test_a_revoked_operator_stops_working_and_others_do_not() -> None:
    console = await build_console()
    ben = await console.tokens.issue(BEN, "laptop")

    await console.tokens.revoke(BEN)

    assert console.client.get("/api/settings", headers=console.auth(ben.token)).status_code == 401
    assert console.client.get("/api/settings", headers=console.auth()).status_code == 200


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"platform": "discord", "platform_user_id": 42, "operator": "ben"}},
        {
            "json": {"platform": "discord", "platform_user_id": 42},
            "params": {"operator": "ben"},
        },
    ],
    ids=["body", "query"],
)
async def test_a_request_cannot_name_the_operator_it_acts_as(
    request_kwargs: dict[str, object],
) -> None:
    """Only the credential decides. An asserted identity is not a fact."""
    console = await build_console()

    response = console.client.post(
        "/api/optouts", headers=console.auth(), **request_kwargs  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert [e.operator for e in await console.changes.recent()] == ["ana"]


async def test_a_second_authorization_header_is_refused_rather_than_resolved() -> None:
    console = await build_console()
    ben = await console.tokens.issue(BEN, "laptop")

    response = console.client.get(
        "/api/settings",
        headers=[
            ("Authorization", f"Bearer {console.credential}"),
            ("Authorization", f"Bearer {ben.token}"),
        ],
    )

    assert response.status_code == 401


async def test_health_and_the_console_bundle_stay_open() -> None:
    """A probe holds no credential, and the interface is code, not configuration."""
    console = await build_console()

    assert console.client.get("/health").status_code == 200
    assert console.client.get("/ready").status_code == 200
    assert console.client.get("/").status_code == 200


# --- the corpus is not reachable ---------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/messages",
        "/api/messages/1",
        "/api/windows",
        "/api/documents",
        "/api/asks",
        "/api/search?q=secret",
    ],
)
async def test_no_endpoint_returns_corpus_content(path: str) -> None:
    console = await build_console()

    response = console.client.get(path, headers=console.auth())

    assert response.status_code == 404
    assert response.json()["error"] == NO_CORPUS


async def test_an_unauthenticated_content_request_is_refused_as_unauthenticated() -> None:
    """The boundary that answers first is the credential, not the path.

    Otherwise an anonymous caller could enumerate which console paths exist.
    """
    console = await build_console()

    assert console.client.get("/api/messages").status_code == 401


async def test_the_status_screen_reports_counts_and_no_content() -> None:
    console = await build_console()

    body = console.client.get("/api/status", headers=console.auth()).json()

    assert body["status"] == "ok"
    assert body["ingestion"]["messages"] == 1200
    assert body["embedding_backlog"] == 7
    assert "text" not in repr(body) or "content" not in repr(body)
    assert set(body["ingestion"]) == {
        "messages",
        "windows",
        "newest_message_at",
        "channels_with_cursor",
        "channels_backfilled",
        "channels_pending_rebuild",
    }


async def test_a_database_that_cannot_be_read_is_reported_rather_than_zeroed() -> None:
    """Zero and "I could not ask" are different answers to "is it ingesting"."""
    console = await build_console(status=StatusSnapshot(database_reachable=False))

    body = console.client.get("/api/status", headers=console.auth()).json()

    assert body["status"] == "unavailable"
    assert body["ingestion"] is None
    assert body["embedding_backlog"] is None
    assert console.client.get("/ready").status_code == 503


# --- secrets -----------------------------------------------------------


@pytest.mark.parametrize("key", ["discord_token", "llm_api_key", "database_url"])
async def test_setting_a_secret_is_refused(key: str) -> None:
    console = await build_console()

    response = console.client.put(
        f"/api/settings/{key}", json={"value": SECRETS["discord"]}, headers=console.auth()
    )

    assert response.status_code == 400
    assert "credential" in response.json()["error"]
    assert not any(row.key == key for row in console.store.rows)


@pytest.mark.parametrize("key", ["discord_token", "llm_api_key", "database_url"])
async def test_a_refused_secret_never_reaches_the_record(key: str) -> None:
    """The refusal is recorded; the value it refused is not.

    An audit trail that helpfully preserved the rejected text would turn
    pasting a credential into the wrong box into a stored secret.
    """
    console = await build_console()

    console.client.put(
        f"/api/settings/{key}", json={"value": SECRETS["model"]}, headers=console.auth()
    )

    assert console.store.refusals, "the refusal itself must be recorded"
    assert all(SECRETS["model"] not in part for r in console.store.refusals for part in r)


async def test_no_response_anywhere_carries_a_secret() -> None:
    """Every GET, with the environment carrying real-looking credentials.

    The console never reads them -- it is not given them -- and this is the
    test that says so rather than the comment claiming it.
    """
    console = await build_console(
        rows=[
            # A row somebody inserted around the console. It must not be
            # resolved and must not be displayed.
            StoredSetting(
                key="discord_token", raw=SECRETS["discord"], updated_by="shell", updated_at=AT
            )
        ]
    )
    await console.services.configuration.refresh()

    paths = [
        "/api/status",
        "/api/settings",
        "/api/federation/servers",
        "/api/federation/allowlist",
        "/api/channels",
        "/api/optouts",
        "/api/tokens",
        "/api/audit",
    ]
    bodies = [console.client.get(p, headers=console.auth()).text for p in paths]

    for body in bodies:
        for name, value in SECRETS.items():
            assert value not in body, f"the {name} credential leaked"
            # Masked is still exposed: a prefix identifies which credential a
            # leaked one is, and confirms a guess.
            assert value[:8] not in body


# --- settings ----------------------------------------------------------


async def test_provenance_uses_the_vocabulary_the_contract_names() -> None:
    """db, env, default -- the three words the interface was written against.

    The console and the API were built in parallel from one contract; a value
    the console does not recognise leaves the column that explains where a
    setting came from empty, which is the column's whole reason for existing.
    """
    console = await build_console(
        rows=[StoredSetting("ask_min_confidence", "0.8", "ana", AT)]
    )
    await console.services.configuration.refresh()

    listed = console.client.get("/api/settings", headers=console.auth()).json()

    assert {v["source"] for v in listed} <= {"db", "env", "default"}


async def test_settings_report_where_each_value_came_from() -> None:
    console = await build_console(
        rows=[StoredSetting("ask_min_confidence", "0.8", "ana", AT)]
    )
    await console.services.configuration.refresh()

    listed = console.client.get("/api/settings", headers=console.auth()).json()
    views = {v["key"]: v for v in listed}

    assert views["ask_min_confidence"]["source"] == "db"
    assert views["ask_min_confidence"]["updated_by"] == "ana"
    # Set in the environment of this deployment, so not a default.
    assert views["indexed_channel_ids"]["source"] == "env"
    # Nobody said anything about this one.
    assert views["window_max_messages"]["source"] == "default"


async def test_a_setting_changed_through_the_api_takes_effect_immediately() -> None:
    console = await build_console()

    response = console.client.put(
        "/api/settings/ask_min_confidence", json={"value": "0.9"}, headers=console.auth()
    )

    assert response.status_code == 200
    assert response.json()["value"] == 0.9
    assert response.json()["source"] == "db"
    assert ("ask_min_confidence", "0.9", "ana") in [
        (r.key, r.raw, r.updated_by) for r in console.store.rows
    ]


async def test_a_malformed_value_is_refused_while_the_operator_is_watching() -> None:
    console = await build_console()

    response = console.client.put(
        "/api/settings/ask_min_confidence", json={"value": "2.0"}, headers=console.auth()
    )

    assert response.status_code == 400
    assert "between 0 and 1" in response.json()["error"]
    assert console.store.refusals


@pytest.mark.parametrize(
    "key",
    [
        "ask_min_confidence",
        "web_timeout_seconds",
        "window_max_messages",
        "ask_extraction_enabled",
        "federation_max_tools_per_run",
    ],
)
async def test_a_credential_pasted_into_a_settings_box_is_not_echoed(key: str) -> None:
    """The box is an ordinary setting, so the secret path never sees this.

    What refuses it is the parser, and the stdlib's number parsers quote the
    text they were given. That message reached the 400 body, the browser and
    `config_audit` -- which migration 0012 makes append-only, so the leak would
    be permanent and readable by every operator through `GET /api/audit`.
    """
    console = await build_console()

    response = console.client.put(
        f"/api/settings/{key}", json={"value": SECRETS["model"]}, headers=console.auth()
    )

    assert response.status_code == 400
    assert SECRETS["model"] not in response.text
    assert console.store.refusals, "the refusal itself must be recorded"
    assert all(SECRETS["model"] not in part for r in console.store.refusals for part in r)
    assert not any(row.key == key and row.raw == SECRETS["model"] for row in console.store.rows)


async def test_an_unknown_setting_is_refused() -> None:
    console = await build_console()

    response = console.client.put(
        "/api/settings/enable_everything", json={"value": "yes"}, headers=console.auth()
    )

    assert response.status_code == 400
    assert "not a setting" in response.json()["error"]


@pytest.mark.parametrize(
    "key", ["federation_tool_allowlist", "federation_servers", "indexed_channel_ids"]
)
async def test_a_validated_setting_cannot_be_written_around_its_endpoint(key: str) -> None:
    """The hole this closes is the whole of the federation screen.

    A raw PUT of `issues:anything:enable-mutation` would enable a mutating
    tool with no probe, no confirmation and no escalation recorded.
    """
    console = await build_console()

    response = console.client.put(
        f"/api/settings/{key}",
        json={"value": "issues:delete_everything:enable-mutation"},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "/api/" in response.json()["error"]
    assert not any(r.key == key for r in console.store.rows)
    assert key in console.kinds(ChangeKind.REFUSED)


async def test_a_validated_setting_is_marked_not_editable_in_the_listing() -> None:
    console = await build_console()

    listed = console.client.get("/api/settings", headers=console.auth()).json()
    views = {v["key"]: v for v in listed}

    assert views["federation_tool_allowlist"]["editable"] is False
    assert views["federation_tool_allowlist"]["managed_by"] == "/api/federation/allowlist"
    assert views["ask_min_confidence"]["editable"] is True


# --- channels ----------------------------------------------------------


async def test_a_channel_the_corpus_shows_no_sign_of_is_reported_not_hidden() -> None:
    console = await build_console()

    response = console.client.post("/api/channels", json={"id": 900}, headers=console.auth())

    assert response.status_code == 200
    channel = response.json()["channel"]
    assert channel["readable_by_bot"] is False
    assert channel["evidence"], "an operator must be told what is not known"
    assert "900" in [r.raw for r in console.store.rows if r.key == "indexed_channel_ids"][0]


async def test_a_channel_with_stored_messages_reads_as_readable() -> None:
    console = await build_console(
        channels=[ChannelEvidence(100, "general", True, messages=42, last_message_at=AT)]
    )

    rows = {c["id"]: c for c in console.client.get("/api/channels", headers=console.auth()).json()}

    assert rows["100"]["readable_by_bot"] is True
    assert rows["100"]["messages"] == 42
    assert rows["100"]["indexed"] is True
    # In the corpus, out of scope: the case an operator forgot about.
    assert rows["200"]["indexed"] is True


async def test_removing_a_channel_says_what_it_does_not_do() -> None:
    console = await build_console()

    response = console.client.delete("/api/channels/100", headers=console.auth())

    assert response.status_code == 200
    assert "stay archived" in response.json()["detail"]
    stored = [r.raw for r in console.store.rows if r.key == "indexed_channel_ids"][0]
    assert stored.split() == ["200"]


async def test_removing_a_channel_that_is_not_in_scope_is_a_404() -> None:
    console = await build_console()

    assert console.client.delete("/api/channels/999", headers=console.auth()).status_code == 404


# --- opt-outs ----------------------------------------------------------


async def test_an_opt_out_purges_and_reports_counts_only() -> None:
    console = await build_console()

    response = console.client.post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    body = response.json()
    assert body["removed"] == {
        "windows": 2,
        "messages": 9,
        "asks": 1,
        "reactions": 0,
        "mentions": 3,
        "documents": 1,
        "total": 16,
    }
    assert console.optouts.purged == [PersonRef("discord", 42)]


async def test_opting_back_in_says_that_nothing_comes_back() -> None:
    console = await build_console()
    await console.optouts.record_opt_out(PersonRef("discord", 42))

    response = console.client.delete("/api/optouts/discord/42", headers=console.auth())

    assert response.status_code == 200
    assert "Nothing purged comes back" in response.json()["detail"]
    assert not console.optouts.excluded


async def test_the_opt_out_list_carries_who_and_when_and_nothing_else() -> None:
    console = await build_console(
        optouts=[OptOutEntry(PersonRef("discord", 42), person_id=3, since=AT)]
    )

    rows = console.client.get("/api/optouts", headers=console.auth()).json()

    assert rows == [{"person": "discord:42", "since": AT.isoformat()}]


# --- MCP tokens --------------------------------------------------------


async def test_the_console_cannot_mint_a_corpus_credential() -> None:
    """The regression this file exists for.

    An MCP credential reads everything one Discord account can see. A console
    that could mint one would hand an operator holding nothing but a `cfa_`
    token a route to private channels -- around the viewer scope, the ACL
    predicate and every other guarantee here -- so there is no route that
    mints one, and the console is not even given a port that could.
    """
    console = await build_console()

    response = console.client.post(
        "/api/tokens",
        json={"label": "laptop", "platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    assert response.status_code == 404
    assert response.json()["error"] == NO_CORPUS
    assert "cfm_" not in response.text
    # Nothing was minted, and nothing pretends one was.
    assert not await console.mcp_tokens.active_tokens()
    assert console.kinds(ChangeKind.ESCALATION) == []


async def test_the_console_is_handed_no_way_to_mint_one() -> None:
    """Not "the handler does not call it" -- the method is not reachable.

    The route above is what an audit finds; this is what stops the next
    version of the route from being written. `AdminServices.mcp_tokens` is a
    `TokenDirectory`, and the object the tests wire in is one, so a handler
    reaching for `issue` fails here rather than in production.
    """
    console = await build_console()

    assert not hasattr(console.services.mcp_tokens, "issue")
    assert not hasattr(console.services.mcp_tokens, "rotate")
    assert not hasattr(console.services.mcp_tokens, "person_for_token")


async def test_a_credential_issued_elsewhere_is_listed_without_its_secret() -> None:
    """Issued by the shell CLI; the console reviews it and nothing more."""
    console = await build_console()
    issued = await console.mcp_tokens.issue(PersonRef("discord", 42), "laptop")

    listing = console.client.get("/api/tokens", headers=console.auth())

    assert issued.token not in listing.text
    assert listing.json() == [
        {
            "id": "discord:42",
            "person": "discord:42",
            "label": "laptop",
            "issued_at": listing.json()[0]["issued_at"],
            "revoked_at": None,
        }
    ]


async def test_a_listing_never_carries_a_stored_hash() -> None:
    console = await build_console()
    issued = await console.mcp_tokens.issue(PersonRef("discord", 42), "laptop")

    listing = console.client.get("/api/tokens", headers=console.auth()).text

    assert issued.token_hash not in listing
    assert issued.token_hash[:12] not in listing


async def test_revoking_a_credential_is_an_ordinary_change() -> None:
    console = await build_console()
    await console.mcp_tokens.issue(PersonRef("discord", 42), "laptop")

    response = console.client.delete("/api/tokens/discord:42", headers=console.auth())

    assert response.status_code == 200
    assert console.kinds(ChangeKind.APPLIED) == ["mcp_token:discord:42"]
    assert not await console.mcp_tokens.active_tokens()


async def test_revoking_a_credential_nobody_holds_is_a_404() -> None:
    console = await build_console()

    response = console.client.delete("/api/tokens/discord:42", headers=console.auth())

    assert response.status_code == 404


# --- the change record -------------------------------------------------


async def test_the_audit_returns_refusals_beside_applied_changes() -> None:
    console = await build_console()
    console.client.put(
        "/api/settings/federation_servers", json={"value": "x=y"}, headers=console.auth()
    )
    console.client.post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    entries = console.client.get("/api/audit", headers=console.auth()).json()

    assert [e["kind"] for e in entries] == ["applied", "refused"]
    assert all(e["operator"] == "ana" for e in entries)
    assert all("at" in e and e["at"] for e in entries)


async def test_the_audit_has_no_route_that_alters_it() -> None:
    """Append-only as a shape, not as a rule somebody remembers."""
    console = await build_console()

    for method in ("post", "put", "patch", "delete"):
        response = getattr(console.client, method)("/api/audit", headers=console.auth())
        assert response.status_code == 404, method


async def test_the_audit_limit_is_bounded() -> None:
    console = await build_console()
    for index in range(5):
        console.client.post(
            "/api/optouts",
            json={"platform": "discord", "platform_user_id": index},
            headers=console.auth(),
        )

    entries = console.client.get("/api/audit?limit=2", headers=console.auth()).json()

    assert len(entries) == 2
