"""The federation screen: validate before accepting, and confirm before widening.

Every test here is about refusing something. That is the shape of the feature:
this is the surface that decides what the agent may reach outside CyberFriend,
and the failures it exists to prevent all look like configuration that was
accepted and then failed somewhere else -- at the agent's next startup, or at
3am when a tool nobody could see enabled turned out to write.

The last test in the file is the one that ties it to reality: whatever the
console stores is fed back through the *bot's* own configuration builder, so a
console that writes something the agent cannot load fails here rather than in
a deploy.
"""

from __future__ import annotations

import pytest

from chatmemory.adapters.mcp_client.config import AllowedTool
from chatmemory.adapters.mcp_client.registry import _effect_for
from chatmemory.admin.audit import ChangeKind
from chatmemory.admin.handlers.federation import effect_for
from chatmemory.app.authorization import ToolEffect
from chatmemory.composition import build_federation_config, parse_allowed_tool
from chatmemory.config import Settings
from tests.unit.test_admin_api import Console, build_console, tool

ISSUES = {
    "issues": (
        tool("search_issues", ToolEffect.READ_ONLY),
        tool("create_issue", ToolEffect.MUTATING),
        tool("sync", ToolEffect.UNDETERMINED),
    )
}


# --- reporting what a server offers ------------------------------------


async def test_listing_servers_probes_them_and_reports_what_they_offer() -> None:
    console = await build_console(offers=ISSUES)

    rows = console.client.get("/api/federation/servers", headers=console.auth()).json()

    assert console.probe.calls == ["issues"]
    assert rows[0]["name"] == "issues"
    assert rows[0]["reachable"] is True
    assert {t["name"] for t in rows[0]["tools"]} == {"search_issues", "create_issue", "sync"}


async def test_an_unreachable_server_is_reported_rather_than_hidden() -> None:
    console = await build_console(offers={})

    rows = console.client.get("/api/federation/servers", headers=console.auth()).json()

    assert rows[0]["reachable"] is False
    assert rows[0]["failure"]
    assert rows[0]["tools"] == []


async def test_a_servers_claim_to_be_read_only_is_shown_as_a_claim() -> None:
    """Only an operator can determine a tool is read-only.

    The console must not launder the server's own annotation into something
    that looks like a determination: it shows the claim, and shows that the
    effect the agent will use is still undetermined.
    """
    console = await build_console(offers={"issues": (tool("sync", ToolEffect.READ_ONLY),)})

    tools = console.client.get(
        "/api/federation/servers", headers=console.auth()
    ).json()[0]["tools"]

    assert tools[0]["claims_read_only"] is True
    assert tools[0]["effect"] == "undetermined"
    assert tools[0]["mutates"] is True


async def test_a_tool_the_operator_declared_read_only_reads_as_read_only() -> None:
    console = await build_console(offers=ISSUES)

    tools = console.client.get(
        "/api/federation/servers", headers=console.auth()
    ).json()[0]["tools"]
    by_name = {t["name"]: t for t in tools}

    # `search_issues:ro` is in the environment allowlist of the harness.
    assert by_name["search_issues"]["effect"] == "read_only"
    assert by_name["search_issues"]["allowlisted"] is True
    assert by_name["sync"]["allowlisted"] is False


@pytest.mark.parametrize(
    "declared", [None, ToolEffect.READ_ONLY, ToolEffect.MUTATING]
)
@pytest.mark.parametrize(
    "advertised", [ToolEffect.READ_ONLY, ToolEffect.MUTATING, ToolEffect.UNDETERMINED]
)
def test_the_console_and_the_registry_agree_on_every_effect(
    declared: ToolEffect | None, advertised: ToolEffect
) -> None:
    """A screen that disagreed with the registry would be worse than no screen.

    It would be believed. The rule is copied rather than imported, so this is
    what keeps the copy honest.
    """
    entry = AllowedTool(
        server="issues",
        tool="sync",
        effect=declared,
        mutation_enabled=False,
    )
    offered = tool("sync", advertised)

    assert effect_for(entry, offered) == _effect_for(entry, offered)
    # And with nothing declared at all, which the registry cannot express:
    assert effect_for(None, offered) == _effect_for(AllowedTool("issues", "sync"), offered)


# --- adding a server ---------------------------------------------------


async def test_a_server_that_cannot_be_reached_is_refused_not_stored() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/servers",
        json={"name": "wiki", "target": "https://wiki.internal/mcp"},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "could not be reached" in response.json()["error"]
    assert "wiki" not in _value(console, "federation_servers")
    assert "federation_servers" in console.kinds(ChangeKind.REFUSED)


