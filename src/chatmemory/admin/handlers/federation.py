"""Federated servers and the tool allowlist: the console's sharpest edge.

This is the screen that decides what the agent may reach outside CyberFriend,
so it is the one place where "store it and find out at startup" is not good
enough. Three checks happen before anything is written, and each of them
exists because of a specific way the deployment would otherwise break:

*   **A server is probed before it is accepted.** Configuration naming a
    server nobody can reach is configuration that degrades silently: the agent
    boots, the tool is unavailable, and the answer says nothing was found.
*   **A tool no configured server offers is refused.** `registry.register`
    raises at startup for exactly this, so storing it would turn an operator's
    typo into a failed deploy of the *bot*, hours later and somewhere else.
*   **A mutating tool needs a confirmation naming it, and a credential holder
    on its server.** The confirmation is the deliberate act; the holder is the
    other half of the grant, and without it `composition` refuses to boot.

Enabling a mutating tool is recorded as an escalation rather than an ordinary
edit, because it widens what the agent may do to the world. The design note
for this change says plainly that moving this decision into a UI leaves the
per-invocation confirmation as the only remaining guard -- so the least this
surface can do is make the act deliberate and name who did it.

What this module writes is text: `name=target` entries and
`server:tool[:ro|:enable-mutation]` entries, exactly as the environment
variables spell them, parsed and rendered by `composition` so the console and
the bot cannot drift into disagreeing about what an entry means.

One thing it deliberately does not return: a tool's description. That is text
an external server controls, and the console is read by the person who decides
what the agent may call. A name and an effect are what the decision needs.

And one thing it deliberately does not repeat: a name it did not recognise.
Every box on this screen is free text, and one of them is where an operator
pastes a credential, so the likeliest wrong value here is a token in the wrong
field -- and every refusal on this path writes both an error body and a change
record. `config_sql` states the rule for the store's own refusals ("the key
and the reason, never the value"); the change record carries `before`/`after`
and so would have kept it, in the one table with a trigger that refuses DELETE.
So a refusal here names a *recognised* value or none at all: a configured
server, or a tool the server itself advertised, is already ours to print;
anything that arrived in the request and matched nothing is `UNRECOGNISED`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NoReturn, TypeVar

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.adapters.mcp_client.client import SessionFactory
from chatmemory.adapters.mcp_client.config import (
    QUALIFIER,
    AllowedTool,
    ConfigurationError,
    ServerConfig,
    qualify,
)
from chatmemory.adapters.mcp_client.registry import ServerDiscovery
from chatmemory.adapters.mcp_client.session import DiscoveredTool, default_session_factory
from chatmemory.admin.audit import escalation, refused
from chatmemory.admin.auth import Actor
from chatmemory.admin.handlers.services import AdminServices, ServerProbe
from chatmemory.admin.handlers.settings import setting_text
from chatmemory.admin.handlers.support import (
    Ok,
    Refused,
    acting_operator,
    body_of,
    flag_field,
    text_field,
)
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.configuration import (
    FEDERATION_CREDENTIAL_HOLDERS,
    FEDERATION_SERVERS,
    FEDERATION_TOOL_ALLOWLIST,
    SettingSpec,
)
from chatmemory.composition import (
    MUTATION_SUFFIX,
    READ_ONLY_SUFFIX,
    parse_allowed_tool,
    parse_credential_holder,
    parse_server,
)

log = structlog.get_logger()

T = TypeVar("T")

PROBE_TIMEOUT_SECONDS = 10.0
"""How long a probe may take before the console calls the server unreachable.

Shorter than the agent's own call timeout on purpose: an operator is watching
this request, and a probe that hangs for the federation default would read as
a broken console rather than as a server that is down.
"""

FAILURE_DETAIL_LIMIT = 200
"""How much of a connection failure is quoted back. Enough to recognise a DNS
error or a 404; not enough for a server to fill an operator's screen."""

UNRECOGNISED = "the name that was submitted"
"""What a refusal calls a name the console did not recognise.

Stands where the value itself used to be interpolated. A refusal that quoted
it would put a mistyped credential into the error body, into whatever logs the
response, and -- through `refused(..., attempted=...)` -- into the change
record, which is append-only at the table and cannot be cleaned up afterwards.
The operator does not need it repeated: they are looking at what they typed.
What they need is what the console *does* know, which is why the refusals
below name the configured servers and the tools actually on offer.
"""


