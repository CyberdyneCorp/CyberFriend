"""Bearer credentials that *determine* the viewer.

The MCP endpoint is the one service given a public domain, so anything the
caller says about itself is an assertion. A `viewer` request parameter would
therefore turn the entire ACL into an honour system: the attacker simply
names someone with broader access.

So identity is not a parameter at all. A token maps to exactly one person,
the mapping lives on the server, and the request has no field capable of
selecting a different one. The blast radius of a leaked token is then one
person's view rather than the whole corpus.

Only hashes are stored. A database dump, a log line, or a backup therefore
yields nothing that can be replayed against the endpoint.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from chatmemory.domain.identity import PersonRef

TOKEN_PREFIX = "cfm_"
"""Marks our tokens in logs and secret scanners. Carries no entropy."""

TOKEN_ENTROPY_BYTES = 32


def generate_token() -> str:
    """A fresh credential. Returned once, at issue, and never recoverable."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def hash_token(token: str) -> str:
    """The stored form of a token.

    A single SHA-256 rather than a password KDF, deliberately: the token is
    256 bits of CSPRNG output, not a human-chosen secret, so there is no
    dictionary to slow down. A KDF here would only add per-request latency to
    every tool call while defending against nothing.
    """
    return sha256(token.encode("utf-8")).hexdigest()


def hashes_match(left: str, right: str) -> bool:
    """Constant-time comparison, for stores that scan rather than index."""
    return hmac.compare_digest(left, right)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A newly minted credential.

    `token` is plaintext and exists only in the return value of `issue`. It is
    never persisted, so losing it means rotating rather than recovering.
    """

    person: PersonRef
    token: str
    token_hash: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """A stored credential, without the credential."""

    person: PersonRef
    token_hash: str
    label: str
    issued_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class TokenDirectory(Protocol):
    """Reviewing and withdrawing credentials, without the power to mint one.

    This is the half of the token store a surface may hold when it is not
    trusted with the corpus. Minting a credential is a grant of read access
    to one person's whole view of Discord, so the ability to do it is kept out
    of the *type* a caller such as the admin console receives rather than out
    of its handlers: a route that tried to mint would then fail to type-check
    instead of shipping and being found by an audit.

    Both operations here are safe in that sense. `active_tokens` returns
    hashes and labels, never a credential, and `revoke` can only narrow what
    exists.
    """

    async def revoke(self, person: PersonRef) -> int:
        """Revoke every live credential of one person. Returns how many."""
        ...

    async def active_tokens(self) -> Sequence[TokenRecord]:
        """Live credentials, for an operator audit. Hashes only."""
        ...


class TokenStore(TokenDirectory, Protocol):
    """The token -> person mapping.

    Note what is absent: there is no method that takes a person and returns
    content, and none that takes a token and returns anything but the one
    person it was issued to. Impersonation has no entry point here.

    The minting half is held only by the MCP surface and by the shell that
    runs `python -m chatmemory.mcp.issue_token`; see `TokenDirectory` for what
    the console gets instead.
    """

    async def person_for_token(self, token: str) -> PersonRef | None:
        """The person this credential acts as, or None if it is not valid.

        Returns None identically for an unknown token and a revoked one: the
        caller learns only that the credential does not work.
        """
        ...

    async def issue(self, person: PersonRef, label: str = "") -> IssuedToken:
        """Mint an additional credential for one person."""
        ...

    async def rotate(self, person: PersonRef, label: str = "") -> IssuedToken:
        """Replace one person's credentials, leaving everyone else's alone.

        Scoped to the person on purpose: rotation is routine (a laptop is
        lost, someone leaves a token in a shell history), and a rotation that
        forced everyone else to re-key would simply not be done.
        """
        ...


class InMemoryTokenStore:
    """A token store for tests and single-process development.

    Stores hashes like the real one, so a test that accidentally asserts on
    stored plaintext fails here too rather than only in production.
    """

    def __init__(self) -> None:
        self._records: dict[str, TokenRecord] = {}

    async def person_for_token(self, token: str) -> PersonRef | None:
        record = self._records.get(hash_token(token))
        if record is None or not record.is_active:
            return None
        return record.person

    async def issue(self, person: PersonRef, label: str = "") -> IssuedToken:
        token = generate_token()
        digest = hash_token(token)
        self._records[digest] = TokenRecord(
            person=person,
            token_hash=digest,
            label=label,
            issued_at=datetime.now().astimezone(),
        )
        return IssuedToken(person=person, token=token, token_hash=digest, label=label)

    async def revoke(self, person: PersonRef) -> int:
        now = datetime.now().astimezone()
        revoked = 0
        for digest, record in list(self._records.items()):
            if record.person == person and record.is_active:
                self._records[digest] = TokenRecord(
                    person=record.person,
                    token_hash=record.token_hash,
                    label=record.label,
                    issued_at=record.issued_at,
                    revoked_at=now,
                )
                revoked += 1
        return revoked

    async def rotate(self, person: PersonRef, label: str = "") -> IssuedToken:
        await self.revoke(person)
        return await self.issue(person, label)

    async def active_tokens(self) -> Sequence[TokenRecord]:
        return [r for r in self._records.values() if r.is_active]
