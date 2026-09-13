"""The hostile-input corpus, and the builders that produce it.

Every file here is one an actual uploader could post: an archive bomb, a PDF
that is only a header, an XML document that asks the parser to fetch a file
off our disk, a file whose name disagrees with its bytes, and a structure
nested deeply enough to blow a recursive walk's stack.

They are built rather than checked in as binaries so it is obvious from the
source what makes each one hostile, and so a reviewer can see that the bomb is
a bomb without opening it.

The self-tests at the bottom check the builders themselves. A corpus that
quietly stopped producing hostile files would make every test that uses it
pass for the wrong reason.
"""

from __future__ import annotations

import io
import zipfile

from chatmemory.app.documents.policy import ChunkingLimits, DocumentPolicy, ParsingLimits

# Small enough that a bomb is cheap to build, large enough to be realistic.
TEST_LIMITS = ParsingLimits(
    max_input_bytes=2 * 1024 * 1024,
    max_extracted_bytes=64 * 1024,
    max_seconds=5.0,
    max_memory_bytes=256 * 1024 * 1024,
    max_nesting_depth=20,
)

TEST_POLICY = DocumentPolicy(
    limits=TEST_LIMITS, chunking=ChunkingLimits(max_tokens=60, overlap_tokens=10)
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --- PDF ---------------------------------------------------------------


def pdf(pages: list[str], metadata: dict[str, str] | None = None) -> bytes:
    """A minimal but genuinely valid PDF, so pypdf really parses it."""
    objects: list[bytes] = []
    font_num = 3 + 2 * len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for index, page in enumerate(pages):
        contents = 4 + 2 * index
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {contents} 0 R /Resources << /Font << /F1 {font_num} 0 R >> >> >>".encode()
        )
        objects.append(_content_stream(page))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    info = None
    if metadata:
        info = len(objects) + 1
        entries = " ".join(f"/{key} ({value})" for key, value in metadata.items())
        objects.append(f"<< {entries} >>".encode())
    return _assemble_pdf(objects, info)


def _content_stream(page: str) -> bytes:
    lines = "\n".join(f"({_escape(line)}) Tj T*" for line in page.split("\n"))
    body = f"BT /F1 12 Tf 72 720 Td 14 TL\n{lines}\nET".encode()
    return b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream"


def _escape(line: str) -> str:
    return line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _assemble_pdf(objects: list[bytes], info: int | None) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objects) + 1} /Root 1 0 R"
    if info is not None:
        trailer += f" /Info {info} 0 R"
    out += b"trailer\n" + (trailer + " >>").encode()
    out += f"\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)


def malformed_pdf() -> bytes:
    """Sniffs as a PDF, is not one. pypdf must fail, not hang or crash us."""
    return b"%PDF-1.4\n" + b"\x01\x02 garbage not an object " * 200


# --- DOCX --------------------------------------------------------------


def docx(
    paragraphs: list[tuple[str, int]],
    footnotes: list[str] | None = None,
    properties: dict[str, str] | None = None,
) -> bytes:
    """A .docx as the bytes Word would write: a zip of XML parts.

    Built by hand rather than with python-docx because the parser under test
    reads the XML itself. That is deliberate: python-docx parses with lxml,
    whose default parser resolves entities, and an uploaded .docx is an
    attacker's XML.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _document_xml(paragraphs))
        if footnotes:
            archive.writestr("word/footnotes.xml", _notes_xml(footnotes))
        if properties:
            archive.writestr("docProps/core.xml", _core_xml(properties))
    return buffer.getvalue()


_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
    'package/2006/content-types"/>'
)


def _paragraph_xml(text: str, heading_level: int) -> str:
    style = (
        f'<w:pPr><w:pStyle w:val="Heading{heading_level}"/></w:pPr>'
        if heading_level
        else ""
    )
    return f"<w:p>{style}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _document_xml(paragraphs: list[tuple[str, int]]) -> str:
    body = "".join(_paragraph_xml(text, level) for text, level in paragraphs)
    return f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'


def _notes_xml(notes: list[str]) -> str:
    body = "".join(_paragraph_xml(note, 0) for note in notes)
    return f'<?xml version="1.0"?><w:footnotes xmlns:w="{W}">{body}</w:footnotes>'


def _core_xml(properties: dict[str, str]) -> str:
    fields = "".join(f"<dc:{key}>{value}</dc:{key}>" for key, value in properties.items())
    return (
        '<?xml version="1.0"?><cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        f'xmlns:dc="http://purl.org/dc/elements/1.1/">{fields}</cp:coreProperties>'
    )


def docx_bomb(payload_bytes: int = 2 * 1024 * 1024) -> bytes:
    """A document-shaped archive that decompresses to far more than it claims.

    Two megabytes of zeros compress to a couple of kilobytes, which is the
    whole trick: the file is small, the extraction is not.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", "0" * payload_bytes)
    return buffer.getvalue()


def zip_of_anything() -> bytes:
    """A plain archive. Not a document, therefore not on the allowlist."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "harmless")
    return buffer.getvalue()


# --- XML ---------------------------------------------------------------


def external_entity_xml(target: str) -> bytes:
    """The classic XXE: a document that asks the parser to read a local file."""
    return (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE report [\n"
        f'  <!ENTITY secret SYSTEM "file://{target}">\n'
        "]>\n"
        "<report><body>&secret;</body></report>"
    ).encode()


def billion_laughs() -> bytes:
    """Entity expansion: small input, unbounded output."""
    return (
        b'<?xml version="1.0"?>\n'
        b"<!DOCTYPE lolz [\n"
        b'  <!ENTITY lol "lol">\n'
        b'  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b'  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
        b'  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        b"]>\n"
        b"<lolz>&lol3;</lolz>"
    )


def deeply_nested_xml(depth: int = 5000) -> bytes:
    """A few kilobytes that a recursive walk turns into a stack overflow."""
    return ('<?xml version="1.0"?>' + "<a>" * depth + "x" + "</a>" * depth).encode()


# --- text --------------------------------------------------------------


def markdown(sections: list[tuple[str, str]]) -> bytes:
    return "\n\n".join(f"# {title}\n\n{body}" for title, body in sections).encode()


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    return "\n".join(lines).encode()


# --- self-tests: the corpus has to actually be hostile ------------------


def test_bomb_is_small_but_expands() -> None:
    data = docx_bomb()
    declared = sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    assert len(data) < 64 * 1024
    assert declared > TEST_LIMITS.max_extracted_bytes * 10


def test_pdf_builder_produces_a_readable_pdf() -> None:
    from pypdf import PdfReader

    data = pdf(["first page", "second page"], {"Title": "Quarterly"})
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 2
    assert "second page" in (reader.pages[1].extract_text() or "")


def test_nested_xml_is_deeper_than_the_limit() -> None:
    assert deeply_nested_xml(100).count(b"<a>") > TEST_LIMITS.max_nesting_depth
