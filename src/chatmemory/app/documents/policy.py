"""The limits and allowlists that make parsing untrusted uploads survivable.

Every value here is a bound on something an uploader controls: how many bytes
they can make us read, how much text those bytes can expand into, how long we
will spend, and which formats we are willing to look at in the first place.

The defaults are deliberately small. A limit that is never hit costs nothing;
a limit set high enough to be comfortable is not a limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from chatmemory.app.documents.model import DocumentFormat

MIB = 1024 * 1024

# OAuth scopes that read more than what has been shared with the team. A
# credential holding any of these turns "post a link" into "make the bot read
# a private document aloud", which needs no access from the attacker at all.
OVERBROAD_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata",
        "drive",
        "drive.readonly",
        "notion.workspace",
        "workspace.read_all",
        "admin",
        "domain_wide_delegation",
    }
)


class CredentialScopeError(ValueError):
    """Raised when a configured credential reads wider than the team can.

    A configuration error rather than a runtime one: this is meant to stop a
    deployment, because every other control in this module is irrelevant if
    the bot can read what nobody in the channel could.
    """


@dataclass(frozen=True, slots=True)
class ParsingLimits:
    """Hard bounds on one parse. All four are enforced, not just size.

    Size alone does not bound extraction: a few hundred kilobytes of zip can
    decompress into gigabytes, and a small malformed PDF can loop a parser
    forever. So extracted size, wall time and address space are bounded too,
    and the wall-time bound is enforced by killing rather than waiting.
    """

    max_input_bytes: int = 16 * MIB
    max_extracted_bytes: int = 8 * MIB
    max_seconds: float = 20.0
    max_memory_bytes: int = 512 * MIB
    max_nesting_depth: int = 20
    max_archive_entries: int = 512
    # Ratio of extracted bytes to input bytes past which a file is an archive
    # bomb rather than an unusually compressible document.
    max_expansion_ratio: float = 200.0


@dataclass(frozen=True, slots=True)
class ChunkingLimits:
    max_tokens: int = 400
    overlap_tokens: int = 60
    min_tokens: int = 24


@dataclass(frozen=True, slots=True)
class ExternalSource:
    """One external system we are willing to follow links into.

    `hosts` is matched against a link's host exactly or as a suffix on a dot
    boundary, so `drive.google.com` never matches `drive.google.com.evil.test`.

    `shared_containers` is the escalation control: a fetched document must
    report membership of one of these (a shared drive, a folder, a workspace
    the team already has). A document the credential can read but that lives
    outside them is discarded as if it did not exist.
    """

    name: str
    hosts: frozenset[str]
    scopes: frozenset[str] = frozenset()
    shared_containers: frozenset[str] = frozenset()

    def matches(self, url: str) -> bool:
        host = urlsplit(url).hostname
        if host is None or urlsplit(url).scheme not in ("http", "https"):
            return False
        host = host.lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def permits_container(self, containers: frozenset[str]) -> bool:
        """Whether a fetched document lives somewhere the team already reaches."""
        if not self.shared_containers:
            # No declared shared scope means nothing is in scope. Fail closed:
            # an empty allowlist that meant "everything" would be the bug.
            return False
        return bool(containers & self.shared_containers)


@dataclass(frozen=True, slots=True)
class ExternalPolicy:
    """Whether, and where, we follow links out of Discord.

    Disabled by default. Fetching external documents adds a second system's
    contents to the archive and a credential that can reach them, and neither
    should appear because someone forgot to opt out.
    """

    enabled: bool = False
    sources: tuple[ExternalSource, ...] = ()
    max_fetch_bytes: int = 16 * MIB
    max_fetch_seconds: float = 15.0
    max_attempts: int = 3

    def source_for(self, url: str) -> ExternalSource | None:
        """The configured source a URL belongs to, or None.

        None means "do not fetch". There is no fallback branch that fetches an
        unmatched URL, which is the point: arbitrary URLs are not reachable
        from this code path at all.
        """
        if not self.enabled:
            return None
        return next((s for s in self.sources if s.matches(url)), None)

    def verify_credentials(self) -> None:
        """Reject a credential scoped wider than the team's own access.

        Called at construction of the ingestion service rather than at first
        fetch, so an over-scoped deployment fails to start instead of failing
        quietly at the moment it reads something it should not have.
        """
        for source in self.sources:
            overbroad = source.scopes & OVERBROAD_SCOPES
            if overbroad:
                raise CredentialScopeError(
                    f"{source.name}: credential scopes read more than the team can: "
                    f"{sorted(overbroad)}"
                )
            if not source.shared_containers:
                raise CredentialScopeError(
                    f"{source.name}: no shared containers configured; the credential's "
                    "reach is therefore unbounded relative to the team's"
                )


@dataclass(frozen=True, slots=True)
class DocumentPolicy:
    """Everything ingestion is allowed to do with an untrusted file."""

    allowed_formats: frozenset[DocumentFormat] = frozenset(DocumentFormat)
    limits: ParsingLimits = field(default_factory=ParsingLimits)
    chunking: ChunkingLimits = field(default_factory=ChunkingLimits)
    external: ExternalPolicy = field(default_factory=ExternalPolicy)
    max_attachment_attempts: int = 3

    def allows(self, fmt: DocumentFormat) -> bool:
        return fmt in self.allowed_formats


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_set(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    return frozenset(p for p in raw.replace(",", " ").split() if p)


def policy_from_env() -> DocumentPolicy:
    """Build a policy from the environment.

    Kept here rather than in `Settings` because these values are a parsing
    threat model, not general configuration, and because the global switch
    below has to default to off no matter what else is configured.
    """
    formats = _env_set("DOCUMENT_ALLOWED_FORMATS")
    allowed = (
        frozenset(DocumentFormat(f) for f in formats) if formats else frozenset(DocumentFormat)
    )
    sources: list[ExternalSource] = []
    for name in sorted(_env_set("EXTERNAL_DOCUMENT_SOURCES")):
        prefix = f"EXTERNAL_{name.upper()}_"
        sources.append(
            ExternalSource(
                name=name,
                hosts=_env_set(prefix + "HOSTS"),
                scopes=_env_set(prefix + "SCOPES"),
                shared_containers=_env_set(prefix + "CONTAINERS"),
            )
        )
    return DocumentPolicy(
        allowed_formats=allowed,
        limits=ParsingLimits(
            max_input_bytes=_env_int("DOCUMENT_MAX_INPUT_BYTES", 16 * MIB),
            max_extracted_bytes=_env_int("DOCUMENT_MAX_EXTRACTED_BYTES", 8 * MIB),
            max_seconds=_env_float("DOCUMENT_MAX_PARSE_SECONDS", 20.0),
            max_memory_bytes=_env_int("DOCUMENT_MAX_MEMORY_BYTES", 512 * MIB),
        ),
        chunking=ChunkingLimits(
            max_tokens=_env_int("DOCUMENT_CHUNK_MAX_TOKENS", 400),
            overlap_tokens=_env_int("DOCUMENT_CHUNK_OVERLAP_TOKENS", 60),
        ),
        external=ExternalPolicy(
            # Off unless explicitly enabled: see ExternalPolicy.
            enabled=_env_bool("EXTERNAL_DOCUMENTS_ENABLED", False),
            sources=tuple(sources),
            max_fetch_bytes=_env_int("EXTERNAL_MAX_FETCH_BYTES", 16 * MIB),
            max_fetch_seconds=_env_float("EXTERNAL_MAX_FETCH_SECONDS", 15.0),
        ),
    )
