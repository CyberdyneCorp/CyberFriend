"""Deciding who an ask fell to.

Most obligations carry no mention -- "can you take a look after standup",
"Leo said he'd handle it", a reply with no name in it at all -- so mention
capture alone finds the minority of them. This resolves the rest from reply
structure and from names that map to known people.

Every path here can end in `UNATTRIBUTED`, and that is the point. A wrong
addressee produces a confident obligation for the wrong person, which costs
more than an ask nobody is assigned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Protocol

from chatmemory.app.asks.candidates import GROUP_ADDRESS
from chatmemory.app.asks.model import (
    UNATTRIBUTED,
    Addressee,
    AskCandidate,
    AskKind,
    ExtractedAsk,
    to_group,
    to_person,
)
from chatmemory.domain.identity import PersonRef
from chatmemory.domain.messages import Message

#: Label used when several people were mentioned. Recording the group is the
#: honest answer; picking one of them is the bug this exists to avoid.
MENTIONED_GROUP = "the people mentioned"

_PUNCTUATION = re.compile(r"[^\w' -]+", re.UNICODE)


class PersonDirectory(Protocol):
    """Names to people, for the names that are unambiguous."""

    def resolve(self, name: str) -> PersonRef | None:
        """The person a name refers to, or None if unknown *or ambiguous*.

        Ambiguity must return None rather than a first match: two people called
        "alex" make every ask to either of them a coin flip otherwise.
        """
        ...


class StaticDirectory:
    """A directory built from display names and nicknames.

    Names that resolve to more than one person are dropped entirely rather than
    ranked, because there is no signal here that could rank them honestly.
    """

    def __init__(self, names: Mapping[str, PersonRef] | Iterable[tuple[str, PersonRef]]) -> None:
        pairs = names.items() if isinstance(names, Mapping) else names
        index: dict[str, set[PersonRef]] = {}
        for raw, person in pairs:
            key = normalise_name(raw)
            if key:
                index.setdefault(key, set()).add(person)
        self._index = index

    def resolve(self, name: str) -> PersonRef | None:
        found = self._index.get(normalise_name(name), set())
        return next(iter(found)) if len(found) == 1 else None


def normalise_name(name: str) -> str:
    stripped = _PUNCTUATION.sub(" ", name.replace("@", " ").lower())
    return " ".join(stripped.split())


def resolve_addressee(
    candidate: AskCandidate, extracted: ExtractedAsk, directory: PersonDirectory
) -> Addressee:
    """Who an ask falls to, or `UNATTRIBUTED`.

    Order matters and is not arbitrary: structure the platform recorded beats
    anything a model inferred, a name that resolves beats the reply chain
    (an ask in a reply may still name someone else), and a group beats any
    individual member of it.
    """
    message = candidate.message
    author = message.author

    if extracted.kind is AskKind.COMMITMENT:
        # The speaker promised it, so the speaker owes it -- regardless of who
        # they were talking to.
        return to_person(author)

    hint = _clean_hint(extracted.addressee_hint)
    if hint and (extracted.addressee_is_group or GROUP_ADDRESS.search(hint)):
        return to_group(hint)

    named = directory.resolve(hint) if hint else None
    mentions = frozenset(message.mentions) - {author}

    if len(mentions) == 1:
        return to_person(next(iter(mentions)))
    if len(mentions) > 1:
        # The model may have disambiguated between them; if it did not, record
        # the group rather than choosing a member.
        if named is not None and named in mentions:
            return to_person(named)
        return to_group(MENTIONED_GROUP)

    if named is not None:
        return to_person(named)
    if hint:
        # The ask names somebody we cannot resolve. Every remaining path would
        # attribute it to a *different* person than the one it names, so this
        # is where guessing would happen and where it stops instead.
        return UNATTRIBUTED

    parent = candidate.reply_parent
    if parent is not None and parent.author != author:
        return to_person(parent.author)

    group = GROUP_ADDRESS.search(message.content)
    if group is not None:
        return to_group(group.group(0).lower())

    return UNATTRIBUTED


def _clean_hint(hint: str | None) -> str:
    if not hint:
        return ""
    cleaned = hint.strip().strip("@ ,.:;!?")
    if cleaned.lower() in _NOT_A_NAME:
        return ""
    return cleaned


#: Hints that name nobody.
#:
#: The first group is what models return instead of omitting the field, and
#: treating them as names would send every unattributable ask to whoever is
#: called "unknown". The second is the model restating the addressee as a
#: pronoun -- "you", "the author" -- which carries no information the reply
#: chain does not already carry, so it must fall through to it rather than
#: blocking it as an unresolvable name would.
_NOT_A_NAME = frozenset(
    {
        "",
        "unknown",
        "none",
        "n/a",
        "null",
        "nobody",
        "unattributed",
        "you",
        "u",
        "them",
        "him",
        "her",
        "they",
        "op",
        "the author",
        "the sender",
        "the person above",
        "the recipient",
    }
)


class ObservedDirectory:
    """A directory that learns names from the conversation as it arrives.

    The ingest process has no roster to build a `StaticDirectory` from: it
    holds a gateway connection for messages, not a member list, and the people
    an ask can name are exactly the people who are talking. So names are
    learned from the messages themselves -- `author_display` is what the
    platform said about the author at the time -- and the same ambiguity rule
    applies as everywhere else: a name seen for two different people resolves
    to nobody, permanently, rather than to whoever was seen first.

    Without this, `resolve` is always None and every ask that names somebody in
    prose ("can leo take this") is recorded UNATTRIBUTED. That is the safe
    direction, but it is also most of the asks the feature exists for.
    """

    def __init__(self, max_names: int = 5000) -> None:
        # Bounded because this lives in a long-running process and grows with
        # the number of distinct display names ever seen, which nothing else
        # bounds. Past the cap it stops learning rather than starts forgetting:
        # dropping a known name would make resolution depend on how recently
        # somebody spoke.
        self._max = max_names
        self._index: dict[str, PersonRef] = {}
        self._ambiguous: set[str] = set()

    def observe(self, message: Message) -> None:
        """Learn the author's display name, if it is usable."""
        self.learn(message.author_display, message.author)

    def learn(self, name: str, person: PersonRef) -> None:
        key = normalise_name(name)
        if not key or key in self._ambiguous:
            return
        known = self._index.get(key)
        if known == person:
            return
        if known is not None:
            # Two people, one name. Neither can be resolved from it again.
            del self._index[key]
            self._ambiguous.add(key)
            return
        if len(self._index) < self._max:
            self._index[key] = person

    def resolve(self, name: str) -> PersonRef | None:
        return self._index.get(normalise_name(name))

    def __len__(self) -> int:
        return len(self._index)
