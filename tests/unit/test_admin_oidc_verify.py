"""Token verification exactly per the CyberdyneAuth contract, one rule at a time.

Each negative case changes one claim of an otherwise valid token, so a pass
here means that rule -- and not some other one -- refused it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt
import pytest

from chatmemory.admin.auth import Role
from chatmemory.admin.oidc.crypto import digest
from chatmemory.admin.oidc.provider import OIDCProvider
from chatmemory.admin.oidc.verify import InvalidToken, verify_access_token, verify_id_token
from tests.e2e.harness.oidc import CLIENT_ID, ISSUER, FakeOIDC
from tests.unit.oidc_support import SETTINGS, Clock

NONCE = "the-nonce"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(clock: Clock) -> FakeOIDC:
    fake = FakeOIDC(clock=clock.epoch)
    fake.add("ana-sub", "ana@cyberdyne.test", [fake.role("admin")])
    fake.add("cass-sub", "cass@cyberdyne.test", None)
    fake.add("dan-sub", "dan@cyberdyne.test", ["other-client:admin", "cyberfriendx:admin"])
    return fake


@pytest.fixture
def provider(fake: FakeOIDC) -> OIDCProvider:
    return OIDCProvider(SETTINGS, transport=fake.transport)


async def _access(token: str, provider: OIDCProvider, clock: Clock) -> Any:
    return await verify_access_token(
        token, keys=provider, issuer=ISSUER, client_id=CLIENT_ID, now=clock()
    )


async def _id(token: str, provider: OIDCProvider, clock: Clock, nonce: str | None = NONCE) -> Any:
    return await verify_id_token(
        token,
        keys=provider,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        now=clock(),
        nonce_hash=digest(nonce) if nonce is not None else None,
    )


# --- the access token --------------------------------------------------


async def test_a_valid_access_token_yields_the_subject_and_roles(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    claims = await _access(fake.mint_access("ana-sub"), provider, clock)

    assert claims.sub == "ana-sub"
    assert claims.roles == frozenset({Role.ADMIN})


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"aud": "someone-else"}, "aud"),
        ({"aud": CLIENT_ID + "-admin"}, "aud"),
        ({"iss": "https://evil.test"}, "iss"),
        ({"type": "refresh"}, "type"),
        ({"type": None}, "type"),
    ],
)
async def test_an_access_token_breaking_one_rule_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock, override: dict[str, Any], rule: str
) -> None:
    claims = {k: v for k, v in fake.access_claims("ana-sub", **override).items() if v is not None}

    with pytest.raises(InvalidToken, match=rule):
        await _access(fake.sign(claims), provider, clock)


async def test_an_expired_access_token_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    token = fake.mint_access("ana-sub")
    clock.advance(timedelta(seconds=fake.access_ttl))

    with pytest.raises(InvalidToken, match="exp"):
        await _access(token, provider, clock)


async def test_a_token_without_exp_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    claims = fake.access_claims("ana-sub")
    del claims["exp"]

    with pytest.raises(InvalidToken, match="exp"):
        await _access(fake.sign(claims), provider, clock)


async def test_a_token_signed_by_an_unpublished_key_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    await _access(fake.mint_access("ana-sub"), provider, clock)  # caches the key set
    fake.rotate_key(publish=False)

    with pytest.raises(InvalidToken, match="kid"):
        await _access(fake.mint_access("ana-sub"), provider, clock)


async def test_the_key_set_is_refetched_at_most_once_a_minute(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    await _access(fake.mint_access("ana-sub"), provider, clock)
    assert fake.jwks_fetches == 1

    fake.rotate_key()
    # Within a minute of the last fetch the key set is not refetched...
    with pytest.raises(InvalidToken, match="kid"):
        await _access(fake.mint_access("ana-sub"), provider, clock)
    assert fake.jwks_fetches == 1


async def test_an_unknown_kid_refetches_the_key_set_once(
    fake: FakeOIDC, clock: Clock
) -> None:
    ticks = iter([0.0, 61.0, 61.5, 62.0])
    provider = OIDCProvider(SETTINGS, transport=fake.transport, monotonic=lambda: next(ticks))
    await _access(fake.mint_access("ana-sub"), provider, clock)

    fake.rotate_key()
    claims = await _access(fake.mint_access("ana-sub"), provider, clock)

    assert claims.sub == "ana-sub"
    assert fake.jwks_fetches == 2


async def test_a_made_up_kid_does_not_hammer_the_issuer(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    await _access(fake.mint_access("ana-sub"), provider, clock)
    for _ in range(5):
        fake.rotate_key(publish=False)
        with pytest.raises(InvalidToken, match="kid"):
            await _access(fake.mint_access("ana-sub"), provider, clock)

    assert fake.jwks_fetches == 1


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_only_rs256_is_accepted(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock, algorithm: str
) -> None:
    key = "secret" if algorithm == "HS256" else None
    token = jwt.encode(fake.access_claims("ana-sub"), key, algorithm=algorithm)

    with pytest.raises(InvalidToken, match="alg"):
        await _access(token, provider, clock)


async def test_an_absent_roles_claim_is_unknown_not_empty(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    claims = await _access(fake.mint_access("cass-sub"), provider, clock)

    assert claims.roles is None


async def test_roles_for_another_client_grant_nothing(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    claims = await _access(fake.mint_access("dan-sub"), provider, clock)

    assert claims.roles == frozenset()


async def test_a_roles_claim_that_is_not_a_list_is_unknown(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    token = fake.mint_access("ana-sub", roles="cyberfriend:admin")

    assert (await _access(token, provider, clock)).roles is None


# --- the id token ------------------------------------------------------


async def test_a_valid_id_token_carries_the_subject_and_auth_time(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    claims = await _id(fake.sign(fake.id_claims("ana-sub", NONCE)), provider, clock)

    assert claims.sub == "ana-sub"
    assert claims.auth_time is not None


async def test_an_id_token_without_the_nonce_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    with pytest.raises(InvalidToken, match="nonce"):
        await _id(fake.sign(fake.id_claims("ana-sub", None)), provider, clock)


async def test_an_id_token_with_another_nonce_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    with pytest.raises(InvalidToken, match="nonce"):
        await _id(fake.sign(fake.id_claims("ana-sub", "another")), provider, clock)


async def test_an_id_token_for_another_client_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    token = fake.sign(fake.id_claims("ana-sub", NONCE, aud="other-client"))

    with pytest.raises(InvalidToken, match="aud"):
        await _id(token, provider, clock)


async def test_an_access_token_is_not_an_id_token(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    """Its audience is `cyberfriend` the API, not the client, so it cannot pass."""
    fake.client_id = "console-client"

    with pytest.raises(InvalidToken, match="aud"):
        await verify_id_token(
            fake.mint_access("ana-sub", nonce=NONCE),
            keys=provider,
            issuer=ISSUER,
            client_id="console-client",
            now=clock(),
            nonce_hash=digest(NONCE),
        )


async def test_a_subject_with_whitespace_is_refused(
    fake: FakeOIDC, provider: OIDCProvider, clock: Clock
) -> None:
    fake.add("bad sub", "x@y", [fake.role("admin")])

    with pytest.raises(InvalidToken, match="sub"):
        await _access(fake.mint_access("bad sub"), provider, clock)


async def test_discovery_naming_another_issuer_is_refused(fake: FakeOIDC) -> None:
    from chatmemory.admin.oidc.provider import ProviderUnavailable

    fake.issuer = "https://auth.test/other"
    provider = OIDCProvider(SETTINGS, transport=fake.transport)

    with pytest.raises(ProviderUnavailable, match="different issuer"):
        await provider.discovery()
