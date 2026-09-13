"""Deciding what a file actually is.

Two rules, and the second is the one that matters:

  1. The format must be on the allowlist.
  2. The format is decided by the file's *content*, and the uploader's claim
     about it must agree.

Trusting the extension means `payload.txt` full of PDF gets handed to the text
path and `invoice.pdf` full of zip gets handed to pypdf. Sniffing alone is not
enough either: a disagreement between claim and content is itself a signal,
and the file is skipped rather than parsed as whichever of the two we happened
to prefer.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from chatmemory.app.documents.model import DocumentFormat, SkipReason
from chatmemory.app.documents.policy import ParsingLimits

# How much of a file we look at to classify it. Enough for magic bytes and a
# representative sample of text, and bounded so classification itself cannot
# be made expensive.
SNIFF_BYTES = 8192

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
XML_PREFIXES = (b"<?xml", b"<!doctype")
BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")

DOCX_MARKER = "word/document.xml"

# Families group the formats that are the *same kind of thing*. A claim and a
# sniff must land in the same family; which text subtype it is (markdown, csv,
# plain) is refined from the name, because those are genuinely the same bytes.
TEXT_FORMATS = frozenset({DocumentFormat.PLAIN_TEXT, DocumentFormat.MARKDOWN, DocumentFormat.CSV})
# XML sits in the text family because it *is* text: an XML document with no
# declaration is indistinguishable from prose that opens with a tag, and
# forcing a decision there would skip valid files. Which of the two a file is
# parsed as is decided by the claim, and either way the bytes are text.
TEXT_FAMILY = TEXT_FORMATS | {DocumentFormat.XML}

_EXTENSION_FORMATS = {
    ".txt": DocumentFormat.PLAIN_TEXT,
    ".text": DocumentFormat.PLAIN_TEXT,
    ".log": DocumentFormat.PLAIN_TEXT,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".csv": DocumentFormat.CSV,
    ".tsv": DocumentFormat.CSV,
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".xml": DocumentFormat.XML,
}

_MEDIA_TYPE_FORMATS = {
    "text/plain": DocumentFormat.PLAIN_TEXT,
    "text/markdown": DocumentFormat.MARKDOWN,
    "text/csv": DocumentFormat.CSV,
    "text/tab-separated-values": DocumentFormat.CSV,
    "application/pdf": DocumentFormat.PDF,
    DocumentFormat.DOCX.value: DocumentFormat.DOCX,
    "application/xml": DocumentFormat.XML,
    "text/xml": DocumentFormat.XML,
}


@dataclass(frozen=True, slots=True)
class SniffResult:
    """What the file is, or why we will not touch it."""

    fmt: DocumentFormat | None = None
    skipped: SkipReason | None = None

    @property
    def ok(self) -> bool:
        return self.fmt is not None


def _family(fmt: DocumentFormat) -> str:
    if fmt in TEXT_FAMILY:
        return "text"
    return fmt.value


def declared_format(filename: str, media_type: str | None) -> DocumentFormat | None:
    """What the uploader claims this is, from its name and declared type.

    The name wins over the declared type: Discord derives the type from the
    name anyway, and a mismatch between the two is not interesting -- the
    mismatch that matters is against the bytes.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    by_extension = _EXTENSION_FORMATS.get(suffix)
    if by_extension is not None:
        return by_extension
    if media_type:
        return _MEDIA_TYPE_FORMATS.get(media_type.split(";")[0].strip().lower())
    return None


def _strip_bom(data: bytes) -> bytes:
    for bom in BOMS:
        if data.startswith(bom):
            return data[len(bom) :]
    return data


def _looks_textual(sample: bytes) -> bool:
    """A cheap binary test: NUL bytes, or too much unprintable content.

    Decoding is the adapter's job; this only has to be confident enough to
    refuse to hand binary junk to a text parser.
    """
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return printable / len(sample) > 0.85


def _zip_format(data: bytes, limits: ParsingLimits) -> SniffResult:
    """Classify a zip container, and refuse one shaped like a bomb.

    The check uses the *declared* uncompressed sizes in the central directory.
    They are attacker-controlled and so cannot be trusted as a guarantee -- the
    extractor enforces the real limit as it reads -- but a file that announces
    a terabyte is not worth opening at all.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)

    if len(infos) > limits.max_archive_entries:
        return SniffResult(skipped=SkipReason.EXTRACTED_TOO_LARGE)

    declared = sum(i.file_size for i in infos)
    if declared > limits.max_extracted_bytes:
        return SniffResult(skipped=SkipReason.EXTRACTED_TOO_LARGE)
    if data and declared / max(len(data), 1) > limits.max_expansion_ratio:
        return SniffResult(skipped=SkipReason.EXTRACTED_TOO_LARGE)
    if any(PurePosixPath(i.filename).parts.count("..") for i in infos):
        return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)

    names = {i.filename for i in infos}
    if DOCX_MARKER in names:
        return SniffResult(fmt=DocumentFormat.DOCX)
    # A zip that is not a document is just an archive, and archives are not on
    # the allowlist. This is where a .zip of anything lands.
    return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)


def sniff(data: bytes, limits: ParsingLimits) -> SniffResult:
    """Classify by content alone. No filename reaches this function."""
    if not data:
        return SniffResult(skipped=SkipReason.EMPTY)

    sample = data[:SNIFF_BYTES]
    if sample.startswith(PDF_MAGIC):
        return SniffResult(fmt=DocumentFormat.PDF)
    if sample.startswith(ZIP_MAGIC):
        return _zip_format(data, limits)

    stripped = _strip_bom(sample).lstrip()
    lowered = stripped[:64].lower()
    if any(lowered.startswith(prefix) for prefix in XML_PREFIXES):
        return SniffResult(fmt=DocumentFormat.XML)
    if _looks_textual(sample):
        return SniffResult(fmt=DocumentFormat.PLAIN_TEXT)
    return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)


def classify(
    data: bytes,
    filename: str,
    media_type: str | None,
    allowed: frozenset[DocumentFormat],
    limits: ParsingLimits,
) -> SniffResult:
    """Decide whether to parse this file, and as what.

    Both the claim and the content must be allowlisted, and they must agree.
    The returned format is the sniffed one, refined to a text subtype from the
    filename where the bytes genuinely cannot distinguish them.
    """
    if len(data) > limits.max_input_bytes:
        return SniffResult(skipped=SkipReason.TOO_LARGE)

    claimed = declared_format(filename, media_type)
    if claimed is None or claimed not in allowed:
        return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)

    sniffed = sniff(data, limits)
    if sniffed.fmt is None:
        return sniffed

    if _family(sniffed.fmt) != _family(claimed):
        return SniffResult(skipped=SkipReason.FORMAT_MISMATCH)

    resolved = claimed if sniffed.fmt in TEXT_FAMILY else sniffed.fmt
    if resolved not in allowed:
        return SniffResult(skipped=SkipReason.NOT_ALLOWLISTED)
    return SniffResult(fmt=resolved)
