"""Indexing scope, as the running processes see it right now.

Scope used to be a frozenset read once from the environment when a process
started. The admin console could store a new one, and nothing read it back:
adding a channel meant a redeploy, and removing one left it captured and
retrievable until somebody restarted both processes. This module is the one
place a consumer asks "is this channel in scope?", and the answer it gives is
the stored value in force at the moment of asking.

Consumers hold a `ScopeProvider` and call `current()` each time they act. A
consumer that copies the set at construction is exactly the bug this replaces,
so nothing here hands out a set to keep.

Two rules are inherited from `chatmemory.app.configuration` rather than
restated in code, and they are why this does not read the table itself:

*   precedence is database, then environment, then default -- so a deployment
    that never touches the console behaves as it always did;
*   a refresh that cannot read stored configuration keeps the scope already in
    force. Falling back to the environment would silently re-add a channel an
    operator removed, which is the dangerous direction for an archive.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

from chatmemory.app.configuration import (
    INDEXED_CHANNEL_IDS,
    REFRESH_INTERVAL_SECONDS,
    ConfigurationSnapshot,
    RefreshReport,
    RuntimeConfiguration,
)
from chatmemory.config import Settings
from chatmemory.ports.configuration import ConfigurationStore

log = structlog.get_logger()


@runtime_checkable
class ScopeProvider(Protocol):
    """The channel ids in indexing scope, as of the moment of the call.

    Deliberately a single method returning an immutable set: a consumer checks
    membership against one consistent value per decision, and there is nothing
    to mutate that could leave two consumers disagreeing about what "current"
    means.
    """

    def current(self) -> frozenset[int]: ...


@dataclass(frozen=True, slots=True)
class StaticScope:
    """A scope that never changes: for tests and for callers with no database.

    Not a production default. A process that answers questions or ingests
    should be handed a `LiveScope`; this exists so that a resolver built in a
    test from a literal set of ids does not need a configuration store.
    """

    channel_ids: frozenset[int]

    def current(self) -> frozenset[int]:
        return self.channel_ids


def as_scope(scope: ScopeProvider | Iterable[int]) -> ScopeProvider:
    """Accept either a provider or a fixed set of ids.

    Kept so existing callers that pass a literal set keep working, while every
    caller that passes a provider gets live behaviour without a second
    constructor to choose between.
    """
    if isinstance(scope, ScopeProvider):
        return scope
    return StaticScope(frozenset(scope))


@dataclass(frozen=True, slots=True)
class ScopeChange:
    """What one refresh did to scope.

    Split into both directions because consumers care about them differently:
    ingest has new history to fetch when a channel is added, and nothing to
    fetch when one is removed.
    """

    added: frozenset[int]
    removed: frozenset[int]
    current: frozenset[int]


ScopeObserver = Callable[[ScopeChange], None]


class LiveScope:
    """Scope read from runtime configuration, refreshed on a bounded period.

    Holds no copy of the scope: `current()` reads the configuration snapshot
    in force, so there is no second value to fall out of date. Observers are
    told only when the channel set itself moves -- a change to some unrelated
    setting in the same refresh is not a scope change.

    One `run_forever` per underlying `RuntimeConfiguration`. Sharing a
    configuration with another consumer is fine; running two refresh loops over
    it only reads the table twice as often.
    """

    def __init__(
        self,
        configuration: RuntimeConfiguration,
        *,
        interval: float = REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._configuration = configuration
        self._interval = interval
        self._observers: list[ScopeObserver] = []
        # The last scope observers were told about, so a refresh that changed
        # only other settings does not wake a backfill sweep for nothing.
        self._announced = self.current()
        self._last_report: RefreshReport | None = None
        self._last_refresh_at: float | None = None
        configuration.on_change(self._configuration_changed)

    @classmethod
    def from_settings(
        cls,
        store: ConfigurationStore,
        settings: Settings,
        environ: Mapping[str, str],
        *,
        interval: float = REFRESH_INTERVAL_SECONDS,
    ) -> LiveScope:
        """The same construction for every process, so bot and ingest agree.

        Starts from the environment; call `refresh` before acting on it so that
        a channel stored-but-removed is not captured during the first period.
        """
        return cls(RuntimeConfiguration.from_settings(store, settings, environ), interval=interval)

    @property
    def interval(self) -> float:
        """The refresh period: the bound on how long a scope change takes to apply."""
        return self._interval

    @property
    def configuration(self) -> RuntimeConfiguration:
        return self._configuration

    def current(self) -> frozenset[int]:
        return self._configuration.current.get(INDEXED_CHANNEL_IDS)

    def on_change(self, observer: ScopeObserver) -> None:
        """Register something that must act on a scope change, not just read it."""
        self._observers.append(observer)

    async def refresh(self) -> RefreshReport:
        """Re-read stored configuration once. Never raises.

        A failed read keeps the scope in force; see the module docstring.
        """
        report = await self._configuration.refresh()
        self._last_report = report
        self._last_refresh_at = time.time()
        if not report.applied:
            log.warning(
                "scope.refresh_failed",
                kept=len(self.current()),
                detail="keeping the indexing scope already in force",
            )
        return report

    async def run_forever(self) -> None:
        """Refresh on the configured period for as long as the process runs.

        Sleeps after refreshing, not before: a process that has just started
        wants stored scope now, not one period from now.
        """
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)

    def status(self) -> dict[str, object]:
        """For the health endpoint: a frozen scope looks exactly like a quiet one."""
        return {
            "channels": len(self.current()),
            "source": self._configuration.current.source(INDEXED_CHANNEL_IDS).value,
            "last_refresh_at": self._last_refresh_at,
            "last_refresh_applied": (
                None if self._last_report is None else self._last_report.applied
            ),
        }

    def _configuration_changed(self, snapshot: ConfigurationSnapshot) -> None:
        now = snapshot.get(INDEXED_CHANNEL_IDS)
        if now == self._announced:
            return
        change = ScopeChange(
            added=now - self._announced, removed=self._announced - now, current=now
        )
        self._announced = now
        log.info(
            "scope.changed",
            added=sorted(change.added),
            removed=sorted(change.removed),
            channels=len(now),
        )
        for observer in self._observers:
            try:
                observer(change)
            except Exception:
                # One consumer that cannot act on a change must not stop the
                # others from seeing it; the configuration's own loop would
                # swallow this too, but only after skipping every later
                # observer registered here.
                log.exception("scope.observer_failed", observer=repr(observer))
