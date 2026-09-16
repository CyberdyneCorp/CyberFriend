"""Configuration an operator may change while the system is running.

The vocabulary, not the policy: what a stored setting looks like, where a
resolved value came from, and what a store of stored settings has to be able
to do. The precedence rules and the setting registry live in
`chatmemory.app.configuration`, because they are decisions rather than
persistence.

Two properties of this port are load-bearing and are stated here rather than
left to an implementation to remember:

*   `load` returns what is stored, and nothing else. It never invents a value
    for a key that is absent -- "absent" is how the environment stays in
    charge of a setting nobody has overridden, and a store that filled the gap
    would make that indistinguishable from an override.
*   every write records who made it and what it replaced, in the same
    transaction as the write. An audit row written afterwards is an audit row
    a crash can lose, and the whole reason stored configuration exists is that
    a change without a record is what the environment already gave us.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class SettingSource(Enum):
    """Where a resolved value came from.

    Reported rather than inferred: a setting edited in the console but
    overridden somewhere else is otherwise indistinguishable from one that did
    not save, and the operator's next move differs completely between the two.
    """

    DATABASE = "database"
    ENVIRONMENT = "environment"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class StoredSetting:
    """One row of stored configuration, exactly as it was written.

    `raw` is the operator's text, unparsed. Parsing belongs to the resolver,
    which knows what the setting means and can keep the previous value when
    the text does not parse; a store that parsed would have to choose between
    raising -- taking down a process over one bad row -- and silently dropping
    the row.
    """

    key: str
    raw: str
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    """A value in force, and the provenance that explains it."""

    value: object
    source: SettingSource
    #: Who wrote it, and when. Set only for a value that came from the
    #: database -- the environment cannot say who exported a variable.
    updated_by: str | None = None
    updated_at: datetime | None = None


class ConfigurationStore(Protocol):
    """Stored configuration and the record of how it got that way."""

    async def load(self) -> Sequence[StoredSetting]:
        """Every stored setting.

        Raises rather than returning an empty sequence when the database
        cannot be reached. The difference matters more here than anywhere
        else: "nothing is stored" means the environment is in charge, and a
        failed read reported that way would silently undo every override an
        operator has made -- including the narrowing ones.
        """
        ...

    async def put(self, key: str, raw: str, operator: str) -> None:
        """Store `raw` under `key`, recording the operator and what it replaced.

        One transaction: the audit row and the setting move together or
        neither does.
        """
        ...

    async def clear(self, key: str, operator: str) -> None:
        """Remove a stored setting, so the environment takes it back.

        Records the removal with the value that was removed. Clearing a key
        that is not stored changes nothing and records nothing.
        """
        ...

    async def record_refusal(self, key: str, operator: str, reason: str) -> None:
        """Record a change that was refused, and why.

        The attempted value is deliberately not part of this call. The most
        likely refusal is somebody pasting a credential into the console, and
        an audit trail that helpfully preserved the rejected text would turn
        every such mistake into a stored secret.
        """
        ...
