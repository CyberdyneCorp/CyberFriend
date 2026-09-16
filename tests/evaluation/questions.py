"""Twenty-one questions with known-correct sources.

Each one names the messages that actually answer it -- not the conversation
around them. A judgement listing a whole thread would measure how the windower
packed that thread rather than whether retrieval found the answer, and would
change every time the windowing rules did.

`leg` is the claim each question is making about *why* it can be answered, and
it is checked rather than asserted in prose:

* `VECTOR` says the wording shares no searchable stem with its own answer. The
  run confirms it by asking the lexical statement alone and requiring that it
  return none of the expected messages. That check is deterministic -- a
  lexeme either matches or it does not -- so a question that quietly becomes
  answerable by keyword fails loudly instead of inflating the score.
* `LEXICAL` says an exact token -- an error code, a migration name, a test name
  -- is the only reliable handle. The run confirms the lexical leg finds it.
  The converse is deliberately *not* asserted: whether the embedding model also
  happens to place a bare identifier near its conversation is a property of the
  model, and freezing it would make the set fail on an unrelated upgrade.
* `EITHER` makes no claim about which leg answers it.

`forbidden` names readable-but-wrong messages, never unreadable ones. A viewer
receiving something they may not read is a breach, and the ACL dimension
counts breaches on its own; letting one land in a quality set would let a
disclosure be reported as a relevance dip. `validate` enforces the separation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chatmemory.app.evaluation.scoring import Judgement
from tests.evaluation import corpus
from tests.evaluation.corpus import DESIGNER, LEAD, NEWCOMER, STAFF


class Leg(StrEnum):
    VECTOR = "vector"
    LEXICAL = "lexical"
    EITHER = "either"


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    id: str
    text: str
    asked_by: int
    judgement: Judgement
    leg: Leg
    why: str


def _q(
    question_id: str,
    text: str,
    asked_by: int,
    leg: Leg,
    why: str,
    expected: frozenset[int] = frozenset(),
    forbidden: frozenset[int] = frozenset(),
) -> GoldenQuestion:
    return GoldenQuestion(
        id=question_id,
        text=text,
        asked_by=asked_by,
        judgement=Judgement(expected=expected, forbidden=forbidden),
        leg=leg,
        why=why,
    )


QUESTIONS: tuple[GoldenQuestion, ...] = (
    # --- answerable by meaning alone ------------------------------------
    _q(
        "coffee-paraphrase",
        "is there any way to get a hot drink in the building at the moment",
        NEWCOMER,
        Leg.VECTOR,
        "no shared stem with 'espresso', 'instant' or 'vendor'",
        expected=frozenset({100103, 100104}),
    ),
    _q(
        "sleep-advice",
        "any advice for sleeping through a racket at night",
        DESIGNER,
        Leg.VECTOR,
        "the answer is a product recommendation, phrased as neither",
        expected=frozenset({100302}),
    ),
    _q(
        "payment-failures",
        "why were customers unable to complete a purchase",
        STAFF,
        Leg.VECTOR,
        "the confusable twin is the other timeout in the same channel",
        expected=frozenset({200101, 200103}),
        forbidden=corpus.ids("search-latency"),
    ),
    _q(
        "slow-results",
        "what was making browsing feel sluggish before it was sorted",
        STAFF,
        Leg.VECTOR,
        "the mirror image of payment-failures; each is the other's trap",
        expected=frozenset({200301, 200302}),
        forbidden=corpus.ids("checkout-timeout"),
    ),
    _q(
        "overnight-stall",
        "what made the shop stop responding in the small hours",
        STAFF,
        Leg.VECTOR,
        "'locked the orders table' is the answer and shares nothing with the question",
        expected=frozenset({200201, 200202}),
    ),
    _q(
        "hiring-allowance",
        "how many people are we allowed to take on",
        LEAD,
        Leg.VECTOR,
        "leadership-only; 'approval for two more engineers' is the answer",
        expected=frozenset({300201}),
    ),
    _q(
        "supplier-payment",
        "what is holding up settling with the data supplier",
        LEAD,
        Leg.VECTOR,
        "leadership-only; 'withhold payment until they issue a credit note'",
        expected=frozenset({300301, 300302}),
    ),
    _q(
        "company-look",
        "are we changing how the company looks on paper",
        DESIGNER,
        Leg.VECTOR,
        "design-only; 'wordmark drops the gradient' shares no stem with it",
        expected=frozenset({400101}),
    ),
    # --- answerable only by an exact token -------------------------------
    _q(
        "error-code",
        "ERR_PSP_TIMEOUT_4471",
        STAFF,
        Leg.LEXICAL,
        "an error code has no paraphrase; the lexical leg is the whole point",
        expected=frozenset({200102}),
    ),
    _q(
        "migration-name",
        "2026_08_14_orders_idx",
        STAFF,
        Leg.LEXICAL,
        "a migration filename, quoted from a log or a review",
        expected=frozenset({200204}),
    ),
    _q(
        "test-name",
        "test_window_rebuild_is_idempotent",
        STAFF,
        Leg.LEXICAL,
        "a test name pasted straight from CI output",
        expected=frozenset({200401}),
    ),
    # --- either leg may serve --------------------------------------------
    _q(
        "parking-badge",
        "do i still have to badge in to bring the car down",
        NEWCOMER,
        Leg.EITHER,
        "shares 'badge' and 'car' with the corpus, so both legs can reach it",
        expected=frozenset({100204}),
    ),
    _q(
        "party-venue",
        "where is the end of year celebration happening",
        NEWCOMER,
        Leg.EITHER,
        "'year' appears in two unrelated threads, so the lexical leg has company",
        expected=frozenset({100501}),
    ),
    _q(
        "contrast-failure",
        "which element is failing the accessibility check",
        DESIGNER,
        Leg.EITHER,
        "design-only; 'fails contrast' is close enough for either leg",
        expected=frozenset({400201}),
    ),
    _q(
        "illustration-brief",
        "who is drawing the new artwork for the pricing page",
        DESIGNER,
        Leg.EITHER,
        "design-only; a later conversation, so recency is not what finds it",
        expected=frozenset({400302}),
    ),
    # --- the same question, a different right answer per viewer ----------
    #
    # Not two phrasings of one question. The corpus genuinely holds two
    # different truths about the office, one confirmed and restricted and one
    # unconfirmed and public, and the correct result depends entirely on who
    # is asking. A retriever that returns the same thing to both is wrong for
    # one of them, and which one it is wrong for decides whether that is a
    # quality bug or a disclosure.
    _q(
        "office-change-lead",
        "what do we actually know about the office changing",
        LEAD,
        Leg.EITHER,
        "the lead may read the signed relocation",
        expected=frozenset({300101, 300102}),
    ),
    _q(
        "office-change-staff",
        "what do we actually know about the office changing",
        STAFF,
        Leg.EITHER,
        "the same words; for a staffer the only readable answer is the rumour",
        expected=frozenset({100401}),
    ),
    # Deliberately without a bait, and the reason is a finding in itself. The
    # first measured run named the rumour thread as this question's trap and
    # found it at rank one -- but the rumour thread is the most relevant thing
    # in #general about the office changing, so surfacing it is retrieval
    # working. What must not happen is an answer presenting hearsay as fact,
    # and that is the grounding stage's job, not the ranker's. What this
    # question measures at this layer is therefore the whole of what retrieval
    # owes it: the confirmed relocation, which the asker may not read, appears
    # nowhere in twenty results -- checked by the ACL sweep.
    _q(
        "relocation-newcomer",
        "are we moving to a new address in the spring",
        NEWCOMER,
        Leg.EITHER,
        "the readable corpus holds no confirmed answer, and the restricted one "
        "must stay out of reach",
    ),
    # --- the right answer is nothing --------------------------------------
    #
    # Retrieval has no similarity floor: the vector leg returns its nearest
    # neighbours whether or not any of them is relevant. So these are not
    # scored on returning an empty list, which would never happen. They are
    # scored on whether the topic-adjacent thread the question is bait for
    # reaches the top of the list.
    _q(
        "bike-shed",
        "when is the bicycle shed being repainted",
        NEWCOMER,
        Leg.EITHER,
        "facilities-shaped, and the car park thread is the facilities bait",
        forbidden=corpus.ids("parking"),
    ),
    _q(
        "cloud-provider",
        "which cloud provider are we migrating to",
        STAFF,
        Leg.EITHER,
        "'migrating' stems onto the database migration thread, which is not an answer",
        forbidden=corpus.ids("index-lock"),
    ),
    _q(
        "unpaid-leave",
        "how much notice do i have to give before taking unpaid leave",
        LEAD,
        Leg.EITHER,
        "compensation-shaped, and the salary band thread is the bait",
        forbidden=corpus.ids("headcount"),
    ),
)


def validate() -> None:
    """Self-checks on the set, run before any of it is believed.

    A golden set is only as good as its own consistency, and each of these
    has a way of silently becoming false while every question still looks
    fine: an expected message the asker cannot read scores an unavoidable
    zero, and a forbidden message they cannot read turns a breach into a
    relevance number.
    """
    seen: set[str] = set()
    for question in QUESTIONS:
        assert question.id not in seen, f"duplicate question id {question.id}"
        seen.add(question.id)

        readable = corpus.readable_by(question.asked_by)
        for message_id in question.judgement.expected:
            channel = corpus.MESSAGE_CHANNELS.get(message_id)
            assert channel is not None, f"{question.id} expects unseeded message {message_id}"
            assert channel in readable, (
                f"{question.id} expects message {message_id} from channel {channel}, "
                f"which its asker may not read"
            )
        for message_id in question.judgement.forbidden:
            channel = corpus.MESSAGE_CHANNELS.get(message_id)
            assert channel is not None, f"{question.id} forbids unseeded message {message_id}"
            assert channel in readable, (
                f"{question.id} forbids message {message_id} from channel {channel}, "
                "which its asker may not read: that is a breach, not a quality trap"
            )
        assert not (question.judgement.expected & question.judgement.forbidden), (
            f"{question.id} both expects and forbids the same message"
        )
        if question.leg is Leg.VECTOR:
            assert question.judgement.answerable, (
                f"{question.id} claims a vector-only answer but expects nothing"
            )
