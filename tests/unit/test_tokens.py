"""The credential itself: what is stored, and what rotation disturbs.

The property under test throughout is that a token is a *capability for one
person*. Nothing here should ever let a holder of one credential reach
another person's identity, and nothing should ever store the credential in a
replayable form.
"""

from __future__ import annotations

from chatmemory.app.tokens import (
    TOKEN_PREFIX,
    InMemoryTokenStore,
    generate_token,
    hash_token,
    hashes_match,
)
from chatmemory.domain.identity import PersonRef

ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)


def test_generated_tokens_are_prefixed_and_unique() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(t.startswith(TOKEN_PREFIX) for t in tokens)
    # 32 bytes, url-safe base64. Short enough to catch a truncated generator.
    assert all(len(t) > len(TOKEN_PREFIX) + 40 for t in tokens)


def test_hashing_is_stable_and_hides_the_token() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert digest == hash_token(token)
    assert token not in digest
    assert digest != hash_token(generate_token())


def test_hashes_match_compares_equal_digests() -> None:
    digest = hash_token("cfm_example")
    assert hashes_match(digest, hash_token("cfm_example"))
    assert not hashes_match(digest, hash_token("cfm_other"))


async def test_a_token_resolves_to_exactly_one_person() -> None:
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE, "laptop")
    assert await store.person_for_token(issued.token) == ALICE


async def test_an_unknown_token_resolves_to_nobody() -> None:
    store = InMemoryTokenStore()
    await store.issue(ALICE)
    assert await store.person_for_token(generate_token()) is None


async def test_the_plaintext_token_is_never_stored() -> None:
    """A dump of the table must yield nothing replayable."""
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE)
    records = await store.active_tokens()
    assert records
    assert all(issued.token not in record.token_hash for record in records)
    assert all(record.token_hash == hash_token(issued.token) for record in records)


async def test_revoking_stops_the_token_working() -> None:
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE)
    assert await store.revoke(ALICE) == 1
    assert await store.person_for_token(issued.token) is None


async def test_a_revoked_token_is_indistinguishable_from_an_unknown_one() -> None:
    """Both answer "not valid" and nothing more.

    A distinct answer would tell a caller holding a stale token that it was
    once real, and for whom.
    """
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE)
    await store.revoke(ALICE)
    assert await store.person_for_token(issued.token) is None
    assert await store.person_for_token(generate_token()) is None


async def test_rotation_replaces_one_person_and_disturbs_nobody_else() -> None:
    """The property that decides whether rotation actually gets done."""
    store = InMemoryTokenStore()
    alice_old = await store.issue(ALICE, "laptop")
    bob = await store.issue(BOB, "laptop")

    alice_new = await store.rotate(ALICE, "replacement")

    assert await store.person_for_token(alice_old.token) is None
    assert await store.person_for_token(alice_new.token) == ALICE
    assert await store.person_for_token(bob.token) == BOB


async def test_rotation_yields_a_different_token() -> None:
    store = InMemoryTokenStore()
    first = await store.issue(ALICE)
    second = await store.rotate(ALICE)
    assert second.token != first.token


async def test_a_person_may_hold_several_tokens() -> None:
    """One per client, so revoking a laptop does not break CI."""
    store = InMemoryTokenStore()
    laptop = await store.issue(ALICE, "laptop")
    ci = await store.issue(ALICE, "ci")
    assert await store.person_for_token(laptop.token) == ALICE
    assert await store.person_for_token(ci.token) == ALICE
    assert len(await store.active_tokens()) == 2
