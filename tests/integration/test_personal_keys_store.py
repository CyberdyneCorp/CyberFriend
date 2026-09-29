"""Connected-app keys on real Postgres: sealed at rest, bound to their person, purged with them.

0037 adds `person_secret` and its DELETE to `purge_person_derived`, so opting
out and "Delete everything" remove the key in the same transaction as the
rest, and a key saved by an opted-out person is dropped by a trigger.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.personal_keys_postgres import (
    PostgresPersonalKeyStore,
    secrets_cipher,
)
from chatmemory.adapters.store.privacy_postgres import PostgresPrivacyStore
from chatmemory.app.personal_keys import CYBERWEALTH, PersonalKeys
from chatmemory.domain.identity import PersonRef
from tests.integration.test_alert_kinds_store import alembic

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 8201)
BOB = PersonRef("discord", 8202)
SECRET = "a1B2-c3D4_" * 4 + "Zq9"
KEY = f"cwk_live_ABCDEFGHJK_{SECRET}"
OTHER_KEY = f"cwk_live_ZZZZZZZZZZ_{SECRET[::-1]}"
SECRETS_KEY = "A" * 43 + "="
ROTATED = "B" * 43 + "="


def store(engine: AsyncEngine, secrets_key: str = SECRETS_KEY) -> PostgresPersonalKeyStore:
    return PostgresPersonalKeyStore(engine, secrets_cipher(secrets_key))


async def person_id(engine: AsyncEngine, person: PersonRef) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT person_id FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"p": person.platform, "u": person.platform_user_id},
        )
    assert found is not None
    return int(found)


async def rows(engine: AsyncEngine) -> list[tuple[int, str, bytes, str]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT person_id, kind, ciphertext, last4 FROM person_secret ORDER BY person_id")
        )
        return [(int(r[0]), str(r[1]), bytes(r[2]), str(r[3])) for r in found]


async def test_the_key_is_stored_sealed_and_read_back_by_its_owner(clean: AsyncEngine) -> None:
    keys = store(clean)
    held = await keys.save(ALICE, CYBERWEALTH, KEY)

    assert held is not None and held.last4 == KEY[-4:]
    [(_, kind, ciphertext, last4)] = await rows(clean)
    assert (kind, last4) == (CYBERWEALTH, KEY[-4:])
    assert KEY.encode() not in ciphertext and SECRET.encode() not in ciphertext
    assert await keys.key_for(ALICE, CYBERWEALTH) == KEY
    assert await keys.key_for(BOB, CYBERWEALTH) is None


async def test_connecting_again_replaces_the_key(clean: AsyncEngine) -> None:
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    await keys.save(ALICE, CYBERWEALTH, OTHER_KEY)
    assert len(await rows(clean)) == 1
    assert await keys.key_for(ALICE, CYBERWEALTH) == OTHER_KEY


async def test_a_ciphertext_moved_to_another_person_does_not_open(clean: AsyncEngine) -> None:
    """The person id is associated data: a copied row is not Bob's bearer."""
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    await keys.save(BOB, CYBERWEALTH, OTHER_KEY)
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "UPDATE person_secret SET ciphertext = "
                "(SELECT ciphertext FROM person_secret WHERE person_id = :a) "
                "WHERE person_id = :b"
            ),
            {"a": await person_id(clean, ALICE), "b": await person_id(clean, BOB)},
        )
    assert await keys.key_for(BOB, CYBERWEALTH) is None


async def test_a_rotated_secrets_key_reads_no_key(clean: AsyncEngine) -> None:
    await store(clean).save(ALICE, CYBERWEALTH, KEY)
    assert await store(clean, ROTATED).key_for(ALICE, CYBERWEALTH) is None


async def test_forget_deletes_only_the_askers_key(clean: AsyncEngine) -> None:
    keys = PersonalKeys(store(clean))
    await keys.connect(ALICE, KEY, direct=True)
    await keys.connect(BOB, OTHER_KEY, direct=True)

    assert await keys.forget(ALICE) == 1

    assert await keys.bearer(ALICE, CYBERWEALTH) is None
    assert await keys.bearer(BOB, CYBERWEALTH) == OTHER_KEY


async def test_held_reads_the_last_four_without_opening_the_key(clean: AsyncEngine) -> None:
    """Under a secrets key that cannot open it, the key is still shown held:
    "what do you know about me?" never decrypts to list it."""
    await store(clean).save(ALICE, CYBERWEALTH, KEY)
    unopenable = store(clean, ROTATED)

    held = await unopenable.held(ALICE, CYBERWEALTH)

    assert held is not None and (held.service, held.last4) == (CYBERWEALTH, KEY[-4:])
    assert await unopenable.key_for(ALICE, CYBERWEALTH) is None
    assert await unopenable.held(BOB, CYBERWEALTH) is None


async def test_held_follows_a_replaced_key(clean: AsyncEngine) -> None:
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    await keys.save(ALICE, CYBERWEALTH, OTHER_KEY)
    held = await keys.held(ALICE, CYBERWEALTH)
    assert held is not None and held.last4 == OTHER_KEY[-4:]


async def test_forgetting_one_service_deletes_only_the_askers_key(clean: AsyncEngine) -> None:
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    await keys.save(BOB, CYBERWEALTH, OTHER_KEY)

    assert await keys.forget(ALICE, CYBERWEALTH) == 1
    assert await keys.forget(ALICE, CYBERWEALTH) == 0

    assert await keys.held(ALICE, CYBERWEALTH) is None
    assert await keys.key_for(BOB, CYBERWEALTH) == OTHER_KEY


async def test_opting_out_deletes_the_key_and_refuses_a_new_one(clean: AsyncEngine) -> None:
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    async with clean.begin() as conn:
        await conn.execute(
            text("INSERT INTO person_opt_out (person_id) VALUES (:p)"),
            {"p": await person_id(clean, ALICE)},
        )

    assert await rows(clean) == []
    assert await keys.save(ALICE, CYBERWEALTH, KEY) is None
    assert await rows(clean) == []


async def test_delete_everything_purges_the_key_and_only_theirs(clean: AsyncEngine) -> None:
    """Erasure calls `purge_person_derived` directly, without an opt-out row."""
    keys = store(clean)
    await keys.save(ALICE, CYBERWEALTH, KEY)
    await keys.save(BOB, CYBERWEALTH, OTHER_KEY)
    async with clean.begin() as conn:
        await conn.execute(
            text("SELECT purge_person_derived(:p)"), {"p": await person_id(clean, ALICE)}
        )
    assert [r[0] for r in await rows(clean)] == [await person_id(clean, BOB)]


async def test_privacy_lists_the_key_by_its_last_four_characters(clean: AsyncEngine) -> None:
    await store(clean).save(ALICE, CYBERWEALTH, KEY)

    inventory = await PostgresPrivacyStore(clean).inventory(ALICE, (), date(2026, 9, 1))

    [held] = inventory.keys
    assert (held.service, held.last4) == (CYBERWEALTH, KEY[-4:])
    assert inventory.summary().keys == 1


async def test_downgrading_restores_the_previous_purge(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0033")
        async with clean.connect() as conn:
            body = await conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'purge_person_derived'")
            )
            table = await conn.scalar(text("SELECT to_regclass('person_secret')"))
        assert "person_secret" not in str(body)
        assert "erasure_request" in str(body)
        assert table is None
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
