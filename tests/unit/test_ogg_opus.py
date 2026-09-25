"""Counting an Ogg Opus stream's real length, which the voice caps rest on.

The declared duration is the sender's word; this count is the bot's. So each
test is a way the count could be made to come out short, and checks it errs
long or refuses instead.
"""

from __future__ import annotations

import pytest

from chatmemory.app.ogg_opus import opus_seconds
from tests.audio import OPUS_HEAD, OPUS_TAGS, TOC_120MS, ogg_opus, ogg_page, raw_page


def test_a_stream_is_as_long_as_its_packets() -> None:
    assert opus_seconds(ogg_opus(7.2)) == pytest.approx(7.2)
    assert opus_seconds(ogg_opus(600, packet=TOC_120MS)) == pytest.approx(600)


@pytest.mark.parametrize(
    ("toc", "samples"),
    [
        (bytes([0x00]), 480),  # SILK 10 ms
        (bytes([0x18]), 2880),  # SILK 60 ms
        (bytes([0x60]), 480),  # hybrid 10 ms
        (bytes([0x78]), 960),  # hybrid 20 ms
        (bytes([0x80]), 120),  # CELT 2.5 ms
        (bytes([0xF9]), 1920),  # CELT 20 ms, two frames
        (bytes([0xFA]), 1920),  # CELT 20 ms, two frames of different sizes
        (bytes([0xFB, 0x03]), 2880),  # CELT 20 ms, code 3 with three frames
        (bytes([0xFB]), 5760),  # code 3 missing its count: the longest packet
        (b"", 5760),  # an empty packet is concealed by the decoder: the longest too
    ],
)
def test_each_packet_counts_what_its_toc_byte_says(toc: bytes, samples: int) -> None:
    stream = ogg_page([OPUS_HEAD]) + ogg_page([OPUS_TAGS]) + ogg_page([toc])

    assert opus_seconds(stream) == pytest.approx(samples / 48_000)


def test_a_packet_spanning_pages_is_counted_once() -> None:
    big = bytes([0xF8]) + bytes(600)
    stream = (
        ogg_page([OPUS_HEAD])
        + ogg_page([OPUS_TAGS])
        # 601 bytes: 255 + 255 on one page, the last 91 on the next.
        + raw_page(bytes([255, 255]), big[:510])
        + raw_page(bytes([91]), big[510:])
    )

    assert opus_seconds(stream) == pytest.approx(0.02)


def test_a_packet_the_stream_never_finishes_is_still_counted() -> None:
    stream = (
        ogg_page([OPUS_HEAD])
        + ogg_page([OPUS_TAGS])
        + raw_page(bytes([255]), bytes([0xF8]) + bytes(254))
    )

    assert opus_seconds(stream) == pytest.approx(0.02)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"%PDF-1.7",
        b"OggS\x00\x02" + bytes(64),
        ogg_opus(3.0) + b"trailing bytes a decoder might resync into",
        b"junk" + ogg_opus(3.0),
        ogg_page([b"OpusHeaX" + bytes(11)]) + ogg_page([OPUS_TAGS]) + ogg_page([bytes([0xF8])]),
        ogg_page([OPUS_HEAD]) + ogg_page([b"Vorbis"]),
        ogg_page([OPUS_HEAD]) + ogg_page([OPUS_TAGS]) + ogg_page([bytes([0xF8])], serial=2),
        ogg_opus(3.0)[:-1],
    ],
    ids=[
        "empty",
        "not ogg",
        "ogg magic only",
        "trailing bytes",
        "leading bytes",
        "not opus",
        "no tags",
        "second stream",
        "truncated page",
    ],
)
def test_anything_but_one_whole_opus_stream_is_refused(data: bytes) -> None:
    assert opus_seconds(data) is None
