"""Feature requests on Discord: what `/suggest` and `/suggestions` say.

A recorded suggestion is acknowledged with its number, a plain statement that
the team will see the text and the person's Discord name, and two buttons
asking whether to message them when its status changes. Only the author can
press them (`RequesterOnlyView`), every press takes the buttons away, and
after the window closes they go without changing anything: no answer means no
messages, which is the default.

Every reply is private, in the language of the suggestion, else the caller's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import discord
import structlog

from chatmemory.adapters.discord.views import RequesterOnlyView
from chatmemory.app.feature_requests import ContactKind, SubmitOutcome, SubmitResult
from chatmemory.app.language import Language
from chatmemory.ports.feature_requests import (
    DEFAULT_DAILY_LIMIT,
    MAX_TEXT_CHARS,
    FeatureRequest,
    RequestStatus,
)

log = structlog.get_logger()

NOTIFY_WINDOW_SECONDS = 300.0
"""How long the [Yes] [No] buttons stay live."""

LISTED_TEXT_CHARS = 120
"""How much of each suggestion `/suggestions` shows, so the reply fits."""

EN, PT = Language.ENGLISH, Language.PORTUGUESE

_TEXT: dict[str, dict[Language, str]] = {
    "recorded": {
        EN: (
            "Recorded as **#{id}**. The team will see your text and your Discord name.\n"
            "Tell you when its status changes?"
        ),
        PT: (
            "Registrada como **#{id}**. A equipe vai ver o seu texto e o seu nome no "
            "Discord.\nQuer que eu te avise quando o status mudar?"
        ),
    },
    "notify_on": {
        EN: "Recorded as **#{id}**. I'll send you a direct message when its status changes.",
        PT: "Registrada como **#{id}**. Te mando uma mensagem direta quando o status mudar.",
    },
    "notify_off": {
        EN: "Recorded as **#{id}**. I won't message you about it.",
        PT: "Registrada como **#{id}**. Não vou te mandar mensagens sobre ela.",
    },
    "already": {
        EN: "You already suggested that: it's recorded as **#{id}**.",
        PT: "Você já sugeriu isso: está registrada como **#{id}**.",
    },
    "empty": {
        EN: "Tell me what you'd like me to be able to do.",
        PT: "Me diga o que você gostaria que eu soubesse fazer.",
    },
    "too_long": {
        EN: "That's longer than I can keep ({limit} characters). Please shorten it.",
        PT: "Isso é mais longo do que consigo guardar ({limit} caracteres). Encurte, por favor.",
    },
    "contact": {
        EN: (
            "I didn't record that: it contains {what}, and the team reads every "
            "suggestion. Please send it again without it."
        ),
        PT: (
            "Não registrei: o texto contém {what}, e a equipe lê todas as sugestões. "
            "Mande de novo sem isso, por favor."
        ),
    },
    "limited": {
        EN: (
            "You've made {limit} suggestions in the last 24 hours, which is as many "
            "as I take. Nothing was recorded; try again tomorrow."
        ),
        PT: (
            "Você já fez {limit} sugestões nas últimas 24 horas, que é o máximo que "
            "eu aceito. Nada foi registrado; tente de novo amanhã."
        ),
    },
    "opted_out": {
        EN: "You've opted out, so I don't keep anything from you, suggestions included.",
        PT: "Você optou por sair, então não guardo nada seu, nem sugestões.",
    },
    "unavailable": {
        EN: "I can't take suggestions here - there's no storage for them in this deployment.",
        PT: "Não consigo receber sugestões aqui - esta instalação não tem onde guardá-las.",
    },
    "none": {
        EN: "You haven't suggested anything yet. `/suggest` records an idea.",
        PT: "Você ainda não sugeriu nada. `/suggest` registra uma ideia.",
    },
    "heading": {
        EN: "**Your suggestions ({count}):**",
        PT: "**Suas sugestões ({count}):**",
    },
    "yes": {EN: "Yes", PT: "Sim"},
    "no": {EN: "No", PT: "Não"},
    "not_yours": {
        EN: "Only the person who made this suggestion can answer this.",
        PT: "Só quem fez esta sugestão pode responder isto.",
    },
}

_CONTACTS: dict[Language, dict[ContactKind, str]] = {
    EN: {
        ContactKind.EMAIL: "an email address",
        ContactKind.PHONE: "a phone number",
        ContactKind.WALLET: "a wallet address",
        ContactKind.CONTACT: "your contact details",
    },
    PT: {
        ContactKind.EMAIL: "um endereço de e-mail",
        ContactKind.PHONE: "um número de telefone",
        ContactKind.WALLET: "um endereço de carteira",
        ContactKind.CONTACT: "seus dados de contato",
    },
}

_STATUSES: dict[Language, dict[RequestStatus, str]] = {
    EN: {
        RequestStatus.NEW: "new",
        RequestStatus.TRIAGED: "triaged",
        RequestStatus.PLANNED: "planned",
        RequestStatus.DONE: "done",
        RequestStatus.DECLINED: "declined",
        RequestStatus.DUPLICATE: "duplicate",
    },
    PT: {
        RequestStatus.NEW: "nova",
        RequestStatus.TRIAGED: "em análise",
        RequestStatus.PLANNED: "planejada",
        RequestStatus.DONE: "feita",
        RequestStatus.DECLINED: "recusada",
        RequestStatus.DUPLICATE: "duplicada",
    },
}


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


def text(key: str, language: Language, **values: object) -> str:
    return _TEXT[key][_lang(language)].format(**values)


def status_label(status: RequestStatus, language: Language) -> str:
    return _STATUSES[_lang(language)][status]


_OUTCOME_KEYS = {
    SubmitOutcome.ALREADY_RECORDED: "already",
    SubmitOutcome.EMPTY: "empty",
    SubmitOutcome.TOO_LONG: "too_long",
    SubmitOutcome.LIMITED: "limited",
    SubmitOutcome.OPTED_OUT: "opted_out",
}


def submitted_message(
    result: SubmitResult, language: Language, daily_limit: int = DEFAULT_DAILY_LIMIT
) -> str:
    """The reply to a submission. A recorded one is `text("recorded")`."""
    if result.outcome is SubmitOutcome.RECORDED:
        return text("recorded", language, id=result.request_id)
    if result.outcome is SubmitOutcome.CONTAINS_CONTACT:
        kind = result.contact or ContactKind.CONTACT
        return text("contact", language, what=_CONTACTS[_lang(language)][kind])
    return text(
        _OUTCOME_KEYS[result.outcome],
        language,
        id=result.request_id,
        limit=daily_limit if result.outcome is SubmitOutcome.LIMITED else MAX_TEXT_CHARS,
    )


def _clip(value: str) -> str:
    return value if len(value) <= LISTED_TEXT_CHARS else value[: LISTED_TEXT_CHARS - 1] + "…"


def suggestion_listing(requests: Sequence[FeatureRequest], language: Language) -> str:
    if not requests:
        return text("none", language)
    lines = [text("heading", language, count=len(requests))]
    lines += [
        f"**#{r.id}** - {status_label(r.status, language)} - {_clip(r.text)}" for r in requests
    ]
    return "\n".join(lines)


# --- the notify question -----------------------------------------------------------


class _EditableMessage(Protocol):
    async def edit(self, *, content: str, view: discord.ui.View) -> object: ...


SetNotify = Callable[[int, bool], Awaitable[bool]]
"""Records the answer for one suggestion: (request id, notify)."""


class NotifyChoiceView(RequesterOnlyView):
    """[Yes] [No] under an acknowledgement, answerable by its author alone."""

    def __init__(
        self,
        requester_id: int,
        request_id: int,
        language: Language,
        set_notify: SetNotify,
        timeout: float = NOTIFY_WINDOW_SECONDS,
    ) -> None:
        super().__init__(requester_id, text("not_yours", language), timeout=timeout)
        self._request_id = request_id
        self._language = language
        self._set_notify = set_notify
        self._message: _EditableMessage | None = None
        self._settled = False
        self.yes_button.label = text("yes", language)
        self.no_button.label = text("no", language)

    def sent_as(self, message: _EditableMessage | None) -> None:
        self._message = message

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.primary)
    async def yes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._answer(interaction, notify=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._answer(interaction, notify=False)

    async def _answer(self, interaction: discord.Interaction, *, notify: bool) -> None:
        if self._settled:
            return
        self._settled = True
        self.disable_buttons()
        self.stop()
        if notify:
            # Stored only on Yes: the column defaults to no messages.
            await self._set_notify(self._request_id, True)
        note = text("notify_on" if notify else "notify_off", self._language, id=self._request_id)
        try:
            await interaction.response.edit_message(content=note, view=self)
        except discord.HTTPException:
            log.info("suggestions.notify_not_redrawn")

    async def on_timeout(self) -> None:
        """No answer is No: take the buttons away and say so."""
        if self._settled:
            return
        self._settled = True
        self.disable_buttons()
        if self._message is None:
            return
        try:
            await self._message.edit(
                content=text("notify_off", self._language, id=self._request_id), view=self
            )
        except discord.HTTPException:
            log.info("suggestions.expiry_not_shown")
