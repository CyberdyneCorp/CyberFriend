"""Telling a person, once, that their questions and answers are recorded.

Traces hold the question as asked and the answer as sent, are kept up to
`TRACE_RETENTION_DAYS`, and admins can read them. The capabilities reply says
so, but nobody reads that before asking, so the first traced reply a person
gets carries the same statement as a notice. It is shown once per
`NOTICE_VERSION`: bump the version when the statement changes (a different
retention period, a wider audience) and everybody is told again, once.

Only where it is true. A deployment without tracing builds no notice, and a
person who opted out is never exported, so the store refuses to record a
notice for them and none is shown. A failed store read shows nothing: a
notice is not worth an answer.
"""

from __future__ import annotations

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.tracing import TracingNoticeStore

log = structlog.get_logger()

NOTICE_VERSION = 1
"""The notice a person was last shown is recorded against this. Raise it when
the statement below changes in substance."""

_RECORDING = {
    Language.ENGLISH: (
        "Your questions and my answers are recorded for up to {days}, and "
        "CyberFriend admins can read them. Use `/privacy` to see how many are kept "
        "or to delete them."
    ),
    Language.PORTUGUESE: (
        "Suas perguntas e minhas respostas ficam registradas por até {days}, e "
        "os administradores do CyberFriend podem lê-las. Use `/privacy` para "
        "ver quantas estão guardadas ou apagá-las."
    ),
}

_DAYS = {
    Language.ENGLISH: ("day", "days"),
    Language.PORTUGUESE: ("dia", "dias"),
}

_NOTICE_LABEL = {Language.ENGLISH: "Note", Language.PORTUGUESE: "Aviso"}


def _known(language: Language) -> Language:
    return Language.PORTUGUESE if language is Language.PORTUGUESE else Language.ENGLISH


def recording_statement(language: Language, retention_days: int) -> str:
    """What is recorded, for how long, who can read it, and how to remove it."""
    key = _known(language)
    one, many = _DAYS[key]
    days = f"{retention_days} {one if retention_days == 1 else many}"
    return _RECORDING[key].format(days=days)


def notice_text(language: Language, retention_days: int) -> str:
    """The notice appended to a reply: the statement, labelled as a notice."""
    key = _known(language)
    return f"**{_NOTICE_LABEL[key]}:** {recording_statement(key, retention_days)}"


class TracingNotice:
    """Decides whether a reply carries the notice, and records that it did."""

    def __init__(
        self,
        store: TracingNoticeStore,
        retention_days: int,
        clock: Clock = utc_now,
        version: int = NOTICE_VERSION,
    ) -> None:
        self._store = store
        self._retention_days = retention_days
        self._clock = clock
        self._version = version

    async def due(self, person: PersonRef) -> bool:
        """True once per person and version, recording it; never raises."""
        try:
            return await self._store.claim_notice(person, self._version, self._clock())
        except Exception as exc:  # noqa: BLE001 - a notice never costs a reply
            log.warning("tracing.notice_failed", person=str(person), error=str(exc))
            return False

    def text(self, language: Language) -> str:
        return notice_text(language, self._retention_days)