# --- probing -----------------------------------------------------------


def make_probe(factory: SessionFactory | None = None) -> ServerProbe:
    """A probe over a session factory. Opens, lists, closes.

    Separate from `Federation.connect` although it does a similar thing: that
    one holds the connection open for the run, and holding one open here would
    leave the console keeping sessions to servers nobody has approved yet.
    """
    open_session = factory or default_session_factory

    async def probe(server: ServerConfig) -> ServerDiscovery:
        try:
            # `asyncio.timeout` rather than `wait_for`, for the reason
            # `client.call` gives: wait_for runs the body in a new task, and
            # the MCP SDK's anyio cancel scopes must be entered and left in
            # the same one or teardown raises instead of closing.
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS), open_session(server) as session:
                tools = tuple(await session.list_tools())
        except Exception as exc:  # noqa: BLE001 - every failure reports the same way
            log.info("admin.probe_failed", server=server.name, error=type(exc).__name__)
            return ServerDiscovery(
                name=server.name, failure=(str(exc) or "unavailable")[:FAILURE_DETAIL_LIMIT]
            )
        return ServerDiscovery(name=server.name, tools=tools)

    return probe


def effect_for(entry: AllowedTool | None, advertised: DiscoveredTool) -> ToolEffect:
    """What the agent will treat this tool as, given what the operator declared.

    A copy of the rule in `registry._effect_for`, and a test asserts the two
    agree for every combination. The console must show the effect the agent
    will actually use: a screen that said "read-only" where the registry says
    "mutating" would be worse than no screen, because it would be believed.

    The rule itself: a server's claim to be read-only is never honoured -- only
    an operator can determine that -- while its claim to *mutate* is believed,
    because believing it costs a confirmation prompt and nothing else.
    """
    if advertised.effect is ToolEffect.MUTATING:
        return ToolEffect.MUTATING
    if entry is not None and entry.effect is not None:
        return entry.effect
    return ToolEffect.UNDETERMINED


# --- routes ------------------------------------------------------------


