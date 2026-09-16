"""Checks on the golden set itself, cheap enough for the ordinary suite.

The expensive measurement is opted into; these are not, because they guard the
ways a golden set rots without anybody noticing. A set whose judgements have
quietly become unsatisfiable still produces numbers, and the numbers still look
like measurements.
"""

from __future__ import annotations

from tests.evaluation import corpus, questions
from tests.evaluation.harness import resolve_viewers
from tests.evaluation.questions import QUESTIONS, Leg


def test_the_question_set_is_internally_consistent() -> None:
    """The two silent failures: an expected message its own asker cannot read,
    which scores an unavoidable zero, and a forbidden message they cannot
    read, which would turn a breach into a mere relevance dip."""
    questions.validate()


def test_the_set_still_covers_every_kind_of_question_it_claims_to() -> None:
    """A coverage claim decays by deletion. Stating it here means dropping the
    last lexical question fails rather than silently narrowing the set."""
    assert len(QUESTIONS) >= 20
    assert sum(1 for q in QUESTIONS if q.leg is Leg.VECTOR) >= 5
    assert sum(1 for q in QUESTIONS if q.leg is Leg.LEXICAL) >= 3
    assert sum(1 for q in QUESTIONS if not q.judgement.answerable) >= 3
    assert sum(1 for q in QUESTIONS if q.judgement.forbidden) >= 4

    # The same words, a different right answer, because the asker differs.
    by_text: dict[str, set[int]] = {}
    for question in QUESTIONS:
        by_text.setdefault(question.text, set()).add(question.asked_by)
    assert any(len(askers) > 1 for askers in by_text.values()), (
        "no question is asked by more than one viewer, so nothing in the set "
        "measures an answer that differs by who is asking"
    )


def test_the_corpus_spans_channels_with_different_memberships() -> None:
    channels = {seed.channel_id for seed in corpus.SEEDS}
    assert channels == corpus.INDEXED
    readable = set(corpus.VIEWER_CHANNELS.values())
    assert len(readable) == len(corpus.VIEWERS), "two viewers share a channel set"


def test_every_seeded_message_id_is_unique() -> None:
    """The judgements name message ids. A duplicate would make one of them
    mean two different things depending on which row won the upsert."""
    ids = [seed.message_id for seed in corpus.SEEDS]
    assert len(ids) == len(set(ids))


async def test_the_resolved_viewers_are_the_ones_the_corpus_declares() -> None:
    """Two statements of the same permissions, one of which is not ours.

    The corpus declares who reads what; `DiscordAclResolver` computes it. If
    they disagree, the golden set is scoring against permissions the bot does
    not apply, and its ACL result means nothing.
    """
    resolved = await resolve_viewers()
    actual = {
        user_id: frozenset(c.platform_channel_id for c in viewer.visible_channels)
        for user_id, viewer in resolved.items()
    }
    assert actual == corpus.VIEWER_CHANNELS
