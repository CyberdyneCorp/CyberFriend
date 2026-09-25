"""Sign-in settings, read from the environment. Off unless every one is set.

The issuer alone is not enough: setting it downscopes every `cfa_` token to
operator, and without the client, the session key and the public URL nobody
could sign in to get admin back. A partial configuration therefore refuses to
start and names what is missing, rather than starting a console nobody can
change anything in.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ISSUER_VAR = "ADMIN_OIDC_ISSUER"
CLIENT_ID_VAR = "ADMIN_OIDC_CLIENT_ID"
CLIENT_SECRET_VAR = "ADMIN_OIDC_CLIENT_SECRET"
SESSION_KEY_VAR = "ADMIN_SESSION_KEY"
PUBLIC_URL_VAR = "ADMIN_PUBLIC_URL"
SCOPES_VAR = "ADMIN_OIDC_SCOPES"

REQUIRED_VARS = (ISSUER_VAR, CLIENT_ID_VAR, CLIENT_SECRET_VAR, SESSION_KEY_VAR, PUBLIC_URL_VAR)

SECRET_VARS = frozenset({CLIENT_SECRET_VAR, SESSION_KEY_VAR})
"""Values that are never logged, recorded or shown."""

ACCESS_AUDIENCE = "cyberfriend"
"""The `aud` every access token must carry, from the CyberdyneAuth contract."""

DEFAULT_SCOPES = "openid email profile"

SESSION_KEY_BYTES = 32


class MisconfiguredSignIn(RuntimeError):
    """Sign-in is partly configured, or a value cannot be used."""


@dataclass(frozen=True, slots=True)
class SignInSettings:
    """Everything the BFF needs to talk to CyberdyneAuth and keep sessions."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    session_key: bytes = field(repr=False)
    public_url: str
    scopes: str = DEFAULT_SCOPES

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def post_logout_redirect_uri(self) -> str:
        return f"{self.public_url}/"

    @property
    def console_url(self) -> str:
        return f"{self.public_url}/#/status"

    def role_claim(self, role: str) -> str:
        """`<client_id>:<role>`, the form the roles claim uses."""
        return f"{self.client_id}:{role}"


def sign_in_settings(environ: Mapping[str, str]) -> SignInSettings | None:
    """The settings, None when sign-in is off, or `MisconfiguredSignIn`."""
    values = {name: environ.get(name, "").strip() for name in REQUIRED_VARS}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MisconfiguredSignIn(
            "CyberdyneAuth sign-in is partly configured; also set " + ", ".join(missing)
        )
    return SignInSettings(
        issuer=values[ISSUER_VAR],
        client_id=values[CLIENT_ID_VAR],
        client_secret=values[CLIENT_SECRET_VAR],
        session_key=_session_key(values[SESSION_KEY_VAR]),
        public_url=_public_url(values[PUBLIC_URL_VAR]),
        scopes=environ.get(SCOPES_VAR, "").strip() or DEFAULT_SCOPES,
    )


def _session_key(raw: str) -> bytes:
    """32 bytes, base64 (standard or url-safe, padding optional)."""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        key = base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as exc:
        raise MisconfiguredSignIn(f"{SESSION_KEY_VAR} must be base64") from exc
    if len(key) != SESSION_KEY_BYTES:
        raise MisconfiguredSignIn(
            f"{SESSION_KEY_VAR} must decode to {SESSION_KEY_BYTES} bytes "
            "(generate one with: openssl rand -base64 32)"
        )
    return key


def _public_url(raw: str) -> str:
    """An https URL without a trailing slash. `__Host-` cookies need https."""
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise MisconfiguredSignIn(
            f"{PUBLIC_URL_VAR} must be the console's https URL, e.g. https://admin.example.com"
        )
    return raw.rstrip("/")
