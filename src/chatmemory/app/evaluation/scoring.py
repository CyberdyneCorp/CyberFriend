"""Scoring for the retrieval golden set.

Every function here is deliberately dull. This is a measurement instrument: if
it is wrong, every decision taken on the strength of its numbers is wrong too,
and a subtly clever metric is indistinguishable from a correct one until long
after it has been trusted. So recall is a set intersection divided by a set
size, reciprocal rank is one over an index, and neither smooths, weights,
normalises, nor interpolates anything.

Two rules shape the types rather than the comments.

**A permission failure is not a quality number.** `AclBreach` is a value, never
a float, and no aggregate on `GoldenReport` consumes one. That is structural
on purpose: a viewer returning a message they may not read is a breach, and a
breach averaged into a mean is a breach that can be offset by two good
questions and reported as 0.94. `GoldenReport.summary` leads with the breach
count for the same reason -- a reader who only skims the first line must still
see it.

**A judgement is about messages, not windows.** The retrieval unit is a
conversation window, and window boundaries move whenever the windowing rules
change. Scoring against window ids would therefore turn a windowing tweak into
a retrieval regression. A window counts as relevant when it *carries* one of
the messages that actually answers the question.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_K = 10
"""Depth both metrics are computed at.

Ten because it is roughly what the answering prompt can hold: evidence that
never enters the context cannot ground an answer, so recall past that depth
measures something the system does not use.
"""


@dataclass(frozen=True, slots=True)
class RankedResult:
    """One position in a ranked result list, reduced to what scoring needs.

    Deliberately not `SearchHit`. Scoring must not be able to read a hit's
    text or score -- a metric that peeks at the score is a metric that can be
    tuned to agree with the ranker it is meant to judge.
    """

    window_id: int
    channel_id: int
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Judgement:
    """What one question expects, as platform message ids.

    `expected` holds only the messages that actually answer the question, not
    every message in the conversation around them. Listing a whole thread
    would make recall a measure of how the windower happened to pack that
    thread rather than of whether retrieval found the answer.

    `forbidden` names the messages a plausible-but-wrong retriever reaches
    for: the near-duplicate topic, the thread that shares a keyword. They are
    readable by the viewer, so surfacing one is a quality defect and not a
    breach -- the ACL dimension is scored separately and never mixed in here.

    An empty `expected` means the corpus does not answer the question. Such a
    question is scored entirely on `forbidden`, because a retriever with no
    similarity floor always returns *something*; the measurable claim is not
    "it returned nothing" but "it did not return the trap".
    """

    expected: frozenset[int] = frozenset()
    forbidden: frozenset[int] = frozenset()

    @property
    def answerable(self) -> bool:
        return bool(self.expected)


@dataclass(frozen=True, slots=True)
class QuestionScore:
    """One question's result, kept per-question so a regression has a name.

    A mean tells you retrieval got worse. It does not tell you which question
    broke, and "recall fell from 0.91 to 0.86" is not something anyone can
    act on.
    """

    question_id: str
    answerable: bool
    returned: int
    recall_at_k: float | None
    reciprocal_rank: float | None
    first_relevant_rank: int | None
    missed: tuple[int, ...]
    forbidden_at_k: tuple[int, ...]
    first_forbidden_rank: int | None = None
    baited: bool = False
    """Whether the question named a near-duplicate at all.

    Kept because `first_forbidden_rank is None` conflates "the bait stayed
    away" with "there was no bait", and averaging the second into a
    trap-avoidance rate would reward adding questions that cannot fail.
    """

    @property
    def trapped(self) -> bool:
        return bool(self.forbidden_at_k)

    @property
    def outranked_by_trap(self) -> bool:
        """The near-duplicate beat the real answer.

        Membership in the top k is a weak claim -- with a small readable
        corpus the whole corpus is the top k -- and this is the strong one.
        It says the ranker preferred the confusable thread to the correct
        one, which is a defect no corpus size can explain away.
        """
        if not self.answerable or self.first_forbidden_rank is None:
            return False
        if self.first_relevant_rank is None:
            return True
        return self.first_forbidden_rank < self.first_relevant_rank

    def line(self, k: int) -> str:
        if self.answerable:
            recall = f"recall@{k}={self.recall_at_k:.2f}" if self.recall_at_k is not None else ""
            rank = self.first_relevant_rank or 0
            body = f"{recall} rr={self.reciprocal_rank:.3f} rank={rank or '-'}"
        else:
            body = f"unanswerable  trap_rank={self.first_forbidden_rank or '-'}"
        notes = []
        if self.missed:
            notes.append(f"missed={list(self.missed)}")
        if self.forbidden_at_k:
            notes.append(f"trap@{self.first_forbidden_rank}={list(self.forbidden_at_k)}")
        if self.outranked_by_trap:
            notes.append("OUTRANKED")
        suffix = ("  " + " ".join(notes)) if notes else ""
        return f"  {self.question_id:<28} {body:<34} n={self.returned}{suffix}"


@dataclass(frozen=True, slots=True)
class AclBreach:
    """A result a viewer was never allowed to see.

    Carries the question and the viewer because those two together are the
    reproduction: a breach that says only "window 412 leaked" cannot be
    re-run.
    """

    question_id: str
    viewer: str
    window_id: int
    channel_id: int
    reason: str

    def line(self) -> str:
        return (
            f"  BREACH {self.question_id} viewer={self.viewer} "
            f"window={self.window_id} channel={self.channel_id}: {self.reason}"
        )


def score_question(
    question_id: str,
    judgement: Judgement,
    ranked: Sequence[RankedResult],
    k: int = DEFAULT_K,
) -> QuestionScore:
    """Recall@k and reciprocal rank for one question.

    recall@k = (answer-bearing messages carried by the top k) / (all of them).
    reciprocal rank = 1 / (position of the first result carrying any of them),
    and 0.0 when none of the top k does -- 0.0 rather than `None`, because a
    question that was asked and not answered belongs in the mean. `None` is
    reserved for a question where the metric is undefined, which is only the
    unanswerable ones.
    """
    top = list(ranked[:k])
    carried = {mid for result in top for mid in result.message_ids}

    recall: float | None = None
    reciprocal: float | None = None
    rank: int | None = None
    missed: tuple[int, ...] = ()

    if judgement.answerable:
        hit = judgement.expected & carried
        recall = len(hit) / len(judgement.expected)
        missed = tuple(sorted(judgement.expected - carried))
        rank = _first_relevant_rank(judgement.expected, top)
        reciprocal = 1.0 / rank if rank is not None else 0.0

    return QuestionScore(
        question_id=question_id,
        answerable=judgement.answerable,
        returned=len(ranked),
        recall_at_k=recall,
        reciprocal_rank=reciprocal,
        first_relevant_rank=rank,
        missed=missed,
        forbidden_at_k=tuple(sorted(judgement.forbidden & carried)),
        first_forbidden_rank=_first_relevant_rank(judgement.forbidden, top),
        baited=bool(judgement.forbidden),
    )


def _first_relevant_rank(
    wanted: frozenset[int], top: Sequence[RankedResult]
) -> int | None:
    """1-based position of the first result carrying one of `wanted`.

    Used for both the expected set and the forbidden one, because "where did
    it land" is the same question either way.
    """
    if not wanted:
        return None
    for position, result in enumerate(top, start=1):
        if wanted & set(result.message_ids):
            return position
    return None


def acl_breaches(
    question_id: str,
    viewer: str,
    readable_channels: frozenset[int],
    message_channels: Mapping[int, int],
    ranked: Sequence[RankedResult],
) -> tuple[AclBreach, ...]:
    """Every result in `ranked` the viewer had no right to receive.

    Checked over the whole list rather than the top k. A leak at rank 40 is
    the same disclosure as a leak at rank 1; truncating first would make the
    permission check inherit a number chosen for relevance.

    Both the window's own channel and the provenance of every message it
    cites are checked. The second is not redundant: a window is a projection
    over messages, and a rebuild that mixed channels would produce a window
    filed under a readable channel whose text came from elsewhere -- which
    the channel predicate alone cannot see.

    `message_channels` is the corpus's own ground truth, not something read
    back out of the database, so a message the system returns and the corpus
    never seeded is reported rather than assumed permitted. Fail closed: an
    unexplained id in a permission check is exactly the thing not to shrug at.
    """
    found: list[AclBreach] = []
    for result in ranked:
        if result.channel_id not in readable_channels:
            found.append(
                AclBreach(
                    question_id=question_id,
                    viewer=viewer,
                    window_id=result.window_id,
                    channel_id=result.channel_id,
                    reason="window is in a channel the viewer may not read",
                )
            )
            continue
        for message_id in result.message_ids:
            origin = message_channels.get(message_id)
            if origin is None:
                found.append(
                    AclBreach(
                        question_id=question_id,
                        viewer=viewer,
                        window_id=result.window_id,
                        channel_id=result.channel_id,
                        reason=f"cites message {message_id}, which the corpus never seeded",
                    )
                )
            elif origin not in readable_channels:
                found.append(
                    AclBreach(
                        question_id=question_id,
                        viewer=viewer,
                        window_id=result.window_id,
                        channel_id=result.channel_id,
                        reason=(
                            f"cites message {message_id} from channel {origin}, "
                            "which the viewer may not read"
                        ),
                    )
                )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Provenance:
    """What produced a set of numbers.

    Recorded alongside the baseline because a number without it cannot be
    compared against anything: a recall of 0.9 measured with one embedding
    model says nothing about a recall of 0.8 measured with another.
    """

    measured_on: str
    embedding_model: str
    embedding_dimensions: int
    corpus_messages: int
    corpus_windows: int
    viewers: int


@dataclass(frozen=True, slots=True)
class GoldenReport:
    """Everything one run measured.

    The ACL dimension is a separate field from the scores and is never folded
    into either mean. See the module docstring for why that is a type-level
    decision rather than a convention.
    """

    k: int
    provenance: Provenance
    scores: tuple[QuestionScore, ...] = ()
    breaches: tuple[AclBreach, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def answerable(self) -> tuple[QuestionScore, ...]:
        return tuple(s for s in self.scores if s.answerable)

    @property
    def unanswerable(self) -> tuple[QuestionScore, ...]:
        return tuple(s for s in self.scores if not s.answerable)

    @property
    def mean_recall_at_k(self) -> float:
        """Mean over answerable questions only. Never over breaches."""
        values = [s.recall_at_k for s in self.answerable if s.recall_at_k is not None]
        return sum(values) / len(values) if values else 0.0

    @property
    def mean_reciprocal_rank(self) -> float:
        values = [s.reciprocal_rank for s in self.answerable if s.reciprocal_rank is not None]
        return sum(values) / len(values) if values else 0.0

    @property
    def trapped(self) -> tuple[QuestionScore, ...]:
        """Questions that surfaced a readable-but-wrong near-duplicate."""
        return tuple(s for s in self.scores if s.trapped)

    @property
    def outranked(self) -> tuple[QuestionScore, ...]:
        """Questions whose near-duplicate beat the real answer."""
        return tuple(s for s in self.scores if s.outranked_by_trap)

    @property
    def clean_unanswerable(self) -> float:
        """Share of baited no-answer questions that avoided their bait.

        Over the baited ones only. A question with nothing to be fooled by
        cannot be fooled, and counting it would let the rate be improved by
        adding questions that cannot fail.
        """
        rows = tuple(s for s in self.unanswerable if s.baited)
        if not rows:
            return 0.0
        return sum(1 for s in rows if not s.trapped) / len(rows)

    @property
    def bait_at_the_top(self) -> tuple[QuestionScore, ...]:
        """No-answer questions whose bait was ranked first.

        The hard claim about an unanswerable question. Whether the bait
        appears at all depends on how much of the corpus the viewer can read;
        whether the system leads with it does not.
        """
        return tuple(
            s for s in self.unanswerable if s.baited and s.first_forbidden_rank == 1
        )

    def summary(self) -> str:
        """The first line a reader sees. It leads with the breach count."""
        acl = "0 breaches" if not self.breaches else f"{len(self.breaches)} BREACHES"
        return (
            f"acl={acl}  "
            f"recall@{self.k}={self.mean_recall_at_k:.3f}  "
            f"mrr={self.mean_reciprocal_rank:.3f}  "
            f"clean_unanswerable={self.clean_unanswerable:.3f}  "
            f"traps={len(self.trapped)}/{len(self.scores)}  "
            f"outranked={len(self.outranked)}"
        )

    def render(self) -> str:
        """The whole run as text, for the log and for the baseline file."""
        head = [
            f"retrieval golden set  k={self.k}",
            f"measured_on={self.provenance.measured_on}",
            f"embedding_model={self.provenance.embedding_model} "
            f"dimensions={self.provenance.embedding_dimensions}",
            f"corpus messages={self.provenance.corpus_messages} "
            f"windows={self.provenance.corpus_windows} "
            f"viewers={self.provenance.viewers}",
            "",
            self.summary(),
            "",
            "ACL",
        ]
        head += [b.line() for b in self.breaches] or ["  none"]
        head += ["", "per question"]
        head += [s.line(self.k) for s in self.scores]
        if self.notes:
            head += ["", "notes", *(f"  {n}" for n in self.notes)]
        return "\n".join(head)
