"""Allowlist, namespacing, and the two very different ways a tool goes missing."""

from __future__ import annotations

import pytest

from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    ConfigurationError,
    FederationConfig,
    ServerConfig,
    qualify,
    split_qualified,
)
from chatmemory.adapters.mcp_client.registry import (
    FederationStartupError,
    ServerDiscovery,
    register,
)
from chatmemory.app.authorization import CredentialScope, ToolEffect
from tests.unit.test_federation_support import read_tool, silent_tool, write_tool

GITHUB = ServerConfig(name="github", target="https://github.example/mcp")
LINEAR = ServerConfig(name="linear", target="https://linear.example/mcp")


def config(*allowed: AllowedTool, servers: tuple[ServerConfig, ...] = (GITHUB, LINEAR)):
    return FederationConfig(servers=servers, allowlist=allowed)


# --- discovery does not confer availability ----------------------------


def test_discovered_but_unlisted_tool_is_not_registered() -> None:
    """The server offers three tools; the operator wrote down one."""
    registration = register(
        config(AllowedTool(server="github", tool="search_issues")),
        [
            ServerDiscovery(
                "github",
                (read_tool("search_issues"), write_tool("close_issue"), read_tool("list_repos")),
            ),
            ServerDiscovery("linear"),
        ],
    )
    assert registration.names == {"github:search_issues"}


def test_a_tool_added_after_deployment_stays_unavailable() -> None:
    """Rediscovery must not be a way to gain capability without a config change."""
    allowed = config(AllowedTool(server="github", tool="search_issues"))
    before = register(allowed, [ServerDiscovery("github", (read_tool("search_issues"),))])
    after = register(
        allowed,
        [ServerDiscovery("github", (read_tool("search_issues"), write_tool("delete_repo")))],
    )
    assert before.names == after.names == {"github:search_issues"}


def test_listed_tool_missing_from_a_reachable_server_is_a_startup_error() -> None:
    """Silently operating without it is the failure mode this replaces."""
    with pytest.raises(FederationStartupError) as raised:
        register(
            config(AllowedTool(server="github", tool="search_issues")),
            [ServerDiscovery("github", (read_tool("list_repos"),))],
        )
    assert "github:search_issues" in str(raised.value)


def test_listed_tool_behind_an_unreachable_server_degrades_instead() -> None:
    """A restarting server must not stop the whole deployment from booting.

    This is the one case that looks identical to the previous test from a
    distance -- a listed tool nobody provides -- and must not behave the same.
    """
    registration = register(
        config(AllowedTool(server="github", tool="search_issues")),
        [ServerDiscovery("github", failure="connection refused")],
    )
    assert registration.names == frozenset()
    assert registration.unavailable_tools == ("github:search_issues",)
    assert registration.unreachable_servers == ("github",)
    assert registration.degraded


def test_one_unreachable_server_does_not_hide_a_healthy_one() -> None:
    registration = register(
        config(
            AllowedTool(server="github", tool="search_issues"),
            AllowedTool(server="linear", tool="search"),
        ),
        [
            ServerDiscovery("github", failure="connection refused"),
            ServerDiscovery("linear", (read_tool("search"),)),
        ],
    )
    assert registration.names == {"linear:search"}
    assert registration.unavailable_tools == ("github:search_issues",)


# --- namespacing -------------------------------------------------------


def test_same_named_tools_on_two_servers_do_not_collide() -> None:
    registration = register(
        config(
            AllowedTool(server="github", tool="search"),
            AllowedTool(server="linear", tool="search"),
        ),
        [
            ServerDiscovery("github", (read_tool("search", "search github"),)),
            ServerDiscovery("linear", (read_tool("search", "search linear"),)),
        ],
    )
    assert registration.names == {"github:search", "linear:search"}
    github = registration.get("github:search")
    linear = registration.get("linear:search")
    assert github is not None and linear is not None
    # Dispatch is by the qualified name, and each half survives the round trip.
    assert (github.permit.server, github.permit.tool) == ("github", "search")
    assert (linear.permit.server, linear.permit.tool) == ("linear", "search")
    assert split_qualified(qualify("linear", "search")) == ("linear", "search")


