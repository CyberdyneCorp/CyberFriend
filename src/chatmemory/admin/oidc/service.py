"""The sign-in: begin, complete, authenticate a session, refresh it, end it.

The callback, in order (design.md of `add-console-cyberdyneauth-login`):

1.  the login record for the state's hash must exist, be unexpired and unused,
    and is marked used in the same step;
2.  the `__Host-cf_login` value must hash to the record's binding, so a
    callback URL replayed into another browser fails;
3.  the code is exchanged with the client secret and the PKCE verifier;
4.  the access token and the id token (nonce included) are verified;
5.  userinfo is fetched and `userinfo.sub == id_token.sub == access_token.sub`
    is required before its email is read;
6.  the role decides: no roles claim, or neither console role, and no session
    is created.

Every request on a session re-verifies the stored access token and takes the
roles from it. Within 60 seconds of expiry it is refreshed first, under a
per-session lock: refresh tokens rotate with reuse detection, so two
concurrent refreshes would present one refresh token twice and CyberdyneAuth
would revoke the family. The lock holds while the console runs one replica.
A refreshed token without a roles claim ends the session as unauthenticated;
one with neither console role ends it as forbidden.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from chatmemory.admin.auth import (
    NO_CONSOLE_ACCESS,
    UNAUTHENTICATED,
    Denied,
    Principal,
    Role,
)
from chatmemory.admin.oidc.config import SignInSettings
from chatmemory.admin.oidc.crypto import (
    TokenCipher,
    UnreadableCiphertext,
    digest,
    pkce_challenge,
    random_value,
    same_digest,
)
from chatmemory.admin.oidc.provider import (
    OIDCProvider,
    ProviderUnavailable,
    TokenRequestFailed,
    TokenResponse,
)
from chatmemory.admin.oidc.store import (
    ADMIN_PURPOSE,
    LoginRecord,
    LoginStore,
    RefreshedTokens,
    SessionRecord,
    SessionStore,
)
from chatmemory.admin.oidc.verify import (
    AccessClaims,
    InvalidToken,
    verify_access_token,
    verify_id_token,
)

log = structlog.get_logger()

LOGIN_LIFETIME = timedelta(minutes=10)
REFRESH_WINDOW = timedelta(seconds=60)
IDLE_TIMEOUT = timedelta(hours=12)
REFRESH_LIFETIME = timedelta(days=30)
"""CyberdyneAuth's refresh lifetime, used when the token response does not say."""

_FAILURES = (InvalidToken, ProviderUnavailable, TokenRequestFailed, UnreadableCiphertext)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Begun:
    """Where to send the browser, and the value for `__Host-cf_login`."""

    authorization_url: str
    binding: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    session_id: str
    principal: Principal


@dataclass(frozen=True, slots=True)
class NoAccess:
    """Signed in at CyberdyneAuth, but no console role. No session was created."""


@dataclass(frozen=True, slots=True)
class SignInFailed:
    #: For the log only; the browser is shown one fixed page.
    reason: str


SignInResult = SignedIn | NoAccess | SignInFailed


def console_roles(granted: frozenset[Role] | None) -> frozenset[Role] | None:
    """The console roles, admin implying operator; None when the claim is absent."""
    if granted is None:
        return None
    return granted | {Role.OPERATOR} if Role.ADMIN in granted else granted


