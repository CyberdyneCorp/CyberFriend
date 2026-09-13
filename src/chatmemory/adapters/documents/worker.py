"""The child process that actually parses an untrusted file.

Run as `python -m chatmemory.adapters.documents.worker`. It reads one request
from stdin, writes one JSON result to stdout, and exits.

Everything about this process is disposable, which is the point. It sets its
own address-space and CPU limits before touching the file, so a parser that
allocates without bound dies here rather than taking the ingester with it, and
its parent kills it outright when the wall clock runs out. A crash in this
process costs one attachment.

It also parses with stdout closed to anything but the result: a library that
prints a warning to stdout would otherwise corrupt the response frame, so the
real stdout is captured and handed back only for the JSON.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from typing import Any

from chatmemory.app.documents.model import DocumentFormat, SkipReason
from chatmemory.app.documents.policy import ParsingLimits

FAILURE = {"ok": False, "skipped": SkipReason.PARSER_FAILED.value}


def _apply_limits(limits: ParsingLimits) -> None:
    """Bound this process before it reads anything.

    RLIMIT_AS caps address space, which is what an allocation bomb exhausts.
    RLIMIT_CPU is a backstop for a parser that spins without allocating: the
    parent's wall-clock kill is the primary control, but a CPU limit turns a
    spin into a signal even if the parent is somehow not watching.
    """
    for which, value in (
        (resource.RLIMIT_AS, limits.max_memory_bytes),
        (resource.RLIMIT_CPU, max(1, int(limits.max_seconds) + 1)),
    ):
        try:
            soft, hard = resource.getrlimit(which)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (ceiling, hard))
        except (ValueError, OSError):
            # A platform that refuses the limit still gets the wall-clock kill
            # and the process boundary; losing one of four controls is not a
            # reason to refuse to parse.
            continue


def _read_request() -> tuple[dict[str, Any], bytes]:
    stdin = sys.stdin.buffer
    header_line = stdin.readline()
    header: dict[str, Any] = json.loads(header_line)
    size = int(header["size"])
    return header, stdin.read(size)


def _limits_from(header: dict[str, Any]) -> ParsingLimits:
    return ParsingLimits(
        max_input_bytes=int(header["max_input_bytes"]),
        max_extracted_bytes=int(header["max_extracted_bytes"]),
        max_seconds=float(header["max_seconds"]),
        max_memory_bytes=int(header["max_memory_bytes"]),
        max_nesting_depth=int(header["max_nesting_depth"]),
    )


def _run() -> dict[str, Any]:
    header, data = _read_request()
    limits = _limits_from(header)
    _apply_limits(limits)

    # Imported after the limits are in place, so even the import is bounded.
    from chatmemory.adapters.documents import parsers

    outcome = parsers.parse(data, DocumentFormat(header["format"]), limits)
    if outcome.document is None:
        reason = outcome.skipped or SkipReason.PARSER_FAILED
        return {"ok": False, "skipped": reason.value}
    document = outcome.document
    return {
        "ok": True,
        "media_type": document.media_type.value,
        "title": document.title,
        "metadata": [list(pair) for pair in document.metadata],
        "segments": [[s.text, s.location, s.heading_level] for s in document.segments],
    }


def main() -> int:
    # Anything a parser prints would land in the middle of the response frame.
    # The result goes to the real stdout; everything else goes to stderr.
    real_stdout = os.dup(1)
    os.dup2(2, 1)
    try:
        result = _run()
    except MemoryError:
        result = {"ok": False, "skipped": SkipReason.EXTRACTED_TOO_LARGE.value}
    except Exception:  # noqa: BLE001 - the parent reads a result, never a traceback
        result = dict(FAILURE)
    with os.fdopen(real_stdout, "w") as out:
        json.dump(result, out)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
