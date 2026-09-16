"""Conversation memory against a real database.

These are the tests the change exists for. Each one names a way remembered
conversation could leak -- past a revocation, across people, from a DM into a
channel, past an opt-out -- and checks that the statement the bot actually runs
does not let it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.memory_postgres import (
    PostgresMemoryStore,
    UnverifiableProvenance,
)
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.memory import ConversationMemory
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Citation
from chatmemory.ports.memory import ConversationLocation, Recollection

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)

GENERAL = ChannelRef("discord", 500)
ENG = ChannelRef("discord", 501)
HR = ChannelRef("discord", 502)

IN_GENERAL = ConversationLocation("discord", GENERAL.platform_channel_id, direct=False)
# Deliberately the same numeric id as the channel: the kind alone must keep
# the two conversations apart.
ALICE_DM = ConversationLocation("discord", GENERAL.platform_channel_id, direct=True)


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


async def record(
    store: PostgresMemoryStore,
    person: PersonRef,
    location: ConversationLocation,
    question: str,
    *channels: ChannelRef,
) -> None:
    stored = await store.record_turn(
        person, location, question, f"answer to {question}", frozenset(channels)
    )
    assert stored


async def questions(
    store: PostgresMemoryStore, who: Viewer, location: ConversationLocation, limit: int = 10
) -> list[str]:
    recollection = await store.recall(who, location, limit)
    return [t.question for t in recollection.turns]


# --- 2.4 a revoked channel withholds the turn ---------------------------


async def test_a_turn_drawing_on_a_revoked_channel_is_not_returned(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "what did eng decide", GENERAL, ENG)
    await record(store, ALICE, IN_GENERAL, "and in general", GENERAL)

    then = viewer(ALICE, GENERAL, ENG)
    assert await questions(store, then, IN_GENERAL) == ["what did eng decide", "and in general"]

    # Access to #eng revoked. The turn that quoted it must not come back, even
    # though it also drew on a channel that is still readable.
    now = viewer(ALICE, GENERAL)
    assert await questions(store, now, IN_GENERAL) == ["and in general"]


async def test_the_check_is_against_current_access_not_a_deletion(clean: AsyncEngine) -> None:
    """Withheld, not destroyed: a re-grant brings the turn back."""
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "hr question", HR)

    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL) == []
    assert await questions(store, viewer(ALICE, GENERAL, HR), IN_GENERAL) == ["hr question"]


async def test_a_viewer_with_no_channels_gets_only_channel_free_turns(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "grounded", GENERAL)
    await record(store, ALICE, IN_GENERAL, "chit-chat")

    assert await questions(store, viewer(ALICE), IN_GENERAL) == ["chit-chat"]


async def test_the_limit_counts_only_permitted_turns(clean: AsyncEngine) -> None:
    """The filter is ahead of the LIMIT. After it, revoked turns would crowd out
    readable ones and the person would silently get less context than exists."""
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "old readable", GENERAL)
    for i in range(5):
        await record(store, ALICE, IN_GENERAL, f"revoked {i}", HR)

    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL, limit=1) == [
        "old readable"
    ]


async def test_turns_come_back_oldest_first_and_bounded_to_the_newest(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    for q in ("one", "two", "three", "four"):
        await record(store, ALICE, IN_GENERAL, q, GENERAL)

    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL, limit=2) == [
        "three",
        "four",
    ]


async def test_answers_are_remembered_with_their_provenance(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "q", ENG, GENERAL)

    recollection = await store.recall(viewer(ALICE, GENERAL, ENG), IN_GENERAL, 5)
    assert recollection.turns[0].answer == "answer to q"
    async with clean.connect() as conn:
        stored = (await conn.execute(text("SELECT channel_ids FROM conversation_turn"))).scalar()
    assert stored == [GENERAL.platform_channel_id, ENG.platform_channel_id]


# --- 2.5 a summary fails whole -------------------------------------------


async def test_a_summary_touching_one_revoked_channel_is_not_returned_at_all(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "general 1", GENERAL)
    await record(store, ALICE, IN_GENERAL, "general 2", GENERAL)
    await record(store, ALICE, IN_GENERAL, "the one hr question", HR)
    await record(store, ALICE, IN_GENERAL, "recent", GENERAL)

    full = viewer(ALICE, GENERAL, HR)
    turns = (await store.recall(full, IN_GENERAL, 10)).turns
    through = turns[2].turn_id
    assert await store.record_summary(ALICE, IN_GENERAL, "digest of three turns", through)

    before = await store.recall(full, IN_GENERAL, 10)
    assert [s.text for s in before.summaries] == ["digest of three turns"]
    assert [t.question for t in before.turns] == ["recent"]

    # HR revoked. Two of the three summarised turns were about #general and
    # are still readable; the summary is withheld anyway, whole.
    after = await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)
    assert after.summaries == ()
    assert [t.question for t in after.turns] == ["recent"]


async def test_the_summary_union_is_computed_by_the_store_not_the_caller(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "eng", ENG)
    await record(store, ALICE, IN_GENERAL, "hr", HR)
    turns = (await store.recall(viewer(ALICE, ENG, HR), IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "first", turns[0].turn_id)
    await record(store, ALICE, IN_GENERAL, "general", GENERAL)
    latest = (await store.recall(viewer(ALICE, ENG, HR, GENERAL), IN_GENERAL, 10)).turns
    # The second summary replaces the first as well as the turns after it, so
    # it inherits the first summary's channels too.
    assert await store.record_summary(ALICE, IN_GENERAL, "second", latest[-1].turn_id)

    async with clean.connect() as conn:
        rows = (
            await conn.execute(text("SELECT text, covered_channel_ids FROM conversation_summary"))
        ).all()
        remaining = (await conn.execute(text("SELECT count(*) FROM conversation_turn"))).scalar()
    assert rows == [
        (
            "second",
            sorted(c.platform_channel_id for c in (ENG, HR, GENERAL)),
        )
    ]
    assert remaining == 0


async def test_a_stale_summariser_cannot_replace_a_newer_summary(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    for q in ("a", "b", "c"):
        await record(store, ALICE, IN_GENERAL, q, GENERAL)
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns

    assert await store.record_summary(ALICE, IN_GENERAL, "newer", turns[2].turn_id)
    assert not await store.record_summary(ALICE, IN_GENERAL, "older", turns[1].turn_id)

    recollection = await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)
    assert [s.text for s in recollection.summaries] == ["newer"]


async def test_nothing_to_replace_writes_no_provenance_free_summary(clean: AsyncEngine) -> None:
    """A summary with no rows behind it would carry an empty channel set and
    pass every check -- whatever text it holds."""
    store = PostgresMemoryStore(clean)
    assert not await store.record_summary(ALICE, IN_GENERAL, "invented", 10_000)
    assert (await store.recall(viewer(ALICE), IN_GENERAL, 10)).empty


# --- 2.6 one person's conversation is theirs ------------------------------


async def test_one_persons_turns_never_appear_for_another_in_the_same_channel(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "alice asks", GENERAL)
    await record(store, BOB, IN_GENERAL, "bob asks", GENERAL)
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "alice digest", turns[0].turn_id)

    # Bob can read everything Alice's turns drew on. Access is not the key;
    # the person is.
    bob = await store.recall(viewer(BOB, GENERAL, ENG, HR), IN_GENERAL, 10)
    assert [t.question for t in bob.turns] == ["bob asks"]
    assert bob.summaries == ()

    alice = await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)
    assert [t.question for t in alice.turns] == []
    assert [s.text for s in alice.summaries] == ["alice digest"]


async def test_an_unknown_person_recalls_nothing(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "alice asks")
    stranger = PersonRef("discord", 9999)
    assert (await store.recall(viewer(stranger, GENERAL), IN_GENERAL, 10)).empty


async def test_the_same_account_id_on_another_platform_is_another_person(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "alice asks")
    lookalike = PersonRef("slack", ALICE.platform_user_id)
    assert (await store.recall(viewer(lookalike, GENERAL), IN_GENERAL, 10)).empty


# --- 2.7 a DM never informs a channel answer ------------------------------


async def test_dm_history_never_appears_for_a_channel_question(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, ALICE_DM, "private question")
    turns = (await store.recall(viewer(ALICE), ALICE_DM, 10)).turns
    assert await store.record_summary(ALICE, ALICE_DM, "dm digest", turns[0].turn_id)
    await record(store, ALICE, ALICE_DM, "second private question")

    in_channel = await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)
    assert in_channel.empty

    in_dm = await store.recall(viewer(ALICE, GENERAL), ALICE_DM, 10)
    assert [t.question for t in in_dm.turns] == ["second private question"]
    assert [s.text for s in in_dm.summaries] == ["dm digest"]


async def test_channel_history_does_not_appear_in_another_channel(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    in_eng = ConversationLocation("discord", ENG.platform_channel_id, direct=False)
    await record(store, ALICE, in_eng, "asked in eng", GENERAL)
    assert (await store.recall(viewer(ALICE, GENERAL, ENG), IN_GENERAL, 10)).empty


# --- persistence ----------------------------------------------------------


async def test_history_survives_a_new_store_instance(clean: AsyncEngine) -> None:
    """What a restart looks like from here: nothing is held in the process."""
    await record(PostgresMemoryStore(clean), ALICE, IN_GENERAL, "before restart", GENERAL)
    fresh = PostgresMemoryStore(clean)
    assert await questions(fresh, viewer(ALICE, GENERAL), IN_GENERAL) == ["before restart"]


# --- provenance fails closed ----------------------------------------------


async def test_a_foreign_platform_source_channel_is_refused(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    # Same numeric id as a readable Discord channel. Stored, it would pass the
    # check for a reason that has nothing to do with the channel it names.
    foreign = ChannelRef("issues", GENERAL.platform_channel_id)
    with pytest.raises(UnverifiableProvenance):
        await store.record_turn(ALICE, IN_GENERAL, "q", "a", frozenset({foreign}))
    assert (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).empty


async def test_the_database_refuses_unknown_provenance(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "seed")
    async with clean.connect() as conn:
        person_id = (await conn.execute(text("SELECT person_id FROM conversation_turn"))).scalar()
    for provenance in ("NULL", "ARRAY[500, NULL]::bigint[]"):
        with pytest.raises(IntegrityError):
            async with clean.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO conversation_turn (person_id, location_platform, "
                        "location_id, location_direct, question, answer, channel_ids) "
                        f"VALUES (:p, 'discord', 500, false, 'q', 'a', {provenance})"
                    ),
                    {"p": person_id},
                )


async def test_the_service_does_not_store_a_turn_from_an_unverifiable_source(
    clean: AsyncEngine,
) -> None:
    memory = ConversationMemory(PostgresMemoryStore(clean))
    alice = viewer(ALICE, GENERAL)
    federated = Answer(
        text="from the tracker",
        citations=(
            Citation(
                channel=ChannelRef("issues", 0),
                message_id=0,
                author_display="",
                excerpt="",
                url="",
                source_system="issues",
            ),
        ),
    )
    assert not await memory.remember(
        alice, IN_GENERAL, "q", federated, consulted=(), informed_by=Recollection()
    )
    grounded = Answer(
        text="from general",
        citations=(Citation(GENERAL, 1, "bob", "x", "https://example"),),
    )
    assert await memory.remember(
        alice, IN_GENERAL, "q2", grounded, consulted=(ENG,), informed_by=Recollection()
    )

    # Provenance includes the consulted-but-uncited channel.
    assert (await memory.recall(alice, IN_GENERAL)).empty
    remembered = await memory.recall(viewer(ALICE, GENERAL, ENG), IN_GENERAL)
    assert [t.question for t in remembered.turns] == ["q2"]


# --- provenance carries forward through what was recalled ------------------


async def test_recall_returns_the_provenance_it_checked(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "eng", ENG, GENERAL)
    await record(store, ALICE, IN_GENERAL, "hr", HR)
    await record(store, ALICE, IN_GENERAL, "recent", GENERAL)
    full = viewer(ALICE, GENERAL, ENG, HR)
    turns = (await store.recall(full, IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "digest", turns[1].turn_id)

    recollection = await store.recall(full, IN_GENERAL, 10)
    [summary] = recollection.summaries
    assert summary.covered_channels == {GENERAL, ENG, HR}
    [turn] = recollection.turns
    assert turn.source_channels == {GENERAL}
    assert recollection.source_channels == {GENERAL, ENG, HR}


async def test_a_web_only_follow_up_restating_memory_is_withheld_after_revocation(
    clean: AsyncEngine,
) -> None:
    """Regression: a turn answered with #eng memory in its prompt drew on #eng.

    Its own evidence was the web, so without inheritance it was stored with no
    channels -- and `'{}' <@ anything` returned it to a person who had lost
    every channel.
    """
    store = PostgresMemoryStore(clean)
    memory = ConversationMemory(store)
    await record(store, ALICE, IN_GENERAL, "what did eng decide", ENG)
    then = viewer(ALICE, GENERAL, ENG)
    shown = await memory.recall(then, IN_GENERAL)
    assert [t.question for t in shown.turns] == ["what did eng decide"]

    web_only = Answer(
        text="about that (answer to what did eng decide): the web agrees",
        consulted_channels=frozenset({ChannelRef("web", 0)}),
    )
    assert await memory.remember(
        then,
        IN_GENERAL,
        "and online?",
        web_only,
        consulted=(ChannelRef("web", 0),),
        informed_by=shown,
    )

    assert await questions(store, then, IN_GENERAL) == ["what did eng decide", "and online?"]
    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL) == []
    assert await questions(store, viewer(ALICE), IN_GENERAL) == []


# --- control: opt-out, forget, retention, deletion -------------------------


async def test_opting_out_purges_memory_through_the_existing_mechanism(
    clean: AsyncEngine,
) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "alice 1", GENERAL)
    await record(store, ALICE, ALICE_DM, "alice dm")
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "digest", turns[0].turn_id)
    await record(store, ALICE, IN_GENERAL, "alice 2", GENERAL)
    await record(store, BOB, IN_GENERAL, "bob", GENERAL)

    # The ordinary opt-out, exactly as the bot and the operator run it. No
    # memory-specific call: the purge hangs off the exclusion record.
    await OptOutService(PostgresRetentionStore(clean)).opt_out(ALICE, "asked")

    async with clean.connect() as conn:
        left = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM conversation_turn), "
                    "(SELECT count(*) FROM conversation_summary)"
                )
            )
        ).one()
    assert tuple(left) == (1, 0)
    assert await questions(store, viewer(BOB, GENERAL), IN_GENERAL) == ["bob"]


async def test_an_opted_out_person_has_no_turns_stored(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await PostgresRetentionStore(clean).record_opt_out(ALICE, "never")

    assert not await store.record_turn(ALICE, IN_GENERAL, "q", "a", frozenset())
    assert (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).empty
    async with clean.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM conversation_turn"))).scalar()
    assert count == 0


async def test_opting_back_in_restores_nothing_but_allows_new_turns(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    optout = OptOutService(PostgresRetentionStore(clean))
    await record(store, ALICE, IN_GENERAL, "before", GENERAL)
    await optout.opt_out(ALICE)
    await optout.opt_in(ALICE)
    await record(store, ALICE, IN_GENERAL, "after", GENERAL)

    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL) == ["after"]


async def test_forget_here_leaves_other_locations(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "channel", GENERAL)
    await record(store, ALICE, ALICE_DM, "dm")
    await record(store, BOB, IN_GENERAL, "bob", GENERAL)

    purge = await store.forget(ALICE, IN_GENERAL)
    assert purge.turns == 1

    assert (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).empty
    assert await questions(store, viewer(ALICE), ALICE_DM) == ["dm"]
    assert await questions(store, viewer(BOB, GENERAL), IN_GENERAL) == ["bob"]


async def test_forget_everywhere_removes_turns_and_summaries(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "channel", GENERAL)
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "digest", turns[0].turn_id)
    await record(store, ALICE, ALICE_DM, "dm")
    await record(store, BOB, IN_GENERAL, "bob", GENERAL)

    purge = await store.forget(ALICE, None)
    assert (purge.turns, purge.summaries) == (1, 1)
    assert (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).empty
    assert (await store.recall(viewer(ALICE), ALICE_DM, 10)).empty
    assert await questions(store, viewer(BOB, GENERAL), IN_GENERAL) == ["bob"]


async def test_retention_keys_a_summary_on_its_oldest_content(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "old", GENERAL)
    await record(store, ALICE, IN_GENERAL, "new", GENERAL)
    long_ago = datetime.now(UTC) - timedelta(days=100)
    async with clean.begin() as conn:
        await conn.execute(
            text("UPDATE conversation_turn SET created_at = :at WHERE question = 'old'"),
            {"at": long_ago},
        )
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns
    # Summarise only the old turn: the summary is written today but holds
    # content from 100 days ago.
    assert await store.record_summary(ALICE, IN_GENERAL, "digest", turns[0].turn_id)

    purge = await store.purge_before(datetime.now(UTC) - timedelta(days=30))
    assert (purge.turns, purge.summaries) == (0, 1)
    assert await questions(store, viewer(ALICE, GENERAL), IN_GENERAL) == ["new"]


async def test_deleting_a_person_cascades_to_their_memory(clean: AsyncEngine) -> None:
    store = PostgresMemoryStore(clean)
    await record(store, ALICE, IN_GENERAL, "q", GENERAL)
    turns = (await store.recall(viewer(ALICE, GENERAL), IN_GENERAL, 10)).turns
    assert await store.record_summary(ALICE, IN_GENERAL, "digest", turns[0].turn_id)
    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM person_platform_id"))
        await conn.execute(text("DELETE FROM person"))
        counts = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM conversation_turn), "
                    "(SELECT count(*) FROM conversation_summary)"
                )
            )
        ).one()
    assert tuple(counts) == (0, 0)
