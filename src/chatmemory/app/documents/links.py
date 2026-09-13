"""Finding the external documents a message linked to.

Note the argument type: this takes a `Message`, not a string. That is the
control, not a convenience. A URL becomes fetchable because a person posted it
in a channel we index, and for no other reason -- so document text and tool
results, which are strings, have no way to reach this function, and there is
no other path from content to a fetch.

Imperative text is likewise not a trigger. "Fetch https://example.test/secret"
produces nothing, because the destination is not a configured source; the
sentence around the link is never consulted either way.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from chatmemory.app.documents.policy import DocumentPolicy, ExternalSource
from chatmemory.domain.messages import Message

# Deliberately narrow: http(s) only, and stopping at the characters that
# commonly follow a URL in prose rather than trying to be clever about it.
_URL = re.compile(r"https?://[^\s<>\"'\])}]+", re.IGNORECASE)

_TRAILING = ".,;:!?'\"“”’)]}"


@dataclass(frozen=True, slots=True)
class ExternalLink:
    """A link we are configured to follow, and the message that carried it."""

    url: str
    source: ExternalSource
    message: Message

    @property
    def channel_id(self) -> int:
        return self.message.channel.platform_channel_id


def _candidates(text: str) -> list[str]:
    return [m.group(0).rstrip(_TRAILING) for m in _URL.finditer(text)]


def links_from_message(message: Message, policy: DocumentPolicy) -> list[ExternalLink]:
    """Configured-source links in one message, in order, deduplicated.

    Returns nothing at all when external fetching is disabled, and nothing for
    any destination outside the configured sources. There is no branch here
    that fetches an unmatched URL.
    """
    if not message.is_visible:
        return []
    seen: set[str] = set()
    links: list[ExternalLink] = []
    for url in _candidates(message.content):
        source = policy.external.source_for(url)
        if source is None or url in seen:
            continue
        seen.add(url)
        links.append(ExternalLink(url=url, source=source, message=message))
    return links


def links_from_messages(
    messages: Sequence[Message], policy: DocumentPolicy
) -> list[ExternalLink]:
    return [link for m in messages for link in links_from_message(m, policy)]
