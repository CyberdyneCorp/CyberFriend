"""Stored settings, and why each value is what it is.

Two decisions in here are worth more than the code around them.

**Provenance is returned with every value.** A setting edited in the console
but overridden by an environment variable looks exactly like a setting that
did not save, and an operator who cannot tell those apart edits the same box
again instead of looking at the deployment.

**Some settings are not editable here, although they are editable.** The
federation server list, the tool allowlist and the indexing scope each have
their own endpoint, because each has a validation step that a raw PUT would
walk straight past: probing a server before storing it, refusing a tool no
server offers, requiring a typed confirmation before a mutating tool is
enabled, and reporting a channel the corpus shows no sign of the bot reading.

That second one is not tidiness. `PUT /api/settings/federation_tool_allowlist`
with the text `issues:delete_repo:enable-mutation` would enable a
state-changing tool with no confirmation and no escalation recorded -- the
same act the federation endpoint spells out in three steps. A door beside a
guarded door is not a boundary, so this handler refuses those keys by name and
says which endpoint owns them.
"""

from __future__ import annotations

from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import refused
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import (
    Refused,
    acting_operator,
    body_of,
    moment,
    value_field,
)
from chatmemory.app.configuration import ConfigurationRefused, SettingView
from chatmemory.ports.configuration import SettingSource

log = structlog.get_logger()

WIRE_SOURCE = {
    SettingSource.DATABASE: "db",
    SettingSource.ENVIRONMENT: "env",
    SettingSource.DEFAULT: "default",
}
"""Provenance as the API contract spells it, which the interface was written to.

Three short words rather than the enum's own names. The vocabulary on the wire
is the contract's, because two things were built against it in parallel and a
console rendering an unrecognised source would show the one screen that exists
to explain where a value came from with nothing in that column.
"""

MANAGED_ELSEWHERE: dict[str, str] = {
    "federation_servers": "/api/federation/servers",
    "federation_tool_allowlist": "/api/federation/allowlist",
    "indexed_channel_ids": "/api/channels",
}
"""Settings whose endpoint validates something a raw edit would skip.

Each of these is stored as ordinary text like any other setting -- the
difference is not the storage, it is that the endpoint named here probes,
confirms or reports before writing it. Editing them through this handler would
keep the storage and lose the check.
"""


def routes(services: AdminServices) -> list[Route]:
    async def list_settings(_: Request) -> JSONResponse:
        # From the in-force snapshot rather than from the store: the operator
        # is shown what the agent is actually using, which is the question,
        # and where each value came from, which is the follow-up.
        return JSONResponse([_view(v) for v in services.configuration.current.explain()])

    async def put_setting(request: Request) -> JSONResponse:
        key = str(request.path_params["key"])
        operator = acting_operator()
        raw = value_field(await body_of(request))

        endpoint = MANAGED_ELSEWHERE.get(key.strip().lower())
        if endpoint is not None:
            reason = (
                f"{key} is edited through {endpoint}, which validates the change "
                "first; a direct edit would store configuration nothing has checked"
            )
            # Recorded, not merely refused: "somebody tried to write the
            # allowlist directly" is exactly the entry that matters when the
            # same tool turns up enabled a week later.
            await services.changes.record(refused(operator, key, reason))
            raise Refused(reason)

        try:
            await services.editor.set(key, raw, operator.name, display=operator.display)
        except ConfigurationRefused as exc:
            # The editor has already recorded this refusal, with the reason
            # and without the attempted value -- the most likely bad value is
            # a credential pasted into the wrong box.
            raise Refused(str(exc)) from exc

        # So that the very next GET shows what was just written rather than the
        # value from before the edit. Long-running processes pick it up on their
        # own cadence; the console must not appear to have lost the edit in the
        # meantime.
        await services.configuration.refresh()
        log.info("admin.setting_changed", setting=key, operator=operator.name)
        return JSONResponse(_current_view(services, key))

    return [
        Route("/api/settings", list_settings, methods=["GET"], name="settings"),
        Route("/api/settings/{key}", put_setting, methods=["PUT"], name="put_setting"),
    ]


def _view(view: SettingView) -> dict[str, Any]:
    endpoint = MANAGED_ELSEWHERE.get(view.key)
    return {
        "key": view.key,
        # JSON-native, so a console renders a list as a list. `text` beside it
        # is the same value in the form an operator types and the store holds.
        "value": _jsonable(view.value),
        "text": setting_text(view.value),
        "source": WIRE_SOURCE[view.source],
        "editable": endpoint is None,
        "managed_by": endpoint,
        "summary": view.summary,
        "updated_by": view.updated_by,
        "updated_at": moment(view.updated_at),
    }


def _current_view(services: AdminServices, key: str) -> dict[str, Any]:
    for view in services.configuration.current.explain():
        if view.key == key:
            return _view(view)
    # Unreachable: the editor refuses an unknown key before anything is
    # written, so a key that got this far is in the registry.
    raise Refused(f"{key} is not a setting")


def _jsonable(value: object) -> Any:
    """Render a setting value for JSON without losing its shape.

    Two rules. Sets are sorted on the way out -- the order a `frozenset`
    iterates in is not stable between processes, and a console that showed the
    same channel list in a different order on every refresh would look like it
    was changing. And every member is a string: a Discord snowflake is 64 bits
    and a browser's JSON number is not, so an id sent as a number comes back
    rounded and the console names a channel nobody has.
    """
    if isinstance(value, frozenset | set):
        return sorted(str(v) for v in value)
    if isinstance(value, tuple | list):
        return [str(v) for v in value]
    return value


def setting_text(value: object) -> str:
    """The value as an operator would type it, and as the store holds it.

    Shared with the handlers that edit a list-shaped setting by rewriting the
    whole row: what they write has to read back identically, or every edit
    would look like a change to the audit even when nothing moved.
    """
    if isinstance(value, frozenset | set):
        # Numerically when they are numbers: a channel list rendered
        # "100 1000 200" is the same set and reads like a mistake.
        return " ".join(str(v) for v in sorted(value, key=_sortable))
    if isinstance(value, tuple | list):
        # Order is preserved, not sorted: these are the operator's own
        # entries, and reordering them would make every edit look like a
        # change to the record even when nothing moved.
        return " ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sortable(value: object) -> tuple[int, float, str]:
    """Order numbers as numbers and everything else as text, in one key."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, str(value))
