"""Adding and removing a channel from indexing scope, from inside Discord.

Indexing is opt-in because a permanent, searchable archive of a channel is a
governance decision. This module decides who may make it and what happens
when they do; everything platform-specific -- how a permission is resolved,
how a notice is posted -- sits behind the ports below.

The rules, in the order they are checked:

*   **Authority comes from live guild state.** The requester must hold Manage
    Channels on the *target* channel, as the platform computes it now. Nothing
    the requester typed is an input to that decision: a request carries an
    authenticated account and a channel id, and no field in which "I am an
    admin" could be said.
*   **The assistant must be able to read and speak in the channel.** Indexing
    a channel it cannot read would store nothing while claiming an archive,
    and one it cannot post in would be archived without the notice every
    member is owed. Both refusals name the missing permission.
*   **Scope is written through runtime configuration.** Never a second list:
    `LiveScope` in the bot and in ingest reads the same stored setting, so a
    change made here is what both processes apply.
*   **Removal withdraws.** Unindexing purges the channel through the store's
    existing purge statement, not merely stops capture.
*   **Every attempt is recorded,** allowed or refused, in the admin change
    record with the requester, the channel and the reason.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from chatmemory.admin.audit import ChangeKind, ChangeRecordStore, ConfigurationChange
from chatmemory.app.configuration import INDEXED_CHANNEL_IDS, ConfigurationEditor
from chatmemory.app.scope import LiveScope
from chatmemory.domain.identity import ChannelRef, PersonRef

log = structlog.get_logger()

PLATFORM = "discord"

# Discord's own names for the permissions, so a refusal points at the exact
# toggle in the channel settings rather than at a paraphrase of it.
MANAGE_CHANNELS = "Manage Channels"
VIEW_CHANNEL = "View Channel"
READ_MESSAGE_HISTORY = "Read Message History"
SEND_MESSAGES = "Send Messages"

# How long after a removal the channel is purged a second time. Ingest applies
# a scope change within one refresh period, so a message posted in that window
# can be captured after the first purge; two periods is past the point where
# any process can still be capturing it.
FOLLOW_UP_PERIODS = 2.0

INDEXED_NOTICE = (
    "**This channel is now archived.** Messages posted here are stored so that "
    "people who can read this channel can search them through me. Someone with "
    "Manage Channels on this channel can stop it with `/unindex`, which also "
    "deletes what was archived."
)


class IndexAction(StrEnum):
    INDEX = "index"
    UNINDEX = "unindex"


class IndexOutcome(StrEnum):
    INDEXED = "indexed"
    ALREADY_INDEXED = "already_indexed"
    UNINDEXED = "unindexed"
    NOT_INDEXED = "not_indexed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class IndexRequest:
    """Who asked, for which channel, to do what.

    Deliberately no free text. The requester is the account the platform
    authenticated and the channel is an id; there is nowhere for a claim of
    authority to be carried, so there is nothing for one to influence.
    """

    requester: PersonRef
    channel_id: int
    action: IndexAction


@dataclass(frozen=True, slots=True)
class ChannelAccess:
    """Permissions on one channel, as live guild state reports them right now."""

    channel: ChannelRef
    name: str
    requester_can_manage: bool
    assistant_can_view: bool
    assistant_can_read_history: bool
    assistant_can_send: bool


class ChannelAccessResolver(Protocol):
    """Resolves permissions from the platform, never from anything the requester said.

    None when the channel is not an indexable channel of this guild -- unknown,
    in another server, or a kind of channel scope does not cover.
    """

    async def resolve(self, requester: PersonRef, channel_id: int) -> ChannelAccess | None: ...


class ChannelPurge(Protocol):
    """One store's existing purge of a channel's content."""

    async def purge_channel(self, channel: ChannelRef) -> int: ...


class IndexNotifier(Protocol):
    """Posts the archive notice in the channel. Returns whether it was posted."""

    async def announce(self, channel: ChannelRef, text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class IndexResult:
    """What happened, and the sentence the requester is shown."""

    outcome: IndexOutcome
    message: str
    missing_permission: str | None = None
    purged: int = 0

    @property
    def allowed(self) -> bool:
        return self.outcome is not IndexOutcome.REFUSED


def requester_actor(person: PersonRef) -> str:
    """The change-record actor for a Discord account.

    Console operator names cannot contain ':' (see `admin.auth`), so no console
    operator can be mistaken for, or forge, an entry made from Discord.
    """
    return f"{person.platform}:{person.platform_user_id}"


def attempt_setting(channel_id: int) -> str:
    """The record's subject for one channel, mirroring `console_access:<name>`.

    Separate from `indexed_channel_ids`, whose own row is written by the
    configuration store in the same statement as the edit: this row says who
    asked from Discord, for which channel, and whether it was allowed.
    """
    return f"discord_indexing:{channel_id}"


def render_scope(channel_ids: frozenset[int]) -> str:
    """Stored text for a scope, in the format `CHANNEL_IDS` parses."""
    return " ".join(str(channel_id) for channel_id in sorted(channel_ids))


class IndexingService:
    """The `/index` and `/unindex` use case.

    One lock per process around read-modify-write of scope: two requests
    arriving together would otherwise each add their channel to the same
    starting set, and the second write would silently drop the first channel.
    """

    def __init__(
        self,
        scope: LiveScope,
        editor: ConfigurationEditor,
        access: ChannelAccessResolver,
        record: ChangeRecordStore,
        purges: Sequence[ChannelPurge],
        notifier: IndexNotifier,
    ) -> None:
        self._scope = scope
        self._editor = editor
        self._access = access
        self._record = record
        self._purges = tuple(purges)
        self._notifier = notifier
        self._lock = asyncio.Lock()
        # Held so a scheduled follow-up purge is not garbage-collected mid-sleep.
        self._follow_ups: set[asyncio.Task[None]] = set()

    async def handle(self, request: IndexRequest) -> IndexResult:
        access = await self._access.resolve(request.requester, request.channel_id)
        refusal = _refusal(request, access)
        if refusal is not None:
            await self._write_record(request, refusal)
            return refusal
        assert access is not None  # `_refusal` returns one when access is None
        async with self._lock:
            result = await self._apply(request, access)
        await self._write_record(request, result)
        return result

    async def wait_for_follow_ups(self) -> None:
        """For tests and shutdown: let scheduled follow-up purges finish."""
        if self._follow_ups:
            await asyncio.gather(*self._follow_ups, return_exceptions=True)

    async def _apply(self, request: IndexRequest, access: ChannelAccess) -> IndexResult:
        # Read stored scope now, not the snapshot from up to one period ago:
        # a channel another operator added since would otherwise be dropped by
        # this write. A read that fails changes nothing, as a refresh would.
        report = await self._scope.refresh()
        if not report.applied:
            return IndexResult(
                IndexOutcome.REFUSED,
                "I couldn't read the current indexing scope, so nothing was changed. "
                "Try again shortly.",
            )
        if request.action is IndexAction.INDEX:
            return await self._index(request, access)
        return await self._unindex(request, access)

    async def _index(self, request: IndexRequest, access: ChannelAccess) -> IndexResult:
        before = self._scope.current()
        mention = f"<#{access.channel.platform_channel_id}>"
        if request.channel_id in before:
            return IndexResult(IndexOutcome.ALREADY_INDEXED, f"{mention} is already indexed.")
        await self._write_scope(before | {request.channel_id}, request.requester)
        if not await self._notifier.announce(access.channel, INDEXED_NOTICE):
            # The notice is the disclosure the channel is owed. An archive its
            # members were not told about is the outcome this feature exists
            # to prevent, so the change is undone rather than kept quietly.
            await self._write_scope(before, request.requester)
            await self._purge(access.channel)
            return IndexResult(
                IndexOutcome.REFUSED,
                f"I couldn't post the archive notice in {mention}, so it was not indexed.",
            )
        return IndexResult(
            IndexOutcome.INDEXED,
            f"{mention} is now indexed. I've posted a notice there, and its history "
            "will be backfilled shortly.",
        )

    async def _unindex(self, request: IndexRequest, access: ChannelAccess) -> IndexResult:
        before = self._scope.current()
        mention = f"<#{access.channel.platform_channel_id}>"
        was_indexed = request.channel_id in before
        if was_indexed:
            await self._write_scope(before - {request.channel_id}, request.requester)
        # Purged either way: content left behind by a removal made elsewhere
        # is still content somebody asked to have withdrawn.
        purged = await self._purge(access.channel)
        self._schedule_follow_up(access.channel)
        if not was_indexed:
            return IndexResult(
                IndexOutcome.NOT_INDEXED,
                f"{mention} wasn't indexed. Anything still archived from it was deleted.",
                purged=purged,
            )
        return IndexResult(
            IndexOutcome.UNINDEXED,
            f"{mention} is no longer indexed, and what was archived from it was deleted.",
            purged=purged,
        )

    async def _write_scope(self, channel_ids: frozenset[int], requester: PersonRef) -> None:
        # Through the editor, which validates and writes the setting and its
        # `indexed_channel_ids` audit row in one statement; then refreshed
        # here so this process's retrieval applies it without waiting a period.
        await self._editor.set(
            INDEXED_CHANNEL_IDS.key, render_scope(channel_ids), requester_actor(requester)
        )
        await self._scope.refresh()

    async def _purge(self, channel: ChannelRef) -> int:
        removed = 0
        for purge in self._purges:
            removed += await purge.purge_channel(channel)
        log.info("indexing.purged_channel", channel=str(channel), removed=removed)
        return removed

    def _schedule_follow_up(self, channel: ChannelRef) -> None:
        task = asyncio.ensure_future(self._follow_up(channel))
        self._follow_ups.add(task)
        task.add_done_callback(self._follow_ups.discard)

    async def _follow_up(self, channel: ChannelRef) -> None:
        """Purge again once every process has applied the removal.

        Ingest captures until its own refresh, so a message posted in that
        window can land after the first purge. Skipped if the channel has been
        indexed again meanwhile -- purging then would delete a live archive.
        """
        await asyncio.sleep(self._scope.interval * FOLLOW_UP_PERIODS)
        try:
            await self._scope.refresh()
            if channel.platform_channel_id in self._scope.current():
                return
            await self._purge(channel)
        except Exception:  # noqa: BLE001 - a background purge must not crash the bot
            log.exception("indexing.follow_up_purge_failed", channel=str(channel))

    async def _write_record(self, request: IndexRequest, result: IndexResult) -> None:
        change = ConfigurationChange(
            operator=requester_actor(request.requester),
            setting=attempt_setting(request.channel_id),
            kind=ChangeKind.APPLIED if result.allowed else ChangeKind.REFUSED,
            after=f"{request.action.value}: {result.outcome.value}",
            reason=result.message,
        )
        try:
            await self._record.record(change)
        except Exception:
            # The scope edit, if there was one, already carries its own audit
            # row from the same statement that wrote it; losing this one is
            # logged loudly rather than turned into a failed command.
            log.exception("indexing.record_failed", setting=change.setting)
        log.info(
            "indexing.attempt",
            requester=str(request.requester),
            channel=request.channel_id,
            action=request.action.value,
            outcome=result.outcome.value,
            missing=result.missing_permission,
        )


def _refusal(request: IndexRequest, access: ChannelAccess | None) -> IndexResult | None:
    """The first reason this request may not proceed, or None.

    Pure over resolved permissions, so every refusal path is testable without
    a guild and none of them can consult anything the requester said.
    """
    if access is None:
        return IndexResult(
            IndexOutcome.REFUSED, "That isn't a text channel in this server I can index."
        )
    if not access.requester_can_manage:
        return _missing(
            MANAGE_CHANNELS, f"You need {MANAGE_CHANNELS} on that channel to change its indexing."
        )
    if request.action is IndexAction.UNINDEX:
        # Withdrawing content needs nothing from the assistant's own access:
        # a channel it can no longer read must still be removable.
        return None
    for granted, permission in (
        (access.assistant_can_view, VIEW_CHANNEL),
        (access.assistant_can_read_history, READ_MESSAGE_HISTORY),
        (access.assistant_can_send, SEND_MESSAGES),
    ):
        if not granted:
            return _missing(
                permission,
                f"I can't index that channel: I'm missing {permission} there.",
            )
    return None


def _missing(permission: str, message: str) -> IndexResult:
    return IndexResult(IndexOutcome.REFUSED, message, missing_permission=permission)
