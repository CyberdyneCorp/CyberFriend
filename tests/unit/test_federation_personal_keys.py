"""CyberWealth's `my_*` tools, called with the asker's own connected-app key.

The key is the bearer on that person's personal calls, in their DM, over a
connection opened for the call alone -- never on the shared connection the
service identity holds, never on an `intel_*` call, never for anybody else.
Proved against a real MCP server over ASGI that refuses any bearer it does not
know, as CyberWealth does.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from structlog.testing import capture_logs

from chatmemory.adapters.mcp_client.client import Failure, connect
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.registry import FederationStartupError
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.app.audit import AuditOutcome
from chatmemory.app.authorization import (
    ActionOrigin,
    CredentialScope,
    InvocationRequest,
    Refusal,
    ToolEffect,
)
from chatmemory.app.personal_keys import CYBERWEALTH, PersonalKeys
from chatmemory.composition import FederatedTools, build_federation, with_personal_keys
from chatmemory.domain.identity import PersonRef
from tests.unit.test_composition import settings
from tests.unit.test_federation_service_auth import (
    ENV,
    FakeIssuer,
    cyberwealth_server,
    served,
)
from tests.unit.test_federation_support import FakeSession, read_tool, session_factory
from tests.unit.test_personal_keys import KEY, MemoryKeyStore

ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)
REVOKED = KEY[:-4] + "0000"
SERVICE = "Bearer token-1"

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


def request(tool: str, *, requester: PersonRef = ALICE, direct: bool = True) -> InvocationRequest:
    return InvocationRequest(
        requester=requester,
        question="how is my budget",
        qualified_name=f"cyberwealth:{tool}",
        arguments={},
        origin=ActionOrigin.REQUESTER_REQUEST,
        private=direct,
        direct=direct,
    )


def offered(tools: FederatedTools) -> RoutedTools:
    return RoutedTools(question="", tools=tools.federation.registration.tools)


def gated(app: ASGIApp, accepted: set[str]) -> ASGIApp:
    """CyberWealth's door: 401 for any bearer it did not issue."""

    async def door(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope["type"] == "http":
            raw = dict(scope["headers"]).get(b"authorization")
            if raw is None or raw.decode() not in accepted:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"code":"auth"}'})
                return
        await app(scope, receive, send)

    return door


async def keyed_keys(**held: str) -> PersonalKeys:
    keys = PersonalKeys(MemoryKeyStore())
    people = {"alice": ALICE, "bob": BOB}
    store = keys._store  # noqa: SLF001 - seeding a fake
    for name, key in held.items():
        await store.save(people[name], CYBERWEALTH, key)
    return keys


@asynccontextmanager
async def cyberwealth(
    keys: PersonalKeys, headers: list[str | None]
) -> AsyncIterator[FederatedTools]:
    async with served(cyberwealth_server(), headers) as app:
        tools = await build_federation(
            settings(
                federation_servers="cyberwealth=http://127.0.0.1/mcp",
                federation_tool_allowlist=(
                    "cyberwealth:intel_server_info:ro,cyberwealth:my_context:ro"
                ),
            ),
            transport=FakeIssuer().transport,
            auth_environ=ENV,
            mcp_transport=httpx2.ASGITransport(app=gated(app, {SERVICE, f"Bearer {KEY}"})),
            personal_keys=keys,
        )
        assert tools is not None
        async with tools.federation:
            yield tools


async def test_a_personal_call_in_a_dm_carries_the_askers_own_key() -> None:
    headers: list[str | None] = []
    async with cyberwealth(await keyed_keys(alice=KEY), headers) as tools:
        before = len(headers)
        outcome = await tools.invoker.invoke(request("my_context"), offered(tools))
        during = headers[before:]

    assert outcome.invoked
    assert outcome.result is not None and "Household: Silva" in outcome.result.text
    assert during and set(during) == {f"Bearer {KEY}"}
    # The shared connection never carried it.
    assert headers[:before] and set(headers[:before]) == {SERVICE}


async def test_an_intel_call_in_a_dm_carries_the_service_identity_not_the_key() -> None:
    headers: list[str | None] = []
    async with cyberwealth(await keyed_keys(alice=KEY), headers) as tools:
        outcome = await tools.invoker.invoke(request("intel_server_info"), offered(tools))
    assert outcome.invoked
    assert f"Bearer {KEY}" not in headers


