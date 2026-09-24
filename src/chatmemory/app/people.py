"""Matching a typed name against the names people go by.

Pure, so the store only has to say who is visible and this decides who a name
refers to. Accent- and case-insensitive: "Joao" is typed for "João" as often
as not. Two tiers, and the better one wins outright:

1.  the whole name equals a person's name ("João Silva" is João Silva, even
    when a João Silveira exists);
2.  otherwise, the name starts a person's name, word by word, the last typed
    word allowed to be a prefix ("Leo" for "Leonardo Araujo", "João S" for
    "João Silva"), or is one of the words of it ("Silva").

Several matches in the winning tier are returned together; choosing between
them is the asker's job, and guessing is how an answer quotes the wrong João.
"""

from __future__ import annotations

from collections.abc import Iterable

from chatmemory.app.asks.resolution import normalise_name
from chatmemory.app.timespan import fold
from chatmemory.domain.search import PersonCandidate

#: A single letter prefix matches half the server.
MIN_PREFIX = 2


def name_key(name: str) -> str:
    """Folded, punctuation-free and whitespace-collapsed."""
    return normalise_name(fold(name))


def _starts(typed: list[str], words: list[str]) -> bool:
    if not typed or len(typed) > len(words):
        return False
    *whole, last = typed
    return words[: len(whole)] == whole and (
        words[len(whole)] == last
        or (len(last) >= MIN_PREFIX and words[len(whole)].startswith(last))
    )


def _partial(typed: list[str], words: list[str]) -> bool:
    return _starts(typed, words) or (len(typed) == 1 and typed[0] in words)


def matching(
    name: str, people: Iterable[PersonCandidate], limit: int
) -> list[PersonCandidate]:
    """The people `name` refers to, best tier only, at most `limit`, by name."""
    wanted = name_key(name)
    if not wanted:
        return []
    typed = wanted.split()
    keyed = [(name_key(person.display), person) for person in people]
    exact = [person for key, person in keyed if key == wanted]
    found = exact or [person for key, person in keyed if _partial(typed, key.split())]
    return sorted(found, key=lambda p: (p.display.casefold(), p.ref.platform_user_id))[:limit]
