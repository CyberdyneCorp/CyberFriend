"""Service authentication for a federated server, and CyberWealth's personal rule.

The bearer is minted with CyberdyneAuth `client_credentials`, cached, refreshed
on expiry and on a 401, and sent on its own server's MCP requests only -- the
last proved against a real MCP server over ASGI, with a second server beside
it that must never see the token.

`my_*` tools and results marked personal are answered only in a DM; results
from the server are cited under its name and never remembered.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from structlog.testing import capture_logs

from chatmemory.adapters.mcp_client.client import Failure, connect
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    ConfigurationError,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.adapters.mcp_client.service_auth import (
    ServiceBearerAuth,
    ServiceCredential,
    ServiceTokenSource,
    ServiceTokenUnavailable,
    authenticated_session_factory,
    env_name,
    service_credentials,
)
from chatmemory.adapters.mcp_client.session import ToolResult, is_personal
from chatmemory.app.audit import AuditOutcome
from chatmemory.app.authorization import (
    ActionOrigin,
    CredentialScope,
    InvocationRequest,
    Refusal,
    ToolEffect,
)
from chatmemory.app.memory import PUBLIC_SOURCE_SYSTEMS, answer_provenance
from chatmemory.app.reasoning.evidence import SourcedCitation
from chatmemory.composition import build_federation
from chatmemory.config import federation_auth_environment
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef
from chatmemory.ports.answers import Answer
from chatmemory.ports.memory import ConversationLocation, Recollection
from tests.unit.test_composition import settings
from tests.unit.test_federation_support import (
    ALICE,
    FakeSession,
    build_stack,
    read_tool,
)

ISSUER = "https://auth.example"
TOKEN_ENDPOINT = f"{ISSUER}/api/v1/auth/oauth2/token"
CW = ServerConfig(name="cyberwealth", target="https://cw.example/mcp", timeout_seconds=5)
OTHER = ServerConfig(name="issues", target="https://issues.example/mcp", timeout_seconds=5)
SECRET = "s3cr3t-value"

ENV = {
    "FEDERATION_AUTH_CYBERWEALTH_ISSUER": ISSUER,
    "FEDERATION_AUTH_CYBERWEALTH_CLIENT_ID": "cyberfriend",
    "FEDERATION_AUTH_CYBERWEALTH_CLIENT_SECRET": SECRET,
    "FEDERATION_AUTH_CYBERWEALTH_AUDIENCE": "cyberwealth",
    "FEDERATION_AUTH_CYBERWEALTH_SCOPE": "cyberwealth:intel.read",
}

CREDENTIAL = ServiceCredential(
    server="cyberwealth",
    issuer=ISSUER,
    client_id="cyberfriend",
    client_secret=SECRET,
    audience="cyberwealth",
    scope="cyberwealth:intel.read",
)


# --- configuration -----------------------------------------------------


def test_a_full_credential_is_read_for_its_server() -> None:
    found = service_credentials((CW, OTHER), ENV)
    assert set(found) == {"cyberwealth"}
    assert found["cyberwealth"] == CREDENTIAL


def test_audience_and_scope_are_optional() -> None:
    env = {k: v for k, v in ENV.items() if not k.endswith(("_AUDIENCE", "_SCOPE"))}
    found = service_credentials((CW,), env)["cyberwealth"]
    assert found.audience is None and found.scope is None


def test_a_hyphenated_server_is_named_with_underscores() -> None:
    server = ServerConfig(name="cyber-wealth", target="https://cw.example/mcp")
    env = {k.replace("CYBERWEALTH", env_name("cyber-wealth")): v for k, v in ENV.items()}
    assert "FEDERATION_AUTH_CYBER_WEALTH_CLIENT_ID" in env
    assert set(service_credentials((server,), env)) == {"cyber-wealth"}


def test_no_variables_means_no_credentials() -> None:
    assert service_credentials((CW,), {"UNRELATED": "x"}) == {}


def test_a_credential_for_an_unconfigured_server_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="no configured federation server"):
        service_credentials((OTHER,), ENV)


@pytest.mark.parametrize("dropped", ["_ISSUER", "_CLIENT_ID", "_CLIENT_SECRET"])
def test_a_half_written_credential_is_refused(dropped: str) -> None:
    env = {k: v for k, v in ENV.items() if not k.endswith(dropped)}
    with pytest.raises(ConfigurationError, match="is missing"):
        service_credentials((CW,), env)


def test_a_plain_http_issuer_is_refused() -> None:
    env = {**ENV, "FEDERATION_AUTH_CYBERWEALTH_ISSUER": "http://auth.example"}
    with pytest.raises(ConfigurationError, match="issuer is not https"):
        service_credentials((CW,), env)


def test_a_plain_http_target_is_refused() -> None:
    server = ServerConfig(name="cyberwealth", target="http://cw.example/mcp")
    with pytest.raises(ConfigurationError, match="target is not https"):
        service_credentials((server,), ENV)


def test_localhost_may_use_plain_http() -> None:
    server = ServerConfig(name="cyberwealth", target="http://127.0.0.1:8000/mcp")
    env = {**ENV, "FEDERATION_AUTH_CYBERWEALTH_ISSUER": "http://localhost:9000"}
    assert set(service_credentials((server,), env)) == {"cyberwealth"}


def test_the_secret_is_not_in_the_repr() -> None:
    assert SECRET not in repr(CREDENTIAL)


# --- the token source --------------------------------------------------


class FakeIssuer:
    """Discovery and a token endpoint that hands out numbered tokens."""

    def __init__(
        self,
        *,
        expires_in: object = 3600,
        refuse: bool = False,
        methods: tuple[str, ...] = ("client_secret_basic", "client_secret_post"),
        issuer: str = ISSUER,
    ) -> None:
        self.expires_in = expires_in
        self.refuse = refuse
        self.methods = methods
        self.issuer = issuer
        self.grants: list[tuple[dict[str, list[str]], str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "token_endpoint_auth_methods_supported": list(self.methods),
                },
            )
        assert str(request.url) == TOKEN_ENDPOINT
        self.grants.append(
            (parse_qs(request.content.decode()), request.headers.get("authorization"))
        )
        if self.refuse:
            return httpx.Response(401, json={"error": "invalid_client"})
        body: dict[str, object] = {
            "access_token": f"token-{len(self.grants)}",
            "token_type": "Bearer",
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_token_is_minted_with_client_credentials_audience_and_scope() -> None:
    issuer = FakeIssuer()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)

    assert await source.bearer() == "token-1"

    form, authorization = issuer.grants[0]
    assert form == {
        "grant_type": ["client_credentials"],
        "audience": ["cyberwealth"],
        "scope": ["cyberwealth:intel.read"],
    }
    assert authorization == "Basic " + base64.b64encode(f"cyberfriend:{SECRET}".encode()).decode()


async def test_client_secret_post_when_it_is_all_the_issuer_offers() -> None:
    issuer = FakeIssuer(methods=("client_secret_post",))
    await ServiceTokenSource(CREDENTIAL, transport=issuer.transport).bearer()
    form, authorization = issuer.grants[0]
    assert authorization is None
    assert form["client_id"] == ["cyberfriend"] and form["client_secret"] == [SECRET]


async def test_a_token_is_reused_until_its_expiry_margin() -> None:
    issuer, clock = FakeIssuer(expires_in=3600), Clock()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport, monotonic=clock)

    assert await source.bearer() == "token-1"
    clock.now += 3600 - 61
    assert await source.bearer() == "token-1"
    assert len(issuer.grants) == 1

    clock.now += 2  # inside the 60 s margin
    assert await source.bearer() == "token-2"


async def test_a_missing_expires_in_gets_a_short_default_lifetime() -> None:
    issuer, clock = FakeIssuer(expires_in=None), Clock()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport, monotonic=clock)
    await source.bearer()
    clock.now += 300
    assert await source.bearer() == "token-2"


async def test_invalidating_the_cached_token_mints_another() -> None:
    issuer = FakeIssuer()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)
    stale = await source.bearer()
    source.invalidate("some-other-token")
    assert await source.bearer() == stale
    source.invalidate(stale)
    assert await source.bearer() == "token-2"


async def test_a_refused_grant_raises_and_logs_neither_secret_nor_token() -> None:
    issuer = FakeIssuer(refuse=True)
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)
    with capture_logs() as logs, pytest.raises(ServiceTokenUnavailable, match="invalid_client"):
        await source.bearer()
    assert logs and SECRET not in json.dumps(logs, default=str)


async def test_discovery_naming_another_issuer_is_refused() -> None:
    issuer = FakeIssuer(issuer="https://evil.example")
    with pytest.raises(ServiceTokenUnavailable, match="different issuer"):
        await ServiceTokenSource(CREDENTIAL, transport=issuer.transport).bearer()
    assert issuer.grants == []


async def test_an_unreachable_issuer_raises_unavailable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = ServiceTokenSource(CREDENTIAL, transport=httpx.MockTransport(down))
    with pytest.raises(ServiceTokenUnavailable, match="unreachable"):
        await source.bearer()


# --- the bearer on the wire ---------------------------------------------


async def test_the_bearer_is_set_and_a_401_is_retried_once_with_a_fresh_token() -> None:
    issuer = FakeIssuer()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)
    seen: list[str | None] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(401 if len(seen) == 1 else 200)

    async with httpx2.AsyncClient(
        auth=ServiceBearerAuth(source), transport=httpx2.MockTransport(server)
    ) as http:
        response = await http.post("https://cw.example/mcp", json={"x": 1})

    assert response.status_code == 200
    assert seen == ["Bearer token-1", "Bearer token-2"]


async def test_a_second_401_is_returned_rather_than_retried_again() -> None:
    issuer = FakeIssuer()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)
    calls: list[int] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(401)

    async with httpx2.AsyncClient(
        auth=ServiceBearerAuth(source), transport=httpx2.MockTransport(server)
    ) as http:
        response = await http.post("https://cw.example/mcp", json={})

    assert response.status_code == 401
    assert len(calls) == 2


ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


def cyberwealth_server() -> MCPServer:
    server: MCPServer = MCPServer(name="cyberwealth", version="1.0.0")

    @server.tool(name="intel_server_info", description="CyberWealth server version")
    async def intel_server_info() -> str:
        return "CyberWealth MCP 1.0"

    @server.tool(name="my_context", description="who this key acts for")
    async def my_context() -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="Household: Silva")],
            structured_content={"visibility": "personal", "household": "Silva"},
        )

    return server


@asynccontextmanager
async def served(server: MCPServer, headers: list[str | None]) -> AsyncIterator[ASGIApp]:
    """The server's streamable-HTTP app, recording each request's Authorization."""
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def recording(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope["type"] == "http":
            raw = dict(scope["headers"]).get(b"authorization")
            headers.append(raw.decode() if raw else None)
        await app(scope, receive, send)

    async with server.session_manager.run():
        yield recording


LOCAL_CW = ServerConfig(name="cyberwealth", target="http://127.0.0.1/mcp", timeout_seconds=5)
INTEL_AND_SEARCH = (
    AllowedTool(server="cyberwealth", tool="intel_server_info", effect=ToolEffect.READ_ONLY),
    AllowedTool(server="issues", tool="search", effect=ToolEffect.READ_ONLY),
)


async def test_a_real_mcp_server_sees_the_bearer_and_the_other_server_does_not() -> None:
    issuer = FakeIssuer()
    source = ServiceTokenSource(CREDENTIAL, transport=issuer.transport)
    cw_headers: list[str | None] = []
    other = FakeSession(tools=(read_tool("search"),))

    @asynccontextmanager
    async def fallback(config: ServerConfig) -> AsyncIterator[FakeSession]:
        assert config.name == "issues"
        yield other

    async with served(cyberwealth_server(), cw_headers) as app:
        factory = authenticated_session_factory(
            {"cyberwealth": source}, fallback, http_transport=httpx2.ASGITransport(app=app)
        )
        federation = await connect(
            FederationConfig(
                servers=(LOCAL_CW, OTHER),
                allowlist=INTEL_AND_SEARCH,
            ),
            factory,
        )
        async with federation:
            assert federation.registration.names == {
                "cyberwealth:intel_server_info",
                "issues:search",
            }
            result = await federation.call(
                federation.permits["cyberwealth:intel_server_info"], {}
            )

    assert result.ok and "CyberWealth MCP 1.0" in result.text
    assert not result.personal
    assert cw_headers and set(cw_headers) == {"Bearer token-1"}
    assert len(issuer.grants) == 1  # one token for the handshake, listing and call


async def test_an_issuer_that_refuses_leaves_the_server_unreachable_not_the_boot() -> None:
    source = ServiceTokenSource(CREDENTIAL, transport=FakeIssuer(refuse=True).transport)
    other = FakeSession(tools=(read_tool("search"),))

    @asynccontextmanager
    async def fallback(config: ServerConfig) -> AsyncIterator[FakeSession]:
        yield other

    async with served(cyberwealth_server(), []) as app:
        federation = await connect(
            FederationConfig(
                servers=(LOCAL_CW, OTHER),
                allowlist=INTEL_AND_SEARCH,
            ),
            authenticated_session_factory(
                {"cyberwealth": source}, fallback, http_transport=httpx2.ASGITransport(app=app)
            ),
        )
        async with federation:
            assert federation.unreachable_servers == ("cyberwealth",)
            assert federation.registration.names == {"issues:search"}


async def test_the_session_reads_visibility_from_structured_content() -> None:
    source = ServiceTokenSource(CREDENTIAL, transport=FakeIssuer().transport)
    async with served(cyberwealth_server(), []) as app:
        factory = authenticated_session_factory(
            {"cyberwealth": source}, http_transport=httpx2.ASGITransport(app=app)
        )
        async with factory(LOCAL_CW) as session:
            mine = await session.call_tool("my_context", {})
            intel = await session.call_tool("intel_server_info", {})
    assert mine.personal and "Household: Silva" in mine.text
    assert not intel.personal


# --- the composition root ------------------------------------------------


async def test_build_federation_sends_the_bearer_from_the_environment() -> None:
    issuer = FakeIssuer()
    headers: list[str | None] = []
    async with served(cyberwealth_server(), headers) as app:
        tools = await build_federation(
            settings(
                federation_servers="cyberwealth=http://127.0.0.1/mcp",
                federation_tool_allowlist="cyberwealth:intel_server_info:ro",
            ),
            transport=issuer.transport,
            auth_environ=ENV,
            mcp_transport=httpx2.ASGITransport(app=app),
        )
        assert tools is not None
        async with tools.federation:
            assert tools.federation.registration.names == {"cyberwealth:intel_server_info"}
    assert headers and set(headers) == {"Bearer token-1"}


async def test_a_bad_credential_drops_the_remote_servers_with_an_error() -> None:
    with capture_logs() as logs:
        tools = await build_federation(
            settings(
                federation_servers="issues=https://issues.example/mcp",
                federation_tool_allowlist="issues:search:ro",
            ),
            auth_environ=ENV,  # names cyberwealth, which is not configured
        )
    assert tools is None  # issues was the only server, and nothing local is on
    assert any(e["event"] == "composition.federation.service_auth_misconfigured" for e in logs)
    assert SECRET not in json.dumps(logs, default=str)


async def test_a_bad_credential_keeps_the_local_tools() -> None:
    """A refused credential costs the remote servers it was for, not Wikipedia."""
    with capture_logs() as logs:
        tools = await build_federation(
            settings(
                federation_servers="issues=https://issues.example/mcp",
                federation_tool_allowlist="issues:search:ro",
                web_tools_enabled=True,
            ),
            auth_environ=ENV,  # names cyberwealth, which is not configured
        )
    assert tools is not None
    try:
        assert "wikipedia:search" in tools.federation.permits
        assert not any(name.startswith("issues:") for name in tools.federation.permits)
    finally:
        await tools.federation.aclose()
    refused = [e for e in logs if e["event"] == "composition.federation.service_auth_misconfigured"]
    assert refused and refused[0]["dropped"] == ["issues"]
    assert SECRET not in json.dumps(logs, default=str)


def test_the_process_environment_supplies_credentials_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only production source of the credentials: `.env` under the process."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "FEDERATION_AUTH_CYBERWEALTH_CLIENT_ID=from-dotenv\n"
        "FEDERATION_AUTH_CYBERWEALTH_AUDIENCE=cyberwealth\n"
        "DISCORD_TOKEN=not-a-federation-key\n"
    )
    monkeypatch.setenv("FEDERATION_AUTH_CYBERWEALTH_CLIENT_ID", "from-process")
    monkeypatch.setenv("FEDERATION_AUTH_CYBERWEALTH_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("FEDERATION_SERVERS", "not-a-federation-key-either")

    found = federation_auth_environment(str(dotenv))

    assert found["FEDERATION_AUTH_CYBERWEALTH_CLIENT_ID"] == "from-process"
    assert found["FEDERATION_AUTH_CYBERWEALTH_CLIENT_SECRET"] == SECRET
    assert found["FEDERATION_AUTH_CYBERWEALTH_AUDIENCE"] == "cyberwealth"
    assert all(key.startswith("FEDERATION_AUTH_") for key in found)


# --- personal tools and results ------------------------------------------


def ask(tool: str, *, direct: bool) -> InvocationRequest:
    return InvocationRequest(
        requester=ALICE,
        question="what is my household",
        qualified_name=f"cyberwealth:{tool}",
        arguments={},
        origin=ActionOrigin.REQUESTER_REQUEST,
        private=direct,
        direct=direct,
    )


def cw_read(tool: str) -> AllowedTool:
    return AllowedTool(
        server="cyberwealth",
        tool=tool,
        credential=CredentialScope.NARROW_READ_ONLY,
        effect=ToolEffect.READ_ONLY,
    )


class LabellingSession(FakeSession):
    """A fake that reads `[personal]` the way the real session does."""

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        result = await super().call_tool(name, arguments)
        return ToolResult(text=result.text, personal=is_personal(result.text, None))


async def personal_stack(session: FakeSession) -> Any:
    return await build_stack(
        (CW,), (cw_read("my_context"), cw_read("intel_server_info")), {"cyberwealth": session}
    )


def all_offered(stack: Any) -> RoutedTools:
    return RoutedTools(question="", tools=stack.federation.registration.tools)


def test_a_my_tool_is_personal_and_an_intel_tool_is_not() -> None:
    assert cw_read("my_context").personal
    assert not cw_read("intel_server_info").personal


async def test_a_personal_tool_is_refused_in_a_channel_without_calling_the_server() -> None:
    session = LabellingSession(tools=(read_tool("my_context"), read_tool("intel_server_info")))
    stack = await personal_stack(session)
    assert stack.federation.permits["cyberwealth:my_context"].personal

    outcome = await stack.invoker.invoke(ask("my_context", direct=False), all_offered(stack))

    assert outcome.decision.refusal is Refusal.PERSONAL_OUTSIDE_DIRECT
    assert session.calls == []
    assert stack.audit.entries()[-1].outcome is AuditOutcome.REFUSED


async def test_an_ephemeral_reply_is_not_a_dm() -> None:
    """Private but not direct: the partner rule is DMs only."""
    session = LabellingSession(tools=(read_tool("my_context"), read_tool("intel_server_info")))
    stack = await personal_stack(session)
    request = InvocationRequest(
        requester=ALICE,
        question="what is my household",
        qualified_name="cyberwealth:my_context",
        arguments={},
        origin=ActionOrigin.REQUESTER_REQUEST,
        private=True,
        direct=False,
    )
    outcome = await stack.invoker.invoke(request, all_offered(stack))
    assert outcome.decision.refusal is Refusal.PERSONAL_OUTSIDE_DIRECT
    assert session.calls == []


async def test_a_personal_tool_is_answered_in_a_dm() -> None:
    session = LabellingSession(
        tools=(read_tool("my_context"), read_tool("intel_server_info")),
        responses={"my_context": "[personal] Household: Silva"},
    )
    stack = await personal_stack(session)

    outcome = await stack.invoker.invoke(ask("my_context", direct=True), all_offered(stack))

    assert outcome.invoked
    assert outcome.result is not None and "Household: Silva" in outcome.result.text


async def test_a_personal_result_from_any_tool_is_withheld_in_a_channel() -> None:
    session = LabellingSession(
        tools=(read_tool("my_context"), read_tool("intel_server_info")),
        responses={"intel_server_info": "[personal] balance 1,234.00 BRL"},
    )
    stack = await personal_stack(session)

    outcome = await stack.invoker.invoke(
        ask("intel_server_info", direct=False), all_offered(stack)
    )

    assert session.call_names == ["intel_server_info"]
    assert not outcome.invoked
    assert outcome.evidence() is None
    assert outcome.result is not None
    assert outcome.result.failure is Failure.PERSONAL_WITHHELD
    assert outcome.result.text == ""
    assert stack.audit.entries()[-1].outcome is AuditOutcome.REFUSED


async def test_a_public_intel_result_is_used_in_a_channel() -> None:
    session = LabellingSession(
        tools=(read_tool("my_context"), read_tool("intel_server_info")),
        responses={"intel_server_info": "CyberWealth MCP 1.0"},
    )
    stack = await personal_stack(session)
    outcome = await stack.invoker.invoke(
        ask("intel_server_info", direct=False), all_offered(stack)
    )
    assert outcome.invoked
    assert outcome.result is not None and outcome.result.attribution == (
        "cyberwealth (intel_server_info)"
    )


@pytest.mark.parametrize(
    ("text", "structured", "expected"),
    [
        ("[personal] Household: Silva", None, True),
        ("  [personal] leading space", None, True),
        ("Household: Silva", {"visibility": "personal"}, True),
        ("CyberWealth MCP 1.0", {"visibility": "public"}, False),
        ("mentions [personal] later", None, False),
        ("plain", None, False),
    ],
)
def test_is_personal(text: str, structured: dict[str, object] | None, expected: bool) -> None:
    assert is_personal(text, structured) is expected


def test_only_a_dm_audience_is_direct() -> None:
    def audience(mode: DeliveryMode) -> Audience:
        return Audience(mode=mode, members=frozenset({ALICE}), readable_channels=frozenset())

    assert audience(DeliveryMode.DIRECT_MESSAGE).is_direct
    assert not audience(DeliveryMode.EPHEMERAL).is_direct
    assert not audience(DeliveryMode.PUBLIC_CHANNEL).is_direct


# --- memory and labelling -------------------------------------------------


def test_cyberwealth_is_not_a_public_source() -> None:
    assert "cyberwealth" not in PUBLIC_SOURCE_SYSTEMS


def test_a_turn_resting_on_cyberwealth_is_not_remembered() -> None:
    channel = ChannelRef("discord", 100)
    location = ConversationLocation(platform="discord", platform_location_id=100, direct=False)
    answer = Answer(
        text="CyberWealth MCP 1.0",
        citations=(
            SourcedCitation(
                channel=ChannelRef("cyberwealth", 0),
                message_id=0,
                author_display="cyberwealth (intel_server_info)",
                excerpt="CyberWealth MCP 1.0",
                url="",
                source_system="cyberwealth",
            ),
        ),
    )
    provenance = answer_provenance(
        location, answer, consulted=(channel, ChannelRef("cyberwealth", 0)),
        informed_by=Recollection(),
    )
    assert provenance is None


async def test_the_loop_marks_a_dm_run_direct_and_a_channel_run_not() -> None:
    from dataclasses import replace

    from tests.unit.test_loop_invocation import ISSUES, ScriptedSurface, build_loop
    from tests.unit.test_reasoning_fixed import question

    dm = ScriptedSurface(ISSUES)
    await build_loop(dm).run(question())
    channel = ScriptedSurface(ISSUES)
    in_channel = question()
    public = replace(in_channel.audience, mode=DeliveryMode.PUBLIC_CHANNEL)
    await build_loop(channel).run(replace(in_channel, audience=public))

    assert [r.direct for r in dm.invocations] == [True]
    assert [r.direct for r in channel.invocations] == [False]