def routes(services: AdminServices) -> list[Route]:
    async def list_servers(_: Request) -> JSONResponse:
        servers = _servers(services)
        allowlist = _allowlist(services)
        # Probed one at a time rather than gathered: a server that hangs its
        # handshake must not delay the report on every healthy one, and the
        # per-probe timeout bounds the whole page.
        found = [(s, await services.probe(s)) for s in servers]
        return JSONResponse([_server_view(s, d, allowlist) for s, d in found])

    async def add_server(request: Request) -> JSONResponse:
        operator = acting_operator()
        body = await body_of(request)
        server = _server_config(text_field(body, "name"), text_field(body, "target"))
        await _refuse_duplicate_server(services, operator, server)

        discovery = await services.probe(server)
        if not discovery.reachable:
            # Unnamed for the same reason the allowlist refusals are: this
            # name is not configured -- that is what the request was for -- so
            # it is still nothing but text that arrived in a body, and
            # `ServerConfig`'s charset admits plenty of credentials.
            reason = f"{UNRECOGNISED} could not be reached: {discovery.failure}"
            await services.changes.record(
                refused(operator, FEDERATION_SERVERS.key, reason)
            )
            raise Refused(reason)

        await _write(
            services,
            FEDERATION_SERVERS,
            [*_raw_words(services, FEDERATION_SERVERS), f"{server.name}={server.target}"],
            operator,
        )
        log.info("admin.federation.server_added", server=server.name, operator=operator.name)
        return Ok(
            changed=FEDERATION_SERVERS.key,
            detail=f"{server.name} answered and offers {len(discovery.tools)} tool(s)",
        ).response(server=_server_view(server, discovery, _allowlist(services)))

    async def remove_server(request: Request) -> JSONResponse:
        operator = acting_operator()
        name = str(request.path_params["name"])
        server = next((s for s in _servers(services) if s.name == name), None)
        if server is None:
            # Unrecognised, so unrepeated -- a path segment is a submitted
            # value like any other. See `UNRECOGNISED`.
            raise Refused(f"{UNRECOGNISED} is not a configured server", status=404)

        listed = [e.qualified_name for e in _allowlist(services) if e.server == name]
        if listed:
            # A tool naming a server that is not configured is a startup error
            # in `FederationConfig`, so removing the server first would leave
            # configuration that stops the bot booting.
            reason = (
                f"{name} still provides allowlisted tools ({', '.join(sorted(listed))}); "
                "remove them first, or the agent will refuse to start"
            )
            await services.changes.record(
                refused(operator, FEDERATION_SERVERS.key, reason, attempted=f"remove {name}")
            )
            raise Refused(reason, status=409)

        await _write(
            services,
            FEDERATION_SERVERS,
            [w for w in _raw_words(services, FEDERATION_SERVERS) if not _names(w, name)],
            operator,
        )
        log.info("admin.federation.server_removed", server=name, operator=operator.name)
        return Ok(changed=FEDERATION_SERVERS.key, detail=f"{name} removed").response()

    async def list_allowlist(_: Request) -> JSONResponse:
        return JSONResponse([_entry_view(e) for e in _allowlist(services)])

    async def add_allowed_tool(request: Request) -> JSONResponse:
        operator = acting_operator()
        body = await body_of(request)
        server_name = text_field(body, "server")
        tool = text_field(body, "tool")
        read_only = flag_field(body, "read_only")
        confirmation = body.get("confirm_tool_name")

        server = await _configured_server(services, operator, server_name)
        if any(e.qualified_name == qualify(server.name, tool) for e in _allowlist(services)):
            raise Refused(f"{qualify(server.name, tool)} is already allowlisted", status=409)
        advertised = await _offered_tool(services, operator, server, tool)
        if read_only:
            entry = _declare(server.name, tool, READ_ONLY_SUFFIX)
        else:
            await _check_confirmation(services, operator, server.name, tool, confirmation)
            await _check_someone_can_spend_it(services, operator, server.name, tool)
            entry = _declare(server.name, tool, MUTATION_SUFFIX)

        await _write(
            services,
            FEDERATION_TOOL_ALLOWLIST,
            [*_raw_words(services, FEDERATION_TOOL_ALLOWLIST), _spec(entry)],
            operator,
        )
        if entry.mutation_enabled:
            await _record_escalation(services, operator, entry)
        log.info(
            "admin.federation.tool_allowed",
            tool=entry.qualified_name,
            mutating=entry.mutation_enabled,
            operator=operator.name,
        )
        return Ok(
            changed=FEDERATION_TOOL_ALLOWLIST.key,
            detail=f"{entry.qualified_name} is now available to the agent",
        ).response(
            tool=_entry_view(entry), warning=_effect_warning(entry, advertised)
        )

    async def remove_allowed_tool(request: Request) -> JSONResponse:
        operator = acting_operator()
        server_name = str(request.path_params["server"])
        tool = str(request.path_params["tool"])
        name = qualify(server_name, tool)
        if not any(e.qualified_name == name for e in _allowlist(services)):
            raise Refused(f"{UNRECOGNISED} is not allowlisted", status=404)

        await _write(
            services,
            FEDERATION_TOOL_ALLOWLIST,
            [
                word
                for word in _raw_words(services, FEDERATION_TOOL_ALLOWLIST)
                if not _is_entry(word, name)
            ],
            operator,
        )
        log.info("admin.federation.tool_revoked", tool=name, operator=operator.name)
        return Ok(
            changed=FEDERATION_TOOL_ALLOWLIST.key, detail=f"{name} is no longer available"
        ).response()

    return [
        Route("/api/federation/servers", list_servers, methods=["GET"], name="servers"),
        Route("/api/federation/servers", add_server, methods=["POST"], name="add_server"),
        Route(
            "/api/federation/servers/{name}",
            remove_server,
            methods=["DELETE"],
            name="remove_server",
        ),
        Route(
            "/api/federation/allowlist", list_allowlist, methods=["GET"], name="allowlist"
        ),
        Route(
            "/api/federation/allowlist",
            add_allowed_tool,
            methods=["POST"],
            name="add_allowed_tool",
        ),
        Route(
            "/api/federation/allowlist/{server}/{tool}",
            remove_allowed_tool,
            methods=["DELETE"],
            name="remove_allowed_tool",
        ),
    ]


