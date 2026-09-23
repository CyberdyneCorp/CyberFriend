"""Position alerts on Discord: the Confirm button, and what `/alert list` shows.

A recognised alert request is answered with what would be watched and two
buttons. Only the asker can press them (`RequesterOnlyView`); Confirm creates
the alerts through `AlertRequests.confirm` and replaces the prompt with what
was created, Cancel replaces it with "nothing was created", and after five
minutes of neither the buttons go and the prompt says it expired. Every
outcome takes the buttons away, so a settled prompt cannot be pressed twice.

Where the prompt is shown follows where it was asked. A slash command's is
ephemeral. A direct message's is a reply in the DM, and only there does it
name the wallet. A mention in a channel gets a reply in the channel -- the
wallet left out, as a saved wallet is left out of every channel reply -- whose
buttons still answer to the asker alone.

`/alert list` and `/alert delete` are keyed on the interaction's user and
answered privately, like `/schedule`, in the language of the person's Discord
client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import discord
import structlog

from chatmemory.adapters.discord.views import RequesterOnlyView
from chatmemory.app.alert_requests import AlertProposal, target_label
from chatmemory.app.alerts import number
from chatmemory.ports.alerts import (
    AlertKind,
    AlertLanguage,
    AlertState,
    PositionAlert,
)

log = structlog.get_logger()

CONFIRM_WINDOW_SECONDS = 300.0
"""How long the buttons stay live. Longer than a tool approval's window: nothing
is waiting on this one, and reading what will be watched takes a moment."""

FAILING_AFTER = 12
"""Consecutive failed reads before a listing says an alert is failing: an
hour at the default sweep."""

EN, PT = AlertLanguage.ENGLISH, AlertLanguage.PORTUGUESE

_WORDS: dict[AlertLanguage, dict[str, str]] = {
    EN: {
        "confirm": "Confirm",
        "cancel": "Cancel",
        "not_yours": "Only the person who asked can confirm this.",
        "cancelled": "Cancelled. Nothing was created.",
        "expired": (
            "This request timed out and nothing was created. Ask again if you still want it."
        ),
        "failed": "I couldn't create those just now. Nothing was saved; ask again in a moment.",
        "unavailable": (
            "I can't manage alerts here - the feature isn't switched on for this deployment."
        ),
        "none": (
            "You have no alerts. Ask me, for example: `tell me when my LP goes out of "
            "range` or `alert me if my health factor drops below 1.3`."
        ),
        "heading": "**Your alerts ({count}):**",
        "since": "since",
        "checked": "last checked",
        "not_checked": "not checked yet",
        "messaged": "last message",
        "failing": "failing: {count} reads in a row could not be made",
        "stopped": "stopped - {reason}",
        "tail": "-# `/alert delete` stops one.",
        "deleted": "Deleted. I won't watch that any more.",
        "not_yours_alert": (
            "I don't have an alert with that number for you. `/alert list` shows yours."
        ),
        "hf": "HF {value}",
    },
    PT: {
        "confirm": "Confirmar",
        "cancel": "Cancelar",
        "not_yours": "Só quem pediu pode confirmar isto.",
        "cancelled": "Cancelado. Nada foi criado.",
        "expired": (
            "Este pedido expirou e nada foi criado. Peça de novo se ainda quiser."
        ),
        "failed": "Não consegui criar agora. Nada foi salvo; peça de novo em instantes.",
        "unavailable": (
            "Não consigo gerenciar alertas aqui - o recurso não está ligado nesta instalação."
        ),
        "none": (
            "Você não tem alertas. Me peça, por exemplo: `me avise quando minha posição "
            "sair da faixa` ou `me avisa se o health factor cair abaixo de 1,3`."
        ),
        "heading": "**Seus alertas ({count}):**",
        "since": "desde",
        "checked": "última verificação",
        "not_checked": "ainda não verificado",
        "messaged": "última mensagem",
        "failing": "com falha: {count} leituras seguidas não puderam ser feitas",
        "stopped": "parado - {reason}",
        "tail": "-# `/alert delete` remove um.",
        "deleted": "Apagado. Não vou mais acompanhar isso.",
        "not_yours_alert": (
            "Não tenho um alerta com esse número para você. `/alert list` mostra os seus."
        ),
        "hf": "HF {value}",
    },
}

_STATES: dict[AlertLanguage, dict[AlertState, str]] = {
    EN: {
        AlertState.UNKNOWN: "not read yet",
        AlertState.IN_RANGE: "in range",
        AlertState.OUT_OF_RANGE: "out of range",
        AlertState.CLOSED: "closed",
        AlertState.OK: "above the limit",
        AlertState.BELOW: "below the limit",
        AlertState.NO_DEBT: "no debt",
    },
    PT: {
        AlertState.UNKNOWN: "ainda não lido",
        AlertState.IN_RANGE: "dentro da faixa",
        AlertState.OUT_OF_RANGE: "fora da faixa",
        AlertState.CLOSED: "fechada",
        AlertState.OK: "acima do limite",
        AlertState.BELOW: "abaixo do limite",
        AlertState.NO_DEBT: "sem dívida",
    },
}

_REASONS: dict[AlertLanguage, dict[str, str]] = {
    PT: {
        "position closed": "posição fechada",
        "direct messages are closed": "suas mensagens diretas estão fechadas",
    },
}


def word(key: str, language: AlertLanguage, **values: object) -> str:
    return _WORDS[language][key].format(**values)


def locale_language(locale: object) -> AlertLanguage:
    """Portuguese for a Portuguese Discord client, English otherwise."""
    return PT if str(getattr(locale, "value", locale)).lower().startswith("pt") else EN


# --- the confirmation ------------------------------------------------------------


class _EditableMessage(Protocol):
    async def edit(self, *, content: str, view: discord.ui.View) -> object: ...


Confirm = Callable[[AlertProposal], Awaitable[str]]


class AlertConfirmView(RequesterOnlyView):
    """Confirm and Cancel for one proposal, answerable by its asker alone."""

    def __init__(
        self,
        proposal: AlertProposal,
        confirm: Confirm,
        timeout: float = CONFIRM_WINDOW_SECONDS,
    ) -> None:
        language = proposal.language
        super().__init__(
            proposal.person.platform_user_id, word("not_yours", language), timeout=timeout
        )
        self._proposal = proposal
        self._confirm = confirm
        self._message: _EditableMessage | None = None
        self._settled = False
        self.confirm_button.label = word("confirm", language)
        self.cancel_button.label = word("cancel", language)

    def sent_as(self, message: _EditableMessage | None) -> None:
        """Remember what the prompt was posted as, so it can be retired."""
        self._message = message

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        if not self._settle():
            return
        try:
            note = await self._confirm(self._proposal)
        except Exception:  # noqa: BLE001 - the press is answered whatever happened
            log.exception("alerts.confirm_failed", person=str(self._proposal.person))
            note = word("failed", self._proposal.language)
        await self._redraw(interaction, note)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        if not self._settle():
            return
        log.info("alerts.cancelled", person=str(self._proposal.person))
        await self._redraw(interaction, word("cancelled", self._proposal.language))

    def _settle(self) -> bool:
        """Take the buttons away; False if they had already been answered."""
        if self._settled:
            return False
        self._settled = True
        self.disable_buttons()
        self.stop()
        return True

    async def _redraw(self, interaction: discord.Interaction, note: str) -> None:
        try:
            await interaction.response.edit_message(content=note, view=self)
        except discord.HTTPException:
            log.info("alerts.confirmation_not_redrawn")

    async def on_timeout(self) -> None:
        """Retire a prompt nobody answered: live buttons would promise a watch."""
        if self._settled:
            return
        self._settled = True
        self.disable_buttons()
        if self._message is None:
            return
        try:
            await self._message.edit(
                content=word("expired", self._proposal.language), view=self
            )
        except discord.HTTPException:
            log.info("alerts.expiry_not_shown")


async def reply_with_confirmation(
    message: discord.Message, proposal: AlertProposal, confirm: Confirm
) -> None:
    """The prompt as a reply to a mention or a DM, where it was asked."""
    view = AlertConfirmView(proposal, confirm)
    sent = await message.reply(
        proposal.text,
        view=view,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.sent_as(sent)


async def follow_up_with_confirmation(
    interaction: discord.Interaction, proposal: AlertProposal, confirm: Confirm
) -> None:
    """The prompt for `/ask`: ephemeral, so only the asker ever sees it.

    `/ask` deferred publicly before it knew this was an alert request, and the
    first followup after a defer takes that deferral's visibility. So the
    public "thinking" message is deleted first; the followup after that is a
    message of its own, and ephemeral.
    """
    view = AlertConfirmView(proposal, confirm)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        log.info("alerts.thinking_not_deleted")
    sent = await interaction.followup.send(
        proposal.text,
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.sent_as(sent)


# --- the listing ------------------------------------------------------------------


def _when(moment: object) -> str:
    return discord.utils.format_dt(moment, "R") if moment is not None else ""  # type: ignore[arg-type]


def _state(alert: PositionAlert, language: AlertLanguage) -> str:
    state = _STATES[language][alert.state]
    if alert.kind is AlertKind.AAVE_HEALTH and alert.last_value is not None:
        state += f", {word('hf', language, value=number(alert.last_value, language))}"
    return f"{state} {word('since', language)} {_when(alert.state_since)}"


def _status(alert: PositionAlert, language: AlertLanguage) -> str:
    """Whether it is working, so silence is legible, as `/schedule list` does."""
    if not alert.active:
        reason = _REASONS.get(language, {}).get(alert.disabled_reason, alert.disabled_reason)
        return word("stopped", language, reason=reason or "-")
    if alert.consecutive_failures >= FAILING_AFTER:
        return word("failing", language, count=alert.consecutive_failures)
    parts = [
        f"{word('checked', language)} {_when(alert.last_checked_at)}"
        if alert.last_checked_at
        else word("not_checked", language)
    ]
    if alert.last_fired_at is not None:
        parts.append(f"{word('messaged', language)} {_when(alert.last_fired_at)}")
    return " · ".join(parts)


def alert_listing(alerts: Sequence[PositionAlert], language: AlertLanguage) -> str:
    if not alerts:
        return word("none", language)
    lines = [word("heading", language, count=len(alerts))]
    for alert in alerts:
        label = target_label(alert.kind, alert.chain, alert.lp, alert.threshold, language)
        lines.append(f"**{alert.id}** - {label} - {_state(alert, language)}")
        lines.append(f"  {_status(alert, language)}")
    lines.append(word("tail", language))
    return "\n".join(lines)
