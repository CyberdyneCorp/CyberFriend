"""What counts as an address, and why it is refused rather than repaired.

The egress guard clears a wallet lookup by rooting: the argument survives only
if the asker typed it. That is the rule that matters, and it is enforced
elsewhere. This module is the gate immediately after it, and it exists because
rooting admits *any* word the asker wrote -- "balance", "base", their own name.
Sending those to an RPC endpoint would be a request per stray word.

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
