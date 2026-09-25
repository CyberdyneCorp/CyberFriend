"""Random values, hashes, PKCE, and the cipher that keeps tokens unusable at rest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RANDOM_BYTES = 32
NONCE_BYTES = 12


def random_value() -> str:
    """32 random bytes, url-safe. For session ids, state, nonce and the binding."""
    return secrets.token_urlsafe(RANDOM_BYTES)


def digest(value: str) -> str:
    """sha256 hex. What is stored in place of a session id, a state or a binding."""
    return hashlib.sha256(value.encode()).hexdigest()


def same_digest(value: str, expected_digest: str) -> bool:
    return hmac.compare_digest(digest(value), expected_digest)


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge: base64url(sha256(verifier)), unpadded."""
    hashed = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(hashed).rstrip(b"=").decode("ascii")


class UnreadableCiphertext(Exception):
    """A stored token did not decrypt: wrong key, wrong row, or tampered."""


class TokenCipher:
    """AES-GCM under `ADMIN_SESSION_KEY`.

    The associated data names the row and the column, so a ciphertext copied
    into another session's row, or from the refresh column to the access
    column, fails to decrypt instead of being used there.
    """

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def seal(self, plaintext: str, *, context: str) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), context.encode())

    def open(self, sealed: bytes, *, context: str) -> str:
        nonce, body = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, body, context.encode()).decode()
        except (InvalidTag, ValueError) as exc:
            raise UnreadableCiphertext(context.split(":", 1)[0]) from exc