# --- checks ------------------------------------------------------------


async def _refuse_duplicate_server(
    services: AdminServices, operator: Actor, server: ServerConfig
) -> None:
    if not any(s.name == server.name for s in _servers(services)):
        return
    reason = f"{server.name} is already configured; remove it before adding it again"
    await services.changes.record(
        refused(operator, FEDERATION_SERVERS.key, reason, attempted=server.name)
    )
    raise Refused(reason, status=409)


async def _configured_server(
    services: AdminServices, operator: Actor, name: str
) -> ServerConfig:
    """The configured server by that name, or a refusal that does not name it."""
    configured = _servers(services)
    server = next((s for s in configured if s.name == name), None)
    if server is not None:
        return server
    known = ", ".join(sorted(s.name for s in configured)) or "none"
    # Neither half of the request is named: the server matched nothing, and
    # the tool has not been checked against anything yet. Saying which servers
    # exist is the useful sentence and leaks nothing -- the same names are on
    # the screen the operator is looking at.
    reason = (
        f"{UNRECOGNISED} is not a configured server, so its tools cannot be "
        f"allowlisted; the configured servers are {known}"
    )
    await _refuse_unrecognised(services, operator, reason)


async def _offered_tool(
    services: AdminServices, operator: Actor, server: ServerConfig, tool: str
) -> DiscoveredTool:
    """The tool as the server describes it, or a refusal.

    Refusing here is what keeps a typo from becoming a failed deploy of the
    bot: `registry.register` raises when a reachable server does not provide
    an allowlisted tool, and that happens at the agent's startup, hours later.

    This is also where a submitted tool name stops being unrecognised text and
    becomes a name the console knows, so both refusals below are on the wrong
    side of that line and neither one repeats what was asked for.
    """
    discovery = await services.probe(server)
    if not discovery.reachable:
        reason = (
            f"{server.name} could not be reached ({discovery.failure}), so the console "
            f"cannot confirm it offers {UNRECOGNISED}"
        )
        await _refuse_unrecognised(services, operator, reason)

    advertised = next((t for t in discovery.tools if t.name == tool), None)
    if advertised is None:
        offered = ", ".join(sorted(t.name for t in discovery.tools)) or "nothing"
        reason = f"{server.name} does not offer {UNRECOGNISED}; it offers {offered}"
        await _refuse_unrecognised(services, operator, reason)
    return advertised


async def _check_confirmation(
    services: AdminServices,
    operator: Actor,
    server: str,
    tool: str,
    confirmation: object,
) -> None:
    """A mutating tool needs a confirmation naming *that* tool.

    Either the bare name or the qualified one: both name this tool and neither
    names a different one. Anything else -- including nothing at all -- is
    refused and recorded, because "somebody tried to enable this and mistyped
    the confirmation" is a thing the next reader of the record wants to see.
    """
    if isinstance(confirmation, str) and confirmation.strip() in {
        tool,
        qualify(server, tool),
    }:
        return
    reason = (
        f"enabling {qualify(server, tool)} changes state on {server}; the request must "
        f"confirm it by naming the tool exactly ({tool!r})"
    )
    await _refuse_known_tool(services, operator, server, tool, reason)


async def _check_someone_can_spend_it(
    services: AdminServices, operator: Actor, server: str, tool: str
) -> None:
    """A mutating tool is useless, and refuses to boot, with no credential holder.

    `composition._check_mutation_is_spendable` raises at startup when a tool
    is enabled on a server nobody holds a credential for. Refusing here turns
    that into a sentence on the operator's screen instead of a failed deploy.
    """
    if server in _credential_holders(services):
        return
    reason = (
        f"nobody holds a credential on {server}; name them in "
        f"{FEDERATION_CREDENTIAL_HOLDERS.key} as server=platform:user_id before "
        f"enabling {qualify(server, tool)}, or the agent will refuse to start"
    )
    await _refuse_known_tool(services, operator, server, tool, reason)


