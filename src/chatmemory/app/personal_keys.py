"""A person's own CyberWealth connected-app key: taken in a DM, sent on their calls only.

CyberWealth answers `my_*` tools only for a user-issued connected-app key
(`cwk_live_...`). The person creates one in CyberWealth and hands it over in
a direct message, either by writing it ("minha chave do cyberwealth é
cwk_live_...") or with `/connect`. From then on it is the bearer on that
person's own `my_*` calls, answered in their DMs, and on nothing else.

What this module decides, and nothing more:

*   what a key looks like (`find_key`), and what merely mentions one
    (`mentions_key`) -- the second is what keeps a key pasted into a channel
    out of the archive and out of every prompt;
*   that a key is taken only from a direct message (`PersonalKeys.connect`);
*   that it is never shown back: only its last four characters are.

Encryption is the store's job: the port takes and returns plaintext, and the
adapter seals it under `PERSONAL_SECRETS_KEY` before it reaches a row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import HeldKey

log = structlog.get_logger()

CYBERWEALTH = "cyberwealth"
"""The federated server a connected-app key is for, and the key's kind."""

KEY_MARKER = "cwk_"

#: CyberWealth's own shape: `cwk_<env>_<prefix10>_<secret43>` (its gitleaks rule).
_KEY = re.compile(
    r"(?<![A-Za-z0-9_-])cwk_(?:live|test|dev)_[0-9A-HJKMNP-TV-Z]{10}_[A-Za-z0-9_-]{43}"
    r"(?![A-Za-z0-9_-])"
)

SHOWN_CHARACTERS = 4


def mentions_key(text: str) -> bool:
    """Whether `text` carries anything that looks like a CyberWealth key.

    Deliberately wider than `find_key`: a key cut short, wrapped in a code
    block or mistyped is still a secret, and a false positive costs one
    message not archived and one warning.
    """
    return KEY_MARKER in text.casefold()


def find_key(text: str) -> str | None:
    """The one well-formed key in `text`, or None (none, or more than one)."""
    found = set(_KEY.findall(text))
    return found.pop() if len(found) == 1 else None


def last_four(key: str) -> str:
    return key[-SHOWN_CHARACTERS:]


class PersonalKeyStore(Protocol):
    """Keys at rest. The adapter encrypts; this port sees plaintext only in `save`
    and `key_for`, keyed on the person's own platform identity."""

    async def save(self, person: PersonRef, service: str, key: str) -> HeldKey | None:
        """Store (or replace) the key. None when the person has opted out."""
        ...

    async def key_for(self, person: PersonRef, service: str) -> str | None: ...

    async def held(self, person: PersonRef, service: str) -> HeldKey | None:
        """Whether the person holds a key for `service`: its last four
        characters and date, read without opening the ciphertext."""
        ...

    async def forget(self, person: PersonRef, service: str | None = None) -> int:
        """Delete the person's key for `service`, or every key of theirs when
        None. Returns how many were held."""
        ...


class ConnectResult(StrEnum):
    CONNECTED = "connected"
    NOT_DIRECT = "not_direct"
    MALFORMED = "malformed"
    REFUSED = "refused"
    """Not stored: the person has opted out."""


@dataclass(frozen=True, slots=True)
class ConnectOutcome:
    result: ConnectResult
    held: HeldKey | None = None


class PersonalKeys:
    """Connect, look up and forget a person's keys."""

    def __init__(self, store: PersonalKeyStore) -> None:
        self._store = store

    async def connect(self, person: PersonRef, text: str, *, direct: bool) -> ConnectOutcome:
        """Store the key in `text` for `person`, only when it came in a DM.

        The text is never logged, and neither is the key: the log line names
        the person and the result.
        """
        if not direct:
            log.info("personal_keys.refused_outside_direct", person=str(person))
            return ConnectOutcome(ConnectResult.NOT_DIRECT)
        key = find_key(text)
        if key is None:
            return ConnectOutcome(ConnectResult.MALFORMED)
        held = await self._store.save(person, CYBERWEALTH, key)
        if held is None:
            return ConnectOutcome(ConnectResult.REFUSED)
        log.info("personal_keys.connected", person=str(person), service=CYBERWEALTH)
        return ConnectOutcome(ConnectResult.CONNECTED, held)

    def covers(self, server: str) -> bool:
        """Whether `server`'s personal tools are called with the person's own key."""
        return server == CYBERWEALTH

    async def bearer(self, person: PersonRef, server: str) -> str | None:
        """The person's own key for `server`, or None."""
        if not self.covers(server):
            return None
        return await self._store.key_for(person, server)

    async def held(self, person: PersonRef) -> HeldKey | None:
        """The person's CyberWealth key as it may be shown: its last four
        characters. The key itself is never read for this."""
        return await self._store.held(person, CYBERWEALTH)

    async def forget(self, person: PersonRef, service: str | None = None) -> int:
        """Delete the person's key for `service`, or all of them when None."""
        removed = await self._store.forget(person, service)
        if removed:
            log.info(
                "personal_keys.forgotten", person=str(person), service=service, removed=removed
            )
        return removed
