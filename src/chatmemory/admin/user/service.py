"""User sign-in: `/link`, the plain and the fresh sign-in, and user sessions.

Every user sign-in starts and ends like the console's (`admin.oidc.service`):
a browser-bound login record, code + PKCE, both tokens verified, the id
token's nonce, and `userinfo.sub == id_token.sub == access_token.sub`. Then,
by the login's purpose:

*   **link** -- started by `/link?code=...` with `prompt=login`. The code's
    sha256 rides in the login record, so only the browser that opened the
    link can finish it. The link is made only when userinfo says
    `email_verified` is true and the email's HMAC equals the one the person
    consented with (`AccountLinking`, one transaction). The person is then
    signed in.
*   **user** -- a plain sign-in. Any valid access token may open a user
    session: roles are not consulted, because a plain user may have none.
*   **fresh** -- a sign-in with `max_age=300`. OIDC requires `auth_time` in the
    id token then; it must be there and within five minutes, and it becomes
    the session's `fresh_auth_at`, which "delete everything" requires to be
    within five minutes.

A session belongs to a subject, not a person: who the person is comes from
`person_account_link` on every request, so an unlink or an erasure takes
effect on the next request.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import structlog

from chatmemory.admin.auth import UNAUTHENTICATED, Denied
from chatmemory.admin.oidc.crypto import (
    TokenCipher,
    UnreadableCiphertext,
    digest,
    random_value,
)
from chatmemory.admin.oidc.provider import ProviderUnavailable, TokenRequestFailed
from chatmemory.admin.oidc.service import (
    IDLE_TIMEOUT,
    REFRESH_WINDOW,
    Begun,
    SignIn,
    SignInFailed,
    Verified,
    email_of,
    refresh_lifetime,
)
from chatmemory.admin.oidc.store import LoginRecord
from chatmemory.admin.oidc.verify import InvalidToken, verify_id_token
from chatmemory.admin.user.store import (
    RefreshedUserTokens,
    UserSessionRecord,
    UserSessionStore,
)
from chatmemory.app.accounts import AccountLinking
from chatmemory.ports.accounts import LinkOutcome

log = structlog.get_logger()

USER_PURPOSE = "user"
LINK_PURPOSE = "link"
FRESH_PURPOSE = "fresh"
USER_PURPOSES = frozenset({USER_PURPOSE, LINK_PURPOSE, FRESH_PURPOSE})

FRESH_SECONDS = 300
"""`max_age` for the sign-in "delete everything" needs, and how long it counts."""
FRESH_WINDOW = timedelta(seconds=FRESH_SECONDS)
CLOCK_SKEW = timedelta(seconds=60)
"""How far in the future an `auth_time` may be and still be believed."""

_FAILURES = (InvalidToken, ProviderUnavailable, TokenRequestFailed, UnreadableCiphertext)


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    """The signed-in account on a `/me` request. Not a person: see `person_for`."""

    sub: str
    email: str | None
    session_hash: str
    fresh_auth_at: datetime | None

    def fresh(self, now: datetime) -> bool:
        return self.fresh_auth_at is not None and now - self.fresh_auth_at <= FRESH_WINDOW


@dataclass(frozen=True, slots=True)
class UserSignedIn:
    session_id: str
    #: Set for a `/link` sign-in: the link was made.
    linked: bool = False


class LinkFailure(StrEnum):
    """Why `/link` made no link. Each has its own page: none reveals more than
    the person signed in already knows about their own account."""

    FAILED = "failed"
    UNVERIFIED = "unverified"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class LinkRefused:
    failure: LinkFailure
    reason: str


UserSignInResult = UserSignedIn | LinkRefused | SignInFailed

_LINK_REFUSALS = {
    LinkOutcome.BAD_CODE: LinkFailure.FAILED,
    LinkOutcome.OTHER_EMAIL: LinkFailure.REFUSED,
    LinkOutcome.SUBJECT_TAKEN: LinkFailure.REFUSED,
}


class UserSignIn:
    """Sign-in and sessions for the user area, over the console's OIDC client."""

    def __init__(
        self,
        sign_in: SignIn,
        sessions: UserSessionStore,
        linking: AccountLinking,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sign_in = sign_in
        self._sessions = sessions
        self._linking = linking
        self._clock = clock
        self._cipher = TokenCipher(sign_in.settings.session_key)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @property
    def sign_in(self) -> SignIn:
        return self._sign_in

    @property
    def linking(self) -> AccountLinking:
        return self._linking

    def now(self) -> datetime:
        return self._clock()

    # --- starting ----------------------------------------------------------

    async def begin(self, purpose: str, *, code_sha256: bytes | None = None) -> Begun:
        """Start a user sign-in. `/link` passes the code's sha256 and asks for
        a fresh login; the fresh sign-in asks for `max_age`."""
        if purpose not in USER_PURPOSES:
            raise ValueError(f"not a user sign-in: {purpose}")
        return await self._sign_in.begin(
            purpose=purpose,
            link_code_hash=code_sha256.hex() if code_sha256 is not None else None,
            max_age=FRESH_SECONDS if purpose == FRESH_PURPOSE else None,
            prompt="login" if purpose == LINK_PURPOSE else None,
        )

    # --- the callback ------------------------------------------------------

    async def complete(
        self, login: LoginRecord, *, code: str | None, binding: str | None
    ) -> UserSignInResult:
        """Finish a user sign-in whose login record the callback consumed."""
        if login.purpose not in USER_PURPOSES:
            return _failed("state")
        verified = await self._sign_in.verify_callback(login, code=code, binding=binding)
        if isinstance(verified, SignInFailed):
            return verified
        fresh_at = self._fresh_auth(login, verified)
        if isinstance(fresh_at, SignInFailed):
            return fresh_at
        if login.purpose == LINK_PURPOSE:
            refused = await self._link(login, verified)
            if refused is not None:
                return refused
        session_id = await self._create_session(verified, fresh_at)
        return UserSignedIn(session_id, linked=login.purpose == LINK_PURPOSE)

    def _fresh_auth(self, login: LoginRecord, verified: Verified) -> datetime | None | SignInFailed:
        """The verified `auth_time` when `max_age` was asked for, else None."""
        if login.max_age is None:
            return None
        auth_time = verified.identity.auth_time
        if auth_time is None:
            return _failed("no auth_time after max_age")
        age = verified.now - auth_time
        if age > timedelta(seconds=login.max_age) or age < -CLOCK_SKEW:
            return _failed("auth_time outside max_age")
        return auth_time

    async def _link(self, login: LoginRecord, verified: Verified) -> LinkRefused | None:
        if login.link_code_hash is None:
            return _refused(LinkFailure.FAILED, "no code")
        email = email_of(verified.info)
        if verified.info.get("email_verified") is not True or email is None:
            return _refused(LinkFailure.UNVERIFIED, "email not verified")
        outcome = await self._linking.link(
            bytes.fromhex(login.link_code_hash),
            email=email,
            issuer=await self._sign_in.provider.issuer(),
            sub=verified.access.sub,
        )
        if outcome is LinkOutcome.LINKED:
            return None
        return _refused(_LINK_REFUSALS[outcome], outcome.value)

    async def _create_session(self, verified: Verified, fresh_at: datetime | None) -> str:
        session_id = random_value()
        id_hash = digest(session_id)
        tokens, now = verified.tokens, verified.now
        await self._sessions.create(
            UserSessionRecord(
                id_hash=id_hash,
                sub=verified.access.sub,
                email=email_of(verified.info),
                access_token_enc=self._seal(tokens.access_token, "access", id_hash),
                refresh_token_enc=self._seal_optional(tokens.refresh_token, "refresh", id_hash),
                id_token_enc=self._seal(verified.id_token, "id", id_hash),
                access_expires_at=verified.access.expires_at,
                created_at=now,
                last_seen_at=now,
                expires_at=now + refresh_lifetime(tokens),
                fresh_auth_at=fresh_at,
            )
        )
        log.info("user.signed_in", sub=verified.access.sub, fresh=fresh_at is not None)
        return session_id

    # --- a request on a session --------------------------------------------

    async def authenticate(self, session_id: str) -> UserPrincipal | Denied:
        """The session's account, refreshed first when its token is nearly spent.

        Refreshing is serialised per session, as the console's is: refresh
        tokens rotate with reuse detection, and two at once would revoke the
        family.
        """
        id_hash = digest(session_id)
        session = await self._sessions.live(id_hash, self._clock(), IDLE_TIMEOUT)
        if session is None:
            return UNAUTHENTICATED
        if session.access_expires_at - self._clock() > REFRESH_WINDOW:
            return await self._use(session)
        lock = self._locks.get(id_hash)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[id_hash] = lock
        async with lock:
            session = await self._sessions.live(id_hash, self._clock(), IDLE_TIMEOUT)
            if session is None:
                return UNAUTHENTICATED
            if session.access_expires_at - self._clock() > REFRESH_WINDOW:
                return await self._use(session)
            return await self._refresh(session)

    async def _use(self, session: UserSessionRecord) -> UserPrincipal | Denied:
        now = self._clock()
        try:
            token = self._open(session.access_token_enc, "access", session.id_hash)
            access = await self._sign_in.verify_access(token, now)
        except _FAILURES as exc:
            log.info("user.session_token_refused", reason=str(exc))
            return UNAUTHENTICATED
        if access.sub != session.sub:
            return UNAUTHENTICATED
        await self._sessions.touch(session.id_hash, now)
        return _principal(session)

    async def _refresh(self, session: UserSessionRecord) -> UserPrincipal | Denied:
        now = self._clock()
        try:
            refreshed, new_refresh = await self._rotate(session, now)
        except _FAILURES as exc:
            log.info("user.refresh_failed", sub=session.sub, reason=str(exc))
            await self._sessions.revoke(session.id_hash, now)
            return UNAUTHENTICATED
        if not await self._sessions.refreshed(session.id_hash, refreshed, now):
            if new_refresh:
                await self._sign_in.provider.revoke(new_refresh)
            return UNAUTHENTICATED
        return _principal(session)

    async def _rotate(
        self, session: UserSessionRecord, now: datetime
    ) -> tuple[RefreshedUserTokens, str | None]:
        if session.refresh_token_enc is None:
            raise InvalidToken("no refresh token")
        provider = self._sign_in.provider
        tokens = await provider.refresh(
            self._open(session.refresh_token_enc, "refresh", session.id_hash)
        )
        access = await self._sign_in.verify_access(tokens.access_token, now)
        if access.sub != session.sub:
            raise InvalidToken("sub")
        id_token = tokens.id_token
        if id_token is not None:
            identity = await verify_id_token(
                id_token,
                keys=provider,
                issuer=await provider.issuer(),
                client_id=self._sign_in.settings.client_id,
                now=now,
                nonce_hash=None,
            )
            if identity.sub != session.sub:
                raise InvalidToken("sub")
        id_hash = session.id_hash
        return (
            RefreshedUserTokens(
                access_token_enc=self._seal(tokens.access_token, "access", id_hash),
                refresh_token_enc=self._seal(tokens.refresh_token, "refresh", id_hash)
                if tokens.refresh_token
                else session.refresh_token_enc,
                id_token_enc=self._seal(id_token, "id", id_hash)
                if id_token
                else session.id_token_enc,
                access_expires_at=access.expires_at,
                expires_at=now + refresh_lifetime(tokens)
                if tokens.refresh_token
                else session.expires_at,
            ),
            tokens.refresh_token,
        )

    # --- ending ------------------------------------------------------------

    async def sign_out(self, session_id: str | None) -> None:
        """Revoke the session here, then its refresh token at CyberdyneAuth."""
        if not session_id:
            return
        session = await self._sessions.revoke(digest(session_id), self._clock())
        if session is not None:
            await self.revoke_refresh(session)

    async def end_subject(self, sub: str, refresh_token: str | None) -> None:
        """After an erasure: no session of this account survives, and the
        refresh token read before it is revoked at CyberdyneAuth."""
        await self._sessions.revoke_subject(sub, self._clock())
        if refresh_token:
            await self._sign_in.provider.revoke(refresh_token)

    async def refresh_token_of(self, principal: UserPrincipal) -> str | None:
        """The session's refresh token, read before an erasure deletes the row."""
        session = await self._sessions.live(principal.session_hash, self._clock(), IDLE_TIMEOUT)
        if session is None or session.refresh_token_enc is None:
            return None
        try:
            return self._open(session.refresh_token_enc, "refresh", session.id_hash)
        except UnreadableCiphertext:
            return None

    async def revoke_refresh(self, session: UserSessionRecord) -> None:
        if session.refresh_token_enc is None:
            return
        try:
            refresh = self._open(session.refresh_token_enc, "refresh", session.id_hash)
        except UnreadableCiphertext:
            return
        await self._sign_in.provider.revoke(refresh)

    # --- helpers -----------------------------------------------------------

    def _seal(self, value: str, column: str, id_hash: str) -> bytes:
        return self._cipher.seal(value, context=f"user-{column}:{id_hash}")

    def _seal_optional(self, value: str | None, column: str, id_hash: str) -> bytes | None:
        return self._seal(value, column, id_hash) if value else None

    def _open(self, sealed: bytes, column: str, id_hash: str) -> str:
        return self._cipher.open(sealed, context=f"user-{column}:{id_hash}")


def _principal(session: UserSessionRecord) -> UserPrincipal:
    return UserPrincipal(
        sub=session.sub,
        email=session.email,
        session_hash=session.id_hash,
        fresh_auth_at=session.fresh_auth_at,
    )


def _failed(reason: str) -> SignInFailed:
    log.info("user.sign_in_refused", reason=reason)
    return SignInFailed(reason)


def _refused(failure: LinkFailure, reason: str) -> LinkRefused:
    log.info("user.link_refused", failure=failure.value, reason=reason)
    return LinkRefused(failure, reason)