def test_a_server_name_cannot_forge_a_qualifier() -> None:
    with pytest.raises(ConfigurationError):
        ServerConfig(name="git:hub", target="x")
    with pytest.raises(ConfigurationError):
        AllowedTool(server="github", tool="search:issues")


# --- effect classification ---------------------------------------------


def test_unannotated_tool_counts_as_mutating() -> None:
    registration = register(
        config(AllowedTool(server="github", tool="run_workflow")),
        [ServerDiscovery("github", (silent_tool("run_workflow"),))],
    )
    tool = registration.get("github:run_workflow")
    assert tool is not None
    assert tool.permit.effect is ToolEffect.UNDETERMINED
    assert tool.permit.effect.mutates


def test_a_server_may_tighten_but_not_loosen_the_operators_declaration() -> None:
    """A compromised server claiming readOnlyHint must not skip confirmation."""
    tightened = register(
        config(
            AllowedTool(
                server="github",
                tool="sync",
                credential=CredentialScope.PER_REQUESTER,
                effect=ToolEffect.MUTATING,
                mutation_enabled=True,
            )
        ),
        [ServerDiscovery("github", (read_tool("sync"),))],
    )
    entry = tightened.get("github:sync")
    assert entry is not None
    assert entry.permit.effect is ToolEffect.MUTATING

    loosened = register(
        config(AllowedTool(server="github", tool="peek", effect=ToolEffect.READ_ONLY)),
        [ServerDiscovery("github", (write_tool("peek"),))],
    )
    peek = loosened.get("github:peek")
    assert peek is not None
    assert peek.permit.effect is ToolEffect.MUTATING


def test_operator_silence_defers_to_the_servers_annotation() -> None:
    registration = register(
        config(AllowedTool(server="github", tool="search")),
        [ServerDiscovery("github", (read_tool("search"),))],
    )
    tool = registration.get("github:search")
    assert tool is not None
    assert tool.permit.effect is ToolEffect.READ_ONLY
    assert not tool.permit.effect.mutates


# --- configuration validated at load -----------------------------------


def test_allowlist_naming_an_unconfigured_server_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not configured"):
        FederationConfig(servers=(GITHUB,), allowlist=(AllowedTool("jira", "search"),))


def test_duplicate_allowlist_entries_are_rejected() -> None:
    entry = AllowedTool(server="github", tool="search")
    with pytest.raises(ConfigurationError, match="duplicate"):
        FederationConfig(servers=(GITHUB,), allowlist=(entry, entry))


def test_ambient_credentials_cannot_be_configured() -> None:
    """Every server member would wield them through the bot."""
    with pytest.raises(ConfigurationError, match="ambient"):
        AllowedTool(server="github", tool="search", credential=CredentialScope.AMBIENT)


def test_a_read_only_identity_cannot_be_granted_mutation() -> None:
    with pytest.raises(ConfigurationError, match="read-only identity"):
        AllowedTool(
            server="github",
            tool="close_issue",
            credential=CredentialScope.NARROW_READ_ONLY,
            mutation_enabled=True,
        )


def test_declaring_read_only_while_enabling_mutation_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="declared read-only"):
        AllowedTool(
            server="github",
            tool="search",
            credential=CredentialScope.PER_REQUESTER,
            effect=ToolEffect.READ_ONLY,
            mutation_enabled=True,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tools_per_run": 0},
        {"max_result_chars": 0},
        {"call_timeout_seconds": 0.0},
    ],
)
def test_budgets_must_be_positive(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        FederationConfig(servers=(GITHUB,), **kwargs)  # type: ignore[arg-type]


def test_empty_configuration_is_valid_and_means_no_federation() -> None:
    registration = register(FederationConfig(), [])
    assert registration.names == frozenset()
    assert not registration.degraded