async def _refuse_known_tool(
    services: AdminServices, operator: Actor, server: str, tool: str, reason: str
) -> NoReturn:
    """Refuse a tool the console has recognised, recording which one it was.

    Safe to record here and only here: `_offered_tool` has already matched the
    name against what the server advertises, so `attempted` holds a name that
    was ours before the request arrived. "Somebody tried to enable this and was
    refused" is the entry that matters when the same tool turns up enabled a
    week later, and it needs the name to be worth reading.
    """
    await services.changes.record(
        refused(
            operator,
            FEDERATION_TOOL_ALLOWLIST.key,
            reason,
            attempted=qualify(server, tool),
        )
    )
    raise Refused(reason)


async def _refuse_unrecognised(
    services: AdminServices, operator: Actor, reason: str
) -> NoReturn:
    """Refuse an allowlist change whose subject the console did not recognise.

    No `attempted`, deliberately -- the same shape `config_sql.RECORD_REFUSAL`
    has, and for the same reason: the record still says that somebody tried and
    why it was refused, without keeping the text they sent. A separate function
    rather than an optional argument, so recording the value is something a
    caller has to choose by name after reading why it is allowed.
    """
    await services.changes.record(refused(operator, FEDERATION_TOOL_ALLOWLIST.key, reason))
    raise Refused(reason)


async def _record_escalation(
    services: AdminServices, operator: Actor, entry: AllowedTool
) -> None:
    """Record enabling a mutating tool as a widening, not as an edit.

    Two entries describe this change: the store writes one for the setting's
    text, and this one names what that text *means*. Filtering the record for
    escalations has to answer "what got more permissive, and who did it", and
    a diff of two allowlist strings does not answer it at a glance.
    """
    await services.changes.record(
        escalation(
            operator,
            setting=f"{FEDERATION_TOOL_ALLOWLIST.key}:{entry.qualified_name}",
            before="not enabled",
            after=f"enabled; {entry.tool} may change state on {entry.server}",
            reason=(
                "confirmed by name from the console; every call is still put to the "
                "person who asked before it is made"
            ),
        )
    )


# --- reading the settings ----------------------------------------------


def _raw_words(services: AdminServices, spec: SettingSpec[Any]) -> list[str]:
    """The setting's current entries, exactly as stored.

    Read-modify-write on one row: two operators editing the same list at the
    same moment means the later write wins over the whole list. The change
    record shows both edits, which is what makes that recoverable rather than
    mysterious.
    """
    value = services.configuration.current.get(spec)
    return [w for w in setting_text(value).split() if w]


def _servers(services: AdminServices) -> tuple[ServerConfig, ...]:
    return tuple(_parsed(_raw_words(services, FEDERATION_SERVERS), parse_server))


def _allowlist(services: AdminServices) -> tuple[AllowedTool, ...]:
    return tuple(_parsed(_raw_words(services, FEDERATION_TOOL_ALLOWLIST), parse_allowed_tool))


def _credential_holders(services: AdminServices) -> set[str]:
    holders: set[str] = set()
    for word in _raw_words(services, FEDERATION_CREDENTIAL_HOLDERS):
        try:
            server, _ = parse_credential_holder(word)
        except ConfigurationError:
            log.warning("admin.federation.unreadable_holder", entry=word)
            continue
        holders.add(server)
    return holders


def _parsed(words: Sequence[str], read: Callable[[str], T]) -> Iterator[T]:
    """Read every entry that parses, and report the ones that do not.

    An unreadable entry cannot be produced through this console -- every write
    goes through the same parsers -- so one here came from the environment or
    from a hand-edited row. Skipping it keeps the screen usable; the warning
    is how anybody finds out it is there.
    """
    for word in words:
        try:
            yield read(word)
        except ConfigurationError:
            log.warning("admin.federation.unreadable_entry", entry=word)


async def _write(
    services: AdminServices, spec: SettingSpec[Any], words: Sequence[str], operator: Actor
) -> None:
    """Store the whole setting, then re-read it so the console is current."""
    await services.editor.set(
        spec.key, " ".join(words), operator.name, display=operator.display
    )
    await services.configuration.refresh()


# --- rendering ---------------------------------------------------------


