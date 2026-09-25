"""Ogg Opus streams of a chosen length, for the voice-question tests.

Only as real as `chatmemory.app.ogg_opus` reads: valid page structure and
lacing, `OpusHead` and `OpusTags`, and one TOC byte per audio packet. The
payload is silence nobody decodes, and the CRC is left zero.
"""

from __future__ import annotations

OPUS_HEAD = (
    b"OpusHead"
    + bytes([1, 1])
    + (312).to_bytes(2, "little")
    + (48_000).to_bytes(4, "little")
    + bytes(3)
)
OPUS_TAGS = b"OpusTags" + bytes(8)

TOC_20MS = 0xF8
"""CELT, 20 ms, one frame."""
TOC_120MS = bytes([0xFB, 6])
"""CELT, 20 ms frames, code 3 with six of them: the longest packet there is."""


def raw_page(lacing: bytes, body: bytes, *, serial: int = 1, sequence: int = 0) -> bytes:
    """One Ogg page with exactly the segment table given."""
    header = (
        b"OggS"
        + bytes([0, 0])
        + bytes(8)
        + serial.to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + bytes(4)
        + bytes([len(lacing)])
    )
    return header + lacing + body


def ogg_page(packets: list[bytes], *, serial: int = 1, sequence: int = 0) -> bytes:
    """One Ogg page holding whole `packets`."""
    lacing = bytearray()
    for packet in packets:
        lacing += bytes([255] * (len(packet) // 255) + [len(packet) % 255])
    assert len(lacing) <= 255, "too many segments for one page"
    return raw_page(bytes(lacing), b"".join(packets), serial=serial, sequence=sequence)


def ogg_opus(seconds: float, *, packet: bytes = bytes([TOC_20MS, 0, 0])) -> bytes:
    """`seconds` of Opus audio in 20 ms packets, or whatever `packet` holds."""
    per_packet = 0.12 if packet[:1] == TOC_120MS[:1] else 0.02
    audio = [packet] * round(seconds / per_packet)
    pages = [ogg_page([OPUS_HEAD]), ogg_page([OPUS_TAGS], sequence=1)]
    for index in range(0, len(audio), 200):
        pages.append(ogg_page(audio[index : index + 200], sequence=2 + index // 200))
    return b"".join(pages)