class SignIn:
    """The BFF's sign-in, over a provider and two stores."""

    def __init__(
        self,
        settings: SignInSettings,
        provider: OIDCProvider,
        logins: LoginStore,
        sessions: SessionStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._logins = logins
        self._sessions = sessions
        self._clock = clock
        self._cipher = TokenCipher(settings.session_key)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @property
    def settings(self) -> SignInSettings:
        return self._settings

    @property
    def provider(self) -> OIDCProvider:
        return self._provider

    # --- sign-in -----------------------------------------------------

    async def begin(self, *, max_age: int | None = None) -> Begun:
        state, nonce, verifier, binding = (random_value() for _ in range(4))
        now = self._clock()
        state_hash = digest(state)
        await self._logins.begin(
            LoginRecord(
                state_hash=state_hash,
                nonce_hash=digest(nonce),
                verifier_enc=self._cipher.seal(verifier, context=f"verifier:{state_hash}"),
                binding_hash=digest(binding),
                expires_at=now + LOGIN_LIFETIME,
                purpose=ADMIN_PURPOSE,
                max_age=max_age,
            ),
            now,
        )
        url = await self._provider.authorization_url(
            state=state, nonce=nonce, challenge=pkce_challenge(verifier), max_age=max_age
        )
        return Begun(authorization_url=url, binding=binding)

    async def complete(
        self, *, state: str | None, code: str | None, binding: str | None
    ) -> SignInResult:
        now = self._clock()
        login = await self._logins.consume(digest(state), now) if state else None
        if login is None or login.purpose != ADMIN_PURPOSE:
            return _failed("state")
        if not binding or not same_digest(binding, login.binding_hash):
            return _failed("binding")
        if not code:
            return _failed("code")
        try:
            return await self._finish(login, code, now)
        except _FAILURES as exc:
            return _failed(f"{type(exc).__name__}: {exc}")

    async def _finish(self, login: LoginRecord, code: str, now: datetime) -> SignInResult:
        verifier = self._cipher.open(login.verifier_enc, context=f"verifier:{login.state_hash}")
        tokens = await self._provider.exchange_code(code, verifier)
        access = await self._verify_access(tokens.access_token, now)
        if tokens.id_token is None:
            return _failed("no id token")
        identity = await verify_id_token(
            tokens.id_token,
            keys=self._provider,
            issuer=await self._provider.issuer(),
            client_id=self._settings.client_id,
            now=now,
            nonce_hash=login.nonce_hash,
        )
        info = await self._provider.userinfo(tokens.access_token)
        if not info.get("sub") == identity.sub == access.sub:
            return _failed("subjects disagree")
        roles = console_roles(access.roles)
        if not roles:
            log.info("admin.oidc.no_console_access", sub=access.sub, claim=roles is not None)
            return NoAccess()
        return await self._create_session(tokens, tokens.id_token, access, roles, info, now)

    async def _create_session(
        self,
        tokens: TokenResponse,
        id_token: str,
        access: AccessClaims,
        roles: frozenset[Role],
        info: Mapping[str, Any],
        now: datetime,
    ) -> SignedIn:
        session_id = random_value()
        id_hash = digest(session_id)
        email = _email(info)
        await self._sessions.create(
            SessionRecord(
                id_hash=id_hash,
                sub=access.sub,
                email=email,
                roles=_role_names(roles),
                access_token_enc=self._seal(tokens.access_token, "access", id_hash),
                refresh_token_enc=self._seal_optional(tokens.refresh_token, "refresh", id_hash),
                id_token_enc=self._seal(id_token, "id", id_hash),
                access_expires_at=access.expires_at,
                created_at=now,
                last_seen_at=now,
                expires_at=now + _refresh_lifetime(tokens),
            )
        )
        log.info("admin.oidc.signed_in", sub=access.sub, roles=_role_names(roles))
        return SignedIn(session_id, _principal(access.sub, email, roles))

    # --- a request on a session --------------------------------------

    async def authenticate(self, session_id: str) -> Principal | Denied:
        id_hash = digest(session_id)
        now = self._clock()
        session = await self._sessions.live(id_hash, now, IDLE_TIMEOUT)
        if session is None:
            return UNAUTHENTICATED
        if session.access_expires_at - now > REFRESH_WINDOW:
            return await self._use(session, now)
        lock = self._locks.get(id_hash)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[id_hash] = lock
        async with lock:
            # Re-read under the lock: the request that held it may have
            # refreshed already, and its refresh token is now spent.
            now = self._clock()
            session = await self._sessions.live(id_hash, now, IDLE_TIMEOUT)
            if session is None:
                return UNAUTHENTICATED
            if session.access_expires_at - now > REFRESH_WINDOW:
                return await self._use(session, now)
            return await self._refresh(session, now)

    async def _use(self, session: SessionRecord, now: datetime) -> Principal | Denied:
        try:
            token = self._open(session.access_token_enc, "access", session.id_hash)
            access = await self._verify_access(token, now)
        except _FAILURES as exc:
            log.info("admin.oidc.session_token_refused", reason=str(exc))
            return UNAUTHENTICATED
        roles = console_roles(access.roles)
        if access.sub != session.sub or not roles:
            return UNAUTHENTICATED
        await self._sessions.touch(session.id_hash, now)
        return _principal(session.sub, session.email, roles)

    async def _refresh(self, session: SessionRecord, now: datetime) -> Principal | Denied:
        try:
            refreshed, access = await self._rotate(session, now)
        except _FAILURES as exc:
            log.info("admin.oidc.refresh_failed", sub=session.sub, reason=str(exc))
            await self._sessions.revoke(session.id_hash, now)
            return UNAUTHENTICATED
        roles = console_roles(access.roles)
        if roles is None or access.sub != session.sub:
            await self._sessions.revoke(session.id_hash, now)
            log.info("admin.oidc.refresh_denied", sub=session.sub, reason="no roles claim")
            return UNAUTHENTICATED
        if not roles:
            await self._sessions.revoke(session.id_hash, now)
            log.info("admin.oidc.refresh_denied", sub=session.sub, reason="no console role")
            return NO_CONSOLE_ACCESS
        await self._sessions.refreshed(
            session.id_hash, replace(refreshed, roles=_role_names(roles)), now
        )
        return _principal(session.sub, session.email, roles)

    async def _rotate(
        self, session: SessionRecord, now: datetime
    ) -> tuple[RefreshedTokens, AccessClaims]:
        if session.refresh_token_enc is None:
            raise InvalidToken("no refresh token")
        refresh_token = self._open(session.refresh_token_enc, "refresh", session.id_hash)
        tokens = await self._provider.refresh(refresh_token)
        access = await self._verify_access(tokens.access_token, now)
        id_token = await self._refreshed_id_token(tokens, session, now)
        id_hash = session.id_hash
        return (
            RefreshedTokens(
                roles=session.roles,
                access_token_enc=self._seal(tokens.access_token, "access", id_hash),
                # Rotating: the new one replaces the spent one. A provider
                # that does not rotate keeps the one we hold.
                refresh_token_enc=self._seal(tokens.refresh_token, "refresh", id_hash)
                if tokens.refresh_token
                else session.refresh_token_enc,
                id_token_enc=self._seal(id_token, "id", id_hash)
                if id_token
                else session.id_token_enc,
                access_expires_at=access.expires_at,
                expires_at=now + _refresh_lifetime(tokens)
                if tokens.refresh_token
                else session.expires_at,
            ),
            access,
        )

    async def _refreshed_id_token(
        self, tokens: TokenResponse, session: SessionRecord, now: datetime
    ) -> str | None:
        """A new id token, if one came, verified and for the same subject."""
        if tokens.id_token is None:
            return None
        identity = await verify_id_token(
            tokens.id_token,
            keys=self._provider,
            issuer=await self._provider.issuer(),
            client_id=self._settings.client_id,
            now=now,
            nonce_hash=None,
        )
        if identity.sub != session.sub:
            raise InvalidToken("sub")
        return tokens.id_token

    # --- sign-out ----------------------------------------------------

    async def sign_out(self, session_id: str | None) -> str | None:
        """Revoke the session, then the refresh token; the end-session URL."""
        if not session_id:
            return None
        session = await self._sessions.revoke(digest(session_id), self._clock())
        if session is None:
            return None
        log.info("admin.oidc.signed_out", sub=session.sub)
        try:
            id_token: str | None = self._open(session.id_token_enc, "id", session.id_hash)
            if session.refresh_token_enc is not None:
                refresh = self._open(session.refresh_token_enc, "refresh", session.id_hash)
                await self._provider.revoke(refresh)
        except UnreadableCiphertext:
            id_token = None
        return await self._provider.end_session_url(id_token)

    # --- helpers -----------------------------------------------------

    async def _verify_access(self, token: str, now: datetime) -> AccessClaims:
        return await verify_access_token(
            token,
            keys=self._provider,
            issuer=await self._provider.issuer(),
            client_id=self._settings.client_id,
            now=now,
        )

    def _seal(self, value: str, column: str, id_hash: str) -> bytes:
        return self._cipher.seal(value, context=f"{column}:{id_hash}")

    def _seal_optional(self, value: str | None, column: str, id_hash: str) -> bytes | None:
        return self._seal(value, column, id_hash) if value else None

    def _open(self, sealed: bytes, column: str, id_hash: str) -> str:
        return self._cipher.open(sealed, context=f"{column}:{id_hash}")


def _failed(reason: str) -> SignInFailed:
    log.info("admin.oidc.sign_in_refused", reason=reason)
    return SignInFailed(reason)


def _principal(sub: str, email: str | None, roles: frozenset[Role]) -> Principal:
    return Principal(subject=sub, display=email or sub, roles=roles, via="oidc")


def _email(info: Mapping[str, Any]) -> str | None:
    """Read only after the subjects matched. At most 320 characters, one line."""
    email = info.get("email")
    if not isinstance(email, str) or not email or len(email) > 320:
        return None
    return email if email.isprintable() else None


def _role_names(roles: frozenset[Role]) -> tuple[str, ...]:
    return tuple(sorted(role.value for role in roles))


def _refresh_lifetime(tokens: TokenResponse) -> timedelta:
    if tokens.refresh_expires_in and tokens.refresh_expires_in > 0:
        return timedelta(seconds=tokens.refresh_expires_in)
    return REFRESH_LIFETIME
