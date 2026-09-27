"""Postgres implementation of `PersonalKeyStore`: keys sealed with AES-GCM.

The key is sealed under `PERSONAL_SECRETS_KEY` before it reaches a statement,
with `person_secret:<person id>:<kind>` as associated data, so a ciphertext
copied onto another person's row, or read back under another kind, fails to
open instead of being used as that person's bearer. Only the last four
characters are stored in the clear.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import personal_keys_sql
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.admin.oidc.crypto import TokenCipher, UnreadableCiphertext
from chatmemory.app.personal_keys import PersonalKeyStore, last_four
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import HeldKey

log = structlog.get_logger()

KEY_BYTES = 32
KEY_VAR = "PERSONAL_SECRETS_KEY"


class UnusableSecretsKey(ValueError):
    """`PERSONAL_SECRETS_KEY` is not 32 bytes of base64."""


def secrets_cipher(raw: str) -> TokenCipher:
    """The cipher for `PERSONAL_SECRETS_KEY`: 32 bytes, base64 (either alphabet)."""
    value = raw.strip()
    padded = value + "=" * (-len(value) % 4)
    try:
        key = base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as exc:
        raise UnusableSecretsKey(f"{KEY_VAR} must be base64") from exc
    if len(key) != KEY_BYTES:
        raise UnusableSecretsKey(
            f"{KEY_VAR} must decode to {KEY_BYTES} bytes "
            "(generate one with: openssl rand -base64 32)"
        )
    return TokenCipher(key)


def _requester(person: PersonRef) -> dict[str, object]:
    return {"platform": person.platform, "platform_user_id": person.platform_user_id}


def _context(person_id: int, kind: str) -> str:
    return f"person_secret:{person_id}:{kind}"


class PostgresPersonalKeyStore:
    """Implements `PersonalKeyStore`."""

    def __init__(self, engine: AsyncEngine, cipher: TokenCipher) -> None:
        self._engine = engine
        self._cipher = cipher

    async def save(self, person: PersonRef, service: str, key: str) -> HeldKey | None:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            sealed = self._cipher.seal(key, context=_context(person_id, service))
            result = await conn.execute(
                personal_keys_sql.UPSERT_KEY,
                {
                    "person_id": person_id,
                    "kind": service,
                    "ciphertext": sealed,
                    "last4": last_four(key),
                },
            )
            created = result.scalar()
        if created is None:
            return None
        return HeldKey(service, last_four(key), cast(datetime, created))

    async def key_for(self, person: PersonRef, service: str) -> str | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    personal_keys_sql.KEY_OF_REQUESTER, {**_requester(person), "kind": service}
                )
            ).first()
        if row is None:
            return None
        try:
            return self._cipher.open(bytes(row[1]), context=_context(int(row[0]), service))
        except UnreadableCiphertext:
            # A rotated PERSONAL_SECRETS_KEY or a tampered row. Treated as no
            # key: the person is asked to connect again, nothing is sent.
            log.error("personal_keys.unreadable", person=str(person), service=service)
            return None

    async def forget(self, person: PersonRef) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(personal_keys_sql.FORGET_ALL_KEYS, _requester(person))
        return result.rowcount or 0


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresPersonalKeyStore) -> PersonalKeyStore:
        return store
