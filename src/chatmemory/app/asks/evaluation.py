"""A hand-labelled set, and the metrics that gate this feature.

Extraction quality is a property of the *model behind the configured endpoint*,
not of this code. The endpoint may be a self-hosted gateway whose
structured-output reliability differs from a hosted one, so quality here is
measured rather than assumed.

**Precision is the gate.** A false "you need to do X" costs more than a missed
one: a missed ask costs somebody a thing they already knew about, while a
fabricated ask costs them trust in every entry, because they cannot tell which
to doubt. Recall is reported so the trade is visible, not enforced.

Addressee accuracy is measured separately because it is the likeliest source of
wrong entries, and because an ask attributed to the wrong person is worse than
one left unattributed.

Decisions ride the same model call, so they have a set of their own here, in
Portuguese and English, gated on precision the same way: a false "we decided
X" is worse than a missed one, because it is repeated as settled. The ask set
has to keep passing too, since the prompt both are read by is shared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.model import (
    AddresseeKind,
    Ask,
    AskCandidate,
    AskKind,
    AskPolicy,
)
from chatmemory.app.asks.resolution import StaticDirectory
from chatmemory.app.decisions.model import DecisionPolicy, ExtractedDecision
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

#: Precision below this is a release blocker. Recall is reported, not gated.
PRECISION_GATE = 0.9

#: Addressee accuracy over correctly-found asks. Lower than the precision gate
#: because `unattributed` is an acceptable outcome and is counted as a miss
#: only when the addressee was in fact determinable.
ADDRESSEE_GATE = 0.8

PLATFORM = "discord"
CHANNEL = ChannelRef(PLATFORM, 900)
T0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

#: The cast. Display names are what a model sees, so the directory it is
#: measured against has to be the one used in production.
PEOPLE: dict[str, PersonRef] = {
    "leo": PersonRef(PLATFORM, 11),
    "hezron": PersonRef(PLATFORM, 12),
    "clinton": PersonRef(PLATFORM, 13),
    "george": PersonRef(PLATFORM, 14),
    "amina": PersonRef(PLATFORM, 15),
}

GROUP = "*group*"
"""Sentinel label for an ask directed at a room or a team."""


@dataclass(frozen=True, slots=True)
class Expected:
    kind: AskKind
    addressee: str | None
    """A name from `PEOPLE`, `GROUP`, or None for unattributed."""

    @property
    def addressee_kind(self) -> AddresseeKind:
        if self.addressee is None:
            return AddresseeKind.UNATTRIBUTED
        if self.addressee == GROUP:
            return AddresseeKind.GROUP
        return AddresseeKind.PERSON


@dataclass(frozen=True, slots=True)
class LabelledExample:
    example_id: str
    author: str
    content: str
    mentions: tuple[str, ...] = ()
    reply_to: tuple[str, str] | None = None
    expected: Expected | None = None
    in_dm: bool = False


#: Hand-labelled. Written to look like the traffic this runs over: mentions,
#: replies, names without mentions, group address, and -- in the majority --
#: ordinary conversation that asks nothing of anybody.
EXAMPLES: tuple[LabelledExample, ...] = (
    LabelledExample(
        "mention-request",
        "hezron",
        "@leo can you take a look at the ingestion PR before EOD?",
        mentions=("leo",),
        expected=Expected(AskKind.REQUEST, "leo"),
    ),
    LabelledExample(
        "mention-question",
        "amina",
        "@george do we have the postgres creds for the new box yet?",
        mentions=("george",),
        expected=Expected(AskKind.QUESTION, "george"),
    ),
    LabelledExample(
        "reply-request",
        "clinton",
        "could you re-run the migration on staging when you get a sec?",
        reply_to=("leo", "migration 0002 is merged"),
        expected=Expected(AskKind.REQUEST, "leo"),
    ),
    LabelledExample(
        "reply-question",
        "george",
        "is that going out today or tomorrow?",
        reply_to=("amina", "the retreat deck is basically done"),
        expected=Expected(AskKind.QUESTION, "amina"),
    ),
    LabelledExample(
        "named-request",
        "amina",
        "clinton can you send me the pods deck before the retreat?",
        expected=Expected(AskKind.REQUEST, "clinton"),
    ),
    LabelledExample(
        "commitment",
        "leo",
        "i'll push the fix for the reconciler tonight",
        expected=Expected(AskKind.COMMITMENT, "leo"),
    ),
    LabelledExample(
        "commitment-reply",
        "george",
        "ok I'll take the invoice thing, you can drop it",
        reply_to=("amina", "someone needs to chase the invoice"),
        expected=Expected(AskKind.COMMITMENT, "george"),
    ),
    LabelledExample(
        "group-request",
        "hezron",
        "can someone on the platform team look at the failing nightly build?",
        expected=Expected(AskKind.REQUEST, GROUP),
    ),
    LabelledExample(
        "group-question",
        "clinton",
        "does anyone know why the gateway keeps reconnecting every few minutes?",
        expected=Expected(AskKind.QUESTION, GROUP),
    ),
    LabelledExample(
        # The addressee is a real person we do not know. Guessing a member of
        # the server here would be the worst available outcome.
        "unknown-name",
        "hezron",
        "can nadia take a look at the terraform for the new cluster?",
        reply_to=("amina", "the new cluster still has no terraform"),
        expected=Expected(AskKind.REQUEST, None),
    ),
    LabelledExample(
        "statement-with-you",
        "leo",
        "you know what, the whole retopo pipeline is just slow at this point",
    ),
    LabelledExample(
        "rhetorical",
        "clinton",
        "why is postgres like this, honestly",
    ),
    LabelledExample(
        "rhetorical-second-person",
        "george",
        "can you believe they shipped that without a single test?",
    ),
    LabelledExample(
        "thanks",
        "amina",
        "thanks @leo, that worked perfectly",
        mentions=("leo",),
    ),
    LabelledExample(
        "praise",
        "hezron",
        "@clinton nice work on the pods deck, that landed really well",
        mentions=("clinton",),
    ),
    LabelledExample(
        "self-answered",
        "george",
        "i was going to ask you about the schema but i found it, it's in 0001",
        reply_to=("leo", "schema is in migrations"),
    ),
    LabelledExample(
        "hypothetical",
        "leo",
        "if you ever need the old dumps they're in the s3 bucket under backups",
    ),
    LabelledExample(
        "announcement",
        "amina",
        "heads up everyone, standup moved to 9:30 starting monday",
    ),
    LabelledExample(
        # "I'll" without a commitment behind it. The nearest miss for the
        # commitment kind, and the one worth labelling.
        "not-a-commitment",
        "clinton",
        "i'll be honest with you, i don't love the design as it stands",
    ),
    LabelledExample(
        "already-done",
        "hezron",
        "someone already fixed it, we're good",
    ),
    LabelledExample(
        "agreement",
        "george",
        "you're right, that's exactly what happened last time",
        reply_to=("leo", "the gateway dropped the edit event"),
    ),
    LabelledExample(
        "status-update",
        "leo",
        "ingestion is caught up to yesterday for all the indexed channels",
    ),
)


def directory() -> StaticDirectory:
    return StaticDirectory(PEOPLE)


def names() -> dict[PersonRef, str]:
    return {person: name for name, person in PEOPLE.items()}


def candidate_of(example: LabelledExample, index: int = 0) -> AskCandidate:
    """Build the candidate an extractor is given for a labelled example.

    Goes through the real candidate filter so the evaluation measures the
    pipeline as deployed, including the cost filter: an example the filter
    rejects yields no candidate and therefore no ask.
    """
    at = T0 + timedelta(minutes=index)
    parent: Message | None = None
    messages: list[Message] = []
    if example.reply_to is not None:
        author, content = example.reply_to
        parent = _message(1000 + index * 2, author, content, at - timedelta(minutes=1))
        messages.append(parent)

    message = _message(
        1001 + index * 2,
        example.author,
        example.content,
        at,
        mentions=frozenset(PEOPLE[m] for m in example.mentions),
        reply_to_id=parent.platform_message_id if parent else None,
    )
    messages.append(message)

    dm = frozenset({CHANNEL}) if example.in_dm else frozenset()
    found = CandidateFilter(dm_channels=dm).candidates(messages)
    match = [c for c in found if c.message.platform_message_id == message.platform_message_id]
    if match:
        return match[0]

    # No addressee signal: extraction never sees it. Represented as a candidate
    # with no signal would be a lie, so the caller is told by way of an empty
    # prediction instead.
    raise LookupError(example.example_id)


def is_candidate(example: LabelledExample, index: int = 0) -> bool:
    try:
        candidate_of(example, index)
    except LookupError:
        return False
    return True


def _message(
    message_id: int,
    author: str,
    content: str,
    at: datetime,
    mentions: frozenset[PersonRef] = frozenset(),
    reply_to_id: int | None = None,
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=CHANNEL,
        author=PEOPLE[author],
        content=content,
        created_at=at,
        reply_to_id=reply_to_id,
        mentions=mentions,
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    addressee_correct: int = 0
    addressee_total: int = 0

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        # No predictions at all is vacuously precise. Reported as such rather
        # than as zero, because zero would read as a failure when the correct
        # description is "it asserted nothing".
        return 1.0 if found == 0 else self.true_positives / found

    @property
    def recall(self) -> float:
        expected = self.true_positives + self.false_negatives
        return 1.0 if expected == 0 else self.true_positives / expected

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 0.0 if total == 0 else 2 * self.precision * self.recall / total

    @property
    def addressee_accuracy(self) -> float:
        if self.addressee_total == 0:
            return 1.0
        return self.addressee_correct / self.addressee_total

    def report(self) -> str:
        return (
            f"precision={self.precision:.3f} recall={self.recall:.3f} "
            f"f1={self.f1:.3f} addressee={self.addressee_accuracy:.3f} "
            f"(tp={self.true_positives} fp={self.false_positives} "
            f"fn={self.false_negatives})"
        )


def evaluate(
    predictions: Mapping[str, Sequence[Ask]],
    examples: Sequence[LabelledExample] = EXAMPLES,
    policy: AskPolicy | None = None,
) -> Metrics:
    """Score predicted asks against the labels.

    Only asks at or above the presentation threshold are scored: an extraction
    that never reaches an answer cannot mislead anybody, and counting it would
    measure something the user never sees.
    """
    settings = policy or AskPolicy()
    tp = fp = fn = 0
    addressee_correct = addressee_total = 0

    for example in examples:
        found = [
            ask
            for ask in predictions.get(example.example_id, ())
            if settings.presentable(ask.confidence)
        ]
        if example.expected is None:
            fp += len(found)
            continue

        matching = [ask for ask in found if ask.kind is example.expected.kind]
        if not matching:
            fn += 1
            fp += len(found)
            continue

        tp += 1
        fp += len(found) - 1
        addressee_total += 1
        if _addressee_matches(matching[0], example.expected):
            addressee_correct += 1

    return Metrics(tp, fp, fn, addressee_correct, addressee_total)


def _addressee_matches(ask: Ask, expected: Expected) -> bool:
    if ask.addressee.kind is not expected.addressee_kind:
        return False
    if expected.addressee_kind is not AddresseeKind.PERSON:
        return True
    return ask.addressee.person == PEOPLE[str(expected.addressee)]


# --- decisions --------------------------------------------------------------

#: Precision below this is a release blocker for the prompt, as for asks.
DECISION_PRECISION_GATE = 0.9


@dataclass(frozen=True, slots=True)
class DecisionExample:
    """A message, the conversation shown before it, and whether it decides.

    `context` is `(author, content)` pairs, oldest first: most conclusions only
    name what was chosen in the message that proposed it.
    """

    example_id: str
    author: str
    content: str
    context: tuple[tuple[str, str], ...] = ()
    decides: bool = False


#: Hand-labelled, in the two languages this server writes in. The negatives are
#: the near misses: proposals, questions and preferences that use the same
#: words a conclusion does.
DECISION_EXAMPLES: tuple[DecisionExample, ...] = (
    DecisionExample(
        "pt-fechou",
        "leo",
        "fechou, vamos com Postgres então",
        context=(("hezron", "Postgres ou Mongo pro serviço novo?"),),
        decides=True,
    ),
    DecisionExample(
        "pt-ficou-decidido",
        "amina",
        "ficou decidido: o deploy passa a ser na sexta",
        decides=True,
    ),
    DecisionExample(
        "pt-combinado",
        "george",
        "combinado, retro só a cada duas semanas a partir de agora",
        context=(("amina", "retro toda semana tá pesado, bora fazer quinzenal?"),),
        decides=True,
    ),
    DecisionExample(
        "pt-bora-de",
        "clinton",
        "beleza, bora de Vercel pro front",
        context=(
            ("leo", "Vercel ou Netlify?"),
            ("hezron", "Vercel já tem o preview que a gente precisa"),
        ),
        decides=True,
    ),
    DecisionExample(
        "pt-decidimos",
        "hezron",
        "decidimos manter o Redis até o fim do trimestre",
        decides=True,
    ),
    DecisionExample(
        "en-we-decided",
        "amina",
        "we decided to freeze the API until the retreat",
        decides=True,
    ),
    DecisionExample(
        "en-lets-go-with",
        "leo",
        "ok, let's go with the managed database then",
        context=(("george", "self-host or managed for the new postgres?"),),
        decides=True,
    ),
    DecisionExample(
        "en-agreed",
        "george",
        "agreed, final call: the launch moves to october",
        decides=True,
    ),
    DecisionExample(
        "pt-proposta",
        "amina",
        "bora fazer o deploy na sexta?",
    ),
    DecisionExample(
        "pt-que-tal",
        "leo",
        "que tal a gente ir de Postgres? vamos com calma nisso",
    ),
    DecisionExample(
        "pt-preferencia",
        "clinton",
        "eu fecharia com a Vercel, mas bora ouvir todo mundo antes",
    ),
    DecisionExample(
        "pt-pergunta",
        "george",
        "ficou decidido alguma coisa sobre o deploy?",
    ),
    DecisionExample(
        "pt-status",
        "hezron",
        "fechou a sprint, 14 tickets entregues",
    ),
    DecisionExample(
        "pt-convite",
        "amina",
        "bora almoçar?",
    ),
    DecisionExample(
        "en-question",
        "clinton",
        "was it agreed that the schema freezes before the retreat?",
    ),
    DecisionExample(
        "en-proposal",
        "leo",
        "what if we're going with option 2 instead?",
    ),
    DecisionExample(
        "en-still-open",
        "george",
        "we decided nothing yet, let's pick it up tomorrow",
    ),
    DecisionExample(
        "en-agreement-with-a-fact",
        "hezron",
        "agreed, the gateway is flaky today",
    ),
)


def decision_candidate_of(example: DecisionExample, index: int = 0) -> AskCandidate:
    """The candidate an extractor is given, through the real filter.

    Raises `LookupError` when the filter drops the message, which the caller
    counts as predicting nothing -- a decision never read is never recorded.
    """
    at = T0 + timedelta(hours=index)
    messages = [
        _message(2000 + index * 10 + offset, author, content, at - timedelta(minutes=5 - offset))
        for offset, (author, content) in enumerate(example.context)
    ]
    source = _message(2009 + index * 10, example.author, example.content, at)
    found = CandidateFilter().candidates([*messages, source])
    match = [c for c in found if c.message.platform_message_id == source.platform_message_id]
    if not match:
        raise LookupError(example.example_id)
    return match[0]


def evaluate_decisions(
    predictions: Mapping[str, Sequence[ExtractedDecision]],
    examples: Sequence[DecisionExample] = DECISION_EXAMPLES,
    policy: DecisionPolicy | None = None,
) -> Metrics:
    """Score predicted decisions against the labels.

    As for asks, only presentable decisions count, and a message that decides
    something counts once however many decisions were reported for it: the
    label is "this message concludes a choice", not how the model split it.
    """
    settings = policy or DecisionPolicy()
    tp = fp = fn = 0
    for example in examples:
        found = [
            d
            for d in predictions.get(example.example_id, ())
            if settings.presentable(d.confidence)
        ]
        if not example.decides:
            fp += len(found)
        elif found:
            tp += 1
        else:
            fn += 1
    return Metrics(tp, fp, fn)


@dataclass(frozen=True, slots=True)
class ExtractionCost:
    """The standing spend, expressed per 10k messages.

    Extraction is proportional to traffic rather than to usage, so this is what
    a busy server pays whether or not anybody asks a question. Populate it from
    a measured run; the defaults are placeholders and are not a claim.
    """

    candidate_rate: float
    prompt_tokens: float
    completion_tokens: float
    prompt_price_per_million: float
    completion_price_per_million: float

    @property
    def calls_per_10k(self) -> float:
        return 10_000 * self.candidate_rate

    @property
    def usd_per_10k_messages(self) -> float:
        calls = self.calls_per_10k
        return (
            calls * self.prompt_tokens * self.prompt_price_per_million / 1_000_000
            + calls
            * self.completion_tokens
            * self.completion_price_per_million
            / 1_000_000
        )