async def test_a_reachable_server_is_stored_and_its_tools_reported() -> None:
    console = await build_console(
        offers={**ISSUES, "wiki": (tool("search", ToolEffect.READ_ONLY),)}
    )

    response = console.client.post(
        "/api/federation/servers",
        json={"name": "wiki", "target": "https://wiki.internal/mcp"},
        headers=console.auth(),
    )

    assert response.status_code == 200
    assert response.json()["server"]["tools"][0]["name"] == "search"
    assert "wiki=https://wiki.internal/mcp" in _value(console, "federation_servers")
    # Discovery is not registration: nothing was allowlisted by adding it.
    assert _value(console, "federation_tool_allowlist") == "issues:search_issues:ro"


async def test_a_duplicate_server_is_refused() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/servers",
        json={"name": "issues", "target": "https://elsewhere/mcp"},
        headers=console.auth(),
    )

    assert response.status_code == 409


async def test_a_target_that_would_not_survive_storage_is_refused() -> None:
    """The setting is one row split on whitespace and commas.

    A target containing either reads back as two entries -- a server whose
    target is silently truncated, which surfaces hours later as a server that
    cannot be reached.
    """
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/servers",
        json={"name": "wiki", "target": "https://wiki/mcp?a=1,2"},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "commas" in response.json()["error"]


async def test_removing_a_server_whose_tools_are_allowlisted_is_refused() -> None:
    """`FederationConfig` refuses an entry naming a server that is not configured.

    Storing that would leave configuration the agent cannot boot with.
    """
    console = await build_console(offers=ISSUES)

    response = console.client.delete("/api/federation/servers/issues", headers=console.auth())

    assert response.status_code == 409
    assert "issues:search_issues" in response.json()["error"]
    assert "issues" in _value(console, "federation_servers")


async def test_removing_an_unused_server_works() -> None:
    console = await build_console(offers=ISSUES)
    console.client.delete(
        "/api/federation/allowlist/issues/search_issues", headers=console.auth()
    )

    response = console.client.delete("/api/federation/servers/issues", headers=console.auth())

    assert response.status_code == 200
    assert _value(console, "federation_servers") == ""


# --- the allowlist -----------------------------------------------------


async def test_a_tool_no_configured_server_offers_is_refused() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": "delete_everything", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "does not offer" in response.json()["error"]
    assert "delete_everything" not in _value(console, "federation_tool_allowlist")
    assert "federation_tool_allowlist" in console.kinds(ChangeKind.REFUSED)


async def test_a_tool_on_a_server_that_is_not_configured_is_refused() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "wiki", "tool": "search", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "not a configured server" in response.json()["error"]


async def test_a_tool_cannot_be_allowlisted_while_its_server_is_unreachable() -> None:
    """Reachable-and-missing is a startup error; unreachable is degradation.

    The console cannot tell the difference without an answer, so it refuses
    rather than storing configuration it has not checked.
    """
    console = await build_console(offers={})

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": "sync", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "could not be reached" in response.json()["error"]


async def test_a_read_only_tool_is_an_ordinary_change() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": "sync", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 200
    assert "issues:sync:ro" in _value(console, "federation_tool_allowlist")
    assert console.kinds(ChangeKind.ESCALATION) == []


@pytest.mark.parametrize(
    "confirmation",
    [None, "", "sync", "issues:sync", "create_issu", "yes"],
    ids=["absent", "empty", "another-tool", "another-qualified", "typo", "generic"],
)
async def test_a_mutating_tool_without_a_matching_confirmation_is_refused(
    confirmation: str | None,
) -> None:
    console = await build_console(offers=ISSUES)
    body = {"server": "issues", "tool": "create_issue", "read_only": False}
    if confirmation is not None:
        body["confirm_tool_name"] = confirmation

    response = console.client.post(
        "/api/federation/allowlist", json=body, headers=console.auth()
    )

    assert response.status_code == 400
    assert "confirm" in response.json()["error"]
    assert "create_issue" not in _value(console, "federation_tool_allowlist")
    assert "federation_tool_allowlist" in console.kinds(ChangeKind.REFUSED)


@pytest.mark.parametrize("confirmation", ["create_issue", "issues:create_issue"])
async def test_a_confirmed_mutating_tool_is_enabled_and_recorded_as_an_escalation(
    confirmation: str,
) -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist",
        json={
            "server": "issues",
            "tool": "create_issue",
            "read_only": False,
            "confirm_tool_name": confirmation,
        },
        headers=console.auth(),
    )

    assert response.status_code == 200
    assert "issues:create_issue:enable-mutation" in _value(
        console, "federation_tool_allowlist"
    )
    assert console.kinds(ChangeKind.ESCALATION) == [
        "federation_tool_allowlist:issues:create_issue"
    ]
    entry = [e for e in await console.changes.recent() if e.kind is ChangeKind.ESCALATION][0]
    assert entry.operator == "ana"
    assert entry.before == "not enabled"
    assert "may change state on issues" in (entry.after or "")


