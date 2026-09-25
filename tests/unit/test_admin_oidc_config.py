"""Sign-in settings, and the cipher that keeps tokens unusable at rest."""

from __future__ import annotations

import base64

import pytest

from chatmemory.admin.oidc.config import MisconfiguredSignIn, sign_in_settings
from chatmemory.admin.oidc.crypto import TokenCipher, UnreadableCiphertext, pkce_challenge
from tests.unit.oidc_support import ENVIRON, SESSION_KEY


def test_nothing_set_is_sign_in_off() -> None:
    assert sign_in_settings({}) is None
    assert sign_in_settings({name: " " for name in ENVIRON}) is None


def test_everything_set_is_sign_in_on() -> None:
    settings = sign_in_settings({**ENVIRON, "ADMIN_PUBLIC_URL": "https://admin.test/"})

    assert settings is not None
    assert settings.redirect_uri == "https://admin.test/auth/callback"
    assert settings.public_origin == "https://admin.test"
    assert settings.session_key == SESSION_KEY
    assert settings.scopes == "openid email profile"
    assert "fake-client-secret" not in repr(settings)


def test_the_issuer_alone_is_refused_with_what_is_missing() -> None:
    with pytest.raises(MisconfiguredSignIn) as refusal:
        sign_in_settings({"ADMIN_OIDC_ISSUER": "https://auth.test"})

    for name in ("ADMIN_OIDC_CLIENT_ID", "ADMIN_OIDC_CLIENT_SECRET", "ADMIN_SESSION_KEY"):
        assert name in str(refusal.value)


@pytest.mark.parametrize(
    "key",
    ["not base64 !!", base64.b64encode(b"short").decode(), base64.b64encode(bytes(33)).decode()],
)
def test_a_session_key_that_is_not_32_bytes_is_refused(key: str) -> None:
    with pytest.raises(MisconfiguredSignIn, match="ADMIN_SESSION_KEY"):
        sign_in_settings({**ENVIRON, "ADMIN_SESSION_KEY": key})


def test_an_unpadded_urlsafe_key_is_accepted() -> None:
    key = base64.urlsafe_b64encode(bytes(range(200, 232))).decode().rstrip("=")

    settings = sign_in_settings({**ENVIRON, "ADMIN_SESSION_KEY": key})

    assert settings is not None and settings.session_key == bytes(range(200, 232))


@pytest.mark.parametrize("url", ["http://admin.test", "admin.test", "https://admin.test/?x=1"])
def test_the_public_url_must_be_https(url: str) -> None:
    with pytest.raises(MisconfiguredSignIn, match="ADMIN_PUBLIC_URL"):
        sign_in_settings({**ENVIRON, "ADMIN_PUBLIC_URL": url})


def test_a_ciphertext_opens_only_in_its_own_row_and_column() -> None:
    cipher = TokenCipher(SESSION_KEY)
    sealed = cipher.seal("eyJ.token", context="access:row-1")

    assert b"eyJ" not in sealed
    assert cipher.open(sealed, context="access:row-1") == "eyJ.token"
    for context in ("access:row-2", "refresh:row-1"):
        with pytest.raises(UnreadableCiphertext):
            cipher.open(sealed, context=context)
    with pytest.raises(UnreadableCiphertext):
        TokenCipher(bytes(32)).open(sealed, context="access:row-1")


def test_the_pkce_challenge_is_rfc_7636_s256() -> None:
    # The example from RFC 7636, appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