def _server_config(name: str, target: str) -> ServerConfig:
    """Validate a submitted server, including how it will be *stored*.

    The stored setting is one text row split on whitespace and commas, so a
    target containing either would read back as two entries -- a server whose
    target is silently truncated, which fails as an unreachable server hours
    later. Refusing it here is the difference between a sentence on the screen
    and a mystery in the log.
    """
    if any(ch.isspace() for ch in target) or "," in target:
        raise Refused(
            "a server target cannot contain spaces or commas; the stored setting "
            "separates entries with them"
        )
    try:
        return ServerConfig(name=name, target=target)
    except ConfigurationError as exc:
        # `ServerConfig` quotes the name it rejected, which is right for a
        # startup error in a log and wrong for a response to whoever typed it:
        # the name box sits next to the target box, and a credential lands in
        # the wrong one. The rule is what the operator needs, so send the rule.
        raise Refused(
            "a server name must be 1-64 characters of lowercase letters, digits, "
            f"'_' or '-', and must not contain {QUALIFIER!r}"
        ) from exc


def _declare(server: str, tool: str, suffix: str) -> AllowedTool:
    """Build the entry by parsing the text that will be stored.

    Deliberately round-trips through `parse_allowed_tool`: what the console
    shows is then what the bot will read, including the refusals -- a tool
    name the parser rejects is refused here rather than stored and discovered
    at the agent's next start.
    """
    try:
        return parse_allowed_tool(f"{server}:{tool}:{suffix}")
    except ConfigurationError as exc:
        raise Refused(str(exc)) from exc


def _spec(entry: AllowedTool) -> str:
    suffix = MUTATION_SUFFIX if entry.mutation_enabled else READ_ONLY_SUFFIX
    return f"{entry.qualified_name}:{suffix}"


def _names(word: str, server: str) -> bool:
    return word.partition("=")[0].strip() == server


def _is_entry(word: str, qualified_name: str) -> bool:
    server, _, rest = word.partition(":")
    tool, _, _suffix = rest.partition(":")
    return qualify(server, tool) == qualified_name


def _server_view(
    server: ServerConfig, discovery: ServerDiscovery, allowlist: Sequence[AllowedTool]
) -> dict[str, Any]:
    by_name = {e.tool: e for e in allowlist if e.server == server.name}
    return {
        "name": server.name,
        "target": server.target,
        "reachable": discovery.reachable,
        "failure": discovery.failure,
        "tools": [_tool_view(t, by_name.get(t.name)) for t in discovery.tools],
    }


def _tool_view(advertised: DiscoveredTool, entry: AllowedTool | None) -> dict[str, Any]:
    effect = effect_for(entry, advertised)
    return {
        "name": advertised.name,
        # What the agent will treat it as, not what the server called it.
        "effect": effect.value,
        "mutates": effect.mutates,
        # Shown separately so an operator can see that a server claims to be
        # read-only *and* that the claim grants nothing until they say so.
        "claims_read_only": advertised.effect is ToolEffect.READ_ONLY,
        "allowlisted": entry is not None,
        "mutation_enabled": entry is not None and entry.mutation_enabled,
    }


def _entry_view(entry: AllowedTool) -> dict[str, Any]:
    effect = entry.effect or ToolEffect.UNDETERMINED
    return {
        "server": entry.server,
        "tool": entry.tool,
        "effect": effect.value,
        # The field the console renders differently: a tool that may change
        # something must not look like one that may not.
        "mutation_enabled": entry.mutation_enabled,
        "credential": entry.credential.value,
    }


def _effect_warning(entry: AllowedTool, advertised: DiscoveredTool) -> str | None:
    """Say so when an operator's declaration and the server's disagree.

    Declaring a tool read-only that the server says mutates does not make it
    read-only -- the registry keeps the server's claim -- so the tool will be
    refused at call time for want of an enable. Silently storing that would
    look like it worked.
    """
    if entry.mutation_enabled or advertised.effect is not ToolEffect.MUTATING:
        return None
    return (
        f"{entry.server} declares {entry.tool} state-changing, so the agent will treat "
        "it as mutating and refuse to call it until it is enabled as such"
    )