async def test_a_mutating_tool_nobody_can_spend_is_refused() -> None:
    """The other half of the grant. Without a holder the agent refuses to boot."""
    console = await build_console(
        offers={**ISSUES, "wiki": (tool("edit", ToolEffect.MUTATING),)}
    )
    console.client.post(
        "/api/federation/servers",
        json={"name": "wiki", "target": "https://wiki.internal/mcp"},
        headers=console.auth(),
    )

    response = console.client.post(
        "/api/federation/allowlist",
        json={
            "server": "wiki",
            "tool": "edit",
            "read_only": False,
            "confirm_tool_name": "edit",
        },
        headers=console.auth(),
    )

    assert response.status_code == 400
    assert "nobody holds a credential" in response.json()["error"]
    assert "wiki:edit" not in _value(console, "federation_tool_allowlist")


async def test_declaring_a_tool_read_only_that_the_server_says_writes_warns() -> None:
    """The declaration does not win here, so the console says what will happen."""
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": "create_issue", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 200
    assert "refuse to call it" in response.json()["warning"]


async def test_the_allowlist_distinguishes_tools_that_may_change_things() -> None:
    console = await build_console(offers=ISSUES)
    console.client.post(
        "/api/federation/allowlist",
        json={
            "server": "issues",
            "tool": "create_issue",
            "read_only": False,
            "confirm_tool_name": "create_issue",
        },
        headers=console.auth(),
    )

    rows = {
        e["tool"]: e
        for e in console.client.get(
            "/api/federation/allowlist", headers=console.auth()
        ).json()
    }

    assert rows["create_issue"]["mutation_enabled"] is True
    assert rows["create_issue"]["credential"] == "per_requester"
    assert rows["search_issues"]["mutation_enabled"] is False


async def test_revoking_a_tool_removes_it() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.delete(
        "/api/federation/allowlist/issues/search_issues", headers=console.auth()
    )

    assert response.status_code == 200
    assert _value(console, "federation_tool_allowlist") == ""
    assert console.client.get("/api/federation/allowlist", headers=console.auth()).json() == []


async def test_revoking_a_tool_that_is_not_allowlisted_is_a_404() -> None:
    console = await build_console(offers=ISSUES)

    response = console.client.delete(
        "/api/federation/allowlist/issues/nothing", headers=console.auth()
    )

    assert response.status_code == 404


# --- what the console writes, the agent must be able to load -----------


async def test_everything_the_console_stores_loads_as_the_agents_own_config() -> None:
    """The check that makes the rest of this file worth having.

    The console writes text; the bot parses it at startup with
    `build_federation_config`, which refuses a tool naming an unconfigured
    server, an enabled mutation nobody can spend, and a malformed entry. Fed
    the console's own output, it must build.
    """
    console = await build_console(
        offers={**ISSUES, "wiki": (tool("search", ToolEffect.READ_ONLY),)}
    )
    console.client.post(
        "/api/federation/servers",
        json={"name": "wiki", "target": "https://wiki.internal/mcp"},
        headers=console.auth(),
    )
    console.client.post(
        "/api/federation/allowlist",
        json={"server": "wiki", "tool": "search", "read_only": True},
        headers=console.auth(),
    )
    console.client.post(
        "/api/federation/allowlist",
        json={
            "server": "issues",
            "tool": "create_issue",
            "read_only": False,
            "confirm_tool_name": "create_issue",
        },
        headers=console.auth(),
    )

    config = build_federation_config(_settings_from(console))

    assert config is not None
    assert sorted(config.server_names) == ["issues", "wiki"]
    assert [e.qualified_name for e in config.allowlist if e.mutation_enabled] == [
        "issues:create_issue"
    ]


async def test_what_the_console_writes_parses_back_into_the_same_entry() -> None:
    console = await build_console(offers=ISSUES)
    console.client.post(
        "/api/federation/allowlist",
        json={
            "server": "issues",
            "tool": "create_issue",
            "read_only": False,
            "confirm_tool_name": "create_issue",
        },
        headers=console.auth(),
    )

    written = _value(console, "federation_tool_allowlist").split()
    entries = [parse_allowed_tool(word) for word in written]

    assert [e.qualified_name for e in entries] == [
        "issues:search_issues",
        "issues:create_issue",
    ]
    assert entries[1].effect is ToolEffect.MUTATING


# --- helpers -----------------------------------------------------------


