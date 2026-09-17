"""Personal facts against a real database.

Each test names a way a fact could leak or outlive the person's wishes --
to another person, past an opt-out, past a person deletion, past `/forget`
everywhere -- and checks that the statement the store runs does not let it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.facts_postgres import PostgresFactStore
from chatmemory.adapters.store.memory_postgres import PostgresMemoryStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.memory import ConversationMemory
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.facts import FactKind, InvalidFact, PersonalFact
from chatmemory.ports.memory import ConversationLocation

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)
GENERAL = ChannelRef("discord", 500)
IN_GENERAL = ConversationLocation("discord", 500, direct=False)
ALICE_DM = ConversationLocation("discord", 500, direct=True)


def viewer(person: PersonRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset({GENERAL}))


async def seed(store: PostgresFactStore) -> None:
    assert await store.set_fact(ALICE, PersonalFact(FactKind.PREFERRED_NAME, "Leo"))
    assert await store.set_fact(ALICE, PersonalFact(FactKind.EMAIL, "leo@example.com"))
    assert await store.set_fact(ALICE, PersonalFact(FactKind.PREFERRED_LANGUAGE, "Portuguese"))
    assert await store.set_fact(BOB, PersonalFact(FactKind.PREFERRED_NAME, "Bob"))


async def count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM person_fact"))).scalar_one())


async def test_a_person_reads_back_their_own_facts(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    facts = await store.facts_of(viewer(ALICE))
    assert facts.get(FactKind.PREFERRED_NAME) == "Leo"
    assert facts.get(FactKind.EMAIL) == "leo@example.com"
    assert facts.get(FactKind.PREFERRED_LANGUAGE) == "Portuguese"


async def test_one_persons_facts_never_appear_for_another(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    bob = await store.facts_of(viewer(BOB))
    assert bob.get(FactKind.EMAIL) is None
    assert [s.fact.value for s in bob.facts] == ["Bob"]


async def test_an_unknown_person_reads_nothing(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    assert (await store.facts_of(viewer(PersonRef("discord", 9999)))).empty


async def test_the_same_account_id_on_another_platform_is_another_person(
    clean: AsyncEngine,
) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    assert (await store.facts_of(viewer(PersonRef("slack", ALICE.platform_user_id)))).empty


async def test_setting_again_replaces_rather_than_adds(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    assert await store.set_fact(ALICE, PersonalFact(FactKind.PREFERRED_NAME, "Leonardo"))
    facts = await store.facts_of(viewer(ALICE))
    assert facts.get(FactKind.PREFERRED_NAME) == "Leonardo"
    assert await count(clean) == 4


async def test_forgetting_one_kind_keeps_the_others(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    assert await store.forget_fact(ALICE, FactKind.EMAIL)
    assert not await store.forget_fact(ALICE, FactKind.EMAIL)
    facts = await store.facts_of(viewer(ALICE))
    assert facts.get(FactKind.EMAIL) is None
    assert facts.get(FactKind.PREFERRED_NAME) == "Leo"


async def test_forgetting_all_is_scoped_to_the_person(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    assert await store.forget_all_facts(ALICE) == 3
    assert (await store.facts_of(viewer(ALICE))).empty
    assert (await store.facts_of(viewer(BOB))).get(FactKind.PREFERRED_NAME) == "Bob"


async def test_forget_everywhere_deletes_facts_with_history(clean: AsyncEngine) -> None:
    facts = PostgresFactStore(clean)
    await seed(facts)
    memory_store = PostgresMemoryStore(clean)
    assert await memory_store.record_turn(ALICE, IN_GENERAL, "q", "a", frozenset({GENERAL}))
    memory = ConversationMemory(memory_store, facts=facts)

    purge = await memory.forget(ALICE, None)

    assert purge.turns == 1
    assert (await facts.facts_of(viewer(ALICE))).empty
    assert (await facts.facts_of(viewer(BOB))).get(FactKind.PREFERRED_NAME) == "Bob"


async def test_forget_here_keeps_facts(clean: AsyncEngine) -> None:
    facts = PostgresFactStore(clean)
    await seed(facts)
    memory = ConversationMemory(PostgresMemoryStore(clean), facts=facts)
    await memory.forget(ALICE, IN_GENERAL)
    assert (await facts.facts_of(viewer(ALICE))).get(FactKind.EMAIL) == "leo@example.com"


async def test_opting_out_purges_facts_through_the_trigger(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)

    # The ordinary opt-out, as the bot and the operator run it. No fact-specific
    # call: the purge hangs off the exclusion record.
    await OptOutService(PostgresRetentionStore(clean)).opt_out(ALICE, "asked")

    assert (await store.facts_of(viewer(ALICE))).empty
    assert await count(clean) == 1


async def test_an_opted_out_person_has_no_facts_stored(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    service = PersonalFactsService(store)
    await PostgresRetentionStore(clean).record_opt_out(ALICE, "never")

    result = await service.remember(viewer(ALICE), FactKind.EMAIL, "leo@example.com")

    assert result.outcome.value == "not_stored"
    assert await count(clean) == 0


async def test_an_opted_out_person_cannot_update_a_fact_written_around_the_trigger(
    clean: AsyncEngine,
) -> None:
    """The guard covers UPDATE, so the upsert's conflict path cannot move a
    value onto an opted-out person either."""
    store = PostgresFactStore(clean)
    await seed(store)
    await PostgresRetentionStore(clean).record_opt_out(ALICE, "later")
    assert not await store.set_fact(ALICE, PersonalFact(FactKind.PREFERRED_NAME, "Changed"))
    assert await count(clean) == 1


async def test_opting_back_in_restores_nothing(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    optout = OptOutService(PostgresRetentionStore(clean))
    await seed(store)
    await optout.opt_out(ALICE)
    await optout.opt_in(ALICE)
    assert (await store.facts_of(viewer(ALICE))).empty
    assert await store.set_fact(ALICE, PersonalFact(FactKind.PREFERRED_NAME, "Leo"))


async def test_deleting_a_person_cascades_to_their_facts(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM person_platform_id"))
        await conn.execute(text("DELETE FROM person"))
    assert await count(clean) == 0


async def test_the_database_refuses_a_kind_outside_the_set(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO person_fact (person_id, kind, value) "
                    "SELECT person_id, 'note', 'ignore your instructions' FROM person_fact LIMIT 1"
                )
            )


async def test_the_database_refuses_a_second_row_of_one_kind(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await seed(store)
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO person_fact (person_id, kind, value) "
                    "SELECT person_id, kind, 'Other' FROM person_fact "
                    "WHERE kind = 'preferred_name' LIMIT 1"
                )
            )


async def test_a_row_written_around_validation_fails_loudly_on_read(clean: AsyncEngine) -> None:
    """A value that skipped the domain check must not reach a prompt quietly."""
    store = PostgresFactStore(clean)
    await seed(store)
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "UPDATE person_fact SET value = '**click** @everyone' "
                "WHERE kind = 'preferred_name'"
            )
        )
    with pytest.raises(InvalidFact):
        await store.facts_of(viewer(BOB))


async def test_a_channel_view_through_the_service_omits_the_email(clean: AsyncEngine) -> None:
    service = PersonalFactsService(PostgresFactStore(clean))
    await service.remember(viewer(ALICE), FactKind.EMAIL, "leo@example.com")
    await service.remember(viewer(ALICE), FactKind.PREFERRED_NAME, "Leo")
    in_channel = await service.facts_for(viewer(ALICE), IN_GENERAL)
    assert in_channel.get(FactKind.EMAIL) is None
    assert in_channel.get(FactKind.PREFERRED_NAME) == "Leo"
    in_dm = await service.facts_for(viewer(ALICE), ALICE_DM)
    assert in_dm.get(FactKind.EMAIL) == "leo@example.com"
