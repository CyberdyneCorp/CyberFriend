"""Connecting out, and surviving every way an external system fails.

The last test in this file runs against a *real* MCP server over the SDK's
in-process transport rather than a fake. That is deliberate: the fakes above
prove the degradation logic, and only a real handshake proves that the
session adapter reads annotations, content blocks and error flags the way the
wire actually delivers them.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from chatmemory.adapters.mcp_client.client import (
    Failure,
    Federation,
    connect,
    truncate,
)
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.registry import FederationStartupError
from chatmemory.adapters.mcp_client.session import (
    MCPToolSession,
    effect_of,
    open_session,
)
from chatmemory.app.authorization import ToolEffect
from tests.unit.test_federation_support import (
    FakeSession,
    read_tool,
    session_factory,
    write_tool,
)

GITHUB = ServerConfig(name="github", target="https://github.example/mcp", timeout_seconds=0.2)
LINEAR = ServerConfig(name="linear", target="https://linear.example/mcp", timeout_seconds=0.2)


def federation_config(**kwargs: object) -> FederationConfig:
    return FederationConfig(
        servers=(GITHUB, LINEAR),
        allowlist=(
            AllowedTool(server="github", tool="search_issues"),
            AllowedTool(server="linear", tool="search"),
        ),
        **kwargs,  # type: ignore[arg-type]
    )


# --- independent loading -----------------------------------------------


async def test_an_unreachable_server_does_not_hide_a_healthy_ones_tools() -> None:
    linear = FakeSession(tools=(read_tool("search"),))
    federation = await connect(federation_config(), session_factory({"linear": linear}))

    assert federation.registration.names == {"linear:search"}
    assert federation.unreachable_servers == ("github",)
    assert "github was unavailable" in " ".join(federation.degradation_notices())


async def test_a_hanging_handshake_does_not_delay_healthy_servers() -> None:
    """One server's timeout must cost its own budget, not everyone's."""

    class Hanging(FakeSession):
        async def list_tools(self) -> list:  # type: ignore[override]
            await asyncio.sleep(5)
            return []

    federation = await connect(
        federation_config(),
        session_factory({"github": Hanging(), "linear": FakeSession(tools=(read_tool("search"),))}),
    )
    assert federation.registration.names == {"linear:search"}
    assert federation.unreachable_servers == ("github",)


async def test_a_startup_error_closes_the_transports_it_opened() -> None:
    closed: list[str] = []

    class Closing(FakeSession):
        pass

    github = Closing(tools=(read_tool("list_repos"),))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory(config: ServerConfig):  # type: ignore[no-untyped-def]
        try:
            yield github
        finally:
            closed.append(config.name)

    with pytest.raises(FederationStartupError):
        await connect(
            FederationConfig(
                servers=(GITHUB,),
                allowlist=(AllowedTool(server="github", tool="search_issues"),),
            ),
            factory,
        )
    assert closed == ["github"]


# --- calling out -------------------------------------------------------


async def stack_with(session: FakeSession) -> Federation:
    return await connect(
        FederationConfig(
            servers=(GITHUB,),
            allowlist=(AllowedTool(server="github", tool="search_issues"),),
            max_result_chars=50,
        ),
        session_factory({"github": session}),
    )


async def test_a_successful_call_is_attributed_to_its_server() -> None:
    federation = await stack_with(
        FakeSession(tools=(read_tool("search_issues"),), responses={"search_issues": "#42 open"})
    )
    permit = federation.permits["github:search_issues"]
    result = await federation.call(permit, {"q": "auth"})

    assert result.ok and result.text == "#42 open"
    assert result.attribution == "github (search_issues)"
    assert result.notice() is None


async def test_a_call_that_exceeds_its_timeout_is_abandoned_and_disclosed() -> None:
    federation = await stack_with(
        FakeSession(tools=(read_tool("search_issues"),), hang_seconds=5)
    )
    permit = federation.permits["github:search_issues"]
    result = await federation.call(permit, {})

    assert not result.ok
    assert result.failure is Failure.TIMEOUT
    notice = result.notice() or ""
    # "did not respond" and "found nothing" must never read the same.
    assert "did not respond" in notice
    assert "missing from this answer" in notice


async def test_a_timeout_does_not_take_the_server_out_of_the_run() -> None:
    session = FakeSession(tools=(read_tool("search_issues"),), hang_seconds=5)
    federation = await stack_with(session)
    permit = federation.permits["github:search_issues"]

    assert (await federation.call(permit, {})).failure is Failure.TIMEOUT
    session.hang_seconds = 0
    assert (await federation.call(permit, {})).ok


async def test_a_server_dying_mid_run_degrades_rather_than_reporting_no_information() -> None:
    session = FakeSession(tools=(read_tool("search_issues"),), die_after=1)
    federation = await stack_with(session)
    permit = federation.permits["github:search_issues"]

    assert (await federation.call(permit, {})).ok
    second = await federation.call(permit, {})
    assert second.failure is Failure.UNAVAILABLE
    assert second.text == ""
    assert "stopped responding" in " ".join(federation.degradation_notices())

    # Further calls short-circuit rather than re-hammering a dead server.
    third = await federation.call(permit, {})
    assert third.failure is Failure.UNAVAILABLE
    assert session.call_names == ["search_issues", "search_issues"]


async def test_a_tool_error_is_a_failure_not_an_empty_result() -> None:
    federation = await stack_with(
        FakeSession(tools=(read_tool("search_issues"),), errors={"search_issues"})
    )
    permit = federation.permits["github:search_issues"]
    result = await federation.call(permit, {})
    assert not result.ok and result.failure is Failure.TOOL_ERROR
    assert "reported an error" in (result.notice() or "")


async def test_an_oversized_result_is_truncated_and_the_answer_says_so() -> None:
    federation = await stack_with(
        FakeSession(tools=(read_tool("search_issues"),), responses={"search_issues": "x" * 500})
    )
    permit = federation.permits["github:search_issues"]
    result = await federation.call(permit, {})

    assert result.truncated
    assert len(result.text) < 500
    assert "truncated" in (result.notice() or "")
    # The truncation survives into the fenced evidence the model sees, so it
    # cannot read a clipped list as a complete one.
    assert "truncated" in result.as_evidence().render()


def test_truncate_reports_whether_anything_was_dropped() -> None:
    assert truncate("short", 10) == ("short", False)
    clipped, dropped = truncate("0123456789", 4)
    assert dropped and clipped.startswith("0123")


async def test_a_call_to_a_server_that_never_connected_fails_closed() -> None:
    federation = await connect(federation_config(), session_factory({}))
    from chatmemory.app.authorization import CredentialScope, ToolPermit

    permit = ToolPermit(
        qualified_name="github:search_issues",
        server="github",
        tool="search_issues",
        effect=ToolEffect.READ_ONLY,
        credential=CredentialScope.NARROW_READ_ONLY,
    )
    result = await federation.call(permit, {})
    assert result.failure is Failure.UNAVAILABLE


# --- the session adapter, against a real server ------------------------


def build_probe_server() -> MCPServer:
    server: MCPServer = MCPServer(name="probe", version="0.1.0")

    @server.tool(
        name="search_issues",
        description="Search issues by text",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def search_issues(q: str) -> str:
        return f"issue matching {q}"

    @server.tool(name="close_issue", description="Close an issue")
    async def close_issue(number: int) -> str:
        return f"closed {number}"

    return server


async def test_session_adapter_against_a_real_mcp_server() -> None:
    """A genuine handshake, list_tools and call_tool over the SDK transport."""
    async with open_session(Client(build_probe_server())) as session:
        discovered = {t.name: t for t in await session.list_tools()}
        assert discovered["search_issues"].effect is ToolEffect.READ_ONLY
        # The server annotated nothing for close_issue, so it is undetermined
        # -- and undetermined behaves as mutating everywhere it is decided on.
        assert discovered["close_issue"].effect is ToolEffect.UNDETERMINED
        assert discovered["close_issue"].effect.mutates

        result = await session.call_tool("search_issues", {"q": "auth"})
        assert "issue matching auth" in result.text
        assert not result.is_error


async def test_federation_end_to_end_against_a_real_mcp_server() -> None:
    """connect -> register -> call, with nothing faked below the transport."""
    probe = build_probe_server()
    server = ServerConfig(name="probe", target="in-process", timeout_seconds=5)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory(config: ServerConfig):  # type: ignore[no-untyped-def]
        async with open_session(Client(probe)) as session:
            yield session

    federation = await connect(
        FederationConfig(
            servers=(server,),
            allowlist=(AllowedTool(server="probe", tool="search_issues"),),
        ),
        factory,
    )
    async with federation:
        # close_issue exists on the server and is deliberately absent here.
        assert federation.registration.names == {"probe:search_issues"}
        result = await federation.call(
            federation.permits["probe:search_issues"], {"q": "login"}
        )
        assert result.ok
        assert "issue matching login" in result.text


def test_effect_of_reads_a_missing_hint_as_undetermined() -> None:
    assert effect_of(None) is ToolEffect.UNDETERMINED
    assert effect_of(ToolAnnotations()) is ToolEffect.UNDETERMINED
    assert effect_of(ToolAnnotations(readOnlyHint=True)) is ToolEffect.READ_ONLY
    assert effect_of(ToolAnnotations(readOnlyHint=False)) is ToolEffect.MUTATING


def test_mcp_tool_session_is_a_tool_session() -> None:
    from chatmemory.adapters.mcp_client.session import ToolSession

    session: ToolSession = MCPToolSession(Client(build_probe_server()))
    assert session is not None


def test_write_tool_helper_is_mutating() -> None:
    assert write_tool("x").effect.mutates