def _value(console: Console, key: str) -> str:
    """One setting as it is in force, in the text form the store holds.

    Read back through the API rather than out of the fake store, because a
    setting an operator has not edited yet is in force from the environment
    and has no stored row -- and "what is in force" is what the agent uses.
    """
    views = {
        v["key"]: v
        for v in console.client.get("/api/settings", headers=console.auth()).json()
    }
    return str(views[key]["text"])


def _settings_from(console: Console) -> Settings:
    """The bot's settings, carrying exactly what the console left in force."""
    return Settings(  # type: ignore[call-arg]
        discord_token="x",
        discord_guild_id=1,
        database_url="postgresql+asyncpg://u:p@h/d",
        llm_api_key="k",
        federation_servers=_value(console, "federation_servers"),
        federation_tool_allowlist=_value(console, "federation_tool_allowlist"),
        federation_credential_holders=_value(console, "federation_credential_holders"),
    )


# --- a credential pasted into a name box -------------------------------

PASTED_CREDENTIAL = "ghp_REALTOKEN_abcdef123456"
"""What an operator's clipboard holds while they are setting up a server.

Shaped like a real personal access token on purpose: it satisfies the tool
name grammar, so nothing about its *form* saves us. Only refusing to repeat it
does.
"""


@pytest.mark.parametrize(
    "body",
    [
        {"server": "issues", "tool": PASTED_CREDENTIAL, "read_only": True},
        {"server": PASTED_CREDENTIAL, "tool": "search_issues", "read_only": True},
        {
            "server": "issues",
            "tool": PASTED_CREDENTIAL,
            "read_only": False,
            "confirm_tool_name": PASTED_CREDENTIAL,
        },
    ],
    ids=["tool-box", "server-box", "mutating"],
)
async def test_a_credential_pasted_into_the_allowlist_is_refused_without_being_kept(
    body: dict[str, object],
) -> None:
    """The refusal must not repeat what it refused, anywhere.

    The change record is append-only at the table, so a secret that reaches it
    is a secret nobody can delete. The attempt is still recorded -- that is the
    entry worth having -- but it is recorded as an attempt, not as a value.
    """
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/allowlist", json=body, headers=console.auth()
    )
    entries = console.client.get("/api/audit", headers=console.auth()).json()

    assert response.status_code == 400
    assert PASTED_CREDENTIAL not in response.text
    assert [e for e in entries if e["kind"] == "refused"], "the attempt is still recorded"
    assert all(PASTED_CREDENTIAL not in str(e) for e in entries)
    assert PASTED_CREDENTIAL not in _value(console, "federation_tool_allowlist")


async def test_a_credential_pasted_while_the_server_is_unreachable_is_not_kept() -> None:
    """The unreachable branch refuses before anything has recognised the name."""
    console = await build_console(offers={})

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": PASTED_CREDENTIAL, "read_only": True},
        headers=console.auth(),
    )
    entries = console.client.get("/api/audit", headers=console.auth()).json()

    assert response.status_code == 400
    assert PASTED_CREDENTIAL not in response.text
    assert all(PASTED_CREDENTIAL not in str(e) for e in entries)


@pytest.mark.parametrize(
    "name",
    [PASTED_CREDENTIAL, "a3f9c1b0d7e24f6890ab5c3d1e7f2048"],
    ids=["rejected-by-charset", "admitted-by-charset"],
)
async def test_a_credential_pasted_into_the_server_name_is_not_echoed_back(
    name: str,
) -> None:
    """Both halves of the name box: the charset stops one of these and not the other.

    A lowercase hexadecimal key is a valid server name as far as
    `ServerConfig` is concerned, so the only thing between it and the record is
    the refusal declining to repeat it.
    """
    console = await build_console(offers=ISSUES)

    response = console.client.post(
        "/api/federation/servers",
        json={"name": name, "target": "https://issues.internal/mcp"},
        headers=console.auth(),
    )
    entries = console.client.get("/api/audit", headers=console.auth()).json()

    assert response.status_code == 400
    assert name not in response.text
    assert all(name not in str(e) for e in entries)


async def test_a_credential_in_a_delete_path_is_not_echoed_back() -> None:
    console = await build_console(offers=ISSUES)

    tool_delete = console.client.delete(
        f"/api/federation/allowlist/issues/{PASTED_CREDENTIAL}", headers=console.auth()
    )
    server_delete = console.client.delete(
        f"/api/federation/servers/{PASTED_CREDENTIAL}", headers=console.auth()
    )

    assert (tool_delete.status_code, server_delete.status_code) == (404, 404)
    assert PASTED_CREDENTIAL not in tool_delete.text
    assert PASTED_CREDENTIAL not in server_delete.text
