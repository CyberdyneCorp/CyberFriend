"""Which channels are archived, and what the corpus says about each one.

Indexing scope is the setting that decides what the permanent record contains,
so adding one is not a preference -- it starts archiving what people say in a
place they may not think of as archived. Removing one stops ingestion within a
refresh period and leaves what was already stored, which is why the response
says so rather than implying a deletion.

**On "can the bot read this channel".** The console holds database credentials
and nothing else: no Discord token, by design, because a console that could
impersonate the bot would hold far more authority than configuring it needs.
So it cannot ask Discord. What it can do is report what the corpus knows --
whether the agent has ever stored a message from that channel, and when the
last one was -- and say plainly that this is evidence rather than a permission
check. An operator adding a channel the bot cannot see gets "the agent has
never stored a message from this channel", which is the honest form of the
warning and the one that is actually true at the moment it is shown.

Reporting rather than refusing, because the two cases are indistinguishable
from here: a channel nobody has posted in yet looks exactly like one the bot
cannot read, and refusing would make a new channel impossible to add.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.handlers.queries import ChannelEvidence
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import (
    Ok,
    Refused,
    account_id,
    acting_operator,
    body_of,
    moment,
    path_int,
)
from chatmemory.app.configuration import INDEXED_CHANNEL_IDS

log = structlog.get_logger()

NEVER_READ = "the agent has never stored a message from this channel"
"""What an operator is told when the corpus holds no evidence of readability.

Worded as the fact it is. "The bot cannot read this channel" would be a claim
the console is not in a position to make, and an operator who acts on a claim
that turns out to be wrong stops believing the next one.
"""


STALE_SCOPE = (
    "the stored indexing scope could not be read, so nothing was changed; try again shortly"
)


def routes(services: AdminServices) -> list[Route]:
    # Serialises this console's own read-refresh-write cycles, so two requests
    # in flight here cannot each start from the same scope and lose one edit.
    scope_lock = asyncio.Lock()

    async def list_channels(_: Request) -> JSONResponse:
        return JSONResponse(await _channel_views(services))

    async def add_channel(request: Request) -> JSONResponse:
        operator = acting_operator()
        channel_id = account_id(await body_of(request), "id")
        evidence = await services.channels.channel(channel_id)
        async with scope_lock:
            scope = await _fresh_scope(services)
            if channel_id in scope:
                raise Refused(f"{channel_id} is already indexed", status=409)
            await _store_scope(services, [*sorted(scope), channel_id])
        log.info(
            "admin.channel_added",
            channel_id=channel_id,
            read_by_bot=evidence is not None and evidence.read_by_bot,
            operator=operator.name,
        )
        return Ok(
            changed=INDEXED_CHANNEL_IDS.key,
            detail=(
                f"{channel_id} is now in indexing scope; ingestion picks it up at the "
                "next configuration refresh"
            ),
        ).response(channel=_view(channel_id, evidence, indexed=True))

    async def remove_channel(request: Request) -> JSONResponse:
        channel_id = path_int(request, "id")
        async with scope_lock:
            scope = await _fresh_scope(services)
            if channel_id not in scope:
                raise Refused(f"{channel_id} is not in indexing scope", status=404)
            await _store_scope(services, sorted(scope - {channel_id}))
        log.info("admin.channel_removed", channel_id=channel_id)
        return Ok(
            changed=INDEXED_CHANNEL_IDS.key,
            detail=(
                f"{channel_id} leaves indexing scope at the next configuration refresh. "
                "Messages already archived from it stay archived; removing them is "
                "retention or an opt-out, not a scope change."
            ),
        ).response()

    return [
        Route("/api/channels", list_channels, methods=["GET"], name="channels"),
        Route("/api/channels", add_channel, methods=["POST"], name="add_channel"),
        Route(
            "/api/channels/{id}", remove_channel, methods=["DELETE"], name="remove_channel"
        ),
    ]


def _scope(services: AdminServices) -> frozenset[int]:
    return services.configuration.current.get(INDEXED_CHANNEL_IDS)


async def _fresh_scope(services: AdminServices) -> frozenset[int]:
    """Stored scope as of now, for a write that replaces the whole set.

    `current` is a copy refreshed on a period, and Discord's `/index` and
    `/unindex` write the same row between refreshes. Writing from that copy
    would put back a channel a moderator with Manage Channels just removed --
    with no permission check, no notice and nothing naming that channel -- or
    drop one they just added. So the console re-reads first, as the indexing
    service does. A read that fails changes nothing: a write from a copy that
    cannot be confirmed current is exactly the stale write this prevents.
    """
    report = await services.configuration.refresh()
    if not report.applied:
        raise Refused(STALE_SCOPE, status=503)
    return _scope(services)


async def _store_scope(services: AdminServices, channel_ids: list[int]) -> None:
    await services.editor.set(
        INDEXED_CHANNEL_IDS.key,
        " ".join(str(c) for c in channel_ids),
        # The operator comes from the credential; the editor records it against
        # the change in the same transaction as the write.
        acting_operator().name,
    )
    await services.configuration.refresh()


async def _channel_views(services: AdminServices) -> list[dict[str, Any]]:
    """Every channel in scope, plus every channel the corpus knows about.

    The union matters in both directions. A channel in scope with no rows is
    the one an operator has just added and wants the warning about; a channel
    with rows that is no longer in scope is the one somebody removed and
    forgot, whose content is still being served.
    """
    evidence = {c.channel_id: c for c in await services.channels.channels()}
    scope = _scope(services)
    return [
        _view(channel_id, evidence.get(channel_id), indexed=channel_id in scope)
        for channel_id in sorted(scope | set(evidence))
    ]


def _view(channel_id: int, evidence: ChannelEvidence | None, *, indexed: bool) -> dict[str, Any]:
    read = evidence is not None and evidence.read_by_bot
    return {
        # A string: a Discord snowflake is 64 bits and a browser's JSON number
        # is not, so an id sent as a number comes back rounded and the console
        # deletes a channel nobody named.
        "id": str(channel_id),
        "name": evidence.name if evidence is not None else "",
        "indexed": indexed,
        "readable_by_bot": read,
        # Counts, never content. "How much is archived here" is a number.
        "messages": evidence.messages if evidence is not None else 0,
        "last_message_at": moment(evidence.last_message_at) if evidence else None,
        "evidence": None if read else NEVER_READ,
    }
