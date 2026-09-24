"""What counts as a chain address. Vocabulary, so both layers can agree.

In `domain` rather than beside the adapter because two layers need the same
answer and must not each keep their own: routing decides whether a question is
about a wallet, and the adapter decides whether a cleared argument may be sent.
Two regexes drifting apart would mean a question routed to the tool whose
argument the tool then refuses, which reads as the feature silently not
working.

Why refused rather than repaired: the egress guard clears a wallet lookup by
rooting, so the argument survives only if the asker typed it. That is the rule
that matters, and it is enforced elsewhere. This is the gate immediately after
it, and it exists because rooting admits *any* word the asker wrote --
"balance", "base", their own name. Sending those to an RPC endpoint would be a
request per stray word.

Refused, never trimmed. The web path retries a not-rooted query with the
foreign words removed, which is right for prose and catastrophic here: trimming
an address down to whichever part of it was valid hex produces a different,
valid-looking address belonging to somebody else.
"""

from __future__ import annotations

import re

ADDRESS = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
"""A 20-byte hex address, anchored at both ends.

Anchored because `re.match` alone would admit `0x…40hex` followed by anything,
and the suffix would travel in the request.
"""


def is_address(text: str) -> bool:
    return bool(ADDRESS.match(text.strip()))


def normalise(text: str) -> str:
    """The form sent to an endpoint: lowercase, whitespace stripped.

    EIP-55 mixes case as a checksum, and rooting compares case-folded, so a
    model may echo an address in different case than the asker typed it. Both
    are the same address to a node, and lowercase is the form that cannot be a
    *wrong* checksum.
    """
    return text.strip().lower()


def find_addresses(text: str) -> tuple[str, ...]:
    """Every address in `text`, in order, without duplicates.

    Used to read the asker's own question. Deliberately not used on retrieved
    content: an address found there is precisely the one that must not be
    looked up.
    """
    seen: dict[str, None] = {}
    for match in re.finditer(r"0x[0-9a-fA-F]{40}", text):
        seen.setdefault(normalise(match.group()), None)
    return tuple(seen)


# "…45e0", "...45e0", or a bare "45e0": how somebody names one of their saved
# wallets without typing it. A bare token must hold a digit, so "cafe" or
# "beef" in a sentence is never read as the end of an address.
_SUFFIX = re.compile(r"(…|\.{2,3})?\b([0-9a-fA-F]{4,8})\b")
SUFFIX_CHARS = 4
"""How many trailing characters name a saved wallet in a reply."""


def named_by_suffix(text: str, saved: tuple[str, ...]) -> tuple[str, ...]:
    """The saved addresses `text` names by their last characters.

    Only ever matched against the asker's own saved wallets, so a suffix can
    select among them and never introduces an address.
    """
    tokens = {
        match.group(2).lower()
        for match in _SUFFIX.finditer(text)
        if match.group(1) or any(c.isdigit() for c in match.group(2))
    }
    return tuple(w for w in saved if any(w.lower().endswith(t) for t in tokens))


def suffix_list(saved: tuple[str, ...]) -> str:
    """`…45e0`, `…1a2b`: the saved wallets as they are offered to choose from."""
    return ", ".join(f"`…{w[-SUFFIX_CHARS:]}`" for w in saved)
