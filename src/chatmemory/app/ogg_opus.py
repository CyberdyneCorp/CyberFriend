"""How long an Ogg Opus recording really is, counted from its packets.

A voice message's `duration_secs` is written by the client that uploaded it,
so it bounds nothing. What a decoder plays is the sum of the frames in every
Opus packet, and each packet states its own frame size and count in its first
byte (RFC 6716, section 3.1). So the length is counted here rather than read
from a header: an Ogg page's granule position is as much the sender's claim as
`duration_secs` is.

The parse is strict, and anything it does not recognise is None -- refused,
never guessed at: bytes outside a page, a second logical stream, a first
packet that is not `OpusHead`. Where a packet is ambiguous it is counted as
long as an Opus packet can be, so the count can only err towards more.
"""

from __future__ import annotations

from dataclasses import dataclass

SAMPLE_RATE = 48_000
"""Opus timestamps are always in 48 kHz samples, whatever the input rate."""

_PAGE_HEADER = 27
_KEPT = 16
"""How much of each packet is kept: the headers and the TOC bytes are all that
is read, and a 10 MB packet must not cost 10 MB per lacing segment to join."""

_SILK_FRAMES = (480, 960, 1920, 2880)
_HYBRID_FRAMES = (480, 960)
_CELT_FRAMES = (120, 240, 480, 960)
_LONGEST_PACKET = 5760
"""120 ms, the most one Opus packet can hold."""


class _Malformed(Exception):
    """Not a single, whole Ogg stream."""


@dataclass(frozen=True, slots=True)
class _Page:
    serial: int
    lacing: bytes
    body: int
    """Where the page's body starts in the data."""


def _page(data: bytes, offset: int) -> _Page:
    header_end = offset + _PAGE_HEADER
    if data[offset : offset + 4] != b"OggS" or len(data) < header_end:
        raise _Malformed
    count = data[header_end - 1]
    lacing = data[header_end : header_end + count]
    if len(lacing) != count:
        raise _Malformed
    return _Page(
        int.from_bytes(data[offset + 14 : offset + 18], "little"), lacing, header_end + count
    )


def _packet_heads(data: bytes) -> list[bytes]:
    """The first bytes of every packet, in order, across page boundaries."""
    heads: list[bytes] = []
    pending = b""
    open_packet = False
    serials: set[int] = set()
    offset = 0
    while offset < len(data):
        page = _page(data, offset)
        serials.add(page.serial)
        offset = page.body + sum(page.lacing)
        if len(serials) > 1 or offset > len(data):
            raise _Malformed
        position = page.body
        for size in page.lacing:
            pending += data[position : position + min(size, _KEPT - len(pending))]
            position += size
            open_packet = size == 255
            if not open_packet:
                heads.append(pending)
                pending = b""
    if open_packet:
        # A packet the stream never finished still reaches the decoder.
        heads.append(pending)
    return heads


def _samples(head: bytes) -> int:
    """What one packet decodes to, from its TOC byte."""
    if not head:
        return _LONGEST_PACKET
    config, code = head[0] >> 3, head[0] & 3
    if config < 12:
        frame = _SILK_FRAMES[config % 4]
    elif config < 16:
        frame = _HYBRID_FRAMES[config % 2]
    else:
        frame = _CELT_FRAMES[config % 4]
    if code == 0:
        return frame
    if code < 3:
        return 2 * frame
    return (head[1] & 0x3F) * frame if len(head) > 1 else _LONGEST_PACKET


def opus_seconds(data: bytes) -> float | None:
    """The seconds of audio in an Ogg Opus stream, or None if it is not one."""
    try:
        heads = _packet_heads(data)
    except _Malformed:
        return None
    if len(heads) < 2 or not heads[0].startswith(b"OpusHead"):
        return None
    if not heads[1].startswith(b"OpusTags"):
        return None
    return sum(_samples(head) for head in heads[2:]) / SAMPLE_RATE
