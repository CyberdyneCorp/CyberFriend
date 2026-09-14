"""Running a parser where it cannot hurt anything.

A parser cannot enforce its own timeout: the failure mode that matters is the
one where it stops returning to us at all. So parsing happens in a child
process, and the limit is enforced from outside it -- the child is killed when
the clock runs out, not asked to stop.

The cost is a process spawn per file, tens of milliseconds against an
ingestion path that is already doing network I/O. The benefit is that the
worst an uploaded file can do is consume its own process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

import chatmemory
from chatmemory.app.documents.model import (
    DocumentFormat,
    ExtractedDocument,
    ParseOutcome,
    SkipReason,
    TextSegment,
)
from chatmemory.app.documents.policy import ParsingLimits
from chatmemory.app.documents.ports import DocumentParser

log = structlog.get_logger()

WORKER_MODULE = "chatmemory.adapters.documents.worker"

# Grace after the wall-clock limit before the child is killed outright. Small,
# because a hung parser is exactly what this exists for.
KILL_GRACE_SECONDS = 1.0


class SubprocessParser:
    """A `DocumentParser` that parses in a killable child process."""

    def __init__(self, limits: ParsingLimits | None = None) -> None:
        self._limits = limits or ParsingLimits()

    async def parse(self, data: bytes, fmt: DocumentFormat, filename: str) -> ParseOutcome:
        """Parse one file, or report why it was skipped. Never raises."""
        if len(data) > self._limits.max_input_bytes:
            return ParseOutcome(skipped=SkipReason.TOO_LARGE)

        try:
            payload = await self._run_child(data, fmt)
        except TimeoutError:
            return ParseOutcome(skipped=SkipReason.TIMED_OUT)
        except Exception:  # noqa: BLE001 - a broken child is a skipped file
            log.warning("document.parser_crashed", filename=filename, format=fmt.value)
            return ParseOutcome(skipped=SkipReason.PARSER_FAILED)

        if payload is None:
            return ParseOutcome(skipped=SkipReason.PARSER_FAILED)
        return _decode_result(payload, fmt)

    async def _run_child(self, data: bytes, fmt: DocumentFormat) -> dict[str, Any] | None:
        header = {
            "format": fmt.value,
            "size": len(data),
            "max_input_bytes": self._limits.max_input_bytes,
            "max_extracted_bytes": self._limits.max_extracted_bytes,
            "max_seconds": self._limits.max_seconds,
            "max_memory_bytes": self._limits.max_memory_bytes,
            "max_nesting_depth": self._limits.max_nesting_depth,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_env(),
        )
        request = json.dumps(header).encode() + b"\n" + data
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(request), timeout=self._limits.max_seconds
            )
        except TimeoutError:
            await _kill(process)
            raise

        if process.returncode != 0 or not stdout.strip():
            # A crash, a signal (RLIMIT_AS and RLIMIT_CPU both arrive as one),
            # or a child that produced nothing. All the same to the caller.
            return None
        decoded: dict[str, Any] = json.loads(stdout)
        return decoded


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill, do not terminate: a parser stuck in C code ignores SIGTERM."""
    if process.returncode is not None:
        return
    process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
    except TimeoutError:  # pragma: no cover - defensive
        log.error("document.parser_unkillable", pid=process.pid)


def _child_env() -> dict[str, str]:
    """Give the child the package, and nothing it does not need.

    PYTHONPATH is set from this package's own location so the worker imports
    the same code as the parent whether the project is installed or run from a
    checkout.
    """
    package_root = str(Path(chatmemory.__file__).parent.parent)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_root}{os.pathsep}{existing}" if existing else package_root
    return env


def _decode_result(payload: dict[str, Any], fmt: DocumentFormat) -> ParseOutcome:
    if not payload.get("ok"):
        raw = payload.get("skipped") or SkipReason.PARSER_FAILED.value
        try:
            return ParseOutcome(skipped=SkipReason(raw))
        except ValueError:  # pragma: no cover - the child's vocabulary is ours
            return ParseOutcome(skipped=SkipReason.PARSER_FAILED)

    segments = tuple(
        TextSegment(text=str(s[0]), location=str(s[1]), heading_level=int(s[2]))
        for s in payload.get("segments", [])
    )
    metadata = tuple((str(k), str(v)) for k, v in payload.get("metadata", []))
    title = payload.get("title")
    return ParseOutcome(
        document=ExtractedDocument(
            media_type=fmt,
            segments=segments,
            title=str(title) if title else None,
            metadata=metadata,
        )
    )


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _parser_conforms(parser: SubprocessParser) -> DocumentParser:
        return parser