async def test_somebody_without_a_key_is_refused_and_nothing_is_sent() -> None:
    headers: list[str | None] = []
    async with cyberwealth(await keyed_keys(alice=KEY), headers) as tools:
        before = len(headers)
        outcome = await tools.invoker.invoke(
            request("my_context", requester=BOB), offered(tools)
        )
        sent = headers[before:]

    assert not outcome.invoked
    assert outcome.result is not None and outcome.result.failure is Failure.NO_PERSONAL_KEY
    assert "DM" in (outcome.notice() or "")
    assert sent == []
    assert tools.audit.entries()[-1].outcome is AuditOutcome.REFUSED


async def test_the_key_is_never_looked_up_for_a_channel_answer() -> None:
    headers: list[str | None] = []
    async with cyberwealth(await keyed_keys(alice=KEY), headers) as tools:
        before = len(headers)
        outcome = await tools.invoker.invoke(
            request("my_context", direct=False), offered(tools)
        )
        sent = headers[before:]
    assert outcome.decision.refusal is Refusal.PERSONAL_OUTSIDE_DIRECT
    assert sent == []


async def test_a_revoked_key_is_reported_as_such_and_costs_nobody_else() -> None:
    headers: list[str | None] = []
    async with cyberwealth(await keyed_keys(alice=REVOKED), headers) as tools:
        with capture_logs() as logs:
            outcome = await tools.invoker.invoke(request("my_context"), offered(tools))
        intel = await tools.invoker.invoke(
            request("intel_server_info", requester=BOB, direct=False), offered(tools)
        )

    assert outcome.result is not None and outcome.result.failure is Failure.KEY_REJECTED
    assert "refused your key" in (outcome.notice() or "")
    # One person's expired key is not the server going away.
    assert tools.federation.lost_servers == {}
    assert intel.invoked
    assert REVOKED not in json.dumps(logs, default=str)


# --- registration --------------------------------------------------------------


CW = ServerConfig(name="cyberwealth", target="https://cw.example/mcp", timeout_seconds=5)


def cw_read(tool: str) -> AllowedTool:
    return AllowedTool(
        server="cyberwealth",
        tool=tool,
        credential=CredentialScope.NARROW_READ_ONLY,
        effect=ToolEffect.READ_ONLY,
    )


async def test_a_personal_tool_the_service_cannot_list_is_registered_for_keyed_calls() -> None:
    """CyberWealth lists only what the credential asking may call, and the
    service principal sees `intel_*`: `my_*` is still callable with a key."""
    session = FakeSession(tools=(read_tool("intel_server_info"),))
    config = FederationConfig(
        servers=(CW,),
        allowlist=(cw_read("intel_server_info"), cw_read("my_unpaid_bills")),
        personal_key_servers=frozenset({"cyberwealth"}),
    )
    federation = await connect(config, session_factory({"cyberwealth": session}))
    async with federation:
        registered = federation.registration.get("cyberwealth:my_unpaid_bills")
    assert registered is not None
    assert registered.permit.personal and registered.permit.effect is ToolEffect.READ_ONLY
    assert "my unpaid bills" in registered.description


async def test_without_keys_an_unlisted_tool_is_still_a_startup_error() -> None:
    session = FakeSession(tools=(read_tool("intel_server_info"),))
    config = FederationConfig(
        servers=(CW,), allowlist=(cw_read("intel_server_info"), cw_read("my_unpaid_bills"))
    )
    with pytest.raises(FederationStartupError):
        await connect(config, session_factory({"cyberwealth": session}))


async def test_an_unlisted_intel_tool_is_still_a_startup_error() -> None:
    session = FakeSession(tools=())
    config = FederationConfig(
        servers=(CW,),
        allowlist=(cw_read("intel_asset_price"),),
        personal_key_servers=frozenset({"cyberwealth"}),
    )
    with pytest.raises(FederationStartupError):
        await connect(config, session_factory({"cyberwealth": session}))


# --- composition -----------------------------------------------------------------


async def test_keys_are_turned_on_for_cyberwealth_over_https_only() -> None:
    keys = await keyed_keys()
    https = FederationConfig(servers=(CW,))
    plain = FederationConfig(
        servers=(ServerConfig(name="cyberwealth", target="http://cw.example/mcp"),)
    )
    other = FederationConfig(servers=(ServerConfig(name="issues", target="https://i.example"),))

    config, keyed = with_personal_keys(https, keys)
    assert keyed is not None and config is not None
    assert config.personal_key_servers == {"cyberwealth"}
    assert config.server("cyberwealth") == CW

    with capture_logs() as logs:
        assert with_personal_keys(plain, keys) == (plain, None)
    assert any(e["event"] == "composition.federation.personal_keys_refused" for e in logs)
    assert with_personal_keys(other, keys) == (other, None)
    assert with_personal_keys(https, None) == (https, None)
