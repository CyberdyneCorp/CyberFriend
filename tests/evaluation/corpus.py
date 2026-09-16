"""The seeded server: four channels, four memberships, fifteen conversations.

Shaped to look like somewhere people actually talk, because a corpus of
unrelated one-liners makes every retriever look good. In particular:

* Conversations run for several turns, so the retrieval unit under test is a
  real window rather than a single sentence indexed on its own.
* The questions in `questions.py` are worded to *paraphrase* the corpus. Where
  a question shares a stem with the corpus it is by accident, and the vector
  questions are checked at run time to confirm the lexical leg cannot answer
  them -- otherwise the set drifts into a keyword test that passes with the
  embeddings switched off.
* Two pairs of conversations are deliberately confusable: a payment timeout and
  a search-latency timeout in the same channel, and a confirmed relocation in
  `#leadership` against unconfirmed floor-swap gossip in `#general`. Each is
  the other's `forbidden` set.
* Every channel except `#general` is readable by some viewers and not others,
  so the ACL sweep has something to catch.

Timing matters to the shape of the index. `WindowBuilder` splits on a fifteen
minute gap, so conversations are two hours apart and their turns two minutes
apart: each conversation becomes exactly one window, which is what makes a
message id a stable thing for a judgement to name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

PLATFORM = "discord"
GUILD = 77

GENERAL, ENGINEERING, LEADERSHIP, DESIGN = 100, 200, 300, 400
INDEXED = frozenset({GENERAL, ENGINEERING, LEADERSHIP, DESIGN})

LEAD, STAFF, DESIGNER, NEWCOMER = 11, 13, 15, 17

DISPLAY = {
    LEAD: "priya",
    STAFF: "tom",
    DESIGNER: "ines",
    NEWCOMER: "sam",
}

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Seed:
    """One message as the corpus declares it.

    The corpus is the ground truth for `message -> channel`, and the ACL check
    reads it from here rather than from the database. A permission check that
    verifies itself against the store it is auditing proves only that the
    store is self-consistent.
    """

    message_id: int
    channel_id: int
    author_id: int
    text: str


@dataclass(frozen=True, slots=True)
class Conversation:
    """A run of turns that becomes one window."""

    key: str
    channel_id: int
    hour: int
    turns: tuple[tuple[int, int, str], ...]  # (message id, author id, text)

    @property
    def seeds(self) -> tuple[Seed, ...]:
        return tuple(
            Seed(message_id=mid, channel_id=self.channel_id, author_id=author, text=text)
            for mid, author, text in self.turns
        )

    def messages(self) -> tuple[Message, ...]:
        started = T0 + timedelta(hours=self.hour)
        return tuple(
            Message(
                platform_message_id=seed.message_id,
                channel=ChannelRef(PLATFORM, seed.channel_id),
                author=PersonRef(PLATFORM, seed.author_id),
                author_display=DISPLAY[seed.author_id],
                content=seed.text,
                created_at=started + timedelta(minutes=2 * position),
            )
            for position, seed in enumerate(self.seeds)
        )


CONVERSATIONS: tuple[Conversation, ...] = (
    # --- #general: readable by everyone ---------------------------------
    Conversation(
        key="coffee",
        channel_id=GENERAL,
        hour=0,
        turns=(
            (100101, LEAD, "the espresso machine in the kitchen packed up again this morning"),
            (100102, STAFF, "it made a grinding noise and then went completely dead"),
            (100103, STAFF, "i rang the vendor, their engineer is booked for thursday afternoon"),
            (100104, DESIGNER, "there is instant in the cupboard above the sink until then"),
            (100105, LEAD, "i will tape a note to the front of it"),
        ),
    ),
    Conversation(
        key="parking",
        channel_id=GENERAL,
        hour=2,
        turns=(
            (100201, STAFF, "the barrier at the car park entrance is jammed open"),
            (100202, LEAD, "facilities know, they are leaving it open until the part arrives"),
            (100203, DESIGNER, "so we do not need to badge in to drive down?"),
            (100204, LEAD, "not this week, just drive straight through"),
        ),
    ),
    Conversation(
        key="beagle",
        channel_id=GENERAL,
        hour=4,
        turns=(
            (100301, DESIGNER, "my neighbour's beagle starts howling around three every night"),
            (100302, STAFF, "a white noise machine genuinely changed my life, worth the forty "
                            "quid"),
            (100303, DESIGNER, "ordering one tonight then"),
        ),
    ),
    # The readable half of the relocation pair. Unconfirmed, and says so.
    Conversation(
        key="floor-rumour",
        channel_id=GENERAL,
        hour=6,
        turns=(
            (100401, STAFF, "someone told me we might swap floors inside this building next year"),
            (100402, DESIGNER, "i heard the same thing but nobody senior has confirmed it"),
            (100403, STAFF, "treat it as gossip until there is an email about it"),
        ),
    ),
    Conversation(
        key="party",
        channel_id=GENERAL,
        hour=8,
        turns=(
            (100501, LEAD, "the december party is booked at the same place as last year"),
            (100502, STAFF, "the food was better than the venue if i remember rightly"),
            (100503, DESIGNER, "i volunteer to make the playlist"),
        ),
    ),
    Conversation(
        key="charger",
        channel_id=GENERAL,
        hour=10,
        turns=(
            (100601, DESIGNER, "did anyone pick up a usb c charger from meeting room two"),
            (100602, STAFF, "there is a pile of abandoned cables in the drawer by the printer"),
            (100603, DESIGNER, "found it, thank you"),
        ),
    ),
    Conversation(
        key="sandwiches",
        channel_id=GENERAL,
        hour=12,
        turns=(
            (100701, STAFF, "the new sandwich place on the corner does a decent falafel wrap"),
            (100702, DESIGNER, "queue is enormous after half twelve though"),
            (100703, LEAD, "go at quarter past and it is empty"),
        ),
    ),
    Conversation(
        key="fire-drill",
        channel_id=GENERAL,
        hour=14,
        turns=(
            (100801, LEAD, "there is an alarm test on tuesday morning, nobody needs to evacuate"),
            (100802, STAFF, "will it be the full siren or just the bell"),
            (100803, LEAD, "full siren, about thirty seconds"),
        ),
    ),
    Conversation(
        key="printer",
        channel_id=GENERAL,
        hour=16,
        turns=(
            (100901, STAFF, "the printer on the second floor chews every third sheet"),
            (100902, DESIGNER, "it is the bottom tray, use the top one"),
            (100903, STAFF, "that worked, thanks"),
        ),
    ),
    Conversation(
        key="aircon",
        channel_id=GENERAL,
        hour=18,
        turns=(
            (101001, DESIGNER, "meeting room four is absolutely arctic again"),
            (101002, STAFF, "the thermostat is locked, facilities have the code"),
            (101003, LEAD, "i have asked them to set it two degrees warmer"),
        ),
    ),
    Conversation(
        key="lurgy",
        channel_id=GENERAL,
        hour=20,
        turns=(
            (101101, STAFF, "half my household has come down with something, i will work from "
                            "home"),
            (101102, LEAD, "no problem, rest up"),
            (101103, DESIGNER, "same here, it is going round"),
        ),
    ),
    Conversation(
        key="train-strike",
        channel_id=GENERAL,
        hour=22,
        turns=(
            (101201, LEAD, "there is industrial action on the line this friday"),
            (101202, STAFF, "replacement buses are running but they add an hour each way"),
            (101203, LEAD, "work from home on friday unless you have something in person"),
        ),
    ),
    Conversation(
        key="plants",
        channel_id=GENERAL,
        hour=24,
        turns=(
            (101301, DESIGNER, "the fig by the window is looking extremely sorry for itself"),
            (101302, STAFF, "there is a watering rota pinned by the sink, nobody has signed it"),
            (101303, DESIGNER, "i will take wednesdays"),
        ),
    ),
    Conversation(
        key="visitor-tablet",
        channel_id=GENERAL,
        hour=26,
        turns=(
            (101401, LEAD, "reception has a tablet for signing guests in now, the paper book is "
                           "gone"),
            (101402, STAFF, "does it print a badge"),
            (101403, LEAD, "yes, sticker comes out underneath"),
        ),
    ),
    Conversation(
        key="recycling",
        channel_id=GENERAL,
        hour=28,
        turns=(
            (101501, STAFF, "where are we supposed to put the used coffee pods"),
            (101502, DESIGNER, "small bin by the dishwasher, not the general waste"),
            (101503, STAFF, "noted"),
        ),
    ),
    Conversation(
        key="scaffolding",
        channel_id=GENERAL,
        hour=30,
        turns=(
            (101601, LEAD, "there will be scaffolding up along the south face for a fortnight"),
            (101602, STAFF, "so the blinds stay down on that side"),
            (101603, LEAD, "afraid so"),
        ),
    ),
    Conversation(
        key="book-club",
        channel_id=GENERAL,
        hour=32,
        turns=(
            (101701, DESIGNER, "book group picked the one about the lighthouse keeper"),
            (101702, STAFF, "how long is it"),
            (101703, DESIGNER, "three hundred pages, we meet in a month"),
        ),
    ),
    Conversation(
        key="desk-riser",
        channel_id=GENERAL,
        hour=34,
        turns=(
            (101801, STAFF, "does anyone have a spare desk riser they are not using"),
            (101802, LEAD, "there are two in the storeroom, ask facilities to unlock it"),
            (101803, STAFF, "got one"),
        ),
    ),
    Conversation(
        key="first-aid",
        channel_id=GENERAL,
        hour=36,
        turns=(
            (101901, LEAD, "we are short of trained first aiders, the course is a single day"),
            (101902, DESIGNER, "put me down for it"),
            (101903, STAFF, "and me"),
        ),
    ),
    Conversation(
        key="post-room",
        channel_id=GENERAL,
        hour=38,
        turns=(
            (102001, STAFF, "parcels are stacking up behind reception again"),
            (102002, LEAD, "collect yours today please, the courier will not take them back"),
            (102003, DESIGNER, "mine is the enormous flat one"),
        ),
    ),
    Conversation(
        key="charity-run",
        channel_id=GENERAL,
        hour=40,
        turns=(
            (102101, DESIGNER, "we have six people signed up for the ten kilometre in october"),
            (102102, STAFF, "is there a team name yet"),
            (102103, DESIGNER, "not yet, suggestions welcome"),
        ),
    ),
    Conversation(
        key="keyboard-noise",
        channel_id=GENERAL,
        hour=42,
        turns=(
            (102201, STAFF, "whoever has the very clicky keyboard, we can hear you from the far "
                            "wall"),
            (102202, DESIGNER, "guilty, i have ordered quieter switches"),
            (102203, LEAD, "much appreciated"),
        ),
    ),
    Conversation(
        key="cake",
        channel_id=GENERAL,
        hour=44,
        turns=(
            (102301, LEAD, "there is cake by reception at three, help yourselves"),
            (102302, DESIGNER, "what is the occasion"),
            (102303, LEAD, "someone's five year anniversary"),
        ),
    ),
    Conversation(
        key="season-ticket",
        channel_id=GENERAL,
        hour=46,
        turns=(
            (102401, STAFF, "is the season ticket loan still a thing"),
            (102402, LEAD, "yes, interest free over twelve months, ask finance for the form"),
            (102403, STAFF, "brilliant"),
        ),
    ),
    # --- #engineering: the eng role -------------------------------------
    Conversation(
        key="checkout-timeout",
        channel_id=ENGINEERING,
        hour=48,
        turns=(
            (200101, STAFF, "checkout is throwing 500s on the payment step for one order in "
                            "twenty"),
            (200102, STAFF, "the log line is ERR_PSP_TIMEOUT_4471 repeated a few hundred times"),
            (200103, LEAD, "that is the payment provider handshake giving up at ten seconds"),
            (200104, STAFF, "raising the client timeout to thirty seconds cleared it in staging"),
            (200105, LEAD, "ship it behind the flag and watch the failure rate for an hour"),
            (200106, STAFF, "deployed, failures are back to baseline"),
        ),
    ),
    Conversation(
        key="index-lock",
        channel_id=ENGINEERING,
        hour=50,
        turns=(
            (200201, LEAD, "the index build locked the orders table for eleven minutes last night"),
            (200202, STAFF, "we ran it inside a single transaction, that is what held the lock"),
            (200203, LEAD, "next time use CONCURRENTLY and keep it outside a transaction"),
            (200204, STAFF, "i will rewrite the 2026_08_14_orders_idx migration before the next "
                            "slot"),
            (200205, LEAD, "and rehearse it against a restored snapshot first"),
        ),
    ),
    # The confusable twin of checkout-timeout: same channel, the same
    # vocabulary of slowness and caches, an entirely different incident.
    Conversation(
        key="search-latency",
        channel_id=ENGINEERING,
        hour=52,
        turns=(
            (200301, STAFF, "the results page sits at four seconds at the ninety-ninth percentile"),
            (200302, LEAD, "it is the fan-out to the recommendation service, which has no cache"),
            (200303, STAFF, "a sixty second cache brought it down to four hundred milliseconds"),
            (200304, LEAD, "leave the cache in and we will revisit the fan-out next quarter"),
        ),
    ),
    Conversation(
        key="flaky-test",
        channel_id=ENGINEERING,
        hour=54,
        turns=(
            (200401, STAFF, "the nightly suite fails one run in five on "
                            "test_window_rebuild_is_idempotent"),
            (200402, LEAD, "it is a clock assumption, the fixture calls now twice and compares"),
            (200403, STAFF, "pinned the clock in the fixture, twenty green runs in a row since"),
        ),
    ),
    Conversation(
        key="ci-queue",
        channel_id=ENGINEERING,
        hour=56,
        turns=(
            (200501, STAFF, "pull requests are waiting twenty minutes for a runner"),
            (200502, LEAD, "we have four, i can approve two more if it stays like this"),
            (200503, STAFF, "it is worst between ten and noon"),
        ),
    ),
    Conversation(
        key="log-volume",
        channel_id=ENGINEERING,
        hour=58,
        turns=(
            (200601, LEAD, "our log bill has doubled since the tracing change"),
            (200602, STAFF, "we are emitting a line per span at debug in production"),
            (200603, LEAD, "drop production to info and keep debug behind an env var"),
        ),
    ),
    Conversation(
        key="dependency-bump",
        channel_id=ENGINEERING,
        hour=60,
        turns=(
            (200701, STAFF, "the http client major version renames two keyword arguments"),
            (200702, LEAD, "wrap it at our own boundary so the rename touches one file"),
            (200703, STAFF, "already done, the diff is tiny"),
        ),
    ),
    Conversation(
        key="oncall-swap",
        channel_id=ENGINEERING,
        hour=62,
        turns=(
            (200801, DESIGNER, "i cannot cover the week of the twelfth, anyone free to swap"),
            (200802, STAFF, "i will take it if you take the week after"),
            (200803, DESIGNER, "deal"),
        ),
    ),
    Conversation(
        key="stale-flags",
        channel_id=ENGINEERING,
        hour=64,
        turns=(
            (200901, LEAD, "there are nineteen flags in the config and eleven are permanently on"),
            (200902, STAFF, "i will delete the permanent ones and the dead branches with them"),
            (200903, LEAD, "one pull request per flag please, they are easier to revert"),
        ),
    ),
    Conversation(
        key="api-deprecation",
        channel_id=ENGINEERING,
        hour=66,
        turns=(
            (201001, STAFF, "two integrators are still calling the first version of the orders "
                            "endpoint"),
            (201002, LEAD, "give them ninety days and a sunset header, then start returning 410"),
            (201003, STAFF, "i will email both of them today"),
        ),
    ),
    Conversation(
        key="staging-data",
        channel_id=ENGINEERING,
        hour=68,
        turns=(
            (201101, STAFF, "the staging dump is four months old and nothing looks realistic"),
            (201102, LEAD, "refresh it monthly from an anonymised export, never from live"),
            (201103, STAFF, "scripted, it runs on the first of the month"),
        ),
    ),
    Conversation(
        key="third-party-quota",
        channel_id=ENGINEERING,
        hour=70,
        turns=(
            (201201, DESIGNER, "the geocoding service started refusing us around lunchtime"),
            (201202, STAFF, "we are over the daily allowance, the plan gives us ten thousand "
                            "calls"),
            (201203, LEAD, "cache the results, most of them are the same handful of postcodes"),
        ),
    ),
    # --- #leadership: the lead role only --------------------------------
    Conversation(
        key="relocation",
        channel_id=LEADERSHIP,
        hour=72,
        turns=(
            (300101, LEAD, "we are signing for the riverside building, the whole team moves in "
                           "march"),
            (300102, LEAD, "the lease on the current floor ends on the last day of february"),
            (300103, LEAD, "nobody says a word outside this channel until HR has the letter ready"),
            (300104, LEAD, "the fit-out budget came in at ninety thousand"),
        ),
    ),
    Conversation(
        key="headcount",
        channel_id=LEADERSHIP,
        hour=74,
        turns=(
            (300201, LEAD, "we have approval for two more engineers and one designer in the first "
                           "quarter"),
            (300202, LEAD, "the designer requisition is contingent on the brand work landing"),
            (300203, LEAD, "bands go up four percent across the board in january"),
        ),
    ),
    Conversation(
        key="vendor-dispute",
        channel_id=LEADERSHIP,
        hour=76,
        turns=(
            (300301, LEAD, "the analytics vendor has invoiced us twice for the same quarter"),
            (300302, LEAD, "legal say we withhold payment until they issue a credit note"),
            (300303, LEAD, "i have paused the renewal conversation until it is sorted out"),
        ),
    ),
    Conversation(
        key="board-pack",
        channel_id=LEADERSHIP,
        hour=78,
        turns=(
            (300401, LEAD, "the board pack needs to be circulated a week ahead this time"),
            (300402, LEAD, "i will write the narrative if finance produce the figures by friday"),
            (300403, LEAD, "keep it to twelve slides"),
        ),
    ),
    Conversation(
        key="insurance",
        channel_id=LEADERSHIP,
        hour=80,
        turns=(
            (300501, LEAD, "the liability cover renews next month and the premium is up a fifth"),
            (300502, LEAD, "get two competing quotes before we accept"),
            (300503, LEAD, "broker is on it"),
        ),
    ),
    Conversation(
        key="offsite",
        channel_id=LEADERSHIP,
        hour=82,
        turns=(
            (300601, LEAD, "two days somewhere reachable by train, not a hotel with a golf course"),
            (300602, LEAD, "one day of planning and one day of nothing structured at all"),
            (300603, LEAD, "i will shortlist three places"),
        ),
    ),
    Conversation(
        key="audit-timeline",
        channel_id=LEADERSHIP,
        hour=84,
        turns=(
            (300701, LEAD, "the external auditors want the ledger extracts by the end of the "
                           "month"),
            (300702, LEAD, "that is tight, finance are still closing the quarter"),
            (300703, LEAD, "i will ask for a fortnight's grace"),
        ),
    ),
    Conversation(
        key="competitor",
        channel_id=LEADERSHIP,
        hour=86,
        turns=(
            (300801, LEAD, "the smaller of our two competitors has raised a large round"),
            (300802, LEAD, "expect them to undercut us on price for a year"),
            (300803, LEAD, "we compete on the integration work, not the sticker"),
        ),
    ),
    # --- #design: the design role ---------------------------------------
    Conversation(
        key="brand-refresh",
        channel_id=DESIGN,
        hour=88,
        turns=(
            (400101, DESIGNER, "the new wordmark drops the gradient and goes to a single weight"),
            (400102, LEAD, "the gradient never reproduced well in print anyway"),
            (400103, DESIGNER, "swatches are in the shared drive under brand slash 2026"),
            (400104, DESIGNER, "we keep the old logo in the mobile app until the next store "
                               "release"),
        ),
    ),
    Conversation(
        key="contrast",
        channel_id=DESIGN,
        hour=90,
        turns=(
            (400201, DESIGNER, "the primary button fails contrast against the new background"),
            (400202, LEAD, "then the background changes, not the button"),
            (400203, DESIGNER, "i will re-run the audit once the palette is locked"),
        ),
    ),
    Conversation(
        key="illustration",
        channel_id=DESIGN,
        hour=92,
        turns=(
            (400301, DESIGNER, "the spot illustrations on the pricing page feel dated"),
            (400302, LEAD, "commission a set from the same studio we used for the report"),
            (400303, DESIGNER, "i will brief them once the wordmark is final"),
        ),
    ),
    Conversation(
        key="icon-set",
        channel_id=DESIGN,
        hour=94,
        turns=(
            (400401, DESIGNER, "the icons are three different stroke widths depending on who drew "
                               "them"),
            (400402, LEAD, "pick one width and redraw the outliers"),
            (400403, DESIGNER, "two pixels, everything else gets redrawn"),
        ),
    ),
    Conversation(
        key="motion",
        channel_id=DESIGN,
        hour=96,
        turns=(
            (400501, DESIGNER, "transitions are all over the place, some are instant and some "
                               "take a second"),
            (400502, LEAD, "write the durations down as three values and use only those"),
            (400503, DESIGNER, "fast, normal and deliberate, then"),
        ),
    ),
    Conversation(
        key="tokens",
        channel_id=DESIGN,
        hour=98,
        turns=(
            (400601, DESIGNER, "the spacing scale in the handoff file does not match what is in "
                               "the code"),
            (400602, LEAD, "the code wins, regenerate the file from it"),
            (400603, DESIGNER, "agreed, i will export it nightly"),
        ),
    ),
    Conversation(
        key="user-testing",
        channel_id=DESIGN,
        hour=100,
        turns=(
            (400701, DESIGNER, "five sessions on the new onboarding, four people missed the skip "
                               "control"),
            (400702, LEAD, "move it and run five more"),
            (400703, DESIGNER, "booked for next week"),
        ),
    ),
    Conversation(
        key="trade-stand",
        channel_id=DESIGN,
        hour=102,
        turns=(
            (400801, DESIGNER, "the exhibition stand graphics need to go to the printer by the "
                               "ninth"),
            (400802, LEAD, "use the old wordmark, the refresh will not be public by then"),
            (400803, DESIGNER, "understood"),
        ),
    ),
)


SEEDS: tuple[Seed, ...] = tuple(seed for c in CONVERSATIONS for seed in c.seeds)

MESSAGE_CHANNELS: dict[int, int] = {s.message_id: s.channel_id for s in SEEDS}
"""Ground truth for the permission check. Read from the corpus, never the DB."""


def messages() -> tuple[Message, ...]:
    return tuple(m for c in CONVERSATIONS for m in c.messages())


def conversation(key: str) -> Conversation:
    return next(c for c in CONVERSATIONS if c.key == key)


def ids(key: str) -> frozenset[int]:
    """Every message id in a conversation, for a `forbidden` set."""
    return frozenset(mid for mid, _, _ in conversation(key).turns)


# --- who may read what ---------------------------------------------------
#
# Declared here as plain data and asserted against `DiscordAclResolver` in the
# test. Two statements of the same fact, one of which the code under test does
# not produce: if the resolver's answer drifts, the golden set says so rather
# than quietly re-baselining its permission expectations on the new behaviour.

ROLES = {
    LEAD: frozenset({"lead", "eng", "design"}),
    STAFF: frozenset({"eng"}),
    DESIGNER: frozenset({"design"}),
    NEWCOMER: frozenset(),
}

VIEWER_CHANNELS: dict[int, frozenset[int]] = {
    LEAD: frozenset({GENERAL, ENGINEERING, LEADERSHIP, DESIGN}),
    STAFF: frozenset({GENERAL, ENGINEERING}),
    DESIGNER: frozenset({GENERAL, DESIGN}),
    NEWCOMER: frozenset({GENERAL}),
}

VIEWERS: tuple[int, ...] = (LEAD, STAFF, DESIGNER, NEWCOMER)


def readable_by(user_id: int) -> frozenset[int]:
    return VIEWER_CHANNELS[user_id]
