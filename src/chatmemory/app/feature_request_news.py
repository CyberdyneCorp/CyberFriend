"""Telling a person their suggestion's status changed, when they asked to be told.

The admin console changes a status; it cannot message anybody. This sweep runs
in the bot process, which can, and sends one direct message per status change
to the author of a suggestion who answered Yes to "tell you when its status
changes?". Nobody else is ever messaged:

*   **Opt-in only.** A row is news only when `notify_on_change` is set, and
    answering Yes starts `notified_status` at the status they already know,
    so the first message is about the first change.
*   **Once per change.** A row is claimed by advancing `notified_status` to
    the status it is about to announce, before the message is sent. The next
    sweep finds nothing new until an admin changes the status again.
*   **Never where they said no.** Opted-out people, people who turned
    notifications off and people whose direct messages are closed are not
    claimed. Opting out and deleting everything purge the rows themselves.

An unsent message is put back: a transient failure is retried on the next
sweep, and a closed direct message records the person as undeliverable (the
same record `/notifications` keeps), which leaves their news unclaimed until
they turn notifications back on.
"""

from __future__ import annotations

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.feature_requests import status_label
from chatmemory.app.language import Language
from chatmemory.app.schedules import TaskMessenger
from chatmemory.ports.feature_requests import StatusNews, StatusNewsStore
from chatmemory.ports.notifications import DeliveryResult

log = structlog.get_logger()

SWEEP_INTERVAL_SECONDS = 600.0
"""How often the bot looks for status changes to announce."""

BATCH = 50
"""Messages per sweep at most; the rest wait for the next one."""

QUOTED_TEXT_CHARS = 200
"""How much of their own suggestion the message quotes back to them."""

_MESSAGE = {
    Language.ENGLISH: (
        "Your suggestion **#{id}** is now **{status}**.\n> {text}\n"
        "`/suggestions` lists all of yours."
    ),
    Language.PORTUGUESE: (
        "Sua sugestão **#{id}** agora está: **{status}**.\n> {text}\n"
        "`/suggestions` mostra todas as suas."
    ),
}


def _language(code: str | None) -> Language:
    return Language.PORTUGUESE if code == "pt" else Language.ENGLISH


def _clip(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= QUOTED_TEXT_CHARS:
        return one_line
    return one_line[: QUOTED_TEXT_CHARS - 1] + "…"


def status_message(news: StatusNews) -> str:
    """The direct message, in the suggestion's own language."""
    language = _language(news.language)
    return _MESSAGE[language].format(
        id=news.request_id,
        status=status_label(news.status, language),
        text=_clip(news.text),
    )


class StatusNewsRunner:
    """One sweep: claim the changes to announce, send each, put back the unsent."""

    def __init__(
        self,
        store: StatusNewsStore,
        messenger: TaskMessenger,
        clock: Clock = utc_now,
        batch: int = BATCH,
    ) -> None:
        self._store = store
        self._messenger = messenger
        self._clock = clock
        self._batch = batch

    async def run_due(self) -> int:
        """Send what is due; how many messages arrived."""
        sent = 0
        for news in await self._store.claim_status_news(self._batch):
            result = await self._messenger.deliver(
                news.person, news.request_id, status_message(news)
            )
            if result is DeliveryResult.SENT:
                sent += 1
                continue
            await self._store.release(news)
            if result is DeliveryResult.CLOSED:
                await self._store.record_undeliverable(news.person, self._clock())
            log.info(
                "suggestions.status_news_not_sent",
                request_id=news.request_id,
                result=str(result),
            )
        return sent
