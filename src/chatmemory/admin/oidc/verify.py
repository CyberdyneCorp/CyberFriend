"""Token verification, exactly as the CyberdyneAuth contract states it.

Access token: RS256 against the issuer's JWKS, `iss` equal to discovery's,
`exp` in the future, `type == "access"`, `aud == "cyberfriend"`. Id token:
RS256, `iss`, `aud == client_id`, `exp`, not `type == "access"`, and -- at
sign-in -- the nonce that sign-in issued. People are keyed on `sub`.

Expiry is checked against an injected clock rather than PyJWT's own, so the
session code and these rules agree about what "now" is.

Roles come from the verified access token only. The claim is a list of
`"<client_id>:<role>"`; an absent (or non-list) claim is *unknown* and is
returned as None, which every caller treats as a denial. A present claim with
neither console role is an empty set: a different refusal, but still one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import jwt

from chatmemory.admin.auth import Role
from chatmemory.admin.oidc.config import ACCESS_AUDIENCE
from chatmemory.admin.oidc.crypto import same_digest

ALGORITHM = "RS256"

#: OIDC Core: `sub` is at most 255 ASCII characters. No whitespace, so it can
#: be written into `oidc:<sub>` and into log lines without escaping.
SUBJECT = re.compile(r"\A[\x21-\x7e]{1,255}\Z")

ROLES_CLAIM = "roles"


class InvalidToken(Exception):
    """A token failed one rule. The message is the rule's name, never the token."""


class KeySource(Protocol):
    """The issuer's signing keys, by key id."""

    async def key_for(self, kid: str | None) -> Any | None:
        """The public key for `kid`, refetching the key set once on a miss."""
        ...


@dataclass(frozen=True, slots=True)
class AccessClaims:
    sub: str
    #: None: the roles claim is absent -- unknown, and denied.
    roles: frozenset[Role] | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IdClaims:
    sub: str
    expires_at: datetime
    auth_time: datetime | None


async def verify_access_token(
    token: str, *, keys: KeySource, issuer: str, client_id: str, now: datetime
) -> AccessClaims:
    claims = await _verified(token, keys=keys, issuer=issuer, audience=ACCESS_AUDIENCE, now=now)
    if claims.get("type") != "access":
        raise InvalidToken("type")
    return AccessClaims(
        sub=claims["sub"],
        roles=roles_from(claims, client_id),
        expires_at=_moment(claims["exp"]),
    )


async def verify_id_token(
    token: str,
    *,
    keys: KeySource,
    issuer: str,
    client_id: str,
    now: datetime,
    nonce_hash: str | None,
) -> IdClaims:
    """`nonce_hash` is the sign-in's; None only for an id token from a refresh."""
    claims = await _verified(token, keys=keys, issuer=issuer, audience=client_id, now=now)
    # The client id is `cyberfriend`, the access audience too, so `aud` alone
    # cannot tell the two apart: an access token is never an id token.
    if claims.get("type") == "access":
        raise InvalidToken("type")
    if nonce_hash is not None:
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not same_digest(nonce, nonce_hash):
            raise InvalidToken("nonce")
    auth_time = claims.get("auth_time")
    return IdClaims(
        sub=claims["sub"],
        expires_at=_moment(claims["exp"]),
        auth_time=_moment(auth_time) if _is_number(auth_time) else None,
    )


def roles_from(claims: dict[str, Any], client_id: str) -> frozenset[Role] | None:
    raw = claims.get(ROLES_CLAIM)
    if not isinstance(raw, list):
        return None
    return frozenset(role for role in Role if f"{client_id}:{role.value}" in raw)


async def _verified(
    token: str, *, keys: KeySource, issuer: str, audience: str, now: datetime
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidToken("malformed") from exc
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("alg")
    kid = header.get("kid")
    key = await keys.key_for(kid if isinstance(kid, str) else None)
    if key is None:
        raise InvalidToken("kid")
    claims = _decode(token, key, issuer=issuer, audience=audience)
    _check_times(claims, now)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not SUBJECT.match(sub):
        raise InvalidToken("sub")
    return claims


def _decode(token: str, key: Any, *, issuer: str, audience: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=issuer,
            options={
                "verify_exp": False,  # checked below against the injected clock
                "verify_nbf": False,
                "verify_iat": False,
                "require": ["exp", "iss", "aud", "sub"],
            },
        )
    except jwt.InvalidAudienceError as exc:
        raise InvalidToken("aud") from exc
    except jwt.InvalidIssuerError as exc:
        raise InvalidToken("iss") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise InvalidToken(f"missing {exc.claim}") from exc
    except jwt.PyJWTError as exc:
        raise InvalidToken("signature") from exc


def _check_times(claims: dict[str, Any], now: datetime) -> None:
    exp = _number(claims.get("exp"))
    if exp is None or exp <= now.timestamp():
        raise InvalidToken("exp")
    if "nbf" in claims:
        nbf = _number(claims["nbf"])
        if nbf is None or nbf > now.timestamp():
            raise InvalidToken("nbf")


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and _is_number(value) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _moment(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)
