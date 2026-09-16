"""The change record: who changed which setting, from what, to what, and when.

Configuration that changes without a deploy changes without review. There is
no pull request on a checkbox, so this record is the only review that remains
-- which is why it carries the value before and the value after rather than
only the fact that something happened.

Three properties decide whether it is worth having:

*   **Attributable.** The operator comes from the credential (see `auth`),
    never from the request, so an entry names a person rather than a token.
*   **Append-only.** The port has one write method. There is no update and no
    delete at any layer, so "the console provides no means of altering or
    removing it" is a shape rather than a rule someone remembers. The Postgres
    adapter adds a trigger that refuses UPDATE and DELETE outright, because a
    shape only binds the code that went through it.
*   **Refusals included.** A boundary nobody can see being tested is a
    boundary nobody maintains. The interesting question after an incident is
    usually "what did it decline to do", and a record of successes alone
    cannot answer it.

Enabling a state-changing federated tool is recorded as `ESCALATION` rather
than `APPLIED`. It is not an ordinary edit: it widens what the agent may do to
the world, and the per-invocation confirmation is the only guard left once the
allowlist has been widened from a UI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from chatmemory.admin.auth import Operator

SHELL_ACTOR = "cli:shell"
"""The actor for a change made from the operator CLI rather than the console.

Bootstrapping is circular otherwise: the first console credential has to be
issued by somebody who holds no console credential. Recording that honestly --
as the shell, not as a person -- is better than attributing it to whichever
name happened to be typed. Operator names cannot contain ':' (see
`auth.OPERATOR_NAME`), so no real operator can forge this one.
"""

SECRET_SETTINGS = frozenset(
    {
        "discord_token",
        "llm_api_key",
        "database_url",
    }
)
"""Settings the console may neither read nor write, and so may never record.

The record is written by the same code path that refuses the change, and a
refusal recorded together with the value it refused would defeat the refusal:
the secret would land in the one table built to be kept forever and read by
operators. Raising here means such an entry cannot be written even by mistake.
"""


class SecretNeverRecorded(Exception):
    """A change naming an environment-only secret reached the record."""


class ChangeKind(StrEnum):
    APPLIED = "applied"
    """An ordinary edit that took effect."""

    REFUSED = "refused"
    """An attempt the system rejected. `reason` says why."""

    ESCALATION = "escalation"
    """A change that widened what the agent may do -- enabling a mutating
    federated tool, or granting somebody console access. Distinct from
    APPLIED so that "what got more permissive, and who did it" is a filter
    rather than a reading exercise."""


def _normalised(setting: str) -> str:
    return setting.strip().lower()


@dataclass(frozen=True, slots=True)
class ConfigurationChange:
    """One change, as the caller describes it. The record is what comes back.

    `before` and `after` are rendered strings rather than typed values: the
    record outlives the schema of whatever was being configured, and an entry
    that cannot be read once a setting has been removed is not a record.
    """

    operator: str
    setting: str
    kind: ChangeKind
    before: str | None = None
    after: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.operator.strip():
            raise ValueError("a change must name who made it")
        if not self.setting.strip():
            raise ValueError("a change must name what it changed")
        if _normalised(self.setting) in SECRET_SETTINGS:
            raise SecretNeverRecorded(
                f"{self.setting} is environment-only and must not reach the record"
            )


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One written entry. Immutable, in the type as well as in the table."""

    sequence: int
    recorded_at: datetime
    operator: str
    setting: str
    kind: ChangeKind
    before: str | None
    after: str | None
    reason: str | None


class ChangeRecordStore(Protocol):
    """The change-record port.

    `record` and `recent`. An edit or a deletion is not something a caller can
    express, so no console route -- including one added later by someone who
    has not read this file -- can reach one.
    """

    async def record(
        self, change: ConfigurationChange, *, now: datetime | None = None
    ) -> ChangeRecord:
        """Write one entry. Returns what was written, sequence and all."""
        ...

    async def recent(self, limit: int = 100) -> Sequence[ChangeRecord]:
        """The newest entries first, for the console's audit view."""
        ...


# --- constructors ------------------------------------------------------
#
# The console path takes an `Operator`, which only `auth.current_operator`
# hands out, so a handler cannot attribute a change to a name it was given.
# `by_shell` is the one way to write an entry that is not a named operator,
# and it lives here where it is visible rather than in whatever calls it.


def applied(
    operator: Operator, setting: str, before: str | None, after: str | None
) -> ConfigurationChange:
    """An edit that took effect."""
    return ConfigurationChange(
        operator=operator.name,
        setting=setting,
        kind=ChangeKind.APPLIED,
        before=before,
        after=after,
    )


def refused(
    operator: Operator,
    setting: str,
    reason: str,
    *,
    before: str | None = None,
    attempted: str | None = None,
) -> ConfigurationChange:
    """An attempt the system rejected, with what was attempted and why.

    `after` carries the value that was *asked for* and did not take effect.
    Recording it is the point: "somebody tried to allowlist this tool and was
    refused" is the entry that matters when the same tool turns up enabled a
    week later.
    """
    return ConfigurationChange(
        operator=operator.name,
        setting=setting,
        kind=ChangeKind.REFUSED,
        before=before,
        after=attempted,
        reason=reason,
    )


def escalation(
    operator: Operator,
    setting: str,
    before: str | None,
    after: str | None,
    reason: str | None = None,
) -> ConfigurationChange:
    """A change that widened what the agent, or the console, may do."""
    return ConfigurationChange(
        operator=operator.name,
        setting=setting,
        kind=ChangeKind.ESCALATION,
        before=before,
        after=after,
        reason=reason,
    )


def by_shell(
    setting: str,
    kind: ChangeKind,
    before: str | None,
    after: str | None,
    reason: str | None = None,
) -> ConfigurationChange:
    """A change made from the operator CLI, attributed to the shell.

    Used for issuing and revoking console credentials, which cannot be
    attributed to a console operator without inventing one.
    """
    return ConfigurationChange(
        operator=SHELL_ACTOR,
        setting=setting,
        kind=kind,
        before=before,
        after=after,
        reason=reason,
    )


class InMemoryChangeRecord:
    """A process-local change record, for tests and single-process development.

    Honest about its limits: it does not survive a restart. What it does
    reproduce is the shape -- append, read, nothing else -- so a test that
    reaches for an edit fails here exactly as it would against Postgres.
    """

    def __init__(self) -> None:
        self._entries: list[ChangeRecord] = []

    async def record(
        self, change: ConfigurationChange, *, now: datetime | None = None
    ) -> ChangeRecord:
        entry = ChangeRecord(
            sequence=len(self._entries) + 1,
            recorded_at=now or datetime.now().astimezone(),
            operator=change.operator,
            setting=change.setting,
            kind=change.kind,
            before=change.before,
            after=change.after,
            reason=change.reason,
        )
        self._entries.append(entry)
        return entry

    async def recent(self, limit: int = 100) -> Sequence[ChangeRecord]:
        # A tuple of frozen dataclasses: a caller holding the result cannot
        # append to it, reorder it, or edit an entry in place.
        return tuple(reversed(self._entries[-limit:])) if limit > 0 else ()
