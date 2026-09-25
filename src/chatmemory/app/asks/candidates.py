"""Which messages are worth paying a model to read.

Extraction is a standing cost proportional to server traffic rather than to
usage, so it runs only over messages carrying a *plausible addressee*: someone
was mentioned, someone was replied to, the bot was spoken to directly, the
message addresses a person in the second person, or the author is promising
something themselves. Most channel traffic is not an obligation and should not
be paid for as though it might be.

The first-person signal is not in the original four, and is here because a
commitment is addressed to the person making it: "i'll push the fix tonight"
mentions nobody, replies to nobody, and is exactly the kind of obligation
"what do I need to do today" is asked about.

The decision signal is not about an addressee at all. The same model call
reads a message for the decision it concludes, and a conclusion is usually
addressed to nobody -- "fechou, vamos com Postgres" mentions no one and replies
to no one. It is bilingual because the rest of this filter is English-only, and
a Portuguese decision would otherwise never be read.

This is a cost filter, not a judgement about whether an ask is present. It is
deliberately generous within its signals and silent outside them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from chatmemory.app.asks.model import AddresseeSignal, AskCandidate
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

# Whole words only: "your" must match, "yourselves" should too, but "youth"
# and "ubuntu" must not. The `u`/`ur` forms are common enough in chat that
# omitting them loses real asks.
SECOND_PERSON = re.compile(
    r"\b(you|your|yours|you're|youre|you'd|you'll|u|ur|y'all|yall)\b",
    re.IGNORECASE,
)

# Address to a room rather than a person. These make a message a candidate,
# and resolution later records the result as group-directed rather than
# assigning it to whoever happens to be nearby.
GROUP_ADDRESS = re.compile(
    r"\b(everyone|everybody|anyone|anybody|someone|somebody|folks|team|all)\b",
    re.IGNORECASE,
)

# A promise, in the forms people actually write. Narrow on purpose: widening
# this to every sentence starting with "I" would put most of a channel's
# traffic through the extractor for almost no additional obligations.
FIRST_PERSON_COMMITMENT = re.compile(
    r"\b(i'?ll|i will|i'?m going to|i'?m gonna|let me|on it|i got (this|it)|i'?ve got (this|it))\b",
    re.IGNORECASE,
)

# A conclusion, in the forms people actually write it, in English and in
# Brazilian Portuguese. Generous on purpose: "bora" is as often an invitation as
# a conclusion, and the model is what tells them apart. What this has to avoid
# is missing the ways a group says it has settled something.
#
# Generous is not the same as indiscriminate: every match is a model call. Left
# out are the forms that are mostly something else and add no conclusion the
# rest do not catch -- "fechada" is how a shop or a position is described,
# "vamos de" is how people travel ("vamos de carro"), and "going with" is
# company ("going with my family"), while "bora de", "let's go with" and
# "we'll go with" still match.
DECISION_MARKERS = re.compile(
    r"\b("
    r"we(?: have|'ve)? decided|decided to|decision is|final call|it'?s settled|"
    r"settled on|agreed|let'?s go with|lets go with|we'?ll go with|"
    r"decidimos|decidido|decidida|fechou|fechado|bora|combinado|combinada|"
    r"vamos com|vamos seguir com|vamos manter|fica assim|ficou assim|"
    r"ficou definido|está definido|tá definido|ta definido"
    r")\b",
    re.IGNORECASE,
)

#: How much preceding conversation the model is shown. Enough to tell a
#: follow-up from an opener; short enough that the cost stays flat.
CONTEXT_MESSAGES = 6


class CandidateFilter:
    """Selects messages with a plausible addressee."""

    def __init__(
        self,
        bots: frozenset[PersonRef] = frozenset(),
        dm_channels: frozenset[ChannelRef] = frozenset(),
        context_messages: int = CONTEXT_MESSAGES,
    ) -> None:
        self._bots = bots
        self._dm_channels = dm_channels
        self._context = context_messages

    def candidates(
        self,
        messages: Sequence[Message],
        parents: Mapping[int, Message] | None = None,
        preceding: Mapping[int, Sequence[Message]] | None = None,
    ) -> list[AskCandidate]:
        """Candidates from one window's messages, oldest first.

        `preceding` is the conversation before a message as the corpus holds
        it, oldest first, and replaces the batch's for the messages it names.
        The batch is only the conversation when it is contiguous: the backlog
        pass reads back an edited message on its own, and a conclusion shown
        without the proposal it settled no longer says what was chosen.
        """
        by_id = {m.platform_message_id: m for m in messages}
        lookup: dict[int, Message] = {**(parents or {}), **by_id}
        before = preceding or {}

        found: list[AskCandidate] = []
        for index, message in enumerate(messages):
            signal = self.signal(message)
            if signal is None:
                continue
            parent = (
                lookup.get(message.reply_to_id) if message.reply_to_id is not None else None
            )
            context = before.get(message.platform_message_id)
            if context is None:
                context = messages[max(0, index - self._context) : index]
            found.append(
                AskCandidate(
                    message=message,
                    signal=signal,
                    context=tuple(context[-self._context :]) if self._context else (),
                    reply_parent=parent,
                )
            )
        return found

    def signal(self, message: Message) -> AddresseeSignal | None:
        """The strongest addressee signal on a message, or None to skip it."""
        if not self._is_extractable(message):
            return None
        if message.mentions:
            return AddresseeSignal.MENTION
        if message.reply_to_id is not None:
            return AddresseeSignal.REPLY
        if message.channel in self._dm_channels:
            # A message sent to the bot is addressed to it by construction.
            return AddresseeSignal.BOT_DM
        text = message.content
        # Before the second-person fallback: "we decided you take the deploy"
        # is read either way, and a decision is the rarer, more specific signal.
        if DECISION_MARKERS.search(text):
            return AddresseeSignal.DECISION
        if SECOND_PERSON.search(text) or GROUP_ADDRESS.search(text):
            return AddresseeSignal.SECOND_PERSON
        if FIRST_PERSON_COMMITMENT.search(text):
            return AddresseeSignal.FIRST_PERSON
        return None

    def _is_extractable(self, message: Message) -> bool:
        """Bots, deletions, joins and system notices carry no obligations.

        Joins and system notices are recognised by their empty body rather than
        by a message type: the corpus stores what people wrote, and a message
        whose whole content is platform-generated ceremony arrives here with
        nothing in it. A bot's own text is excluded for the same reason it is
        never ingested -- it is our output, not somebody's request.
        """
        if not message.is_visible:
            return False
        if message.author in self._bots:
            return False
        return bool(message.content.strip())
